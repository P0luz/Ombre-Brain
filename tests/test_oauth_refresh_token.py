import asyncio
import base64
import concurrent.futures
import hashlib
import json
import threading
import time
import urllib.parse

import pytest

import web.oauth as oauth_mod


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
    def __init__(self, body=None, *, headers=None, path_params=None,
                 method="POST", query_params=None, client_host="127.0.0.1"):
        self._body = {} if body is None else body
        self.headers = headers or {"content-type": "application/json", "host": "ombre.example"}
        self.url = FakeUrl()
        self.path_params = path_params or {}
        self.method = method
        self.query_params = query_params or {}
        self.client = type("Client", (), {"host": client_host})()

    async def json(self):
        return self._body

    async def form(self):
        return self._body


def _payload(response):
    return json.loads(response.body)


def test_oauth_authorize_page_exposes_progress_timeout_and_trace_id():
    html = oauth_mod._oauth_authorize_html(
        "client-1",
        "https://chatgpt.com/callback",
        "state-1",
        "a" * 43,
    )

    assert 'id="oauth-form"' in html
    assert 'id="oauth-submit"' in html
    assert 'name="trace_id"' in html
    assert "正在验证…" in html
    assert "等待超过 30 秒" in html
    assert "诊断编号" in html


def test_oauth_log_fields_remove_control_characters_and_limit_length():
    value = "trace\r\n\t伪造\u2028" + "x" * 40

    sanitized = oauth_mod._oauth_log_field(value, 24)

    assert len(sanitized) == 24
    assert all(char.isprintable() for char in sanitized)
    assert "\r" not in sanitized
    assert "\n" not in sanitized


def _fresh_oauth_routes(monkeypatch, tmp_path, *, auth_required=True):
    oauth_mod._oauth_clients.clear()
    oauth_mod._oauth_codes.clear()
    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_token_resources.clear()
    if hasattr(oauth_mod, "_mcp_refresh_tokens"):
        oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod.sh._login_failures.clear()
    oauth_mod.sh._login_locked_until.clear()
    if hasattr(oauth_mod.sh, "_login_source_lru"):
        oauth_mod.sh._login_source_lru.clear()
    if hasattr(oauth_mod.sh, "_login_global_attempts"):
        oauth_mod.sh._login_global_attempts.clear()
    if hasattr(oauth_mod, "_oauth_registration_source_attempts"):
        oauth_mod._oauth_registration_source_attempts.clear()
    if hasattr(oauth_mod, "_oauth_registration_global_attempts"):
        oauth_mod._oauth_registration_global_attempts.clear()
    monkeypatch.setattr(oauth_mod.sh, "config", {
        "buckets_dir": str(tmp_path / "buckets"),
        "mcp_require_auth": auth_required,
    })

    mcp = FakeMCP()
    oauth_mod.register(mcp)
    return mcp.routes


@pytest.fixture
def oauth_routes(monkeypatch, tmp_path):
    return _fresh_oauth_routes(monkeypatch, tmp_path)


@pytest.mark.asyncio
async def test_oauth_metadata_and_registration_advertise_refresh_token(oauth_routes):
    metadata_response = await oauth_routes[("GET", "/.well-known/oauth-authorization-server")](
        JsonRequest()
    )
    metadata = _payload(metadata_response)

    register_response = await oauth_routes[("POST", "/oauth/register")](
        JsonRequest({"redirect_uris": ["https://client.example/callback"]})
    )
    registration = _payload(register_response)

    assert "refresh_token" in metadata["grant_types_supported"]
    assert "refresh_token" in registration["grant_types"]


