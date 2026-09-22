from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from embedding_engine import EmbeddingEngine
from tools.breath.search import surface_search


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content


class FailingDehydrator:
    async def dehydrate(self, content, meta=None):
        raise RuntimeError("summary provider offline")


class DisabledEmbedding:
    enabled = False


class StrictEmbedding:
    enabled = True

    def __init__(self, pairs=None, error=None):
        self.pairs = list(pairs or [])
        self.error = error
        self.strict_calls = 0
        self.compat_calls = 0

    async def search_similar_strict(self, query, top_k=10):
        self.strict_calls += 1
        if self.error:
            raise self.error
        return self.pairs[:top_k]

    async def search_similar(self, query, top_k=10):
        self.compat_calls += 1
        raise AssertionError("surface_search must not issue a second vector query")

    def status(self):
        return {
            "enabled": True,
            "backend": "api",
            "model": "test-embedding",
            "vector_dim": 768,
            "embedding_count": len(self.pairs),
        }


class OutboxStatus:
    def status(self):
        return {
            "running": True,
            "provider_ready": True,
            "pending": 2,
            "retrying": 1,
            "last_success": "2026-07-19T12:00:00",
            "last_error": "provider timeout",
        }


def install_runtime(bucket_mgr, decay_eng, dehydrator, embedding):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = decay_eng
    rt.dehydrator = dehydrator
    rt.embedding_engine = embedding
    rt.embedding_outbox = None
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


async def run_search(query, *, domain="", tags=None):
    return await surface_search(
        query=query,
        max_results=10,
        max_tokens=10000,
        domain=domain,
        valence=-1,
        arousal=-1,
        tag_filter=tags or [],
    )


@pytest.mark.asyncio
async def test_dynamic_memory_remains_readable_without_summary_provider(
    bucket_mgr, decay_eng, monkeypatch
):
    bucket_id = await bucket_mgr.create_internal(
        content="The cedar notebook contains the recovery phrase.",
        domain=["operations"],
    )
    install_runtime(
        bucket_mgr,
        decay_eng,
        FailingDehydrator(),
        DisabledEmbedding(),
    )
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    result = await run_search("cedar notebook")

    assert bucket_id in result
    assert "cedar notebook" in result
    assert "语义索引暂不可用" in result
    assert "摘要服务暂不可用" not in result


@pytest.mark.asyncio
async def test_vector_provider_failure_falls_back_to_keyword_search(
    bucket_mgr, decay_eng, monkeypatch
):
    bucket_id = await bucket_mgr.create_internal(
        content="Project Halcyon uses the blue deployment lane.",
        domain=["work"],
    )
    embedding = StrictEmbedding(error=TimeoutError("provider timeout"))
    install_runtime(bucket_mgr, decay_eng, EchoDehydrator(), embedding)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    result = await run_search("Project Halcyon")

    assert bucket_id in result
    assert "语义索引暂不可用" in result
    assert embedding.strict_calls == 1
    assert embedding.compat_calls == 0


@pytest.mark.asyncio
async def test_semantic_only_candidate_is_recalled_with_one_vector_query(
    bucket_mgr, decay_eng, monkeypatch
):
    bucket_id = await bucket_mgr.create_internal(
        content="A quiet recollection with no lexical overlap.",
        domain=["journal"],
    )
    embedding = StrictEmbedding(pairs=[(bucket_id, 0.91)])
    install_runtime(bucket_mgr, decay_eng, EchoDehydrator(), embedding)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    result = await run_search("entirely different query terms")

    assert bucket_id in result
    assert "[语义关联]" in result
    assert embedding.strict_calls == 1
    assert embedding.compat_calls == 0


@pytest.mark.asyncio
async def test_semantic_search_logs_index_outbox_and_rank_diagnostics(
    bucket_mgr, decay_eng
):
    bucket_id = await bucket_mgr.create_internal(
        content="A semantic diagnostic target.",
        domain=["journal"],
    )
    embedding = StrictEmbedding(pairs=[(bucket_id, 0.91)])
    install_runtime(bucket_mgr, decay_eng, EchoDehydrator(), embedding)
    rt.embedding_outbox = OutboxStatus()

    result = await run_search("diagnostic query")

    assert bucket_id in result
    semantic_call = next(
        call for call in rt.logger.info.call_args_list
        if call.args and "phase=semantic" in str(call.args[0])
    )
    diagnostics = semantic_call.args[1]
    assert diagnostics["engine"]["model"] == "test-embedding"
    assert diagnostics["engine"]["embedding_count"] == 1
    assert diagnostics["outbox"]["pending"] == 2
    assert diagnostics["outbox"]["retrying"] == 1
    assert diagnostics["vector_top"] == [
        {"bucket_id": bucket_id, "score": 0.91}
    ]
    assert "diagnostic query" not in str(diagnostics)


@pytest.mark.asyncio
async def test_semantic_candidate_cannot_bypass_domain_filter(
    bucket_mgr, decay_eng, monkeypatch
):
    allowed_id = await bucket_mgr.create_internal(
        content="Allowed workspace memory contains the quartz marker.",
        domain=["work"],
    )
    blocked_id = await bucket_mgr.create_internal(
        content="Private journal memory unrelated to work.",
        domain=["private"],
    )
    embedding = StrictEmbedding(pairs=[(blocked_id, 0.99)])
    install_runtime(bucket_mgr, decay_eng, EchoDehydrator(), embedding)
    monkeypatch.setattr("tools.breath.search.random.random", lambda: 1.0)

    result = await run_search("quartz marker", domain="work")

    assert allowed_id in result
    assert blocked_id not in result


@pytest.mark.asyncio
async def test_missing_vector_does_not_reduce_keyword_candidate_score(bucket_mgr):
    bucket_mgr._bm25 = None
    target_id = await bucket_mgr.create_internal(
        content="Orchid release checklist is ready.",
        domain=["work"],
    )
    other_id = await bucket_mgr.create_internal(
        content="A separate memory with a vector.",
        domain=["misc"],
    )

    without_semantic = await bucket_mgr.search(
        "Orchid release checklist",
        vector_scores={},
    )
    with_other_vector = await bucket_mgr.search(
        "Orchid release checklist",
        vector_scores={other_id: 0.99},
    )
    score_without = next(item["score"] for item in without_semantic if item["id"] == target_id)
    score_with = next(item["score"] for item in with_other_vector if item["id"] == target_id)

    assert score_with == score_without


@pytest.mark.asyncio
async def test_empty_vector_index_does_not_call_query_provider(tmp_path, monkeypatch):
    engine = EmbeddingEngine({
        "buckets_dir": str(tmp_path / "vault"),
        "embedding": {
            "enabled": True,
            "api_key": "not-used",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
        },
    })

    async def unexpected_provider_call(_query):
        raise AssertionError("empty index should not spend a query embedding call")

    monkeypatch.setattr(engine, "_generate_async", unexpected_provider_call)

    assert await engine.search_similar_strict("anything", top_k=5) == []
