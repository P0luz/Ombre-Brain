"""
========================================
web/search.py — 检索 / 重复 / 概念网络 / breath 调试
========================================

- /api/search：关键词+向量检索
- /api/duplicates：重复候选 pair（记忆健康面板）
- /api/network：概念网络图（wikilink + tag 共现）
- /api/breath、/api/breath-debug：breath 浮现结果 / 四维评分分解

对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from ombrebrain.policy.surfacing import SurfacePolicyVM
from tools._common import check_query_size
from tools.plan.core import letter_lock_state
from . import _shared as sh

logger = sh.logger
_SURFACE_POLICY = SurfacePolicyVM.default()

try:
    from utils import strip_wikilinks, extract_wikilinks  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks, extract_wikilinks  # type: ignore


_SEMANTIC_DISABLED_NOTE = "语义索引暂不可用，本次仅使用关键词/BM25"


async def _semantic_scores_for_dashboard(
    query: str,
    top_k: int,
    *,
    allowed_bucket_ids: set[str] | None,
) -> tuple[dict[str, float], str]:
    """显式跑一次向量查询，失败时给出可读原因。

    跟 tools/breath/search.py 的 _semantic_scores 同一个套路——如果不显式跑
    这一步、直接把裸 query 扔给 bucket_mgr.search()，它内部会自己悄悄尝试
    embedding_engine.search_similar()，失败只打一行 warning 日志就吞掉异常，
    调用方（这里是 Dashboard）完全看不出这次检索到底有没有真的用上语义通道。
    """
    engine = sh.embedding_engine
    if not engine or not getattr(engine, "enabled", False):
        return {}, _SEMANTIC_DISABLED_NOTE
    try:
        strict_search = getattr(engine, "search_similar_strict", None)
        if callable(strict_search) and allowed_bucket_ids is not None:
            pairs = await strict_search(
                query,
                top_k=top_k,
                allowed_bucket_ids=allowed_bucket_ids,
            )
        elif callable(strict_search):
            pairs = await strict_search(query, top_k=top_k)
        elif allowed_bucket_ids is not None:
            pairs = await engine.search_similar(
                query,
                top_k=top_k,
                allowed_bucket_ids=allowed_bucket_ids,
            )
        else:
            pairs = await engine.search_similar(query, top_k=top_k)
        return {bucket_id: float(score) for bucket_id, score in pairs}, ""
    except Exception as exc:
        logger.warning(
            f"/api/search semantic search failed; using keyword/BM25 only: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}, _SEMANTIC_DISABLED_NOTE


def _unit_query_float(raw: str | None, name: str) -> float | None:
    if raw in (None, ""):
        return None
    import math

    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number in [0,1]") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be a finite number in [0,1]")
    return value


def register(mcp) -> None:

    @mcp.custom_route("/api/search", methods=["GET"])
    async def api_search(request: Request) -> Response:
        """Search buckets by query."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        query = request.query_params.get("q", "")
        if not query:
            return JSONResponse({"error": "missing q parameter"}, status_code=400)
        query_error = check_query_size(query)
        if query_error:
            return JSONResponse({"error": query_error}, status_code=400)
        try:
            list_all = getattr(sh.bucket_mgr, "list_all", None)
            if callable(list_all):
                all_buckets = await list_all(include_archive=False)
                allowed_bucket_ids = {
                    str(bucket.get("id")) for bucket in all_buckets
                    if not letter_lock_state(bucket, "human")["locked"]
                }
            else:  # pragma: no cover - 兼容极简第三方 manager
                allowed_bucket_ids = None
            vector_scores, semantic_notice = await _semantic_scores_for_dashboard(
                query,
                top_k=50,
                allowed_bucket_ids=allowed_bucket_ids,
            )
            if allowed_bucket_ids is None:
                matches = await sh.bucket_mgr.search(
                    query, limit=50, vector_scores=vector_scores
                )
            else:
                matches = await sh.bucket_mgr.search(
                    query,
                    limit=50,
                    vector_scores=vector_scores,
                    allowed_bucket_ids=allowed_bucket_ids,
                )
            result = []
            for b in matches:
                if letter_lock_state(b, "human")["locked"]:
                    continue
                if not _SURFACE_POLICY.evaluate_bucket(b, mode="search").allowed:
                    continue
                meta = b.get("metadata", {})
                result.append({
                    "id": b["id"],
                    "name": meta.get("name", b["id"]),
                    "score": b.get("score", 0),
                    "domain": meta.get("domain", []),
                    "valence": meta.get("valence", 0.5),
                    "arousal": meta.get("arousal", 0.3),
                    "content_preview": strip_wikilinks(b.get("content", ""))[:200],
                })
                if len(result) >= 10:
                    break
            response = JSONResponse(result)
            # 响应体保持纯数组不变（前端 Array.isArray(results) 依赖这个形状），
            # 语义检索是否降级改用响应头传递——不破坏现有调用方，同时让「离线要
            # 明确提示」这条约定在这个接口上也真正可核实，而不是只在日志里。
            response.headers["X-Semantic-Search"] = (
                "degraded" if semantic_notice else "ok"
            )
            return response
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/duplicates", methods=["GET"])
    async def api_duplicates(request: Request) -> Response:
        """List bucket pairs flagged as duplicate candidates (sim > 0.95).

        iter 1.6 §4：每次 hold/grow 写完后 _check_duplicate_for 在两边写 dup_candidate +
        dup_score。本接口把所有这种标记的桶聚合成 pair，前端「记忆健康」面板可据此让
        她/他挨个确认是否合并。返回去重后的 pair 列表。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_b = await sh.bucket_mgr.list_all(include_archive=False)
            all_b = [
                b for b in all_b
                if not letter_lock_state(b, "human")["locked"]
            ]
            seen: set[frozenset] = set()
            pairs: list[dict] = []
            index = {b["id"]: b for b in all_b}
            for b in all_b:
                meta = b.get("metadata", {}) or {}
                other_id = meta.get("dup_candidate")
                if not other_id or other_id not in index:
                    continue
                key = frozenset((b["id"], other_id))
                if key in seen:
                    continue
                seen.add(key)
                other = index[other_id]
                pairs.append({
                    "a": {"id": b["id"], "name": meta.get("name", b["id"])},
                    "b": {"id": other_id, "name": other["metadata"].get("name", other_id)},
                    "score": meta.get("dup_score") or other["metadata"].get("dup_score"),
                })
            pairs.sort(key=lambda p: p.get("score") or 0, reverse=True)
            return JSONResponse({"pairs": pairs, "total": len(pairs)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/network", methods=["GET"])
    async def api_network(request: Request) -> Response:
        """Concept graph for visualization.

        iter 2.0+ §network rewrite: nodes are CONCEPT TOKENS that the user types
        inside their notes — `[[wikilinks]]` and frontmatter `tags`. Bucket
        filenames are NOT nodes. Two tokens get an edge whenever they co-occur
        in the same bucket. Edge weight = number of buckets containing both.

        iter 2.0+：节点 = 笔记里的双链词与 tag，不是文件名。两个词在同一个桶里出现就连一条边，
        边权重 = 共同出现的桶数。文件名只在前端搜索/详情里出现。

        Modes:
          - default `concept`：concept token graph (wikilinks + tags)
          - `embedding`：保留旧的桶级语义相似度网络（备用）
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        mode = (request.query_params.get("mode") or "concept").strip().lower()
        # 兼容旧入口 mode=wikilinks → 等价 concept
        if mode == "wikilinks":
            mode = "concept"
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            all_buckets = [
                b for b in all_buckets
                if not letter_lock_state(b, "human")["locked"]
            ]

            if mode == "embedding":
                # 旧的桶→桶相似度图（保留）
                nodes = []
                for b in all_buckets:
                    meta = b.get("metadata", {})
                    bid = b["id"]
                    nodes.append({
                        "id": bid,
                        "name": meta.get("name", bid),
                        "kind": "bucket",
                        "type": meta.get("type", "dynamic"),
                        "score": sh.decay_engine.calculate_score(meta),
                        "resolved": meta.get("resolved", False),
                        "pinned": meta.get("pinned", False),
                        "anchor": bool(meta.get("anchor")),  # #10
                    })
                edges = []
                embeddings = {}
                if sh.embedding_engine and sh.embedding_engine.enabled:
                    for b in all_buckets:
                        emb = await sh.embedding_engine.get_embedding(b["id"])
                        if emb is not None:
                            embeddings[b["id"]] = emb
                ids = list(embeddings.keys())
                for i, id_a in enumerate(ids):
                    for id_b in ids[i + 1:]:
                        sim = sh.embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > 0.5:
                            edges.append({"source": id_a, "target": id_b, "weight": round(sim, 3), "kind": "similarity"})
                return JSONResponse({"nodes": nodes, "edges": edges, "mode": mode})

            # ---- concept mode ----
            # token_id → {"label": str, "kind": "wiki"|"tag"|"mixed", "freq": int, "buckets": [bucket_id...]}
            # token_id 用规范化后的 lower-case 文本作 key，避免 "Memory" 与 "memory" 拆成两个节点
            tokens: dict[str, dict] = {}
            # bucket_id → set(token_id)，给后面共现统计用
            bucket_tokens: dict[str, set] = {}

            def _norm(s: str) -> str:
                return (s or "").strip()

            for b in all_buckets:
                bid = b["id"]
                meta = b.get("metadata", {}) or {}
                content = b.get("content", "") or ""

                seen: set[str] = set()
                # 1) 笔记正文里的 [[wikilinks]]
                for ref in extract_wikilinks(content):
                    label = _norm(ref)
                    if not label:
                        continue
                    key = label.lower()
                    node = tokens.setdefault(key, {"label": label, "kind": "wiki", "freq": 0, "buckets": []})
                    if key not in seen:
                        node["freq"] += 1
                        node["buckets"].append(bid)
                        seen.add(key)
                    # wiki 优先；若曾被标记为 tag，升级为 mixed
                    if node["kind"] == "tag":
                        node["kind"] = "mixed"

                # 2) frontmatter 的 tags（list 或字符串都兼容）
                raw_tags = meta.get("tags") or []
                if isinstance(raw_tags, str):
                    raw_tags = [t.strip() for t in raw_tags.split(",")]
                for t in raw_tags:
                    label = _norm(str(t)).lstrip("#")
                    if not label:
                        continue
                    key = label.lower()
                    node = tokens.setdefault(key, {"label": label, "kind": "tag", "freq": 0, "buckets": []})
                    if key not in seen:
                        node["freq"] += 1
                        node["buckets"].append(bid)
                        seen.add(key)
                    if node["kind"] == "wiki":
                        node["kind"] = "mixed"

                if seen:
                    bucket_tokens[bid] = seen

            # 共现边：同一个桶里的 token 两两相连，权重 = 共同出现的桶数
            # 复杂度上限是 sum(k_i^2) 其中 k_i 是单桶 token 数；正常都很小
            co_count: dict[tuple[str, str], int] = {}
            for bid, toks in bucket_tokens.items():
                ts = sorted(toks)
                for i, a in enumerate(ts):
                    for b_ in ts[i + 1:]:
                        co_key: tuple[str, str] = (a, b_)
                        co_count[co_key] = co_count.get(co_key, 0) + 1

            # #10: 标记「出现在至少一个 anchor 桶里」的 concept token
            anchor_bucket_ids = {
                b["id"] for b in all_buckets
                if (b.get("metadata") or {}).get("anchor")
            }
            nodes = [
                {
                    "id": k, "label": v["label"], "kind": v["kind"],
                    "freq": v["freq"], "buckets": v["buckets"],
                    "anchor": bool(anchor_bucket_ids and any(bid in anchor_bucket_ids for bid in v["buckets"])),
                }
                for k, v in tokens.items()
            ]
            edges = [{"source": a, "target": b_, "weight": w, "kind": "cooccur"} for (a, b_), w in co_count.items()]

            return JSONResponse({"nodes": nodes, "edges": edges, "mode": mode})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/breath", methods=["GET"])
    async def api_breath(request: Request) -> Response:
        """Lightweight breath surface: returns top-N buckets by decay score."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            n = max(1, min(int(request.query_params.get("n", "10")), 50))
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "n must be an integer in [1,50]"}, status_code=400)
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            results = []
            for bucket in all_buckets:
                if letter_lock_state(bucket, "human")["locked"]:
                    continue
                if not _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous").allowed:
                    continue
                meta = bucket.get("metadata", {})
                score = sh.decay_engine.calculate_score(meta)
                if meta.get("resolved"):
                    score *= 0.3
                results.append({
                    "id": bucket["id"],
                    "name": meta.get("name", bucket["id"]),
                    "score": round(score, 4),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                })
            results.sort(key=lambda x: x["score"], reverse=True)
            return JSONResponse({"buckets": results[:n]})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/breath-debug", methods=["GET"])
    async def api_breath_debug(request: Request) -> Response:
        """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        query = request.query_params.get("q", "")
        query_error = check_query_size(query)
        if query_error:
            return JSONResponse({"error": query_error}, status_code=400)
        _qv_raw = request.query_params.get("valence")
        _qa_raw = request.query_params.get("arousal")
        try:
            q_valence = _unit_query_float(_qv_raw, "valence")
            q_arousal = _unit_query_float(_qa_raw, "arousal")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            results = []
            w = {
                "topic": sh.bucket_mgr.w_topic,
                "emotion": sh.bucket_mgr.w_emotion,
                "time": sh.bucket_mgr.w_time,
                "importance": sh.bucket_mgr.w_importance,
            }
            w_sum = sum(w.values())

            for bucket in all_buckets:
                if letter_lock_state(bucket, "human")["locked"]:
                    continue
                meta = bucket.get("metadata", {})
                bid = bucket["id"]
                try:
                    topic = sh.bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                    emotion = sh.bucket_mgr._calc_emotion_score(q_valence if q_valence is not None else 0.5, q_arousal if q_arousal is not None else 0.5, meta)
                    time_s = sh.bucket_mgr._calc_time_score(meta)
                    imp = max(1, min(10, int(meta.get("importance") or 5))) / 10.0

                    raw_total = (
                        topic * w["topic"]
                        + emotion * w["emotion"]
                        + time_s * w["time"]
                        + imp * w["importance"]
                    )
                    normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                    resolved = meta.get("resolved", False)
                    if resolved:
                        normalized *= 0.3

                    results.append({
                        "id": bid,
                        "name": meta.get("name", bid),
                        "domain": meta.get("domain", []),
                        "type": meta.get("type", "dynamic"),
                        "resolved": resolved,
                        "pinned": meta.get("pinned", False),
                        "scores": {
                            "topic": round(topic, 4),
                            "emotion": round(emotion, 4),
                            "time": round(time_s, 4),
                            "importance": round(imp, 4),
                        },
                        "weights": w,
                        "raw_total": round(raw_total, 4),
                        "normalized": round(normalized, 2),
                        "passed_threshold": normalized >= sh.bucket_mgr.fuzzy_threshold,
                    })
                except Exception as _score_exc:
                    logger.error(
                        f"Scoring failed for bucket {bid!r}: {type(_score_exc).__name__}: {_score_exc}",
                        exc_info=True,
                    )
                    continue

            results.sort(key=lambda x: x["normalized"], reverse=True)
            passed = [r for r in results if r["passed_threshold"]]
            return JSONResponse({
                "query": query,
                "valence": q_valence,
                "arousal": q_arousal,
                "weights": w,
                "threshold": sh.bucket_mgr.fuzzy_threshold,
                "total_candidates": len(results),
                "passed_count": len(passed),
                "results": results[:50],  # top 50 for debug
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
