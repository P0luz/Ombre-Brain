import json
import time

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
    def __init__(
        self,
        body=None,
        *,
        headers=None,
        path_params=None,
        method=None,
        query_params=None,
    ):
        self._body = body or {}
        self.headers = headers or {"content-type": "application/json", "host": "ombre.example"}
        self.url = FakeUrl()
        self.path_params = path_params or {}
        if method is not None:
            self.method = method
        self.query_params = query_params or {}

    async def json(self):
        return self._body

    async def form(self):
        return self._body


class AuthorizeRequest(JsonRequest):
    method = "POST"


def _payload(response):
    return json.loads(response.body)


@pytest.fixture
def oauth_routes(monkeypatch, tmp_path):
    oauth_mod._oauth_clients.clear()
    oauth_mod._oauth_codes.clear()
    oauth_mod._mcp_tokens.clear()
    if hasattr(oauth_mod, "_mcp_refresh_tokens"):
        oauth_mod._mcp_refresh_tokens.clear()
    monkeypatch.setattr(oauth_mod.sh, "config", {"buckets_dir": str(tmp_path / "buckets")})

    mcp = FakeMCP()
    oauth_mod.register(mcp)
    return mcp.routes


@pytest.mark.asyncio
async def test_oauth_client_query_cannot_preselect_local_identity(
    oauth_routes, monkeypatch
):
    client_id = "client-query-caller"
    redirect_uri = "https://client.example/callback"
    oauth_mod._oauth_clients[client_id] = {
        "redirect_uris": [redirect_uri],
        "client_name": "Caller Query Test",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)

    response = await oauth_routes[("GET", "/oauth/authorize")](
        JsonRequest(
            method="GET",
            query_params={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "resource": "https://ombre.example/mcp",
                "code_challenge": "q" * 43,
                "code_challenge_method": "S256",
                "caller": "cheng",
            },
        )
    )

    html = response.body.decode()
    assert response.status_code == 200
    assert '<option value="" selected>' in html
    assert '<option value="cheng" selected>' not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["client_id", "redirect_uri", "state", "resource"])
async def test_authorize_get_does_not_reflect_oversized_public_fields(
    oauth_routes, monkeypatch, field
):
    client_id = "client-bounded"
    redirect_uri = "https://client.example/callback"
    oauth_mod._oauth_clients[client_id] = {
        "redirect_uris": [redirect_uri],
        "client_name": "Bounded Client",
    }
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    marker = "OVERSIZED-SENTINEL-" + "z" * 5000
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "resource": "https://ombre.example/mcp",
        "code_challenge": "q" * 43,
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    query[field] = marker

    response = await oauth_routes[("GET", "/oauth/authorize")](
        JsonRequest(method="GET", query_params=query)
    )

    html = response.body.decode()
    assert marker not in html
    assert len(html) < 20_000


def test_authorize_renderer_defensively_bounds_hidden_values():
    marker = "RENDER-SENTINEL-" + "x" * 5000

    html = oauth_mod._oauth_authorize_html(
        marker,
        marker,
        marker,
        marker,
        resource=marker,
        scope=marker,
    )

    assert marker not in html
    assert len(html) < 20_000


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

    assert refreshed["access_token"] != first_access_token
    assert refreshed["token_type"] == "Bearer"
    assert refreshed["scope"] == "mcp"
    assert oauth_mod._is_valid_mcp_token(refreshed["access_token"]) is True


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

    assert refresh_response.status_code == 200
    assert oauth_mod._is_valid_mcp_token(refreshed["access_token"]) is True


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
async def test_authorization_requires_canonical_caller_and_binds_code(
    oauth_routes, monkeypatch
):
    oauth_mod._oauth_clients["client-1"] = {
        "redirect_uris": ["https://client.example/callback"],
        "client_name": "Identity Client",
    }
    async def _valid_password_proof(_request, _verifier, _value):
        return True, 0

    def _store_test_code(code, code_data, _proof):
        oauth_mod._oauth_codes[code] = dict(code_data)
        return True

    monkeypatch.setattr(
        oauth_mod, "_run_public_password_verification", _valid_password_proof
    )
    monkeypatch.setattr(oauth_mod, "_store_authorization_code", _store_test_code)
    monkeypatch.setattr(oauth_mod.sh, "_is_setup_needed", lambda: False)
    monkeypatch.setattr(oauth_mod, "_activate_oauth_client", lambda _client_id: True)
    base_body = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "state": "state-1",
        "code_challenge": "q" * 43,
        "password": "valid",
    }

    invalid = await oauth_routes[("POST", "/oauth/authorize")](
        AuthorizeRequest({**base_body, "caller": "cc"})
    )
    assert invalid.status_code == 400
    assert not oauth_mod._oauth_codes

    valid = await oauth_routes[("POST", "/oauth/authorize")](
        AuthorizeRequest({**base_body, "caller": "huaiyin_cc"})
    )
    assert valid.status_code == 302
    assert len(oauth_mod._oauth_codes) == 1
    code_data = next(iter(oauth_mod._oauth_codes.values()))
    assert code_data["caller"] == "huaiyin_cc"


