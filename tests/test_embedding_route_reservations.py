import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import embedding_engine as embedding_engine_module
import migration_engine
import utils
from web import embedding as embedding_web


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    def __init__(self, body):
        self.body = body
        self.json_calls = 0
        self.headers = {}
        self.query_params = {}

    async def json(self):
        self.json_calls += 1
        return self.body


class NeverReadRequest(JsonRequest):
    async def json(self):
        self.json_calls += 1
        pytest.fail("a losing request must be rejected before its first await")


def response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_persist_embedding_yaml_propagates_atomic_write_failure(monkeypatch):
    def fail_write(_mutate):
        raise OSError("synthetic read-only config")

    monkeypatch.setattr(utils, "atomic_update_config_yaml", fail_write)

    with pytest.raises(OSError, match="synthetic read-only config"):
        embedding_web._persist_embedding_yaml({"backend": "ollama", "enabled": True})


@pytest.fixture(autouse=True)
def reset_process_reservations():
    migration_engine.reset_for_test()
    owner = embedding_web._ollama_pull_owner
    if owner is not None:
        embedding_web._release_ollama_pull(owner)
    embedding_web._ollama_pull_state = {
        "running": False,
        "model": "",
        "percent": 0,
        "status": "idle",
        "error": "",
    }
    embedding_web._ollama_pull_task = None
    yield
    migration_engine.reset_for_test()
    owner = embedding_web._ollama_pull_owner
    if owner is not None:
        embedding_web._release_ollama_pull(owner)


@pytest.mark.asyncio
async def test_embedding_migration_reserves_before_staging_and_provider_await(
    monkeypatch, tmp_path
):
    probe_entered = asyncio.Event()
    allow_probe = asyncio.Event()
    calls = {"construct": 0, "reset": 0, "stop": 0, "start": 0, "swap": 0}
    constructed_configs = []
    persisted_updates = []

    live_db = tmp_path / "embeddings.db"
    live_db.write_bytes(b"old-live")
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()

    class Backend:
        @staticmethod
        def model_name():
            return "reserved-model"

        @staticmethod
        def vector_dim():
            return 3

    class TargetEngine:
        enabled = True

        def __init__(self, config):
            calls["construct"] += 1
            constructed_configs.append(
                json.loads(json.dumps(config["embedding"]))
            )
            self.db_path = config["embedding"]["db_path"]
            self._backend = Backend()

        def _init_db(self):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.db_path).touch()

        async def _generate_async(self, _text):
            probe_entered.set()
            await allow_probe.wait()
            return [0.1, 0.2, 0.3]

        def _write_meta(self, _key, _value):
            return None

    class BucketManager:
        async def list_all(self, *, include_archive):
            assert include_archive is True
            return []

    class Outbox:
        running = True

        async def stop(self):
            calls["stop"] += 1
            self.running = False

        async def start(self, *, reconcile):
            assert reconcile is True
            calls["start"] += 1
            self.running = True

    def fake_reset(*_args):
        calls["reset"] += 1

    monkeypatch.setattr(embedding_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        embedding_web.sh,
        "_read_json_object",
        lambda request: request.json(),
    )
    monkeypatch.setattr(
        embedding_web.sh,
        "config",
        {
            "buckets_dir": str(buckets_dir),
            "embedding": {
                "enabled": True,
                "backend": "api",
                "api_key": "siliconflow-secret",
                "api_format": "openai_compat",
                "base_url": "https://api.siliconflow.cn/v1",
                "model": "BAAI/bge-m3",
            },
        },
    )
    monkeypatch.setattr(
        embedding_web.sh,
        "embedding_engine",
        SimpleNamespace(db_path=str(live_db)),
    )
    monkeypatch.setattr(embedding_web.sh, "bucket_mgr", BucketManager())
    monkeypatch.setattr(embedding_web.sh, "embedding_outbox", Outbox())
    monkeypatch.setattr(
        embedding_web.sh,
        "replace_embedding_engine",
        lambda _engine: calls.__setitem__("swap", calls["swap"] + 1),
    )
    monkeypatch.setattr(
        embedding_web,
        "_persist_embedding_yaml",
        lambda updates: persisted_updates.append(dict(updates)),
    )
    monkeypatch.setattr(embedding_engine_module, "EmbeddingEngine", TargetEngine)
    monkeypatch.setattr(migration_engine, "reset_stale_migration_state", fake_reset)

    mcp = FakeMCP()
    embedding_web.register(mcp)
    handler = mcp.routes[("POST", "/api/embedding/migrate")]

    first_request = JsonRequest(
        {
            "target_backend": "ollama",
            "api_format": "ollama",
            "base_url": "",
            "model": "bge-m3",
        }
    )
    first_task = asyncio.create_task(handler(first_request))
    await asyncio.wait_for(probe_entered.wait(), timeout=2)

    assert constructed_configs[0]["api_format"] == "ollama"
    assert constructed_configs[0]["base_url"] == ""
    assert constructed_configs[0]["model"] == "bge-m3"

    losing_request = NeverReadRequest({"target_backend": "api"})
    losing_response = await handler(losing_request)

    assert losing_response.status_code == 409
    assert losing_request.json_calls == 0
    assert calls == {
        "construct": 1,
        "reset": 0,
        "stop": 0,
        "start": 0,
        "swap": 0,
    }

    allow_probe.set()
    first_response = await asyncio.wait_for(first_task, timeout=2)
    assert first_response.status_code == 202
    payload = response_json(first_response)
    assert payload["ok"] is True
    assert payload["accepted"] is True
    assert payload["completed"] is False
    assert payload["status_path"].endswith("_pending_migration_status.json")

    assert migration_engine._migration_task is not None
    await asyncio.wait_for(migration_engine._migration_task, timeout=2)
    await asyncio.sleep(0)

    # E-MIG-01 coordinates the outbox through the shared DB gate and only
    # publishes runtime/config after a validated private SQLite generation.
    # This deliberately minimal fake cannot publish, so none of the legacy
    # stop/swap/persist hooks may run.
    assert calls["stop"] == 0
    assert calls["start"] == 0
    assert calls["swap"] == 0
    assert persisted_updates == []
    assert migration_engine.is_running() is False


