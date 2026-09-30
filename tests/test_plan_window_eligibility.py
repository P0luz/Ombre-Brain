"""Phase 2 — window eligibility 纯函数 `is_window_open` 的独立单元测试。

只测 `tools.plan.core.is_window_open`：给定 Plan metadata 与显式 `now`，判断该
Plan 是否具备 window resurfacing eligibility。全部 deterministic——`now` 显式传入，
不依赖系统时钟，不做 Phase 3 / resurfacing / relation 相关的任何事。
"""

from datetime import datetime, timedelta, timezone

import pytest

from tools.plan.core import is_window_open

TZ8 = timezone(timedelta(hours=8))
UTC = timezone.utc

# 模拟 Phase 1 归一化后落盘的窗口值（带显式偏移）。
# window_start = 2026-11-15 00:00:00+08:00  == 2026-11-14 16:00:00Z
# window_end   = 2026-11-23 23:59:59.999999+08:00 == 2026-11-23 15:59:59.999999Z
WS = "2026-11-15T00:00:00+08:00"
WE = "2026-11-23T23:59:59.999999+08:00"


def _meta(status="active", **kw):
    m = {"status": status}
    m.update(kw)
    return m


# ---------------------------------------------------------------------------
# 1–5：active + 双边 window
# ---------------------------------------------------------------------------
def test_active_dual_before_start_false():
    now = datetime(2026, 11, 14, 23, 0, 0, tzinfo=TZ8)  # UTC 15:00 < start 16:00
    assert is_window_open(_meta(window_start=WS, window_end=WE), now) is False


def test_active_dual_now_equals_start_true():
    now = datetime(2026, 11, 15, 0, 0, 0, tzinfo=TZ8)  # == window_start
    assert is_window_open(_meta(window_start=WS, window_end=WE), now) is True


def test_active_dual_inside_true():
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta(window_start=WS, window_end=WE), now) is True


def test_active_dual_now_equals_end_true():
    now = datetime(2026, 11, 23, 23, 59, 59, 999999, tzinfo=TZ8)  # == window_end
    assert is_window_open(_meta(window_start=WS, window_end=WE), now) is True


def test_active_dual_after_end_false():
    now = datetime(2026, 11, 24, 0, 0, 0, tzinfo=TZ8)  # 一微秒都算过窗
    assert is_window_open(_meta(window_start=WS, window_end=WE), now) is False


# ---------------------------------------------------------------------------
# 6：只有 window_start
# ---------------------------------------------------------------------------
def test_start_only_before_start_false():
    now = datetime(2026, 11, 14, 23, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta(window_start=WS), now) is False


def test_start_only_at_start_true():
    now = datetime(2026, 11, 15, 0, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta(window_start=WS), now) is True


def test_start_only_after_start_true():
    now = datetime(2027, 3, 1, 0, 0, 0, tzinfo=TZ8)  # 很久以后仍开着（无 end）
    assert is_window_open(_meta(window_start=WS), now) is True


# ---------------------------------------------------------------------------
# 7：只有 window_end
# ---------------------------------------------------------------------------
def test_end_only_before_end_true():
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=TZ8)  # end 之前（无 start）
    assert is_window_open(_meta(window_end=WE), now) is True


def test_end_only_at_end_true():
    now = datetime(2026, 11, 23, 23, 59, 59, 999999, tzinfo=TZ8)
    assert is_window_open(_meta(window_end=WE), now) is True


def test_end_only_after_end_false():
    now = datetime(2026, 11, 24, 0, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta(window_end=WE), now) is False


# ---------------------------------------------------------------------------
# 8：active 但完全无 window -> False（不给历史 active Plan 凭空资格）
# ---------------------------------------------------------------------------
def test_active_no_window_false():
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta(), now) is False


# ---------------------------------------------------------------------------
# 9–10：非 active 状态即便时间在窗口内也 False
# ---------------------------------------------------------------------------
def test_resolved_inside_window_false():
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta("resolved", window_start=WS, window_end=WE), now) is False


def test_abandoned_inside_window_false():
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=TZ8)
    assert is_window_open(_meta("abandoned", window_start=WS, window_end=WE), now) is False


def test_missing_status_inside_window_false():
    """防御：status 缺失也视为非 active。"""
    now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=TZ8)
    meta = {"window_start": WS, "window_end": WE}  # 无 status
    assert is_window_open(meta, now) is False


# ---------------------------------------------------------------------------
# 11：跨时区等价 instant —— 比较遵循 Phase 1 timezone 语义
# ---------------------------------------------------------------------------
def test_cross_timezone_inside_via_utc():
    meta = _meta(window_start=WS, window_end=WE)
    # 2026-11-20 04:00Z == 2026-11-20 12:00+08，窗口内
    now = datetime(2026, 11, 20, 4, 0, 0, tzinfo=UTC)
    assert is_window_open(meta, now) is True


def test_cross_timezone_just_before_start_utc():
    meta = _meta(window_start=WS, window_end=WE)
    # 比 start(16:00Z) 早一秒，用 UTC 表示
    now = datetime(2026, 11, 14, 15, 59, 59, tzinfo=UTC)
    assert is_window_open(meta, now) is False


# ---------------------------------------------------------------------------
# now contract（Phase 2 修复）：naive now 明确按 UTC，不按项目配置本地时区补全
# ---------------------------------------------------------------------------
# 杀手锏窗口：[00:00Z, 04:00Z]，naive now = 02:00
#   正确（按 UTC）：02:00Z 落在窗内 -> True
#   错误（按 +08）：02:00+08 == 前一天 18:00Z 落在窗外 -> False
# 断言 True，就锁死了本次修复；若回归成本地补全，这条会红。
_UTC_WIN_START = "2026-11-15T00:00:00+00:00"
_UTC_WIN_END = "2026-11-15T04:00:00+00:00"


def test_A_naive_now_interpreted_as_utc_not_local():
    meta = _meta(window_start=_UTC_WIN_START, window_end=_UTC_WIN_END)
    naive_now = datetime(2026, 11, 15, 2, 0, 0)  # naive，无 tzinfo
    assert is_window_open(meta, naive_now) is True


def test_B_aware_utc_equals_aware_plus8_same_instant():
    meta = _meta(window_start=_UTC_WIN_START, window_end=_UTC_WIN_END)
    aware_utc = datetime(2026, 11, 15, 2, 0, 0, tzinfo=UTC)   # 02:00Z
    aware_p8 = datetime(2026, 11, 15, 10, 0, 0, tzinfo=TZ8)   # 10:00+08 == 02:00Z
    assert is_window_open(meta, aware_utc) == is_window_open(meta, aware_p8)
    assert is_window_open(meta, aware_utc) is True


def test_C_naive_utc_now_equals_aware_utc_now():
    meta = _meta(window_start=_UTC_WIN_START, window_end=_UTC_WIN_END)
    naive_now = datetime(2026, 11, 15, 2, 0, 0)
    aware_utc_now = datetime(2026, 11, 15, 2, 0, 0, tzinfo=UTC)
    assert is_window_open(meta, naive_now) == is_window_open(meta, aware_utc_now)
    assert is_window_open(meta, naive_now) is True


def test_D_now_none_raises_type_error():
    meta = _meta(window_start=WS, window_end=WE)
    with pytest.raises(TypeError):
        is_window_open(meta, None)


def test_E_now_unsupported_type_raises_type_error():
    meta = _meta(window_start=WS, window_end=WE)
    with pytest.raises(TypeError):
        is_window_open(meta, "2026-11-15")
    with pytest.raises(TypeError):
        is_window_open(meta, 1763200000)
