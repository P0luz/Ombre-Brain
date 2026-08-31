import ast
import asyncio
import json
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from server_app import (
    DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES,
    DEFAULT_MAX_MCP_REQUEST_BYTES,
    HTTPRuntimeSettings,
    MCPJSONAcceptShim,
    MCPAuthMiddleware,
    NgrokHeaderMiddleware,
    OriginCSRFGuardMiddleware,
    RuntimeLifecycle,
    build_http_app,
    install_runtime_lifespan,
)


# 唯一的连接器 /mcp。顺序即 server.py 里的注册顺序。
EXPECTED_PUBLIC_MCP_TOOLS = (
    "breath",
    "breath_search",
    "breath_advanced",
    "hold",
    "grow",
    "trace",
    "dream",
    "anchor",
    "release",
    "pulse",
    "plan",
    "letter_write",
    "letter_lock_update",
    "letter_read",
    "feel",
    "I",
)

# 信件。3.2.0 挪到 /mcp-extra，3.4.0 并回主链路——「该不该在这时候用」是
# 工具描述的事，拆连接器解决不了它，却要多维护一整套边界。
EXPECTED_LETTER_TOOLS = (
    "letter_write",
    "letter_lock_update",
    "letter_read",
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES),
        (0, 0),
        ("2048", 2048),
        (-1, DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES),
        ("bad", DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES),
    ],
)
def test_management_request_limit_is_normalized(raw, expected):
    limits = {} if raw is None else {"max_management_request_bytes": raw}

    settings = HTTPRuntimeSettings.from_config({"limits": limits})

    assert settings.max_management_request_bytes == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("accept_headers", "expected"),
    [
        ([], b"application/json"),
        ([(b"accept", b"*/*")], b"*/*, application/json"),
        (
            [(b"accept", b"text/event-stream, */*")],
            b"text/event-stream, */*, application/json",
        ),
        (
            [(b"accept", b"application/*; q=0.8")],
            b"application/*; q=0.8, application/json",
        ),
    ],
)
async def test_json_accept_shim_normalizes_missing_or_wildcard_accept(
    accept_headers,
    expected,
):
    downstream = RecordingASGIApp()
    middleware = MCPJSONAcceptShim(downstream)
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"content-type", b"application/json"), *accept_headers],
    }

    await middleware(scope, _empty_receive, _discard_send)

    forwarded = dict(downstream.scopes[0]["headers"])
    assert forwarded[b"accept"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "accept"),
    [
        ("/mcp", b"application/json"),
        ("/mcp", b"text/event-stream"),
        ("/mcp", b"*/*;q=0"),
        ("/health", b"*/*"),
    ],
)
async def test_json_accept_shim_preserves_explicit_or_non_mcp_accept(path, accept):
    downstream = RecordingASGIApp()
    middleware = MCPJSONAcceptShim(downstream)
    scope = {
        "type": "http",
        "path": path,
        "headers": [(b"accept", accept)],
    }

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes[0] is scope


@pytest.mark.asyncio
async def test_letter_tools_live_on_the_main_connector():
    """信件三工具回到主连接器，且没有第二个 FastMCP 实例。

    3.2.0 拆出去是为了工具数量——claude.ai 工具过多时会改用 tool_search 延迟
    加载。但拆连接器管不住「该不该在这时候用」，那是工具描述的事；代价却是
    实打实的第二套边界（严格参数校验、体积限制、鉴权），漏跟一处就是旁路。
    """
    import server

    main_names = {tool.name for tool in await server.mcp.list_tools()}

    assert set(EXPECTED_LETTER_TOOLS) <= main_names
    # 实例本身必须消失，否则「并回主链路」只是又多挂了一份
    assert not hasattr(server, "mcp_extra")


