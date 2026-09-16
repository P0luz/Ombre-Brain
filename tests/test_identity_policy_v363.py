"""v3.6.3 caller ownership policy regression tests."""

from __future__ import annotations

import pytest

from errors import ToolInputError
from tools import _identity
from tools import _runtime as rt
from tools.breath.catalog import surface_catalog
from tools.grow.core import grow_items
from tools.i.core import I_CANDIDATE_TAG, _promote_candidate, _write_candidate
from tools.plan.core import plan_create


def _bucket(bucket_id: str, owner: str = "", *, content: str = "memory") -> dict:
    tags = [f"owner:{owner}"] if owner else []
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": bucket_id,
            "type": "dynamic",
            "tags": tags,
            "domain": ["test"],
            "importance": 7,
            "created": "2026-09-02T10:00:00+09:00",
            "last_active": "2026-09-02T10:00:00+09:00",
        },
    }


@pytest.fixture(autouse=True)
def identity_config(monkeypatch):
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
                "allow_tags": ["breath:all"],
                "allow_bucket_ids": [],
            }
        },
    )
    _identity.set_caller("")
    yield
    _identity.set_caller("")


def test_default_filter_keeps_own_shared_core_allowlisted_and_legacy():
    buckets = [
        _bucket("own", "cheng"),
        _bucket("foreign", "huaiyin"),
        _bucket("shared", "shared"),
        _bucket("core", "shared_core"),
        _bucket("context", "shared_context"),
        _bucket("resource", "shared_resource"),
        _bucket("legacy"),
        _bucket("multi", "cheng"),
        _bucket("allow", "huaiyin"),
    ]
    buckets[7]["metadata"]["tags"].append("owner:huaiyin")
    buckets[8]["metadata"]["tags"].append("breath:all")

    with _identity.caller_context("澄"):
        kept = _identity.filter_default(buckets)

    assert [bucket["id"] for bucket in kept] == [
        "own",
        "shared",
        "core",
        "legacy",
        "allow",
    ]


def test_untagged_deny_and_callerless_legacy_compatibility(monkeypatch):
    legacy = _bucket("legacy")
    with _identity.caller_context("cheng"):
        assert _identity.filter_default([legacy]) == [legacy]
        monkeypatch.setitem(rt.config["identity_filter"], "untagged", "deny")
        assert _identity.filter_default([legacy]) == []
    assert _identity.filter_default([legacy]) == [legacy]


def test_write_owner_is_authenticated_and_model_claims_are_discarded():
    with _identity.caller_context("cheng"):
        assert _identity.ensure_write_owner(["topic"]) == ["topic", "owner:cheng"]
        assert _identity.ensure_write_owner(["owner:shared"]) == ["owner:shared"]
        with pytest.raises(ValueError, match="其他本地身份"):
            _identity.ensure_write_owner(["owner:huaiyin"])
        with pytest.raises(ValueError, match="多个不同 owner"):
            _identity.ensure_write_owner(["owner:cheng", "owner:shared"])
    with pytest.raises(ValueError, match="已认证 caller"):
        _identity.ensure_write_owner(["owner:cheng"])
    assert _identity.ensure_write_owner(["legacy"]) == ["legacy"]
    assert _identity.strip_owner_tags(["owner:huaiyin", "topic"]) == ["topic"]


def test_owner_attribution_marks_cross_identity_query_hits():
    with _identity.caller_context("cheng"):
        assert _identity.attribution(_bucket("x", "cheng")["metadata"]) == " [owner:cheng]"
        assert _identity.attribution(_bucket("x", "huaiyin")["metadata"]) == (
            " [owner:huaiyin] [非本线记忆]"
        )


class _Manager:
    def __init__(self, buckets: list[dict] | None = None):
        self.buckets = {bucket["id"]: bucket for bucket in (buckets or [])}
        self.created: list[dict] = []

    async def list_all(self, include_archive=False):
        return list(self.buckets.values())

    async def create(self, content: str, **kwargs):
        bucket_id = f"new-{len(self.created) + 1}"
        record = _bucket(bucket_id, content=content)
        record["metadata"].update(
            {
                "tags": list(kwargs.get("tags") or []),
                "type": kwargs.get("bucket_type", "dynamic"),
                "domain": list(kwargs.get("domain") or []),
            }
        )
        self.buckets[bucket_id] = record
        self.created.append(record)
        return bucket_id

    async def update(self, bucket_id: str, **kwargs):
        self.buckets[bucket_id]["metadata"].update(kwargs)
        return True

    async def get(self, bucket_id: str):
        return self.buckets.get(bucket_id)

    def footprint_snapshot(self):
        class _Snapshot:
            @staticmethod
            def summary(bucket_id, metadata):
                return "footprint"

        return _Snapshot()


class _Decay:
    async def ensure_started(self):
        return None


@pytest.mark.asyncio
async def test_catalog_automatic_read_does_not_leak_foreign(monkeypatch):
    manager = _Manager([_bucket("mine", "cheng"), _bucket("theirs", "huaiyin")])
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    with _identity.caller_context("cheng"):
        text = await surface_catalog(max_results=20)
    assert "mine" in text
    assert "theirs" not in text
    assert "[owner:cheng]" in text


@pytest.mark.asyncio
async def test_plan_dedup_is_owner_scoped_and_new_plan_gets_owner(monkeypatch):
    foreign = _bucket("foreign-plan", "huaiyin", content="same plan")
    foreign["metadata"].update({"type": "plan", "status": "active"})
    manager = _Manager([foreign])
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", _Decay())

    with _identity.caller_context("cheng"):
        result = await plan_create("same plan")

    assert result.startswith("📋plan→new-1")
    assert "owner:cheng" in manager.created[0]["metadata"]["tags"]


@pytest.mark.asyncio
async def test_i_candidate_gets_owner_and_foreign_candidate_cannot_promote(monkeypatch):
    manager = _Manager()
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    with _identity.caller_context("cheng"):
        await _write_candidate("a stable observation", "values")
    candidate = manager.created[0]
    assert I_CANDIDATE_TAG in candidate["metadata"]["tags"]
    assert "owner:cheng" in candidate["metadata"]["tags"]

    foreign = _bucket("foreign-i", "huaiyin", content="not mine")
    foreign["metadata"].update(
        {
            "tags": [I_CANDIDATE_TAG, "owner:huaiyin"],
            "i_stage": "candidate",
            "i_dream_dates": ["2026-08-30", "2026-08-31", "2026-09-01"],
        }
    )
    manager.buckets[foreign["id"]] = foreign
    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await _promote_candidate("foreign-i", "")


@pytest.mark.asyncio
async def test_grow_items_rejects_foreign_owner_before_any_write(monkeypatch):
    manager = _Manager()
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    item = {
        "content": "verbatim event",
        "title": "event",
        "tags": ["owner:huaiyin"],
        "domain": ["test"],
        "valence": 0.5,
        "arousal": 0.3,
        "importance": 5,
    }
    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await grow_items([item])
    assert manager.created == []
