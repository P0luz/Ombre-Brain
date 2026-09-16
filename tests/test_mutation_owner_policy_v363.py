"""Authenticated caller authorization for bucket mutations."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from errors import ToolInputError
from tools import _identity
from tools import _runtime as rt
from tools.anchor.core import anchor_set
from tools.trace.core import trace_core


def _bucket(bucket_id: str, *owners: str) -> dict:
    return {
        "id": bucket_id,
        "content": "memory",
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "tags": [f"owner:{owner}" for owner in owners],
            "importance": 5,
            "created": "2026-09-04T09:00:00+09:00",
            "last_active": "2026-09-04T09:00:00+09:00",
        },
    }


class _Manager:
    def __init__(self, buckets: list[dict]):
        self.buckets = {bucket["id"]: bucket for bucket in buckets}
        self.updates: list[tuple[str, dict]] = []
        self.anchor_calls: list[tuple[str, bool]] = []
        self.touch_calls: list[tuple[str, bool, object]] = []
        self.ripple_predicate = None

    async def get(self, bucket_id: str):
        return self.buckets.get(bucket_id)

    async def update(self, bucket_id: str, **kwargs):
        self.updates.append((bucket_id, kwargs))
        self.buckets[bucket_id]["metadata"].update(kwargs)
        return True

    async def set_anchor(self, bucket_id: str, value: bool):
        self.anchor_calls.append((bucket_id, value))
        return {"ok": True, "count": 1, "limit": 24}

    @contextmanager
    def ripple_admission(self, predicate):
        self.ripple_predicate = predicate
        yield

    async def touch(self, bucket_id: str, ripple=True):
        self.touch_calls.append((bucket_id, ripple, self.ripple_predicate))


@pytest.fixture(autouse=True)
def identity_config(monkeypatch):
    monkeypatch.setattr(rt, "logger", logging.getLogger("test.mutation-owner"))
    monkeypatch.setattr(
        rt,
        "config",
        {
            "identity_filter": {
                "enabled": True,
                "untagged": "allow",
                "shared_owner_values": ["shared", "shared_core"],
                "exclude_shared_values": ["shared_context", "shared_resource"],
                "known_owners": ["cheng", "huaiyin", "huaiyin_cc"],
            }
        },
    )
    _identity.set_caller("")
    yield
    _identity.set_caller("")


def test_authenticated_mutation_policy_is_strict_and_callerless_is_legacy():
    with _identity.caller_context("cheng"):
        assert _identity.mutation_owner(_bucket("own", "cheng")["metadata"]) == "cheng"
        assert _identity.mutation_owner(_bucket("shared", "shared")["metadata"]) == "shared"
        assert _identity.mutation_owner(
            _bucket("core", "shared_core")["metadata"]
        ) == "shared_core"
        with pytest.raises(ValueError, match="其他本地身份"):
            _identity.mutation_owner(_bucket("foreign", "huaiyin")["metadata"])
        with pytest.raises(ValueError, match="唯一有效 owner"):
            _identity.mutation_owner(_bucket("legacy")["metadata"])
        with pytest.raises(ValueError, match="唯一有效 owner"):
            _identity.mutation_owner(
                _bucket("mixed", "cheng", "huaiyin")["metadata"]
            )
        with pytest.raises(ValueError, match="不允许"):
            _identity.mutation_owner(
                _bucket("context", "shared_context")["metadata"]
            )

    assert _identity.mutation_owner(_bucket("legacy")["metadata"]) == ""
    assert _identity.admitted_mutation(_bucket("foreign", "huaiyin")) is True


def test_authenticated_tag_edit_preserves_owner_and_rejects_owner_change():
    meta = _bucket("own", "cheng")["metadata"]
    with _identity.caller_context("cheng"):
        assert _identity.preserve_mutation_owner(meta, ["new-tag"]) == [
            "new-tag",
            "owner:cheng",
        ]
        with pytest.raises(ValueError, match="不能改变"):
            _identity.preserve_mutation_owner(meta, ["owner:shared", "new-tag"])


@pytest.mark.asyncio
async def test_trace_rejects_foreign_before_update(monkeypatch):
    manager = _Manager([_bucket("foreign", "huaiyin")])
    monkeypatch.setattr(rt, "bucket_mgr", manager)

    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await trace_core("foreign", resolved=1)

    assert manager.updates == []


@pytest.mark.asyncio
async def test_trace_tag_edit_keeps_existing_owner(monkeypatch):
    manager = _Manager([_bucket("own", "cheng")])
    monkeypatch.setattr(rt, "bucket_mgr", manager)

    with _identity.caller_context("cheng"):
        result = await trace_core("own", tags="fresh")

    assert result.startswith("已修改记忆桶 own")
    assert manager.updates[0][1]["tags"] == ["fresh", "owner:cheng"]


@pytest.mark.asyncio
async def test_anchor_rejects_foreign_before_set_anchor(monkeypatch):
    manager = _Manager([_bucket("foreign", "huaiyin")])
    monkeypatch.setattr(rt, "bucket_mgr", manager)

    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await anchor_set("foreign")

    assert manager.anchor_calls == []


@pytest.mark.asyncio
async def test_relation_edit_requires_both_bucket_owners(monkeypatch):
    manager = _Manager(
        [_bucket("own", "cheng"), _bucket("foreign", "huaiyin")]
    )
    monkeypatch.setattr(rt, "bucket_mgr", manager)

    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await trace_core("own", unlink="foreign")


@pytest.mark.asyncio
async def test_reinforce_passes_owner_filter_to_time_ripple(monkeypatch):
    manager = _Manager([_bucket("own", "cheng")])
    monkeypatch.setattr(rt, "bucket_mgr", manager)

    with _identity.caller_context("cheng"):
        await trace_core("own", reinforce=True)
        _, ripple, predicate = manager.touch_calls[0]
        assert ripple is True
        assert callable(predicate)
        assert predicate(_bucket("mine", "cheng")) is True
        assert predicate(_bucket("foreign", "huaiyin")) is False


@pytest.mark.asyncio
async def test_real_time_ripple_rechecks_owner_under_target_lock(bucket_mgr):
    source = await bucket_mgr.create_internal(
        content="source memory",
        name="source",
        domain=["test"],
        tags=["owner:cheng"],
    )
    own_neighbor = await bucket_mgr.create_internal(
        content="own neighbor",
        name="own",
        domain=["test"],
        tags=["owner:cheng"],
    )
    foreign_neighbor = await bucket_mgr.create_internal(
        content="foreign neighbor",
        name="foreign",
        domain=["test"],
        tags=["owner:huaiyin"],
    )

    with _identity.caller_context("cheng"):
        with bucket_mgr.ripple_admission(_identity.admitted_mutation):
            await bucket_mgr.touch(source, ripple=True)

    own = await bucket_mgr.get(own_neighbor)
    foreign = await bucket_mgr.get(foreign_neighbor)
    assert float(own["metadata"].get("activation_count") or 0) == 0.3
    assert float(foreign["metadata"].get("activation_count") or 0) == 0.0