def test_letter_tools_keep_strict_arguments_after_merge():
    """并回主链路不能顺手弄丢严格参数校验。

    letter_write 是能创建记忆的写工具——参数拼错照样返回成功、目标字段没应用
    却已经落库，是最难发现的那类错。搬家时最容易掉的就是这种跟着工具走的边界。
    """
    import server

    for name in EXPECTED_LETTER_TOOLS:
        tool = server.mcp._tool_manager.get_tool(name)
        assert tool is not None, name
        assert tool.fn_metadata.arg_model.model_config.get("extra") == "forbid", name


def test_extra_connector_route_is_gone():
    """/mcp-extra 不再挂路由，也不再被当作 MCP 端点。

    匹配器如果还认它，那条已经不存在的路径会跳过管理面的体积限制才走到 404。
    """
    import server
    from web.request_limits import is_mcp_endpoint_path

    app = build_http_app(
        server.mcp,
        "streamable-http",
        settings=HTTPRuntimeSettings(
            auth_required=False,
            max_request_bytes=DEFAULT_MAX_MCP_REQUEST_BYTES,
        ),
        token_validator=lambda *_args, **_kwargs: False,
        lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
    )
    paths = {getattr(route, "path", None) for route in app.router.routes}

    assert "/mcp" in paths
    assert "/mcp-extra" not in paths
    assert not is_mcp_endpoint_path("/mcp-extra")
    assert is_mcp_endpoint_path("/mcp")


