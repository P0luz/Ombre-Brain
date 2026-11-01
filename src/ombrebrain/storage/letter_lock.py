"""Letter 的锁语义 —— 纯元数据计算，没有任何 I/O。

原先长在 `tools/plan/core.py` 里。3.6.5 下沉到这里，是因为 `you` / `them` 的
证据闸也要判「这封信对 AI 开没开」，而 `ombrebrain/` 不能反过来 import `tools/`
（分层是单向的）。

**下沉而不是抄一份**：锁是安全边界，两份实现迟早会不一样，而不一样的那天
错的方向是「本该锁着的被读了」。`tools/plan/core.py` 现在从这里引入，
所有既有调用点的名字和行为都不变。
"""

from __future__ import annotations

from datetime import datetime, timezone

LETTER_LOCK_TYPES = {"none", "timed", "permanent"}


def normalize_lock_type(value: object) -> str:
    lock_type = str(value or "none").strip().lower()
    if lock_type not in LETTER_LOCK_TYPES:
        raise ValueError("lock_type must be one of: none, timed, permanent")
    return lock_type


def is_letter_bucket(bucket: dict) -> bool:
    """生命周期改写存储类型后，仍能识别逻辑上的 Letter。"""
    meta = bucket.get("metadata") or {}
    if not isinstance(meta, dict):
        return False
    if str(meta.get("type") or "").strip().casefold() == "letter":
        return True
    if str(meta.get("source_tool") or "").strip().casefold() == "letter":
        return True
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    if isinstance(tags, (list, tuple, set)) and "__letter__" in tags:
        return True
    return (
        str(meta.get("locked_by") or "").strip() in {"human", "ai"}
        and str(meta.get("lock_type") or "").strip().casefold()
        in {"timed", "permanent"}
    )


def letter_lock_state(
    bucket: dict, caller_side: str | None, *, now: datetime | None = None
) -> dict:
    meta = bucket.get("metadata") or {}
    try:
        lock_type = normalize_lock_type(meta.get("lock_type", "none"))
    except ValueError:
        lock_type = "none"
    unlock_date = meta.get("unlock_date") or None
    locked_by = str(meta.get("locked_by") or "").strip() or None
    expired = False
    if lock_type == "timed" and unlock_date:
        try:
            parsed = datetime.fromisoformat(str(unlock_date).replace("Z", "+00:00"))
            # 不再要求 parsed 带时区。原先是 `bool(parsed.tzinfo) and ...`：
            # 没有时区就直接判「没过期」，于是一封 2020 年就该解锁的信永远锁着，
            # letter_read 一辈子看不到它，而 Dashboard 照常显示——两边说法不一致，
            # 用户无从判断是数据没了还是工具坏了。
            #
            # 裸时间交给 astimezone() 按系统本地时区解释，和 OB 其它时间字段
            # 一个口径。写入路径（tools/plan/core.py）硬要求带时区，所以裸值只
            # 可能来自手改 Markdown、导入外部记忆包或从旧备份恢复——而「文件可以
            # 手改」正是这套存储的卖点。最坏是早/晚解锁几小时，远好过永不解锁。
            reference = now or datetime.now(timezone.utc)
            expired = reference.astimezone(timezone.utc) >= parsed.astimezone(
                timezone.utc
            )
        except (TypeError, ValueError):
            expired = False
    effective_type = "none" if expired else lock_type
    owner = bool(locked_by and caller_side == locked_by)
    locked = effective_type != "none" and not owner
    return {
        "lock_type": effective_type,
        "stored_lock_type": lock_type,
        "unlock_date": None if expired else unlock_date,
        "locked_by": locked_by,
        "owner": owner,
        "locked": locked,
        "expired": expired,
    }


def letter_is_open_to_ai(bucket: dict, *, now: datetime | None = None) -> bool:
    """这封信此刻对 AI 是开着的吗——`you` / `them` 判能不能拿它当依据。

    上锁的信不能当依据：模型能拿一封自己还读不到的信去撑一条认识，而且
    「这封信里有没有出现某个名字」这种报错本身就是一次泄漏。
    """
    return not letter_lock_state(bucket, "ai", now=now)["locked"]
