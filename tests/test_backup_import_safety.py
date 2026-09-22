"""GitHub restore 备份闸门与 M-03 事务结果回归测试。

恢复必须绑定共享导入事务和明确的管理员 owner。若导入前的本地 zip 备份没成功，
默认必须拦下；事务失败不能返回成功；Markdown 已提交但派生同步失败时必须显式
保留已提交边界与失败状态，不能把局部完成伪装成完整成功。
"""
import pytest

from web import _shared as sh
from web import github as github_web


class FakeMcp:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def deco(fn):
            self.routes[(path, tuple(methods))] = fn
            return fn
        return deco


class FakeRequest:
    def __init__(self, body=None):
        self._body = body or {}
        self.headers = {}
        self.query_params = {}
        self.cookies = {}

    async def json(self):
        return self._body


class FakeSync:
    def __init__(self, result=None):
        self.called = False
        self.result = result or {
            "ok": True,
            "markdown_committed": True,
            "derived_state": "complete",
            "imported": 3,
            "skipped": 0,
        }
        self.call = None

    async def import_from_github(self, buckets_dir, **kwargs):
        self.called = True
        self.call = {"buckets_dir": buckets_dir, **kwargs}
        return dict(self.result)


@pytest.fixture
def import_route(monkeypatch, tmp_path):
    mcp = FakeMcp()
    github_web.register(mcp)
    monkeypatch.setattr(sh, "_require_auth", lambda req: None)
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    fake = FakeSync()
    monkeypatch.setattr(sh, "github_sync_instance", fake)
    monkeypatch.setattr(sh, "bucket_mgr", object())
    handler = mcp.routes[("/api/github/import", ("POST",))]
    return handler, fake


async def _run(handler, body=None):
    resp = await handler(FakeRequest(body or {}))
    import json as _j
    return resp.status_code, _j.loads(bytes(resp.body).decode("utf-8"))


@pytest.mark.asyncio
async def test_import_blocked_when_backup_fails(monkeypatch, import_route):
    handler, fake = import_route
    monkeypatch.setattr(github_web, "_pre_import_backup", lambda d: "")  # 备份失败
    status, data = await _run(handler)
    assert status == 409
    assert data.get("backup_failed") is True
    assert fake.called is False   # 关键：没有触碰本地记忆


@pytest.mark.asyncio
async def test_import_proceeds_when_backup_ok(monkeypatch, import_route):
    handler, fake = import_route
    monkeypatch.setattr(github_web, "_pre_import_backup", lambda d: "/backups/x.zip")
    status, data = await _run(
        handler,
        {
            "decisions": {"candidate-a": "overwrite"},
            "assigned_owners": {"candidate-a": "cheng"},
        },
    )
    assert status == 200
    assert data.get("ok") is True
    assert fake.called is True
    assert fake.call["bucket_manager"] is sh.bucket_mgr
    assert fake.call["job_owner"] == github_web.ADMIN_RESTORE_SCOPE
    assert fake.call["decisions"] == {"candidate-a": "overwrite"}
    assert fake.call["assigned_owners"] == {"candidate-a": "cheng"}


@pytest.mark.asyncio
async def test_force_overrides_failed_backup(monkeypatch, import_route):
    handler, fake = import_route
    monkeypatch.setattr(github_web, "_pre_import_backup", lambda d: "")
    status, data = await _run(handler, {"force": True})
    assert status == 200
    assert fake.called is True


@pytest.mark.asyncio
async def test_import_rejects_when_bucket_manager_is_unavailable(
    monkeypatch, import_route
):
    handler, fake = import_route
    monkeypatch.setattr(sh, "bucket_mgr", None)
    backup_called = False

    def backup(_):
        nonlocal backup_called
        backup_called = True
        return "/backups/should-not-exist.zip"

    monkeypatch.setattr(github_web, "_pre_import_backup", backup)
    status, data = await _run(handler)
    assert status == 503
    assert data == {"ok": False, "error": "bucket manager 未初始化"}
    assert backup_called is False
    assert fake.called is False


@pytest.mark.asyncio
async def test_transaction_failure_never_returns_success(monkeypatch, import_route):
    handler, fake = import_route
    fake.result = {
        "ok": False,
        "markdown_committed": False,
        "transaction_id": "tx-failed",
        "error": "transaction failed",
    }
    monkeypatch.setattr(github_web, "_pre_import_backup", lambda d: "/backups/x.zip")
    status, data = await _run(handler)
    assert status == 422
    assert data["ok"] is False
    assert data["markdown_committed"] is False
    assert data["transaction_id"] == "tx-failed"


@pytest.mark.asyncio
async def test_derived_failure_reports_markdown_commit_boundary(
    monkeypatch, import_route
):
    handler, fake = import_route
    fake.result = {
        "ok": False,
        "markdown_committed": True,
        "transaction_id": "tx-derived-failed",
        "derived_state": "error",
        "errors": ["embedding provider unavailable"],
    }
    monkeypatch.setattr(github_web, "_pre_import_backup", lambda d: "/backups/x.zip")
    status, data = await _run(handler)
    assert status == 200
    assert data["ok"] is False
    assert data["markdown_committed"] is True
    assert data["derived_state"] == "error"
    assert data["transaction_id"] == "tx-derived-failed"
    assert data["errors"] == ["embedding provider unavailable"]
