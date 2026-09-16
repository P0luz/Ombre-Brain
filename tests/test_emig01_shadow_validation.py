"""Strict synthetic shadow validation; no production DB is opened."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys

import pytest

from embedding_publish import (
    ShadowValidationError,
    create_shadow_path,
    validate_shadow_generation,
)

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from emig01_process_worker import create_embedding_db


def _valid_pair(tmp_path: Path):
    live = tmp_path / "embeddings.db"
    create_embedding_db(live, model="old-model", rows={"old": [0.0, 1.0]})
    shadow = create_shadow_path(live)
    create_embedding_db(
        shadow,
        model="new-model",
        rows={"a": [0.1, 0.2], "b": [0.3, 0.4]},
    )
    return live, shadow


def test_valid_shadow_returns_non_content_fingerprint(tmp_path):
    live, shadow = _valid_pair(tmp_path)
    metadata = validate_shadow_generation(
        shadow,
        live_db_path=live,
        expected_model="new-model",
        expected_dim=2,
        expected_count=2,
        expected_bucket_ids={"a", "b"},
    )
    assert metadata["leaf"] == shadow.name
    assert metadata["row_count"] == 2
    assert metadata["model"] == "new-model"
    assert metadata["dimensions"] == 2
    assert len(metadata["sha256"]) == 64
    assert "embedding" not in metadata


def test_shadow_generation_metadata_must_match_internal_txid(tmp_path):
    live, shadow = _valid_pair(tmp_path)
    with pytest.raises(ShadowValidationError, match="generation metadata mismatch"):
        validate_shadow_generation(
            shadow,
            live_db_path=live,
            expected_model="new-model",
            expected_dim=2,
            expected_count=2,
            expected_generation="0" * 32,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_model", "wrong", "model metadata mismatch"),
        ("expected_dim", 3, "dimension metadata mismatch"),
        ("expected_count", 3, "row count mismatch"),
        ("expected_bucket_ids", {"a", "missing"}, "bucket set mismatch"),
    ],
)
def test_model_dimension_count_and_bucket_set_must_match(
    tmp_path,
    field,
    value,
    message,
):
    live, shadow = _valid_pair(tmp_path)
    kwargs = {
        "expected_model": "new-model",
        "expected_dim": 2,
        "expected_count": 2,
        "expected_bucket_ids": {"a", "b"},
    }
    kwargs[field] = value
    with pytest.raises(ShadowValidationError, match=message):
        validate_shadow_generation(shadow, live_db_path=live, **kwargs)


@pytest.mark.parametrize(
    "bad_vector",
    [
        [0.1],
        [0.1, float("nan")],
        [0.1, float("inf")],
        [0.1, True],
        {"not": "a list"},
        "not-json",
    ],
)
def test_invalid_vector_shape_type_or_finiteness_is_rejected(tmp_path, bad_vector):
    live = tmp_path / "embeddings.db"
    create_embedding_db(live, rows={})
    shadow = create_shadow_path(live)
    create_embedding_db(shadow, model="new-model", rows={})
    conn = sqlite3.connect(shadow)
    try:
        payload = (
            bad_vector
            if isinstance(bad_vector, str)
            else json.dumps(bad_vector, allow_nan=True)
        )
        conn.execute(
            "INSERT INTO embeddings(bucket_id, embedding, updated_at) VALUES(?,?,?)",
            ("bad", payload, "now"),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises((ShadowValidationError, json.JSONDecodeError)):
        validate_shadow_generation(
            shadow,
            live_db_path=live,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
        )


def test_missing_contract_schema_is_rejected(tmp_path):
    live = tmp_path / "embeddings.db"
    create_embedding_db(live)
    shadow = create_shadow_path(live)
    conn = sqlite3.connect(shadow)
    try:
        conn.execute("CREATE TABLE something_else(value TEXT)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ShadowValidationError, match="missing embeddings"):
        validate_shadow_generation(
            shadow,
            live_db_path=live,
            expected_model="new-model",
            expected_dim=2,
            expected_count=0,
        )


def test_wrong_primary_key_contract_is_rejected(tmp_path):
    live = tmp_path / "embeddings.db"
    create_embedding_db(live)
    shadow = create_shadow_path(live)
    conn = sqlite3.connect(shadow)
    try:
        conn.execute(
            "CREATE TABLE embeddings("
            "bucket_id TEXT, embedding TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE embeddings_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO embeddings_meta VALUES(?,?)",
            [("model_name", "new-model"), ("vector_dim", "2")],
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ShadowValidationError, match="primary key"):
        validate_shadow_generation(
            shadow,
            live_db_path=live,
            expected_model="new-model",
            expected_dim=2,
            expected_count=0,
        )


@pytest.mark.parametrize("suffix", ["-shm", "-journal"])
def test_orphan_or_rollback_sidecar_fails_closed(tmp_path, suffix):
    live, shadow = _valid_pair(tmp_path)
    Path(f"{shadow}{suffix}").write_bytes(b"unexpected")
    with pytest.raises(ShadowValidationError, match="sidecar|journal"):
        validate_shadow_generation(
            shadow,
            live_db_path=live,
            expected_model="new-model",
            expected_dim=2,
            expected_count=2,
        )


def test_wal_sidecar_is_checkpointed_and_cleaned_before_fingerprint(tmp_path):
    live, shadow = _valid_pair(tmp_path)
    # A real migration may close with WAL work pending.  Validation may
    # checkpoint that WAL, but it must publish only a clean main file.
    conn = sqlite3.connect(shadow)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "UPDATE embeddings SET updated_at='later' WHERE bucket_id='a'"
        )
        conn.commit()
    finally:
        conn.close()
    validate_shadow_generation(
        shadow,
        live_db_path=live,
        expected_model="new-model",
        expected_dim=2,
        expected_count=2,
    )
    assert not Path(f"{shadow}-wal").exists()
    assert not Path(f"{shadow}-shm").exists()
    assert not Path(f"{shadow}-journal").exists()


@pytest.mark.parametrize("expected_dim", [0, -1, 2.5, True])
def test_invalid_expected_dimension_is_rejected(tmp_path, expected_dim):
    live, shadow = _valid_pair(tmp_path)
    with pytest.raises(ShadowValidationError):
        validate_shadow_generation(
            shadow,
            live_db_path=live,
            expected_model="new-model",
            expected_dim=expected_dim,
            expected_count=2,
        )
