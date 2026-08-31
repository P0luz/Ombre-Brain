from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from tools.breath import dispatch as breath_dispatch
from tools.trace import dispatch as trace_dispatch


class DisabledEmbedding:
    enabled = False


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 5)


class CountingBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)
        self.touched: list[str] = []

    async def get(self, bucket_id):
        for b in self.buckets:
            if b["id"] == bucket_id:
                return b
        return None

    async def get_including_archive(self, bucket_id):
        return await self.get(bucket_id)

    async def search(self, _query, **_kwargs):
        return list(self.buckets)

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def touch(self, bucket_id, ripple=True):
        self.touched.append(bucket_id)

    async def touch_many(self, bucket_ids, ripple=False):
        self.touched.extend(bucket_ids)

    async def get_stats(self):
        return {"permanent_count": 0, "dynamic_count": len(self.buckets)}

    def footprint_snapshot(self):
        raise RuntimeError("no footprint in tests")


def _bucket(bucket_id, content, *, activation_count=3):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "importance": 7,
            "domain": ["回归测试"],
            "created": "2026-08-01T10:00:00",
            "last_active": "2026-08-01T10:00:00",
            "activation_count": activation_count,
        },
    }


@pytest.fixture
def manager(monkeypatch):
    mgr = CountingBucketManager([
        _bucket("hit-one", "被反复查询的那条记忆。"),
        _bucket("hit-two", "另一条也会命中的记忆。"),
    ])
    monkeypatch.setattr(rt, "config", {"surfacing": {}})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "deletion_requests", None, raising=False)
    monkeypatch.setattr(rt, "them_service", None, raising=False)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)
    return mgr


@pytest.mark.asyncio
async def test_query_search_reinforces_nothing(manager):
    out = await breath_dispatch(query="记忆")

    assert "被反复查询的那条记忆。" in out
    assert manager.touched == []


@pytest.mark.asyncio
async def test_repeated_queries_never_accumulate_weight(manager):
    for _ in range(20):
        await breath_dispatch(query="记忆")

    assert manager.touched == []


@pytest.mark.asyncio
async def test_exact_bucket_id_lookup_reinforces_nothing(manager):
    out = await breath_dispatch(query="hit-one")

    assert "被反复查询的那条记忆。" in out
    assert manager.touched == []


@pytest.mark.asyncio
async def test_default_surfacing_still_reinforces_nothing(manager):
    await breath_dispatch()

    assert manager.touched == []


@pytest.mark.asyncio
async def test_explicit_reinforce_touches_exactly_that_bucket(manager):
    result = await trace_dispatch(bucket_id="hit-one", reinforce=True)

    assert "已强化" in result
    assert manager.touched == ["hit-one"]


@pytest.mark.asyncio
async def test_explicit_reinforce_reports_the_new_count(manager):
    result = await trace_dispatch(bucket_id="hit-one", reinforce=True)

    assert "3" in result and "4" in result


@pytest.mark.asyncio
async def test_reinforce_is_per_bucket_not_per_candidate_set(manager):
    await breath_dispatch(query="记忆")
    await trace_dispatch(bucket_id="hit-one", reinforce=True)

    assert manager.touched == ["hit-one"]
    assert "hit-two" not in manager.touched


@pytest.mark.asyncio
async def test_reinforce_on_a_missing_bucket_fails_loudly(manager):
    with pytest.raises(ToolInputError, match="找不到记忆"):
        await trace_dispatch(bucket_id="no-such-bucket", reinforce=True)

    assert manager.touched == []


@pytest.mark.asyncio
async def test_reinforce_false_does_not_touch(manager):
    await trace_dispatch(bucket_id="hit-one", reinforce=False)

    assert manager.touched == []


@pytest.mark.asyncio
async def test_reinforce_refuses_to_share_a_call_with_field_updates(manager):
    with pytest.raises(ToolInputError, match="必须单独调用"):
        await trace_dispatch(bucket_id="hit-one", reinforce=True, importance=9)

    assert manager.touched == []


@pytest.mark.asyncio
async def test_reinforce_refuses_to_share_a_call_with_relation_edit(manager):
    with pytest.raises(ToolInputError, match="不能与关系修正同时使用"):
        await trace_dispatch(bucket_id="hit-one", reinforce=True, unlink="hit-two")

    assert manager.touched == []


@pytest.mark.asyncio
async def test_reinforce_refuses_to_share_a_call_with_quotes_replace(manager):
    with pytest.raises(ToolInputError, match="不能与 quotes_replace 同时使用"):
        await trace_dispatch(bucket_id="hit-one", reinforce=True, quotes_replace=[])

    assert manager.touched == []
