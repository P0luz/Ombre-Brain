"""记忆足迹：把内部事件镜像压缩成 breath 可读的一行经历。

Footprint 不保存或重写记忆正文，也不是新的真源。它读取兼容的
``_ledger/events.jsonl``，忽略 touch/索引等技术噪声，只表达对模型有意义的
创建、补充、遗忘、归档与恢复。旧 LedgerMirror 继续作为落盘适配器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# Production E1/E2 used this module name for immutable create-time origin
# receipts before upstream v3.6 introduced the event-history FootprintSnapshot.
# Keep both contracts available; they are complementary and have disjoint names.
from .origin_receipt import (  # noqa: F401
    ALL_ACTOR_KINDS,
    ALL_SURFACES,
    ALL_VIA,
    FIELD_NAMES,
    KNOWN_CALLERS,
    MCP_VIA,
    ORIGIN_VIA_LABELS,
    SCHEMA_VERSION,
    FootprintOriginError,
    cli_origin,
    dashboard_letter_origin,
    import_origin,
    mcp_origin,
    render_origin_line,
    system_origin,
    validate_origin,
)


_ARCHIVED_KINDS = {"archived", "deleted", "tombstone"}

_ACTOR_LABELS = {
    "llm": "LLM",
    "human": "用户",
    "system": "系统",
}

_SOURCE_LABELS = {
    "hold": "hold",
    "grow": "grow",
    "import": "用户导入",
    "plan": "plan",
    "letter": "letter",
    "i": "I",
    "direct": "系统直接",
}


@dataclass(frozen=True)
class FootprintSnapshot:
    """一次 breath 内复用的只读足迹快照，避免每个桶重复扫描 JSONL。"""

    events_by_trace: dict[str, tuple[dict[str, Any], ...]]

    @classmethod
    def from_events(cls, events: Iterable[dict[str, Any]]) -> "FootprintSnapshot":
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            trace_id = str(event.get("trace_id") or "").strip()
            if trace_id:
                grouped.setdefault(trace_id, []).append(dict(event))
        return cls({key: tuple(value) for key, value in grouped.items()})

    def summary(self, trace_id: str, metadata: dict | None = None) -> str:
        labels = [
            label
            for event in self.events_by_trace.get(str(trace_id), ())
            if (label := _event_label(event))
        ]
        if not labels:
            labels = ["已留下"]
        compact: list[str] = []
        for label in labels:
            if compact and compact[-1].split("×", 1)[0] == label:
                base = compact[-1].split("×", 1)[0]
                count = int(compact[-1].split("×", 1)[1]) + 1 if "×" in compact[-1] else 2
                compact[-1] = f"{base}×{count}"
            else:
                compact.append(label)
        # 创建永远保留；中间过长时只留最早一步和最近三步。
        if len(compact) > 4:
            compact = [compact[0], "…", *compact[-2:]]
        return "👣 Footprint：" + " → ".join(compact)

    def original_kind(self, trace_id: str, metadata: dict | None = None) -> str:
        for event in self.events_by_trace.get(str(trace_id), ()):
            kind = str(event.get("trace_kind") or "").strip().lower()
            if kind and kind not in _ARCHIVED_KINDS:
                return kind
        meta_kind = str((metadata or {}).get("type") or "").strip().lower()
        return meta_kind if meta_kind not in _ARCHIVED_KINDS else "dynamic"


def _event_label(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "TraceCreated":
        source = str(payload.get("source_tool") or "direct").strip().lower()
        source_label = _SOURCE_LABELS.get(source, "已声明来源")
        actor_label = _actor_label(payload)
        pinned_suffix = "并钉为核心" if payload.get("pinned") else ""
        return f"{actor_label}经{source_label}创建{pinned_suffix}"
    if event_type == "TraceRestored":
        return "重新回忆"
    if event_type == "TraceArchived":
        return "淡去归档"
    if event_type == "TraceDeletedToArchive":
        return "删除到档案"
    if event_type == "TraceHardDeleted":
        return "测试痕迹清理"
    if event_type != "TraceUpdated":
        return ""  # TraceTouched 和未知技术事件不占 breath token。

    fields = {str(item) for item in payload.get("changed_fields") or []}
    if "last_merged_by" in fields:
        return "事件补充"
    if "dont_surface" in fields:
        return "主动淡忘" if payload.get("dont_surface") else "重新浮现"
    if "pinned" in fields:
        action = "钉为核心" if payload.get("pinned") else "解除核心"
        return f"{_actor_label(payload)}{action}"
    if "anchor" in fields:
        return "设为地标" if payload.get("anchor") else "解除地标"
    if "resolved" in fields:
        return "已经放下" if payload.get("resolved") else "重新激活"
    if "content" in fields:
        return "正文重构"
    return "更新"


def _actor_label(payload: dict[str, Any]) -> str:
    actor = str(payload.get("event_actor") or "").strip().lower()
    return _ACTOR_LABELS.get(actor, "操作者未记录")
