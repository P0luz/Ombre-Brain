from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from ombrebrain.storage.vector_codec import (
    decode_vector,
    encode_vector,
    is_valid_stored_vector,
)

VECTOR = [0.5, -0.25, 0.125, 0.0]


def test_blob_round_trip():
    assert decode_vector(encode_vector(VECTOR)).tolist() == VECTOR


def test_legacy_json_text_is_still_decoded():
    assert decode_vector(json.dumps(VECTOR)).tolist() == VECTOR


def test_both_formats_decode_to_the_same_vector():
    assert np.array_equal(
        decode_vector(encode_vector(VECTOR)), decode_vector(json.dumps(VECTOR))
    )


def test_blob_is_much_smaller_than_the_json_it_replaces():
    # 真实向量是长小数（0.039182737…），不是 0.001 这种短的
    rng = np.random.default_rng(0)
    big = rng.standard_normal(4096).tolist()
    blob = len(encode_vector(big))
    text = len(json.dumps(big).encode("utf-8"))
    assert blob * 4 < text, f"blob={blob} json={text}"


@pytest.mark.parametrize(
    "raw",
    [b"", b"abc", b"\x00" * 6, "", "[]", "not json", "{}", "[1, \"two\"]", "[1, null]", None, 42],
)
def test_unusable_cells_are_refused(raw):
    assert is_valid_stored_vector(raw) is False
    with pytest.raises((ValueError, TypeError, json.JSONDecodeError)):
        decode_vector(raw)


@pytest.mark.parametrize("bad", [[float("inf")], [float("nan")], [1.0, float("-inf")]])
def test_non_finite_values_are_refused_in_both_directions(bad):
    with pytest.raises(ValueError):
        encode_vector(bad)
    with pytest.raises(ValueError):
        decode_vector(json.dumps(bad).replace("Infinity", "1e999"))


def test_sqlite_keeps_a_blob_in_the_text_column(tmp_path):
    db = str(tmp_path / "e.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL)")
    conn.execute("INSERT INTO embeddings VALUES (?, ?)", ("b1", encode_vector(VECTOR)))
    conn.execute("INSERT INTO embeddings VALUES (?, ?)", ("b2", json.dumps(VECTOR)))
    conn.commit()

    rows = dict(conn.execute("SELECT bucket_id, typeof(embedding) FROM embeddings"))
    assert rows == {"b1": "blob", "b2": "text"}

    for bucket_id, raw in conn.execute("SELECT bucket_id, embedding FROM embeddings"):
        assert decode_vector(raw).tolist() == VECTOR, bucket_id
    conn.close()


def test_presence_query_still_finds_both_formats(tmp_path):
    db = str(tmp_path / "e.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO embeddings VALUES (?, ?)",
        [
            ("blob", encode_vector(VECTOR)),
            ("json", json.dumps(VECTOR)),
            ("blank", ""),
            ("whitespace_bytes", encode_vector([1.356e-19, 0.5])),
        ],
    )
    conn.commit()

    found = {
        row[0]
        for row in conn.execute("SELECT bucket_id FROM embeddings WHERE TRIM(embedding) <> ''")
    }
    assert found == {"blob", "json", "whitespace_bytes"}
    conn.close()


def test_projection_diagnostic_accepts_both_formats():
    from ombrebrain.projection.projection_vector import _valid_vector_json

    assert _valid_vector_json(encode_vector(VECTOR)) is True
    assert _valid_vector_json(json.dumps(VECTOR)) is True
    assert _valid_vector_json(b"\x01\x02\x03") is False


def test_migration_keeps_blob_vectors():
    from migrate_engine import MigrateEngine

    kept = MigrateEngine._normalize_embedding_vector(
        encode_vector(VECTOR), "blob", len(encode_vector(VECTOR)), len(VECTOR)
    )
    assert kept is not None
    assert decode_vector(kept).tolist() == VECTOR


def test_migration_still_refuses_a_wrong_dimension_blob():
    blob = encode_vector(VECTOR)
    assert (
        MigrateEngineRef()._normalize_embedding_vector(blob, "blob", len(blob), len(VECTOR) + 1)
        is None
    )


def MigrateEngineRef():
    from migrate_engine import MigrateEngine

    return MigrateEngine


# ---- 真实引擎的端到端 ----

def _engine(tmp_path):
    from embedding_engine import EmbeddingEngine

    return EmbeddingEngine(
        {
            "buckets_dir": str(tmp_path),
            "embedding": {
                "enabled": True,
                "provider": "openai",
                "api_format": "openai_compat",
                "base_url": "https://example.invalid/v1",
                "api_key": "k",
                "model": "m",
                "timeout_seconds": 5,
            },
        }
    )


def _seed(engine, rows):
    conn = sqlite3.connect(engine.db_path)
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings (bucket_id, embedding, updated_at, content_hash)"
        " VALUES (?, ?, '2026-08-28T00:00:00', 'h')",
        rows,
    )
    conn.commit()
    conn.close()


def test_engine_writes_blobs_and_reads_them_back(tmp_path):
    engine = _engine(tmp_path)
    engine._store_embedding("b1", VECTOR, "hash")

    conn = sqlite3.connect(engine.db_path)
    stored_type = conn.execute(
        "SELECT typeof(embedding) FROM embeddings WHERE bucket_id = 'b1'"
    ).fetchone()[0]
    conn.close()

    assert stored_type == "blob"


@pytest.mark.asyncio
async def test_get_embedding_reads_a_legacy_json_row(tmp_path):
    engine = _engine(tmp_path)
    _seed(engine, [("legacy", json.dumps(VECTOR))])

    assert await engine.get_embedding("legacy") == VECTOR


