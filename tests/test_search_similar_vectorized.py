"""search_similar_strict 向量化改造的一致性 + 边界行为测试。

覆盖两层：
1. _cosine_similarity_batch（numpy 矩阵路径）在数值上必须和逐对调用
   _cosine_similarity（纯 Python 路径）一致——浮点近似比较。
2. search_similar_strict 端到端：malformed / 维度不匹配 / 零向量 的跳过与
   安全处理，以及 top-k 排序正确性。这部分原来没有针对真实 EmbeddingEngine
   的覆盖（tests/test_comprehensive.py 只测了 _cosine_similarity 本身）。
"""

import json
import os
import random
import sqlite3

import pytest

from embedding_engine import EmbeddingEngine

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _make_engine(tmp_path, dim=1024):
    buckets_dir = tmp_path / "buckets"
    os.makedirs(buckets_dir, exist_ok=True)
    engine = EmbeddingEngine(
        {
            "buckets_dir": str(buckets_dir),
            "embedding": {
                "enabled": True,
                "api_key": "test-key",
                "api_format": "openai_compat",
                "base_url": GEMINI_OPENAI_BASE_URL,
                "model": "gemini-embedding-001",
                "dim": dim,
            },
        }
    )
    return engine


class TestCosineSimilarityBatchMatchesPairwise:
    """numpy 矩阵路径必须和纯 Python 逐对路径数值一致。"""

    def test_random_vectors_match_pairwise(self):
        random.seed(1234)
        dim = 1024
        query = [random.uniform(-1, 1) for _ in range(dim)]
        vectors = [[random.uniform(-1, 1) for _ in range(dim)] for _ in range(92)]

        batch = EmbeddingEngine._cosine_similarity_batch(query, vectors)
        pairwise = [EmbeddingEngine._cosine_similarity(query, v) for v in vectors]

        assert len(batch) == len(pairwise)
        for b, p in zip(batch, pairwise):
            assert b == pytest.approx(p, abs=1e-9)

    def test_identical_vectors_match_pairwise(self):
        v = [0.5, -0.5, 1.0, 0.0]
        batch = EmbeddingEngine._cosine_similarity_batch(v, [v, v])
        for b in batch:
            assert b == pytest.approx(1.0, abs=1e-9)

    def test_zero_vector_row_matches_pairwise_zero(self):
        query = [1.0, 0.0, 0.0]
        vectors = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
        batch = EmbeddingEngine._cosine_similarity_batch(query, vectors)
        pairwise = [EmbeddingEngine._cosine_similarity(query, v) for v in vectors]
        assert batch[0] == pytest.approx(0.0, abs=1e-9) == pytest.approx(pairwise[0], abs=1e-9)
        assert batch[1] == pytest.approx(1.0, abs=1e-9) == pytest.approx(pairwise[1], abs=1e-9)

    def test_zero_query_vector_is_safe(self):
        query = [0.0, 0.0, 0.0]
        vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        batch = EmbeddingEngine._cosine_similarity_batch(query, vectors)
        assert list(batch) == pytest.approx([0.0, 0.0], abs=1e-9)


@pytest.mark.asyncio
class TestSearchSimilarStrictVectorized:
    async def test_skips_malformed_and_dim_mismatch_but_keeps_valid(self, tmp_path):
        engine = _make_engine(tmp_path, dim=4)
        query_vec = [1.0, 0.0, 0.0, 0.0]
        engine._generate_async = lambda text: _async_return(query_vec)

        engine._store_embedding("good_same", [1.0, 0.0, 0.0, 0.0])
        engine._store_embedding("good_orthogonal", [0.0, 1.0, 0.0, 0.0])
        engine._store_embedding("zero_vec", [0.0, 0.0, 0.0, 0.0])
        engine._store_embedding("dim_mismatch", [1.0, 0.0])  # 2 维 != query 4 维

        # 直接写一行非法 JSON，模拟 malformed embedding。
        conn = sqlite3.connect(engine.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (bucket_id, embedding, updated_at, content_hash) "
                "VALUES (?, ?, ?, ?)",
                ("malformed", "{not valid json", "2026-01-01T00:00:00Z", ""),
            )
            conn.commit()
        finally:
            conn.close()

        results = await engine.search_similar_strict("query", top_k=10)
        ids = [bid for bid, _ in results]

        assert "malformed" not in ids
        assert "dim_mismatch" not in ids
        assert "good_same" in ids
        assert "good_orthogonal" in ids
        assert "zero_vec" in ids

        scores = dict(results)
        assert scores["good_same"] == pytest.approx(1.0, abs=1e-9)
        assert scores["good_orthogonal"] == pytest.approx(0.0, abs=1e-9)
        assert scores["zero_vec"] == pytest.approx(0.0, abs=1e-9)

    async def test_top_k_ordering_matches_pure_python_reference(self, tmp_path):
        engine = _make_engine(tmp_path, dim=16)
        random.seed(99)
        query_vec = [random.uniform(-1, 1) for _ in range(16)]
        engine._generate_async = lambda text: _async_return(query_vec)

        stored = {}
        for i in range(50):
            vec = [random.uniform(-1, 1) for _ in range(16)]
            bucket_id = f"bucket_{i}"
            stored[bucket_id] = vec
            engine._store_embedding(bucket_id, vec)

        top_k = 5
        results = await engine.search_similar_strict("query", top_k=top_k)

        # 纯 Python 参照实现：逐对 _cosine_similarity + 排序，不走矩阵路径。
        reference = sorted(
            ((bid, EmbeddingEngine._cosine_similarity(query_vec, vec)) for bid, vec in stored.items()),
            key=lambda x: x[1],
            reverse=True,
        )[:top_k]

        assert len(results) == top_k
        assert [bid for bid, _ in results] == [bid for bid, _ in reference]
        for (_, sim_vec), (_, sim_ref) in zip(results, reference):
            assert sim_vec == pytest.approx(sim_ref, abs=1e-9)

    async def test_no_valid_rows_returns_empty(self, tmp_path):
        engine = _make_engine(tmp_path, dim=4)
        query_vec = [1.0, 0.0, 0.0, 0.0]
        engine._generate_async = lambda text: _async_return(query_vec)
        engine._store_embedding("dim_mismatch", [1.0, 0.0])

        results = await engine.search_similar_strict("query", top_k=10)
        assert results == []


async def _async_return(value):
    return value
