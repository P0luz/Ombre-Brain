import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette

from server_app import (
    DEFAULT_MAX_MCP_REQUEST_BYTES,
    HTTPRuntimeSettings,
    MCPAcceptShim,
    MCPAuthMiddleware,
    RuntimeLifecycle,
    build_http_app,
    install_runtime_lifespan,
    merge_mcp_tool_registries,
)


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def _record(self, level, message, *args):
        self.messages.append((level, message % args if args else message))

    def debug(self, message, *args):
        self._record("debug", message, *args)

    def info(self, message, *args):
        self._record("info", message, *args)

    def warning(self, message, *args):
        self._record("warning", message, *args)


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


@pytest.mark.parametrize(
    ("config", "auth_required", "limit"),
    [
        ({}, True, DEFAULT_MAX_MCP_REQUEST_BYTES),
        ({"mcp_require_auth": "false", "limits": {"max_mcp_request_bytes": 0}}, False, 0),
        ({"limits": {"max_mcp_request_bytes": "1024"}}, True, 1024),
        ({"limits": {"max_mcp_request_bytes": -1}}, True, DEFAULT_MAX_MCP_REQUEST_BYTES),
        ({"limits": {"max_mcp_request_bytes": "bad"}}, True, DEFAULT_MAX_MCP_REQUEST_BYTES),
    ],
)
def test_http_runtime_settings_are_normalized(config, auth_required, limit):
    settings = HTTPRuntimeSettings.from_config(config)

    assert settings.auth_required is auth_required
    assert settings.max_request_bytes == limit


def test_merge_mcp_tool_registries_keeps_one_public_manifest():
    primary = SimpleNamespace(
        _tool_manager=SimpleNamespace(_tools={"breath": object()})
    )
    extra = SimpleNamespace(
        _tool_manager=SimpleNamespace(_tools={"dream": object(), "pulse": object()})
    )

    count = merge_mcp_tool_registries(primary, extra)

    assert count == 2
    assert set(primary._tool_manager._tools) == {"breath", "dream", "pulse"}


@pytest.mark.asyncio
async def test_accept_shim_adds_both_mcp_media_types():
    downstream = RecordingASGIApp()
    middleware = MCPAcceptShim(downstream)
    messages = []
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"accept", b"application/json")],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    forwarded = dict(downstream.scopes[0]["headers"])[b"accept"]
    assert b"application/json" in forwarded
    assert b"text/event-stream" in forwarded


@pytest.mark.asyncio
async def test_accept_shim_leaves_non_mcp_routes_unchanged():
    downstream = RecordingASGIApp()
    middleware = MCPAcceptShim(downstream)
    scope = {
        "type": "http",
        "path": "/health",
        "headers": [(b"accept", b"application/json")],
    }

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes[0] is scope


@pytest.mark.asyncio
async def test_auth_middleware_rejects_missing_token_with_canonical_metadata_url():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
    )
    messages = []
    scope = {
        "type": "http",
        "scheme": "http",
        "path": "/mcp",
        "headers": [
            (b"host", b"internal:8000"),
            (b"x-forwarded-proto", b"https, http"),
            (b"x-forwarded-host", b"ombre.example, proxy.local"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 401
    payload = json.loads(messages[1]["body"])
    assert payload["resource_metadata"] == (
        "https://ombre.example/.well-known/oauth-protected-resource/mcp"
    )


@pytest.mark.asyncio
async def test_auth_middleware_validates_token_against_exact_resource():
    downstream = RecordingASGIApp()
    seen = {}

    def validator(token, *, resource):
        seen.update(token=token, resource=resource)
        return True

    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=validator,
    )
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/mcp/",
        "headers": [
            (b"host", b"ombre.example"),
            (b"authorization", b"Bearer token-1"),
        ],
    }

    await middleware(scope, _empty_receive, _discard_send)

    assert seen == {"token": "token-1", "resource": "https://ombre.example/mcp"}
    assert downstream.scopes == [scope]


@pytest.mark.asyncio
async def test_auth_middleware_accepts_only_the_configured_gateway_key(monkeypatch):
    monkeypatch.setenv("OMBRE_GATEWAY_API_KEY", "gateway-secret")
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
    )
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/mcp",
        "headers": [
            (b"host", b"ombre.example"),
            (b"x-ombre-gateway-key", b"gateway-secret"),
        ],
    }

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes == [scope]


