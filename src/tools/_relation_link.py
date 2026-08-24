"""写入后自动建立桶间关系（3.2.0）。

**为什么模型不感知这件事**：关联不是一个决定，是一个结果。我不会先想
"我要把这两段记忆连起来"再去建立它——是因为涉及同一个人、发生在同一个地方、
带来同样的感受，所以它们本来就连着，我只是**发现**它们连着。让模型显式调
`relation_attach` 等于把一个"发现"改造成一个"操作"，而判断依据只能是
"这两条看起来有关"——那正是向量相似度已经在做的事，重复且更差。

**为什么不调 LLM**：写入路径上加 LLM 调用会拖慢 `hold`、多一个会失败的外部
依赖，而 relation 只是 hint，不值得这个代价。规则 + 向量足够。

**为什么是 fire-and-forget**：关系建不出来不该让记忆写入失败。这里的任何异常
都只记日志，绝不向上抛——记忆本身永远比它的 hint 重要。

阈值与每桶上限见 `relation_store`，调整必须改施工单文档。
"""

from __future__ import annotations

from datetime import datetime

from ombrebrain.storage.relation_store import (
    AUTO_MAX_LINKS_PER_BUCKET,
    AUTO_RELATED_MIN_SCORE,
    MAX_RELATION_LINKS,
    collect_missing_reference_reverse,
    infer_auto_relation_type,
    merge_auto_links,
    normalize_relation_links,
    reverse_relation_type,
)

from . import _runtime as rt

# 这些类型不参与自动关联，与 relation_hint 的展示排除保持一致：
# plan 是待办、feel 是感受、letter 是写给未来的信、i 是自我认知——
# 它们各自成层，横向连起来只会制造噪音。
_EXCLUDED_TYPES = frozenset(
    {"plan", "feel", "letter", "i", "i_candidate", "identity", "archived"}
)

# 一次检索的候选数。取比上限大一截，因为候选里会被过滤掉不少
# （类型排除、已存在的关系、低于门槛的）。
_SEARCH_TOP_K = 24


def _bucket_type(meta: dict) -> str:
    return str((meta or {}).get("type") or "dynamic").strip().lower()