@pytest.mark.asyncio
async def test_caller_survives_code_refresh_and_process_restart(oauth_routes):
    oauth_mod._oauth_codes["code-identity"] = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "code_challenge": "",
        "caller": "huaiyin",
        "expires": time.time() + 60,
    }

    token_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "authorization_code",
            "code": "code-identity",
            "client_id": "client-1",
        })
    )
    initial = _payload(token_response)
    assert oauth_mod._mcp_token_identity(initial["access_token"]) == (True, "huaiyin")

    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_refresh_tokens.clear()
    oauth_mod._load_mcp_tokens()

    refresh_response = await oauth_routes[("POST", "/oauth/token")](
        JsonRequest({
            "grant_type": "refresh_token",
            "refresh_token": initial["refresh_token"],
            "client_id": "client-1",
        })
    )
    refreshed = _payload(refresh_response)
    assert oauth_mod._mcp_token_identity(refreshed["access_token"]) == (True, "huaiyin")


def test_legacy_token_migration_stays_identityless(monkeypatch, tmp_path):
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    token_file = buckets_dir / ".dashboard_mcp_tokens.json"
    token_file.write_text(json.dumps({
        "access_tokens": {"old-access": time.time() + 60},
        "refresh_tokens": {
            "old-refresh": {
                "expires": time.time() + 60,
                "client_id": "old-client",
            }
        },
    }), encoding="utf-8")
    monkeypatch.setattr(oauth_mod.sh, "config", {"buckets_dir": str(buckets_dir)})
    oauth_mod._mcp_tokens.clear()
    oauth_mod._mcp_refresh_tokens.clear()

    oauth_mod._load_mcp_tokens()

    assert oauth_mod._mcp_token_identity("old-access") == (True, "")
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["access_tokens"]["old-access"]["caller"] == ""
    assert saved["refresh_tokens"]["old-refresh"]["caller"] == ""


def test_token_identity_overrides_header_and_query():
    token = oauth_mod._issue_mcp_access_token("cheng")
    scope = {
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"x-ob-caller", b"huaiyin_cc"),
        ],
        "query_string": b"caller=huaiyin",
    }
    assert oauth_mod._resolve_mcp_caller(scope) == ("cheng", "token", "cheng")

    legacy = oauth_mod._issue_mcp_access_token("")
    legacy_scope = {
        "headers": [
            (b"authorization", f"Bearer {legacy}".encode()),
            (b"x-ob-caller", b"huaiyin_cc"),
        ],
        "query_string": b"caller=huaiyin",
    }
    assert oauth_mod._resolve_mcp_caller(legacy_scope) == (
        "", "legacy_token", ""
    )


def test_header_precedes_query_without_valid_token():
    scope = {
        "headers": [(b"x-ob-caller", b"huaiyin_cc")],
        "query_string": b"caller=huaiyin",
    }
    assert oauth_mod._resolve_mcp_caller(scope) == (
        "huaiyin_cc", "header", "huaiyin_cc"
    )
