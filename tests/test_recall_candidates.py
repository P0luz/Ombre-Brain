import json

import httpx
import pytest

from tools.breath.candidates import recall_candidates
from tests.test_breath_call_mode import SearchBucketManager, _bucket, _install


@pytest.mark.asyncio
async def test_shared_automatic_policy_and_field_allowlist(monkeypatch):
    manager = SearchBucketManager([
        _bucket("allowed", "neutral fixture", private_field="do not return"),
        _bucket("muted", "hidden fixture", dont_surface=True),
        _bucket("digested", "hidden fixture", digested=True),
        _bucket("letter", "hidden fixture", type="letter"),
        _bucket("archive", "hidden fixture", type="archived"),
    ])
    _install(monkeypatch, manager)
    payload = await recall_candidates("fixture")
    assert payload["schema_version"] == 1
    assert [candidate["bucket_id"] for candidate in payload["candidates"]] == ["allowed"]
    candidate = payload["candidates"][0]
    assert set(candidate) == {
        "bucket_id", "content", "score", "created_at", "updated_at", "domain", "tags",
        "importance", "valence", "arousal", "policy", "relations",
    }
    assert candidate["content"] == "neutral fixture"
    assert candidate["policy"] == {"silent_context": True, "speakable": False}
    assert candidate["relations"] == {"corrects": [], "supersedes": []}
    assert not manager.touched


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["短正文", "🙂" * 1200])
async def test_query_candidate_and_wire_limits(monkeypatch, body):
    import server

    class Manager(SearchBucketManager):
        async def search(self, query, **kwargs):
            assert query == "词" * 2000
            assert kwargs["include_archive"] is False
            return await super().search(query, **kwargs)

    _install(monkeypatch, Manager([_bucket(str(i), body) for i in range(12)]))
    response = await server.recall_candidates(" \t" + "词" * 2100 + "\n")
    assert not response.isError
    assert response.content == []
    payload = response.structuredContent
    assert 0 < len(payload["candidates"]) <= 6
    if len(body) < 1000:
        assert len(payload["candidates"]) == 6
    assert all(item["content"] == body[:1000] for item in payload["candidates"])
    wire = {"jsonrpc": "2.0", "id": 1, "result": response.model_dump(by_alias=True)}
    assert len(json.dumps(wire, ensure_ascii=True).encode()) <= 24 * 1024


@pytest.mark.asyncio
async def test_blank_skips_retrieval_and_failure_is_redacted(monkeypatch):
    class Manager(SearchBucketManager):
        async def search(self, query, **kwargs):
            assert query == "fixture"
            raise RuntimeError("sensitive upstream detail")

    _install(monkeypatch, Manager([]))
    import server

    empty = await server.recall_candidates(" \t\n")
    assert empty.structuredContent == {"schema_version": 1, "candidates": []}
    failure = await server.recall_candidates("fixture")
    assert failure.isError and failure.content == [] and failure.structuredContent is None


@pytest.mark.asyncio
async def test_authenticated_mcp_wire_response(monkeypatch):
    import server
    from server_app import HTTPRuntimeSettings, RuntimeLifecycle, build_http_app
    from tests.test_server_app import RecordingLogger

    _install(monkeypatch, SearchBucketManager([_bucket("allowed", "wire fixture")]))
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = build_http_app(
        server.mcp, "streamable-http",
        settings=HTTPRuntimeSettings(auth_required=True, max_request_bytes=16384),
        token_validator=lambda token, **kwargs: token == "synthetic-bearer",
        lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
    )
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "recall_candidates", "arguments": {"query": "fixture"}},
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        ) as client:
            assert (await client.post("/mcp", headers=headers, json=request)).status_code == 401
            headers["Authorization"] = "Bearer synthetic-bearer"
            response = await client.post("/mcp", headers=headers, json=request)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["content"] == []
    assert result["structuredContent"]["candidates"][0]["content"] == "wire fixture"
