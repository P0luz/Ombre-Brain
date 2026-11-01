from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tools import _runtime as rt
from tools.dream.hints import (
    _MAX_SELF_CANDIDATES_PER_DREAM,
    collect_self_candidates,
)
from tools.i import core as i_core


def _candidate(bucket_id, *, created, passes=(), **meta):
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
            **meta,
        },
    }


class _FakeBucketManager:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)

    async def update(self, bucket_id, **kwargs):
        bucket = self.buckets.get(bucket_id)
        if not bucket:
            return False
        bucket["metadata"].update(kwargs)
        return True

    async def list_all(self, include_archive: bool = False):
        return list(self.buckets.values())


class _NoopDecay:
    async def ensure_started(self) -> None:
        return None


def _env(monkeypatch, buckets):
    manager = _FakeBucketManager(buckets)
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay(), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(
        rt, "logger", __import__("logging").getLogger("test"), raising=False
    )
    return manager


@pytest.mark.asyncio
async def test_starved_candidates_are_still_counted_as_offered(monkeypatch):
    """没排上的也算「这场梦它在队列里」。

    见证数回答「被看见过几次」，offered 回答「本可以被看见几次」。只有前者时，
    「等了 13 天还是 0/3」既可能是没做几次梦，也可能是每场都没排到——没法分辨。
    """
    monkeypatch.setattr("tools.dream.hints.rt.embedding_engine", None)
    buckets = [
        _candidate(f"c{i}", created=f"2026-08-{i + 1:02d}")
        for i in range(_MAX_SELF_CANDIDATES_PER_DREAM + 3)
    ]

    review = await collect_self_candidates(buckets, window_hours=48)

    assert review.starved == 3
    assert len(review.candidates) == _MAX_SELF_CANDIDATES_PER_DREAM
    assert set(review.pending_ids) == {b["id"] for b in buckets}


@pytest.mark.asyncio
async def test_offer_counts_once_per_day(monkeypatch):
    manager = _env(monkeypatch, [_candidate("c1", created="2026-08-01")])

    assert await i_core.record_dream_offer(["c1"]) == 1
    assert await i_core.record_dream_offer(["c1"]) == 0

    meta = manager.buckets["c1"]["metadata"]
    assert meta["i_dream_offered"] == 1

    # 换一天再做梦：计数才该往前走。
    meta["i_dream_offered_last"] = "2020-01-01"
    assert await i_core.record_dream_offer(["c1"]) == 1
    assert meta["i_dream_offered"] == 2


@pytest.mark.asyncio
async def test_offer_skips_buckets_that_are_no_longer_candidates(monkeypatch):
    manager = _env(
        monkeypatch,
        [_candidate("c1", created="2026-08-01", i_stage="promoted")],
    )

    assert await i_core.record_dream_offer(["c1", "missing"]) == 0
    assert "i_dream_offered" not in manager.buckets["c1"]["metadata"]


@pytest.mark.asyncio
async def test_read_separates_never_dreamt_from_never_picked(monkeypatch):
    created = (datetime.now() - timedelta(days=13)).isoformat()
    _env(
        monkeypatch,
        [
            _candidate("starved", created=created, i_dream_offered=8),
            _candidate("untouched", created=created),
        ],
    )

    out = await i_core.i_core(read=True)

    # 同样是「等了 13 天、0/3」，这两条的原因完全不同。
    assert "经历 8 场梦，一次都没排到" in out
    assert "还没经历过 dream" in out
    assert "已等 13 天" in out
