import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import web.buckets as buckets_web
import web.config_api as config_api
import web.import_api as import_api
import utils


ROOT = Path(__file__).resolve().parents[1]


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
    def __init__(self, body=None, *, path_params=None, method="POST"):
        self._body = body or {}
        self.path_params = path_params or {}
        self.headers = {}
        self.query_params = {}
        self.method = method

    async def json(self):
        return self._body


def _json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_pulse_shows_special_bucket_counts_separately(monkeypatch):
    from tools.anchor import core as anchor_core

    class FakeDecay:
        is_running = True

        async def ensure_started(self):
            return None

    class FakeBucketManager:
        async def get_stats(self):
            return {
                "permanent_count": 1,
                "dynamic_count": 2,
                "archive_count": 3,
                "feel_count": 4,
                "plan_count": 5,
                "letter_count": 6,
                "total_size_kb": 7.0,
            }

        async def list_all(self, include_archive=False):
            return []

    monkeypatch.setattr(anchor_core.rt, "decay_engine", FakeDecay(), raising=False)
    monkeypatch.setattr(anchor_core.rt, "bucket_mgr", FakeBucketManager(), raising=False)
    monkeypatch.setattr(anchor_core.rt, "embedding_engine", None, raising=False)

    result = await anchor_core.pulse()

    assert "feel 桶: 4 条" in result
    assert "plan 桶: 5 条" in result
    assert "letter 桶: 6 封" in result


