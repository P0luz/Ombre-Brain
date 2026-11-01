"""Validation and presentation helpers for Relation sidecar ledgers."""
from __future__ import annotations

from typing import Any

MAX_RELATION_LINKS = 64
MAX_ACTIVE_RELATION_LINKS = 16
MAX_RELATION_LABEL_CHARS = 20
MAX_RELATION_TYPE_CHARS = 32
MAX_RELATION_ID_CHARS = 64
_FIXED_RELATION_TYPES = frozenset({
    "caused_by", "causes", "continuation_of", "continues", "related_to",
    "same_event",
})
_RELATION_TYPES = _FIXED_RELATION_TYPES | {"custom"}
_REVERSE_RELATION_TYPES = {
    "caused_by": "causes",
    "causes": "caused_by",
    "continuation_of": "continues",
    "continues": "continuation_of",
    "related_to": "related_to",
    "same_event": "same_event",
    "custom": "custom",
}
_DEFAULT_DISPLAY_LABELS = {
    "caused_by": "原因",
    "causes": "结果",
    "continuation_of": "前段",
    "continues": "后续",
    "related_to": "相关",
    "same_event": "同一事件",
}


# ============================================================
# 自动建立关系（3.2.0）
# ------------------------------------------------------------
# 关联不是一个决定，是一个结果。我不会先想"我要把这两段记忆连起来"再去建立它——
# 是因为涉及同一个人、发生在同一个地方、带来同样的感受，所以它们本来就连着，
# 我只是**发现**它们连着。所以这件事交给后端，模型不感知。
#
# 判定只用规则 + 向量相似度，**不调 LLM**：写入路径上加 LLM 调用会拖慢 hold、
# 多一个会失败的外部依赖，而 relation 只是 hint，不值得这个代价。
#
# 阈值来自 2026-08-18 对 917 桶真实记忆的全量扫描（见施工单 §3.1 调整记录）：
#   related_to 原定 0.65 会建出 7,620 条，47.8% 的桶撞上每桶上限——
#   一旦大面积撞上限，阈值就形同虚设，决定挂哪几条的变成"截断时谁排前八"。
#   上调到 0.72 后每桶中位 2 条，只有 3.5% 需要截断。
# ============================================================

AUTO_SAME_EVENT_MIN_SCORE = 0.85
AUTO_SAME_EVENT_MAX_HOURS = 6.0
AUTO_CONTINUATION_MIN_SCORE = 0.75
AUTO_CONTINUATION_MAX_HOURS = 72.0
AUTO_RELATED_MIN_SCORE = 0.72
# 每桶自动关系上限。防热点桶连成蜘蛛网，不是常规裁剪手段——
# 正常情况下绝大多数桶远达不到这个数。
AUTO_MAX_LINKS_PER_BUCKET = 8


def infer_auto_relation_type(score: float, hours_apart: float | None) -> str | None:
    """按相似度与时间差推断该建哪种关系；判不出来就返回 None。

    只建三种。`caused_by` / `causes` / `custom` **永远不自动建**——
    因果需要语义理解，规则判不了，宁可不建也不能瞎建。

    时间差未知（缺 created）时不建带时间条件的两种，降级到 related_to。
    """
    try:
        score = float(score)
    except (TypeError, ValueError):
        return None
    if score < AUTO_RELATED_MIN_SCORE:
        return None
    if (
        score >= AUTO_SAME_EVENT_MIN_SCORE
        and hours_apart is not None
        and hours_apart <= AUTO_SAME_EVENT_MAX_HOURS
    ):
        return "same_event"
    if (
        score >= AUTO_CONTINUATION_MIN_SCORE
        and hours_apart is not None
        and hours_apart <= AUTO_CONTINUATION_MAX_HOURS
    ):
        return "continuation_of"
    return "related_to"


