"""
========================================
web/buckets.py — 记忆桶管理 + 设置 + 锚点 + 自我认知读取
========================================

仪表板「记忆」页的后端：列表/详情、pin/resolve/archive/forget、批量遗忘、彻底清除
（写删除通知队列）、采样与 human 名设置（持久化 config.yaml）、锚点、/api/self。

对外暴露：register(mcp)。
========================================
"""

import os
import re
import yaml

from starlette.requests import Request
from starlette.responses import Response

from memory_messages import resolved_hint
from . import _shared as sh

logger = sh.logger

try:
    from utils import strip_wikilinks, display_bucket_name  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks, display_bucket_name  # type: ignore

try:
    from tools._common import check_pinned_quota as _check_pinned_quota  # type: ignore
except ImportError:  # pragma: no cover
    from ..tools._common import check_pinned_quota as _check_pinned_quota  # type: ignore


async def rename_human_in_buckets(old: str, new: str) -> dict:
    """把所有桶里字面量 `old` 正则替换成 `new`（name / content / why_remembered /
    letter user_name 四处都换）。

    用途：她/他改了称呼后，改名前就存在的老桶仍写着旧词（默认「用户」），breath 里
    新桶显示新名、老桶还是旧名，看起来"批量替换没生效"。这里一次性补齐。

    刻意**直接改 frontmatter，不走 bucket_mgr.update**：update 会把 last_active 刷成现在，
    改个称呼不该让全部记忆显得"刚刚动过"、扰乱衰减与浮现排序。content 改了的桶顺手重算
    embedding（best-effort，无 key/standby 时静默跳过）。

    返回 {buckets_changed, replacements}。old 为空 / old==new 时直接 no-op。"""
    import frontmatter as _fm

    if not old or not new or old == new:
        return {"buckets_changed": 0, "replacements": 0}

    pat = re.compile(re.escape(old))
    bm = sh.bucket_mgr
    dirs = list(bm._active_dirs) + [bm.archive_dir]
    changed, total = 0, 0

    for _root, _fname, fpath in bm._iter_md_files(dirs):
        try:
            post = _fm.load(fpath)
        except Exception:
            continue
        n = 0
        content_changed = False
        new_content, c = pat.subn(new, post.content or "")
        if c:
            post.content = new_content
            n += c
            content_changed = True
        for field in ("name", "why_remembered", "user_name"):
            v = post.get(field)
            if isinstance(v, str) and v:
                nv, c2 = pat.subn(new, v)
                if c2:
                    post[field] = nv
                    n += c2
        if not n:
            continue
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(_fm.dumps(post))
        except OSError as e:
            logger.warning(f"rename_human write failed {fpath}: {e}")
            continue
        changed += 1
        total += n
        if content_changed:
            bid = post.get("id")
            ee = sh.embedding_engine
            if bid and ee and getattr(ee, "enabled", False):
                try:
                    await ee.generate_and_store(bid, post.content or "")
                except Exception:
                    pass

    try:
        bm._invalidate_bm25()
    except Exception:
        pass
    logger.info(f"rename_human_in_buckets: '{old}'->'{new}' changed={changed} replacements={total}")
    return {"buckets_changed": changed, "replacements": total}