@pytest.mark.asyncio
async def test_pulse_marks_anchor_without_replacing_icon_and_release_clears_it(
    monkeypatch,
):
    from tools.anchor import core as anchor_core

    class FakeDecay:
        is_running = True

        async def ensure_started(self):
            return None

        def calculate_score(self, _meta):
            return 1.0

    class FakeBucketManager:
        def __init__(self):
            self.buckets = [
                {
                    "id": "anchor-1",
                    "content": "anchor body",
                    "metadata": {
                        "type": "permanent",
                        "name": "坐标系",
                        "domain": ["关系"],
                        "importance": 9,
                        "anchor": "true",
                    },
                },
                {
                    "id": "ordinary-1",
                    "content": "ordinary body",
                    "metadata": {
                        "type": "dynamic",
                        "name": "普通记忆",
                        "domain": ["生活"],
                        "importance": 5,
                        "anchor": "false",
                    },
                },
            ]

        async def get_stats(self):
            return {
                "permanent_count": 1,
                "dynamic_count": 1,
                "archive_count": 0,
                "feel_count": 0,
                "plan_count": 0,
                "letter_count": 0,
                "total_size_kb": 1.0,
            }

        async def list_all(self, include_archive=False):
            return self.buckets

        async def set_anchor(self, bucket_id, value):
            bucket = next(b for b in self.buckets if b["id"] == bucket_id)
            if value:
                bucket["metadata"]["anchor"] = True
            else:
                bucket["metadata"].pop("anchor", None)
            count = sum(
                1 for b in self.buckets if b["metadata"].get("anchor") is True
            )
            return {"ok": True, "anchor": value, "count": count, "limit": 24}

    bucket_mgr = FakeBucketManager()
    monkeypatch.setattr(anchor_core.rt, "decay_engine", FakeDecay(), raising=False)
    monkeypatch.setattr(anchor_core.rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(anchor_core.rt, "embedding_engine", None, raising=False)
    monkeypatch.setattr(anchor_core.rt, "mark_op", None, raising=False)

    before = await anchor_core.pulse()
    anchor_line = next(line for line in before.splitlines() if "anchor-1" in line)
    ordinary_line = next(line for line in before.splitlines() if "ordinary-1" in line)

    assert anchor_line.startswith("📦 [anchor-1]")
    assert "⚓ [anchor]" in anchor_line
    assert "⚓ [anchor]" not in ordinary_line

    await anchor_core.anchor_release("anchor-1")
    after = await anchor_core.pulse()

    assert "⚓ [anchor]" not in after


@pytest.mark.asyncio
async def test_grow_shortpath_explains_hold_style_single_memory(monkeypatch):
    from tools.grow import shortpath

    class FakeLogger:
        def info(self, *_args, **_kwargs):
            pass

    class FakeDehydrator:
        async def analyze(self, _content, *, include_why=False):
            assert include_why is True
            return {
                "importance": 5,
                "tags": ["短句"],
                "domain": ["测试"],
                "valence": 0.5,
                "arousal": 0.3,
                "suggested_name": "短内容",
                "why_remembered": "这条会影响我后续的判断。",
            }

    captured = {}

    async def fake_merge_or_create(**kwargs):
        captured.update(kwargs)
        return "bucket-1", False, ""

    async def fake_background(*_args, **_kwargs):
        return None

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(shortpath.rt, "logger", FakeLogger(), raising=False)
    monkeypatch.setattr(shortpath.rt, "dehydrator", FakeDehydrator(), raising=False)
    monkeypatch.setattr(shortpath, "merge_or_create", fake_merge_or_create)
    monkeypatch.setattr(shortpath, "check_plan_resolution", fake_background)
    monkeypatch.setattr(shortpath, "check_duplicate_for", fake_background)
    monkeypatch.setattr(shortpath.asyncio, "create_task", fake_create_task)

    result = await shortpath.grow_shortpath("短句")

    assert "短内容已按 hold 路径保存为单条记忆" in result
    assert captured["why_remembered"] == (
        "这条会影响我后续的判断。"
    )
    assert captured["merge_why_remembered"] == (
        "这条会影响我后续的判断。"
    )


@pytest.mark.asyncio
async def test_host_vault_set_returns_restart_required_message(monkeypatch, tmp_path):
    monkeypatch.setattr(import_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_api.sh, "in_docker", lambda: False)
    monkeypatch.setattr(import_api.sh, "_project_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(import_api.sh, "_write_env_var", lambda _key, _value: None)

    mcp = FakeMCP()
    import_api.register(mcp)

    response = await mcp.routes[("POST", "/api/host-vault")](
        JsonRequest({"value": "D:/Vault/Ombre"})
    )
    payload = _json(response)

    assert payload["ok"] is True
    assert payload["restart_required"] is True
    assert "重启" in payload["message"]


@pytest.mark.asyncio
async def test_host_vault_set_rejects_container_local_fake_save(monkeypatch):
    writes = []
    monkeypatch.setattr(import_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_api.sh, "in_docker", lambda: True)
    monkeypatch.setattr(import_api.sh, "_write_env_var", lambda key, value: writes.append((key, value)))

    mcp = FakeMCP()
    import_api.register(mcp)

    response = await mcp.routes[("POST", "/api/host-vault")](
        JsonRequest({"value": "D:/Vault/Ombre"})
    )
    payload = _json(response)

    assert response.status_code == 409
    assert payload["compose_managed"] is True
    assert payload["restart_required"] is True
    assert "compose" in payload["error"].lower()
    assert writes == []


@pytest.mark.asyncio
async def test_host_vault_get_reports_compose_injected_value_in_container(monkeypatch):
    monkeypatch.setattr(import_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(import_api.sh, "in_docker", lambda: True)
    monkeypatch.setenv("OMBRE_HOST_VAULT_DIR", "D:/Vault/Ombre")
    monkeypatch.setattr(
        import_api.sh,
        "_read_env_var",
        lambda _name: pytest.fail("container must not read its local .env for a host mount"),
    )

    mcp = FakeMCP()
    import_api.register(mcp)

    response = await mcp.routes[("GET", "/api/host-vault")](JsonRequest())
    payload = _json(response)

    assert response.status_code == 200
    assert payload["value"] == "D:/Vault/Ombre"
    assert payload["source"] == "env"
    assert payload["env_file"] is None
    assert payload["compose_managed"] is True


@pytest.mark.asyncio
async def test_bucket_resolve_route_returns_trace_aligned_message(monkeypatch):
    class FakeBucketManager:
        def __init__(self):
            self.row = {"id": "b1", "metadata": {"id": "b1", "resolved": False}}
            self.updates = []

        async def get(self, bucket_id):
            return self.row if bucket_id == "b1" else None

        async def update(self, bucket_id, **updates):
            self.updates.append((bucket_id, updates))
            self.row["metadata"].update(updates)
            return True

    bucket_mgr = FakeBucketManager()
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "bucket_mgr", bucket_mgr, raising=False)

    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/bucket/{bucket_id}/resolve")](
        JsonRequest(path_params={"bucket_id": "b1"})
    )
    payload = _json(response)

    assert payload["resolved"] is True
    assert payload["message"] == "已沉底，只在关键词触发时重新浮现"


def test_config_example_marks_wikilink_deprecated_without_active_stanza():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")

    assert "\nwikilink:" not in text
    assert "wikilink" in text
    assert "deprecated" in text.lower() or "已废弃" in text


def test_dashboard_single_bucket_delete_is_not_labeled_as_hard_delete():
    for rel in ("frontend/dashboard.html",):
        text = (ROOT / rel).read_text(encoding="utf-8")

        assert "归档" in text
        assert "bucketDelete(this.dataset.bucketId)" not in text
        assert "这将彻底删除此记忆桶" not in text
        assert "你真的要永久删除吗" not in text
        assert "彻底删除这封信" not in text
        assert "你真的要永久删除这封信吗" not in text
        assert 'title="彻底删除"' not in text
        assert "/api/buckets/purge" not in text
        assert "进入清理模式" not in text
        assert "批量永久删除" not in text


def test_dashboard_exposes_oauth_authentication_switch():
    for rel in ("frontend/dashboard.html",):
        text = (ROOT / rel).read_text(encoding="utf-8")

        assert 'id="cfg-mcp-auth"' in text
        assert "开启 OAuth（Claude.ai 网页版 / Claude Code 远程需要）" in text
        assert "saveMcpAuth()" in text
        assert "mcp_require_auth: false, persist: true" in text
        assert 'id="btn-restart"' in text
        assert "restartService()" in text
        assert "setRestartRequired(!!result.restart_required" in text
        assert "安全门禁已强制开启鉴权" in text


def test_dashboard_exposes_mcp_static_token_and_hybrid_modes():
    for rel in ("frontend/dashboard.html",):
        text = (ROOT / rel).read_text(encoding="utf-8")

        # 共存模式显式启用；纯 OAuth 不会因遗留 token 悄悄扩大凭据面。
        assert '<option value="oauth">' in text
        assert '<option value="hybrid">' in text
        assert '<option value="token">' in text
        assert '<option value="off">' in text
        assert 'id="mcp-token-panel"' in text
        assert "regenerateMcpToken()" in text
        assert "/api/mcp-token/regenerate" in text
        assert "Ombre-MCP-Token" in text
        assert "OAuth + 静态 Token 共存" in text


@pytest.mark.asyncio
async def test_dashboard_hybrid_mode_persists_and_requires_restart(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    runtime = {
        "transport": "streamable-http",
        "mcp_require_auth": True,
        "mcp_auth_mode": "oauth",
    }
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("OMBRE_MCP_AUTH_MODE", raising=False)
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"mcp_auth_mode": "hybrid", "persist": True})
    )
    payload = _json(response)
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert payload["mcp_auth_mode"] == "hybrid"
    assert payload["mcp_auth_mode_effective"] == "oauth"
    assert payload["restart_required"] is True
    assert persisted["mcp_auth_mode"] == "hybrid"
    assert runtime["mcp_auth_mode"] == "oauth"


