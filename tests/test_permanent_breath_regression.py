import asyncio
import os
from unittest.mock import MagicMock

import frontmatter
import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default
from tools.breath.search import surface_search
from tools.breath.importance import surface_by_importance
from tools._common import repair_pinned_desync


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content


class FailingDehydrator:
    async def dehydrate(self, content, meta=None):
        raise RuntimeError("dehydrate unavailable")


class EmptyEmbedding:
    enabled = False

    async def search_similar(self, query, top_k=20):
        return []


class SearchEmbedding:
    enabled = True

    async def search_similar(self, query, top_k=20):
        return []


class SearchPolicyBucketManager:
    def __init__(self):
        self.touched = []
        self.buckets = [
            self._bucket("visible", "Visible query memory.", {"name": "Visible"}),
            self._bucket("hidden", "Hidden query memory.", {"name": "Hidden", "dont_surface": True}),
            self._bucket("deleted", "Deleted query memory.", {"name": "Deleted", "deleted_at": "2026-07-03T00:00:00+00:00"}),
            self._bucket("tombstone", "Tombstone query memory.", {"name": "Tombstone", "tombstone": True}),
            self._bucket("archived", "Archived query memory.", {"name": "Archived", "type": "archived"}),
        ]

    def _bucket(self, bucket_id, content, metadata):
        base = {"type": "dynamic", "importance": 5, "domain": []}
        base.update(metadata)
        return {"id": bucket_id, "content": content, "metadata": base}

    async def search(
        self,
        query,
        limit=20,
        domain_filter=None,
        query_valence=None,
        query_arousal=None,
        vector_scores=None,
    ):
        assert query == "query"
        return list(self.buckets)

    async def touch(self, bucket_id):
        self.touched.append(bucket_id)

    async def touch_many(self, bucket_ids, ripple=False):
        self.touched.extend(bucket_ids)

    async def list_all(self, include_archive=False):
        return []


def install_runtime(bucket_mgr, decay_eng, dehydrator):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_eng
    rt.dehydrator = dehydrator
    rt.embedding_engine = EmptyEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


def install_search_runtime(bucket_mgr, decay_eng, dehydrator):
    install_runtime(bucket_mgr, decay_eng, dehydrator)
    rt.embedding_engine = SearchEmbedding()