@pytest.mark.asyncio
async def test_oauth_routes_are_not_advertised_when_mcp_auth_is_disabled(
    monkeypatch, tmp_path
):
    routes = _fresh_oauth_routes(
        monkeypatch, tmp_path, auth_required=False
    )

    requests = [
        (("GET", "/.well-known/oauth-protected-resource"), JsonRequest(method="GET")),
        (
            ("GET", "/.well-known/oauth-protected-resource/{resource_path:path}"),
            JsonRequest(method="GET", path_params={"resource_path": "mcp"}),
        ),
        (("GET", "/.well-known/oauth-authorization-server"), JsonRequest(method="GET")),
        (("POST", "/oauth/register"), JsonRequest()),
        (("GET", "/oauth/authorize"), JsonRequest(method="GET")),
        (("POST", "/oauth/token"), JsonRequest()),
    ]

    for route, request in requests:
        response = await routes[route](request)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_protected_resource_metadata_rejects_unknown_mcp_path(
    oauth_routes,
):
    response = await oauth_routes[
        ("GET", "/.well-known/oauth-protected-resource/{resource_path:path}")
    ](
        JsonRequest(
            method="GET",
            path_params={"resource_path": "retired-mcp-endpoint"},
        )
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_protected_resource_metadata_only_describes_real_mcp_resource(
    oauth_routes,
):
    root_response = await oauth_routes[
        ("GET", "/.well-known/oauth-protected-resource")
    ](JsonRequest(method="GET"))
    mcp_response = await oauth_routes[
        ("GET", "/.well-known/oauth-protected-resource/{resource_path:path}")
    ](JsonRequest(method="GET", path_params={"resource_path": "mcp"}))

    assert root_response.status_code == 200
    assert _payload(root_response)["resource"] == "https://ombre.example/mcp"
    assert root_response.headers["cache-control"] == "no-store"
    assert mcp_response.status_code == 200
    assert _payload(mcp_response)["resource"] == "https://ombre.example/mcp"
    assert mcp_response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_oauth_route_visibility_uses_startup_config_snapshot(
    monkeypatch, tmp_path
):
    enabled_routes = _fresh_oauth_routes(
        monkeypatch, tmp_path, auth_required=True
    )
    oauth_mod.sh.config["mcp_require_auth"] = False

    enabled_response = await enabled_routes[
        ("GET", "/.well-known/oauth-protected-resource")
    ](JsonRequest(method="GET"))
    assert enabled_response.status_code == 200

    disabled_routes = _fresh_oauth_routes(
        monkeypatch, tmp_path, auth_required=False
    )
    oauth_mod.sh.config["mcp_require_auth"] = True

    disabled_response = await disabled_routes[
        ("GET", "/.well-known/oauth-protected-resource")
    ](JsonRequest(method="GET"))
    assert disabled_response.status_code == 404


@pytest.mark.asyncio
async def test_refresh_token_grant_renews_access_without_browser_authorization(oauth_routes):
    oauth_mod._oauth_clients["client-1"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Headless Client",
    }
    oauth_mod._oauth_codes["code-1"] = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "code_challenge": "",
        "expires": time.time() + 60,
    }

    token_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": "client-1",
        })
    )
    initial = _payload(token_response)
    first_access_token = initial["access_token"]
    refresh_token = initial["refresh_token"]

    oauth_mod._mcp_tokens[first_access_token] = time.time() - 1
    assert oauth_mod._is_valid_mcp_token(first_access_token) is False

    refresh_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "client-1",
        })
    )
    refreshed = _payload(refresh_response)
    replacement_refresh_token = refreshed["refresh_token"]

    assert refreshed["access_token"] != first_access_token
    assert replacement_refresh_token != refresh_token
    assert refreshed["token_type"] == "Bearer"
    assert refreshed["scope"] == "mcp"
    assert oauth_mod._is_valid_mcp_token(refreshed["access_token"]) is True
    assert refresh_token not in oauth_mod._mcp_refresh_tokens
    assert replacement_refresh_token in oauth_mod._mcp_refresh_tokens

    replay_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "client-1",
        })
    )

    assert replay_response.status_code == 400
    assert _payload(replay_response)["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_refresh_token_survives_process_restart(oauth_routes):
    oauth_mod._oauth_clients["client-1"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Headless Client",
    }
    oauth_mod._oauth_codes["code-1"] = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "code_challenge": "",
        "expires": time.time() + 60,
    }

    token_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": "client-1",
        })
    )
    refresh_token = _payload(token_response)["refresh_token"]

    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod._load_mcp_tokens()

    refresh_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "client-1",
        })
    )
    refreshed = _payload(refresh_response)
    replacement_refresh_token = refreshed["refresh_token"]

    assert refresh_response.status_code == 200
    assert replacement_refresh_token != refresh_token
    assert oauth_mod._is_valid_mcp_token(refreshed["access_token"]) is True

    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod._load_mcp_tokens()

    assert refresh_token not in oauth_mod._mcp_refresh_tokens
    assert replacement_refresh_token in oauth_mod._mcp_refresh_tokens

    replay_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "client-1",
        })
    )
    next_rotation = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": replacement_refresh_token,
            "client_id": "client-1",
        })
    )

    assert replay_response.status_code == 400
    assert _payload(replay_response)["error"] == "invalid_grant"
    assert next_rotation.status_code == 200
    assert _payload(next_rotation)["refresh_token"] != replacement_refresh_token


