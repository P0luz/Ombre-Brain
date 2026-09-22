"""Concurrency regressions for the historical conversation import start path."""

import asyncio
import json
import threading

import pytest

import import_memory as import_memory_module
from import_memory import ImportEngine
from ombrebrain.eventsourcing.footprint import import_origin
from web import import_api


def _trusted_import_kwargs() -> dict:
    return {
        "owner": "cheng",
        "footprint_origin": import_origin("mcp_tool", "cheng"),
    }


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class BlockingDehydrator:
    api_available = True

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def _chat(self, *_args, **_kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return "[]"


class ImmediateDehydrator:
    api_available = True

    async def _chat(self, *_args, **_kwargs):
        return "[]"


class BodyRequest:
    headers = {}

    def __init__(self, body: str, filename: str = "upload.md"):
        self._body = body.encode("utf-8")
        self.query_params = {"filename": filename, "owner": "cheng"}

    async def body(self):
        return self._body


def _payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


async def _wait_until_finished(engine: ImportEngine) -> None:
    async def wait_loop():
        while engine.is_running:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_loop(), timeout=2)


@pytest.mark.asyncio
async def test_import_engine_concurrent_starts_have_one_owner(tmp_path):
    dehydrator = BlockingDehydrator()
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "human": "用户"},
        object(),
        dehydrator,
    )
    raw = "Human: first request\nAssistant: acknowledged"

    first_task = asyncio.create_task(
        engine.start(raw, filename="first.md", **_trusted_import_kwargs())
    )
    await asyncio.wait_for(dehydrator.entered.wait(), timeout=2)
    first_job_id = engine.active_job_id

    rejected = await engine.start(
        raw, filename="second.md", **_trusted_import_kwargs()
    )

    assert rejected == {
        "error": "Import already running",
        "job_id": first_job_id,
    }
    assert engine.is_running is True

    dehydrator.release.set()
    completed = await asyncio.wait_for(first_task, timeout=2)
    assert completed["status"] == "completed"
    assert completed["job_id"] == first_job_id
    assert dehydrator.calls == 1
    assert engine.is_running is False
    assert engine.active_job_id == ""
    assert engine._chunks == []


@pytest.mark.asyncio
async def test_upload_toctou_returns_one_started_and_one_409(tmp_path, monkeypatch):
    dehydrator = BlockingDehydrator()
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "human": "用户"},
        object(),
        dehydrator,
    )
    monkeypatch.setattr(import_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_api.sh, "import_engine", engine, raising=False)

    mcp = FakeMCP()
    import_api.register(mcp)
    upload = mcp.routes[("POST", "/api/import/upload")]
    body_entered = asyncio.Event()
    release_body = asyncio.Event()

    class BlockingBodyRequest(BodyRequest):
        async def body(self):
            body_entered.set()
            await release_body.wait()
            return self._body

    class MustNotReadRequest(BodyRequest):
        async def body(self):
            raise AssertionError("losing upload must be rejected before reading its body")

    first_request = BlockingBodyRequest(
        "Human: alpha\nAssistant: one",
        "alpha.md",
    )
    second_request = MustNotReadRequest(
        "Human: beta\nAssistant: two",
        "beta.md",
    )

    first_task = asyncio.create_task(upload(first_request))
    await asyncio.wait_for(body_entered.wait(), timeout=2)
    assert engine.is_running is True
    rejected = await upload(second_request)
    release_body.set()
    started = await asyncio.wait_for(first_task, timeout=2)
    started_payload = _payload(started)
    rejected_payload = _payload(rejected)

    assert started_payload["status"] == "started"
    assert started_payload["job_id"]
    assert rejected.status_code == 409
    assert rejected_payload["job_id"] == started_payload["job_id"]
    assert "active" in rejected_payload["error"].lower()

    await asyncio.wait_for(dehydrator.entered.wait(), timeout=2)
    assert dehydrator.calls == 1
    dehydrator.release.set()
    await _wait_until_finished(engine)

    status = engine.get_status()
    assert status["status"] == "completed"
    assert status["job_id"] == started_payload["job_id"]
    assert status["source_file"] == started_payload["filename"]
    assert status["source_file"] == first_request.query_params["filename"]
    assert status["source_file"] != second_request.query_params["filename"]


