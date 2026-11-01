import pytest

from tools.dream.hints import (
    _MAX_SELF_CANDIDATES_PER_DREAM,
    collect_self_candidates,
)
from tools.i import I_PROMOTE_THRESHOLD


def _candidate(bucket_id, *, created, passes=()):
    return {
        "id": bucket_id,
        "content": f"我觉得 {bucket_id}",
        "metadata": {
            "type": "dynamic",
            "tags": ["__i_candidate__"],
            "i_stage": "candidate",
            "i_dream_dates": list(passes),
            "created": created,
            "importance": 6,
        },
    }


@pytest.mark.asyncio
async def test_the_neediest_candidate_comes_first(monkeypatch):
    """按「还差几次见证」排，不按 created。

    原先是 created 升序 + 无上限，而渲染逐条撞预算、撞满即丢且不计见证——
    新写的候选永远排在队尾，于是永远拿不到见证、永远转不了正。
    """
    monkeypatch.setattr("tools.dream.hints.rt.embedding_engine", None)
    buckets = [
        _candidate("old-but-full", created="2026-01-01", passes=["a", "b"]),
        _candidate("new-and-empty", created="2026-08-01", passes=[]),
    ]

    review = await collect_self_candidates(buckets, window_hours=48)

    assert [c.bucket["id"] for c in review.candidates] == [
        "new-and-empty",
        "old-but-full",
    ]


@pytest.mark.asyncio
async def test_candidates_with_enough_witnesses_move_out_of_the_queue(monkeypatch):
    monkeypatch.setattr("tools.dream.hints.rt.embedding_engine", None)
    ready_passes = [f"2026-08-{i:02d}" for i in range(1, I_PROMOTE_THRESHOLD + 1)]
    buckets = [
        _candidate("ready", created="2026-01-01", passes=ready_passes),
        _candidate("growing", created="2026-08-01", passes=[]),
    ]

    review = await collect_self_candidates(buckets, window_hours=48)

    assert [c.bucket["id"] for c in review.candidates] == ["growing"]
    assert [c.bucket["id"] for c in review.ready] == ["ready"]


@pytest.mark.asyncio
async def test_the_growing_queue_is_capped(monkeypatch):
    monkeypatch.setattr("tools.dream.hints.rt.embedding_engine", None)
    buckets = [
        _candidate(f"c{i}", created=f"2026-08-{i + 1:02d}")
        for i in range(_MAX_SELF_CANDIDATES_PER_DREAM + 3)
    ]

    review = await collect_self_candidates(buckets, window_hours=48)

    assert len(review.candidates) == _MAX_SELF_CANDIDATES_PER_DREAM
    assert review.starved == 3


@pytest.mark.asyncio
async def test_equally_needy_candidates_rotate_by_least_recently_seen(monkeypatch):
    """同样缺见证时，最久没被见证的先来——否则固定几条会把名额包了。"""
    monkeypatch.setattr("tools.dream.hints.rt.embedding_engine", None)
    buckets = [
        _candidate("seen-recently", created="2026-01-01", passes=["2026-08-20"]),
        _candidate("seen-long-ago", created="2026-01-02", passes=["2026-08-01"]),
    ]

    review = await collect_self_candidates(buckets, window_hours=48)

    assert [c.bucket["id"] for c in review.candidates] == [
        "seen-long-ago",
        "seen-recently",
    ]


@pytest.mark.asyncio
async def test_an_old_candidate_is_not_dropped_by_the_window(monkeypatch):
    """候选不受近期窗口限制——这是既有契约，别在排序改动里弄丢。"""
    monkeypatch.setattr("tools.dream.hints.rt.embedding_engine", None)
    buckets = [_candidate("ancient", created="2020-01-01")]

    review = await collect_self_candidates(buckets, window_hours=1)

    assert [c.bucket["id"] for c in review.candidates] == ["ancient"]
