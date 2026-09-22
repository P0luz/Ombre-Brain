"""Conversation-import owner routing and provider failure boundary."""

from __future__ import annotations

import hashlib
import logging

import pytest

from bucket_manager import BucketManager
from import_memory import ImportEngine
from tools import _runtime as rt
from ombrebrain.eventsourcing.footprint import import_origin


class FakeDehydrator:
    api_available = True

    async def merge(self, *_args):
        raise AssertionError("fuzzy merge must not run in raw exact-only mode")


class FailingEmbedding:
    enabled = True

    async def generate_and_store(self, *_args):
        raise RuntimeError("provider unavailable")

    async def get_embedding(self, *_args):
        return None

    async def search_similar(self, *_args, **_kwargs):
        return []

    def delete_embedding(self, *_args):
        return None


class SuccessfulEmbedding(FailingEmbedding):
    async def generate_and_store(self, *_args):
        return True


def _config(tmp_path):
    return {
        "buckets_dir": str(tmp_path),
        "merge_threshold": 50,
        "matching": {"fuzzy_threshold": 1, "max_results": 50},
        "wikilink": {"enabled": False},
        "limits": {"max_pinned": 20},
        "scoring_weights": {},
        "embedding": {"enabled": True},
    }


def _wire_runtime(monkeypatch, config, manager, embedding, dehydrator):
    monkeypatch.setattr(rt, "config", config, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(rt, "embedding_engine", embedding, raising=False)
    monkeypatch.setattr(rt, "dehydrator", dehydrator, raising=False)
    monkeypatch.setattr(rt, "logger", logging.getLogger("m03-owner"), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["", "unknown-owner"])
async def test_import_start_requires_explicit_known_owner(tmp_path, owner):
    engine = ImportEngine(
        _config(tmp_path),
        bucket_mgr=object(),
        dehydrator=FakeDehydrator(),
    )
    result = await engine.start("content", owner=owner)
    assert "error" in result
    assert "explicit valid owner" in result["error"]


@pytest.mark.asyncio
async def test_resume_rejects_owner_different_from_original_job(tmp_path):
    raw = "same source"
    engine = ImportEngine(
        _config(tmp_path),
        bucket_mgr=object(),
        dehydrator=FakeDehydrator(),
    )
    engine.state.data = {
        "source_file": "chat.txt",
        "source_hash": hashlib.sha256(raw.encode()).hexdigest()[:16],
        "total_chunks": 1,
        "processed": 0,
        "status": "paused",
        "owner": "cheng",
        "errors": [],
    }
    engine.state.save()
    result = await engine.start(
        raw,
        resume=True,
        owner="huaiyin",
        footprint_origin=import_origin("mcp_tool", "huaiyin"),
    )
    assert "owner differs" in result["error"]
    assert engine.is_running is False


@pytest.mark.asyncio
async def test_raw_import_is_exact_only_and_never_fuzzy_merges(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    embedding = SuccessfulEmbedding()
    dehydrator = FakeDehydrator()
    manager = BucketManager(config, embedding_engine=embedding)
    _wire_runtime(monkeypatch, config, manager, embedding, dehydrator)
    await manager.create_internal(
        content="nearly identical memory",
        tags=["owner:cheng"],
        domain=["m03"],
        importance=5,
    )
    failing = FailingEmbedding()
    manager.embedding_engine = failing
    monkeypatch.setattr(rt, "embedding_engine", failing, raising=False)
    engine = ImportEngine(config, manager, dehydrator, embedding)
    engine._job_owner = "cheng"
    engine._job_origin = import_origin("mcp_tool", "cheng")
    engine.state.data.update(
        {"api_calls": 0, "memories_raw": 0, "memories_created": 0, "memories_merged": 0, "errors": []}
    )

    async def extracted(_content):
        return [
            {
                "name": "raw",
                "content": "nearly identical memory!",
                "domain": ["m03"],
                "tags": [],
                "importance": 5,
                "preserve_raw": True,
            }
        ]

    monkeypatch.setattr(engine, "_extract_memories", extracted)
    await engine._process_single_chunk({"content": "chunk"}, preserve_raw=False)
    rows = await manager.list_all(include_archive=False, fresh=True)
    assert len(rows) == 2
    assert all("owner:cheng" in row["metadata"]["tags"] for row in rows)


@pytest.mark.asyncio
async def test_cross_owner_identical_import_content_never_merges(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    embedding = FailingEmbedding()
    dehydrator = FakeDehydrator()
    manager = BucketManager(config, embedding_engine=embedding)
    _wire_runtime(monkeypatch, config, manager, embedding, dehydrator)
    content = "same imported body across two explicit owners"
    for owner in ("cheng", "huaiyin"):
        engine = ImportEngine(config, manager, dehydrator, embedding)
        engine._job_owner = owner
        engine._job_origin = import_origin("mcp_tool", owner)
        merged = await engine._merge_or_create_item(
            {
                "name": owner,
                "content": content,
                "domain": ["m03"],
                "tags": [],
                "importance": 5,
                "valence": 0.5,
                "arousal": 0.3,
            }
        )
        assert merged is False
    rows = [
        row
        for row in await manager.list_all(include_archive=False, fresh=True)
        if row["content"] == content
    ]
    assert len(rows) == 2
    owners = {
        tag
        for row in rows
        for tag in row["metadata"]["tags"]
        if tag.startswith("owner:")
    }
    assert owners == {"owner:cheng", "owner:huaiyin"}


@pytest.mark.asyncio
async def test_provider_failure_after_markdown_commit_never_loses_body(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    embedding = FailingEmbedding()
    dehydrator = FakeDehydrator()
    manager = BucketManager(config, embedding_engine=embedding)
    _wire_runtime(monkeypatch, config, manager, embedding, dehydrator)
    engine = ImportEngine(config, manager, dehydrator, embedding)
    engine._job_owner = "cheng"
    engine._job_origin = import_origin("mcp_tool", "cheng")
    content = "authoritative imported Markdown survives provider failure"
    merged = await engine._merge_or_create_item(
        {
            "name": "provider failure",
            "content": content,
            "domain": ["m03"],
            "tags": [],
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
        }
    )
    assert merged is False
    rows = await manager.list_all(include_archive=False, fresh=True)
    assert any(row["content"] == content for row in rows)
