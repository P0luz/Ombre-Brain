from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utils import now_iso, parse_iso_datetime

LEGACY = "2026-08-17T17:34:17"
AWARE_POSITIVE = "2026-08-17T17:34:17+01:00"
AWARE_NEGATIVE = "2026-08-17T17:34:17-05:00"
AWARE_Z = "2026-08-17T17:34:17Z"


def test_now_iso_is_self_describing():
    assert datetime.fromisoformat(now_iso()).utcoffset() is not None


@pytest.mark.parametrize("value", [LEGACY, AWARE_POSITIVE, AWARE_NEGATIVE, AWARE_Z])
def test_every_format_parses_to_a_naive_local_datetime(value):
    parsed = parse_iso_datetime(value)
    assert parsed.tzinfo is None


def test_offsets_are_honoured_not_ignored():
    east = parse_iso_datetime(AWARE_POSITIVE)
    west = parse_iso_datetime(AWARE_NEGATIVE)
    assert west - east == timedelta(hours=6)


def test_z_and_zero_offset_agree():
    assert parse_iso_datetime(AWARE_Z) == parse_iso_datetime(
        "2026-08-17T17:34:17+00:00"
    )


# ---- 这两处以前直接 fromisoformat，带偏移的值会让它们炸 ----

def test_i_stall_diagnostics_survive_an_offset_timestamp():
    from tools.i.core import _stall_note

    meta = {"created": now_iso(), "i_dream_dates": []}
    assert isinstance(_stall_note(meta, 0), str)


def test_i_stall_diagnostics_report_days_for_an_old_offset_timestamp():
    from tools.i.core import _stall_note

    old = (datetime.now(timezone.utc) - timedelta(days=5)).astimezone().isoformat(
        timespec="seconds"
    )
    assert "5 天" in _stall_note({"created": old, "i_dream_dates": []}, 0)


@pytest.mark.parametrize("value", [LEGACY, AWARE_POSITIVE, AWARE_NEGATIVE, AWARE_Z])
def test_relation_link_reads_every_format(value):
    from tools._relation_link import _created_at

    assert _created_at({"created": value}) is not None


def test_relation_link_measures_a_negative_offset_correctly():
    from tools._relation_link import _hours_apart

    gap = _hours_apart({"created": AWARE_POSITIVE}, {"created": AWARE_NEGATIVE})
    assert gap == pytest.approx(6.0)


def test_relation_link_returns_none_for_garbage():
    from tools._relation_link import _created_at

    assert _created_at({"created": "不是时间"}) is None
    assert _created_at({}) is None


# ---- plan 改动历史（报告人的第 3 条：与同子系统的 unlock_date 口径一致）----

def test_plan_change_log_entries_carry_an_offset():
    from ombrebrain.domain.plan_history import append_plan_change_log

    entry = append_plan_change_log([], "created")[0]
    assert datetime.fromisoformat(entry["ts"]).utcoffset() is not None


def test_plan_change_log_stays_append_only():
    from ombrebrain.domain.plan_history import append_plan_change_log

    first = append_plan_change_log([], "created")
    second = append_plan_change_log(first, "resolved", note="done")

    assert len(first) == 1
    assert len(second) == 2
    assert second[0] == first[0]
    assert second[1]["action"] == "resolved"
    assert second[1]["note"] == "done"


# ---- 不该带偏移的地方：文件名与桶 ID ----

def test_bucket_names_keep_a_filename_safe_timestamp():
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/bucket_manager.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r'_ts = datetime\.now\(\)\.strftime\("%Y-%m-%d %H-%M-%S"\)', source)


def test_build_feel_ids_stay_sortable_digits():
    from tools.hold.feel import _build_feel_id

    bucket_id = _build_feel_id(0.8)
    stamp = bucket_id.split("_")[1]
    assert stamp.isdigit() and len(stamp) == 12
