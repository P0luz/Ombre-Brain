"""Real HTTP coverage for the management paths used by a desktop host.

The suite is intentionally one ordered flow because it performs first-run
password setup in an isolated Docker volume. It never targets a user vault.
"""

import os
import re
from urllib.parse import urlsplit

import httpx
import pytest


def _bucket_id(text: str) -> str:
    match = re.search(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])", text)
    assert match, text
    return match.group(0)


def _configured_base_url() -> str:
    explicit = os.environ.get("OMBRE_DOCKER_WEB_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    mcp_url = os.environ.get("OMBRE_DOCKER_INTEGRATION_URL", "").strip()
    if not mcp_url:
        return ""
    parsed = urlsplit(mcp_url)
    return f"{parsed.scheme}://{parsed.netloc}"


BASE_URL = _configured_base_url()
SETUP_TOKEN = os.environ.get("OMBRE_DOCKER_SETUP_TOKEN", "").strip()
HOOK_TOKEN = os.environ.get("OMBRE_DOCKER_HOOK_TOKEN", "").strip()
pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="Docker Web integration service is not configured",
)


def test_desktop_management_api_first_run_and_authenticated_flow():
    with httpx.Client(base_url=BASE_URL, timeout=60.0, trust_env=False) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        version = client.get("/api/version")
        assert version.status_code == 200
        assert version.json()["version"]

        assert client.get("/api/update-info").status_code == 401

        auth_before = client.get("/auth/status")
        assert auth_before.status_code == 200
        assert auth_before.json() == {
            "authenticated": False,
            "setup_needed": True,
        }

        protected_before = client.get("/api/config")
        assert protected_before.status_code == 401

        setup_headers = (
            {"X-Ombre-Setup-Token": SETUP_TOKEN} if SETUP_TOKEN else {}
        )
        if SETUP_TOKEN:
            remote_without_token = client.post(
                "/auth/setup",
                json={"password": "docker-audit-password"},
            )
            assert remote_without_token.status_code == 403

        weak_password = client.post(
            "/auth/setup",
            json={"password": "123"},
            headers=setup_headers,
        )
        assert weak_password.status_code == 400

        setup = client.post(
            "/auth/setup",
            json={"password": "docker-audit-password"},
            headers=setup_headers,
        )
        assert setup.status_code == 200
        assert setup.json()["ok"] is True
        assert client.cookies.get("ombre_session")

        update_info = client.get("/api/update-info")
        assert update_info.status_code == 200
        assert update_info.json()["version"] == version.json()["version"]

        auth_after = client.get("/auth/status")
        assert auth_after.json() == {
            "authenticated": True,
            "setup_needed": False,
        }

        config = client.get("/api/config")
        assert config.status_code == 200
        config_payload = config.json()
        assert config_payload["transport_effective"] == "streamable-http"
        assert config_payload["mcp_require_auth_effective"] is False
        assert "api_key" not in config_payload.get("dehydration", {})
        assert "api_key" not in config_payload.get("embedding", {})

        invalid_update = client.post(
            "/api/config",
            json={"embedding": "not-an-object"},
        )
        assert invalid_update.status_code == 400

        config_update = client.post(
            "/api/config",
            json={"surfacing": {"breath_max_results": 11}, "persist": False},
        )
        assert config_update.status_code == 200
        assert "surfacing.breath_max_results" in config_update.json()["updated"]
        assert client.get("/api/config").json()["surfacing"]["breath_max_results"] == 11

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["version"] == version.json()["version"]
        assert status.json()["decay_engine"] == "running"

        diagnostics = client.get("/api/system/diagnostics")
        assert diagnostics.status_code == 200
        assert "checks" in diagnostics.json()

        # Letter time-lock flow in a real container: Dashboard is the trusted
        # writer side, MCP is the other side, and neither can override it with
        # ordinary tool arguments.
        human_secret = "docker-dashboard-locked-body"
        human_title = "docker-dashboard-locked-title"
        created_human = client.post(
            "/api/letter",
            json={
                "author": "user",
                "user_name": "李四",
                "content": human_secret,
                "title": human_title,
                "lock_type": "permanent",
            },
        )
        assert created_human.status_code == 200
        human_receipt = created_human.json()
        assert human_receipt["lock_type"] == "permanent"
        assert human_secret not in created_human.text
        assert human_title not in created_human.text

        dashboard_letters = client.get("/api/letters").json()["letters"]
        human_item = next(item for item in dashboard_letters if item["id"] == human_receipt["id"])
        assert human_item["locked"] is False
        assert human_item["content"] == human_secret

        edited_human_secret = "docker-dashboard-edited-locked-body"
        edited_human_title = "docker-dashboard-edited-locked-title"
        edited_human = client.patch(
            f"/api/letter/{human_receipt['id']}",
            json={"content": edited_human_secret, "title": edited_human_title},
        )
        assert edited_human.status_code == 200
        assert client.patch(
            f"/api/letter/{human_receipt['id']}",
            json={"content": "must-not-apply", "lock_type": "none"},
        ).status_code == 400
        human_item = next(
            item for item in client.get("/api/letters").json()["letters"]
            if item["id"] == human_receipt["id"]
        )
        assert human_item["content"] == edited_human_secret
        assert human_item["title"] == edited_human_title
        assert human_item["lock_type"] == "permanent"
        assert human_item["writer_name"]

        request_id = 0

        def mcp_request(method, params=None, path="/mcp"):
            nonlocal request_id
            request_id += 1
            response = client.post(
                path,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            )
            assert response.status_code == 200, response.text
            return response.json()

        # 信件 3.2.0 拆到 /mcp-extra、3.4.0 并回 /mcp。这条用例验证的是
        # "AI 通过 MCP 读信时看不到人类的私密信件"——两次搬家边界都没变，
        # 变的只是端点。
        mcp_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "letter-dashboard-audit", "version": "1"},
        })
        ai_read = mcp_request("tools/call", {
            "name": "letter_read",
            "arguments": {"limit": 20},
        })
        ai_read_text = ai_read["result"]["content"][0]["text"]
        assert "李四" in ai_read_text
        assert human_secret not in ai_read_text
        assert human_title not in ai_read_text

        ai_secret = "docker-mcp-locked-body"
        ai_title = "docker-mcp-locked-title"
        ai_write = mcp_request("tools/call", {
            "name": "letter_write",
            "arguments": {
                "author": "ai",
                "content": ai_secret,
                "title": ai_title,
                "lock_type": "permanent",
            },
        })
        ai_receipt_text = ai_write["result"]["content"][0]["text"]
        ai_letter_id = _bucket_id(ai_receipt_text)
        assert "🔒permanent" in ai_receipt_text
        assert ai_secret not in ai_receipt_text and ai_title not in ai_receipt_text

        dashboard_letters = client.get("/api/letters").json()["letters"]
        ai_item = next(item for item in dashboard_letters if item["id"] == ai_letter_id)
        assert ai_item["locked"] is True
        assert ai_item["writer_name"] == "张三"
        assert "content" not in ai_item and "title" not in ai_item
        assert client.patch(
            f"/api/letter/{ai_letter_id}", json={"lock_type": "none"}
        ).status_code == 403
        assert client.patch(
            f"/api/letter/{ai_letter_id}", json={"content": "must-not-apply"}
        ).status_code == 403

        # A newer ordinary Letter must not suppress the independent notice for
        # the older still-locked incoming Letter.
        ordinary = client.post(
            "/api/letter",
            json={"author": "user", "content": "newer ordinary docker letter"},
        )
        assert ordinary.status_code == 200

        dashboard_hook = client.get("/breath-hook")
        assert dashboard_hook.status_code == 200
        assert "张三给你留了一封永久锁信" in dashboard_hook.text
        assert ai_secret not in dashboard_hook.text and ai_title not in dashboard_hook.text

        if HOOK_TOKEN:
            ai_hook = client.get(
                "/breath-hook", headers={"X-Ombre-Hook-Token": HOOK_TOKEN}
            )
            assert ai_hook.status_code == 200
            assert "李四给你留了一封永久锁信" in ai_hook.text
            assert edited_human_secret not in ai_hook.text
            assert edited_human_title not in ai_hook.text

        with httpx.Client(base_url=BASE_URL, timeout=60.0, trust_env=False) as public_client:
            public_hook = public_client.get("/breath-hook")
            assert public_hook.status_code == 200
            assert human_secret not in public_hook.text
            assert ai_secret not in public_hook.text

        malformed_object_routes = [
            ("POST", "/api/config"),
            ("POST", "/api/import/review"),
            ("POST", "/api/plans/missing/action"),
            ("POST", "/api/github/config"),
            ("POST", "/api/embedding/migrate"),
            ("POST", "/api/embedding/local/pull"),
            ("POST", "/api/letter"),
            ("POST", "/api/models"),
            ("POST", "/api/env-config"),
            ("POST", "/api/transport"),
            ("POST", "/api/buckets/forget"),
            ("POST", "/api/settings/sampling"),
            ("POST", "/api/settings/human"),
            ("POST", "/api/settings/human/sync-existing"),
        ]
        for method, path in malformed_object_routes:
            malformed = client.request(method, path, json=[])
            assert malformed.status_code == 400, (path, malformed.text)

        excessive_query = "q" * (16 * 1024 + 1)
        assert client.get("/api/search", params={"q": excessive_query}).status_code == 400
        assert client.get("/api/breath-debug", params={"q": excessive_query}).status_code == 400
        assert client.get("/api/breath-debug", params={"valence": "nan"}).status_code == 400
        assert client.get("/api/breath", params={"n": "not-an-int"}).status_code == 400
        assert client.get("/api/import/results", params={"limit": "nan"}).status_code == 400
        assert client.get("/api/letters", params={"author": excessive_query}).status_code == 400

        def oversized_chunks():
            for _ in range(65):
                yield b"x" * (64 * 1024)

        chunked_oversize = client.post(
            "/auth/login",
            content=oversized_chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert chunked_oversize.status_code == 413

        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
        assert client.get("/.well-known/oauth-protected-resource/not-a-route").status_code == 404
        # /mcp-extra 自 3.4.0 随信件并回主链路而再次退役：这条路不存在了，
        # 要的就是 404。3.2.0–3.3.0 期间它是信件连接器，那时 GET 拿的是 406
        # （streamable-http 只接受 POST + JSON Accept）。
        assert client.get("/mcp-extra").status_code == 404
        # 主连接器仍在，GET 拿 406 而不是 404
        assert client.get("/mcp").status_code == 406

        invalid_transport = client.post(
            "/api/transport",
            json={"transport": "not-a-transport"},
        )
        assert invalid_transport.status_code == 400

        unchanged_transport = client.post(
            "/api/transport",
            json={"transport": "streamable-http"},
        )
        assert unchanged_transport.status_code == 200
        assert unchanged_transport.json()["restarting"] is False

        logout = client.post("/auth/logout")
        assert logout.status_code == 200
        assert client.get("/api/config").status_code == 401