def _created_at(meta: dict) -> datetime | None:
    raw = str((meta or {}).get("created") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00").split("+")[0])
    except ValueError:
        return None


def _hours_apart(left: dict, right: dict) -> float | None:
    a, b = _created_at(left), _created_at(right)
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 3600.0


def _eligible(meta: dict) -> bool:
    if _bucket_type(meta) in _EXCLUDED_TYPES:
        return False
    # 已删除到档案的桶不再参与新关系
    return not (meta.get("deleted_at") or meta.get("tombstone"))


async def infer_links_for(bucket_id: str, content: str) -> list[dict]:
    """为一个新桶推断该建立的关系，不写盘。

    分成"推断"和"落库"两步是为了让判定可测——阈值行为不该只能靠跑完整
    写入链路来验证。
    """
    bucket_id = str(bucket_id or "").strip()
    if not bucket_id or not str(content or "").strip():
        return []

    engine = getattr(rt, "embedding_engine", None)
    if not engine or not getattr(engine, "enabled", False):
        return []

    pairs = await engine.search_similar(content, top_k=_SEARCH_TOP_K)
    if not pairs:
        return []

    source = await rt.bucket_mgr.get(bucket_id)
    if not source or not _eligible(source.get("metadata") or {}):
        return []
    source_meta = source.get("metadata") or {}

    inferred: list[dict] = []
    for target_id, score in pairs:
        target_id = str(target_id or "").strip()
        if not target_id or target_id == bucket_id:
            continue
        if float(score) < AUTO_RELATED_MIN_SCORE:
            continue
        target = await rt.bucket_mgr.get(target_id)
        if not target:
            continue
        target_meta = target.get("metadata") or {}
        if not _eligible(target_meta):
            continue
        relation_type = infer_auto_relation_type(
            float(score), _hours_apart(source_meta, target_meta)
        )
        if relation_type is None:
            continue
        inferred.append(
            {
                "target_bucket_id": target_id,
                "type": relation_type,
                "label": "",
                "status": "active",
                "auto": True,
                "score": round(float(score), 4),
            }
        )
        if len(inferred) >= AUTO_MAX_LINKS_PER_BUCKET:
            break
    return inferred


async def link_new_bucket(bucket_id: str, content: str) -> int:
    """推断并双向写入关系。返回实际建立的条数。

    调用方应当 `asyncio.create_task(...)`，不要 await——写入返回不等这个。
    """
    try:
        inferred = await infer_links_for(bucket_id, content)
    except Exception as exc:  # noqa: BLE001 - fire-and-forget，绝不影响写入
        rt.logger.warning(
            f"auto relation inference failed / 自动关系推断失败: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0

    built = 0
    for link in inferred:
        target_id = link["target_bucket_id"]
        reverse = {
            **link,
            "target_bucket_id": bucket_id,
            "type": reverse_relation_type(link["type"]),
        }

        def _mutation(left_post, right_post, _link=link, _reverse=reverse):
            try:
                left_links = normalize_relation_links(
                    left_post.metadata.get("relation_links")
                )
                right_links = normalize_relation_links(
                    right_post.metadata.get("relation_links")
                )
            except ValueError:
                # 存量数据写坏了不该拖累新关系，但也不在这里悄悄修复它
                return False, False, 0
            merged_left = merge_auto_links(left_links, [_link])
            merged_right = merge_auto_links(right_links, [_reverse])
            left_changed = merged_left != left_links
            right_changed = merged_right != right_links
            if left_changed:
                left_post["relation_links"] = normalize_relation_links(merged_left)
            if right_changed:
                right_post["relation_links"] = normalize_relation_links(merged_right)
            return left_changed, right_changed, int(left_changed or right_changed)

        try:
            result = await rt.bucket_mgr.mutate_relation_pair(
                bucket_id, target_id, _mutation
            )
        except Exception as exc:  # noqa: BLE001
            rt.logger.warning(
                f"auto relation write failed / 自动关系写入失败 "
                f"{bucket_id}->{target_id}: {type(exc).__name__}: {exc}"
            )
            continue
        built += int(result or 0)

    if built:
        rt.logger.info(
            f"auto relations built / 自动建立关系: {bucket_id} -> {built} 条"
        )
    return built


async def backfill_reference_reverse_links(all_buckets: list[dict]) -> int:
    """补齐 references 的反向边 referenced_by（幂等）。

    在 dream 全量扫描时调用（fire-and-forget 或 await 都行）。任何异常只记日志，
    绝不影响记忆本身；反边是补强的 hint，不是正文。
    """
    try:
        missing = collect_missing_reference_reverse(all_buckets)
    except Exception as exc:  # noqa: BLE001
        rt.logger.warning(
            f"reference reverse scan failed / 引用反边扫描失败: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0

    built = 0
    for target_id, source_id in missing:
        def _mutation(post, _source_id=source_id):
            try:
                links = normalize_relation_links(post.metadata.get("relation_links"))
            except ValueError:
                # 存量坏数据不在这里悄悄修，也不拖累补齐。
                return False, 0
            if any(
                l.get("type") == "referenced_by"
                and l.get("target_bucket_id") == _source_id
                for l in links
            ):
                return False, 0
            if len(links) >= MAX_RELATION_LINKS:
                return False, 0
            links.append(
                {
                    "target_bucket_id": _source_id,
                    "type": "referenced_by",
                    "label": "",
                    "status": "active",
                }
            )
            try:
                post["relation_links"] = normalize_relation_links(links)
            except ValueError:
                return False, 0
            return True, 1

        try:
            result = await rt.bucket_mgr.mutate_relation_links(target_id, _mutation)
        except Exception as exc:  # noqa: BLE001
            rt.logger.warning(
                f"reference reverse write failed / 引用反边写入失败 "
                f"{target_id}<-{source_id}: {type(exc).__name__}: {exc}"
            )
            continue
        built += int(result or 0)

    if built:
        rt.logger.info(
            f"reference reverse backfill / 补齐引用反边: {built} 条"
        )
    return built


async def backfill_auto_relations(all_buckets: list[dict]) -> dict[str, int]:
    """给存量桶全量回填自动边（same_event / continuation_of / related_to），幂等。

    自动建边（link_new_bucket）只在「新桶写入那一刻」增量触发，从不为存量老桶
    回填——所以 8 月 10 日那批回溯写入的老桶彼此之间一直没有 ≈ 同刻 / ↔ 相关。
    这个函数补上这一环：对每个非空活跃桶走一遍 link_new_bucket，边经
    merge_auto_links 去重，重复跑不重复建、也不覆盖手动边（source=manual 原样保留）。

    依赖 embedding 引擎（向量相似度）；未启用时直接返回，不报错。
    供 tools/backfill_relations.py 调用，一次性重建历史关系。
    """
    engine = getattr(rt, "embedding_engine", None)
    if not engine or not getattr(engine, "enabled", False):
        return {"scanned": 0, "built": 0, "engine_disabled": 1}

    scanned = 0
    built = 0
    for b in all_buckets or []:
        meta = b.get("metadata") or {}
        content = str(b.get("content") or "").strip()
        if not content or not _eligible(meta):
            continue
        scanned += 1
        try:
            built += await link_new_bucket(b["id"], content)
        except Exception as exc:  # noqa: BLE001 - 回填不该因单桶失败而中断
            rt.logger.warning(
                f"auto relation backfill failed / 自动关系回填失败 "
                f"{b['id']}: {type(exc).__name__}: {exc}"
            )
    return {"scanned": scanned, "built": built, "engine_disabled": 0}


def collect_missing_feel_links(all_buckets: list[dict]) -> list[tuple[str, str]]:
    """扫描 feel 桶的 triggered_by，找出缺 references 边的 (feel_id, source_id)。

    旧版 feel 写入时 source_bucket 只存 triggered_by 字段、不落关系边。3.9.0 起
    新写的 feel 会落 references 边，但存量老 feel 仍是两头断。这里纯判定：只找
    「triggered_by 有值、且 feel 身上没有 references 边指向它」的 (feel, source)。
    不写盘，便于单独测试。
    """
    if not all_buckets:
        return []

    by_id: dict[str, dict] = {}
    for b in all_buckets:
        bid = str((b or {}).get("id") or "").strip()
        if not bid:
            continue
        by_id[bid] = b

    missing: set[tuple[str, str]] = set()
    for b in all_buckets:
        meta = b.get("metadata") or {}
        if _bucket_type(meta) != "feel":
            continue
        feel_id = str(b.get("id") or "").strip()
        source_id = str(meta.get("triggered_by") or "").strip()
        if not feel_id or not source_id or source_id == feel_id:
            continue
        if source_id not in by_id:
            continue
        try:
            links = normalize_relation_links(meta.get("relation_links"))
        except ValueError:
            links = []
        has_ref = any(
            l.get("type") == "references"
            and l.get("target_bucket_id") == source_id
            and l.get("status") == "active"
            for l in links
        )
        if not has_ref:
            missing.add((feel_id, source_id))
    return sorted(missing)


async def backfill_feel_links(all_buckets: list[dict]) -> int:
    """给存量 feel 桶回填 references 边 + referenced_by 反边（幂等）。

    老 feel 的 triggered_by 有值但 references 边没有（旧版不落关系边）。扫一遍，
    把 triggered_by 落成：
      feel --references--> 源事件
      源事件 --referenced_by--> feel（反边）
    供 tools/backfill_feel_links.py 调用，一次性重建。任何异常只记日志，不影响记忆。
    """
    try:
        missing = collect_missing_feel_links(all_buckets)
    except Exception as exc:  # noqa: BLE001
        rt.logger.warning(
            f"feel link scan failed / 感受回填扫描失败: "
            f"{type(exc).__name__}: {exc}"
        )
        return 0

    built = 0
    for feel_id, source_id in missing:
        def _feel_side(post, _source_id=source_id):
            try:
                links = normalize_relation_links(post.metadata.get("relation_links"))
            except ValueError:
                return False, 0
            if any(
                l.get("type") == "references"
                and l.get("target_bucket_id") == _source_id
                for l in links
            ):
                return False, 0
            if len(links) >= MAX_RELATION_LINKS:
                return False, 0
            links.append(
                {
                    "target_bucket_id": _source_id,
                    "type": "references",
                    "label": "",
                    "status": "active",
                }
            )
            try:
                post["relation_links"] = normalize_relation_links(links)
            except ValueError:
                return False, 0
            return True, 1

        def _source_side(post, _feel_id=feel_id):
            try:
                links = normalize_relation_links(post.metadata.get("relation_links"))
            except ValueError:
                return False, 0
            if any(
                l.get("type") == "referenced_by"
                and l.get("target_bucket_id") == _feel_id
                for l in links
            ):
                return False, 0
            if len(links) >= MAX_RELATION_LINKS:
                return False, 0
            links.append(
                {
                    "target_bucket_id": _feel_id,
                    "type": "referenced_by",
                    "label": "",
                    "status": "active",
                }
            )
            try:
                post["relation_links"] = normalize_relation_links(links)
            except ValueError:
                return False, 0
            return True, 1

        try:
            left = await rt.bucket_mgr.mutate_relation_links(feel_id, _feel_side)
        except Exception as exc:  # noqa: BLE001
            rt.logger.warning(
                f"feel link write failed / 感受回填写入失败 "
                f"{feel_id}->{source_id}: {type(exc).__name__}: {exc}"
            )
            continue
        try:
            right = await rt.bucket_mgr.mutate_relation_links(source_id, _source_side)
        except Exception as exc:  # noqa: BLE001
            rt.logger.warning(
                f"feel reverse link write failed / 感受回填反边失败 "
                f"{source_id}<-{feel_id}: {type(exc).__name__}: {exc}"
            )
            continue
        built += int(left or 0) + int(right or 0)

    if built:
        rt.logger.info(f"feel links backfill / 感受回填: {built} 条")
    return built