@pytest.mark.asyncio
async def test_dashboard_oauth_switch_persists_to_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        config_api.sh,
        "config",
        {"transport": "streamable-http", "mcp_require_auth": True},
    )
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("OMBRE_MCP_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"mcp_require_auth": False, "persist": True})
    )
    payload = _json(response)
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["restart_required"] is True
    assert payload["mcp_require_auth_effective"] is True
    # Startup-bound settings remain the effective runtime snapshot until restart.
    assert config_api.sh.config["mcp_require_auth"] is True
    assert persisted["mcp_require_auth"] is False


@pytest.mark.asyncio
async def test_dashboard_mcp_startup_settings_require_persistence_and_do_not_publish_on_failure(
    monkeypatch,
):
    runtime = {
        "transport": "streamable-http",
        "mcp_require_auth": True,
        "mcp_auth_mode": "oauth",
    }
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    mcp = FakeMCP()
    config_api.register(mcp)

    not_persisted = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"mcp_require_auth": False})
    )
    assert not_persisted.status_code == 400
    assert runtime == {
        "transport": "streamable-http",
        "mcp_require_auth": True,
        "mcp_auth_mode": "oauth",
    }

    monkeypatch.setattr(
        config_api,
        "atomic_update_config_yaml",
        lambda _mutate: (_ for _ in ()).throw(OSError("read-only mount")),
    )
    failed = await mcp.routes[("POST", "/api/config")](
        JsonRequest(
            {
                "mcp_require_auth": False,
                "mcp_auth_mode": "token",
                "persist": True,
            }
        )
    )
    assert failed.status_code == 500
    assert runtime == {
        "transport": "streamable-http",
        "mcp_require_auth": True,
        "mcp_auth_mode": "oauth",
    }


@pytest.mark.asyncio
async def test_dashboard_hot_config_persist_failure_rolls_back_runtime(
    monkeypatch,
):
    runtime = {
        "transport": "streamable-http",
        "dehydration": {"model": "old-model", "max_tokens": 1000},
        "merge_threshold": 70,
    }
    dehydrator = SimpleNamespace(
        model="old-model",
        base_url="https://old.example/v1",
        max_tokens=1000,
        temperature=0.2,
        timeout_seconds=30.0,
        api_format="openai_compat",
        api_key="",
        api_available=False,
        client=None,
    )
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "dehydrator", dehydrator)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setattr(
        config_api,
        "atomic_update_config_yaml",
        lambda _mutate: (_ for _ in ()).throw(OSError("只读挂载")),
    )
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](JsonRequest({
        "dehydration": {"model": "new-model", "max_tokens": 2000},
        "merge_threshold": 55,
        "persist": True,
    }))

    assert response.status_code == 500
    assert _json(response)["updated"] == []
    assert runtime == {
        "transport": "streamable-http",
        "dehydration": {"model": "old-model", "max_tokens": 1000},
        "merge_threshold": 70,
    }
    assert dehydrator.model == "old-model"
    assert dehydrator.max_tokens == 1000


