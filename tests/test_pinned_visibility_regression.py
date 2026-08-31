from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.search import surface_search
from tools.breath.surface import surface_default
from tools.dream import dispatch as dream_dispatch


class EmptyDehydrator:
    async def dehydrate(self, content, meta=None):
        return ""


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content


class DummyDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, meta):
        if meta.get("pinned") or meta.get("protected") or meta.get("type") == "permanent":
            return 999.0
        return float(meta.get("importance") or 5)


class EmptyEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


def install_runtime(bucket_mgr, dehydrator):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = DummyDecay()
    rt.dehydrator = dehydrator
    rt.embedding_engine = EmptyEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None


@pytest.mark.asyncio
async def test_default_breath_falls_back_to_raw_pinned_content_when_dehydrate_returns_empty(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="Pinned bucket body must remain readable.",
        pinned=True,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, EmptyDehydrator())

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert bucket_id in result
    assert "Pinned bucket body must remain readable" in result


@pytest.mark.asyncio
async def test_dream_does_not_surface_pinned_bucket_content(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="Pinned dream context must remain visible.",
        pinned=True,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, EchoDehydrator())

    result = await dream_dispatch(window_hours=48)

    assert bucket_id not in result
    assert "Pinned dream context must remain visible" not in result
    assert "过去 48 小时内没有需要消化的新记忆" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket_type", ["dynamic", "permanent"])
async def test_protected_memory_is_silent_by_default_but_explicitly_readable(
    bucket_mgr,
    bucket_type,
):
    content = f"Protected {bucket_type} memory is explicit-read only."
    bucket_id = await bucket_mgr.create(
        content=content,
        protected=True,
        bucket_type=bucket_type,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, EchoDehydrator())

    breath = await surface_default(
        max_results=10,
        max_tokens=10000,
        tag_filter=[],
    )
    dream = await dream_dispatch(window_hours=48)
    explicit = await surface_search(
        query=bucket_id,
        max_results=1,
        max_tokens=10000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )

    assert content not in breath
    assert content not in dream
    assert content in explicit
    assert "受保护记忆" in explicit
