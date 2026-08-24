import json

import pytest
from starlette.requests import Request

from web import _shared as shared_web
from web import buckets as buckets_web


SERVICE_TOKEN = "s" * 40


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class FakeBucketManager:
    async def get(self, bucket_id):
        if bucket_id != "feel-1":
            return None
        return {
            "id": bucket_id,
            "content": "raw [[memory]]",
            "metadata": {"type": "feel", "name": "Feeling"},
        }

    async def get_triggered_feels(self, _bucket_id):
        return []


class FakeDecayEngine:
    @staticmethod
    def calculate_score(_metadata):
        return 1.0


def _request(method, path, authorization=""):
    headers = []
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "path_params": {"bucket_id": "feel-1"},
            "query_string": b"",
            "headers": headers,
        }
    )


@pytest.fixture
def service_token(monkeypatch, tmp_path):
    monkeypatch.setenv("OMBRE_MCP_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setattr(
        shared_web,
        "config",
        {"buckets_dir": str(tmp_path)},
        raising=False,
    )


def test_service_auth_requires_strong_exact_bearer(monkeypatch, service_token):
    assert shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", f"Bearer {SERVICE_TOKEN}")
    )
    assert not shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", "Bearer wrong")
    )
    assert not shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", f"Basic {SERVICE_TOKEN}")
    )

    monkeypatch.setenv("OMBRE_MCP_SERVICE_TOKEN", "short")
    assert not shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", "Bearer short")
    )


@pytest.mark.asyncio
async def test_service_token_reads_exact_bucket_detail(
    monkeypatch,
    service_token,
):
    monkeypatch.setattr(
        buckets_web.sh, "bucket_mgr", FakeBucketManager(), raising=False
    )
    monkeypatch.setattr(
        buckets_web.sh, "decay_engine", FakeDecayEngine(), raising=False
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("GET", "/api/bucket/{bucket_id}")](
        _request("GET", "/api/bucket/feel-1", f"Bearer {SERVICE_TOKEN}")
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["metadata"]["type"] == "feel"
    assert payload["content"] == "raw [[memory]]"
    assert payload["display_content"] == "raw memory"


@pytest.mark.asyncio
async def test_service_token_does_not_authorize_bucket_mutations(
    service_token,
):
    mcp = FakeMCP()
    buckets_web.register(mcp)
    request = _request(
        "POST",
        "/api/bucket/feel-1/resolve",
        f"Bearer {SERVICE_TOKEN}",
    )

    response = await mcp.routes[("POST", "/api/bucket/{bucket_id}/resolve")](
        request
    )

    assert response.status_code == 401
    assert json.loads(response.body)["error"] == "Unauthorized"


MCP_TOKEN = "m" * 40


@pytest.fixture
def mcp_token(monkeypatch, tmp_path):
    """Configure only the MCP static token, no dedicated service token."""
    monkeypatch.delenv("OMBRE_MCP_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("OMBRE_MCP_TOKEN", MCP_TOKEN)
    monkeypatch.setattr(
        shared_web,
        "config",
        {"buckets_dir": str(tmp_path), "mcp_token": MCP_TOKEN},
        raising=False,
    )


def test_mcp_static_token_authenticates_read_routes(monkeypatch, mcp_token):
    assert shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", f"Bearer {MCP_TOKEN}")
    )
    assert not shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", "Bearer wrong")
    )
    # service token env is unset; should not break
    assert not shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", f"Bearer {SERVICE_TOKEN}")
    )


@pytest.mark.asyncio
async def test_mcp_token_reads_exact_bucket_detail(monkeypatch, mcp_token):
    monkeypatch.setattr(
        buckets_web.sh, "bucket_mgr", FakeBucketManager(), raising=False
    )
    monkeypatch.setattr(
        buckets_web.sh, "decay_engine", FakeDecayEngine(), raising=False
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    response = await mcp.routes[("GET", "/api/bucket/{bucket_id}")](
        _request("GET", "/api/bucket/feel-1", f"Bearer {MCP_TOKEN}")
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["metadata"]["type"] == "feel"
    assert payload["content"] == "raw [[memory]]"


def test_no_token_configured_rejects_all(monkeypatch, tmp_path):
    monkeypatch.delenv("OMBRE_MCP_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(
        shared_web,
        "config",
        {"buckets_dir": str(tmp_path)},
        raising=False,
    )
    assert not shared_web._is_service_token_authenticated(
        _request("GET", "/api/bucket/feel-1", "Bearer anything")
    )