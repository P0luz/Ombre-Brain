"""Shared, read-only display helpers for letter metadata."""

from datetime import date, datetime, timedelta, timezone


# Asia/Tokyo has no daylight-saving transitions, so a standard-library fixed
# offset avoids requiring the optional ``tzdata`` package on Windows.
TOKYO = timezone(timedelta(hours=9), name="Asia/Tokyo")


def _tokyo_date(value: object) -> date | None:
    """Parse an ISO date/datetime and normalize its calendar date to Tokyo."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
            )
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TOKYO)
    else:
        parsed = parsed.astimezone(TOKYO)
    return parsed.date()


def format_letter_written_age(metadata: dict, *, now: datetime | None = None) -> str:
    """Return a Tokyo-calendar display label without changing persisted metadata."""
    metadata = metadata if isinstance(metadata, dict) else {}
    raw = (
        metadata.get("letter_date")
        or metadata.get("date")
        or metadata.get("created")
        or ""
    )
    written = _tokyo_date(raw)
    if written is None:
        raw_text = str(raw).strip()
        return f"写于 {raw_text} · 日期未知" if raw_text else "写于 日期未知"

    current = _tokyo_date(now or datetime.now(TOKYO))
    if current is None:  # defensive only; the default above is always valid
        return f"写于 {written.isoformat()} · 日期未知"
    days = (current - written).days
    if days < 0:
        age = "日期未知"
    elif days == 0:
        age = "今天"
    else:
        age = f"{days} 天前"
    return f"写于 {written.isoformat()} · {age}"