@pytest.mark.asyncio
async def test_refresh_token_grant_rejects_unknown_refresh_token(oauth_routes):
    response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": "not-issued",
            "client_id": "client-1",
        })
    )
    payload = _payload(response)

    assert response.status_code == 400
    assert payload["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_refresh_validation_failure_does_not_consume_valid_token(oauth_routes):
    resource = "https://ombre.example/mcp"
    refresh_token = oauth_mod._issue_mcp_refresh_token("client-1", resource)
    oauth_mod._save_mcp_tokens()

    wrong_client = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "different-client",
            "resource": resource,
        })
    )
    wrong_resource = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "client-1",
            "resource": "https://other.example/mcp",
        })
    )

    assert wrong_client.status_code == 400
    assert _payload(wrong_client)["error"] == "invalid_grant"
    assert wrong_resource.status_code == 400
    assert _payload(wrong_resource)["error"] == "invalid_target"
    assert refresh_token in oauth_mod._mcp_refresh_tokens


def test_revoke_all_mcp_grants_clears_refresh_tokens_durably(oauth_routes):
    resource = "https://ombre.example/mcp"
    oauth_mod._oauth_codes["pending-code"] = {
        "expires": time.time() + 60,
    }
    access_token = oauth_mod._issue_mcp_access_token(resource)
    refresh_token = oauth_mod._issue_mcp_refresh_token("client-1", resource)
    oauth_mod._save_mcp_tokens()

    oauth_mod.revoke_all_mcp_grants()

    assert oauth_mod._oauth_codes == {}
    assert access_token not in oauth_mod._mcp_tokens
    assert refresh_token not in oauth_mod._mcp_refresh_tokens

    oauth_mod._load_mcp_tokens()
    assert oauth_mod._mcp_tokens == {}
    assert oauth_mod._mcp_refresh_tokens == {}


@pytest.mark.asyncio
async def test_oauth_popup_completes_pkce_flow_and_binds_mcp_resource(
    oauth_routes, monkeypatch
):
    """回归：授权页弹出后必须能完整走通 code + PKCE + resource 换 token。"""
    client_id = "client-browser"
    redirect_uri = "https://client.example/callback"
    resource = "https://ombre.example/mcp"
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    oauth_mod._oauth_clients[client_id] = {
        "redirect_uris": [redirect_uri],
        "client_name": "Browser Client",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "secret")

    authorize_get = await oauth_routes[("GET", "/oauth/authorize")](
        JsonRequest(
            method="GET",
            query_params={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "state": "state-1",
                "scope": "mcp",
                "resource": resource,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
    )
    assert authorize_get.status_code == 200
    assert f'name="resource" value="{resource}"' in authorize_get.body.decode()

    authorize_post = await oauth_routes[("POST", "/oauth/authorize")](
        JsonRequest({
            "password": "secret",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "state-1",
            "scope": "mcp",
            "resource": resource,
            "code_challenge": challenge,
        })
    )
    assert authorize_post.status_code == 302
    assert oauth_mod._oauth_clients[client_id]["activated"] is True
    assert (
        oauth_mod._oauth_clients[client_id]["expires"] - time.time()
        > oauth_mod._OAUTH_CLIENT_PENDING_TTL
    )
    location = authorize_post.headers["location"]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert query["state"] == ["state-1"]
    code = query["code"][0]

    token_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": resource,
        })
    )
    token = _payload(token_response)

    assert token_response.status_code == 200
    assert token_response.headers["cache-control"] == "no-store"
    assert 0 < token["expires_in"] < 2_147_483_647
    assert oauth_mod._is_valid_mcp_token(token["access_token"], resource) is True
    assert oauth_mod._is_valid_mcp_token(
        token["access_token"], "https://other.example/mcp"
    ) is False

    refresh_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": client_id,
            "resource": resource,
        })
    )
    refreshed = _payload(refresh_response)
    assert refresh_response.status_code == 200
    assert oauth_mod._is_valid_mcp_token(refreshed["access_token"], resource) is True


