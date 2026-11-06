"""静态 Token 与 OAuth + Token 共存鉴权回归。

覆盖：
- MCPAuthMiddleware 在 token 模式下接受 Authorization: Bearer / Ombre-MCP-Token 两种请求头
- 错误或缺失 token 一律 401
- token 模式下 OAuth 的 discovery/register/authorize/token 路由全部 404（互斥性）
- oauth（默认）/ off 两个既有模式不受影响（回归）
- mcp_auth_mode=token 但未配置密钥时，load_config() 自动回退为 oauth 并告警
- _is_valid_static_mcp_token 的 env 优先级与常量时间比较行为
- hybrid 模式同时接受 OAuth Bearer 与静态 Bearer，并保留 OAuth discovery/DCR
- 静态 Token 为空时 load_config() 的告警不抢先配置日志，随后 setup_logging() 照常写 server.log
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import web.oauth as oauth_mod
from server_app import HTTPRuntimeSettings, MCPAuthMiddleware


class RecordingASGIApp:
    def __init__(self):
        self.scopes = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _collect_into(messages):
    async def send(message):
        messages.append(message)

    return send


async def _discard_send(_message):
    return None


def _scope(headers):
    return {
        "type": "http",
        "scheme": "https",
        "path": "/mcp",
        "headers": [(b"host", b"ombre.example"), *headers],
    }


# --- MCPAuthMiddleware in token mode ---------------------------------------

@pytest.mark.asyncio
async def test_token_mode_accepts_correct_bearer_token():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="token",
        token_validator=lambda token, **_kwargs: token == "secret-1",
    )
    scope = _scope([(b"authorization", b"Bearer secret-1")])

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes == [scope]


@pytest.mark.asyncio
async def test_token_mode_accepts_correct_custom_header():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="token",
        token_validator=lambda token, **_kwargs: token == "secret-1",
    )
    scope = _scope([(b"ombre-mcp-token", b"secret-1")])

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes == [scope]


@pytest.mark.asyncio
async def test_token_mode_rejects_wrong_token_via_either_header():
    for headers in (
        [(b"authorization", b"Bearer wrong")],
        [(b"ombre-mcp-token", b"wrong")],
        [],  # missing entirely
    ):
        downstream = RecordingASGIApp()
        middleware = MCPAuthMiddleware(
            downstream,
            auth_required=True,
            auth_mode="token",
            token_validator=lambda token, **_kwargs: token == "secret-1",
        )
        messages = []

        await middleware(_scope(headers), _empty_receive, _collect_into(messages))

        assert downstream.scopes == []
        assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_token_mode_401_challenge_has_no_oauth_resource_metadata():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="token",
        token_validator=lambda *_a, **_k: False,
    )
    messages = []

    await middleware(_scope([]), _empty_receive, _collect_into(messages))

    payload = json.loads(messages[1]["body"])
    assert "resource_metadata" not in payload
    headers = dict(messages[0]["headers"])
    assert b"resource_metadata" not in headers[b"www-authenticate"]


@pytest.mark.asyncio
async def test_oauth_mode_ignores_custom_header_fallback():
    """默认 oauth 模式回归：自定义请求头不应绕过 OAuth Bearer 校验。"""
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="oauth",
        token_validator=lambda *_a, **_k: False,
    )
    messages = []

    await middleware(
        _scope([(b"ombre-mcp-token", b"whatever")]), _empty_receive, _collect_into(messages)
    )

    assert downstream.scopes == []
    assert messages[0]["status"] == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token", "expected"),
    [("oauth-access", True), ("static-secret", True), ("wrong", False)],
)
async def test_hybrid_bearer_runs_and_combines_both_validators(token, expected):
    downstream = RecordingASGIApp()
    calls = []

    def oauth_validator(value, **_kwargs):
        calls.append(("oauth", value))
        return value == "oauth-access"

    def static_validator(value, **_kwargs):
        calls.append(("static", value))
        return value == "static-secret"

    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="hybrid",
        token_validator=oauth_validator,
        static_token_validator=static_validator,
    )
    messages = []
    scope = _scope([(b"authorization", f"Bearer {token}".encode())])

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert calls == [("oauth", token), ("static", token)]
    assert bool(downstream.scopes) is expected
    if not expected:
        assert messages[0]["status"] == 401
        payload = json.loads(messages[1]["body"])
        assert payload["resource_metadata"].endswith(
            "/.well-known/oauth-protected-resource/mcp"
        )


@pytest.mark.asyncio
async def test_hybrid_custom_header_uses_only_static_validator():
    downstream = RecordingASGIApp()
    oauth_calls = []
    static_calls = []
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="hybrid",
        token_validator=lambda token, **_kwargs: oauth_calls.append(token) or True,
        static_token_validator=lambda token, **_kwargs: static_calls.append(token)
        or token == "static-secret",
    )

    await middleware(
        _scope([(b"ombre-mcp-token", b"static-secret")]),
        _empty_receive,
        _discard_send,
    )

    assert downstream.scopes
    assert oauth_calls == []
    assert static_calls == ["static-secret"]


@pytest.mark.asyncio
async def test_hybrid_custom_header_does_not_accept_oauth_access_token():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        auth_mode="hybrid",
        token_validator=lambda token, **_kwargs: token == "oauth-access",
        static_token_validator=lambda *_args, **_kwargs: False,
    )
    messages = []

    await middleware(
        _scope([(b"ombre-mcp-token", b"oauth-access")]),
        _empty_receive,
        _collect_into(messages),
    )

    assert downstream.scopes == []
    assert messages[0]["status"] == 401
    assert b"resource_metadata" in dict(messages[0]["headers"])[b"www-authenticate"]


def test_http_runtime_settings_defaults_and_reads_auth_mode():
    assert HTTPRuntimeSettings.from_config({}).auth_mode == "oauth"
    assert HTTPRuntimeSettings.from_config({"mcp_auth_mode": "token"}).auth_mode == "token"
    assert HTTPRuntimeSettings.from_config({"mcp_auth_mode": "hybrid"}).auth_mode == "hybrid"
    # 非法值一律回退 oauth，不能悄悄变成拒绝一切或全放行
    assert HTTPRuntimeSettings.from_config({"mcp_auth_mode": "bogus"}).auth_mode == "oauth"


# --- OAuth route visibility (mutual exclusivity) ----------------------------

class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            for method in methods:
                self.routes[(method, path)] = fn
            return fn

        return decorator


class FakeUrl:
    scheme = "https"
    netloc = "ombre.example"


class JsonRequest:
    def __init__(self, body=None, *, headers=None, path_params=None, method="POST"):
        self._body = {} if body is None else body
        self.headers = headers or {"content-type": "application/json", "host": "ombre.example"}
        self.url = FakeUrl()
        self.path_params = path_params or {}
        self.method = method
        self.query_params = {}
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_oauth_routes_404_when_auth_mode_is_token(monkeypatch, tmp_path):
    oauth_mod._oauth_clients.clear()
    oauth_mod._oauth_codes.clear()
    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_token_resources.clear()
    monkeypatch.setattr(oauth_mod.sh, "config", {
        "buckets_dir": str(tmp_path / "buckets"),
        "mcp_require_auth": True,
        "mcp_auth_mode": "token",
    })

    mcp = FakeMCP()
    oauth_mod.register(mcp)

    for key in (
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("POST", "/oauth/register"),
        ("GET", "/oauth/authorize"),
        ("POST", "/oauth/token"),
    ):
        response = await mcp.routes[key](JsonRequest(method=key[0]))
        assert response.status_code == 404, f"{key} should 404 when mcp_auth_mode=token"


@pytest.mark.asyncio
async def test_hybrid_mode_keeps_oauth_discovery_and_dcr_active(monkeypatch, tmp_path):
    oauth_mod._oauth_clients.clear()
    oauth_mod._oauth_codes.clear()
    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_token_resources.clear()
    monkeypatch.setattr(
        oauth_mod.sh,
        "config",
        {
            "buckets_dir": str(tmp_path / "buckets"),
            "mcp_require_auth": True,
            "mcp_auth_mode": "hybrid",
        },
    )
    mcp = FakeMCP()
    oauth_mod.register(mcp)

    metadata = await mcp.routes[("GET", "/.well-known/oauth-protected-resource")](
        JsonRequest(method="GET")
    )
    registered = await mcp.routes[("POST", "/oauth/register")](
        JsonRequest(
            {
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "client_name": "Claude",
            }
        )
    )

    assert metadata.status_code == 200
    assert registered.status_code == 201
    assert json.loads(registered.body)["client_id"]


def test_oauth_required_from_config_supports_oauth_and_hybrid(monkeypatch):
    monkeypatch.setattr(oauth_mod.sh, "config", {"mcp_require_auth": True})
    assert oauth_mod._oauth_required_from_config() is True

    monkeypatch.setattr(
        oauth_mod.sh, "config", {"mcp_require_auth": True, "mcp_auth_mode": "hybrid"}
    )
    assert oauth_mod._oauth_required_from_config() is True

    monkeypatch.setattr(
        oauth_mod.sh, "config", {"mcp_require_auth": True, "mcp_auth_mode": "oauth"}
    )
    assert oauth_mod._oauth_required_from_config() is True

    monkeypatch.setattr(
        oauth_mod.sh, "config", {"mcp_require_auth": True, "mcp_auth_mode": "token"}
    )
    assert oauth_mod._oauth_required_from_config() is False

    monkeypatch.setattr(
        oauth_mod.sh, "config", {"mcp_require_auth": False, "mcp_auth_mode": "token"}
    )
    assert oauth_mod._oauth_required_from_config() is False


# --- _is_valid_static_mcp_token ---------------------------------------------

def test_static_token_validator_rejects_empty_or_unconfigured(monkeypatch):
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod.sh, "config", {"mcp_token": ""})
    assert oauth_mod._is_valid_static_mcp_token("") is False
    assert oauth_mod._is_valid_static_mcp_token("anything") is False


def test_static_token_validator_matches_configured_secret(monkeypatch):
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod.sh, "config", {"mcp_token": "my-secret"})
    assert oauth_mod._is_valid_static_mcp_token("my-secret") is True
    assert oauth_mod._is_valid_static_mcp_token("wrong") is False


def test_static_token_validator_env_takes_priority_over_config(monkeypatch):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "env-secret")
    monkeypatch.setattr(oauth_mod.sh, "config", {"mcp_token": "config-secret"})
    assert oauth_mod._is_valid_static_mcp_token("env-secret") is True
    assert oauth_mod._is_valid_static_mcp_token("config-secret") is False


def test_static_token_validator_handles_unicode_without_type_error(monkeypatch):
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(oauth_mod.sh, "config", {"mcp_token": "密钥-一"})

    assert oauth_mod._is_valid_static_mcp_token("密钥-一") is True
    assert oauth_mod._is_valid_static_mcp_token("密钥-二") is False


# --- load_config() safety fallback ------------------------------------------

def test_load_config_falls_back_to_oauth_when_token_mode_has_no_secret(
    monkeypatch, tmp_path, caplog
):
    from utils import load_config

    monkeypatch.delenv("OMBRE_MCP_AUTH_MODE", raising=False)
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
buckets_dir: buckets
mcp_auth_mode: "token"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(str(cfg_path))

    assert config["mcp_auth_mode"] == "oauth"


def test_load_config_keeps_token_mode_when_secret_is_present(monkeypatch, tmp_path):
    from utils import load_config

    monkeypatch.delenv("OMBRE_MCP_AUTH_MODE", raising=False)
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "env-secret")

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        """
buckets_dir: buckets
mcp_auth_mode: "token"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(str(cfg_path))

    assert config["mcp_auth_mode"] == "token"
    assert config["mcp_token"] == "env-secret"