@pytest.mark.asyncio
async def test_kelivo_compatible_stateless_json_handshake_lists_all_tools():
    """Kelivo 风格多请求握手不应依赖会话头或 SSE 解析。"""

    import server

    assert server.mcp.settings.json_response is True
    assert server.mcp.settings.stateless_http is True
    registered = await server.mcp.list_tools()
    assert [tool.name for tool in registered] == list(EXPECTED_PUBLIC_MCP_TOOLS)

    app = build_http_app(
        server.mcp,
        "streamable-http",
        settings=HTTPRuntimeSettings(
            auth_required=False,
            max_request_bytes=DEFAULT_MAX_MCP_REQUEST_BYTES,
        ),
        token_validator=lambda *_args, **_kwargs: False,
        lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
    )
    headers = {
        # 一些简化客户端沿用 HTTP 库默认值；生产中间件需把通配范围落到 JSON。
        "accept": "*/*",
        "content-type": "application/json",
    }
    protocol_versions = (
        "2024-11-05",
        "2025-03-26",
        "2025-06-18",
        "2025-11-25",
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as client:
            for index, protocol_version in enumerate(protocol_versions, start=1):
                initialize = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": index * 10,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": protocol_version,
                            "capabilities": {},
                            "clientInfo": {
                                "name": "Kelivo MCP",
                                "version": "1.1.17",
                            },
                        },
                    },
                )
                assert initialize.status_code == 200
                assert initialize.headers["content-type"].startswith(
                    "application/json"
                )
                assert "mcp-session-id" not in initialize.headers
                initialize_payload = initialize.json()
                assert (
                    initialize_payload["result"]["protocolVersion"]
                    == protocol_version
                )
                capabilities = initialize_payload["result"]["capabilities"]
                assert isinstance(capabilities.get("tools"), dict)

                negotiated = initialize_payload["result"]["protocolVersion"]
                post_init_headers = dict(headers)
                if negotiated >= "2025-06-18":
                    post_init_headers["mcp-protocol-version"] = negotiated

                initialized = await client.post(
                    "/mcp",
                    headers=post_init_headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                )
                assert initialized.status_code == 202
                assert "mcp-session-id" not in initialized.headers

                tools_response = await client.post(
                    "/mcp",
                    headers=post_init_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": index * 10 + 1,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                assert tools_response.status_code == 200
                assert tools_response.headers["content-type"].startswith(
                    "application/json"
                )
                assert "mcp-session-id" not in tools_response.headers
                tools = tools_response.json()["result"]["tools"]
                assert [tool["name"] for tool in tools] == list(
                    EXPECTED_PUBLIC_MCP_TOOLS
                )
                assert all(isinstance(tool.get("inputSchema"), dict) for tool in tools)


def test_legacy_sse_transport_is_rejected():
    """2026-08-09 起 legacy SSE 传输下线：build_http_app 必须明确拒绝，不能悄悄放行。"""

    with pytest.raises(ValueError, match="sse"):
        build_http_app(
            object(),
            "sse",
            settings=HTTPRuntimeSettings(
                auth_required=False,
                max_request_bytes=DEFAULT_MAX_MCP_REQUEST_BYTES,
            ),
            token_validator=lambda *_args, **_kwargs: False,
            lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scheme", "host", "origin"),
    [
        ("https", "Ombre.Example:443", "HTTPS://ombre.example/"),
        ("http", "ombre.example:80", "http://OMBRE.EXAMPLE///"),
        ("https", "ombre.example:8443", "https://OMBRE.example:8443/"),
        ("https", "[2001:DB8::1]:443", "https://[2001:db8::1]/"),
    ],
)
async def test_csrf_guard_allows_normalized_same_origin_direct_request(
    scheme, host, origin
):
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": "PATCH",
        "scheme": scheme,
        "path": "/api/bucket/b1",
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", host.encode("ascii")),
            (b"origin", origin.encode("ascii")),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == [scope]
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/bucket/b1"),
        ("POST", "/api/restart"),
        ("POST", "/api/do-update"),
    ],
)
async def test_csrf_guard_allows_public_origin_from_trusted_proxy(
    monkeypatch, method, path
):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": path,
        "client": ("10.20.30.40", 49152),
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"origin", b"https://PUBLIC.example/"),
            (b"x-forwarded-proto", b"https, http"),
            (b"x-forwarded-host", b"public.example:443, proxy.internal"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == [scope]
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/bucket/b1/edit"),
        ("POST", "/api/env-config"),
        ("POST", "/api/config"),
        ("POST", "/api/restart"),
        ("POST", "/api/do-update"),
    ],
)
async def test_csrf_guard_uses_same_origin_fetch_signal_when_proxy_omits_headers(
    monkeypatch,
    method,
    path,
):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": method,
        "scheme": "http",
        "path": path,
        "client": ("10.20.30.40", 49152),
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"origin", b"https://public.example"),
            (b"sec-fetch-site", b"same-origin"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == [scope]
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
async def test_csrf_guard_accepts_configured_public_origin_without_fetch_metadata():
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(
        downstream,
        public_origin="HTTPS://Public.Example:443/mcp/",
    )
    messages = []
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/env-config",
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"internal.service:8000"),
            (b"origin", b"https://public.example"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == [scope]
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
async def test_csrf_guard_cross_site_signal_overrides_configured_public_origin():
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(
        downstream,
        public_origin="https://public.example",
    )
    messages = []
    scope = {
        "type": "http",
        "method": "PATCH",
        "scheme": "http",
        "path": "/api/bucket/b1",
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"internal.service:8000"),
            (b"origin", b"https://public.example"),
            (b"sec-fetch-site", b"cross-site"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    ["null", "file://", "https://public.example/path", "not a URL"],
)
async def test_csrf_guard_rejects_invalid_origin_despite_same_origin_signal(origin):
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(
        downstream,
        public_origin="https://public.example",
    )
    messages = []
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/env-config",
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"internal.service:8000"),
            (b"origin", origin.encode("ascii")),
            (b"sec-fetch-site", b"same-origin"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "duplicate_name",
    [
        b"origin",
        b"host",
        b"sec-fetch-site",
        b"forwarded",
        b"x-forwarded-for",
        b"x-forwarded-host",
        b"x-forwarded-proto",
    ],
)
async def test_csrf_guard_rejects_duplicate_security_headers(duplicate_name):
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(
        downstream,
        public_origin="https://public.example",
    )
    messages = []
    headers = [
        (b"host", b"internal.service:8000"),
        (b"origin", b"https://public.example"),
        (b"sec-fetch-site", b"same-origin"),
    ]
    existing = next(
        (value for name, value in headers if name == duplicate_name),
        None,
    )
    if existing is None:
        headers.extend(
            [
                (duplicate_name, b"synthetic"),
                (duplicate_name, b"synthetic"),
            ]
        )
    else:
        headers.append((duplicate_name, existing))
    scope = {
        "type": "http",
        "method": "PATCH",
        "scheme": "http",
        "path": "/api/bucket/b1/edit",
        "client": ("198.51.100.4", 50123),
        "headers": headers,
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site"])
async def test_csrf_guard_rejects_non_same_origin_fetch_signal_without_origin(
    fetch_site,
):
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "path": "/auth/login",
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"ombre.example"),
            (b"sec-fetch-site", fetch_site.encode("ascii")),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 403


