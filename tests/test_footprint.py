from pathlib import Path
from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from ombrebrain.eventsourcing.footprint import FootprintSnapshot
from tools._common import count_pinned
from tools.breath.search import surface_search
from tools.breath.surface import surface_default
from tools.trace.core import trace_core


class _NoEmbedding:
    enabled = False


def _install_runtime(bucket_mgr, decay_eng) -> None:
    rt.config = {"limits": {}, "surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_eng
    rt.embedding_engine = _NoEmbedding()
    rt.embedding_outbox = None
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.v3_runtime = None


def test_footprint_is_compact_and_ignores_technical_touch_events():
    snapshot = FootprintSnapshot.from_events([
        {"trace_id": "m1", "event_type": "TraceCreated", "trace_kind": "dynamic"},
        {"trace_id": "m1", "event_type": "TraceTouched", "trace_kind": "dynamic"},
        {
            "trace_id": "m1",
            "event_type": "TraceUpdated",
            "trace_kind": "dynamic",
            "payload": {
                "changed_fields": ["content", "last_merged_by"],
                "last_merged_by": "hold",
            },
        },
        {"trace_id": "m1", "event_type": "TraceArchived", "trace_kind": "archived"},
    ])

    assert snapshot.summary("m1") == (
        "👣 Footprint：操作者未记录经系统直接创建 → 事件补充 → 淡去归档"
    )
    assert snapshot.original_kind("m1") == "dynamic"


@pytest.mark.asyncio
async def test_default_breath_shows_footprint_after_each_memory(bucket_mgr, decay_eng):
    bucket_id = await bucket_mgr.create(
        content="A memory whose path remains visible.",
        domain=["life"],
        importance=8,
    )
    _install_runtime(bucket_mgr, decay_eng)

    output = await surface_default(max_results=5, max_tokens=10000, tag_filter=[])

    body_at = output.index("A memory whose path remains visible.")
    footprint_at = output.index("👣 Footprint：系统经系统直接创建")
    assert bucket_id in output
    assert footprint_at > body_at


@pytest.mark.asyncio
async def test_query_discovers_archive_and_prints_explicit_restore_call(
    bucket_mgr, decay_eng
):
    bucket_id = await bucket_mgr.create(
        content="The hidden lantern memory is useful now.",
        domain=["life"],
    )
    assert await bucket_mgr.delete(bucket_id) is True
    archived = await bucket_mgr.get_including_archive(bucket_id)
    archived_path = Path(archived["path"])
    _install_runtime(bucket_mgr, decay_eng)

    output = await surface_search(
        query="hidden lantern",
        max_results=5,
        max_tokens=10000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )

    assert "[query 命中·已删除到档案]" in output
    assert "The hidden lantern memory is useful now." in output
    assert "👣 Footprint：系统经系统直接创建 → 删除到档案" in output
    assert f'trace(bucket_id="{bucket_id}", restore=True)' in output
    assert archived_path.exists()
    assert await bucket_mgr.get(bucket_id) is None


@pytest.mark.asyncio
async def test_trace_restore_is_explicit_and_reindexes_bucket(bucket_mgr, decay_eng):
    bucket_id = await bucket_mgr.create(
        content="A returned memory.", domain=["life"]
    )
    assert await bucket_mgr.delete(bucket_id) is True
    _install_runtime(bucket_mgr, decay_eng)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_core(bucket_id, restore=True, content="do not overwrite")
    assert 'restore=True 必须单独调用' in str(excinfo.value)
    assert await bucket_mgr.get(bucket_id) is None

    restored = await trace_core(bucket_id, restore=True)
    active = await bucket_mgr.get(bucket_id)

    assert restored == f"已重新回忆并恢复记忆桶: {bucket_id}"
    assert active is not None
    assert active["metadata"]["type"] == "dynamic"
    assert "deleted_at" not in active["metadata"]
    assert bucket_id in bucket_mgr.embedding_engine._store
    assert bucket_mgr.footprint_snapshot().summary(bucket_id).endswith("重新回忆")


@pytest.mark.asyncio
async def test_trace_restore_archived_pin_does_not_silently_repin(
    bucket_mgr,
    decay_eng,
):
    """归档保留 pin 历史，但显式恢复不会让它重新占用活跃配额。"""
    bucket_id = await bucket_mgr.create(
        content="A pinned memory returning without a silent repin.",
        domain=["life"],
        pinned=True,
    )
    _install_runtime(bucket_mgr, decay_eng)

    assert await count_pinned() == 1
    assert await bucket_mgr.archive(bucket_id) is True

    archived = await bucket_mgr.get_including_archive(bucket_id)
    assert archived is not None
    assert archived["metadata"]["type"] == "archived"
    assert archived["metadata"]["pinned"] is True
    assert await count_pinned() == 0

    restored = await trace_core(bucket_id, restore=True)
    active = await bucket_mgr.get(bucket_id)

    assert restored == f"已重新回忆并恢复记忆桶: {bucket_id}"
    assert active is not None
    assert active["metadata"]["type"] == "permanent"
    assert active["metadata"].get("pinned", False) is False
    assert await count_pinned() == 0
    assert bucket_mgr.footprint_snapshot().summary(bucket_id).endswith("重新回忆")


def test_footprint_records_declared_source_and_pin_actors():
    snapshot = FootprintSnapshot.from_events([
        {
            "trace_id": "m2",
            "event_type": "TraceCreated",
            "trace_kind": "permanent",
            "payload": {
                "source_tool": "import",
                "event_actor": "human",
                "pinned": False,
            },
        },
        {
            "trace_id": "m2",
            "event_type": "TraceUpdated",
            "trace_kind": "permanent",
            "payload": {
                "changed_fields": ["pinned"],
                "pinned": True,
                "event_actor": "human",
            },
        },
        {
            "trace_id": "m2",
            "event_type": "TraceUpdated",
            "trace_kind": "dynamic",
            "payload": {
                "changed_fields": ["pinned", "importance"],
                "pinned": False,
                "importance": 6,
                "event_actor": "llm",
            },
        },
    ])

    assert snapshot.summary("m2") == (
        "👣 Footprint：用户经用户导入创建 → 用户钉为核心 → LLM解除核心"
    )