def merge_auto_links(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把新推断的自动关系并进已有 links，返回归一化后的结果。

    - **手动关系一条都不动**：`auto` 为假的 link 原样保留，自动的才参与裁剪。
      虽然 3.0.0 之后已经没有手动入口，存量数据仍然存在，不该被后来的自动
      推断挤掉。
    - 同一个 target 已存在则跳过，不重复也不改写它原来的类型。
    - 超过每桶上限时按相似度保留最高的几条。
    """
    kept = list(existing or [])
    seen = {str(link.get("target_bucket_id") or "") for link in kept}
    manual = [link for link in kept if not link.get("auto")]
    auto = [link for link in kept if link.get("auto")]

    for link in incoming:
        target = str(link.get("target_bucket_id") or "")
        if not target or target in seen:
            continue
        seen.add(target)
        auto.append(link)

    budget = max(0, AUTO_MAX_LINKS_PER_BUCKET - len(manual))
    auto.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    return manual + auto[:budget]


# ============================================================
# 修正连错的关系（3.3.0）
# ------------------------------------------------------------
# 自动建立是「发现」，修正是「我看过之后不同意」——后者只能是显式动作，
# 所以走 trace，而不是再加一条后端规则去猜自己刚才是不是判错了。
#
# 改过的关系一律**降级为手动关系**（去掉 auto / score）：它已经被明确看过
# 一次，不该再被后来的自动推断按相似度挤掉。`merge_auto_links` 保护 manual。
#
# unlink 是物理移除，不是标成 status="detached"：3.0.0 删掉了
# relation_restore，detached 会变成一个再也回不来的僵尸状态，还白占
# MAX_RELATION_LINKS 的名额。删掉也不会被自动重建——`link_new_bucket` 只在
# **新建桶**时触发一次推断，那一对桶不会再出生第二次。
# ============================================================


def unlink_relation(
    links: list[dict[str, Any]],
    target_bucket_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    """移除指向 target 的那条关系，返回 (新列表, 是否有变化)。

    找不到不是错误，交给调用方决定怎么措辞——单向残留（一侧有、另一侧没有）
    在存量数据里真实存在，两边都得能各自删干净。
    """
    target_bucket_id = str(target_bucket_id or "").strip()
    if not target_bucket_id:
        return list(links or []), False
    kept = [
        link
        for link in (links or [])
        if str(link.get("target_bucket_id") or "").strip() != target_bucket_id
    ]
    return kept, len(kept) != len(links or [])


def retype_relation(
    links: list[dict[str, Any]],
    target_bucket_id: str,
    relation_type: str,
) -> tuple[list[dict[str, Any]], bool]:
    """把指向 target 的那条关系改成新类型，并降级为手动关系。

    **不会凭空建立**：target 不在 links 里就原样返回 False。「建立」仍然只归
    后端，这里能做的只有「你连的这条，类型不对」。

    原 label 保留：自动关系本来就没有 label，而存量 custom 关系的 label 是
    当初写下的原话，改类型不该顺手把它抹掉。
    """
    target_bucket_id = str(target_bucket_id or "").strip()
    relation_type = normalize_relation_type(relation_type)
    if not target_bucket_id:
        return list(links or []), False

    updated: list[dict[str, Any]] = []
    changed = False
    for link in links or []:
        if str(link.get("target_bucket_id") or "").strip() != target_bucket_id:
            updated.append(link)
            continue
        if link.get("type") == relation_type and not link.get("auto"):
            # 类型没变、也已经是手动关系，不必改写文件
            updated.append(link)
            continue
        revised = {k: v for k, v in link.items() if k not in {"auto", "score"}}
        revised["type"] = relation_type
        updated.append(revised)
        changed = True
    return updated, changed


def normalize_relation_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("relation_type 必须是字符串安全键")
    value = value.strip().lower()
    if value not in _RELATION_TYPES:
        raise ValueError("relation_type must be one of the six fixed types or custom")
    return value


def reverse_relation_type(value: Any) -> str:
    return _REVERSE_RELATION_TYPES[normalize_relation_type(value)]


def normalize_relation_label(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("relation label 必须是字符串")
    if "\r" in value or "\n" in value:
        raise ValueError("relation label 不允许换行")
    value = value.strip()
    if len(value) > MAX_RELATION_LABEL_CHARS:
        raise ValueError(f"relation label 最多 {MAX_RELATION_LABEL_CHARS} 个字符")
    return value


def normalize_relation_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("relation_id 必须是字符串")
    value = value.strip()
    if not value or "\r" in value or "\n" in value or len(value) > MAX_RELATION_ID_CHARS:
        raise ValueError("relation_id 格式无效")
    return value


def normalize_relation_links(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("relation_links 必须是列表")
    if len(value) > MAX_RELATION_LINKS:
        raise ValueError(f"relation_links 过多（{len(value)} > {MAX_RELATION_LINKS}）")
    links: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("relation_links 每项必须是对象")
        target_bucket_id = item.get("target_bucket_id")
        if not isinstance(target_bucket_id, str):
            raise ValueError("relation_links target_bucket_id 必须是字符串")
        target_bucket_id = target_bucket_id.strip()
        if not target_bucket_id or "\r" in target_bucket_id or "\n" in target_bucket_id:
            raise ValueError("relation_links 包含非法 target_bucket_id")
        status = item.get("status")
        if not isinstance(status, str):
            raise ValueError("relation_links status 必须是字符串")
        status = status.strip().lower()
        if status not in {"active", "detached"}:
            raise ValueError("relation_links status 必须是 active 或 detached")
        relation_type = normalize_relation_type(item.get("type"))
        label = normalize_relation_label(item.get("label"))
        if relation_type == "custom" and not label:
            raise ValueError("custom relation 必须有 label")
        relation_id = normalize_relation_id(item.get("relation_id"))
        normalized = {
            "target_bucket_id": target_bucket_id,
            "type": relation_type,
            "label": label,
            "status": status,
        }
        # auto 标记让自动建立的关系可以被区分和整体回滚；score 留着是为了
        # 上限截断时知道该保留哪几条。两者都只在自动关系上出现。
        if item.get("auto"):
            normalized["auto"] = True
            try:
                score = round(float(item.get("score")), 4)
            except (TypeError, ValueError):
                score = None
            if score is not None:
                normalized["score"] = score
        # V1 历史单向边没有 relation_id，保留原形不强制迁移。
        if relation_id:
            normalized["relation_id"] = relation_id
        links.append(normalized)
    if sum(item["status"] == "active" for item in links) > MAX_ACTIVE_RELATION_LINKS:
        raise ValueError(f"活动 relation_links 过多（>{MAX_ACTIVE_RELATION_LINKS}）")
    return links


def relation_display_label(relation_type: str, label: str | None = "") -> str:
    """Render a short human-facing label without reading the target bucket."""
    relation_type = normalize_relation_type(relation_type)
    label = normalize_relation_label(label)
    if relation_type == "custom":
        return label or "自定义"
    base = _DEFAULT_DISPLAY_LABELS.get(relation_type, relation_type)
    # 新建的固定六型不写 label；旧 V1 若已存在 label，仍保留展示以免丢信息。
    return f"{base}·{label}" if label else base


def relation_hint(bucket: dict, limit: int = 2) -> str:
    meta = bucket.get("metadata") or {}
    if str(meta.get("type") or "dynamic").strip().lower() in {"plan", "feel", "letter", "i", "i_candidate", "identity"}:
        return ""
    try:
        links = normalize_relation_links(meta.get("relation_links"))
    except ValueError:
        return ""
    active = [link for link in links if link["status"] == "active"]
    rows = []
    for link in active[:limit]:
        label = relation_display_label(link["type"], link["label"])
        rows.append(f"↳ {label} → {link['target_bucket_id']}")
    hidden = len(active) - len(rows)
    if hidden > 0:
        rows.append(f"↳ 另有 {hidden} 条 relation")
    return "\n".join(rows)
