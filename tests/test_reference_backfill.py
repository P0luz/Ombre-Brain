"""references 反向边补齐（referenced_by）+ trace 事后改 event_time。

- collect_missing_reference_reverse：纯判定，找缺反边的 (被引用, 引用) 对。
- backfill_reference_reverse_links：幂等写入，dream 全量扫时调用。
- trace(event_time=...)：事后修正/清除语义时间。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools._relation_link import backfill_reference_reverse_links
from tools.trace.core import trace_core
from ombrebrain.storage.relation_store import collect_missing_reference_reverse


def _bucket(bid, btype="dynamic", links=None):
    meta = {"type": btype}
    if links:
        meta["relation_links"] = links
    return {"id": bid, "content": bid, "metadata": meta}


# ---------------- 纯判定 ----------------

def test_collect_finds_missing_reverse():
    buckets = [
        _bucket("a", links=[
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
        ]),
        _bucket("b"),
    ]
    assert collect_missing_reference_reverse(buckets) == [("b", "a")]


def test_collect_skips_existing_reverse():
    buckets = [
        _bucket("a", links=[
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
        ]),
        _bucket("b", links=[
            {"target_bucket_id": "a", "type": "referenced_by", "label": "", "status": "active"},
        ]),
    ]
    assert collect_missing_reference_reverse(buckets) == []


def test_collect_skips_excluded_and_missing_target():
    buckets = [
        _bucket("a", links=[
            # 目标 b 是 feel 桶（排除类型）→ 不补
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
            # 目标 c 不在非归档集合里 → 不补
            {"target_bucket_id": "c", "type": "references", "label": "", "status": "active"},
            # detached 边不参与
            {"target_bucket_id": "d", "type": "references", "label": "", "status": "detached"},
        ]),
        _bucket("b", btype="feel"),
        _bucket("d"),
    ]
    assert collect_missing_reference_reverse(buckets) == []


def test_collect_deduplicates_and_sorts():
    buckets = [
        _bucket("a", links=[
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
        ]),
        _bucket("c", links=[
            {"target_bucket_id": "b", "type": "references", "label": "", "status": "active"},
        ]),
        _bucket("b"),
    ]
    assert collect_missing_reference_reverse(buckets) == [("b", "a"), ("b", "c")]


# ---------------- 写入（幂等）----------------

@pytest.fixture
def backfill_runtime(monkeypatch, bucket_mgr, test_config):
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    return bucket_mgr


@pytest.mark.asyncio
async def test_backfill_adds_reverse_and_is_idempotent(backfill_runtime):
    manager = backfill_runtime
    target_id = await manager.create(content="先有的一段记忆", domain=["测试"])
    source_id = await manager.create(
        content="后写的一段，提到了之前",
        domain=["测试"],
        references=[target_id],
    )

    all_buckets = await manager.list_all(include_archive=False)
    built = await backfill_reference_reverse_links(all_buckets)
    assert built == 1

    target = await manager.get(target_id)
    links = target["metadata"].get("relation_links") or []
    reverse = [l for l in links if l.get("type") == "referenced_by"]
    assert len(reverse) == 1
    assert reverse[0]["target_bucket_id"] == source_id
    assert reverse[0].get("auto") is not True  # 反边是补强关系，不标 auto

    # 再跑一遍：幂等，0 条新增
    all_buckets = await manager.list_all(include_archive=False)
    assert await backfill_reference_reverse_links(all_buckets) == 0


# ---------------- trace 事后改 event_time ----------------

@pytest.fixture
def trace_runtime(monkeypatch, bucket_mgr, test_config):
    monkeypatch.setattr(rt, "config", test_config, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "v3_runtime", None, raising=False)
    return bucket_mgr


@pytest.mark.asyncio
async def test_trace_sets_and_clears_event_time(trace_runtime):
    manager = trace_runtime
    bid = await manager.create(content="一条普通记忆", domain=["测试"])
    bucket = await manager.get(bid)
    assert not bucket["metadata"].get("event_time")

    await trace_core(bid, event_time="2025-12-05")
    bucket = await manager.get(bid)
    assert bucket["metadata"]["event_time"] == "2025-12-05"
    assert bucket["metadata"]["event_time_source"] == "manual"

    await trace_core(bid, event_time="\\clear")
    bucket = await manager.get(bid)
    assert not bucket["metadata"].get("event_time")
    assert not bucket["metadata"].get("event_time_source")
