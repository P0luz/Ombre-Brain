"""Configurable cold-start priority for default breath surfacing."""

import re
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default


class NoopDecay:
    def calculate_score(self, meta):
        return float(meta.get("importance") or 5)


class OrderedBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def list_all(self, include_archive=False):
        return list(self.buckets)


def _bucket(bucket_id: str, importance: int, activation_count: int) -> dict:
    return {
        "id": bucket_id,
        "content": f"content for {bucket_id}",
        "metadata": {
            "type": "dynamic",
            "importance": importance,
            "activation_count": activation_count,
            "domain": [],
        },
    }


def _install_runtime(buckets, cold_start_max_results):
    surfacing = {}
    if cold_start_max_results is not None:
        surfacing["cold_start_max_results"] = cold_start_max_results
    rt.config = {"surfacing": surfacing}
    rt.bucket_mgr = OrderedBucketManager(buckets)
    rt.decay_engine = NoopDecay()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


def _primary_ids(output: str) -> list[str]:
    primary = output.split("=== 浮现记忆 ===\n", 1)[1]
    primary = primary.split("\n\n=== ", 1)[0]
    return re.findall(r"\[bucket_id:([^\]]+)\]", primary)


@pytest.mark.parametrize(
    ("configured_limit", "max_results", "expected_ids"),
    [
        (0, 1, ["high"]),
        (1, 2, ["cold-a", "high"]),
        (2, 3, ["cold-a", "cold-b", "high"]),
        (None, 3, ["cold-a", "cold-b", "high"]),
    ],
)
@pytest.mark.asyncio
async def test_cold_start_priority_count_is_configurable(
    monkeypatch, configured_limit, max_results, expected_ids
):
    buckets = [
        _bucket("cold-a", importance=8, activation_count=0),
        _bucket("cold-b", importance=8, activation_count=0),
        _bucket("high", importance=10, activation_count=1),
        _bucket("next", importance=9, activation_count=1),
    ]
    _install_runtime(buckets, configured_limit)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=max_results,
        max_tokens=10000,
        tag_filter=[],
    )

    assert _primary_ids(output) == expected_ids


@pytest.mark.asyncio
async def test_zero_cold_start_allows_sampling_to_choose_any_ranked_candidate(
    monkeypatch,
):
    buckets = [
        _bucket("cold", importance=8, activation_count=0),
        _bucket("high", importance=10, activation_count=1),
        _bucket("next", importance=9, activation_count=1),
        _bucket("low", importance=7, activation_count=1),
    ]
    _install_runtime(buckets, 0)
    rt.config["surfacing"]["sampling"] = {
        "enabled": True,
        "top_k": 4,
        "sample_k": 1,
        "temperature": 1.0,
    }
    monkeypatch.setattr(
        "tools.breath.surface.random.choices",
        lambda population, weights, k: [len(population) - 1],
    )
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=1,
        max_tokens=10000,
        tag_filter=[],
    )

    assert _primary_ids(output) == ["low"]


@pytest.mark.asyncio
async def test_sampling_returns_only_picked_subset_when_max_results_covers_pool(
    monkeypatch,
):
    buckets = [
        _bucket("rank-1", importance=10, activation_count=1),
        _bucket("rank-2", importance=9, activation_count=1),
        _bucket("rank-3", importance=8, activation_count=1),
        _bucket("rank-4", importance=7, activation_count=1),
        _bucket("rank-5", importance=6, activation_count=1),
    ]
    _install_runtime(buckets, 0)
    rt.config["surfacing"]["sampling"] = {
        "enabled": True,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 1.0,
    }
    monkeypatch.setattr(
        "tools.breath.surface.random.choices",
        lambda population, weights, k: [len(population) - 1],
    )
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=5,
        max_tokens=10000,
        tag_filter=[],
    )

    assert _primary_ids(output) == ["rank-5", "rank-4"]


@pytest.mark.asyncio
async def test_sampling_never_expands_pool_when_sample_k_exceeds_top_k(
    monkeypatch,
):
    buckets = [
        _bucket("rank-1", importance=10, activation_count=1),
        _bucket("rank-2", importance=9, activation_count=1),
        _bucket("outside-pool", importance=8, activation_count=1),
    ]
    _install_runtime(buckets, 0)
    rt.config["surfacing"]["sampling"] = {
        "enabled": True,
        "top_k": 2,
        "sample_k": 4,
        "temperature": 1.0,
    }
    monkeypatch.setattr(
        "tools.breath.surface.random.choices",
        lambda population, weights, k: [len(population) - 1],
    )
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=5,
        max_tokens=10000,
        tag_filter=[],
    )

    assert _primary_ids(output) == ["rank-2", "rank-1"]
    assert "bucket_id:outside-pool" not in output


@pytest.mark.asyncio
async def test_zero_cold_start_does_not_reinsert_unvisited_bucket_as_passive(
    monkeypatch,
):
    buckets = [
        _bucket("cold", importance=8, activation_count=0),
        _bucket("high", importance=10, activation_count=1),
    ]
    _install_runtime(buckets, 0)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=1,
        max_tokens=10000,
        tag_filter=[],
    )

    assert _primary_ids(output) == ["high"]
    assert "bucket_id:cold" not in output


@pytest.mark.parametrize("invalid_limit", [True, 1.5, -1, 3])
@pytest.mark.asyncio
async def test_invalid_cold_start_limit_falls_back_to_two(
    monkeypatch,
    invalid_limit,
):
    buckets = [
        _bucket("cold-a", importance=8, activation_count=0),
        _bucket("cold-b", importance=8, activation_count=0),
        _bucket("high", importance=10, activation_count=1),
    ]
    _install_runtime(buckets, invalid_limit)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda items: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)

    output = await surface_default(
        max_results=3,
        max_tokens=10000,
        tag_filter=[],
    )

    assert _primary_ids(output) == ["cold-a", "cold-b", "high"]