def register(mcp) -> None:

    @mcp.custom_route("/api/buckets", methods=["GET"])
    async def api_buckets(request: Request) -> Response:
        """List all buckets with metadata (no content for efficiency)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=True)
            result = []
            include_deleted = request.query_params.get("include_deleted", "").lower() in ("1", "true", "yes")
            for b in all_buckets:
                meta = b.get("metadata", {})
                if meta.get("deleted_at") and not include_deleted:
                    continue
                result.append({
                    "id": b["id"],
                    "name": display_bucket_name(meta.get("name", b["id"])),
                    "type": meta.get("type", "dynamic"),
                    "domain": meta.get("domain", []),
                    "tags": meta.get("tags", []),
                    "valence": meta.get("valence", 0.5),
                    "arousal": meta.get("arousal", 0.3),
                    "model_valence": meta.get("model_valence"),
                    "importance": meta.get("importance", 5),
                    "resolved": meta.get("resolved", False),
                    "pinned": meta.get("pinned", False),
                    "digested": meta.get("digested", False),
                    "created": meta.get("created", ""),
                    "last_active": meta.get("last_active", ""),
                    "activation_count": meta.get("activation_count", 1),
                    "score": sh.decay_engine.calculate_score(meta),
                    "content_preview": strip_wikilinks(b.get("content", ""))[:200],
                    # iter 1.8 新增字段（后台老桶读出默认值）
                    "why_remembered": meta.get("why_remembered", ""),
                    "dont_surface": bool(meta.get("dont_surface", False)),
                    "first_of_kind": bool(meta.get("first_of_kind", False)),
                    "weight": meta.get("weight"),  # plan 专有，非 plan 为 None
                    "triggered_by": meta.get("triggered_by", ""),
                    "deleted_at": meta.get("deleted_at", ""),
                })
            result.sort(key=lambda x: x["score"], reverse=True)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
    async def api_bucket_detail(request: Request) -> Response:
        """Get full bucket content by ID."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        meta = bucket.get("metadata", {})
        # iter 1.9 D / iter 2.0 §10 U-04: 反向链——只扫 feel_dir，O(feel桶数) 而非全库扫描
        triggered_feels = []
        try:
            triggered_feels = await sh.bucket_mgr.get_triggered_feels(bucket_id)
        except Exception as e:
            logger.warning(f"triggered_feels lookup failed / 反向链查询失败: {e}")
        return JSONResponse({
            "id": bucket["id"],
            "metadata": meta,
            "content": strip_wikilinks(bucket.get("content", "")),
            "score": sh.decay_engine.calculate_score(meta),
            "triggered_feels": triggered_feels,  # iter 1.9 D
        })


    # ---- Bucket-level mutation endpoints (iter 1.4) ----
    # 桶维度变更端点：钉选/解钉、resolve toggle、归档、删除到档案
    @mcp.custom_route("/api/bucket/{bucket_id}/pin", methods=["POST"])
    async def api_bucket_pin(request: Request) -> Response:
        """Toggle pinned flag (also flips type permanent⇄dynamic when needed)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        meta = bucket["metadata"]
        new_pinned = not bool(meta.get("pinned", False))
        update_kwargs: dict[str, object] = {"pinned": new_pinned}
        # Pinning: importance jumps to 10 + type→permanent. Unpin reverts type→dynamic.
        if new_pinned:
            quota_err = await _check_pinned_quota()
            if quota_err:
                return JSONResponse({"error": quota_err}, status_code=400)
            update_kwargs["importance"] = 10
            update_kwargs["type"] = "permanent"
        else:
            if meta.get("type") == "permanent":
                update_kwargs["type"] = "dynamic"
        try:
            await sh.bucket_mgr.update(bucket_id, **update_kwargs)
            return JSONResponse({"ok": True, "pinned": new_pinned})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/bucket/{bucket_id}/resolve", methods=["POST"])
    async def api_bucket_resolve(request: Request) -> Response:
        """Toggle resolved flag."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        new_resolved = not bool(bucket["metadata"].get("resolved", False))
        try:
            await sh.bucket_mgr.update(bucket_id, resolved=new_resolved)
            return JSONResponse({
                "ok": True,
                "resolved": new_resolved,
                "message": resolved_hint(new_resolved),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/bucket/{bucket_id}/archive", methods=["POST"])
    async def api_bucket_archive(request: Request) -> Response:
        """Move bucket to archive directory (soft delete)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        try:
            ok = await sh.bucket_mgr.archive(bucket_id)
            if not ok:
                return JSONResponse({"error": "archive failed or bucket not found"}, status_code=404)
            return JSONResponse({"ok": True, "archived": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # ---- iter 1.8: 主动遗忘开关 / voluntary forget toggle ---------
    # Toggle the dont_surface flag. Bucket itself stays on disk, only its
    # active push to breath() is suppressed. Search still finds it.
    # 切换 dont_surface 字段。桶仍在磁盘上，只是不再主动浮现到 breath。
    # 搜索（breath(query=...)）仍能找到它。
    @mcp.custom_route("/api/bucket/{bucket_id}/forget", methods=["POST"])
    async def api_bucket_forget(request: Request) -> Response:
        """Toggle dont_surface flag (iter 1.8 voluntary forget)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        new_val = not bool(bucket["metadata"].get("dont_surface", False))
        try:
            await sh.bucket_mgr.update(bucket_id, dont_surface=new_val)
            return JSONResponse({"ok": True, "dont_surface": new_val})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # ---- iter 1.9 C: 批量主动遗忘 / batch voluntary forget ---------
    # Body: {ids: [...], dont_surface: true|false}
    # 不像单条端点那样 toggle —— 批量必须显式说成 true 还是 false，避免误反转。
    @mcp.custom_route("/api/buckets/forget", methods=["POST"])
    async def api_buckets_forget_batch(request: Request) -> Response:
        """Batch toggle dont_surface for many buckets (iter 1.9 §C)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
        if "dont_surface" not in body:
            return JSONResponse({"error": "dont_surface (bool) required"}, status_code=400)
        target = bool(body["dont_surface"])
        ok_ids, missing_ids, errors = [], [], []
        for bid in ids:
            try:
                b = await sh.bucket_mgr.get(bid)
                if not b:
                    missing_ids.append(bid)
                    continue
                await sh.bucket_mgr.update(bid, dont_surface=target)
                ok_ids.append(bid)
            except Exception as e:
                errors.append({"id": bid, "error": str(e)})
                logger.warning(f"batch forget failed for {bid}: {e}")
        return JSONResponse({
            "ok": True,
            "dont_surface": target,
            "updated": ok_ids,
            "missing": missing_ids,
            "errors": errors,
        })


    # ---- iter 1.9 B: dashboard 调 sampling 配置 / sampling control ----
    # GET 返回当前 surfacing.sampling；POST 接收新值并热更新内存里的 config。
    # 这里只改运行时 config，不写回 yaml—— yaml 持久化交给 1.6 已有的设置面板机制（如开发者愿意手 sync）。
    @mcp.custom_route("/api/settings/sampling", methods=["GET", "POST"])
    async def api_settings_sampling(request: Request) -> Response:
        """Get / hot-update breath weighted sampling settings (iter 1.9 §B)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        surfacing = sh.config.setdefault("surfacing", {})
        sampling = surfacing.setdefault("sampling", {})
        if request.method == "GET":
            return JSONResponse({
                "enabled": bool(sampling.get("enabled", False)),
                "top_k": int(sampling.get("top_k") or 5),
                "sample_k": int(sampling.get("sample_k") or 2),
                "temperature": float(sampling.get("temperature") or 0.7),
            })
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        # Validate ranges; reject silently-corrupt inputs at the boundary
        try:
            if "enabled" in body:
                sampling["enabled"] = bool(body["enabled"])
            if "top_k" in body:
                tk = int(body["top_k"])
                if not (1 <= tk <= 50):
                    return JSONResponse({"error": "top_k must be in [1,50]"}, status_code=400)
                sampling["top_k"] = tk
            if "sample_k" in body:
                sk = int(body["sample_k"])
                if not (1 <= sk <= 20):
                    return JSONResponse({"error": "sample_k must be in [1,20]"}, status_code=400)
                sampling["sample_k"] = sk
            if "temperature" in body:
                t = float(body["temperature"])
                if not (0.1 <= t <= 5.0):
                    return JSONResponse({"error": "temperature must be in [0.1,5.0]"}, status_code=400)
                sampling["temperature"] = t
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": f"invalid field type: {e}"}, status_code=400)

        # --- 写回 config.yaml（iter 2.0 §10 U-03 修复：重启后设置不丢失）---
        try:
            from utils import config_file_path
            _cfg_path = config_file_path()
            _disk: dict[str, object] = {}
            if os.path.exists(_cfg_path):
                with open(_cfg_path, "r", encoding="utf-8") as _f:
                    _disk = yaml.safe_load(_f) or {}
            _disk_sf = _disk.setdefault("surfacing", {})
            if not isinstance(_disk_sf, dict):
                _disk_sf = {}
                _disk["surfacing"] = _disk_sf
            _disk_samp = _disk_sf.setdefault("sampling", {})
            if not isinstance(_disk_samp, dict):
                _disk_samp = {}
                _disk_sf["sampling"] = _disk_samp
            _disk_samp.update({
                "enabled": sampling.get("enabled", False),
                "top_k": sampling.get("top_k", 5),
                "sample_k": sampling.get("sample_k", 2),
                "temperature": sampling.get("temperature", 0.7),
            })
            with open(_cfg_path, "w", encoding="utf-8") as _f:
                yaml.dump(_disk, _f, default_flow_style=False, allow_unicode=True)
        except Exception as _e:
            logger.warning(f"sampling persist failed: {_e}")  # 不阻断热更新响应

        return JSONResponse({"ok": True, **sampling})


    # ---- iter 2.0: /api/settings/human — 读写通知称呼（human 宏）----
    # GET 返回当前 human 配置；POST 更新内存并写回 config.yaml。
    @mcp.custom_route("/api/settings/human", methods=["GET", "POST"])
    async def api_settings_human(request: Request) -> Response:
        """Get / update the 'human' display name used in deletion notices."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.method == "GET":
            return JSONResponse({"human": sh.config.get("human", "人类")})
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        human = body.get("human", "").strip()
        if not human:
            human = "人类"
        if len(human) > 20:
            return JSONResponse({"error": "human name must be ≤ 20 characters"}, status_code=400)
        # 旧称呼（默认「用户」，与 dehydrator / import 的兜底同源）—— 用于把老桶里的旧词换成新名。
        old_human = (sh.config.get("human") or "用户").strip() or "用户"
        sh.config["human"] = human
        # 同步活的 dehydrator.human：否则改名后、重启前，新记忆仍按旧称呼脱水。
        if getattr(sh, "dehydrator", None) is not None and hasattr(sh.dehydrator, "human"):
            sh.dehydrator.human = human
        # 写回 config.yaml
        try:
            from utils import config_file_path
            _cfg_path = config_file_path()
            _disk2: dict[str, object] = {}
            if os.path.exists(_cfg_path):
                with open(_cfg_path, "r", encoding="utf-8") as _f:
                    _disk2 = yaml.safe_load(_f) or {}
            _disk2["human"] = human
            with open(_cfg_path, "w", encoding="utf-8") as _f:
                yaml.dump(_disk2, _f, default_flow_style=False, allow_unicode=True)
        except Exception as _e:
            logger.warning(f"human name persist failed: {_e}")
        # 改名时把老桶里残留的旧称呼一起换成新名（name/content/why_remembered/user_name）。
        renamed = {"buckets_changed": 0, "replacements": 0}
        if old_human and old_human != human:
            try:
                renamed = await rename_human_in_buckets(old_human, human)
            except Exception as _re:
                logger.warning(f"human rename batch failed: {_re}")
        return JSONResponse({"ok": True, "human": human, "renamed": renamed})

    # ---- 手动「同步旧记忆」：把指定旧称呼（默认「用户」）批量换成当前称呼 ----
    # 用于：已经改过昵称、但改名前就存在的老桶仍写着旧词，breath 里新旧名并存。
    @mcp.custom_route("/api/settings/human/sync-existing", methods=["POST"])
    async def api_settings_human_sync(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        from_term = (body.get("from") or "用户").strip()
        cur = (sh.config.get("human") or "人类").strip() or "人类"
        if not from_term:
            return JSONResponse({"error": "缺少要替换的旧称呼"}, status_code=400)
        if from_term == cur:
            return JSONResponse({
                "ok": True, "from": from_term, "to": cur,
                "renamed": {"buckets_changed": 0, "replacements": 0},
                "note": "要替换的词与当前称呼相同，无需处理",
            })
        try:
            stats = await rename_human_in_buckets(from_term, cur)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "from": from_term, "to": cur, "renamed": stats})


    # ---- iter 2.0: anchor 端点 / coordinate-system buckets ----
    # anchor = 「定义我们是谁」的 24 槽。不进默认 breath，硬上限。
    @mcp.custom_route("/api/anchors", methods=["GET"])
    async def api_anchors_list(request: Request) -> Response:
        """Return all anchor buckets (sorted by created asc)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            anchors = await sh.bucket_mgr.list_anchors()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        items = []
        for b in anchors:
            m = b.get("metadata", {})
            items.append({
                "id": b["id"],
                "name": display_bucket_name(m.get("name") or b["id"]),
                "created": m.get("created", ""),
                "domain": m.get("domain", []),
                "tags": m.get("tags", []),
                "type": m.get("type", "dynamic"),
                "pinned": bool(m.get("pinned", False)),
                "preview": (b.get("content", "") or "")[:80],
            })
        return JSONResponse({
            "ok": True,
            "count": len(items),
            "limit": sh.bucket_mgr.ANCHOR_LIMIT,
            "anchors": items,
        })


    @mcp.custom_route("/api/bucket/{bucket_id}/anchor", methods=["POST"])
    async def api_bucket_anchor(request: Request) -> Response:
        """Toggle anchor flag on a bucket. 409 if cap reached when setting True."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        # Allow explicit value via JSON body; default = toggle
        target = None
        try:
            body = await request.json()
            if "value" in body:
                target = bool(body["value"])
        except Exception:
            pass  # no body → toggle
        if target is None:
            target = not bool(bucket["metadata"].get("anchor", False))
        result = await sh.bucket_mgr.set_anchor(bucket_id, target)
        if not result["ok"]:
            # Cap-reached errors → 409 Conflict; everything else → 500
            status = 409 if "上限" in result.get("error", "") or "limit" in result.get("error", "") else 500
            return JSONResponse(result, status_code=status)
        return JSONResponse(result)


    @mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
    async def api_bucket_delete(request: Request) -> Response:
        """Delete to archive (F-10): requires ?confirm=true. Moves file to archive/ + stamps deleted_at."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.query_params.get("confirm", "").lower() not in ("true", "1", "yes"):
            return JSONResponse({"error": "confirm=true required for delete-to-archive"}, status_code=400)
        bucket_id = request.path_params["bucket_id"]
        try:
            ok = await sh.bucket_mgr.delete(bucket_id)
            if not ok:
                return JSONResponse({"error": "bucket not found"}, status_code=404)
            return JSONResponse({"ok": True, "deleted": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/buckets/purge", methods=["POST"])
    async def api_buckets_purge(request: Request) -> Response:
        """Dashboard-only hard purge: physically removes files and generates an AI notification.

        Only callable from the dashboard (requires X-Purge-Confirm header).
        Not exposed as an MCP tool — the AI cannot trigger this.
        After purge, _pending_deletions.json is written; the next tool call
        sends a one-time notice to the AI about what was deleted.
        """
        from starlette.responses import JSONResponse
        import frontmatter as _fm
        err = sh._require_auth(request)
        if err:
            return err
        # Extra safeguard header — prevents automated/tool-based calls
        if request.headers.get("X-Purge-Confirm") != "dashboard-purge-v1":
            return JSONResponse({"error": "missing or invalid X-Purge-Confirm header"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        ids = body.get("ids", [])
        if not ids or not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
        if len(ids) > 200:
            return JSONResponse({"error": "too many ids (max 200 per request)"}, status_code=400)

        deleted_names: list = []
        failed: list = []
        for bid in ids:
            if not isinstance(bid, str) or not bid.strip():
                continue
            bid = bid.strip()
            file_path = sh.bucket_mgr._find_bucket_file(bid)
            if not file_path:
                failed.append(bid)
                continue
            # Read display name before deletion
            try:
                post = _fm.load(file_path)
                name = str(post.get("name") or bid)
            except Exception:
                name = bid
            try:
                os.remove(file_path)
                if sh.embedding_engine:
                    try:
                        sh.embedding_engine.delete_embedding(bid)
                    except Exception:
                        pass
                deleted_names.append(name)
                logger.info(f"[PURGE] hard-deleted bucket: {bid} ({name})")
            except OSError as e:
                logger.error(f"[PURGE] failed to delete {bid}: {e}")
                failed.append(bid)

        if deleted_names:
            sh.write_deletion_notice(deleted_names)

        return JSONResponse({"ok": True, "deleted": len(deleted_names), "failed": failed})


    # ---- letter REST endpoints (iter 1.4) ------------------------
    # =============================================================
    # /api/letters、/api/letter、/letters、/api/letter/{id} —— 已拆分到 web/letters.py
    # =============================================================


    @mcp.custom_route("/api/self", methods=["GET"])
    async def api_self(request: Request) -> Response:
        """Return all self-type (I tool) entries, newest first."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_b = await sh.bucket_mgr.list_all(include_archive=False)
            self_buckets = [
                b for b in all_b
                if b["metadata"].get("type") == "i"
                or "__i__" in (b["metadata"].get("tags") or [])
            ]
            self_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            result = []
            for b in self_buckets:
                meta = b["metadata"]
                tags = meta.get("tags") or []
                aspect = next((t.replace("aspect:", "") for t in tags if t.startswith("aspect:")), "")
                result.append({
                    "id": b["id"],
                    "content": b.get("content", ""),
                    "aspect": aspect,
                    "created": meta.get("created", ""),
                })
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # =============================================================
    # /api/search、/api/duplicates、/api/network、/api/breath、/api/breath-debug
    # —— 已拆分到 web/search.py
    # =============================================================
