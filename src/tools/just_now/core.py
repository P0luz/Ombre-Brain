"""MCP-facing dispatcher for the ephemeral Just Now Context ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Optional

from .. import _identity
from .. import _runtime as rt
from .store import (
    JustNowConflict,
    JustNowCorruptStore,
    JustNowError,
    JustNowLedger,
    JustNowLimits,
    JustNowValidationError,
)


_ledger_cache: tuple[tuple[Any, ...], JustNowLedger] | None = None


def _configured_ledger() -> JustNowLedger:
    global _ledger_cache
    cfg = (rt.config or {}).get("just_now", {}) or {}
    raw_path = str(
        cfg.get("path")
        or os.environ.get("OMBRE_JUST_NOW_PATH")
        or (
            Path(tempfile.gettempdir())
            / "ombre-brain-runtime"
            / "just-now-v1.json"
        )
    )
    path = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    limits = JustNowLimits(
        ttl_seconds=int(cfg.get("ttl_seconds") or 6 * 60 * 60),
        max_items_per_stream=int(cfg.get("max_items_per_stream") or 40),
        max_content_chars=int(cfg.get("max_content_chars") or 4000),
        max_read_items=int(cfg.get("max_read_items") or 40),
    )
    key = (
        str(path),
        limits.ttl_seconds,
        limits.max_items_per_stream,
        limits.max_content_chars,
        limits.max_read_items,
    )
    if _ledger_cache is None or _ledger_cache[0] != key:
        _ledger_cache = (key, JustNowLedger(path, limits=limits))
    return _ledger_cache[1]


def _json(payload: dict[str, Any], ledger: JustNowLedger) -> str:
    enriched = dict(payload)
    enriched["policy"] = {
        "schema": "ombre-just-now-v1",
        "ttl_seconds": ledger.limits.ttl_seconds,
        "max_items_per_stream": ledger.limits.max_items_per_stream,
        "long_term_memory": False,
        "llm_calls": 0,
        "embedding_calls": 0,
    }
    return json.dumps(enriched, ensure_ascii=False, indent=2)


async def dispatch(
    action: Optional[str] = "read",
    source: Optional[str] = "",
    task_id: Optional[str] = "",
    role: Optional[str] = "",
    content: Optional[str] = "",
    occurred_at: Optional[str] = "",
    source_cursor: Optional[str] = "",
    event_id: Optional[str] = "",
    session_id: Optional[str] = "",
    cursor: Optional[str] = "",
    limit: Optional[int] = 0,
    after_seq: Optional[int] = 0,
    confirm: Optional[bool] = False,
) -> str:
    normalized_action = str(action or "read").strip().lower()
    caller = _identity.normalize_caller(_identity.get_caller())
    if not caller:
        return (
            "Just Now Context 已拒绝：当前请求没有可识别的 caller 身份。"
            "不会回退到 callerless 共享账本。"
        )

    if rt.mark_op:
        rt.mark_op("just_now")
    rt.record_v3_tool_event(
        "just_now",
        {
            "action": normalized_action,
            "source_len": len(str(source or "")),
            "task_id_len": len(str(task_id or "")),
            "role": str(role or ""),
            "content_len": len(content or ""),
            "has_occurred_at": bool(occurred_at),
            "has_source_cursor": bool(source_cursor),
            "has_event_id": bool(event_id),
            "has_session_id": bool(session_id),
            "has_cursor": bool(cursor),
            "limit": int(limit or 0),
            "after_seq": int(after_seq or 0),
            "confirm": bool(confirm),
        },
    )

    try:
        ledger = _configured_ledger()
        common = {
            "caller": caller,
            "source": str(source or ""),
            "task_id": str(task_id or ""),
        }
        if normalized_action == "append":
            result = ledger.append(
                **common,
                role=str(role or ""),
                content=str(content or ""),
                occurred_at=str(occurred_at or ""),
                source_cursor=str(source_cursor or ""),
                event_id=str(event_id or ""),
                session_id=str(session_id or ""),
            )
            return _json({"action": "append", **result}, ledger)

        if normalized_action == "read":
            result = ledger.read(
                **common,
                limit=int(limit) if limit and int(limit) > 0 else None,
                after_seq=int(after_seq or 0),
            )
            return _json({"action": "read", **result}, ledger)

        if normalized_action == "ack":
            result = ledger.ack_cursor(
                **common,
                cursor=str(cursor or ""),
            )
            return _json({"action": "ack", **result}, ledger)

        if normalized_action == "clear":
            if not confirm:
                raise JustNowValidationError(
                    "clear requires confirm=true and only clears the exact stream"
                )
            removed = ledger.clear_stream(**common)
            return _json(
                {
                    "action": "clear",
                    "caller": caller,
                    "source": str(source or ""),
                    "task_id": str(task_id or ""),
                    "removed": removed,
                },
                ledger,
            )

        raise JustNowValidationError(
            "action must be append, read, ack, or clear"
        )
    except JustNowCorruptStore as exc:
        rt.logger.error("just_now corrupt store: %s", exc)
        return (
            "Just Now Context 已拒绝：本地短期账本损坏，系统没有覆盖原文件。"
            "请检查服务端日志后修复或隔离该文件。"
        )
    except JustNowConflict as exc:
        return f"Just Now Context 冲突：{exc}"
    except (JustNowValidationError, ValueError, TypeError) as exc:
        return f"Just Now Context 已拒绝：{exc}"
    except JustNowError as exc:
        return f"Just Now Context 失败：{exc}"
