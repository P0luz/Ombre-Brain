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
    "same_event", "references", "referenced_by",
})
_RELATION_TYPES = _FIXED_RELATION_TYPES | {"custom"}
_REVERSE_RELATION_TYPES = {
    "caused_by": "causes",
    "causes": "caused_by",
    "continuation_of": "continues",
    "continues": "continuation_of",
    "related_to": "related_to",
    "same_event": "same_event",
    "references": "referenced_by",
    "referenced_by": "references",
    "custom": "custom",
}
_DEFAULT_DISPLAY_LABELS = {
    "caused_by": "原因",
    "causes": "结果",
    "continuation_of": "前段",
    "continues": "后续",
    "related_to": "相关",
    "same_event": "同一事件",
    "references": "引用",
    "referenced_by": "被引用",
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


def normalize_relation_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("relation_type 必须是字符串安全键")
    value = value.strip().lower()
    if value not in _RELATION_TYPES:
        raise ValueError("relation_type must be one of the fixed types or custom")
    return value


def reverse_relation_type(value: Any) -> str:
    return _REVERSE_RELATION_TYPES[normalize_relation_type(value)]


def is_fixed_relation_type(value: Any) -> bool:
    return normalize_relation_type(value) in _FIXED_RELATION_TYPES


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
        # source 标记边的来源：manual（模型手动声明，含 create 的 references 与
        # link 补线）等。只在显式传入时保留，历史边不补标。
        source = item.get("source")
        if source:
            normalized["source"] = str(source).strip()[:16]
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


# ============================================================
# 路口渲染（3.3.0）：把 relation_links 渲染成按方向分组的路口。
# ------------------------------------------------------------
# recall 与 breath/catalog/dream 三处浮现层共用同一套分组与标签，
# 不各写各的。方向标签统一走「走起来」的口味；关系本身不动，只换
# 展示的皮。relation_hint 是零 I/O 的轻量版（只给目标 id，服务同步
# 渲染路径），render_junction 是完整版（读邻居给标题/日期，服务
# recall 这种「可以承受一次 I/O」的路径）。
# ============================================================

# 这些类型各自成层，不参与关系网的展示（作为桶自身或邻居都跳过）。
# feel 也在其中——但 feel 是「定向参与」：它只牵着它来自的事件（references）和
# 被事件顺出（referenced_by），其余边类型对 feel 无意义。所以 feel 的排除不是
# 完全排除，而是在渲染层靠 FEEL_ONLY_RELATION_TYPES 做定向过滤（见 relation_hint /
# render_junction）。plan/letter/i 才是完全排除。
EXCLUDED_RELATION_TYPES = frozenset({
    "plan", "feel", "letter", "i", "i_candidate", "identity",
})

# feel 定向参与的边类型：感受只走这两条，不横向连、不参与时间边。
FEEL_ONLY_RELATION_TYPES = frozenset({"references", "referenced_by"})

# 路口分组的展示顺序：时间方向在前，相关/自定义殿后——
# 让「沿着时间往回走」的顺序自然：因为 → 之前 → 同刻 → 之后 → 所以 → 相关 → 引用。
# references 是「我自己写下的强关系」，放在相关之后、自定义之前。
DIRECTION_GROUPS = (
    ("caused_by", "← 因为"),
    ("continuation_of", "← 之前"),
    ("same_event", "≈ 同刻"),
    ("continues", "→ 之后"),
    ("causes", "→ 所以"),
    ("related_to", "↔ 相关"),
    ("references", "🔗 引用"),
    ("referenced_by", "🔗 被引用"),
)


def bucket_type(meta: dict) -> str:
    return str((meta or {}).get("type") or "dynamic").strip().lower()


def bucket_title(bucket: dict) -> str:
    """取一个桶的展示标题：title → name → 正文首行（截 40 字）→ id。"""
    meta = bucket.get("metadata") or {}
    title = str(meta.get("title") or "").strip()
    if title:
        return title
    name = str(meta.get("name") or "").strip()
    if name:
        return name
    content = str(bucket.get("content") or "").strip()
    if content:
        first = content.splitlines()[0].strip()
        if first:
            return first[:40] + ("…" if len(first) > 40 else "")
    return str(bucket.get("id") or "")


def bucket_date(meta: dict) -> str:
    """优先 event_time（事情发生的时间），回退 created（记下的时间）。"""
    raw = str((meta or {}).get("event_time") or "").strip()
    if not raw:
        raw = str((meta or {}).get("created") or "").strip()
    return raw[:10] if raw else ""


def _direction_of(rel_type: str) -> str:
    for key, label in DIRECTION_GROUPS:
        if rel_type == key:
            return label
    return ""


def relation_hint(bucket: dict, limit: int = 2) -> str:
    """主 breath 浮现的轻量路口：按方向分组，只给目标 id，不读邻居。

    标题/日期需要读邻居（异步 I/O），那是 render_junction 的活；这里保持同步、
    零 I/O——只负责让浮现末尾能看清「有几个方向、每个方向指向谁」，
    想读全文就用 recall(id) 点进去。
    """
    meta = bucket.get("metadata") or {}
    btype = bucket_type(meta)
    if btype in EXCLUDED_RELATION_TYPES and btype != "feel":
        return ""
    feel_only = btype == "feel"
    try:
        links = normalize_relation_links(meta.get("relation_links"))
    except ValueError:
        return ""
    active = [link for link in links if link.get("status") == "active"]
    grouped: dict[str, list[str]] = {}
    custom_ids: list[str] = []
    for link in active:
        target_id = str(link.get("target_bucket_id") or "").strip()
        if not target_id:
            continue
        rel_type = link.get("type") or ""
        if feel_only and rel_type not in FEEL_ONLY_RELATION_TYPES:
            continue
        if rel_type == "custom":
            custom_ids.append(target_id)
            continue
        direction = _direction_of(rel_type)
        if not direction:
            continue
        grouped.setdefault(direction, []).append(target_id)

    rows: list[str] = []
    for _key, label in DIRECTION_GROUPS:
        ids = grouped.get(label, [])
        if not ids:
            continue
        shown = ids[:limit]
        hidden = len(ids) - len(shown)
        tail = f"（另有 {hidden}）" if hidden else ""
        rows.append(f"{label}: " + ", ".join(shown) + tail)
    if custom_ids:
        rows.append("· 自定义: " + ", ".join(custom_ids[:limit]))
    return "\n".join(rows)


async def render_junction(
    bucket: dict,
    get_neighbor,
    limit_per_direction: int = 5,
) -> str:
    """把一条记忆的 relation_links 渲染成按方向分组的完整路口（带标题/日期）。

    这是 relation_hint 的完整版：读邻居拿标题与日期，给 recall 这种「可以承受
    一次 I/O」的路径用。get_neighbor 是 async (bucket_id) -> dict|None，由调用方
    注入 `rt.bucket_mgr.get`，本函数不反向依赖 bucket_mgr，便于单测。

    没有任何可走的邻居时返回空字符串，调用方自己决定要不要补「珍珠」提示。
    """
    if get_neighbor is None:
        # 读不到邻居时退回零 I/O 的轻量版（只给 id）
        return relation_hint(bucket, limit=limit_per_direction)

    meta = bucket.get("metadata") or {}
    btype = bucket_type(meta)
    if btype in EXCLUDED_RELATION_TYPES and btype != "feel":
        return ""
    feel_only = btype == "feel"
    try:
        links = normalize_relation_links(meta.get("relation_links"))
    except ValueError:
        return ""
    active = [link for link in links if link.get("status") == "active"]
    bucket_id = str(bucket.get("id") or "")

    grouped: dict[str, list[str]] = {}
    custom_rows: list[str] = []
    for link in active:
        rel_type = link.get("type") or ""
        if feel_only and rel_type not in FEEL_ONLY_RELATION_TYPES:
            continue
        target_id = str(link.get("target_bucket_id") or "").strip()
        if not target_id or target_id == bucket_id:
            continue
        neighbor = await get_neighbor(target_id)
        if not neighbor:
            continue
        nmeta = neighbor.get("metadata") or {}
        nbtype = bucket_type(nmeta)
        if nbtype in EXCLUDED_RELATION_TYPES and nbtype != "feel":
            continue
        # feel 邻居只通过 references/referenced_by 定向边显示（感受跟着事件浮现）
        if nbtype == "feel" and rel_type not in FEEL_ONLY_RELATION_TYPES:
            continue
        ntitle = bucket_title(neighbor)
        ndate = bucket_date(nmeta)
        date_part = f" 〔{ndate}〕" if ndate else ""
        if rel_type == "custom":
            label = relation_display_label(rel_type, link.get("label"))
            custom_rows.append(f"  ↳ {label} · {ntitle}{date_part} {target_id}")
            continue
        direction = _direction_of(rel_type)
        if not direction:
            continue
        grouped.setdefault(direction, []).append(f"  ↳ {ntitle}{date_part} {target_id}")

    parts: list[str] = []
    for _key, label in DIRECTION_GROUPS:
        rows = grouped.get(label, [])
        if not rows:
            continue
        shown = rows[:limit_per_direction]
        hidden = len(rows) - len(shown)
        parts.append(f"{label}（{len(rows)}）")
        parts.extend(shown)
        if hidden > 0:
            parts.append(f"  …另有 {hidden} 条")
    if custom_rows:
        parts.append("· 自定义")
        parts.extend(custom_rows)
    return "\n".join(parts)


# ============================================================
# references 反向边补齐（3.4.0+）
# ------------------------------------------------------------
# references 是模型在 hold 时手动声明的「这条正文提到了哪条桶」，落成 A→B 的
# 单向边。被引用的 B 身上没有反向记号，导致从 B recall 时看不到这根线。
# 这里补一条 referenced_by（B→A）反边，让线从两头都能拎起来。
# 幂等：已有反边的不再列出。只处理非归档、非排除类型的目标桶。
# ============================================================

def collect_missing_reference_reverse(all_buckets: list[dict]) -> list[tuple[str, str]]:
    """扫描 references 边，找出缺失 referenced_by 反边的 (被引用桶id, 引用桶id)。

    返回按 (被引用, 引用) 字典序去重后的列表。不写盘，纯判定，便于单独测试。
    """
    if not all_buckets:
        return []

    def _active_links(meta: dict) -> list[dict]:
        try:
            return normalize_relation_links(meta.get("relation_links"))
        except ValueError:
            return []

    by_id: dict[str, dict] = {}
    for b in all_buckets:
        bid = str((b or {}).get("id") or "").strip()
        if not bid:
            continue
        meta = b.get("metadata") or {}
        if bucket_type(meta) in EXCLUDED_RELATION_TYPES:
            continue
        by_id[bid] = b

    missing: set[tuple[str, str]] = set()
    for source in all_buckets:
        smeta = source.get("metadata") or {}
        if bucket_type(smeta) in EXCLUDED_RELATION_TYPES:
            continue
        sid = str(source.get("id") or "").strip()
        if not sid:
            continue
        for link in _active_links(smeta):
            if link.get("type") != "references" or link.get("status") != "active":
                continue
            target_id = link.get("target_bucket_id") or ""
            if target_id == sid or target_id not in by_id:
                continue
            tlinks = _active_links(by_id[target_id].get("metadata") or {})
            has_reverse = any(
                l.get("type") == "referenced_by"
                and l.get("target_bucket_id") == sid
                for l in tlinks
            )
            if not has_reverse:
                missing.add((target_id, sid))
    return sorted(missing)
