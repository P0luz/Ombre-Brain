import json
import os
from types import SimpleNamespace

import pytest

import web.config_api as config_api
from utils import get_ai_name, load_config


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


class JsonRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_env_config_can_clear_ai_display_name(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_NAME", "trainsprout")
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(config_api.sh, "config", {})

    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest({"updates": {"AI_NAME": ""}})
    )
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert "AI_NAME" in payload["updated"]
    assert os.environ.get("AI_NAME") is None
    assert get_ai_name() == "AI"


@pytest.mark.asyncio
async def test_compress_runtime_reload_survives_config_persistence_failure(
    monkeypatch, tmp_path
):
    import openai

    created_clients = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created_clients.append(self)

    old_client = object()
    dehydrator = SimpleNamespace(
        api_key="old-key",
        base_url="https://old.example/v1",
        model="old-model",
        timeout_seconds=60.0,
        api_format="openai_compat",
        api_available=True,
        client=old_client,
    )
    runtime_config = {
        "dehydration": {
            "api_key": "old-key",
            "base_url": "https://old.example/v1",
            "model": "old-model",
            "timeout_seconds": 60,
            "api_format": "openai_compat",
        }
    }
    persistence_calls = []

    def fail_config_persistence(mutate):
        persisted = {}
        mutate(persisted)
        persistence_calls.append(persisted)
        raise OSError("Device or resource busy")

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env")
    )
    monkeypatch.setattr(config_api.sh, "config", runtime_config)
    monkeypatch.setattr(config_api.sh, "dehydrator", dehydrator)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", fail_config_persistence)
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setenv("OMBRE_COMPRESS_API_KEY", "old-key")
    monkeypatch.setenv("OMBRE_COMPRESS_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("OMBRE_COMPRESS_MODEL", "old-model")
    monkeypatch.setenv("OMBRE_COMPRESS_TIMEOUT_SECONDS", "60")

    updates = {
        # Deliberately not client-construction order: the route must stage the
        # complete batch and build exactly one client from the final values.
        "OMBRE_COMPRESS_MODEL": "new-model",
        "OMBRE_COMPRESS_API_KEY": "new-key",
        "OMBRE_COMPRESS_TIMEOUT_SECONDS": "45",
        "OMBRE_COMPRESS_BASE_URL": "https://new.example/v1",
    }
    mcp = FakeMCP()
    config_api.register(mcp)

    response = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest({"updates": updates})
    )
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["partial"] is True
    assert payload["updated"] == list(updates)
    assert payload["persisted"] == []
    assert any(
        "config.yaml 持久化失败" in warning
        and "运行时已生效" in warning
        and "重启后可能恢复旧值" in warning
        for warning in payload["warnings"]
    )
    assert len(persistence_calls) == 1
    assert persistence_calls[0]["dehydration"] == {
        "model": "new-model",
        "api_key": "new-key",
        "timeout_seconds": "45",
        "base_url": "https://new.example/v1",
    }

    assert runtime_config["dehydration"]["api_key"] == "new-key"
    assert runtime_config["dehydration"]["base_url"] == "https://new.example/v1"
    assert runtime_config["dehydration"]["model"] == "new-model"
    assert dehydrator.api_key == "new-key"
    assert dehydrator.base_url == "https://new.example/v1"
    assert dehydrator.model == "new-model"
    assert dehydrator.timeout_seconds == 45.0
    assert dehydrator.api_available is True
    assert len(created_clients) == 1
    assert dehydrator.client is created_clients[0]
    assert created_clients[0].kwargs == {
        "api_key": "new-key",
        "base_url": "https://new.example/v1",
        "timeout": 45.0,
        # 重试归 Dehydrator._chat 管；SDK 再自己重试就是 3x3=9 次尝试
        "max_retries": 0,
    }
    assert os.environ["OMBRE_COMPRESS_API_KEY"] == "new-key"
    assert os.environ["OMBRE_COMPRESS_BASE_URL"] == "https://new.example/v1"


