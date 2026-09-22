"""OAuth helper shared by isolated Docker integration suites."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx


def issue_oauth_token(
    *,
    base_url: str,
    password: str,
    caller: str,
    resource: str | None = None,
) -> str:
    """Complete DCR + PKCE and return a caller-bound MCP access token."""
    base_url = base_url.rstrip("/")
    resource = resource or f"{base_url}/mcp"
    callback = "https://client.example/docker-integration/callback"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    with httpx.Client(base_url=base_url, timeout=30.0, trust_env=False) as client:
        registration = client.post(
            "/oauth/register",
            json={
                "redirect_uris": [callback],
                "client_name": "Docker Integration Suite",
            },
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]

        authorized = client.post(
            "/oauth/authorize",
            data={
                "password": password,
                "caller": caller,
                "client_id": client_id,
                "redirect_uri": callback,
                "state": "docker-integration",
                "scope": "mcp",
                "resource": resource,
                "code_challenge": challenge,
            },
            follow_redirects=False,
        )
        assert authorized.status_code == 302, authorized.text
        code = parse_qs(urlsplit(authorized.headers["location"]).query)["code"][0]

        exchanged = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "client_id": client_id,
                "redirect_uri": callback,
                "resource": resource,
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        return exchanged.json()["access_token"]
