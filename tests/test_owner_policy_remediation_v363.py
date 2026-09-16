"""Adversarial regressions for the Batch 2-4 owner-policy remediation."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from deletion_requests import DeletionRequestStore
from errors import ToolInputError
from tools import _identity
from tools import _runtime as rt
from tools import _relation_link
from tools._common import (
    cascade_plan_resolved_to_buckets,
    check_duplicate_for,
    check_plan_resolution,
)
from tools.hold.feel import store_feel
from tools.anchor.core import pulse
from tools.i.core import I_CANDIDATE_TAG, _promote_candidate, record_dream_pass
from tools.plan.core import letter_lock_update, letter_read, plan_create


def _bucket(bucket_id: str, owner: str, *, bucket_type: str = "dynamic") -> dict:
    return {
        "id": bucket_id,
        "content": f"secret-{bucket_id}",
        "metadata": {
            "name": bucket_id,
            "type": bucket_type,
            "tags": [f"owner:{owner}"],
            "domain": [bucket_type],
            "importance": 6,
            "created": "2026-09-04T09:00:00+09:00",
            "last_active": "2026-09-04T09:00:00+09:00",
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
    monkeypatch.setattr(rt, "logger", logging.getLogger("test.owner-remediation"))
    _identity.set_caller("")
    yield
    _identity.set_caller("")


def test_remote_callerless_is_read_only_but_local_legacy_remains_available():
    meta = _bucket("mine", "cheng")["metadata"]
    assert _identity.mutation_owner(meta) == ""
    with _identity.caller_context("", transport="http"):
        with pytest.raises(ValueError, match="必须绑定明确"):
            _identity.mutation_owner(meta)
        with pytest.raises(ValueError, match="写入记忆必须绑定明确"):
            _identity.ensure_write_owner(["topic"])
        assert _identity.admitted_mutation(_bucket("mine", "cheng")) is False


@pytest.mark.asyncio
async def test_storage_guard_rechecks_exact_owner_under_bucket_lock(bucket_mgr):
    bucket_id = await bucket_mgr.create_internal(
        content="guarded memory",
        name="guarded",
        domain=["test"],
        tags=["owner:cheng"],
    )
    with _identity.caller_context("cheng"):
        owner = _identity.mutation_owner(
            (await bucket_mgr.get(bucket_id))["metadata"]
        )
        await bucket_mgr.update(bucket_id, tags=["owner:shared"])
        with _identity.manager_mutation_guard(
            bucket_mgr, {bucket_id: owner}
        ):
            updated = await bucket_mgr.update(bucket_id, resolved=True)

    assert updated is False
    saved = await bucket_mgr.get(bucket_id)
    assert saved["metadata"]["tags"] == ["owner:shared"]
    assert saved["metadata"].get("resolved") is not True


@pytest.mark.asyncio
async def test_rejected_owner_update_does_not_persist_media_sidecar(bucket_mgr):
    bucket_id = await bucket_mgr.create_internal(
        content="guarded media",
        name="guarded-media",
        domain=["test"],
        tags=["owner:cheng"],
    )

    class RecordingMediaStore:
        def __init__(self):
            self.calls = []

        async def persist(self, requested_bucket_id, media):
            self.calls.append((requested_bucket_id, media))
            return [{"path": "should-not-exist.png"}]

    media_store = RecordingMediaStore()
    bucket_mgr.media_store = media_store
    with _identity.caller_context("cheng"):
        owner = _identity.mutation_owner(
            (await bucket_mgr.get(bucket_id))["metadata"]
        )
        await bucket_mgr.update(bucket_id, tags=["owner:shared"])
        with _identity.manager_mutation_guard(bucket_mgr, {bucket_id: owner}):
            updated = await bucket_mgr.update(
                bucket_id, media_append=[{"path": "input.png"}]
            )

    assert updated is False
    assert media_store.calls == []


@pytest.mark.asyncio
async def test_anchor_limit_rejection_precedes_media_persistence(
    bucket_mgr, monkeypatch
):
    bucket_id = await bucket_mgr.create_internal(
        content="anchor candidate",
        name="anchor-media",
        domain=["test"],
        tags=["owner:cheng"],
    )

    class RecordingMediaStore:
        def __init__(self):
            self.calls = []

        async def persist(self, requested_bucket_id, media):
            self.calls.append((requested_bucket_id, media))
            return [{"path": "should-not-exist.png"}]

    media_store = RecordingMediaStore()
    bucket_mgr.media_store = media_store
    monkeypatch.setattr(bucket_mgr, "ANCHOR_LIMIT", 0)

    with _identity.caller_context("cheng"):
        owner = _identity.mutation_owner(
            (await bucket_mgr.get(bucket_id))["metadata"]
        )
        with _identity.manager_mutation_guard(bucket_mgr, {bucket_id: owner}):
            updated = await bucket_mgr.update(
                bucket_id,
                anchor=True,
                media_append=[{"path": "input.png"}],
            )

    assert updated is False
    assert media_store.calls == []


class _ListManager:
    def __init__(self, buckets: list[dict]):
        self.buckets = {bucket["id"]: bucket for bucket in buckets}
        self.deleted: list[str] = []
        self.archived: list[str] = []
        self.update_calls: list[tuple[str, dict]] = []

    async def list_all(self, include_archive=False):
        return list(self.buckets.values())

    async def get(self, bucket_id: str):
        return self.buckets.get(bucket_id)

    async def update(self, bucket_id: str, **kwargs):
        self.update_calls.append((bucket_id, kwargs))
        self.buckets[bucket_id]["metadata"].update(kwargs)
        return True

    async def search(self, _query: str, **_kwargs):
        return list(self.buckets.values())

    async def delete(self, bucket_id: str):
        self.deleted.append(bucket_id)
        return True

    async def archive(self, bucket_id: str):
        self.archived.append(bucket_id)
        return True


@pytest.mark.asyncio
async def test_letter_read_and_lock_update_do_not_cross_owner(monkeypatch):
    own = _bucket("own-letter", "cheng", bucket_type="letter")
    own["metadata"].update({"author": "AI", "lock_type": "none", "locked_by": "ai"})
    foreign = _bucket("foreign-letter", "huaiyin", bucket_type="letter")
    foreign["metadata"].update(
        {"author": "AI", "lock_type": "none", "locked_by": "ai"}
    )
    manager = _ListManager([own, foreign])
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "embedding_engine", None)

    with _identity.caller_context("cheng"):
        rendered = await letter_read()
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await letter_lock_update("foreign-letter", "permanent")

    assert "secret-own-letter" in rendered
    assert "foreign-letter" not in rendered


@pytest.mark.asyncio
async def test_plan_cannot_bind_or_cascade_to_foreign_owner(monkeypatch):
    foreign = _bucket("foreign", "huaiyin")
    manager = _ListManager([foreign])
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(
        rt, "decay_engine", SimpleNamespace(ensure_started=_async_noop)
    )

    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="其他本地身份"):
            await plan_create("my plan", related_bucket="foreign")
        changed = await cascade_plan_resolved_to_buckets(
            {"tags": ["owner:cheng"], "related_bucket": "foreign"},
            "plan-1",
        )

    assert changed == []
    assert foreign["metadata"].get("resolved") is not True


@pytest.mark.asyncio
async def test_read_allowlist_never_authorizes_derived_writes(monkeypatch):
    foreign_plan = _bucket("foreign-plan", "huaiyin", bucket_type="plan")
    foreign_plan["metadata"].update(
        {"status": "active", "tags": ["owner:huaiyin", "breath:all"]}
    )
    foreign_i = _bucket("foreign-i", "huaiyin")
    foreign_i["metadata"].update(
        {
            "tags": ["owner:huaiyin", "breath:all", I_CANDIDATE_TAG],
            "i_stage": "candidate",
            "i_dream_dates": [],
        }
    )
    manager = _ListManager([foreign_plan, foreign_i])
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "embedding_engine", None)
    monkeypatch.setattr(
        rt,
        "dehydrator",
        SimpleNamespace(
            judge_plan_resolution=lambda *_args, **_kwargs: {
                "resolved": True,
                "confidence": 1.0,
                "reason": "done",
            }
        ),
    )

    with _identity.caller_context("cheng"):
        await check_plan_resolution("done")
        recorded = await record_dream_pass(["foreign-i"])

    assert recorded == 0
    assert manager.update_calls == []


@pytest.mark.asyncio
async def test_i_promote_requires_mutation_admission_not_read_visibility(monkeypatch):
    candidate = _bucket("untagged-i", "cheng")
    candidate["metadata"].update(
        {
            "tags": [I_CANDIDATE_TAG],
            "i_stage": "candidate",
            "i_dream_dates": ["2026-09-01", "2026-09-02", "2026-09-03"],
        }
    )
    manager = _ListManager([candidate])
    monkeypatch.setattr(rt, "bucket_mgr", manager)

    with _identity.caller_context("cheng"):
        with pytest.raises(ToolInputError, match="唯一有效 owner"):
            await _promote_candidate("untagged-i", "")

    assert manager.update_calls == []


@pytest.mark.asyncio
async def test_owner_scoped_background_mutations_keep_their_success_paths(
    bucket_mgr, monkeypatch
):
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    source_id = await bucket_mgr.create_internal(
        content="source memory",
        name="source",
        domain=["test"],
        tags=["owner:cheng"],
    )
    target_id = await bucket_mgr.create_internal(
        content="target memory",
        name="target",
        domain=["test"],
        tags=["owner:cheng"],
    )

    async def inferred(_bucket_id, _content):
        return [
            {
                "target_bucket_id": target_id,
                "type": "related_to",
                "label": "",
                "status": "active",
                "auto": True,
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(_relation_link, "infer_links_for", inferred)

    class SimilarityEngine:
        enabled = True

        async def search_similar(self, _text, top_k):
            return [(target_id, 0.99)]

    monkeypatch.setattr(rt, "embedding_engine", SimilarityEngine())

    with _identity.caller_context("cheng"):
        assert await _relation_link.link_new_bucket(source_id, "source memory") == 1
        await check_duplicate_for(source_id, "source memory")
        await store_feel(
            "I noticed the connection.",
            [],
            0.7,
            0.4,
            source_id,
            "regression",
        )

    source = await bucket_mgr.get(source_id)
    target = await bucket_mgr.get(target_id)
    assert source["metadata"]["relation_links"][0]["target_bucket_id"] == target_id
    assert target["metadata"]["relation_links"][0]["target_bucket_id"] == source_id
    assert source["metadata"]["dup_candidate"] == target_id
    assert target["metadata"]["dup_candidate"] == source_id
    assert source["metadata"]["digested"] is True
    assert source["metadata"]["model_valence"] == pytest.approx(0.7)


async def _async_noop():
    return None


@pytest.mark.asyncio
async def test_deletion_request_is_visible_and_decidable_only_by_bucket_owner(
    tmp_path,
):
    foreign = _bucket("foreign", "huaiyin")
    manager = _ListManager([foreign])
    store = DeletionRequestStore(str(tmp_path), manager)
    submitted = await store.submit("foreign", "please remove")
    request_id = submitted["request"]["request_id"]

    with _identity.caller_context("cheng"):
        assert await store.render_pending_batch() == ""
        result = await store.decide(request_id, "approve")

    assert result["ok"] is False
    assert result["code"] == "owner_denied"
    assert manager.deleted == []


@pytest.mark.asyncio
async def test_pulse_lists_only_owner_scoped_details(monkeypatch):
    own = _bucket("own", "cheng")
    foreign = _bucket("foreign", "huaiyin")
    manager = _ListManager([own, foreign])
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "embedding_engine", None)
    monkeypatch.setattr(
        rt,
        "decay_engine",
        SimpleNamespace(
            ensure_started=_async_noop,
            is_running=True,
            calculate_score=lambda _meta: 1.0,
        ),
    )

    with _identity.caller_context("cheng"):
        rendered = await pulse()

    assert "own" in rendered
    assert "foreign" not in rendered
    assert "secret-foreign" not in rendered