@pytest.mark.asyncio
async def test_start_exception_releases_reservation_for_next_job(tmp_path, monkeypatch):
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "human": "用户"},
        object(),
        ImmediateDehydrator(),
    )
    original_detect = import_memory_module.detect_and_parse

    def fail_parse(*_args, **_kwargs):
        raise RuntimeError("synthetic parse failure")

    monkeypatch.setattr(import_memory_module, "detect_and_parse", fail_parse)
    with pytest.raises(RuntimeError, match="synthetic parse failure"):
        await engine.start(
            "Human: broken",
            filename="broken.md",
            **_trusted_import_kwargs(),
        )

    assert engine.is_running is False
    assert engine.active_job_id == ""
    assert engine.get_status()["status"] == "error"
    assert engine._chunks == []

    monkeypatch.setattr(import_memory_module, "detect_and_parse", original_detect)
    completed = await engine.start(
        "Human: recovered\nAssistant: ready",
        filename="recovered.md",
        **_trusted_import_kwargs(),
    )
    assert completed["status"] == "completed"
    assert engine.is_running is False


@pytest.mark.asyncio
async def test_pause_then_resume_uses_a_new_reservation(tmp_path, monkeypatch):
    chunks = [
        {
            "content": "[用户] chunk one",
            "timestamp_start": "",
            "timestamp_end": "",
            "turn_count": 1,
        },
        {
            "content": "[用户] chunk two",
            "timestamp_start": "",
            "timestamp_end": "",
            "turn_count": 1,
        },
    ]
    monkeypatch.setattr(
        import_memory_module,
        "chunk_turns",
        lambda *_args, **_kwargs: list(chunks),
    )
    dehydrator = BlockingDehydrator()
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "human": "用户"},
        object(),
        dehydrator,
    )
    raw = "Human: pause me\nAssistant: okay"

    first_task = asyncio.create_task(
        engine.start(raw, filename="pause.md", **_trusted_import_kwargs())
    )
    await asyncio.wait_for(dehydrator.entered.wait(), timeout=2)
    first_job_id = engine.active_job_id
    engine.pause()
    dehydrator.release.set()
    paused = await asyncio.wait_for(first_task, timeout=2)

    assert paused["status"] == "paused"
    assert paused["processed"] == 1
    assert paused["job_id"] == first_job_id
    assert engine.is_running is False
    assert engine._chunks == chunks

    resumed = await engine.start(
        raw,
        filename="pause.md",
        resume=True,
        **_trusted_import_kwargs(),
    )
    assert resumed["status"] == "completed"
    assert resumed["processed"] == 2
    assert resumed["job_id"] != first_job_id
    assert engine.is_running is False
    assert engine._chunks == []


@pytest.mark.asyncio
async def test_upload_schedule_failure_releases_reservation(tmp_path, monkeypatch):
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "human": "用户"},
        object(),
        ImmediateDehydrator(),
    )
    monkeypatch.setattr(import_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_api.sh, "import_engine", engine, raising=False)
    monkeypatch.setattr(
        import_api,
        "spawn_background",
        lambda _coro: (_ for _ in ()).throw(RuntimeError("scheduler unavailable")),
    )
    mcp = FakeMCP()
    import_api.register(mcp)

    response = await mcp.routes[("POST", "/api/import/upload")](
        BodyRequest("Human: schedule\nAssistant: failure")
    )

    assert response.status_code == 500
    assert _payload(response)["error"] == "Failed to schedule import"
    assert engine.is_running is False
    assert engine.active_job_id == ""

    # The cross-route heavy-work admission must also be released, otherwise a
    # scheduler failure leaves every later upload stuck at 409 forever.
    response_again = await mcp.routes[("POST", "/api/import/upload")](
        BodyRequest("Human: retry\nAssistant: failure")
    )
    assert response_again.status_code == 500


def test_history_import_limit_is_safe_by_default_and_hard_capped(monkeypatch):
    monkeypatch.setattr(import_api.sh, "config", {}, raising=False)
    assert import_api._max_import_upload_bytes() == 4 * 1024 * 1024

    monkeypatch.setattr(
        import_api.sh,
        "config",
        {"limits": {"max_import_upload_bytes": 512 * 1024 * 1024}},
        raising=False,
    )
    assert import_api._max_import_upload_bytes() == 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_cancelled_parser_is_reaped_and_persisted_as_error(tmp_path, monkeypatch):
    engine = ImportEngine(
        {"buckets_dir": str(tmp_path), "human": "用户"},
        object(),
        ImmediateDehydrator(),
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_prepare(*_args):
        entered.set()
        release.wait(timeout=2)
        return (
            "abcd1234",
            1,
            [{"content": "[用户] safe", "timestamp_start": "", "timestamp_end": ""}],
        )

    monkeypatch.setattr(import_memory_module, "_prepare_import", blocking_prepare)
    task = asyncio.create_task(
        engine.start(
            "Human: wait",
            filename="wait.md",
            **_trusted_import_kwargs(),
        )
    )
    while not entered.is_set():
        await asyncio.sleep(0)

    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    assert engine.is_running is True

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert engine.is_running is False
    assert engine.active_job_id == ""
    assert engine.get_status()["status"] == "error"
    assert "cancelled" in engine.get_status()["errors"][-1].lower()
    assert engine._chunks == []
