from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import web.buckets as buckets_web
import web.config_api as config_web
import web.github as github_web


class FakeMCP:
    def __init__(self) -> None:
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    headers = {}
    query_params = {}
    cookies = {}

    def __init__(self, body=None, *, method="POST") -> None:
        self._body = body
        self.method = method

    async def json(self):
        return self._body


def payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def base_runtime(monkeypatch) -> None:
    monkeypatch.setattr(config_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(github_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_web.sh, "dehydrator", None, raising=False)
    monkeypatch.setattr(config_web.sh, "embedding_engine", None, raising=False)
    monkeypatch.setattr(config_web.sh, "bucket_mgr", None, raising=False)
    monkeypatch.setattr(config_web.sh, "import_engine", None, raising=False)
    monkeypatch.setattr(config_web.sh, "migrate_engine", None, raising=False)


@pytest.mark.asyncio
async def test_api_config_persists_before_runtime_and_preserves_dict_identity(
    monkeypatch, tmp_path: Path
) -> None:
    base_runtime(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("merge_threshold: 70\nunrelated: keep\n", encoding="utf-8")
    runtime = {"merge_threshold": 70, "unrelated": "keep"}
    identity = id(runtime)
    monkeypatch.setattr(config_web.sh, "config", runtime)
    monkeypatch.setattr(config_web, "_config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_web.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"merge_threshold": 55, "persist": True})
    )

    assert response.status_code == 200
    assert payload(response)["ok"] is True
    assert id(runtime) == identity
    assert runtime["merge_threshold"] == 55
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted == {"merge_threshold": 55, "unrelated": "keep"}


@pytest.mark.asyncio
async def test_api_config_persistence_failure_does_not_publish_runtime(
    monkeypatch,
) -> None:
    base_runtime(monkeypatch)
    runtime = {"merge_threshold": 70}
    monkeypatch.setattr(config_web.sh, "config", runtime)
    monkeypatch.setattr(
        config_web,
        "run_config_transaction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    mcp = FakeMCP()
    config_web.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"merge_threshold": 55, "persist": True})
    )

    assert response.status_code == 500
    assert payload(response)["ok"] is False
    assert payload(response)["updated"] == []
    assert runtime == {"merge_threshold": 70}


@pytest.mark.asyncio
async def test_sampling_and_human_failures_never_report_success(monkeypatch) -> None:
    base_runtime(monkeypatch)
    runtime = {
        "human": "old",
        "surfacing": {
            "sampling": {
                "enabled": False,
                "top_k": 5,
                "sample_k": 2,
                "temperature": 0.7,
            }
        },
    }
    monkeypatch.setattr(buckets_web.sh, "config", runtime)
    monkeypatch.setattr(
        buckets_web,
        "run_config_transaction",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")),
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    sampling = await mcp.routes[("POST", "/api/settings/sampling")](
        JsonRequest({"enabled": True})
    )
    human = await mcp.routes[("POST", "/api/settings/human")](
        JsonRequest({"human": "new"})
    )

    assert sampling.status_code == 500
    assert human.status_code == 500
    assert payload(sampling)["ok"] is False
    assert payload(human)["ok"] is False
    assert runtime["human"] == "old"
    assert runtime["surfacing"]["sampling"]["enabled"] is False


@pytest.mark.asyncio
async def test_github_clear_is_persisted_and_runtime_apply_is_compensatable(
    monkeypatch, tmp_path: Path
) -> None:
    base_runtime(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "unrelated": {"keep": True},
                "github_sync": {
                    "token": "secret",
                    "repo": "owner/repo",
                    "auto_interval_minutes": 60,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        github_web.sh,
        "config",
        {
            "unrelated": {"keep": True},
            "github_sync": {
                "token": "secret",
                "repo": "owner/repo",
                "auto_interval_minutes": 60,
            },
        },
    )
    intervals = []
    monkeypatch.setattr(github_web, "_config_file_path", lambda: str(config_path))
    monkeypatch.setattr(github_web.sh, "github_sync_instance", SimpleNamespace())
    monkeypatch.setattr(
        github_web.sh, "restart_github_auto_task", intervals.append
    )
    monkeypatch.setattr(
        github_web.sh,
        "get_github_auto_interval",
        lambda: intervals[-1] if intervals else 60,
    )
    mcp = FakeMCP()
    github_web.register(mcp)

    response = await mcp.routes[("POST", "/api/github/config")](
            JsonRequest({"token": "", "repo": "", "clear": True})
    )

    assert response.status_code == 200
    assert payload(response)["ok"] is True
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["unrelated"] == {"keep": True}
    assert persisted["github_sync"]["repo"] == ""
    assert "token" not in persisted["github_sync"]
    assert github_web.sh.github_sync_instance is None
    assert intervals == [0]


@pytest.mark.asyncio
async def test_env_config_rejects_cross_file_batch_and_embedding_identity_change(
    monkeypatch,
) -> None:
    base_runtime(monkeypatch)
    monkeypatch.setattr(
        config_web.sh,
        "config",
        {
            "dehydration": {"api_key": ""},
            "embedding": {"model": "old-model", "api_key": ""},
        },
    )
    mcp = FakeMCP()
    config_web.register(mcp)
    mixed = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest(
            {
                "updates": {
                    "OMBRE_COMPRESS_API_KEY": "key",
                    "AI_NAME": "name",
                }
            }
        )
    )
    identity = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest({"updates": {"OMBRE_EMBED_MODEL": "new-model"}})
    )
    assert mixed.status_code == 400
    assert identity.status_code == 400
    assert payload(mixed)["updated"] == []
    assert payload(identity)["updated"] == []
