"""创建时间区间过滤 —— breath 五条分支共用。

原来这两个函数长在 `search.py` 里，于是只有检索分支认 `date_from`/`date_to`。
`breath_advanced(date_to="2026-07-01")` 不带 query 时会静默返回 8 月的桶：
参数收下了、schema 也认，就是没人用它。**这是最难发现的那类错——不是报错，
是无声地给了你没要的东西。**

放在这里是为了让「接上日期过滤」变成一次 import，而不是每条分支各抄一份
边界判断。抄的那份迟早会和这份不一样。
"""

from __future__ import annotations

from datetime import datetime, time

from errors import ToolInputError
from utils import parse_iso_datetime

_INVALID = "日期格式无效，请使用 YYYY-MM-DD 或 ISO 8601 时间。"
_REVERSED = "date_from 不能晚于 date_to。"


def parse_date_bound(value: str, *, upper: bool) -> datetime | None:
    """解析创建时间边界；YYYY-MM-DD 的上界包含当天全日。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = parse_iso_datetime(raw)
    if len(raw) == 10:
        day = parsed.date()
        return datetime.combine(day, time.max if upper else time.min)
    return parsed


def parse_created_range(
    date_from: str, date_to: str
) -> tuple[datetime | None, datetime | None]:
    """解析并校验一对边界，非法输入直接抛 ToolInputError。

    在 `dispatch()` 里统一调用一次，五条分支拿到的是同一对已校验的边界——
    分支各自解析的话，「哪条分支对 `2026-13-01` 报错」会变成一件要逐个试的事。
    """
    try:
        created_from = parse_date_bound(date_from, upper=False)
        created_to = parse_date_bound(date_to, upper=True)
    except (TypeError, ValueError):
        raise ToolInputError(_INVALID)
    if created_from and created_to and created_from > created_to:
        raise ToolInputError(_REVERSED)
    return created_from, created_to


def bucket_in_created_range(
    bucket: dict,
    created_from: datetime | None,
    created_to: datetime | None,
) -> bool:
    """没有边界时一律放行；`created` 缺失或写坏时在有边界的情况下排除。

    排除而不是放行：调用方给了时间范围，就是明确说「只要这段时间里的」。
    读不出创建时间的桶无法证明自己在范围内，放行等于悄悄破坏这个约定。
    """
    if created_from is None and created_to is None:
        return True
    raw_created = str((bucket.get("metadata") or {}).get("created") or "").strip()
    if not raw_created:
        return False
    try:
        created = parse_iso_datetime(raw_created)
    except (TypeError, ValueError):
        return False
    if created_from is not None and created < created_from:
        return False
    if created_to is not None and created > created_to:
        return False
    return True