@pytest.mark.asyncio
async def test_default_breath_surfaces_type_permanent_bucket_without_pinned_flag(bucket_mgr, decay_eng):
    bucket_id = await bucket_mgr.create(
        content="Core rule alpha must always be visible.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, decay_eng, EchoDehydrator())

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert bucket_id in result
    assert "Core rule alpha" in result


@pytest.mark.asyncio
async def test_default_breath_respects_dont_surface_even_for_core_bucket(bucket_mgr, decay_eng):
    bucket_id = await bucket_mgr.create(
        content="Core rule beta should stay hidden from spontaneous breath.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )
    await bucket_mgr.update(bucket_id, dont_surface=True)
    install_runtime(bucket_mgr, decay_eng, EchoDehydrator())

    result = await surface_default(max_results=10, max_tokens=10000, tag_filter=[])

    assert bucket_id not in result
    assert "Core rule beta" not in result


@pytest.mark.asyncio
async def test_search_breath_returns_raw_content_without_dehydration(
    bucket_mgr,
    decay_eng,
    monkeypatch,
):
    """主动检索的两个派生服务都离线时，Markdown 原文仍然可读。"""
    bucket_id = await bucket_mgr.create(
        content="Candlelit protocol belongs to the permanent rules.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, decay_eng, FailingDehydrator())

    import tools.breath.search as search_mod

    monkeypatch.setattr(search_mod.random, "random", lambda: 1.0)

    result = await surface_search(
        query="Candlelit protocol",
        max_results=10,
        max_tokens=10000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )

    assert bucket_id in result
    assert "Candlelit protocol" in result
    assert "语义索引暂不可用" in result
    assert "摘要服务暂不可用" not in result


@pytest.mark.asyncio
async def test_search_breath_filters_terminal_states_but_keeps_dont_surface(decay_eng, monkeypatch):
    bucket_mgr = SearchPolicyBucketManager()
    install_search_runtime(bucket_mgr, decay_eng, EchoDehydrator())

    import tools.breath.search as search_mod

    monkeypatch.setattr(search_mod.random, "random", lambda: 1.0)

    result = await surface_search(
        query="query",
        max_results=10,
        max_tokens=10000,
        domain="",
        valence=-1,
        arousal=-1,
        tag_filter=[],
    )
    await asyncio.sleep(0)

    assert "Visible query memory" in result
    assert "Hidden query memory" in result
    assert "Deleted query memory" in result
    assert "Tombstone query memory" in result
    assert "Archived query memory" in result
    assert "restore=True" in result
    assert bucket_mgr.touched == []


@pytest.mark.asyncio
async def test_search_domain_filter_matches_legacy_scalar_domain_on_permanent(bucket_mgr):
    permanent_id = await bucket_mgr.create(
        content="Legacy scalar domain permanent rule.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )
    await bucket_mgr.create(
        content="Dynamic bucket in the same domain but without the query phrase.",
        domain=["rules"],
    )

    path = bucket_mgr._find_bucket_file(permanent_id)
    post = frontmatter.load(path)
    post["domain"] = "rules"
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    results = await bucket_mgr.search("Legacy scalar", domain_filter=["rules"], limit=10)
    result_ids = {bucket["id"] for bucket in results}

    assert permanent_id in result_ids


@pytest.mark.asyncio
async def test_decay_cycle_preserves_explicit_permanent_bucket_without_pinned_flag(bucket_mgr, decay_eng):
    permanent_id = await bucket_mgr.create(
        content="Permanent memory is a first-class bucket type.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )

    stats = await decay_eng.run_decay_cycle()
    bucket = await bucket_mgr.get(permanent_id)

    assert stats["demoted_orphans"] == 0
    assert bucket["metadata"]["type"] == "permanent"
    assert f"{os.sep}permanent{os.sep}" in bucket["path"]


@pytest.mark.asyncio
async def test_direct_pinned_create_writes_permanent_type_and_unpin_moves_to_dynamic(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="Direct pinned create should keep type and path in sync.",
        pinned=True,
    )

    pinned = await bucket_mgr.get(bucket_id)
    assert pinned["metadata"]["type"] == "permanent"
    assert pinned["metadata"]["pinned"] is True
    assert f"{os.sep}permanent{os.sep}" in pinned["path"]

    await bucket_mgr.update(bucket_id, pinned=False)
    unpinned = await bucket_mgr.get(bucket_id)

    assert unpinned["metadata"]["type"] == "dynamic"
    assert unpinned["metadata"]["pinned"] is False
    assert f"{os.sep}dynamic{os.sep}" in unpinned["path"]


@pytest.mark.asyncio
async def test_importance_breath_falls_back_to_raw_permanent_content_when_dehydrate_fails(
    bucket_mgr,
    decay_eng,
):
    bucket_id = await bucket_mgr.create(
        content="Permanent importance fallback should be readable.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )
    install_runtime(bucket_mgr, decay_eng, FailingDehydrator())

    result = await surface_by_importance(importance_min=8, max_tokens=10000, tag_filter=[])

    assert bucket_id in result
    assert "Permanent importance fallback" in result


@pytest.mark.asyncio
async def test_repair_pinned_desync_does_not_demote_explicit_permanent_bucket(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="Permanent repair guard should stay in permanent storage.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )
    rt.logger = MagicMock()

    preview = await repair_pinned_desync(bucket_mgr, apply=False)
    applied = await repair_pinned_desync(bucket_mgr, apply=True)
    bucket = await bucket_mgr.get(bucket_id)

    assert preview["orphans"] == []
    assert applied["demoted"] == 0
    assert bucket["metadata"]["type"] == "permanent"
    assert f"{os.sep}permanent{os.sep}" in bucket["path"]


@pytest.mark.asyncio
async def test_idempotent_unpinned_update_preserves_explicit_permanent_bucket(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="Permanent buckets should survive an idempotent pinned false update.",
        bucket_type="permanent",
        importance=10,
        domain=["rules"],
    )

    await bucket_mgr.update(bucket_id, pinned=False)
    bucket = await bucket_mgr.get(bucket_id)

    assert bucket["metadata"]["type"] == "permanent"
    assert bucket["metadata"].get("pinned") is False
    assert f"{os.sep}permanent{os.sep}" in bucket["path"]
