import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.datastructures import Headers

from dehydrator import Dehydrator
from embedding_engine import GeminiNativeEmbeddingEngine
from server_app import MCPAuthMiddleware, SecurityHeadersMiddleware
from web import auth as auth_web
from web import config_api as config_api_web
from web import import_api as import_api_web
from web.request_limits import MCPRequestBodyLimitMiddleware, is_sse_endpoint_path


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class RecordingASGIApp:
    def __init__(self, status=204, headers=None):
        self.status = status
        self.headers = list(headers or [])
        self.scopes = []

    async def __call__(self, scope, _receive, send):
        self.scopes.append(scope)
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": list(self.headers),
            }
        )
        await send({"type": "http.response.body", "body": b""})


async def empty_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def collect_into(messages):
    async def send(message):
        messages.append(message)

    return send


def http_scope(path, *, method="GET", headers=None):
    return {
        "type": "http",
        "scheme": "https",
        "method": method,
        "path": path,
        "client": ("127.0.0.1", 49152),
        "headers": [(b"host", b"ombre.example"), *(headers or [])],
    }


@pytest.mark.parametrize(
    "path",
    ["/sse", "/sse/", "/messages", "/messages/", "/messages/session-1"],
)
@pytest.mark.asyncio
async def test_legacy_sse_transport_legs_require_auth(path):
    downstream = RecordingASGIApp()
    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=lambda *_args, **_kwargs: False,
        path_matcher=is_sse_endpoint_path,
    )
    sent = []

    await middleware(http_scope(path), empty_receive, collect_into(sent))

    assert downstream.scopes == []
    assert sent[0]["status"] == 401
    assert json.loads(sent[1]["body"])["error"] == "Unauthorized"


@pytest.mark.parametrize("path", ["/sse", "/messages/session-1"])
@pytest.mark.asyncio
async def test_legacy_sse_token_is_bound_to_canonical_mcp_resource(path):
    downstream = RecordingASGIApp()
    validated = []

    def validator(token, *, resource):
        validated.append((token, resource))
        return True

    middleware = MCPAuthMiddleware(
        downstream,
        auth_required=True,
        token_validator=validator,
        path_matcher=is_sse_endpoint_path,
    )
    scope = http_scope(path, headers=[(b"authorization", b"Bearer sse-token")])

    await middleware(scope, empty_receive, collect_into([]))

    assert downstream.scopes == [scope]
    assert validated == [("sse-token", "https://ombre.example/mcp")]


@pytest.mark.parametrize(
    ("path", "matched"),
    [
        ("/sse", True),
        ("/messages/id", True),
        ("/sse-evil", False),
        ("/messagesevil", False),
        ("/api/messages/id", False),
    ],
)
def test_sse_path_matcher_rejects_prefix_lookalikes(path, matched):
    assert is_sse_endpoint_path(path) is matched