@pytest.mark.asyncio
async def test_csrf_guard_ignores_spoofed_forwarding_headers(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128")
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": "PATCH",
        "scheme": "https",
        "path": "/api/bucket/b1",
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"ombre.example"),
            (b"origin", b"https://evil.example"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"evil.example"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 403


@pytest.mark.asyncio
async def test_csrf_guard_rejects_cross_origin_through_trusted_proxy(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/auth/login",
        "client": ("10.20.30.40", 49152),
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"origin", b"https://evil.example"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"ombre.example"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == []
    assert messages[0]["status"] == 403
    assert json.loads(messages[1]["body"]) == {
        "error": "Cross-origin request rejected"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/mcp",
        "/mcp/",
        "/oauth/token",
        "/.well-known/oauth-protected-resource/mcp",
    ],
)
async def test_csrf_guard_keeps_bearer_and_oauth_routes_exempt(path):
    downstream = RecordingASGIApp()
    middleware = OriginCSRFGuardMiddleware(downstream)
    messages = []
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"ombre.example"),
            (b"origin", b"https://cross-origin.example"),
            (b"sec-fetch-site", b"cross-site"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    assert downstream.scopes == [scope]
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
async def test_ngrok_header_middleware_adds_skip_warning_header():
    downstream = RecordingASGIApp()
    middleware = NgrokHeaderMiddleware(downstream)
    messages = []
    scope = {"type": "http", "path": "/mcp", "headers": []}

    await middleware(scope, _empty_receive, _collect_into(messages))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert (b"ngrok-skip-browser-warning", b"true") in start["headers"]


@pytest.mark.asyncio
async def test_ngrok_header_middleware_applies_regardless_of_status():
    class RejectingApp:
        async def __call__(self, scope, receive, send):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    middleware = NgrokHeaderMiddleware(RejectingApp())
    messages = []
    scope = {"type": "http", "path": "/mcp", "headers": []}

    await middleware(scope, _empty_receive, _collect_into(messages))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 401
    assert (b"ngrok-skip-browser-warning", b"true") in start["headers"]


@pytest.mark.asyncio
async def test_ngrok_header_middleware_does_not_duplicate_existing_header():
    class PreHeaderedApp:
        async def __call__(self, scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"ngrok-skip-browser-warning", b"true")],
                }
            )
            await send({"type": "http.response.body", "body": b""})

    middleware = NgrokHeaderMiddleware(PreHeaderedApp())
    messages = []
    scope = {"type": "http", "path": "/mcp", "headers": []}

    await middleware(scope, _empty_receive, _collect_into(messages))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["headers"].count((b"ngrok-skip-browser-warning", b"true")) == 1


