"""Spawn-safe helpers for the E-MIG-01 acceptance tests.

Every path is supplied by a parent test's ``tmp_path``.  No helper discovers or
opens the production configuration or embedding database.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def create_embedding_db(
    path: str | os.PathLike[str],
    *,
    model: str = "test-model",
    dim: int = 2,
    rows: dict[str, list[float]] | None = None,
    generation: str | None = None,
) -> None:
    """Create a minimal contract-compatible synthetic embedding DB."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    try:
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
        if generation is None:
            match = re.fullmatch(r"([0-9a-f]{32})\.shadow\.db", target.name)
            generation = match.group(1) if match else ""
        metadata = [("model_name", model), ("vector_dim", str(dim))]
        if generation:
            metadata.append(("generation", generation))
        conn.executemany(
            "INSERT INTO embeddings_meta(key, value) VALUES (?, ?)",
            metadata,
        )
        for bucket_id, vector in (rows or {}).items():
            import json

            conn.execute(
                "INSERT INTO embeddings(bucket_id, embedding, updated_at) "
                "VALUES (?, ?, ?)",
                (bucket_id, json.dumps(vector), "2026-07-29T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


def shared_reader_worker(
    db_path: str,
    entered,
    release,
    exited,
) -> None:
    from embedding_publish import embedding_db_turn

    with embedding_db_turn(db_path):
        entered.set()
        release.wait(30)
    exited.set()


def publisher_worker(
    db_path: str,
    entered,
    release,
) -> None:
    from embedding_publish import _embedding_publish_turn

    with _embedding_publish_turn(db_path):
        entered.set()
        release.wait(30)


def late_reader_worker(
    db_path: str,
    entered,
) -> None:
    from embedding_publish import embedding_db_turn

    with embedding_db_turn(db_path):
        entered.set()


def reservation_worker(
    db_path: str,
    entered,
    release,
    result,
    *,
    hard_exit: bool = False,
) -> None:
    from embedding_publish import reserve_migration

    reservation = reserve_migration(db_path)
    result.put(reservation is not None)
    if reservation is None:
        return
    entered.set()
    if hard_exit:
        os._exit(0)
    release.wait(30)
    reservation.close()


def crash_publish_worker(
    db_path: str,
    shadow_path: str,
    crash_after_replace: int,
) -> None:
    """Hard-exit immediately after the requested namespace replacement."""

    import embedding_publish as ep

    original = ep._write_through_replace
    calls = 0

    def crashing_replace(source: Path, target: Path) -> None:
        nonlocal calls
        original(source, target)
        # Count only DB namespace exchange, not durable manifest temp replaces.
        if target.name.endswith(".old.db") or target.name == Path(db_path).name:
            calls += 1
            if calls == crash_after_replace:
                os._exit(70 + calls)

    ep._write_through_replace = crashing_replace
    asyncio.run(
        ep.publish_shadow_generation(
            db_path=db_path,
            shadow_path=shadow_path,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            expected_bucket_ids={"new"},
        )
    )


def timed_shared_reader_worker(
    db_path: str,
    entered,
    release,
    result,
) -> None:
    from embedding_publish import embedding_db_turn

    started = time.monotonic()
    with embedding_db_turn(db_path):
        result.put(time.monotonic() - started)
        entered.set()
        release.wait(30)


def config_publisher_worker(
    config_path: str,
    entered,
    release,
) -> None:
    from embedding_publish import config_yaml_turn

    with config_yaml_turn(config_path, exclusive=True):
        entered.set()
        release.wait(30)
