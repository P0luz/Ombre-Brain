from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools import _identity
from tools.dream import dispatch as dream_dispatch


class DummyDecay:
    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        return float(meta.get("importance") or 5)


class DisabledEmbedding:
    enabled = False


def install_runtime(bucket_mgr):
    rt.config = {
        "identity_filter": {
            "enabled": True,
            "untagged": "allow",
            "known_owners": ["cheng", "huaiyin", "huaiyin_cc"],
            "shared_owner_values": ["shared", "shared_core"],
            "exclude_shared_values": ["shared_context", "shared_resource"],
        },
        "surfacing": {"feel_max_tokens": 6000},
    }
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = DummyDecay()
    rt.embedding_engine = DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None


@pytest.mark.asyncio
async def test_dream_filters_recent_core_plans_and_feels_by_caller(bucket_mgr):
    own_core_id = await bucket_mgr.create_internal(
        "OWN CORE",
        tags=["owner:huaiyin_cc"],
        pinned=True,
    )
    own_ids = {
        await bucket_mgr.create_internal("OWN RECENT", tags=["owner:huaiyin_cc"]),
        await bucket_mgr.create_internal(
            "OWN PLAN",
            tags=["owner:huaiyin_cc"],
            bucket_type="plan",
        ),
        await bucket_mgr.create_internal(
            "OWN FEEL",
            tags=["owner:huaiyin_cc"],
            bucket_type="feel",
        ),
    }
    foreign_ids = {
        await bucket_mgr.create_internal("FOREIGN RECENT", tags=["owner:cheng"]),
        await bucket_mgr.create_internal(
            "FOREIGN CORE",
            tags=["owner:cheng"],
            pinned=True,
        ),
        await bucket_mgr.create_internal(
            "FOREIGN PLAN",
            tags=["owner:cheng"],
            bucket_type="plan",
        ),
        await bucket_mgr.create_internal(
            "FOREIGN FEEL",
            tags=["owner:cheng"],
            bucket_type="feel",
        ),
    }
    untagged_id = await bucket_mgr.create_internal("UNTAGGED LEGACY")
    install_runtime(bucket_mgr)

    _identity.set_caller("huaiyin_cc")
    try:
        result = await dream_dispatch(window_hours=48)
    finally:
        _identity.set_caller("")

    for bucket_id in own_ids:
        assert bucket_id in result
    assert own_core_id not in result
    assert "OWN CORE" not in result
    for bucket_id in foreign_ids:
        assert bucket_id not in result
    assert untagged_id in result
    assert "FOREIGN RECENT" not in result
    assert "FOREIGN CORE" not in result
    assert "FOREIGN PLAN" not in result
    assert "FOREIGN FEEL" not in result


@pytest.mark.asyncio
async def test_dream_keeps_legacy_behavior_without_caller(bucket_mgr):
    cheng_id = await bucket_mgr.create_internal("CHENG MEMORY", tags=["owner:cheng"])
    cc_id = await bucket_mgr.create_internal("CC MEMORY", tags=["owner:huaiyin_cc"])
    install_runtime(bucket_mgr)

    _identity.set_caller("")
    result = await dream_dispatch(window_hours=48)

    assert cheng_id in result
    assert cc_id in result