@pytest.mark.asyncio
async def test_dashboard_rejects_unsafe_no_auth_before_config_write(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    writes = []
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        config_api.sh,
        "config",
        {"transport": "streamable-http", "mcp_require_auth": True},
    )
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("OMBRE_MCP_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    monkeypatch.setattr(
        config_api,
        "atomic_update_config_yaml",
        lambda mutate: writes.append(mutate),
    )
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"mcp_require_auth": False, "persist": True})
    )
    payload = _json(response)

    assert response.status_code == 400
    assert payload["mcp_network_security"]["guard_required"] is True
    assert writes == []
    assert not config_path.exists()
    assert config_api.sh.config == {
        "transport": "streamable-http",
        "mcp_require_auth": True,
    }


@pytest.mark.asyncio
async def test_dashboard_rechecks_network_safety_inside_atomic_config_turn(
    monkeypatch,
):
    runtime = {"transport": "stdio", "mcp_require_auth": True}
    latest_after_attempt = {}

    def writer(mutate):
        latest = {"transport": "streamable-http", "mcp_require_auth": True}
        try:
            mutate(latest)
        finally:
            latest_after_attempt.update(latest)
        return latest

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", writer)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"mcp_require_auth": False, "persist": True})
    )

    assert response.status_code == 400
    assert "关闭 MCP 鉴权仅允许" in _json(response)["error"]
    assert latest_after_attempt == {
        "transport": "streamable-http",
        "mcp_require_auth": True,
    }
    assert runtime == {"transport": "stdio", "mcp_require_auth": True}


@pytest.mark.asyncio
async def test_dashboard_reports_restart_after_open_runtime_is_repaired(
    monkeypatch,
):
    from ombrebrain.security.deployment_profile import enforce_mcp_network_guard

    runtime = {"transport": "streamable-http", "mcp_require_auth": False}
    enforce_mcp_network_guard(
        runtime,
        environment={"OMBRE_BIND_HOST": "0.0.0.0"},
    )
    persisted = {"transport": "streamable-http", "mcp_require_auth": False}

    def writer(mutate):
        candidate = copy.deepcopy(persisted)
        mutate(candidate)
        persisted.clear()
        persisted.update(candidate)
        return candidate

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("OMBRE_MCP_REQUIRE_AUTH", raising=False)
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", writer)
    monkeypatch.setattr(config_api, "read_config_yaml", lambda: copy.deepcopy(persisted))
    mcp = FakeMCP()
    config_api.register(mcp)

    repaired = await mcp.routes[("POST", "/api/config")](JsonRequest({
        "mcp_require_auth": True,
        "persist": True,
    }))
    repaired_payload = _json(repaired)
    refreshed = await mcp.routes[("GET", "/api/config")](JsonRequest(method="GET"))
    refreshed_payload = _json(refreshed)

    assert repaired.status_code == 200
    assert repaired_payload["mcp_require_auth"] is True
    assert repaired_payload["mcp_require_auth_effective"] is False
    assert repaired_payload["mcp_network_security"]["guard_active"] is False
    assert repaired_payload["restart_required"] is True
    assert persisted["mcp_require_auth"] is True
    assert refreshed.status_code == 200
    assert refreshed_payload["mcp_require_auth"] is True
    assert refreshed_payload["mcp_require_auth_effective"] is False
    assert refreshed_payload["mcp_network_security"]["guard_active"] is False
    assert refreshed_payload["restart_required"] is True


@pytest.mark.asyncio
async def test_dashboard_explicit_insecure_override_is_auditable(monkeypatch):
    runtime = {"transport": "streamable-http", "mcp_require_auth": True}
    persisted = {"transport": "streamable-http", "mcp_require_auth": True}

    def writer(mutate):
        candidate = copy.deepcopy(persisted)
        mutate(candidate)
        persisted.clear()
        persisted.update(candidate)
        return candidate

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("OMBRE_MCP_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("OMBRE_ALLOW_INSECURE_MCP", "true")
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", writer)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](JsonRequest({
        "mcp_require_auth": False,
        "persist": True,
    }))
    payload = _json(response)

    assert response.status_code == 200
    assert persisted["mcp_require_auth"] is False
    assert payload["mcp_network_security"]["override_active"] is True
    assert payload["mcp_network_security"]["guard_active"] is False
    assert payload["warnings"]
    assert payload["restart_required"] is True


