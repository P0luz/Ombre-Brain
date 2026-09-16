import json
import sqlite3

import pytest

from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror


def _write_embedding_db(path, rows, meta=None):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE embeddings_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        for key, value in (meta or {}).items():
            conn.execute(
                "INSERT INTO embeddings_meta (key, value) VALUES (?, ?)",
                (key, value),
            )
        for bucket_id, embedding in rows:
            conn.execute(
                "INSERT INTO embeddings (bucket_id, embedding, updated_at) VALUES (?, ?, ?)",
                (bucket_id, embedding, "2026-07-03T00:00:00+00:00"),
            )


def test_vector_projection_manifest_reports_embedding_drift_without_mutation(tmp_path):
    from ombrebrain.projection.projection_vector import TraceVectorProjectionManifest

    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="active-ok",
        trace_kind="dynamic",
        payload={"name": "active ok"},
        body="body one",
    )
    ledger.append_event(
        event_type="TraceCreated",
        trace_id="active-missing",
        trace_kind="dynamic",
        payload={"name": "active missing"},
        body="body two",
    )
    ledger.append_event(
        event_type="TraceDeletedToArchive",
        trace_id="gone",
        trace_kind="dynamic",
        payload={
            "name": "gone",
            "deleted_at": "2026-07-03T00:00:00+00:00",
            "tombstone": True,
        },
        body="body three",
    )
    db_path = tmp_path / "embeddings.db"
    _write_embedding_db(
        db_path,
        [
            ("active-ok", json.dumps([0.1, 0.2, 0.3])),
            ("orphan", json.dumps([0.4, 0.5, 0.6])),
            ("bad-vector", "{not json"),
        ],
        meta={"model_name": "bge-m3", "vector_dim": "3"},
    )

    projection = TraceVectorProjectionManifest(db_path)
    report = projection.rebuild(ledger.iter_events())

    assert report["projection_name"] == "trace_vector_manifest"
    assert report["projection_role"] == "shadow"
    assert report["canonical"] is False
    assert report["db_exists"] is True
    assert report["model_name"] == "bge-m3"
    assert report["vector_dim"] == 3
    assert report["expected_trace_count"] == 2
    assert report["vector_count"] == 2
    assert report["missing_vector_count"] == 1
    assert report["orphan_vector_count"] == 2
    assert report["malformed_vector_count"] == 1
    assert report["missing_vector_ids"] == ["active-missing"]
    assert report["orphan_vector_ids"] == ["bad-vector", "orphan"]
    assert report["malformed_vector_ids"] == ["bad-vector"]
    assert report["applied_seq"] == ledger.latest_seq()


def test_vector_projection_reads_embedding_rows_as_a_stream(monkeypatch, tmp_path):
    from ombrebrain.projection import projection_vector as projection_mod

    class StreamingCursor:
        def __init__(self, rows):
            self.rows = rows
            self.iterated = False

        def __iter__(self):
            self.iterated = True
            return iter(self.rows)

        def fetchall(self):
            raise AssertionError("vector projection must not fetchall")

    embedding_cursor = StreamingCursor(
        [
            ("valid", json.dumps([0.1, 0.2])),
            ("malformed", "{not json"),
        ]
    )
    meta_cursor = StreamingCursor(
        [("model_name", "stream-model"), ("vector_dim", "2")]
    )

    class StreamingConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query):
            if "FROM embeddings_meta" in query:
                return meta_cursor
            if "FROM embeddings" in query:
                return embedding_cursor
            raise AssertionError(f"unexpected query: {query}")

    db_path = tmp_path / "streaming.db"
    db_path.touch()
    monkeypatch.setattr(
        projection_mod.sqlite3,
        "connect",
        lambda _path: StreamingConnection(),
    )

    report = projection_mod.TraceVectorProjectionManifest(db_path)._read_db()

    assert embedding_cursor.iterated is True
    assert meta_cursor.iterated is True
    assert report == {
        "db_exists": True,
        "model_name": "stream-model",
        "vector_dim": 2,
        "stored_vector_ids": ["malformed", "valid"],
        "valid_vector_ids": ["valid"],
        "malformed_vector_ids": ["malformed"],
    }


def test_vector_projection_does_not_report_partial_rows_after_cursor_failure(
    monkeypatch,
    tmp_path,
):
    from ombrebrain.projection import projection_vector as projection_mod

    class FailingCursor:
        def __iter__(self):
            yield "first", json.dumps([0.1])
            raise sqlite3.OperationalError("cursor read failed")

    class FailingConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query):
            assert "FROM embeddings" in query
            return FailingCursor()

    db_path = tmp_path / "failing-stream.db"
    db_path.touch()
    monkeypatch.setattr(
        projection_mod.sqlite3,
        "connect",
        lambda _path: FailingConnection(),
    )

    with pytest.raises(sqlite3.OperationalError, match="cursor read failed"):
        projection_mod.TraceVectorProjectionManifest(db_path)._read_db()


def test_bucket_manager_ledger_report_includes_vector_projection(
    test_config,
    fake_embedding_engine,
):
    import asyncio

    from bucket_manager import BucketManager

    async def scenario():
        manager = BucketManager(test_config, embedding_engine=fake_embedding_engine)
        bucket_id = await manager.create_internal("vector projection source", domain=["vector"])
        return manager, bucket_id, manager.ledger_integrity_report()

    manager, bucket_id, report = asyncio.run(scenario())
    projection = report["vector_projection"]

    assert projection["projection_name"] == "trace_vector_manifest"
    assert projection["projection_role"] == "shadow"
    assert projection["canonical"] is False
    assert projection["path"].endswith("embeddings.db")
    assert projection["expected_trace_count"] == 1
    assert projection["applied_seq"] == report["latest_seq"]
    assert projection["source_latest_seq"] == report["latest_seq"]
    assert projection["lag"] == 0
    assert bucket_id in projection["missing_vector_ids"]
    assert str(manager.base_dir) in projection["path"]