@pytest.mark.asyncio
async def test_oauth_popup_explains_missing_dashboard_setup(oauth_routes, monkeypatch):
    oauth_mod._oauth_clients["client-setup"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Setup Client",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: True)

    response = await oauth_routes[("GET", "/oauth/authorize")](
        JsonRequest(
            method="GET",
            query_params={
                "client_id": "client-setup",
                "redirect_uri": "https://client.example/callback",
                "response_type": "code",
                "resource": "https://ombre.example/mcp",
                "code_challenge": "s" * 43,
                "code_challenge_method": "S256",
            },
        )
    )

    assert response.status_code == 503
    assert "尚未设置 Dashboard 密码" in response.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        [],
        {"redirect_uris": "https://client.example/callback"},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"redirect_uris": ["file:///tmp/token"]},
        {"redirect_uris": ["http://attacker.example/callback"]},
        {"redirect_uris": ["https://user:pass@client.example/callback"]},
    ],
)
async def test_oauth_registration_rejects_unsafe_metadata(oauth_routes, body):
    response = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))

    assert response.status_code == 400
    assert _payload(response)["error"] == "invalid_client_metadata"


@pytest.mark.asyncio
async def test_oauth_registration_allows_https_loopback_and_native_callbacks(
    oauth_routes,
):
    callbacks = [
        "https://client.example/callback",
        "http://127.0.0.1:8765/callback",
        "vscode://ombre/callback",
    ]

    response = await oauth_routes[("POST", "/oauth/register")](
        JsonRequest({"redirect_uris": callbacks, "client_name": "Safe Client"})
    )

    assert response.status_code == 201
    assert _payload(response)["redirect_uris"] == callbacks


@pytest.mark.asyncio
async def test_oauth_registration_rejects_redirect_uri_total_budget(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_REDIRECT_URIS_TOTAL_CHARS", 80)
    callbacks = [
        "https://client.example/callback/" + ("a" * 20),
        "https://client.example/callback/" + ("b" * 20),
    ]

    response = await oauth_routes[("POST", "/oauth/register")](
        JsonRequest({"redirect_uris": callbacks})
    )

    assert response.status_code == 400
    payload = _payload(response)
    assert payload["error"] == "invalid_client_metadata"
    assert "total length" in payload["error_description"]
    assert oauth_mod._oauth_clients == {}


@pytest.mark.asyncio
async def test_oauth_registration_state_is_bounded(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 1)
    body = {"redirect_uris": ["https://client.example/callback"]}

    first = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    first_client_id = _payload(first)["client_id"]
    second = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    second_client_id = _payload(second)["client_id"]

    assert first.status_code == 201
    assert second.status_code == 201
    assert list(oauth_mod._oauth_clients) == [second_client_id]
    assert first_client_id not in oauth_mod._oauth_clients


@pytest.mark.asyncio
async def test_oauth_registration_pending_capacity_returns_429_without_eviction(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_PENDING_OAUTH_CLIENTS", 1)
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 10)
    body = {"redirect_uris": ["https://client.example/callback"]}

    first = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    first_client_id = _payload(first)["client_id"]
    second = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.headers["cache-control"] == "no-store"
    assert int(second.headers["retry-after"]) > 0
    assert list(oauth_mod._oauth_clients) == [first_client_id]


@pytest.mark.asyncio
async def test_oauth_pending_capacity_does_not_count_or_evict_authorized_clients(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_PENDING_OAUTH_CLIENTS", 1)
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 10)
    body = {"redirect_uris": ["https://client.example/callback"]}

    authorized = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    authorized_id = _payload(authorized)["client_id"]
    assert oauth_mod._activate_oauth_client(authorized_id) is True

    pending = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    pending_id = _payload(pending)["client_id"]
    blocked = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))

    assert pending.status_code == 201
    assert blocked.status_code == 429
    assert set(oauth_mod._oauth_clients) == {authorized_id, pending_id}
    assert oauth_mod._oauth_clients[authorized_id]["activated"] is True


