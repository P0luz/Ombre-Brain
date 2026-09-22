from unittest.mock import MagicMock

import pytest

from tools import _relation_link
from tools import _runtime as rt


def _bucket(bucket_id, *, owner="cheng", bucket_type="dynamic", created=""):
    metadata = {"tags": [f"owner:{owner}"], "type": bucket_type}
    if created:
        metadata["created"] = created
    return {"id": bucket_id, "content": bucket_id, "metadata": metadata}


class Manager:
    def __init__(self, buckets):
        self.buckets = {bucket["id"]: bucket for bucket in buckets}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class Engine:
    enabled = True

    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = []

    async def search_similar(self, content, *, top_k):
        self.calls.append((content, top_k))
        return self.pairs


@pytest.mark.asyncio
@pytest.mark.parametrize(("bucket_id", "content"), [("", "body"), ("source", "")])
async def test_infer_links_rejects_blank_identity_or_content_without_searching(
    monkeypatch,
    bucket_id,
    content,
):
    engine = Engine([("target", 0.99)])
    monkeypatch.setattr(rt, "embedding_engine", engine)

    assert await _relation_link.infer_links_for(bucket_id, content) == []
    assert engine.calls == []


@pytest.mark.asyncio
async def test_infer_links_returns_empty_when_embedding_is_unavailable(monkeypatch):
    monkeypatch.setattr(rt, "embedding_engine", None)

    assert await _relation_link.infer_links_for("source", "body") == []


@pytest.mark.asyncio
async def test_infer_links_filters_candidates_and_classifies_time_relationships(monkeypatch):
    source = _bucket("source", created="2026-09-12T10:00:00Z")
    same_event = _bucket("same", created="2026-09-12T12:00:00Z")
    continuation = _bucket("continuation", created="2026-09-13T10:00:00Z")
    related = _bucket("related")
    excluded = _bucket("plan", bucket_type="plan")
    foreign = _bucket("foreign", owner="huaiyin")
    manager = Manager([source, same_event, continuation, related, excluded, foreign])
    engine = Engine(
        [
            ("source", 1.0),
            ("", 0.99),
            ("below-threshold", 0.71),
            ("missing", 0.99),
            ("plan", 0.99),
            ("foreign", 0.99),
            ("same", 0.90),
            ("continuation", 0.80),
            ("related", 0.73),
        ]
    )
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "embedding_engine", engine)

    links = await _relation_link.infer_links_for("source", "body")

    assert engine.calls == [("body", _relation_link._SEARCH_TOP_K)]
    assert links == [
        {
            "target_bucket_id": "same",
            "type": "same_event",
            "label": "",
            "status": "active",
            "auto": True,
            "score": 0.9,
        },
        {
            "target_bucket_id": "continuation",
            "type": "continuation_of",
            "label": "",
            "status": "active",
            "auto": True,
            "score": 0.8,
        },
        {
            "target_bucket_id": "related",
            "type": "related_to",
            "label": "",
            "status": "active",
            "auto": True,
            "score": 0.73,
        },
    ]


@pytest.mark.asyncio
async def test_infer_links_stops_at_automatic_link_budget(monkeypatch):
    source = _bucket("source")
    targets = [_bucket(f"target-{index}") for index in range(12)]
    monkeypatch.setattr(rt, "bucket_mgr", Manager([source, *targets]))
    monkeypatch.setattr(
        rt,
        "embedding_engine",
        Engine([(target["id"], 0.9) for target in targets]),
    )

    links = await _relation_link.infer_links_for("source", "body")

    assert len(links) == _relation_link.AUTO_MAX_LINKS_PER_BUCKET
    assert [link["target_bucket_id"] for link in links] == [
        f"target-{index}" for index in range(_relation_link.AUTO_MAX_LINKS_PER_BUCKET)
    ]


@pytest.mark.asyncio
async def test_link_new_bucket_contains_inference_failures(monkeypatch):
    async def fail_inference(_bucket_id, _content):
        raise RuntimeError("embedding offline")

    logger = MagicMock()
    monkeypatch.setattr(_relation_link, "infer_links_for", fail_inference)
    monkeypatch.setattr(rt, "logger", logger)

    assert await _relation_link.link_new_bucket("source", "body") == 0
    logger.warning.assert_called_once()
    assert "embedding offline" in logger.warning.call_args.args[0]