@pytest.mark.asyncio
async def test_ngrok_header_middleware_ignores_non_http_scopes():
    downstream = RecordingASGIApp()
    middleware = NgrokHeaderMiddleware(downstream)
    scope = {"type": "lifespan"}

    await middleware(scope, _empty_receive, _discard_send)

    assert downstream.scopes == [scope]


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
        "client": ("127.0.0.1", 49152),
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
async def test_auth_middleware_ignores_forwarded_resource_from_untrusted_peer(
    monkeypatch,
):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128")
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
        "client": ("198.51.100.4", 50123),
        "headers": [
            (b"host", b"ombre.example:443"),
            (b"x-forwarded-proto", b"http"),
            (b"x-forwarded-host", b"evil.example"),
        ],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    payload = json.loads(messages[1]["body"])
    assert payload["resource_metadata"] == (
        "https://ombre.example/.well-known/oauth-protected-resource/mcp"
    )


@pytest.mark.asyncio
async def test_auth_middleware_passes_retired_mcp_extra_path_through():
    """/mcp-extra 自 3.4.0 再次退役，中间件放行让它落到 router 去拿 404。

    3.2.0 到 3.3.0 之间它是信件连接器，必须受鉴权保护。并回主链路后路由已经
    不存在，继续在中间件里发 401 challenge 反而是错的信号——那会让客户端以为
    换个 token 就能连上一个其实已经没有的端点。

    这条与 `test_extra_connector_route_is_gone` 成对：一条管路由，一条管鉴权面。
    """
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
        "path": "/mcp-extra",
        "headers": [(b"host", b"ombre.example")],
    }

    await middleware(scope, _empty_receive, _collect_into(messages))

    # 退役路径：中间件不拦，交给 router（这里是 RecordingASGIApp，回 204）
    assert downstream.scopes == [scope]
    assert messages[0]["status"] == 204


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


@pytest.mark.asyncio
async def test_auth_middleware_skips_mcp_cors_preflight():
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: pytest.fail(
            "CORS preflight must not be authenticated"
        ),
        auth_mode="token",
    )
    scope = {
        "type": "http",
        "method": "OPTIONS",
        "path": "/mcp",
        "headers": [(b"origin", b"https://polaris.example")],
    }

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
        you_service=RecordingService("you", events),
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
        "you:start",
        "github:0",
        "you:stop",
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


def test_build_http_app_uses_the_managed_stack():
    class FakeMCP:
        def streamable_http_app(self):
            return Starlette()

    lifecycle = RuntimeLifecycle(logger=RecordingLogger())
    settings = HTTPRuntimeSettings(
        auth_required=False,
        max_request_bytes=2048,
        public_origin="https://public.example",
    )

    app = build_http_app(
        FakeMCP(),
        "streamable-http",
        settings=settings,
        token_validator=lambda *_args, **_kwargs: False,
        lifecycle=lifecycle,
    )

    middleware_names = {item.cls.__name__ for item in app.user_middleware}
    assert middleware_names >= {
        "CORSMiddleware",
        "OriginCSRFGuardMiddleware",
        "MCPRequestBodyLimitMiddleware",
        "ManagementRequestBodyLimitMiddleware",
        "MCPAuthMiddleware",
        "NgrokHeaderMiddleware",
        "MCPJSONAcceptShim",
    }
    csrf_middleware = next(
        item
        for item in app.user_middleware
        if item.cls is OriginCSRFGuardMiddleware
    )
    assert csrf_middleware.kwargs["public_origin"] == "https://public.example"
    middleware_order = [item.cls.__name__ for item in app.user_middleware]
    assert middleware_order.index("CORSMiddleware") < middleware_order.index(
        "MCPAuthMiddleware"
    )
    cors_middleware = next(
        item for item in app.user_middleware if item.cls.__name__ == "CORSMiddleware"
    )
    assert set(cors_middleware.kwargs["expose_headers"]) >= {
        "Mcp-Session-Id",
        "WWW-Authenticate",
        "Ngrok-Skip-Browser-Warning",
    }
    assert app.state.ombre_http_settings is settings
    assert app.state.ombre_runtime_lifecycle is lifecycle


