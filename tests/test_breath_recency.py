from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default


class DisabledEmbedding:
    enabled = False


class WeightedDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("_score", metadata.get("importance") or 5))


class PlainBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def get_stats(self):
        return {"permanent_count": 0, "dynamic_count": len(self.buckets)}

    def footprint_snapshot(self):
        raise RuntimeError("no footprint in tests")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _bucket(bucket_id, *, age_days=0, age_hours=0, score=1.0,
            importance=5, activation_count=3):
    created = datetime.now() - timedelta(days=age_days, hours=age_hours)
    return {
        "id": bucket_id,
        "content": f"{bucket_id} 的正文。",
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "importance": importance,
            "domain": ["回归测试"],
            "created": _iso(created),
            "last_active": _iso(created),
            "activation_count": activation_count,
            "_score": score,
        },
    }


def _install(monkeypatch, manager, surfacing=None):
    monkeypatch.setattr(rt, "config", {"surfacing": surfacing or {}})
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", WeightedDecay())
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda _seq: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)


@pytest.mark.asyncio
async def test_recent_buckets_get_reserved_slots_against_high_weight_veterans(
    monkeypatch,
):
    veterans = [
        _bucket(f"old-{i}", age_days=30, score=51.0 - i) for i in range(10)
    ]
    fresh = [_bucket(f"new-{i}", age_days=i, score=0.5) for i in range(3)]
    _install(monkeypatch, PlainBucketManager(veterans + fresh))

    out = await surface_default(max_results=10, max_tokens=100_000, tag_filter=[])

    surfaced = [b["id"] for b in veterans + fresh if f"[bucket_id:{b['id']}]" in out]
    assert any(bid.startswith("new-") for bid in surfaced), surfaced
    assert sum(1 for bid in surfaced if bid.startswith("new-")) == 3
    assert "[bucket_id:old-0]" in out


@pytest.mark.asyncio
async def test_recent_slots_are_sorted_newest_first(monkeypatch):
    fresh = [_bucket(f"new-{i}", age_days=i, score=0.5) for i in range(3)]
    veterans = [_bucket(f"old-{i}", age_days=30, score=51.0) for i in range(10)]
    _install(monkeypatch, PlainBucketManager(veterans + fresh))

    out = await surface_default(max_results=10, max_tokens=100_000, tag_filter=[])

    positions = [out.index(f"[bucket_id:new-{i}]") for i in range(3)]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_buckets_older_than_the_window_do_not_claim_the_quota(monkeypatch):
    veterans = [_bucket(f"old-{i}", age_days=30, score=51.0) for i in range(10)]
    stale = _bucket("eight-days", age_days=8, score=0.5)
    _install(monkeypatch, PlainBucketManager(veterans + [stale]))

    out = await surface_default(max_results=5, max_tokens=100_000, tag_filter=[])

    assert "[bucket_id:eight-days]" not in out


@pytest.mark.asyncio
async def test_quota_never_takes_more_than_half_the_surface(monkeypatch):
    veterans = [_bucket(f"old-{i}", age_days=30, score=51.0) for i in range(10)]
    fresh = [_bucket(f"new-{i}", age_days=0, score=0.5) for i in range(5)]
    _install(monkeypatch, PlainBucketManager(veterans + fresh))

    out = await surface_default(max_results=4, max_tokens=100_000, tag_filter=[])

    new_count = sum(1 for i in range(5) if f"[bucket_id:new-{i}]" in out)
    assert new_count <= 2, f"4 个位置里新桶占了 {new_count} 个"


@pytest.mark.asyncio
async def test_recent_slots_zero_restores_the_old_behaviour(monkeypatch):
    veterans = [_bucket(f"old-{i}", age_days=30, score=51.0 - i) for i in range(10)]
    fresh = [_bucket("new-0", age_days=0, score=0.5)]
    _install(
        monkeypatch,
        PlainBucketManager(veterans + fresh),
        surfacing={"recent_slots": 0},
    )

    out = await surface_default(max_results=5, max_tokens=100_000, tag_filter=[])

    assert "[bucket_id:new-0]" not in out


@pytest.mark.asyncio
async def test_quota_is_a_noop_when_everything_is_already_recent(monkeypatch):
    fresh = [_bucket(f"new-{i}", age_days=0, score=10.0 - i) for i in range(5)]
    _install(monkeypatch, PlainBucketManager(fresh))

    out = await surface_default(max_results=5, max_tokens=100_000, tag_filter=[])

    positions = [out.index(f"[bucket_id:new-{i}]") for i in range(5)]
    assert positions == sorted(positions)


@pytest.mark.asyncio
async def test_a_bucket_created_minutes_ago_is_not_long_unsurfaced(
    monkeypatch,
):
    fresh_important = [
        _bucket(f"fresh-{i}", age_hours=0, score=0.5,
                importance=9, activation_count=0)
        for i in range(4)
    ]
    filler = [_bucket(f"old-{i}", age_days=30, score=20.0) for i in range(5)]
    _install(monkeypatch, PlainBucketManager(filler + fresh_important))

    out = await surface_default(max_results=5, max_tokens=100_000, tag_filter=[])

    passive_section = out.split("=== 久未浮现 ===")
    if len(passive_section) > 1:
        for i in range(4):
            assert f"[bucket_id:fresh-{i}]" not in passive_section[1]


@pytest.mark.asyncio
async def test_an_old_never_activated_bucket_still_counts_as_long_unsurfaced(
    monkeypatch,
):
    decoys = [
        _bucket(f"cold-{i}", age_days=40, score=30.0,
                importance=9, activation_count=0)
        for i in range(2)
    ]
    forgotten = _bucket(
        "forgotten", age_days=40, score=0.1, importance=9, activation_count=0
    )
    filler = [_bucket(f"old-{i}", age_days=30, score=20.0) for i in range(5)]
    _install(monkeypatch, PlainBucketManager(decoys + [forgotten] + filler))

    out = await surface_default(max_results=3, max_tokens=100_000, tag_filter=[])

    assert "=== 久未浮现 ===" in out
    assert "[bucket_id:forgotten]" in out.split("=== 久未浮现 ===")[1]
