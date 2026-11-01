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

from utils import parse_iso_datetime

from ombrebrain.storage.relation_store import (
    AUTO_MAX_LINKS_PER_BUCKET,
    AUTO_RELATED_MIN_SCORE,
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
        # 原来是 .split("+")[0] 手工剥偏移——负偏移（-05:00）剥不掉，
        # 剥出来的还是 aware，和 naive 的比较会炸。统一走 parse_iso_datetime。
        return parse_iso_datetime(raw)
    except (TypeError, ValueError):
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
