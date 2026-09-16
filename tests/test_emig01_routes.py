"""HTTP contract tests for E-MIG-01 migration acceptance and terminal status."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import embedding_engine as embedding_engine_module
import migration_engine
import utils
from web import _shared as sh
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


class FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


class FakeBackend:
    def model_name(self):
        return "new-model"

    def vector_dim(self):
        return 2


class FakeTargetEngine:
    instances = []
    probe = [0.1, 0.2]

    def __init__(self, config):
        self.config = config
        self.db_path = config["embedding"]["db_path"]
        self._backend = FakeBackend()
        self.enabled = True
        self.__class__.instances.append(self)

    async def _generate_async(self, _content):
        return list(self.__class__.probe)


def _json(response):
    return json.loads(response.body.decode("utf-8"))


def _setup_route(monkeypatch, tmp_path):
    live = tmp_path / "embeddings.db"
    live.write_bytes(b"synthetic-live-generation")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "embedding:\n  enabled: true\n  model: old-model\n  dim: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        sh,
        "config",
        {
            "buckets_dir": str(tmp_path / "vault"),
            "embedding": {
                "enabled": True,
                "api_key": "test-key",
                "model": "old-model",
                "dim": 2,
            },
        },
    )
    monkeypatch.setattr(sh, "embedding_engine", SimpleNamespace(db_path=str(live)))
    monkeypatch.setattr(
        sh,
        "bucket_mgr",
        SimpleNamespace(list_all=lambda **_kwargs: []),
        raising=False,
    )
    monkeypatch.setattr(
        sh,
        "publish_embedding_runtime",
        lambda _engine: (),
        raising=False,
    )
    monkeypatch.setattr(
        sh,
        "restore_embedding_runtime",
        lambda _snapshot: None,
        raising=False,
    )
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(
        embedding_engine_module,
        "EmbeddingEngine",
        FakeTargetEngine,
    )
    monkeypatch.setattr(migration_engine, "is_running", lambda: False)
    monkeypatch.setattr(
        migration_engine,
        "status_path_for",
        lambda _vault: str(tmp_path / "status.json"),
    )
    fake_mcp = FakeMCP()
    embedding_web.register(fake_mcp)
    return fake_mcp, live


@pytest.mark.asyncio
async def test_202_means_accepted_not_completed_and_uses_private_shadow(
    tmp_path,
    monkeypatch,
):
    fake_mcp, live = _setup_route(monkeypatch, tmp_path)
    captured = {}

    def start(cfg, *, reservation, **_kwargs):
        captured["cfg"] = cfg
        captured["reservation"] = reservation
        return object()

    monkeypatch.setattr(migration_engine, "start_migration", start)
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(
        FakeRequest(
            {
                "target_backend": "api",
                "api_key": "test-key",
                "model": "new-model",
            }
        )
    )
    body = _json(response)
    try:
        assert response.status_code == 202
        assert body["ok"] is True
        assert body["accepted"] is True
        assert body["completed"] is False
        assert "不代表发布完成" in body["message"]
        assert "api_key" not in body

        cfg = captured["cfg"]
        target_db = Path(cfg.target_engine.db_path)
        assert target_db != live
        assert target_db.parent == live.parent / ".embedding-generations"
        assert target_db.name == f"{body['txid']}.shadow.db"
        assert cfg.config_forward_patch["model"] == "new-model"
        assert "api_key" not in cfg.config_forward_patch

        # Publication must use the provider's post-shadow effective metadata,
        # not a stale route-time probe.
        cfg.target_engine._backend = SimpleNamespace(
            model_name=lambda: "effective-model",
            vector_dim=lambda: 7,
        )
        cfg.runtime_apply()
        assert sh.config["embedding"]["model"] == "effective-model"
        assert sh.config["embedding"]["dim"] == 7
    finally:
        captured["reservation"].close()


@pytest.mark.asyncio
async def test_provider_probe_failure_leaves_live_bytes_and_cleans_shadow(
    tmp_path,
    monkeypatch,
):
    fake_mcp, live = _setup_route(monkeypatch, tmp_path)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    FakeTargetEngine.probe = []
    try:
        handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
        response = await handler(
            FakeRequest({"target_backend": "api", "api_key": "test-key"})
        )
    finally:
        FakeTargetEngine.probe = [0.1, 0.2]
    assert response.status_code == 400
    assert "生产向量库未触碰" in _json(response)["error"]
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before
    generation_dir = live.parent / ".embedding-generations"
    assert not list(generation_dir.glob("*.shadow.db"))


@pytest.mark.asyncio
async def test_target_constructor_failure_leaves_live_bytes_and_cleans_shadow(
    tmp_path,
    monkeypatch,
):
    fake_mcp, live = _setup_route(monkeypatch, tmp_path)
    before = hashlib.sha256(live.read_bytes()).hexdigest()

    class BrokenTarget:
        def __init__(self, _config):
            raise RuntimeError("constructor secret detail must not escape")

    monkeypatch.setattr(embedding_engine_module, "EmbeddingEngine", BrokenTarget)
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(FakeRequest({"target_backend": "api"}))
    body = _json(response)
    assert response.status_code == 400
    assert body["error"] == "目标引擎构造失败"
    assert "secret detail" not in body["error"]
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before
    assert not list(
        (live.parent / ".embedding-generations").glob("*.shadow.db")
    )


@pytest.mark.asyncio
async def test_cross_process_reservation_conflict_returns_409(tmp_path, monkeypatch):
    fake_mcp, _live = _setup_route(monkeypatch, tmp_path)
    monkeypatch.setattr(embedding_web, "reserve_migration", lambda _path: None)
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(FakeRequest({"target_backend": "api"}))
    assert response.status_code == 409
    assert _json(response)["ok"] is False
    assert "跨进程迁移" in _json(response)["error"]


@pytest.mark.asyncio
async def test_secret_key_change_is_rejected_without_touching_live_or_shadow(
    tmp_path,
    monkeypatch,
):
    fake_mcp, live = _setup_route(monkeypatch, tmp_path)
    before = hashlib.sha256(live.read_bytes()).hexdigest()
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(
        FakeRequest(
            {
                "target_backend": "api",
                "api_key": "different-secret",
            }
        )
    )
    assert response.status_code == 400
    assert "不在迁移事务中更换" in _json(response)["error"]
    assert hashlib.sha256(live.read_bytes()).hexdigest() == before
    assert not list(
        (live.parent / ".embedding-generations").glob("*.shadow.db")
    )


@pytest.mark.asyncio
async def test_start_rejection_closes_reservation_and_removes_private_shadow(
    tmp_path,
    monkeypatch,
):
    fake_mcp, live = _setup_route(monkeypatch, tmp_path)
    monkeypatch.setattr(migration_engine, "start_migration", lambda *_a, **_k: None)
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(FakeRequest({"target_backend": "api"}))
    assert response.status_code == 409
    assert not list(
        (live.parent / ".embedding-generations").glob("*.shadow.db")
    )


@pytest.mark.asyncio
async def test_external_model_path_text_cannot_control_shadow_leaf(
    tmp_path,
    monkeypatch,
):
    fake_mcp, live = _setup_route(monkeypatch, tmp_path)
    captured = {}

    def start(cfg, *, reservation, **_kwargs):
        captured.update(cfg=cfg, reservation=reservation)
        return object()

    monkeypatch.setattr(migration_engine, "start_migration", start)
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(
        FakeRequest({"target_backend": "api", "model": "..\\..\\evil.db"})
    )
    try:
        assert response.status_code == 202
        target = Path(captured["cfg"].target_engine.db_path)
        assert target.parent == live.parent / ".embedding-generations"
        assert target.name.endswith(".shadow.db")
        assert ".." not in target.name
        assert "\\" not in target.name and "/" not in target.name
    finally:
        captured["reservation"].close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {"model": 42},
        {"model": "bad\ncontrol"},
        {"api_format": "unknown"},
        {"target_backend": "unsupported"},
    ],
)
async def test_malformed_or_unsupported_request_is_rejected(
    tmp_path,
    monkeypatch,
    body,
):
    fake_mcp, _live = _setup_route(monkeypatch, tmp_path)
    handler = fake_mcp.routes[("POST", "/api/embedding/migrate")]
    response = await handler(FakeRequest(body))
    assert response.status_code == 400
    assert _json(response)["ok"] is False


@pytest.mark.asyncio
async def test_status_preserves_publish_failed_terminal_state(tmp_path, monkeypatch):
    fake_mcp, _live = _setup_route(monkeypatch, tmp_path)
    monkeypatch.setattr(migration_engine, "is_running", lambda: False)
    monkeypatch.setattr(
        migration_engine,
        "read_status",
        lambda _path: {
            "phase": "publish_failed",
            "error": "rollback failed loudly",
        },
    )
    handler = fake_mcp.routes[("GET", "/api/embedding/migrate/status")]
    response = await handler(FakeRequest({}))
    body = _json(response)
    assert response.status_code == 200
    assert body["running"] is False
    assert body["status"]["phase"] == "publish_failed"
    assert "rollback failed loudly" in body["status"]["error"]