@pytest.mark.asyncio
async def test_oauth_registration_returns_503_without_publishing_on_disk_failure(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 1)
    existing = {
        "redirect_uris": ["https://existing.example/callback"],
        "client_name": "Existing",
        "created_at": time.time(),
        "activated": False,
        "expires": time.time() + 60,
    }
    oauth_mod._oauth_clients["existing-client"] = dict(existing)
    monkeypatch.setattr(
        oauth_mod.sh,
        "_atomic_write_private_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    response = await oauth_routes[("POST", "/oauth/register")](
        JsonRequest(
            {"redirect_uris": ["https://new.example/callback"]},
            client_host="198.51.100.90",
        )
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"
    assert oauth_mod._oauth_clients == {"existing-client": existing}


@pytest.mark.asyncio
async def test_oauth_activation_returns_503_and_discards_unpublished_code(
    oauth_routes, monkeypatch
):
    client_id = "client-persist-failure"
    redirect_uri = "https://client.example/callback"
    before = {
        "redirect_uris": [redirect_uri],
        "client_name": "Persistence Test",
        "created_at": time.time(),
        "activated": False,
        "expires": time.time() + 60,
    }
    oauth_mod._oauth_clients[client_id] = dict(before)
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "secret")
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setattr(
        oauth_mod.sh,
        "_atomic_write_private_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    response = await oauth_routes[("POST", "/oauth/authorize")](
        JsonRequest(
            {
                "password": "secret",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "mcp",
                "resource": "https://ombre.example/mcp",
                "code_challenge": "c" * 43,
            }
        )
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"
    assert oauth_mod._oauth_clients == {client_id: before}
    assert oauth_mod._oauth_codes == {}


@pytest.mark.asyncio
async def test_oauth_registration_rate_limits_one_source_before_capacity_exhaustion(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 2)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 10)
    body = {"redirect_uris": ["https://client.example/callback"]}

    responses = [
        await oauth_routes[("POST", "/oauth/register")](
            JsonRequest(body, client_host="198.51.100.60")
        )
        for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [201, 201, 429]
    assert int(responses[-1].headers["retry-after"]) > 0
    assert len(oauth_mod._oauth_clients) == 2


@pytest.mark.asyncio
async def test_oauth_registration_global_limit_survives_source_rotation(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 2)
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 10)
    body = {"redirect_uris": ["https://client.example/callback"]}

    responses = [
        await oauth_routes[("POST", "/oauth/register")](
            JsonRequest(body, client_host=f"198.51.100.{index}")
        )
        for index in range(1, 4)
    ]

    assert [response.status_code for response in responses] == [201, 201, 429]
    assert len(oauth_mod._oauth_clients) == 2


@pytest.mark.asyncio
async def test_oauth_registration_ignores_forged_forwarding_from_direct_peer(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 1)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    body = {"redirect_uris": ["https://client.example/callback"]}
    headers_a = {
        "content-type": "application/json",
        "host": "ombre.example",
        "x-forwarded-for": "198.51.100.1",
    }
    headers_b = {**headers_a, "x-forwarded-for": "198.51.100.2"}

    first = await oauth_routes[("POST", "/oauth/register")](
        JsonRequest(body, headers=headers_a, client_host="203.0.113.20")
    )
    second = await oauth_routes[("POST", "/oauth/register")](
        JsonRequest(body, headers=headers_b, client_host="203.0.113.20")
    )

    assert first.status_code == 201
    assert second.status_code == 429


def test_oauth_registration_limiter_is_atomic_across_threads(monkeypatch):
    oauth_mod._oauth_registration_source_attempts.clear()
    oauth_mod._oauth_registration_global_attempts.clear()
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 3)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 3)
    request = JsonRequest(client_host="198.51.100.61")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        results = list(
            executor.map(
                lambda _index: oauth_mod._reserve_oauth_registration(request),
                range(12),
            )
        )

    assert results.count(0) == 3
    assert sum(retry > 0 for retry in results) == 9


def test_oauth_registration_source_tracking_is_bounded(monkeypatch):
    oauth_mod._oauth_registration_source_attempts.clear()
    oauth_mod._oauth_registration_global_attempts.clear()
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_MAX_TRACKED_SOURCES", 2)

    for index in range(1, 4):
        assert oauth_mod._reserve_oauth_registration(
            JsonRequest(client_host=f"198.51.100.{index}")
        ) == 0

    assert len(oauth_mod._oauth_registration_source_attempts) == 2
    assert "198.51.100.1" not in oauth_mod._oauth_registration_source_attempts


@pytest.mark.asyncio
async def test_oauth_registration_evicts_oldest_unused_client_at_capacity(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 2)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    body = {"redirect_uris": ["https://client.example/callback"]}

    first = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    first_client_id = _payload(first)["client_id"]
    second = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    second_client_id = _payload(second)["client_id"]
    third = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    third_client_id = _payload(third)["client_id"]

    assert [first.status_code, second.status_code, third.status_code] == [201, 201, 201]
    assert first_client_id not in oauth_mod._oauth_clients
    assert set(oauth_mod._oauth_clients) == {second_client_id, third_client_id}
    assert all(
        client["expires"] - time.time() <= oauth_mod._OAUTH_CLIENT_PENDING_TTL + 1
        for client in oauth_mod._oauth_clients.values()
    )