@pytest.mark.asyncio
async def test_dashboard_reports_platform_managed_open_runtime_as_effective(
    monkeypatch,
):
    from ombrebrain.security.deployment_profile import enforce_mcp_network_guard

    runtime = {"transport": "streamable-http", "mcp_require_auth": False}
    enforce_mcp_network_guard(
        runtime,
        environment={
            "OMBRE_BIND_HOST": "0.0.0.0",
            "OMBRE_MCP_REQUIRE_AUTH": "false",
        },
    )
    persisted = {"transport": "streamable-http", "mcp_require_auth": False}

    def writer(mutate):
        candidate = copy.deepcopy(persisted)
        mutate(candidate)
        persisted.clear()
        persisted.update(candidate)
        return candidate

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("OMBRE_MCP_REQUIRE_AUTH", "false")
    monkeypatch.delenv("OMBRE_ALLOW_INSECURE_MCP", raising=False)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", writer)
    monkeypatch.setattr(config_api, "read_config_yaml", lambda: copy.deepcopy(persisted))
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](JsonRequest({
        "mcp_require_auth": True,
        "persist": True,
    }))
    payload = _json(response)
    refreshed = await mcp.routes[("GET", "/api/config")](JsonRequest(method="GET"))
    refreshed_payload = _json(refreshed)

    assert response.status_code == 200
    assert persisted["mcp_require_auth"] is True
    assert payload["mcp_require_auth"] is True
    assert payload["mcp_require_auth_effective"] is False
    assert payload["mcp_network_security"]["guard_active"] is False
    assert payload["restart_required"] is False
    assert payload["warnings"]
    assert "仍由平台环境变量控制" in payload["message"]
    assert refreshed.status_code == 200
    assert refreshed_payload["mcp_require_auth"] is True
    assert refreshed_payload["mcp_require_auth_effective"] is False
    assert refreshed_payload["mcp_network_security"]["guard_active"] is False
    assert refreshed_payload["restart_required"] is False


@pytest.mark.asyncio
async def test_dashboard_does_not_promise_restart_can_override_platform_no_auth(
    monkeypatch,
):
    from ombrebrain.security.deployment_profile import enforce_mcp_network_guard

    runtime = {"transport": "streamable-http", "mcp_require_auth": False}
    enforce_mcp_network_guard(
        runtime,
        environment={
            "OMBRE_BIND_HOST": "0.0.0.0",
            "OMBRE_MCP_REQUIRE_AUTH": "false",
            "OMBRE_ALLOW_INSECURE_MCP": "true",
        },
    )
    persisted = {"transport": "streamable-http", "mcp_require_auth": False}

    def writer(mutate):
        candidate = copy.deepcopy(persisted)
        mutate(candidate)
        persisted.clear()
        persisted.update(candidate)
        return candidate

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setenv("OMBRE_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("OMBRE_MCP_REQUIRE_AUTH", "false")
    monkeypatch.setenv("OMBRE_ALLOW_INSECURE_MCP", "true")
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", writer)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](JsonRequest({
        "mcp_require_auth": True,
        "persist": True,
    }))
    payload = _json(response)

    assert response.status_code == 200
    assert persisted["mcp_require_auth"] is True
    assert payload["mcp_require_auth_effective"] is False
    assert payload["mcp_network_security"]["override_active"] is True
    assert payload["mcp_network_security"]["auth_environment_override"] is True
    assert payload["restart_required"] is False
    assert any("OMBRE_MCP_REQUIRE_AUTH" in warning for warning in payload["warnings"])
    assert "仍由平台环境变量控制" in payload["message"]


@pytest.mark.asyncio
async def test_dashboard_rejects_mixing_startup_and_hot_runtime_settings(
    monkeypatch,
):
    runtime = {"transport": "streamable-http", "mcp_require_auth": True}
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](JsonRequest({
        "mcp_require_auth": True,
        "surfacing": {"breath_max_results": 11},
        "persist": True,
    }))

    assert response.status_code == 400
    assert "separate requests" in _json(response)["error"]
    assert runtime == {"transport": "streamable-http", "mcp_require_auth": True}


@pytest.mark.asyncio
async def test_mcp_token_regenerate_write_failure_keeps_the_live_token(monkeypatch):
    runtime = {"mcp_auth_mode": "token", "mcp_token": "old-live-token"}
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(
        config_api.secrets,
        "token_urlsafe",
        lambda _size: "new-uncommitted-token",
    )
    monkeypatch.setattr(
        config_api,
        "atomic_update_config_yaml",
        lambda _mutate: (_ for _ in ()).throw(OSError("read-only config mount")),
    )
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/mcp-token/regenerate")](JsonRequest())

    assert response.status_code == 500
    assert "persist failed" in _json(response)["error"]
    assert runtime == {"mcp_auth_mode": "token", "mcp_token": "old-live-token"}
    assert b"new-uncommitted-token" not in response.body