@pytest.mark.asyncio
async def test_auth_middleware_rejects_an_incorrect_gateway_key(monkeypatch):
    monkeypatch.setenv("OMBRE_GATEWAY_API_KEY", "gateway-secret")
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
    )
    messages = []
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/mcp",
        "headers": [
            (b"host", b"ombre.example"),
            (b"x-ombre-gateway-key", b"wrong-key"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 401


@pytest.mark.asyncio
async def test_auth_middleware_can_be_explicitly_disabled():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=False,
        token_validator=lambda *_args, **_kwargs: False,
    )
    scope = {"type": "http", "path": "/mcp", "headers": []}

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes == [scope]


class RecordingService:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    async def start(self):
        self.events.append(f"{self.name}:start")

    async def stop(self):
        self.events.append(f"{self.name}:stop")


@pytest.mark.asyncio
async def test_runtime_lifecycle_starts_and_stops_every_owned_service(tmp_path):
    events = []
    logger = RecordingLogger()
    marker = tmp_path / ".boot_fails"
    marker.write_text("2", encoding="utf-8")

    async def ollama_start():
        events.append("ollama:start")

    async def ollama_stop():
        events.append("ollama:stop")

    lifecycle = RuntimeLifecycle(
        logger=logger,
        decay_engine=RecordingService("decay", events),
        embedding_outbox=RecordingService("outbox", events),
        ensure_ollama_child=ollama_start,
        stop_ollama_child=ollama_stop,
        load_tunnel_config=lambda: {"auto_start": True, "token": "tunnel-token"},
        start_tunnel=lambda token: (events.append(f"tunnel:start:{token}") or True, "ok"),
        stop_tunnel=lambda: events.append("tunnel:stop"),
        restart_github_auto_task=lambda interval: events.append(f"github:{interval}"),
        github_auto_interval=9,
        boot_marker_path=str(marker),
    )

    await lifecycle.start()
    await lifecycle.start()
    await lifecycle.stop()
    await lifecycle.stop()

    assert events == [
        "tunnel:start:tunnel-token",
        "github:9",
        "decay:start",
        "ollama:start",
        "outbox:start",
        "github:0",
        "outbox:stop",
        "decay:stop",
        "ollama:stop",
        "tunnel:stop",
    ]
    assert marker.read_text(encoding="utf-8") == "0"


@pytest.mark.asyncio
async def test_runtime_lifecycle_cancels_keepalive_on_shutdown():
    lifecycle = RuntimeLifecycle(
        logger=RecordingLogger(),
        keepalive_url="http://127.0.0.1:1/health",
        keepalive_initial_delay=3600,
    )

    await lifecycle.start()
    task = lifecycle._keepalive_task
    await asyncio.sleep(0)
    await lifecycle.stop()

    assert task is not None
    assert task.done()
    assert lifecycle._keepalive_task is None


@pytest.mark.asyncio
async def test_runtime_lifecycle_logs_optional_service_failures_without_leaking():
    logger = RecordingLogger()

    class FailingService:
        async def start(self):
            raise RuntimeError("start failed")

        async def stop(self):
            raise RuntimeError("stop failed")

    lifecycle = RuntimeLifecycle(
        logger=logger,
        decay_engine=FailingService(),
        embedding_outbox=FailingService(),
        load_tunnel_config=lambda: (_ for _ in ()).throw(RuntimeError("tunnel failed")),
        start_tunnel=lambda _token: (True, "unused"),
        stop_tunnel=lambda: (_ for _ in ()).throw(RuntimeError("stop tunnel failed")),
    )

    await lifecycle.start()
    await lifecycle.stop()

    warnings = "\n".join(message for level, message in logger.messages if level == "warning")
    assert "tunnel auto-start failed" in warnings
    assert "decay engine start failed" in warnings
    assert "embedding outbox stop failed" in warnings
    assert "tunnel stop failed" in warnings


@pytest.mark.asyncio
async def test_runtime_lifespan_composes_with_parent_lifespan():
    events = []

    @asynccontextmanager
    async def parent(_app):
        events.append("parent:start")
        try:
            yield
        finally:
            events.append("parent:stop")

    class FakeLifecycle:
        async def start(self):
            events.append("runtime:start")

        async def stop(self):
            events.append("runtime:stop")

    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=parent))
    install_runtime_lifespan(app, FakeLifecycle())

    async with app.router.lifespan_context(app):
        events.append("body")

    assert events == [
        "parent:start",
        "runtime:start",
        "body",
        "runtime:stop",
        "parent:stop",
    ]


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_build_http_app_uses_same_managed_stack_for_both_http_transports(transport):
    class FakeMCP:
        def streamable_http_app(self):
            return Starlette()

        def sse_app(self):
            return Starlette()

    lifecycle = RuntimeLifecycle(logger=RecordingLogger())
    settings = HTTPRuntimeSettings(auth_required=False, max_request_bytes=2048)

    app = build_http_app(
        FakeMCP(),
        transport,
        settings=settings,
        token_validator=lambda *_args, **_kwargs: False,
        lifecycle=lifecycle,
    )

    middleware_names = {item.cls.__name__ for item in app.user_middleware}
    assert middleware_names >= {
        "CORSMiddleware",
        "MCPRequestBodyLimitMiddleware",
        "MCPAcceptShim",
        "MCPAuthMiddleware",
    }
    assert app.state.ombre_http_settings is settings
    assert app.state.ombre_runtime_lifecycle is lifecycle


def test_build_http_app_rejects_stdio_transport():
    with pytest.raises(ValueError, match="stdio"):
        build_http_app(
            SimpleNamespace(),
            "stdio",
            settings=HTTPRuntimeSettings(True, 1024),
            token_validator=lambda *_args, **_kwargs: False,
            lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
        )