@pytest.mark.asyncio
async def test_cancelled_migration_releases_owner_and_runs_cleanup_callback(tmp_path):
    fetch_started = asyncio.Event()
    never_finish = asyncio.Event()
    completions = []

    async def fetch_buckets():
        fetch_started.set()
        await never_finish.wait()
        return []

    cfg = migration_engine.MigrationConfig(
        buckets_dir=str(tmp_path / "buckets"),
        db_path=str(tmp_path / "embeddings.db"),
        target_backend="api",
        target_model="model",
        target_dim=3,
        target_engine=SimpleNamespace(),
        fetch_buckets=fetch_buckets,
    )
    reservation = migration_engine.reserve_migration(cfg.db_path)
    assert reservation is not None
    shadow_path = migration_engine.create_shadow_path(
        cfg.db_path,
        reservation=reservation,
    )
    cfg.target_engine = SimpleNamespace(
        db_path=str(shadow_path),
        list_all_ids=lambda: [],
    )
    task = migration_engine.start_migration(
        cfg,
        on_complete=completions.append,
        reservation=reservation,
    )
    assert task is not None
    await asyncio.wait_for(fetch_started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert completions == [False]
    assert migration_engine.is_running() is False
    next_reservation = migration_engine.reserve_migration(cfg.db_path)
    assert next_reservation is not None
    next_reservation.close()


@pytest.mark.asyncio
async def test_ollama_pull_reserves_before_body_and_connectivity_await(monkeypatch):
    version_probe_entered = asyncio.Event()
    allow_version_probe = asyncio.Event()
    calls = {"get": 0, "stream": 0}

    class VersionResponse:
        @staticmethod
        def raise_for_status():
            return None

    class PullResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield '{"status":"success"}'

    class Client:
        def __init__(self, **_kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            calls["get"] += 1
            version_probe_entered.set()
            await allow_version_probe.wait()
            return VersionResponse()

        def stream(self, _method, _url, *, json):
            assert json == {"name": "bge-m3", "stream": True}
            calls["stream"] += 1
            return PullResponse()

    monkeypatch.setattr(embedding_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(embedding_web, "_ollama_base", lambda: "http://ollama.test")
    monkeypatch.setattr(embedding_web.httpx, "AsyncClient", Client)

    mcp = FakeMCP()
    embedding_web.register(mcp)
    handler = mcp.routes[("POST", "/api/embedding/local/pull")]

    first_request = JsonRequest({"model": "bge-m3"})
    first_task = asyncio.create_task(handler(first_request))
    await asyncio.wait_for(version_probe_entered.wait(), timeout=2)

    losing_request = NeverReadRequest({"model": "other-model"})
    losing_response = await handler(losing_request)
    assert losing_response.status_code == 409
    assert losing_request.json_calls == 0
    assert calls == {"get": 1, "stream": 0}
    assert embedding_web._ollama_pull_state["running"] is True
    assert embedding_web._ollama_pull_state["status"] == "checking"

    allow_version_probe.set()
    first_response = await asyncio.wait_for(first_task, timeout=2)
    assert first_response.status_code == 200
    assert response_json(first_response) == {
        "ok": True,
        "started": True,
        "pulling": "bge-m3",
    }

    assert embedding_web._ollama_pull_task is not None
    await asyncio.wait_for(embedding_web._ollama_pull_task, timeout=2)
    assert calls == {"get": 1, "stream": 1}
    assert embedding_web._ollama_pull_state["status"] == "success"
    assert embedding_web._ollama_pull_state["running"] is False
    assert embedding_web._ollama_pull_owner is None