@pytest.mark.asyncio
async def test_oauth_registration_never_evicts_an_authorized_client(
    oauth_routes, monkeypatch
):
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 1)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    body = {"redirect_uris": ["https://client.example/callback"]}

    first = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))
    client_id = _payload(first)["client_id"]
    oauth_mod._activate_oauth_client(client_id)
    second = await oauth_routes[("POST", "/oauth/register")](JsonRequest(body))

    assert second.status_code == 429
    assert list(oauth_mod._oauth_clients) == [client_id]
    assert oauth_mod._oauth_clients[client_id]["activated"] is True


def test_oauth_registration_registry_bound_is_atomic_across_event_loops(
    monkeypatch, tmp_path
):
    routes = _fresh_oauth_routes(monkeypatch, tmp_path)
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 4)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    route = routes[("POST", "/oauth/register")]
    body = {"redirect_uris": ["https://client.example/callback"]}

    def register_one(index):
        return asyncio.run(
            route(
                JsonRequest(
                    body,
                    client_host=f"198.51.100.{index + 1}",
                )
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(register_one, range(12)))

    assert all(response.status_code == 201 for response in responses)
    assert len(oauth_mod._oauth_clients) == 4


def test_oauth_pending_capacity_is_atomic_across_event_loops(
    monkeypatch, tmp_path
):
    routes = _fresh_oauth_routes(monkeypatch, tmp_path)
    monkeypatch.setattr(oauth_mod, "_MAX_PENDING_OAUTH_CLIENTS", 4)
    monkeypatch.setattr(oauth_mod, "_MAX_OAUTH_CLIENTS", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_SOURCE_MAX", 100)
    monkeypatch.setattr(oauth_mod, "_OAUTH_REGISTRATION_GLOBAL_MAX", 100)
    route = routes[("POST", "/oauth/register")]
    body = {"redirect_uris": ["https://client.example/callback"]}

    def register_one(index):
        return asyncio.run(
            route(
                JsonRequest(
                    body,
                    client_host=f"198.51.100.{index + 1}",
                )
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        responses = list(executor.map(register_one, range(12)))

    assert sum(response.status_code == 201 for response in responses) == 4
    assert sum(response.status_code == 429 for response in responses) == 8
    assert len(oauth_mod._oauth_clients) == 4


@pytest.mark.asyncio
async def test_oauth_registration_survives_route_restart(monkeypatch, tmp_path):
    routes = _fresh_oauth_routes(monkeypatch, tmp_path)
    callback = "https://client.example/callback"
    response = await routes[("POST", "/oauth/register")](
        JsonRequest({"redirect_uris": [callback], "client_name": "Persistent Client"})
    )
    client_id = _payload(response)["client_id"]

    oauth_mod._oauth_clients.clear()
    restarted_routes = _fresh_oauth_routes(monkeypatch, tmp_path)
    assert restarted_routes
    ok, error = oauth_mod._validate_authorize_redirect(client_id, callback)
    assert ok is True
    assert error == ""
    assert client_id in oauth_mod._oauth_clients


def test_load_oauth_clients_rejects_expired_and_unsafe_records(monkeypatch, tmp_path):
    buckets = tmp_path / "buckets"
    buckets.mkdir()
    monkeypatch.setattr(oauth_mod.sh, "config", {"buckets_dir": str(buckets)})
    registry = {
        "valid": {
            "redirect_uris": ["https://client.example/callback"],
            "client_name": "Valid",
            "expires": time.time() + 60,
        },
        "expired": {
            "redirect_uris": ["https://client.example/callback"],
            "client_name": "Expired",
            "expires": time.time() - 1,
        },
        "unsafe": {
            "redirect_uris": ["javascript:alert(1)"],
            "client_name": "Unsafe",
            "expires": time.time() + 60,
        },
    }
    (buckets / ".oauth_clients.json").write_text(json.dumps(registry), encoding="utf-8")

    oauth_mod._oauth_clients.clear()
    oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod._load_oauth_clients()

    assert list(oauth_mod._oauth_clients) == ["valid"]
    assert oauth_mod._oauth_clients["valid"]["activated"] is False
    assert (
        oauth_mod._oauth_clients["valid"]["expires"] - time.time()
        <= oauth_mod._OAUTH_CLIENT_PENDING_TTL + 1
    )


def test_load_oauth_clients_preserves_legacy_client_with_active_grant(
    monkeypatch, tmp_path
):
    buckets = tmp_path / "buckets"
    buckets.mkdir()
    monkeypatch.setattr(oauth_mod.sh, "config", {"buckets_dir": str(buckets)})
    expires = time.time() + 86400
    registry = {
        "authorized-client": {
            "redirect_uris": ["https://client.example/callback"],
            "client_name": "Legacy Authorized",
            "expires": expires,
        },
    }
    (buckets / ".oauth_clients.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod._mcp_refresh_tokens["private-refresh-token"] = {
        "client_id": "authorized-client",
        "expires": expires,
        "resource": "https://ombre.example/mcp",
    }

    oauth_mod._oauth_clients.clear()
    oauth_mod._load_oauth_clients()

    restored = oauth_mod._oauth_clients["authorized-client"]
    assert restored["activated"] is True
    assert restored["expires"] == expires


@pytest.mark.asyncio
async def test_oauth_authorize_password_failures_share_login_lockout(
    oauth_routes, monkeypatch
):
    oauth_mod._oauth_clients["client-rate"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Rate Test",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setattr(
        oauth_mod.sh, "_verify_password_for_rotation", lambda _password: None
    )
    body = {
        "password": "wrong",
        "client_id": "client-rate",
        "redirect_uri": "https://client.example/callback",
        "scope": "mcp",
        "resource": "https://ombre.example/mcp",
        "code_challenge": "c" * 43,
    }

    for _ in range(oauth_mod.sh._LOGIN_MAX_FAILURES):
        response = await oauth_routes[("POST", "/oauth/authorize")](
            JsonRequest(body, client_host="198.51.100.40")
        )
        assert response.status_code == 401

    locked = await oauth_routes[("POST", "/oauth/authorize")](
        JsonRequest(body, client_host="198.51.100.40")
    )
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_oauth_authorize_lockout_survives_rotating_forged_forwarded_for(
    oauth_routes, monkeypatch
):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128")
    oauth_mod._oauth_clients["client-rotate"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Rotate Test",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setattr(
        oauth_mod.sh, "_verify_password_for_rotation", lambda _password: None
    )
    body = {
        "password": "wrong",
        "client_id": "client-rotate",
        "redirect_uri": "https://client.example/callback",
        "scope": "mcp",
        "resource": "https://ombre.example/mcp",
        "code_challenge": "c" * 43,
    }

    def appended(index):
        # 反代用追加写法：左边是客户端自带的伪造值，右边是反代写的真地址。
        return JsonRequest(
            body,
            headers={
                "content-type": "application/json",
                "host": "ombre.example",
                "x-forwarded-for": f"198.51.100.{index + 1}, 203.0.113.7",
            },
            client_host="127.0.0.1",
        )

    for index in range(oauth_mod.sh._LOGIN_MAX_FAILURES):
        response = await oauth_routes[("POST", "/oauth/authorize")](
            appended(index)
        )
        assert response.status_code == 401

    locked = await oauth_routes[("POST", "/oauth/authorize")](appended(99))
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


@pytest.mark.asyncio
async def test_oauth_authorize_password_verification_does_not_block_event_loop(
    oauth_routes, monkeypatch
):
    oauth_mod._oauth_clients["client-kdf"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "KDF Test",
    }
    started = threading.Event()
    release = threading.Event()

    def slow_verifier(_password):
        started.set()
        release.wait(timeout=1)
        return False

    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setattr(oauth_mod.sh, "_reserve_global_login_attempt", lambda: 0)
    monkeypatch.setattr(
        oauth_mod.sh, "_verify_password_for_rotation", slow_verifier
    )
    body = {
        "password": "wrong",
        "client_id": "client-kdf",
        "redirect_uri": "https://client.example/callback",
        "scope": "mcp",
        "resource": "https://ombre.example/mcp",
        "code_challenge": "c" * 43,
    }

    watchdog = threading.Timer(0.25, release.set)
    watchdog.start()
    started_at = time.perf_counter()
    task = asyncio.create_task(
        oauth_routes[("POST", "/oauth/authorize")](JsonRequest(body))
    )
    await asyncio.sleep(0.01)
    event_loop_delay = time.perf_counter() - started_at

    assert started.is_set()
    assert event_loop_delay < 0.1
    release.set()
    response = await task
    watchdog.cancel()
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_oauth_authorize_rejects_invalid_scope_and_pkce(
    oauth_routes, monkeypatch
):
    oauth_mod._oauth_clients["client-validation"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Validation Test",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    base = {
        "client_id": "client-validation",
        "redirect_uri": "https://client.example/callback",
        "response_type": "code",
        "resource": "https://ombre.example/mcp",
        "code_challenge_method": "S256",
    }

    bad_scope = await oauth_routes[("GET", "/oauth/authorize")](
        JsonRequest(
            method="GET",
            query_params={**base, "scope": "mcp admin", "code_challenge": "c" * 43},
        )
    )
    bad_pkce = await oauth_routes[("GET", "/oauth/authorize")](
        JsonRequest(
            method="GET",
            query_params={**base, "scope": "mcp", "code_challenge": "short"},
        )
    )

    assert bad_scope.status_code == 400
    assert bad_pkce.status_code == 400


def test_oauth_forwarded_host_is_only_used_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8")
    headers = {
        "host": "ombre.example",
        "x-forwarded-host": "evil.example",
        "x-forwarded-proto": "http",
    }

    direct = JsonRequest(headers=headers, client_host="198.51.100.4")
    proxied = JsonRequest(headers=headers, client_host="127.0.0.1")

    assert oauth_mod._public_base_url(direct) == "https://ombre.example"
    assert oauth_mod._public_base_url(proxied) == "http://evil.example"


@pytest.mark.asyncio
async def test_code_exchange_returns_503_without_consuming_code_on_disk_failure(
    oauth_routes, monkeypatch
):
    code_data = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "code_challenge": "",
        "resource": "https://ombre.example/mcp",
        "scope": "mcp",
        "expires": time.time() + 60,
    }
    oauth_mod._oauth_codes["retryable-code"] = dict(code_data)
    monkeypatch.setattr(
        oauth_mod,
        "_persist_mcp_token_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            oauth_mod.OAuthPersistenceError("disk full")
        ),
    )

    response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest(
            {
                "grant_type": "authorization_code",
                "code": "retryable-code",
                "client_id": "client-1",
                "redirect_uri": "https://client.example/callback",
            }
        )
    )

    assert response.status_code == 503
    assert _payload(response)["error"] == "temporarily_unavailable"
    assert "retryable-code" in oauth_mod._oauth_codes
    assert oauth_mod._mcp_tokens == {}
    assert oauth_mod._mcp_refresh_tokens == {}


@pytest.mark.asyncio
async def test_refresh_rotation_returns_503_and_keeps_old_token_on_disk_failure(
    oauth_routes, monkeypatch
):
    refresh_data = {
        "client_id": "client-1",
        "resource": "https://ombre.example/mcp",
        "expires": time.time() + 60,
    }
    oauth_mod._mcp_refresh_tokens["retryable-refresh"] = dict(refresh_data)
    monkeypatch.setattr(
        oauth_mod,
        "_persist_mcp_token_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            oauth_mod.OAuthPersistenceError("disk full")
        ),
    )

    response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest(
            {
                "grant_type": "refresh_token",
                "refresh_token": "retryable-refresh",
                "client_id": "client-1",
                "resource": "https://ombre.example/mcp",
            }
        )
    )

    assert response.status_code == 503
    assert _payload(response)["error"] == "temporarily_unavailable"
    assert oauth_mod._mcp_refresh_tokens == {
        "retryable-refresh": refresh_data
    }
    assert oauth_mod._mcp_tokens == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_retry", "queued_retry", "expected"),
    [
        (17, 0, "登录服务繁忙，请 17 秒后重试"),
        (0, 13, "尝试过于频繁，请 13 秒后再试"),
    ],
)
async def test_oauth_authorize_rate_limit_messages_are_valid_utf8(
    oauth_routes, monkeypatch, global_retry, queued_retry, expected
):
    oauth_mod._oauth_clients["client-message"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Message Client",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setattr(
        oauth_mod.sh,
        "_reserve_global_login_attempt",
        lambda: global_retry,
    )

    async def verification_result(*_args, **_kwargs):
        return False, queued_retry

    monkeypatch.setattr(
        oauth_mod, "_run_public_password_verification", verification_result
    )
    response = await oauth_routes[("POST", "/oauth/authorize")](
        JsonRequest(
            {
                "password": "secret",
                "client_id": "client-message",
                "redirect_uri": "https://client.example/callback",
                "scope": "mcp",
                "resource": "https://ombre.example/mcp",
                "code_challenge": "c" * 43,
            }
        )
    )

    decoded = response.body.decode("utf-8")
    assert response.status_code == 429
    assert expected in decoded
    assert "�" not in decoded
