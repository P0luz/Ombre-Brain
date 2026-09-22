"""Lease lifetime tests for ordinary provider/SQLite/config operations."""

from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path
import sys

import pytest
import yaml

from embedding_engine import EmbeddingEngine
from embedding_publish import (
    config_mutation_guard,
    create_shadow_path,
    embedding_db_turn,
    publish_shadow_generation,
)

_HELPERS = Path(__file__).resolve().parent / "helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from emig01_process_worker import (
    config_publisher_worker,
    create_embedding_db,
    publisher_worker,
)


class _BlockingBackend:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event):
        self.entered = entered
        self.release = release

    def model_name(self):
        return "test-model"

    def vector_dim(self):
        return 2

    async def generate_async(self, _text):
        self.entered.set()
        await self.release.wait()
        return [0.1, 0.2]


def _disabled_engine(tmp_path: Path) -> EmbeddingEngine:
    return EmbeddingEngine(
        {
            "buckets_dir": str(tmp_path),
            "embedding": {
                "enabled": False,
                "db_path": str(tmp_path / "embeddings.db"),
            },
        }
    )


@pytest.mark.asyncio
async def test_provider_through_sqlite_commit_holds_live_shared_lease(tmp_path):
    engine = _disabled_engine(tmp_path)
    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()
    engine._backend = _BlockingBackend(provider_entered, provider_release)
    engine.enabled = True

    operation = asyncio.create_task(
        engine.generate_and_store("bucket-1", "synthetic content")
    )
    await asyncio.wait_for(provider_entered.wait(), timeout=2)

    ctx = multiprocessing.get_context("spawn")
    publish_entered = ctx.Event()
    publish_release = ctx.Event()
    publisher = ctx.Process(
        target=publisher_worker,
        args=(engine.db_path, publish_entered, publish_release),
    )
    publisher.start()
    try:
        assert not publish_entered.wait(0.4), (
            "publisher entered while provider/SQLite operation was live"
        )
        provider_release.set()
        assert await asyncio.wait_for(operation, timeout=3) is True
        assert publish_entered.wait(20)
    finally:
        provider_release.set()
        publish_release.set()
        publisher.join(timeout=5)
        if publisher.is_alive():
            publisher.terminate()
            publisher.join(timeout=3)
    assert publisher.exitcode == 0
    assert await engine.get_embedding("bucket-1") == pytest.approx([0.1, 0.2])


@pytest.mark.asyncio
async def test_provider_exception_releases_live_lease(tmp_path):
    engine = _disabled_engine(tmp_path)

    class FailingBackend:
        def model_name(self):
            return "test-model"

        def vector_dim(self):
            return 2

        async def generate_async(self, _text):
            raise RuntimeError("provider injected")

    engine._backend = FailingBackend()
    engine.enabled = True
    assert await engine.generate_and_store("bucket-1", "content") is False

    # A later operation must not hang behind a leaked shared lease.
    assert engine.list_all_ids() == []


@pytest.mark.asyncio
async def test_config_mutation_guard_blocks_exclusive_publish_until_body_finishes(
    tmp_path,
):
    config = tmp_path / "config.yaml"
    config.write_text("embedding: {}\n", encoding="utf-8")
    entered = asyncio.Event()
    release = asyncio.Event()

    @config_mutation_guard(path_getter=lambda: config)
    async def writer():
        entered.set()
        await release.wait()

    task = asyncio.create_task(writer())
    await asyncio.wait_for(entered.wait(), timeout=2)

    ctx = multiprocessing.get_context("spawn")
    exclusive_entered = ctx.Event()
    exclusive_release = ctx.Event()
    process = ctx.Process(
        target=config_publisher_worker,
        args=(str(config), exclusive_entered, exclusive_release),
    )
    process.start()
    try:
        assert not exclusive_entered.wait(0.4)
        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert exclusive_entered.wait(20)
    finally:
        release.set()
        exclusive_release.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=3)
    assert process.exitcode == 0


@pytest.mark.asyncio
async def test_config_then_embedding_lock_order_avoids_writer_publisher_deadlock(
    tmp_path,
):
    db = tmp_path / "embeddings.db"
    create_embedding_db(db, model="old-model", rows={"old": [0.0, 1.0]})
    shadow = create_shadow_path(db)
    create_embedding_db(
        shadow,
        model="new-model",
        rows={"new": [1.0, 0.0]},
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"embedding": {"model": "old-model", "dim": 2}}),
        encoding="utf-8",
    )
    config_writer_entered = asyncio.Event()
    let_writer_take_embedding = asyncio.Event()

    @config_mutation_guard(path_getter=lambda: config)
    async def ordinary_config_writer():
        config_writer_entered.set()
        await let_writer_take_embedding.wait()
        # Models a config handler that constructs/probes an engine while it
        # still holds its shared config lease.
        with embedding_db_turn(db):
            await asyncio.sleep(0)

    writer = asyncio.create_task(ordinary_config_writer())
    await asyncio.wait_for(config_writer_entered.wait(), timeout=2)
    publisher = asyncio.create_task(
        publish_shadow_generation(
            db_path=db,
            shadow_path=shadow,
            expected_model="new-model",
            expected_dim=2,
            expected_count=1,
            config_path=config,
            config_forward_patch={"model": "new-model", "dim": 2},
        )
    )
    await asyncio.sleep(0.1)
    let_writer_take_embedding.set()
    await asyncio.wait_for(writer, timeout=3)
    manifest = await asyncio.wait_for(publisher, timeout=10)
    assert manifest["state"] == "COMMITTED"
    persisted = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert persisted["embedding"]["model"] == "new-model"
