"""Shared helpers for the append-only plan change log."""

from typing import Any

from utils import now_iso

__all__ = ["append_plan_change_log"]


def append_plan_change_log(
    old_history: Any,
    action: str,
    **fields: Any,
) -> list[dict[str, Any]]:
    """Copy a plan history and append one normalized timestamped entry."""
    history = list(old_history or [])
    entry: dict[str, Any] = {
        # 走 now_iso（带本地偏移）而不是裸的 datetime.now()：改动历史本来就是
        # 要跨机器对齐的东西，同子系统的 plan unlock_date 早就硬要求带时区了。
        "ts": now_iso(),
        "action": action,
    }
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    history.append(entry)
    return history

