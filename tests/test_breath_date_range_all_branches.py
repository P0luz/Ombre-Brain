from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from tools.breath import dispatch


OLD = "六月那件事，很久以前了。"
NEW = "八月这件事，是最近的。"


class DisabledEmbedding:
    enabled = False


class ExplodingDehydrator:
    async def dehydrate(self, *_args, **_kwargs):
        raise AssertionError("这些用例不该调 LLM")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 0)


class DatedBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)
        self.touched = []

    async def get(self, _bucket_id):
        return None

    async def get_including_archive(self, _bucket_id):
        return None

    async def search(self, _query, **_kwargs):
        return list(self.buckets)

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def touch_many(self, bucket_ids, ripple=False):
        self.touched.extend(bucket_ids)

    async def get_stats(self):
        return {"permanent_count": 0, "dynamic_count": len(self.buckets)}

    def footprint_snapshot(self):
        raise RuntimeError("no footprint in tests")


def _bucket(
    bucket_id,
    content,
    *,
    created,
    bucket_type="dynamic",
    importance=9,
    pinned=False,
):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": bucket_type,
            "importance": importance,
            "pinned": pinned,
            "domain": ["回归测试"],
            "created": created,
            "activation_count": 3,
            "last_active": created,
        },
    }


def _install(monkeypatch, manager):
    monkeypatch.setattr(rt, "config", {"surfacing": {}})
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "dehydrator", ExplodingDehydrator())
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_a, **_k: None)
    monkeypatch.setattr(rt, "deletion_requests", None, raising=False)
    monkeypatch.setattr(rt, "them_service", None, raising=False)


def _two(monkeypatch, **kwargs):
    manager = DatedBucketManager([
        _bucket("old-one", OLD, created="2026-06-15T10:00:00", **kwargs),
        _bucket("new-one", NEW, created="2026-08-20T10:00:00", **kwargs),
    ])
    _install(monkeypatch, manager)
    return manager


@pytest.mark.asyncio
async def test_default_surfacing_honours_date_to(monkeypatch):
    _two(monkeypatch)

    out = await dispatch(date_to="2026-07-01")

    assert OLD in out
    assert NEW not in out


@pytest.mark.asyncio
async def test_default_surfacing_honours_date_from(monkeypatch):
    _two(monkeypatch)

    out = await dispatch(date_from="2026-08-01")

    assert NEW in out
    assert OLD not in out


@pytest.mark.asyncio
async def test_core_principles_ignore_the_date_filter(monkeypatch):
    manager = DatedBucketManager([
        _bucket("old-one", OLD, created="2026-06-15T10:00:00"),
        _bucket("new-one", NEW, created="2026-08-20T10:00:00"),
        _bucket(
            "core-one", "我说话要算数。",
            created="2026-01-01T10:00:00", bucket_type="permanent",
            importance=10, pinned=True,
        ),
    ])
    _install(monkeypatch, manager)

    out = await dispatch(date_from="2026-08-01")

    assert "我说话要算数。" in out
    assert OLD not in out


@pytest.mark.asyncio
async def test_catalog_honours_the_date_range(monkeypatch):
    _two(monkeypatch)

    out = await dispatch(catalog=True, date_to="2026-07-01")

    assert "old-one" in out
    assert "new-one" not in out


@pytest.mark.asyncio
async def test_importance_min_honours_the_date_range(monkeypatch):
    _two(monkeypatch)

    out = await dispatch(importance_min=9, date_to="2026-07-01")

    assert OLD in out
    assert NEW not in out


@pytest.mark.asyncio
async def test_feel_honours_the_date_range(monkeypatch):
    manager = DatedBucketManager([
        _bucket(
            "old-feel", "六月的时候我很累。",
            created="2026-06-15T10:00:00", bucket_type="feel",
        ),
        _bucket(
            "new-feel", "八月的时候我很累。",
            created="2026-08-20T10:00:00", bucket_type="feel",
        ),
    ])
    _install(monkeypatch, manager)

    out = await dispatch(domain="feel", query="累", date_to="2026-07-01")

    assert "六月的时候我很累。" in out
    assert "八月的时候我很累。" not in out


@pytest.mark.asyncio
async def test_search_still_honours_the_date_range(monkeypatch):
    _two(monkeypatch)

    out = await dispatch(query="那件事", date_to="2026-07-01")

    assert NEW not in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"query": "那件事"},
        {"catalog": True},
        {"importance_min": 9},
        {"domain": "feel", "query": "累"},
    ],
)
async def test_invalid_date_is_rejected_on_every_branch(monkeypatch, kwargs):
    _two(monkeypatch)

    with pytest.raises(ToolInputError, match="日期格式无效"):
        await dispatch(date_from="2026-13-01", **kwargs)


@pytest.mark.asyncio
async def test_reversed_range_is_rejected(monkeypatch):
    _two(monkeypatch)

    with pytest.raises(ToolInputError, match="不能晚于"):
        await dispatch(date_from="2026-08-01", date_to="2026-06-01")


@pytest.mark.asyncio
async def test_bucket_without_created_is_excluded_when_a_range_is_given(monkeypatch):
    manager = DatedBucketManager([
        _bucket("dated", NEW, created="2026-08-20T10:00:00"),
        _bucket("undated", "没有创建时间的桶", created=""),
    ])
    _install(monkeypatch, manager)

    out = await dispatch(date_from="2026-01-01")

    assert "没有创建时间的桶" not in out


@pytest.mark.asyncio
async def test_no_range_returns_everything(monkeypatch):
    _two(monkeypatch)

    out = await dispatch()

    assert OLD in out and NEW in out