def test_mcp_token_regenerate_serializes_disk_and_runtime_commit_across_loops(
    monkeypatch,
):
    runtime = {"mcp_auth_mode": "token", "mcp_token": "old-token"}
    persisted = {"mcp_token": "old-token"}
    thread_state = threading.local()
    first_persisted = threading.Event()
    second_attempting = threading.Event()
    second_persisted = threading.Event()
    release_first = threading.Event()

    def next_token(_size):
        return thread_state.token

    def persist(mutate):
        candidate = copy.deepcopy(persisted)
        mutate(candidate)
        persisted.clear()
        persisted.update(candidate)
        if candidate["mcp_token"] == "token-a":
            first_persisted.set()
            assert release_first.wait(5), "first token commit was not released"
        else:
            second_persisted.set()
        return candidate

    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.secrets, "token_urlsafe", next_token)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", persist)
    mcp = FakeMCP()
    config_api.register(mcp)
    handler = mcp.routes[("POST", "/api/mcp-token/regenerate")]

    def invoke(token, attempting=None):
        thread_state.token = token
        if attempting is not None:
            attempting.set()
        return asyncio.run(handler(JsonRequest()))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, "token-a")
        try:
            assert first_persisted.wait(3), "first token never reached persistence"
            second = executor.submit(invoke, "token-b", second_attempting)
            assert second_attempting.wait(3), "second token request did not start"
            assert not second_persisted.wait(0.5), (
                "second token persisted before the first runtime publish"
            )
        finally:
            release_first.set()
        first_response = first.result(timeout=5)
        second_response = second.result(timeout=5)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert persisted["mcp_token"] == "token-b"
    assert runtime["mcp_token"] == persisted["mcp_token"]


@pytest.mark.parametrize(
    "invalid_update",
    [
        {"merge_threshold": -1},
        {"merge_threshold": 101},
        {"merge_threshold": True},
        {"merge_threshold": 1.5},
        {"host_port": 0},
        {"host_port": 65536},
        {"dehydration": {"max_tokens": 127}},
        {"dehydration": {"temperature": float("nan")}},
        {"dehydration": {"temperature": float("inf")}},
        {"dehydration": {"temperature": True}},
        {"dehydration": {"timeout_seconds": 0}},
        {"surfacing": {"breath_max_results": 0}},
        {"surfacing": {"breath_max_tokens": 499}},
        {"surfacing": {"breath_max_tokens": 40001}},
        {"surfacing": {"feel_max_tokens": 20001}},
        {"surfacing": {"sampling": {"top_k": 0}}},
        {"surfacing": {"sampling": {"sample_k": 21}}},
        {"surfacing": {"sampling": {"temperature": float("nan")}}},
        {"surfacing": {"sampling": {"temperature": float("inf")}}},
    ],
)
@pytest.mark.asyncio
async def test_dashboard_config_rejects_nonfinite_and_out_of_range_numbers_atomically(
    monkeypatch,
    invalid_update,
):
    runtime = {
        "merge_threshold": 75,
        "host_port": 8000,
        "dehydration": {"temperature": 0.2},
        "surfacing": {"breath_max_results": 20},
    }
    original = copy.deepcopy(runtime)
    persistence_calls = []
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(
        config_api,
        "atomic_update_config_yaml",
        lambda mutate: persistence_calls.append(mutate),
    )
    mcp = FakeMCP()
    config_api.register(mcp)
    body = copy.deepcopy(invalid_update)
    body["persist"] = True

    response = await mcp.routes[("POST", "/api/config")](JsonRequest(body))

    assert response.status_code == 400
    assert runtime == original
    assert persistence_calls == []


@pytest.mark.asyncio
async def test_dashboard_config_persists_validated_numeric_types(monkeypatch):
    runtime = {"merge_threshold": 75, "host_port": 8000, "surfacing": {}}
    persisted = {}

    def persist(mutate):
        target = {}
        mutate(target)
        persisted.update(target)
        return target

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", persist)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest(
            {
                "merge_threshold": "42",
                "host_port": "8123",
                "surfacing": {
                    "breath_max_results": "11",
                    "breath_max_tokens": "35000",
                    "feel_max_tokens": "7000",
                    "sampling": {
                        "enabled": True,
                        "top_k": "9",
                        "sample_k": "3",
                        "temperature": "0.8",
                    },
                },
                "persist": True,
            }
        )
    )

    assert response.status_code == 200
    assert runtime["merge_threshold"] == 42
    assert runtime["host_port"] == 8123
    assert runtime["surfacing"] == {
        "breath_max_results": 11,
        "breath_max_tokens": 35000,
        "feel_max_tokens": 7000,
    }
    assert persisted["merge_threshold"] == 42
    assert persisted["host_port"] == 8123
    assert persisted["surfacing"] == {
        "breath_max_results": 11,
        "breath_max_tokens": 35000,
        "feel_max_tokens": 7000,
        "sampling": {
            "enabled": True,
            "top_k": 9,
            "sample_k": 3,
            "temperature": 0.8,
        },
    }


@pytest.mark.asyncio
async def test_dashboard_config_applies_zero_dehydration_temperature(monkeypatch):
    runtime = {"dehydration": {"temperature": 0.3}}
    dehydrator = SimpleNamespace(
        model="model",
        base_url="https://example.invalid/v1",
        max_tokens=1024,
        temperature=0.3,
        timeout_seconds=60.0,
        api_format="openai_compat",
        api_key="",
        api_available=False,
        client=None,
    )
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "dehydrator", dehydrator, raising=False)
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"dehydration": {"temperature": 0.0}})
    )

    assert response.status_code == 200
    assert runtime["dehydration"]["temperature"] == 0.0
    assert dehydrator.temperature == 0.0


