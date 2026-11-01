from __future__ import annotations

import datetime
import math

import pytest

from ombrebrain.storage.metadata_normalize import (
    _MEANING_LIST_MAX_ITEMS,
    _MEDIA_MAX_ITEMS,
    _normalize_meaning_list,
    _normalize_media,
    _normalize_metadata_value,
    _sanitize_text,
)


@pytest.mark.parametrize(
    "raw, must_go",
    [
        ("带\x00NUL", "\x00"),
        ("bidi‮override", "‮"),
        ("isolate⁦x⁩", "⁦"),
        ("退格\x08", "\x08"),
        ("删除符\x7f", "\x7f"),
    ],
)
def test_control_and_bidi_characters_are_stripped(raw, must_go):
    assert must_go not in _sanitize_text(raw)


@pytest.mark.parametrize("keep", ["\n", "\r", "\t"])
def test_whitespace_survives(keep):
    assert keep in _sanitize_text(f"前{keep}后")


def test_emoji_and_cjk_survive():
    text = "emoji😀与CJK中文"
    assert _sanitize_text(text) == text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_numbers_do_not_reach_storage(value):
    out = _normalize_metadata_value(value)
    assert not isinstance(out, float) or math.isfinite(out)


def test_datetime_becomes_a_string():
    out = _normalize_metadata_value(datetime.datetime(2026, 8, 28, 12, 0, 0))
    assert isinstance(out, str)


def test_deep_nesting_is_refused_not_truncated():
    deep = value = {}
    for _ in range(200):
        value["next"] = {}
        value = value["next"]

    with pytest.raises(ValueError, match="nesting-depth"):
        _normalize_metadata_value(deep)


def test_shallow_nesting_passes_through():
    assert _normalize_metadata_value({"a": {"b": {"c": 1}}}) == {"a": {"b": {"c": 1}}}


def test_media_list_is_capped():
    out = _normalize_media([{"path": f"{i}.png"} for i in range(_MEDIA_MAX_ITEMS + 40)])
    assert len(out) <= _MEDIA_MAX_ITEMS


def test_media_rejects_non_dict_items():
    assert _normalize_media(["裸字符串"]) == []


def test_meaning_list_is_capped():
    out = _normalize_meaning_list([f"第 {i} 条" for i in range(_MEANING_LIST_MAX_ITEMS + 30)])
    assert len(out) <= _MEANING_LIST_MAX_ITEMS


def test_blank_meaning_entries_are_dropped():
    assert _normalize_meaning_list(["", "   ", "留下这条"]) == ["留下这条"]


def test_bucket_manager_still_exposes_the_same_entry_points():
    from bucket_manager import BucketManager

    for name in (
        "_sanitize_text",
        "_normalize_metadata_value",
        "_normalize_metadata_list",
        "_normalize_meaning_item",
        "_normalize_meaning_list",
        "_normalize_media",
    ):
        assert callable(getattr(BucketManager, name))

    assert BucketManager._sanitize_text("带\x00NUL") == _sanitize_text("带\x00NUL")