@pytest.mark.asyncio
async def test_build_http_app_answers_mcp_preflight_before_token_auth():
    class FakeMCP:
        def streamable_http_app(self):
            return Starlette()

    app = build_http_app(
        FakeMCP(),
        "streamable-http",
        settings=HTTPRuntimeSettings(
            auth_required=True,
            max_request_bytes=2048,
            auth_mode="token",
        ),
        token_validator=lambda *_args, **_kwargs: pytest.fail(
            "CORS preflight must not be authenticated"
        ),
        lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
    )
    messages = []
    scope = {
        "type": "http",
        "http_version": "1.1",
        "scheme": "https",
        "method": "OPTIONS",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "server": ("ombre.example", 443),
        "client": ("127.0.0.1", 50000),
        "headers": [
            (b"host", b"ombre.example"),
            (b"origin", b"https://polaris.example"),
            (b"access-control-request-method", b"POST"),
            (b"access-control-request-headers", b"authorization,content-type"),
        ],
    }

    await app(scope, _empty_receive, _collect_into(messages))

    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert start["status"] == 200
    assert headers[b"access-control-allow-origin"] == b"*"
    assert b"POST" in headers[b"access-control-allow-methods"]
    assert b"authorization" in headers[b"access-control-allow-headers"].lower()


def test_build_http_app_rejects_stdio_transport():
    with pytest.raises(ValueError, match="stdio"):
        build_http_app(
            SimpleNamespace(),
            "stdio",
            settings=HTTPRuntimeSettings(True, 1024),
            token_validator=lambda *_args, **_kwargs: False,
            lifecycle=RuntimeLifecycle(logger=RecordingLogger()),
        )


def test_stdio_runtime_lifecycle_owns_embedding_outbox():
    source_path = Path(__file__).resolve().parents[1] / "src" / "server.py"
    module = ast.parse(source_path.read_text(encoding="utf-8"))

    stdio_branch = None
    for node in ast.walk(module):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if (
            isinstance(test.left, ast.Name)
            and test.left.id == "transport"
            and any(
                isinstance(value, ast.Constant) and value.value == "stdio"
                for value in test.comparators
            )
        ):
            stdio_branch = node.body
            break

    assert stdio_branch is not None
    lifecycle_calls = [
        item
        for statement in stdio_branch
        for item in ast.walk(statement)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "RuntimeLifecycle"
    ]
    assert len(lifecycle_calls) == 1

    keywords = {keyword.arg: keyword.value for keyword in lifecycle_calls[0].keywords}
    outbox = keywords.get("embedding_outbox")
    assert isinstance(outbox, ast.Name)
    assert outbox.id == "embedding_outbox"


@pytest.mark.asyncio
async def test_stdio_mcp_server_resets_existing_boot_marker_after_successful_handshake(
    tmp_path,
):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    runtime_root = tmp_path / "runtime"
    shutil.copytree(Path(__file__).resolve().parents[1] / "src", runtime_root / "src")
    shutil.copy(Path(__file__).resolve().parents[1] / "VERSION", runtime_root / "VERSION")

    buckets_dir = tmp_path / "buckets"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps(
            {
                "transport": "stdio",
                "buckets_dir": str(buckets_dir),
                "embedding": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    marker = runtime_root / ".boot_fails"
    marker.write_text("1\n", encoding="utf-8")

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(runtime_root / "src" / "server.py")],
        cwd=runtime_root,
        env={
            "OMBRE_CONFIG_PATH": str(config_path),
            "OMBRE_BUCKETS_DIR": str(buckets_dir),
            "OMBRE_LOG_DIR": str(tmp_path / "logs"),
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()

    assert initialized.protocolVersion
    assert [tool.name for tool in listed.tools] == list(EXPECTED_PUBLIC_MCP_TOOLS)
    assert marker.read_text(encoding="utf-8") == "0"
