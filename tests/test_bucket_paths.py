from __future__ import annotations

import os

import pytest

from ombrebrain.storage.bucket_paths import (
    has_ambiguous_letter_marker,
    has_strong_letter_marker,
    path_is_within,
    same_path,
)


def test_file_inside_the_directory_is_within(tmp_path):
    target = tmp_path / "buckets" / "a.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    assert path_is_within(str(target), str(tmp_path / "buckets")) is True


def test_dot_dot_escape_is_refused(tmp_path):
    inside = tmp_path / "buckets"
    inside.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("x", encoding="utf-8")

    escaped = str(inside / ".." / "secret.md")
    assert path_is_within(escaped, str(inside)) is False


def test_sibling_directory_is_not_within(tmp_path):
    (tmp_path / "buckets").mkdir()
    (tmp_path / "buckets-backup").mkdir()
    victim = tmp_path / "buckets-backup" / "a.md"
    victim.write_text("x", encoding="utf-8")

    assert path_is_within(str(victim), str(tmp_path / "buckets")) is False


@pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt",
    reason="需要无需提权的 symlink",
)
def test_symlink_pointing_outside_is_refused(tmp_path):
    inside = tmp_path / "buckets"
    inside.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = outside / "a.md"
    real.write_text("x", encoding="utf-8")
    link = inside / "link.md"
    os.symlink(real, link)

    assert path_is_within(str(link), str(inside)) is False


def test_same_path_normalises_relative_forms(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("x", encoding="utf-8")

    assert same_path(str(target), str(tmp_path / "." / "a.md")) is True
    assert same_path(str(target), str(tmp_path / "b.md")) is False


@pytest.mark.parametrize(
    "post",
    [
        {"source_tool": "letter"},
        {"source_tool": "LETTER"},
        {"tags": ["__letter__"]},
        {"tags": "__letter__,other"},
    ],
)
def test_strong_letter_markers(post):
    assert has_strong_letter_marker(post) is True


@pytest.mark.parametrize(
    "post",
    [{}, {"source_tool": "hold"}, {"tags": ["letter"]}, {"tags": 123}, {"domain": ["letter"]}],
)
def test_domain_alone_is_not_a_strong_marker(post):
    assert has_strong_letter_marker(post) is False


def test_domain_letter_is_the_ambiguous_case():
    assert has_ambiguous_letter_marker({"domain": ["letter"]}) is True
    assert has_ambiguous_letter_marker({"domain": "letter"}) is True
    assert has_ambiguous_letter_marker({"domain": ["work"]}) is False
    assert has_ambiguous_letter_marker({}) is False


def test_bucket_manager_still_exposes_the_same_entry_points():
    from bucket_manager import BucketManager

    for name in (
        "_same_path",
        "_path_is_within",
        "_has_strong_letter_marker",
        "_has_ambiguous_letter_marker",
    ):
        assert callable(getattr(BucketManager, name))
