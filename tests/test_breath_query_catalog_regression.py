"""Regression coverage for query/catalog result shaping in breath.

The advanced entry point has two deliberately different contracts:

* query mode returns only ranked matches and applies ``max_results`` to the
  final memory entries; unrelated pinned/core buckets are not prefixed;
* catalog mode short-circuits retrieval and renders every bucket as one
  metadata line, including pinned/core buckets, without any stored body.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from tools.breath import dispatch


QUERY = "两个都是你 怎么还让我选"


class DisabledEmbedding:
    enabled = False


class ExplodingDehydrator:
    async def dehydrate(self, *_args, **_kwargs):
        raise AssertionError("query/catalog rendering must not call the LLM")


class NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("importance") or 0)


class RankedBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)
        self.search_calls = 0
        self.touched = []

    async def get(self, _bucket_id):
        return None

    async def search(self, _query, **_kwargs):
        self.search_calls += 1
        # Model a broad index response: the exact target ranks first, while an
        # unrelated core bucket and another ordinary bucket trail behind it.
        return list(self.buckets)

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.buckets)

    async def touch_many(self, bucket_ids, ripple=False):
        assert ripple is False
        self.touched.extend(bucket_ids)


def _bucket(
    bucket_id,
    content,
    *,
    name,
    bucket_type="dynamic",
    importance=5,
    pinned=False,
    created="2026-07-19T12:00:00",
):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": name,
            "type": bucket_type,
            "importance": importance,
            "pinned": pinned,
            "domain": ["回归测试"],
            "created": created,
        },
    }


def _install_runtime(monkeypatch, manager):
    monkeypatch.setattr(rt, "config", {"surfacing": {}})
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", NoopDecay())
    monkeypatch.setattr(rt, "dehydrator", ExplodingDehydrator())
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_args, **_kwargs: None)


@pytest.mark.asyncio
async def test_precise_query_applies_max_results_before_unrelated_core_can_take_budget(monkeypatch):
    target = _bucket(
        "precise-target",
        "精准命中正文：两个都是你，怎么还让我选。",
        name="精准命中",
        importance=8,
    )
    unrelated_core = _bucket(
        "unrelated-core",
        "无关核心准则全文。" * 400,
        name="无关核心准则",
        bucket_type="permanent",
        importance=10,
        pinned=True,
    )
    trailing = _bucket(
        "trailing-result",
        "第二条普通搜索结果正文。",
        name="后续结果",
        importance=6,
    )
    manager = RankedBucketManager([target, unrelated_core, trailing])
    _install_runtime(monkeypatch, manager)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    output = await dispatch(query=QUERY, max_results=1, max_tokens=6000)
    await asyncio.sleep(0)

    assert output.count("[bucket_id:") == 1
    assert "[bucket_id:precise-target]" in output
    assert target["content"] in output
    assert "=== 核心准则 ===" not in output
    assert "[bucket_id:unrelated-core]" not in output
    assert unrelated_core["content"] not in output
    assert "[bucket_id:trailing-result]" not in output
    assert "token 预算不足" not in output
    assert manager.touched == []


@pytest.mark.asyncio
async def test_catalog_with_query_renders_core_as_metadata_only_and_never_runs_search(monkeypatch):
    target = _bucket(
        "precise-target",
        "CATALOG_TARGET_BODY_MUST_NOT_SURFACE",
        name="精准命中",
        importance=8,
    )
    core = _bucket(
        "catalog-core",
        "CATALOG_CORE_BODY_MUST_NOT_SURFACE",
        name="目录核心准则",
        bucket_type="permanent",
        importance=10,
        pinned=True,
    )
    other = _bucket(
        "catalog-other",
        "CATALOG_OTHER_BODY_MUST_NOT_SURFACE",
        name="其他记忆",
        importance=4,
    )
    manager = RankedBucketManager([target, core, other])
    _install_runtime(monkeypatch, manager)

    output = await dispatch(
        query=QUERY,
        catalog=True,
        max_results=3,
        max_tokens=6000,
    )

    assert "=== 记忆目录（3 桶）===" in output
    assert "📌目录核心准则 | 回归测试 | 10" in output
    assert "精准命中 | 回归测试 | 8" in output
    assert "其他记忆 | 回归测试 | 4" in output
    assert "CATALOG_CORE_BODY_MUST_NOT_SURFACE" not in output
    assert "CATALOG_TARGET_BODY_MUST_NOT_SURFACE" not in output
    assert "CATALOG_OTHER_BODY_MUST_NOT_SURFACE" not in output
    assert "[bucket_id:" not in output
    assert "=== 核心准则 ===" not in output
    assert "=== 浮现记忆 ===" not in output
    assert manager.search_calls == 0
    assert manager.touched == []


@pytest.mark.asyncio
async def test_query_created_date_range_filters_hits_and_associations(monkeypatch):
    on_day = _bucket(
        "on-day",
        "7月19日的游戏聊天。",
        name="当日记忆",
        created="2026-07-19T00:30:00",
    )
    previous_day = _bucket(
        "previous-day",
        "7月18日的游戏聊天。",
        name="前日记忆",
        created="2026-07-18T23:59:59",
    )
    manager = RankedBucketManager([on_day, previous_day])
    _install_runtime(monkeypatch, manager)

    output = await dispatch(
        query="游戏聊天",
        date_from="2026-07-19",
        date_to="2026-07-19",
        max_results=5,
        max_tokens=6000,
    )

    assert "[bucket_id:on-day]" in output
    assert "[bucket_id:previous-day]" not in output


@pytest.mark.asyncio
async def test_query_rejects_invalid_created_date_range(monkeypatch):
    manager = RankedBucketManager([])
    _install_runtime(monkeypatch, manager)

    with pytest.raises(ToolInputError) as excinfo:
        await dispatch(
        query="游戏",
        date_from="2026-07-20",
        date_to="2026-07-19",
        )

    assert 'date_from 不能晚于 date_to' in str(excinfo.value)
    assert manager.search_calls == 0