@pytest.mark.asyncio
async def test_compress_client_rebuild_failure_is_not_reported_as_success(
    monkeypatch, tmp_path
):
    import openai

    def fail_client_rebuild(**kwargs):
        raise ValueError("invalid base URL")

    persistence_called = False

    def persist_unexpectedly(_mutate):
        nonlocal persistence_called
        persistence_called = True

    old_client = object()
    dehydrator = SimpleNamespace(
        api_key="old-key",
        base_url="https://old.example/v1",
        model="old-model",
        timeout_seconds=60.0,
        api_format="openai_compat",
        api_available=True,
        client=old_client,
    )
    runtime_config = {
        "dehydration": {
            "api_key": "old-key",
            "base_url": "https://old.example/v1",
            "model": "old-model",
            "timeout_seconds": 60,
            "api_format": "openai_compat",
        }
    }

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env")
    )
    monkeypatch.setattr(config_api.sh, "config", runtime_config)
    monkeypatch.setattr(config_api.sh, "dehydrator", dehydrator)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", persist_unexpectedly)
    monkeypatch.setattr(openai, "AsyncOpenAI", fail_client_rebuild)
    monkeypatch.setenv("OMBRE_COMPRESS_API_KEY", "old-key")
    monkeypatch.setenv("OMBRE_COMPRESS_BASE_URL", "https://old.example/v1")

    mcp = FakeMCP()
    config_api.register(mcp)
    response = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest(
            {
                "updates": {
                    "OMBRE_COMPRESS_API_KEY": "new-key",
                    "OMBRE_COMPRESS_BASE_URL": "not-a-valid-base-url",
                }
            }
        )
    )
    payload = json.loads(response.body)

    assert payload["ok"] is False
    assert payload["partial"] is False
    assert payload["updated"] == []
    assert payload["persisted"] == []
    assert "压缩配置热更新失败" in payload["error"]
    assert "ValueError: invalid base URL" in payload["error"]
    assert persistence_called is False
    assert runtime_config["dehydration"]["api_key"] == "old-key"
    assert runtime_config["dehydration"]["base_url"] == "https://old.example/v1"
    assert dehydrator.api_key == "old-key"
    assert dehydrator.base_url == "https://old.example/v1"
    assert dehydrator.client is old_client
    assert os.environ["OMBRE_COMPRESS_API_KEY"] == "old-key"
    assert os.environ["OMBRE_COMPRESS_BASE_URL"] == "https://old.example/v1"


def test_v1_environment_names_remain_compatible(request, monkeypatch, tmp_path):
    # load_config() 把 legacy PASSWORD 映射成 OMBRE_DASHBOARD_PASSWORD 时是直接写
    # os.environ 的。monkeypatch.delenv 在变量原本就不存在时不记录任何东西，于是
    # 还原不了这个「测试期间才被创建」的变量——它会泄漏到本次 session 的后续用例，
    # 让 web/auth 的用例在随机序下变红（env 密码模式会让请求在 JSON 校验前短路）。
    request.addfinalizer(
        lambda: os.environ.pop("OMBRE_DASHBOARD_PASSWORD", None)
    )
    monkeypatch.delenv("OMBRE_COMPRESS_API_KEY", raising=False)
    monkeypatch.delenv("OMBRE_COMPRESS_BASE_URL", raising=False)
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("OMBRE_API_KEY", "legacy-key")
    monkeypatch.setenv("OMBRE_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("PASSWORD", "legacy-password")
    monkeypatch.setenv("OMBRE_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.delenv("OMBRE_BUCKETS_DIR", raising=False)

    config = load_config(str(tmp_path / "missing-config.yaml"))

    assert config["dehydration"]["api_key"] == "legacy-key"
    assert config["dehydration"]["base_url"] == "https://legacy.example/v1"
    assert os.environ["OMBRE_DASHBOARD_PASSWORD"] == "legacy-password"
    assert config["media_dir"] == str(tmp_path / "vault" / "_media")


@pytest.mark.asyncio
async def test_embedding_provider_tuple_rebuilds_and_persists_once(
    monkeypatch, tmp_path
):
    runtime_config = {
        "embedding": {
            "enabled": True,
            "api_key": "old-key",
            "api_format": "ollama",
            "base_url": "",
            "model": "bge-m3",
        }
    }
    rebuild_snapshots = []
    persisted_configs = []

    def rebuild_once():
        rebuild_snapshots.append(dict(runtime_config["embedding"]))
        return SimpleNamespace(enabled=True)

    def persist_once(mutate):
        saved = {}
        mutate(saved)
        persisted_configs.append(saved)

    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        config_api.sh, "_project_env_path", lambda: str(tmp_path / ".env")
    )
    monkeypatch.setattr(config_api.sh, "config", runtime_config)
    monkeypatch.setattr(
        config_api.sh, "embedding_engine", SimpleNamespace(enabled=True)
    )
    monkeypatch.setattr(config_api, "_rebuild_embedding_runtime", rebuild_once)
    monkeypatch.setattr(config_api, "atomic_update_config_yaml", persist_once)

    updates = {
        "OMBRE_EMBED_API_KEY": "new-key",
        "OMBRE_EMBED_BASE_URL": "https://api.siliconflow.cn/v1",
        "OMBRE_EMBED_MODEL": "BAAI/bge-m3",
        "OMBRE_EMBED_FORMAT": "openai_compat",
    }
    mcp = FakeMCP()
    config_api.register(mcp)
    response = await mcp.routes[("POST", "/api/env-config")](
        JsonRequest({"updates": updates})
    )
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["partial"] is False
    assert payload["updated"] == list(updates)
    assert len(rebuild_snapshots) == 1
    assert rebuild_snapshots[0] == {
        "enabled": True,
        "api_key": "new-key",
        "api_format": "openai_compat",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "BAAI/bge-m3",
    }
    assert len(persisted_configs) == 1
    assert persisted_configs[0]["embedding"]["api_key"] == "new-key"
    assert persisted_configs[0]["embedding"]["base_url"] == updates[
        "OMBRE_EMBED_BASE_URL"
    ]
    assert persisted_configs[0]["embedding"]["model"] == "BAAI/bge-m3"
    assert persisted_configs[0]["embedding"]["api_format"] == "openai_compat"


# ============================================================
# ai_name 的两级来源：config.yaml（随 vault 持久化）→ AI_NAME 环境变量 → "AI"
#
# 为什么 config 排在环境变量前面：Docker 下 config.yaml 落在挂载的 vault 里，
# 容器重建/重启都不丢；环境变量得靠 compose 逐个透传，漏一个就静默退回默认名
# （带锁 Letter 曾因此在 Docker 上完全无法创建）。
# ============================================================

def _reset_ai_name_cache():
    """_config_ai_name 按 (路径, mtime) 缓存，测试之间必须清掉避免互相污染。"""
    import utils
    utils._ai_name_cache = None


def _point_config_at(monkeypatch, path):
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(path))
    _reset_ai_name_cache()