def test_load_config_keeps_hybrid_without_static_secret(monkeypatch, tmp_path, caplog):
    from utils import load_config

    monkeypatch.delenv("OMBRE_MCP_AUTH_MODE", raising=False)
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        'buckets_dir: buckets\nmcp_auth_mode: "hybrid"\n', encoding="utf-8"
    )

    config = load_config(str(cfg_path))

    assert config["mcp_auth_mode"] == "hybrid"
    assert config["mcp_token"] == ""
    assert "OAuth 仍可用" in caplog.text


# 和 server.py 启动顺序一致：先 load_config()，再 setup_logging()。子进程里跑，
# 不动本进程的全局 logging（pytest 的 caplog 也挂在根 logger 上）。
_LOAD_CONFIG_THEN_SETUP_LOGGING = """
import logging
import sys

sys.path.insert(0, sys.argv[1])
from utils import load_config, setup_logging

config = load_config(sys.argv[2])
setup_logging(config.get("log_level", "INFO"))
logging.getLogger("ombre_brain").info("startup-info-probe")
print(config["mcp_auth_mode"])
"""


@pytest.mark.parametrize(
    ("mode", "expected_mode", "warning_text"),
    [
        ("hybrid", "hybrid", "OAuth 仍可用"),
        ("token", "oauth", "已自动回退为 oauth 模式"),
    ],
    ids=["hybrid", "token"],
)
def test_empty_static_token_warning_does_not_preempt_setup_logging(
    tmp_path, mode, expected_mode, warning_text
):
    # 换环境变量口令会清空 config.yaml 的静态 Token，之后每次启动都走这条告警。
    # 告警若用根 logger 的模块级函数，会隐式 basicConfig，setup_logging 的配置随之落空：
    # 格式退回 WARNING:root:、INFO 丢失、<数据目录>/.logs/server.log 不再写。
    buckets = tmp_path / "buckets"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f'buckets_dir: "{buckets.as_posix()}"\nmcp_auth_mode: "{mode}"\n',
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("OMBRE_MCP_TOKEN", "OMBRE_MCP_AUTH_MODE", "OMBRE_LOG_DIR", "OMBRE_LOG_FILE")
    }
    env["OMBRE_BUCKETS_DIR"] = str(buckets)
    env["OMBRE_VAULT_DIR"] = str(buckets)
    env["PYTHONIOENCODING"] = "utf-8"
    src_dir = Path(__file__).resolve().parent.parent / "src"

    proc = subprocess.run(
        [sys.executable, "-c", _LOAD_CONFIG_THEN_SETUP_LOGGING, str(src_dir), str(cfg_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected_mode
    log_text = (buckets / ".logs" / "server.log").read_text(encoding="utf-8")
    assert "ombre_brain INFO: startup-info-probe" in log_text
    assert "ombre_brain INFO: startup-info-probe" in proc.stderr
    # setup_logging() 之前根 logger 没有 handler，告警经 logging.lastResort 写到 stderr，仍看得见。
    assert warning_text in proc.stderr
    assert "WARNING:root:" not in proc.stderr


def test_load_config_accepts_hybrid_environment_override(monkeypatch, tmp_path):
    from utils import load_config

    monkeypatch.setenv("OMBRE_MCP_AUTH_MODE", "hybrid")
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "env-secret")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("buckets_dir: buckets\n", encoding="utf-8")

    config = load_config(str(cfg_path))

    assert config["mcp_auth_mode"] == "hybrid"
    assert config["mcp_token"] == "env-secret"


def test_load_config_default_auth_mode_is_oauth(monkeypatch, tmp_path):
    from utils import load_config

    monkeypatch.delenv("OMBRE_MCP_AUTH_MODE", raising=False)
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("buckets_dir: buckets\n", encoding="utf-8")

    config = load_config(str(cfg_path))

    assert config["mcp_auth_mode"] == "oauth"
    assert config["mcp_token"] == ""
