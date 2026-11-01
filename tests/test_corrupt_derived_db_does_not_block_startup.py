from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

CORRUPT = {
    "not-a-database": "这根本不是数据库".encode("utf-8") * 50,
    "random-bytes": bytes(range(256)) * 40,
    "sqlite-header-then-garbage": b"SQLite format 3\x00" + b"\xff" * 4000,
}


def _truncated_db(path: Path, keep) -> bytes:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t(a)")
    conn.executemany("INSERT INTO t VALUES (?)", [(str(i) * 200,) for i in range(400)])
    conn.commit()
    conn.close()
    raw = path.read_bytes()
    path.unlink()
    return raw[: keep(len(raw))]


def _embedding_config(root: Path) -> dict:
    return {
        "buckets_dir": str(root),
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


def _dehydration_config(root: Path) -> dict:
    return {
        "buckets_dir": str(root),
        "dehydration": {
            "enabled": True,
            "api_format": "openai_compat",
            "base_url": "https://example.invalid/v1",
            "api_key": "k",
            "model": "m",
            "timeout_seconds": 5,
        },
    }


def _quarantined(directory: Path, stem: str) -> list[Path]:
    return list(directory.glob(f"{stem}.corrupt-*"))


@pytest.mark.parametrize("blob", CORRUPT.values(), ids=list(CORRUPT))
def test_corrupt_vector_db_is_quarantined_instead_of_raising(tmp_path, blob):
    from embedding_engine import EmbeddingEngine

    config = _embedding_config(tmp_path)
    db = Path(EmbeddingEngine(config).db_path)
    db.write_bytes(blob)

    engine = EmbeddingEngine(config)

    assert _quarantined(db.parent, db.name)
    assert sqlite3.connect(str(db)).execute(
        "SELECT count(*) FROM embeddings"
    ).fetchone() == (0,)
    assert engine.enabled is True


@pytest.mark.parametrize("keep", [lambda n: n // 2, lambda n: 200], ids=["half", "head"])
def test_truncated_vector_db_is_quarantined(tmp_path, keep):
    from embedding_engine import EmbeddingEngine

    config = _embedding_config(tmp_path)
    db = Path(EmbeddingEngine(config).db_path)
    db.write_bytes(_truncated_db(db, keep))

    EmbeddingEngine(config)

    assert _quarantined(db.parent, db.name)


def test_healthy_vector_db_is_never_quarantined(tmp_path):
    from embedding_engine import EmbeddingEngine

    config = _embedding_config(tmp_path)
    db = Path(EmbeddingEngine(config).db_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO embeddings (bucket_id, embedding, updated_at) VALUES ('b1', '[1]', 'x')"
    )
    conn.commit()
    conn.close()

    EmbeddingEngine(config)

    assert not _quarantined(db.parent, db.name)
    assert sqlite3.connect(str(db)).execute(
        "SELECT count(*) FROM embeddings"
    ).fetchone() == (1,)


@pytest.mark.parametrize("blob", CORRUPT.values(), ids=list(CORRUPT))
def test_corrupt_dehydration_cache_is_quarantined(tmp_path, blob):
    from dehydrator import Dehydrator

    config = _dehydration_config(tmp_path)
    cache = Path(Dehydrator(config).cache_db_path)
    cache.write_bytes(blob)

    dehydrator = Dehydrator(config)

    assert _quarantined(cache.parent, cache.name)
    assert dehydrator._cache_conn.execute(
        "SELECT count(*) FROM dehydration_cache"
    ).fetchone() == (0,)


def test_healthy_dehydration_cache_keeps_its_rows(tmp_path):
    from dehydrator import Dehydrator

    config = _dehydration_config(tmp_path)
    first = Dehydrator(config)
    first._cache_conn.execute(
        "INSERT INTO dehydration_cache (content_hash, summary, model)"
        " VALUES ('h', 's', 'm')"
    )
    first._cache_conn.commit()
    cache = Path(first.cache_db_path)
    first._cache_conn.close()

    second = Dehydrator(config)

    assert not _quarantined(cache.parent, cache.name)
    assert second._cache_conn.execute(
        "SELECT count(*) FROM dehydration_cache"
    ).fetchone() == (1,)
