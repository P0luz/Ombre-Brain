"""Phase 3 Step 1 — collect_resurfaced_plans 纯筛选函数单测。

只测 tools.plan.core.collect_resurfaced_plans：给定 buckets、shown_ids、now，
选出 active + related_bucket ∈ shown_ids + window eligible 的 plan，按 created
倒序、按 plan id 去重。全部 deterministic，now 显式传入，不碰 surface_default。
"""

from datetime import datetime, timedelta, timezone

from tools.plan.core import collect_resurfaced_plans

TZ8 = timezone(timedelta(hours=8))

# 窗口 2026-11-15 ~ 11-23（+08）
WS = "2026-11-15T00:00:00+08:00"
WE = "2026-11-23T23:59:59.999999+08:00"

NOW_IN = datetime(2026, 11, 20, 12, 0, 0, tzinfo=TZ8)      # 窗内
NOW_BEFORE = datetime(2026, 11, 10, 0, 0, 0, tzinfo=TZ8)   # 窗前
NOW_AFTER = datetime(2026, 11, 25, 0, 0, 0, tzinfo=TZ8)    # 窗后


def _plan(pid, related, *, status="active", ws=WS, we=WE,
          created="2026-11-01T00:00:00+08:00", btype="plan"):
    return {
        "id": pid,
        "metadata": {
            "type": btype,
            "status": status,
            "related_bucket": related,
            "window_start": ws,
            "window_end": we,
            "created": created,
        },
    }


def _ids(rows):
    return [b["id"] for b in rows]


# --- 命中路径 ---
def test_picks_active_window_open_related_in_shown():
    buckets = [_plan("p1", "bkt1")]
    assert _ids(collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN)) == ["p1"]


def test_related_not_in_shown_excluded():
    buckets = [_plan("p1", "bkt1")]
    assert collect_resurfaced_plans(buckets, {"bkt2"}, NOW_IN) == []


def test_empty_related_excluded():
    buckets = [_plan("p1", "")]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN) == []


# --- status 过滤 ---
def test_resolved_excluded():
    buckets = [_plan("p1", "bkt1", status="resolved")]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN) == []


def test_abandoned_excluded():
    buckets = [_plan("p1", "bkt1", status="abandoned")]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN) == []


def test_dont_surface_excluded():
    plan = _plan("p1", "bkt1")
    plan["metadata"]["dont_surface"] = True
    assert collect_resurfaced_plans([plan], {"bkt1"}, NOW_IN) == []


def test_digested_excluded():
    plan = _plan("p1", "bkt1")
    plan["metadata"]["digested"] = "true"
    assert collect_resurfaced_plans([plan], {"bkt1"}, NOW_IN) == []


# --- window 过滤（复用 is_window_open）---
def test_window_not_yet_open_excluded():
    buckets = [_plan("p1", "bkt1")]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_BEFORE) == []


def test_window_already_closed_excluded():
    buckets = [_plan("p1", "bkt1")]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_AFTER) == []


def test_no_window_active_plan_excluded():
    # 无 window 的 active plan 不因 related 命中而 resurface
    buckets = [_plan("p1", "bkt1", ws=None, we=None)]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN) == []


# --- 非 plan / 空 shown ---
def test_non_plan_bucket_excluded():
    buckets = [_plan("p1", "bkt1", btype="dynamic")]
    assert collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN) == []


def test_empty_shown_returns_empty():
    buckets = [_plan("p1", "bkt1")]
    assert collect_resurfaced_plans(buckets, set(), NOW_IN) == []
    assert collect_resurfaced_plans(buckets, None, NOW_IN) == []


# --- 多 plan 指向同一桶：都选（不同 plan id 不去重）---
def test_multiple_plans_same_bucket_all_picked():
    buckets = [_plan("p1", "bkt1"), _plan("p2", "bkt1")]
    out = _ids(collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN))
    assert set(out) == {"p1", "p2"}


# --- created 倒序（新→旧）---
def test_created_desc_order():
    early = _plan("p_old", "bkt1", created="2026-10-01T00:00:00+08:00")
    late = _plan("p_new", "bkt1", created="2026-11-05T00:00:00+08:00")
    buckets = [early, late]  # 故意乱序放
    assert _ids(collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN)) == ["p_new", "p_old"]


# --- 混合：只返回 eligible ---
def test_mixed_only_eligible_returned():
    buckets = [
        _plan("p1", "bkt1"),                      # 命中
        _plan("p2", "bkt2"),                      # related 不在 shown
        _plan("p3", "bkt1", status="resolved"),   # 非 active
        _plan("p4", "bkt1", ws=None, we=None),    # 无窗口
        _plan("p5", "bkt1", created="2026-11-08T00:00:00+08:00"),  # 命中（更晚）
    ]
    out = _ids(collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN))
    assert out == ["p5", "p1"]  # created DESC，p5 更晚在前


def test_status_active_with_whitespace_and_case_included():
    """status 归一化：`" Active "` → `"active"` → 入选（不是排除）。"""
    buckets = [_plan("p1", "bkt1", status=" Active ")]
    assert _ids(collect_resurfaced_plans(buckets, {"bkt1"}, NOW_IN)) == ["p1"]
