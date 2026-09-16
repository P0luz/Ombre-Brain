"""Write-time owner injection and owner-safe merge tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import tools._runtime as rt
from tools import _identity
from tools._common import merge_or_create
import tools.hold as hold_mod


@pytest.fixture(autouse=True)
def reset_caller():
    _identity.set_caller("")
    yield
    _identity.set_caller("")


@pytest.fixture
def write_rt(bucket_mgr, monkeypatch):
    monkeypatch.setattr(
        rt, "config", {"limits": {}, "merge_threshold": 75}, raising=False
    )
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "embedding_engine", bucket_mgr.embedding_engine, raising=False)
    return bucket_mgr


def test_ensure_write_owner_injects_caller_and_respects_explicit_owner():
    _identity.set_caller("cheng")
    assert _identity.ensure_write_owner(["x"]) == ["x", "owner:cheng"]
    assert _identity.ensure_write_owner(["x", "owner:shared_context"]) == [
        "x",
        "owner:shared_context",
    ]


def test_ensure_write_owner_rejects_conflicting_owners():
    with pytest.raises(ValueError, match="多个不同 owner"):
        _identity.ensure_write_owner(["owner:cheng", "owner:huaiyin"])


def test_write_owner_receipt_shows_effective_or_missing_owner():
    _identity.set_caller("cheng")
    assert _identity.write_owner_receipt() == "owner:cheng"
    assert _identity.write_owner_receipt(["x", "owner:shared_context"]) == (
        "owner:shared_context"
    )
    _identity.set_caller("")
    assert _identity.write_owner_receipt() == "owner:未声明"


@pytest.mark.asyncio
async def test_merge_or_create_injects_owner_on_new_bucket(write_rt, monkeypatch):
    bucket_mgr = write_rt
    _identity.set_caller("cheng")
    monkeypatch.setattr(bucket_mgr, "search", AsyncMock(return_value=[]))

    bucket_id, is_merged, _ = await merge_or_create(
        content="new owner-tagged content",
        tags=["test"],
        importance=5,
        domain=["工作"],
        valence=0.5,
        arousal=0.3,
        source_tool="hold",
    )

    assert not is_merged
    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["tags"] == ["test", "owner:cheng"]


@pytest.mark.asyncio
async def test_merge_skips_other_owner_and_uses_same_owner(write_rt, monkeypatch):
    bucket_mgr = write_rt
    other_id = await bucket_mgr.create_internal(
        content="other", tags=["owner:huaiyin"], domain=["工作"]
    )
    same_id = await bucket_mgr.create_internal(
        content="same", tags=["owner:cheng"], domain=["工作"]
    )
    other = await bucket_mgr.get(other_id)
    same = await bucket_mgr.get(same_id)
    monkeypatch.setattr(
        bucket_mgr,
        "search",
        AsyncMock(
            return_value=[
                {**other, "score": 99},
                {**same, "score": 98},
            ]
        ),
    )
    _identity.set_caller("cheng")

    bucket_id, is_merged, _ = await merge_or_create(
        content="incoming",
        tags=[],
        importance=5,
        domain=["工作"],
        valence=0.5,
        arousal=0.3,
        raw_merge=True,
        source_tool="hold",
    )

    assert is_merged
    assert bucket_id == same_id
    unchanged = await bucket_mgr.get(other_id)
    assert unchanged["content"] == "other"


@pytest.mark.asyncio
async def test_merge_does_not_claim_untagged_bucket(write_rt, monkeypatch):
    bucket_mgr = write_rt
    legacy_id = await bucket_mgr.create_internal(
        content="legacy", tags=["old"], domain=["工作"]
    )
    legacy = await bucket_mgr.get(legacy_id)
    monkeypatch.setattr(
        bucket_mgr,
        "search",
        AsyncMock(return_value=[{**legacy, "score": 99}]),
    )
    _identity.set_caller("cheng")

    bucket_id, is_merged, _ = await merge_or_create(
        content="incoming",
        tags=[],
        importance=5,
        domain=["工作"],
        valence=0.5,
        arousal=0.3,
        raw_merge=True,
        source_tool="hold",
    )

    assert not is_merged
    assert bucket_id != legacy_id
    unchanged = await bucket_mgr.get(legacy_id)
    assert unchanged["metadata"]["tags"] == ["old"]


@pytest.mark.asyncio
async def test_hold_direct_branches_receive_injected_owner(monkeypatch):
    _identity.set_caller("cheng")
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "record_v3_tool_event", lambda *_a, **_k: None)

    class NoopDecay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(
        rt,
        "decay_engine",
        NoopDecay(),
        raising=False,
    )
    captured = {}

    async def fake_pinned(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(hold_mod, "store_pinned", fake_pinned)
    monkeypatch.setattr(hold_mod, "enforce_pinned_quota", AsyncMock(return_value=True))
    out = await hold_mod.dispatch(content="pinned", tags="x", pinned=True)

    assert out == "ok\nowner:cheng"
    assert captured["extra_tags"] == ["x", "owner:cheng"]