@pytest.mark.asyncio
async def test_dashboard_public_mcp_address_persists_round_trips_and_clears(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"deployment": {"profile": "advanced"}}), encoding="utf-8"
    )
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        config_api.sh,
        "config",
        {"mcp_require_auth": True, "deployment": {"profile": "advanced"}},
    )
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_api.register(mcp)

    saved = await mcp.routes[("POST", "/api/config")](
        JsonRequest(
            {
                "deployment": {"public_url": "https://OB.Example:443/mcp"},
                "persist": True,
            }
        )
    )
    saved_payload = _json(saved)
    reloaded = await mcp.routes[("GET", "/api/config")](JsonRequest())

    assert saved.status_code == 200
    assert saved_payload["restart_required"] is True
    assert saved_payload["deployment"]["public_url"] == "https://ob.example"
    assert saved_payload["deployment"]["public_url_effective"] == ""
    reloaded_payload = _json(reloaded)
    assert reloaded_payload["deployment"]["public_url"] == "https://ob.example"
    assert reloaded_payload["deployment"]["public_url_effective"] == ""
    assert reloaded_payload["restart_required"] is True
    assert config_api.sh.config["deployment"] == {"profile": "advanced"}
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["deployment"] == {
        "profile": "advanced",
        "public_url": "https://ob.example",
    }

    cleared = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"deployment": {"public_url": ""}, "persist": True})
    )
    cleared_payload = _json(cleared)
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert cleared_payload["deployment"]["public_url"] == ""
    assert persisted["deployment"] == {"profile": "advanced"}


@pytest.mark.asyncio
async def test_dashboard_public_mcp_address_rejects_non_origin_without_mutation(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    original = {"deployment": {"public_url": "https://safe.example"}}
    config_path.write_text(yaml.safe_dump(original), encoding="utf-8")
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", dict(original))
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest(
            {
                "deployment": {"public_url": "https://safe.example/other?x=1"},
                "persist": True,
            }
        )
    )

    assert response.status_code == 400
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == original
    assert config_api.sh.config == original


@pytest.mark.asyncio
async def test_dashboard_public_mcp_address_write_failure_is_not_published_to_runtime(
    monkeypatch
):
    runtime = {"deployment": {"public_url": "https://old.example"}}
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(
        config_api,
        "atomic_update_config_yaml",
        lambda _mutate: (_ for _ in ()).throw(OSError("read-only mount")),
    )
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest(
            {
                "deployment": {"public_url": "https://new.example/mcp"},
                "persist": True,
            }
        )
    )

    assert response.status_code == 500
    assert "persist failed" in _json(response)["error"]
    assert runtime == {"deployment": {"public_url": "https://old.example"}}


@pytest.mark.parametrize(
    "body",
    [
        {"top_k": True},
        {"top_k": 2.5},
        {"sample_k": False},
        {"sample_k": 3.5},
        {"temperature": True},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
    ],
)
@pytest.mark.asyncio
async def test_sampling_settings_reject_invalid_numeric_values_without_mutation(
    monkeypatch,
    body,
):
    old_sampling = {
        "enabled": False,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 0.7,
    }
    runtime = {"surfacing": {"sampling": old_sampling}}
    persist_calls = []
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "config", runtime)
    monkeypatch.setattr(
        buckets_web,
        "atomic_update_config_yaml",
        lambda mutate: persist_calls.append(mutate),
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/settings/sampling")](
        JsonRequest({"top_k": 10, **body})
    )

    assert response.status_code == 400
    assert runtime["surfacing"]["sampling"] is old_sampling
    assert old_sampling == {
        "enabled": False,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 0.7,
    }
    assert persist_calls == []


@pytest.mark.asyncio
async def test_sampling_settings_write_failure_keeps_runtime_unchanged(monkeypatch):
    old_sampling = {
        "enabled": False,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 0.7,
    }
    runtime = {"surfacing": {"sampling": old_sampling}}
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "config", runtime)
    monkeypatch.setattr(
        buckets_web,
        "atomic_update_config_yaml",
        lambda _mutate: (_ for _ in ()).throw(OSError("read-only config")),
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/settings/sampling")](
        JsonRequest({"enabled": True, "top_k": 10, "temperature": 1.2})
    )

    assert response.status_code == 500
    assert runtime["surfacing"]["sampling"] is old_sampling
    assert old_sampling == {
        "enabled": False,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 0.7,
    }