@pytest.mark.asyncio
async def test_get_embedding_reads_a_blob_row(tmp_path):
    engine = _engine(tmp_path)
    _seed(engine, [("modern", encode_vector(VECTOR))])

    assert await engine.get_embedding("modern") == VECTOR


@pytest.mark.asyncio
async def test_mixed_rows_rank_identically(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    rng = np.random.default_rng(7)
    vectors = {f"v{i}": rng.standard_normal(16).astype(np.float32).tolist() for i in range(12)}
    # 同一批向量，一半存成老 JSON，一半存成 BLOB
    _seed(
        engine,
        [
            (bid, json.dumps(vec) if index % 2 else encode_vector(vec))
            for index, (bid, vec) in enumerate(vectors.items())
        ],
    )
    query = rng.standard_normal(16).astype(np.float32).tolist()

    async def fake_query(_text):
        return query

    monkeypatch.setattr(engine, "_generate_async", fake_query)
    ranked = await engine.search_similar_strict("q", top_k=12)

    matrix = np.asarray(list(vectors.values()), dtype=np.float64)
    q = np.asarray(query, dtype=np.float64)
    expected = matrix @ q / (np.linalg.norm(matrix, axis=1) * np.linalg.norm(q))
    want = sorted(zip(vectors, expected), key=lambda kv: -kv[1])

    assert [bid for bid, _ in ranked] == [bid for bid, _ in want]
    for (_, got), (_, exp) in zip(ranked, want):
        assert got == pytest.approx(exp, abs=1e-5)


@pytest.mark.asyncio
async def test_a_corrupt_row_is_skipped_not_fatal(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    good = [1.0, 0.0, 0.0, 0.0]
    _seed(engine, [("good", encode_vector(good)), ("truncated", b"\x00\x01\x02")])

    async def fake_query(_text):
        return good

    monkeypatch.setattr(engine, "_generate_async", fake_query)
    ranked = await engine.search_similar_strict("q", top_k=10)

    assert [bid for bid, _ in ranked] == ["good"]


# ---- 存量 JSON 行的启动回填 ----

def _typeof_counts(db):
    conn = sqlite3.connect(db)
    out = dict(conn.execute("SELECT typeof(embedding), COUNT(*) FROM embeddings GROUP BY 1"))
    conn.close()
    return out


def test_startup_converts_legacy_json_rows(tmp_path):
    engine = _engine(tmp_path)
    _seed(engine, [(f"b{i}", json.dumps(VECTOR)) for i in range(20)])
    assert _typeof_counts(engine.db_path) == {"text": 20}

    _engine(tmp_path)

    assert _typeof_counts(engine.db_path) == {"blob": 20}


@pytest.mark.asyncio
async def test_conversion_preserves_the_vector(tmp_path):
    engine = _engine(tmp_path)
    _seed(engine, [("b1", json.dumps(VECTOR))])

    converted = _engine(tmp_path)

    assert await converted.get_embedding("b1") == pytest.approx(VECTOR)


@pytest.mark.parametrize("bad", ["not json", "", "[]", "[1, \"two\"]"])
def test_unconvertible_rows_are_left_alone(tmp_path, bad):
    engine = _engine(tmp_path)
    _seed(engine, [("good", json.dumps(VECTOR)), ("bad", bad)])

    _engine(tmp_path)

    conn = sqlite3.connect(engine.db_path)
    kept = conn.execute("SELECT embedding FROM embeddings WHERE bucket_id = 'bad'").fetchone()[0]
    good_type = conn.execute(
        "SELECT typeof(embedding) FROM embeddings WHERE bucket_id = 'good'"
    ).fetchone()[0]
    conn.close()

    assert kept == bad
    assert good_type == "blob"


def test_a_bad_row_does_not_block_later_rows(tmp_path):
    engine = _engine(tmp_path)
    rows = [("bad", "not json")] + [(f"b{i}", json.dumps(VECTOR)) for i in range(30)]
    _seed(engine, rows)

    _engine(tmp_path)

    assert _typeof_counts(engine.db_path) == {"blob": 30, "text": 1}


def test_blob_rows_are_not_rewritten(tmp_path):
    engine = _engine(tmp_path)
    _seed(engine, [("b1", encode_vector(VECTOR))])
    conn = sqlite3.connect(engine.db_path)
    before = conn.execute("SELECT embedding FROM embeddings WHERE bucket_id='b1'").fetchone()[0]
    conn.close()

    _engine(tmp_path)

    conn = sqlite3.connect(engine.db_path)
    after = conn.execute("SELECT embedding FROM embeddings WHERE bucket_id='b1'").fetchone()[0]
    conn.close()
    assert after == before


def test_meaning_embedding_is_converted_too(tmp_path):
    engine = _engine(tmp_path)
    conn = sqlite3.connect(engine.db_path)
    conn.execute(
        "INSERT INTO embeddings (bucket_id, embedding, updated_at, content_hash,"
        " meaning_embedding) VALUES ('b1', ?, 't', 'h', ?)",
        (json.dumps(VECTOR), json.dumps(VECTOR)),
    )
    conn.commit()
    conn.close()

    _engine(tmp_path)

    conn = sqlite3.connect(engine.db_path)
    kinds = conn.execute(
        "SELECT typeof(embedding), typeof(meaning_embedding) FROM embeddings"
    ).fetchone()
    conn.close()
    assert kinds == ("blob", "blob")


def test_backfill_respects_its_time_budget(tmp_path, monkeypatch):
    import embedding_engine as ee

    engine = _engine(tmp_path)
    _seed(engine, [(f"b{i}", json.dumps(VECTOR)) for i in range(50)])
    monkeypatch.setattr(ee, "_BACKFILL_BUDGET_SECONDS", -1.0)

    _engine(tmp_path)

    assert _typeof_counts(engine.db_path) == {"text": 50}
