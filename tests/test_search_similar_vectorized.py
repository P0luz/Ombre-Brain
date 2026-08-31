import json
import random
import sqlite3

import pytest

import embedding_engine as embedding_module
from embedding_engine import EmbeddingEngine


def test_cosine_similarity_batch_matches_pairwise():
    random.seed(1234)
    query = [random.uniform(-1, 1) for _ in range(64)]
    vectors = [[random.uniform(-1, 1) for _ in range(64)] for _ in range(20)]

    batch = EmbeddingEngine._cosine_similarity_batch(query, vectors)
    pairwise = [EmbeddingEngine._cosine_similarity(query, vector) for vector in vectors]

    assert list(batch) == pytest.approx(pairwise, abs=1e-12)


def test_cosine_similarity_batch_handles_zero_norms():
    batch = EmbeddingEngine._cosine_similarity_batch(
        [1.0, 0.0], [[0.0, 0.0], [1.0, 0.0]]
    )
    assert list(batch) == pytest.approx([0.0, 1.0])


@pytest.mark.asyncio
async def test_vectorized_search_preserves_content_meaning_and_tie_behavior(
    tmp_path, monkeypatch
):
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    engine = EmbeddingEngine({
        "buckets_dir": str(buckets_dir),
        "embedding": {
            "enabled": True,
            "api_key": "test-key",
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "dim": 3,
        },
    })

    async def generate(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(engine, "_generate_async", generate)
    now = "2026-01-01T00:00:00Z"
    with sqlite3.connect(engine.db_path) as conn:
        rows = [
            ("meaning_wins", [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]),
            ("tie_first", [0.0, 1.0, 0.0], None),
            ("tie_second", [0.0, -1.0, 0.0], None),
            ("dim_mismatch", [1.0, 0.0], None),
        ]
        for bucket_id, content, meaning in rows:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    bucket_id,
                    json.dumps(content),
                    json.dumps(meaning) if meaning is not None else None,
                    now,
                    "",
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("malformed", "{bad json", None, now, ""),
        )

    results = await engine.search_similar_strict("query", top_k=10)

    assert results[0][0] == "meaning_wins"
    assert results[0][1] == pytest.approx(1.0)
    assert [bucket_id for bucket_id, _ in results[1:]] == [
        "tie_first",
        "tie_second",
        "dim_mismatch",
    ]
    assert "malformed" not in dict(results)


@pytest.mark.asyncio
async def test_allowed_ids_filter_is_applied_before_hidden_vector_is_decoded(
    tmp_path, monkeypatch
):
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    engine = EmbeddingEngine({
        "buckets_dir": str(buckets_dir),
        "embedding": {
            "enabled": True,
            "api_key": "test-key",
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "dim": 3,
        },
    })

    async def generate(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(engine, "_generate_async", generate)
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("hidden", "{malformed-hidden-vector", None, "now", ""),
        )
        conn.execute(
            "INSERT OR REPLACE INTO embeddings "
            "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            ("visible", json.dumps([1.0, 0.0, 0.0]), None, "now", ""),
        )

    results = await engine.search_similar_strict(
        "query", top_k=10, allowed_bucket_ids={"visible"}
    )
    assert results == [("visible", pytest.approx(1.0))]


@pytest.mark.asyncio
async def test_vector_search_bounds_matrix_to_sqlite_batch(tmp_path, monkeypatch):
    """A large vault must not become one giant Python-list + NumPy matrix."""
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    engine = EmbeddingEngine({
        "buckets_dir": str(buckets_dir),
        "embedding": {
            "enabled": True,
            "api_key": "test-key",
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "dim": 3,
        },
    })

    async def generate(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(engine, "_generate_async", generate)
    monkeypatch.setattr(embedding_module, "_SEARCH_BATCH_ROWS", 2)
    now = "2026-01-01T00:00:00Z"
    with sqlite3.connect(engine.db_path) as conn:
        for index in range(7):
            vector = [1.0, float(index), 0.0]
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"bucket-{index}",
                    json.dumps(vector),
                    json.dumps(vector),
                    now,
                    "",
                ),
            )

    original_batch = engine._cosine_similarity_batch
    matrix_sizes: list[int] = []

    def record_batch(query, vectors):
        matrix_sizes.append(len(vectors))
        assert len(vectors) <= 4  # two SQLite rows × content/meaning vectors
        return original_batch(query, vectors)

    monkeypatch.setattr(engine, "_cosine_similarity_batch", record_batch)

    results = await engine.search_similar_strict("query", top_k=7)

    assert len(results) == 7
    assert matrix_sizes == [4, 4, 4, 2]


@pytest.mark.asyncio
async def test_vector_search_bounds_ranking_heap_and_preserves_earliest_ties(
    tmp_path, monkeypatch
):
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    engine = EmbeddingEngine({
        "buckets_dir": str(buckets_dir),
        "embedding": {
            "enabled": True,
            "api_key": "test-key",
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "dim": 3,
        },
    })

    async def generate(_text):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(engine, "_generate_async", generate)
    monkeypatch.setattr(embedding_module, "_SEARCH_BATCH_ROWS", 2)
    now = "2026-01-01T00:00:00Z"
    with sqlite3.connect(engine.db_path) as conn:
        for index in range(20):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(bucket_id, embedding, meaning_embedding, updated_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"tie-{index:02d}", "[1, 0, 0]", None, now, ""),
            )

    real_push = embedding_module.heapq.heappush
    real_replace = embedding_module.heapq.heapreplace
    observed_heap_sizes: list[int] = []

    def tracked_push(heap, item):
        result = real_push(heap, item)
        observed_heap_sizes.append(len(heap))
        return result

    def tracked_replace(heap, item):
        result = real_replace(heap, item)
        observed_heap_sizes.append(len(heap))
        return result

    monkeypatch.setattr(embedding_module.heapq, "heappush", tracked_push)
    monkeypatch.setattr(embedding_module.heapq, "heapreplace", tracked_replace)

    results = await engine.search_similar_strict("query", top_k=3)

    assert [bucket_id for bucket_id, _score in results] == [
        "tie-00",
        "tie-01",
        "tie-02",
    ]
    assert max(observed_heap_sizes) == 3