@pytest.mark.asyncio
async def test_sampling_settings_persist_before_atomic_runtime_publish(monkeypatch):
    old_sampling = {
        "enabled": False,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 0.7,
    }
    runtime = {"surfacing": {"sampling": old_sampling}}
    persisted = {}

    def persist(mutate):
        assert old_sampling["top_k"] == 5
        target = {}
        mutate(target)
        persisted.update(target)
        return target

    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "config", runtime)
    monkeypatch.setattr(buckets_web, "atomic_update_config_yaml", persist)
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/settings/sampling")](
        JsonRequest(
            {
                "enabled": True,
                "top_k": "10",
                "sample_k": "4",
                "temperature": "1.2",
            }
        )
    )

    assert response.status_code == 200
    assert runtime["surfacing"]["sampling"] is old_sampling
    assert old_sampling == {
        "enabled": True,
        "top_k": 10,
        "sample_k": 4,
        "temperature": 1.2,
    }
    assert persisted["surfacing"]["sampling"] == old_sampling


def test_sampling_settings_serialize_commit_and_merge_across_event_loops(
    monkeypatch,
):
    old_sampling = {
        "enabled": False,
        "top_k": 5,
        "sample_k": 2,
        "temperature": 0.7,
    }
    runtime = {"surfacing": {"sampling": old_sampling}}
    persisted = {"surfacing": {"sampling": copy.deepcopy(old_sampling)}}
    disk_lock = threading.Lock()
    first_persisted = threading.Event()
    second_attempting = threading.Event()
    second_persisted = threading.Event()
    release_first = threading.Event()

    def persist(mutate):
        with disk_lock:
            candidate = copy.deepcopy(persisted)
            mutate(candidate)
            persisted.clear()
            persisted.update(candidate)
        sampling = candidate["surfacing"]["sampling"]
        if sampling["temperature"] == 0.7:
            first_persisted.set()
            assert release_first.wait(5), "first sampling commit was not released"
        else:
            second_persisted.set()
        return candidate

    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "config", runtime)
    monkeypatch.setattr(buckets_web, "atomic_update_config_yaml", persist)
    mcp = FakeMCP()
    buckets_web.register(mcp)
    handler = mcp.routes[("POST", "/api/settings/sampling")]

    def invoke(body, attempting=None):
        if attempting is not None:
            attempting.set()
        return asyncio.run(handler(JsonRequest(body)))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(invoke, {"top_k": 10})
        try:
            assert first_persisted.wait(3), "first sampling update was not persisted"
            second = executor.submit(
                invoke,
                {"temperature": 1.2},
                second_attempting,
            )
            assert second_attempting.wait(3), "second sampling request did not start"
            assert not second_persisted.wait(0.5), (
                "second sampling update persisted before the first runtime publish"
            )
        finally:
            release_first.set()
        first_response = first.result(timeout=5)
        second_response = second.result(timeout=5)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    expected = {
        "enabled": False,
        "top_k": 10,
        "sample_k": 2,
        "temperature": 1.2,
    }
    assert persisted["surfacing"]["sampling"] == expected
    assert runtime["surfacing"]["sampling"] is old_sampling
    assert old_sampling == expected


@pytest.mark.asyncio
async def test_retired_purge_endpoint_never_deletes_memory(monkeypatch):
    class ExplodingBucketManager:
        def __getattr__(self, name):
            raise AssertionError(f"retired purge endpoint touched bucket manager: {name}")

    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        buckets_web.sh, "bucket_mgr", ExplodingBucketManager(), raising=False
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("POST", "/api/buckets/purge")](
        JsonRequest({"ids": ["memory-1"]})
    )
    payload = _json(response)

    assert response.status_code == 410
    assert payload["error"] == "physical_deletion_forbidden"
    assert "Markdown 文件会继续保留" in payload["message"]


# ============================================================
# 时区设置：只给日期不写时区时按它理解（Letter 定时锁等）
# ============================================================

@pytest.mark.asyncio
async def test_timezone_persists_and_is_hot_applied(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    runtime = {"transport": "streamable-http"}
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", runtime)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"timezone": "America/New_York", "persist": True})
    )
    payload = _json(response)
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert "timezone" in payload["updated"]
    assert persisted["timezone"] == "America/New_York"
    # 时区是热更新项：不需要重启
    assert config_api.sh.config["timezone"] == "America/New_York"


@pytest.mark.asyncio
async def test_invalid_timezone_is_rejected_instead_of_silently_falling_back(
    monkeypatch, tmp_path
):
    """写进去一个解析不了的名字，之后每次解析日期都会静默回退 +08:00，
    用户却以为自己设置成功了——必须当场拒绝。"""
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(config_api.sh, "config", {"transport": "streamable-http"})
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setattr(utils, "config_file_path", lambda: str(config_path))
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/config")](
        JsonRequest({"timezone": "Nowhere/Fake", "persist": True})
    )

    assert response.status_code == 400
    assert "无法识别时区" in _json(response)["error"]
    assert not config_path.exists() or "timezone" not in (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    )
