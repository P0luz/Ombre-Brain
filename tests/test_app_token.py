# ============================================================
# Test: App 的小钥匙 OMBRE_APP_TOKEN(2026-11-01)
#
# 原生 App 的 Journal 页经 dwell-bridge 读 / 改 / 删记忆。dwell 带这把钥匙,只开白名单:
#   1. 不设钥匙 = 关着:带什么 Bearer 都是 401(和以前一样)
#   2. 钥匙对 + 白名单里的门:放行(读桶列表 / 桶详情 / 改桶 / 删桶 / 信箱 / 便利贴)
#   3. 钥匙对 + 白名单外的门:照旧 401(设置、回收站、改密码……)
#   4. 钥匙错 = 401;「/api/bucket/<id>/更深」不能被前缀放进来
#   5. /api/buckets?full=1 带原文(不去 [[链接]])和到期提醒字段;不带参数照旧只有预览
#   6. /api/todos/update 改字、校验、找不到回 404
# ============================================================

import os
import sys
import tempfile

import pytest

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient  # noqa: E402

TOKEN = "app-token-for-tests"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def server_mod():
    bd = tempfile.mkdtemp()
    for sub in ("permanent", "dynamic", "archive", "feel"):
        os.makedirs(os.path.join(bd, sub), exist_ok=True)
    os.environ["OMBRE_BUCKETS_DIR"] = bd
    os.environ["OMBRE_DASHBOARD_PASSWORD"] = "dashboard-test-pw"
    os.environ.setdefault("OMBRE_SEAL_WORD", "test-seal")
    os.environ["OMBRE_HOOK_SKIP"] = "1"
    sys.modules.pop("server", None)
    import server  # noqa: WPS433
    return server


@pytest.fixture
def client(server_mod, monkeypatch):
    """没登录的客户端(不带面板 cookie),钥匙默认配上。"""
    monkeypatch.setenv("OMBRE_APP_TOKEN", TOKEN)
    return TestClient(server_mod.mcp.streamable_http_app())


@pytest.fixture
def bucket(server_mod):
    import anyio
    return anyio.run(
        lambda: server_mod.bucket_mgr.create(
            content="她说想去海边。[[退潮]]", name="App 钥匙测试桶", tags=["a"],
            domain=["日常"], importance=5, valence=0.5, arousal=0.3,
        )
    )


class TestOffByDefault:
    def test_no_token_configured_means_401(self, server_mod, monkeypatch):
        monkeypatch.delenv("OMBRE_APP_TOKEN", raising=False)
        c = TestClient(server_mod.mcp.streamable_http_app())
        assert c.get("/api/buckets", headers=AUTH).status_code == 401


class TestWhitelist:
    def test_read_buckets(self, client, bucket):
        r = client.get("/api/buckets", headers=AUTH)
        assert r.status_code == 200
        assert any(b["id"] == bucket for b in r.json())

    def test_bucket_detail_patch_delete(self, client, bucket):
        assert client.get(f"/api/bucket/{bucket}", headers=AUTH).status_code == 200
        r = client.patch(f"/api/bucket/{bucket}", headers=AUTH, json={"content": "改过了"})
        assert r.status_code == 200
        assert client.delete(f"/api/bucket/{bucket}", headers=AUTH).status_code == 200

    def test_letters_and_todos(self, client):
        assert client.get("/api/letters", headers=AUTH).status_code == 200
        assert client.get("/api/todos", headers=AUTH).status_code == 200

    def test_wrong_token(self, client):
        r = client.get("/api/buckets", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/config"),
        ("GET", "/api/trash"),
        ("GET", "/api/status"),
        ("POST", "/api/todos/add"),
        ("POST", "/auth/change-password"),
    ])
    def test_outside_whitelist_still_401(self, client, method, path):
        r = client.request(method, path, headers=AUTH, json={})
        assert r.status_code == 401

    def test_prefix_does_not_open_deeper_paths(self, server_mod, monkeypatch):
        monkeypatch.setenv("OMBRE_APP_TOKEN", TOKEN)

        class Req:
            headers = AUTH_LOWER
            method = "POST"

            class url:
                path = "/api/bucket/abc/restore"

        assert server_mod._app_token_ok(Req) is False
        Req.url.path = "/api/bucket/abc"
        assert server_mod._app_token_ok(Req) is True


AUTH_LOWER = {"authorization": f"Bearer {TOKEN}"}


class TestFullList:
    def test_full_has_raw_content_and_trigger(self, client, bucket):
        r = client.get("/api/buckets?full=1", headers=AUTH)
        hit = next(b for b in r.json() if b["id"] == bucket)
        assert "[[退潮]]" in hit["content"]          # 原文,不去链接
        assert "trigger_date" in hit and "trigger_handled" in hit

    def test_default_has_preview_only(self, client, bucket):
        hit = next(b for b in client.get("/api/buckets", headers=AUTH).json() if b["id"] == bucket)
        assert "content" not in hit
        assert "[[" not in hit["content_preview"]


class TestTodoUpdate:
    def test_update_text(self, client, server_mod):
        with server_mod._todos_lock():
            items = server_mod._load_todos_list()
            tid = server_mod._new_todo_id(items)
            items.append({"id": tid, "text": "原来的", "done": False, "created": "2026-11-01T10:00:00"})
            server_mod._save_todos_list(items)
        r = client.post("/api/todos/update", headers=AUTH, json={"id": tid, "text": "改过的"})
        assert r.status_code == 200
        hit = next(it for it in r.json()["items"] if it["id"] == tid)
        assert hit["text"] == "改过的" and "edited" in hit and "by" not in hit

    def test_update_validates(self, client):
        assert client.post("/api/todos/update", headers=AUTH, json={"id": "", "text": "x"}).status_code == 400
        assert client.post("/api/todos/update", headers=AUTH, json={"id": "9999", "text": ""}).status_code == 400
        assert client.post("/api/todos/update", headers=AUTH, json={"id": "9999", "text": "x"}).status_code == 404