@pytest.mark.parametrize("path", ["/sse", "/messages", "/messages/session-1"])
@pytest.mark.asyncio
async def test_legacy_sse_routes_reject_chunked_oversize_bodies(path):
    downstream = RecordingASGIApp()
    middleware = MCPRequestBodyLimitMiddleware(
        downstream,
        max_bytes=10,
        path_matcher=is_sse_endpoint_path,
    )
    chunks = iter(
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"789012", "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(chunks)

    await middleware(
        http_scope(path, method="POST"),
        receive,
        collect_into(sent),
    )

    assert downstream.scopes == []
    assert sent[0]["status"] == 413
    assert "exceeds 10 bytes" in json.loads(sent[1]["body"])["error"]


@pytest.mark.asyncio
async def test_sse_get_handshake_is_not_treated_as_a_request_body():
    downstream = RecordingASGIApp()
    middleware = MCPRequestBodyLimitMiddleware(
        downstream,
        max_bytes=10,
        path_matcher=is_sse_endpoint_path,
    )
    scope = http_scope(
        "/sse",
        headers=[(b"content-length", b"999999")],
    )

    await middleware(scope, empty_receive, collect_into([]))

    assert downstream.scopes == [scope]


@pytest.mark.parametrize("status", [200, 401, 500])
@pytest.mark.asyncio
async def test_security_headers_apply_to_success_auth_failure_and_error(status):
    downstream = RecordingASGIApp(status=status)
    middleware = SecurityHeadersMiddleware(downstream)
    sent = []

    await middleware(http_scope("/"), empty_receive, collect_into(sent))

    start = next(message for message in sent if message["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert headers[b"content-security-policy"] == b"frame-ancestors 'none'"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"permissions-policy"] == (
        b"camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    )


@pytest.mark.asyncio
async def test_security_headers_do_not_duplicate_or_override_existing_policy():
    downstream = RecordingASGIApp(
        status=200,
        headers=[(b"Referrer-Policy", b"same-origin")],
    )
    middleware = SecurityHeadersMiddleware(downstream)
    sent = []

    await middleware(http_scope("/"), empty_receive, collect_into(sent))

    start = next(message for message in sent if message["type"] == "http.response.start")
    referrer_headers = [
        value
        for key, value in start["headers"]
        if key.lower() == b"referrer-policy"
    ]
    assert referrer_headers == [b"same-origin"]


@pytest.mark.parametrize(
    ("host", "headers", "allowed"),
    [
        ("127.0.0.1", {"Host": "localhost"}, True),
        ("::1", {"Host": "[::1]:8000"}, True),
        ("localhost", {"Host": "127.42.0.1:65535"}, True),
        ("203.0.113.10", {"Host": "localhost"}, False),
        (
            "127.0.0.1",
            {"Host": "localhost", "X-Forwarded-For": "203.0.113.10"},
            False,
        ),
        (
            "127.0.0.1",
            {"Host": "localhost", "Forwarded": "for=203.0.113.10"},
            False,
        ),
        (
            "127.0.0.1",
            {"Host": "localhost", "X-Forwarded-Host": "public.example"},
            False,
        ),
        (
            "127.0.0.1",
            {"Host": "localhost", "X-Forwarded-Proto": "https"},
            False,
        ),
    ],
)
def test_initial_setup_is_local_only_without_bootstrap_token(
    monkeypatch, host, headers, allowed
):
    monkeypatch.delenv("OMBRE_SETUP_TOKEN", raising=False)
    request = SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=host),
    )

    assert auth_web._setup_request_allowed(request) is allowed


@pytest.mark.parametrize(
    "authority",
    [
        "localhost",
        "LOCALHOST.",
        "localhost:1",
        "localhost.:65535",
        "127.0.0.1",
        "127.255.255.254:8000",
        "[::1]",
        "[0:0:0:0:0:0:0:1]:443",
    ],
)
def test_initial_setup_accepts_explicit_loopback_host_authorities(
    monkeypatch, authority
):
    monkeypatch.delenv("OMBRE_SETUP_TOKEN", raising=False)
    request = SimpleNamespace(
        headers=Headers({"Host": authority}),
        client=SimpleNamespace(host="127.0.0.1"),
    )

    assert auth_web._setup_request_allowed(request) is True


@pytest.mark.parametrize(
    "authority",
    [
        "attacker.example",
        "evil@localhost",
        "127.1",
        "2130706433",
        "0177.0.0.1",
        "127.0.0.1.",
        "::1",
        "[::1%25lo]",
        "[::1]suffix",
        "localhost:",
        "localhost:0",
        "localhost:65536",
        "localhost:not-a-port",
        "localhost/path",
    ],
)
def test_initial_setup_rejects_ambiguous_or_non_loopback_host_authorities(
    monkeypatch, authority
):
    monkeypatch.delenv("OMBRE_SETUP_TOKEN", raising=False)
    request = SimpleNamespace(
        headers=Headers({"Host": authority}),
        client=SimpleNamespace(host="127.0.0.1"),
    )

    assert auth_web._setup_request_allowed(request) is False


def test_initial_setup_rejects_missing_or_duplicate_host(monkeypatch):
    monkeypatch.delenv("OMBRE_SETUP_TOKEN", raising=False)
    missing = SimpleNamespace(
        headers=Headers(),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    duplicate = SimpleNamespace(
        headers=Headers(
            raw=[
                (b"host", b"localhost"),
                (b"host", b"127.0.0.1"),
            ]
        ),
        client=SimpleNamespace(host="127.0.0.1"),
    )

    assert auth_web._setup_request_allowed(missing) is False
    assert auth_web._setup_request_allowed(duplicate) is False


def test_remote_initial_setup_accepts_only_matching_bootstrap_token(monkeypatch):
    monkeypatch.setenv("OMBRE_SETUP_TOKEN", "bootstrap-secret")
    remote = SimpleNamespace(
        headers={"X-Ombre-Setup-Token": "bootstrap-secret"},
        client=SimpleNamespace(host="203.0.113.10"),
    )
    wrong = SimpleNamespace(
        headers={"X-Ombre-Setup-Token": "wrong"},
        client=SimpleNamespace(host="203.0.113.10"),
    )

    assert auth_web._setup_request_allowed(remote) is True
    assert auth_web._setup_request_allowed(wrong) is False


@pytest.mark.asyncio
async def test_concurrent_initial_setup_creates_exactly_one_session(monkeypatch):
    configured = {"value": False}
    saved_passwords = []
    sessions = {"stale-session": 1.0}
    created_sessions = []
    set_cookies = []
    revoked = []

    monkeypatch.setattr(auth_web, "_setup_lock", asyncio.Lock())
    monkeypatch.setattr(
        auth_web.sh,
        "_is_setup_needed",
        lambda: not configured["value"],
    )
    monkeypatch.setattr(auth_web.sh, "_login_retry_after", lambda _request: 0)

    def save_password(password):
        saved_passwords.append(password)
        configured["value"] = True

    def create_session():
        token = f"session-{len(created_sessions) + 1}"
        created_sessions.append(token)
        sessions[token] = 2.0
        return token

    monkeypatch.setattr(auth_web.sh, "_save_password_hash", save_password)
    monkeypatch.setattr(auth_web.sh, "_sessions", sessions)
    monkeypatch.setattr(auth_web.sh, "_revoke_all_sessions", sessions.clear)
    monkeypatch.setattr(auth_web.sh, "_create_session", create_session)
    monkeypatch.setattr(
        auth_web.sh,
        "_set_session_cookie",
        lambda _response, token, _request: set_cookies.append(token),
    )
    monkeypatch.setattr(auth_web, "_revoke_mcp_grants", lambda: revoked.append(True))

    arrived = {"count": 0}
    both_ready = asyncio.Event()

    class RacingRequest:
        headers = {"Host": "localhost"}
        client = SimpleNamespace(host="127.0.0.1")

        def __init__(self, password):
            self.password = password

        async def json(self):
            arrived["count"] += 1
            if arrived["count"] == 2:
                both_ready.set()
            await asyncio.wait_for(both_ready.wait(), timeout=1)
            return {"password": self.password}

    mcp = FakeMCP()
    auth_web.register(mcp)
    setup = mcp.routes[("POST", "/auth/setup")]

    responses = await asyncio.gather(
        setup(RacingRequest("winner-one")),
        setup(RacingRequest("winner-two")),
    )

    assert sorted(response.status_code for response in responses) == [200, 400]
    assert len(saved_passwords) == 1
    assert created_sessions == ["session-1"]
    assert sessions == {"session-1": 2.0}
    assert set_cookies == ["session-1"]
    assert revoked == [True]


@pytest.mark.asyncio
async def test_native_gemini_calls_keep_api_keys_out_of_urls(monkeypatch, tmp_path):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith(":generateContent"):
                return Response(
                    {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
                )
            return Response({"embedding": {"values": [0.1, 0.2]}})

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    dehy_key = "dehydration-query-secret"
    embed_key = "embedding-query-secret"
    dehydrator = Dehydrator(
        {
            "buckets_dir": str(tmp_path),
            "dehydration": {
                "api_key": dehy_key,
                "api_format": "gemini",
                "model": "models/gemini-test",
            },
        }
    )
    embedding = GeminiNativeEmbeddingEngine(
        embed_key,
        "models/gemini-embedding-test",
        dim=2,
    )

    try:
        assert await dehydrator._chat_gemini("system", "user") == "ok"
        assert await embedding.generate_async("memory") == [0.1, 0.2]
    finally:
        dehydrator._cache_conn.close()

    assert len(calls) == 2
    for (url, kwargs), expected_key in zip(calls, (dehy_key, embed_key)):
        assert urlsplit(url).query == ""
        assert expected_key not in url
        assert kwargs["headers"] == {"x-goog-api-key": expected_key}
        assert "params" not in kwargs


@pytest.mark.asyncio
async def test_openai_compat_passes_configured_extra_body(tmp_path):
    dehydrator = Dehydrator(
        {
            "buckets_dir": str(tmp_path),
            "dehydration": {
                "api_key": "test-key",
                "model": "deepseek-v4-flash",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        }
    )
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    dehydrator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    try:
        result = await dehydrator._chat_once("system", "user")
    finally:
        dehydrator.close()

    assert result == "OK"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize(
    ("model", "expected_limit_key"),
    [
        ("gpt-5", "max_completion_tokens"),
        ("openai/gpt-5-mini", "max_completion_tokens"),
        ("models/gpt-5.1", "max_completion_tokens"),
        ("gpt-50x", "max_tokens"),
        ("deepseek-v4-flash", "max_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_openai_compat_uses_model_specific_completion_limit(
    tmp_path,
    model,
    expected_limit_key,
):
    dehydrator = Dehydrator(
        {
            "buckets_dir": str(tmp_path),
            "dehydration": {"api_key": "test-key", "model": model},
        }
    )
    captured = {}

    class Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    dehydrator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    try:
        result = await dehydrator._chat_once("system", "user", max_tokens=7)
    finally:
        dehydrator.close()

    other_limit_key = (
        "max_tokens"
        if expected_limit_key == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert result == "OK"
    assert captured[expected_limit_key] == 7
    assert other_limit_key not in captured


@pytest.mark.parametrize(
    ("model", "expected_limit_key"),
    [
        ("azure/gpt-5.1", "max_completion_tokens"),
        ("gpt-4o-mini", "max_tokens"),
    ],
)
@pytest.mark.asyncio
async def test_dehydration_probe_uses_model_specific_completion_limit(
    monkeypatch,
    model,
    expected_limit_key,
):
    calls = []

    class Response:
        status_code = 200

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(config_api_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(
        config_api_web.sh,
        "config",
        {
            "dehydration": {
                "api_key": "probe-key",
                "base_url": "https://provider.example/v1",
                "model": model,
            }
        },
    )
    mcp = FakeMCP()
    config_api_web.register(mcp)

    response = await mcp.routes[("POST", "/api/test/dehydration")](object())

    other_limit_key = (
        "max_tokens"
        if expected_limit_key == "max_completion_tokens"
        else "max_completion_tokens"
    )
    assert response.status_code == 200
    [(url, kwargs)] = calls
    assert url == "https://provider.example/v1/chat/completions"
    assert kwargs["json"][expected_limit_key] == 5
    assert other_limit_key not in kwargs["json"]


@pytest.mark.asyncio
async def test_gemini_model_catalog_keeps_api_key_out_of_query(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "models": [
                    {
                        "name": "models/gemini-test",
                        "supportedGenerationMethods": ["generateContent"],
                    }
                ]
            }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    class Request:
        async def json(self):
            return {
                "api_key": "catalog-query-secret",
                "api_format": "gemini",
            }

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(config_api_web.sh, "_require_auth", lambda _request: None)
    mcp = FakeMCP()
    config_api_web.register(mcp)

    response = await mcp.routes[("POST", "/api/models")](Request())

    assert response.status_code == 200
    assert json.loads(response.body)["models"] == ["gemini-test"]
    [(url, kwargs)] = calls
    assert urlsplit(url).query == ""
    assert kwargs["params"] == {"pageSize": 200}
    assert kwargs["headers"] == {"x-goog-api-key": "catalog-query-secret"}


@pytest.mark.asyncio
async def test_webhook_failure_log_does_not_expose_signed_url(monkeypatch):
    import server as server_mod

    hook_url = "https://hooks.example/secret-path?token=secret-query"
    log_messages = []

    class Logger:
        def warning(self, message, *args):
            log_messages.append(message % args if args else message)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            raise RuntimeError(f"provider failure at {url}")

    monkeypatch.setenv("OMBRE_HOOK_URL", hook_url)
    monkeypatch.delenv("OMBRE_HOOK_SKIP", raising=False)
    monkeypatch.setattr(server_mod, "logger", Logger())
    monkeypatch.setattr(server_mod.httpx, "AsyncClient", Client)

    await server_mod._fire_webhook("breath", {"matches": 1})

    assert log_messages == ["Webhook push failed (breath): RuntimeError"]
    combined = "\n".join(log_messages)
    assert hook_url not in combined
    assert "secret-path" not in combined
    assert "secret-query" not in combined


def test_mcp_operation_logs_never_copy_text_or_control_sequences():
    import server as server_mod

    secret = "private query\r\n\t\x1b[31mFAKE-ERROR"
    rendered = server_mod._fmt_log_args(
        {"query": secret, "author": "Alice", "max_results": 3}
    )

    assert "private" not in rendered
    assert "Alice" not in rendered
    assert "FAKE-ERROR" not in rendered
    assert "\r" not in rendered
    assert "\n" not in rendered
    assert "\x1b" not in rendered
    assert f"query=str_len:{len(secret)}" in rendered
    assert "author=str_len:5" in rendered
    assert "max_results=3" in rendered


@pytest.mark.asyncio
async def test_mcp_error_response_never_includes_another_calls_log_tail(monkeypatch):
    import errors as errors_mod
    import server as server_mod

    monkeypatch.setattr(
        errors_mod,
        "get_recent_logs",
        lambda _limit: ["prior-client-private-query"],
    )

    async def fail():
        raise RuntimeError("current failure\r\n\x1b[31mforged")

    result = await server_mod._with_notice(fail(), op="security_probe")

    assert "prior-client-private-query" not in result
    assert "--- 最近" not in result
    assert "RuntimeError" in result
    assert "异常正文已隐藏" in result
    assert "current failure" not in result
    assert "forged" not in result
    assert "\\x0d" not in result
    assert "\x1b" not in result


@pytest.mark.asyncio
async def test_mcp_error_fallback_still_sanitizes_exception_text(monkeypatch):
    import server as server_mod

    monkeypatch.setattr(
        server_mod,
        "format_error",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("formatter down")),
    )

    async def fail():
        raise RuntimeError("https://provider.invalid/?key=secret\r\n\x1b[31mforged")

    result = await server_mod._with_notice(fail(), op="security_probe")

    assert "RuntimeError" in result
    assert "异常正文已隐藏" in result
    assert "https://" not in result
    assert "provider.invalid" not in result
    assert "secret" not in result
    assert "forged" not in result
    assert "\\x0d" not in result
    assert "\r" not in result
    assert "\n\x1b" not in result
    assert "\x1b" not in result


@pytest.mark.asyncio
async def test_mcp_public_tool_error_returns_only_reviewed_static_message():
    import server as server_mod
    from errors import PublicToolError

    safe_message = (
        "API key 未配置或调用失败，日记拆分无法完成，桶未创建。"
        "请检查 OMBRE_COMPRESS_API_KEY。"
    )

    async def fail():
        try:
            raise RuntimeError("provider-secret-key https://provider.invalid/private")
        except RuntimeError as provider_error:
            raise PublicToolError(safe_message) from provider_error

    result = await server_mod._with_notice(fail(), op="grow")

    assert safe_message in result
    assert "provider-secret-key" not in result
    assert "provider.invalid" not in result


@pytest.mark.parametrize(
    "message",
    ("", "line one\nline two", "control\x1btext", "x" * 501),
)
def test_public_tool_error_rejects_unsafe_public_messages(message):
    from errors import PublicToolError

    with pytest.raises(ValueError):
        PublicToolError(message)


@pytest.mark.asyncio
async def test_mcp_exception_secrets_never_reach_response_persistence_or_logs(
    monkeypatch, tmp_path
):
    import errors as errors_mod
    import server as server_mod

    error_path = tmp_path / "errors.jsonl"
    log_messages = []

    class CapturingLogger:
        @staticmethod
        def _render(message, args):
            return (message % args) if args else str(message)

        def info(self, message, *args, **_kwargs):
            log_messages.append(self._render(message, args))

        def error(self, message, *args, **_kwargs):
            log_messages.append(self._render(message, args))

        def exception(self, *_args, **_kwargs):
            raise AssertionError("异常路径不得记录 traceback")

    logger = CapturingLogger()
    monkeypatch.setattr(errors_mod, "_errors_path", str(error_path))
    monkeypatch.setattr(errors_mod, "logger", logger)
    monkeypatch.setattr(server_mod, "logger", logger)

    async def fail():
        raise RuntimeError(
            "https://provider.invalid/private?api_key=super-secret-token "
            "C:\\Users\\Alice\\.env /home/alice/.ssh/id_rsa\r\n"
            "\x1b[31mFORGED-LOG"
        )

    response = await server_mod._with_notice(fail(), op="security_probe")
    persisted = error_path.read_text(encoding="utf-8")
    logs = "\n".join(log_messages)

    assert "RuntimeError" in response
    assert "异常正文已隐藏" in response
    for rendered in (response, persisted, logs):
        assert "https://" not in rendered
        assert "provider.invalid" not in rendered
        assert "super-secret-token" not in rendered
        assert "C:\\Users\\Alice" not in rendered
        assert "/home/alice/.ssh" not in rendered
        assert "FORGED-LOG" not in rendered
        assert "\x1b" not in rendered


@pytest.mark.parametrize(
    "tool_name",
    (
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
        "I",
    ),
)
def test_all_public_mcp_argument_models_forbid_unknown_fields(tool_name):
    """所有公开工具都必须拒绝未知参数。

    Pydantic 默认的 extra=ignore 会让拼错的参数看似调用成功——写工具甚至会在
    未应用目标字段的情况下仍然创建记忆。信件 3.2.0 挂到 /mcp-extra、3.4.0 并回
    主链路，两次搬家这条边界都必须跟着工具走。
    """
    import server as server_mod

    public_tool = server_mod.mcp._tool_manager.get_tool(tool_name)

    assert public_tool is not None, f"{tool_name} 在主连接器上找不到"
    assert public_tool.fn_metadata.arg_model.model_config.get("extra") == "forbid"


@pytest.mark.asyncio
async def test_chunked_multipart_raw_stream_is_bounded_before_form_result():
    class ChunkedRequest:
        def __init__(self):
            self.headers = {
                "content-type": "multipart/form-data; boundary=security-test"
            }
            self.messages = iter(
                [
                    {
                        "type": "http.request",
                        "body": b"a" * 600_000,
                        "more_body": True,
                    },
                    {
                        "type": "http.request",
                        "body": b"b" * 600_000,
                        "more_body": False,
                    },
                ]
            )
            self.received = 0
            self.original_receive = self.receive
            self._receive = self.original_receive

        async def receive(self):
            self.received += 1
            return next(self.messages)

        async def form(self, **_kwargs):
            while True:
                message = await self._receive()
                if not message.get("more_body", False):
                    return {"file": object()}

    request = ChunkedRequest()

    with pytest.raises(ValueError, match="Upload too large"):
        await import_api_web._read_multipart_form_limited(request, payload_limit=8)

    assert request.received == 2
    assert request._receive is request.original_receive


def test_safe_error_detail_redacts_credential_forms():
    """异常正文可以给出去，但三类常见凭证形态必须先抹掉。"""
    from errors import safe_error_detail

    detail = safe_error_detail(
        RuntimeError(
            "call failed: Authorization: Bearer abc123secret, "
            "api_key=sk-livesecretvalue123456 at https://provider.invalid"
        )
    )

    assert "abc123secret" not in detail
    assert "sk-livesecretvalue123456" not in detail
    assert "[REDACTED]" in detail
    # 非凭证信息要保留，否则脱敏就等于把排查线索一起删了
    assert "call failed" in detail


def test_safe_error_detail_falls_back_and_bounds_length():
    """空正文回落到异常类名；超长正文截断，避免把整页 traceback 塞给客户端。"""
    from errors import safe_error_detail

    assert safe_error_detail(RuntimeError("")) == "RuntimeError"
    assert len(safe_error_detail(RuntimeError("x" * 5000))) <= 200


@pytest.mark.asyncio
async def test_tool_return_redacts_credentials_on_broad_failure(monkeypatch):
    """工具层 broad-except 的回传不得把凭证原样送到 MCP 客户端。"""
    import tools._runtime as rt
    from tools.breath.catalog import surface_catalog

    class ExplodingBucketMgr:
        async def list_all(self, include_archive=False):
            raise RuntimeError("disk backend rejected api_key=sk-livesecretvalue123456")

    monkeypatch.setattr(rt, "bucket_mgr", ExplodingBucketMgr(), raising=False)

    result = await surface_catalog()

    assert "sk-livesecretvalue123456" not in result
    assert "[REDACTED]" in result
    assert "获取记忆目录失败" in result