def test_config_ai_name_wins_over_env(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('ai_name: "Ombre"\n', encoding="utf-8")
    monkeypatch.setenv("AI_NAME", "从环境变量来的名字")
    _point_config_at(monkeypatch, cfg)

    assert get_ai_name() == "Ombre"


def test_empty_config_ai_name_falls_back_to_env(monkeypatch, tmp_path):
    """默认空值：config 里留空表示「没配」，行为与改动前完全一致。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text('ai_name: ""\n', encoding="utf-8")
    monkeypatch.setenv("AI_NAME", "环境变量兜底")
    _point_config_at(monkeypatch, cfg)

    assert get_ai_name() == "环境变量兜底"


def test_no_config_and_no_env_falls_back_to_ai(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("log_level: INFO\n", encoding="utf-8")
    monkeypatch.delenv("AI_NAME", raising=False)
    _point_config_at(monkeypatch, cfg)

    assert get_ai_name() == "AI"


def test_missing_config_file_does_not_break_signing(monkeypatch, tmp_path):
    """配置文件不存在时不能抛异常——署名逻辑遍布 letter/prompt/Dashboard。"""
    monkeypatch.setenv("AI_NAME", "仍然可用")
    _point_config_at(monkeypatch, tmp_path / "不存在的.yaml")

    assert get_ai_name() == "仍然可用"


def test_broken_config_falls_back_instead_of_raising(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("这不是: 合法的: yaml: [[[\n", encoding="utf-8")
    monkeypatch.setenv("AI_NAME", "坏配置也要能署名")
    _point_config_at(monkeypatch, cfg)

    assert get_ai_name() == "坏配置也要能署名"


def test_config_edit_invalidates_cache(monkeypatch, tmp_path):
    """改完配置必须立刻生效，不能被 mtime 缓存挡住。"""
    import os as _os

    cfg = tmp_path / "config.yaml"
    cfg.write_text('ai_name: "旧名字"\n', encoding="utf-8")
    monkeypatch.delenv("AI_NAME", raising=False)
    _point_config_at(monkeypatch, cfg)
    assert get_ai_name() == "旧名字"

    cfg.write_text('ai_name: "新名字"\n', encoding="utf-8")
    # 同秒内改写可能 mtime 不变，显式推进以确保测的是缓存失效而不是时钟精度
    stat = _os.stat(cfg)
    _os.utime(cfg, (stat.st_atime + 5, stat.st_mtime + 5))

    assert get_ai_name() == "新名字"
