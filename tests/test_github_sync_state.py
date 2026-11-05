"""同步历史落盘与按到期补跑；所有数据和时间均为测试替身。"""
import ast
import asyncio
import inspect
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import github_sync as module
from github_sync import GitHubSync


def instance(root, **kwargs):
    # 兼容旧构造函数，让旧实现失败在行为断言上。
    options = {"token": "fake-secret", "repo": "owner/repo", **kwargs}
    if "state_dir" in inspect.signature(GitHubSync).parameters:
        options["state_dir"] = str(root)
    return GitHubSync(**options)


@pytest.mark.asyncio
async def test_restart_and_target_identity(tmp_path, monkeypatch):
    gh = instance(tmp_path)
    (tmp_path / "a.md").write_text("fake memory")
    monkeypatch.setattr(gh, "_batch_commit", AsyncMock(return_value=1))
    assert (await gh.sync(str(tmp_path)))["ok"]
    stamp = gh.status()["last_sync"]
    assert instance(tmp_path).status()["last_sync"] == stamp
    for change in ({"repo": "other/repo"}, {"branch": "other"}, {"path_prefix": "other"}):
        assert instance(tmp_path, **change).status()["last_sync"] is None
    data = json.loads((tmp_path / "_github_sync_state.json").read_text())
    assert data["last_success_at"] == stamp
    assert data["last_attempt_at"]
    assert data["file_count"] == 1


@pytest.mark.parametrize("content", [None, "{", "[]", '{"target": null}'])
def test_missing_or_corrupt_history(tmp_path, content):
    if content is not None:
        (tmp_path / "_github_sync_state.json").write_text(content)
    assert instance(tmp_path).status()["last_sync"] is None


@pytest.mark.asyncio
async def test_write_failure_does_not_fail_upload(tmp_path, monkeypatch, caplog):
    gh = instance(tmp_path)
    (tmp_path / "a.md").write_text("fake memory")
    monkeypatch.setattr(gh, "_batch_commit", AsyncMock(return_value=1))
    def fail(*args):
        raise OSError("fake disk full")
    monkeypatch.setattr(module, "atomic_write_text", fail, raising=False)
    assert (await gh.sync(str(tmp_path)))["ok"]
    assert gh.status()["last_sync"]
    assert "fake disk full" in caplog.text


@pytest.mark.asyncio
async def test_failure_history_is_sanitized_and_empty_success_resets(tmp_path, monkeypatch):
    gh = instance(tmp_path)
    (tmp_path / "a.md").write_text("fake memory")
    monkeypatch.setattr(gh, "_batch_commit", AsyncMock(side_effect=RuntimeError("fake-secret failed")))
    assert not (await gh.sync(str(tmp_path)))["ok"]
    reloaded = instance(tmp_path)
    assert reloaded.consecutive_failures == 1
    assert "fake-secret" not in reloaded.status()["last_error"]
    assert reloaded.status()["last_sync"] is None
    monkeypatch.setattr(reloaded, "_collect_files", lambda *a: {})
    assert (await reloaded.sync(str(tmp_path)))["ok"]
    assert instance(tmp_path).consecutive_failures == 0


@pytest.mark.asyncio
async def test_interval_save_keeps_success(tmp_path, monkeypatch):
    from tests.test_github_config_api import _setup, JsonRequest
    import web.github as web
    mcp, _, restarts = _setup(monkeypatch, tmp_path, {"token": "fake-secret", "repo": "owner/repo"})
    web.sh.config["buckets_dir"] = str(tmp_path)
    gh = instance(tmp_path)
    await gh.sync(str(tmp_path))
    monkeypatch.setattr(web.sh, "github_sync_instance", gh)
    response = await mcp.routes[("POST", "/api/github/config")](JsonRequest({"auto_interval_minutes": 360}))
    assert response.status_code == 200
    assert web.sh.github_sync_instance.status()["last_sync"] == gh.status()["last_sync"]
    assert restarts == [360]


@pytest.mark.asyncio
async def test_restore_keeps_upload_time(tmp_path, monkeypatch):
    import httpx
    import base64
    gh = instance(tmp_path)
    await gh.sync(str(tmp_path))
    before = gh.status()["last_sync"]
    monkeypatch.setattr(module, "_now_iso", lambda: "2026-11-04T12:00:00+00:00")
    async def request(client, method, url, **kwargs):
        if "/git/ref/" in url:
            data = {"object": {"sha": "head"}}
        elif "/git/commits/" in url:
            data = {"tree": {"sha": "tree"}}
        elif "/git/trees/" in url:
            data = {"tree": [{"type": "blob", "path": "ombre/a.md", "sha": "blob", "size": 4}]}
        else:
            data = {"encoding": "base64", "content": base64.b64encode(b"fake").decode()}
        return httpx.Response(200, json=data, request=httpx.Request(method, url))
    monkeypatch.setattr(gh, "_request", request)
    assert (await gh.import_from_github(str(tmp_path)))["ok"]
    assert gh.status()["last_sync"] == before
    assert instance(tmp_path).status()["last_restore_at"] == "2026-11-04T12:00:00+00:00"


def load_loop(gh, root):
    # 直接编译生产函数，避免 import server 启动整套服务。
    tree = ast.parse(Path("src/server.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_github_sync_loop")
    scope = {"_wsh": SimpleNamespace(github_sync_instance=gh), "config": {"buckets_dir": str(root)},
             "logger": logging.getLogger("test"), "datetime": datetime, "timezone": timezone}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "src/server.py", "exec"), scope)
    return scope[fn.name]


@pytest.mark.asyncio
@pytest.mark.parametrize("age, failure, interval, expected", [
    (None, None, 360, ["sync", 21600]), (18000, None, 360, [3600, "sync", 21600]),
    (22000, None, 360, ["sync", 21600]), (None, "upload", 360, ["sync", 1800]),
    (None, "validate", 360, [1800]), (None, "exception", 10, ["sync", 600]),
])
async def test_due_and_retry(tmp_path, monkeypatch, age, failure, interval, expected):
    now = [1800000000.0]
    def stamp():
        return datetime.fromtimestamp(now[0], timezone.utc).isoformat()
    gh = SimpleNamespace(is_validated=False, last_success_at=None if age is None else datetime.fromtimestamp(now[0]-age, timezone.utc).isoformat())
    gh.status = lambda: {"last_sync": gh.last_success_at}
    gh.record_failure = lambda error: None
    events = []
    async def validate():
        gh.is_validated = failure != "validate"
        return {"ok": gh.is_validated}
    async def sync(root):
        events.append("sync")
        if failure == "exception":
            raise RuntimeError("fake failure")
        if failure != "upload":
            gh.last_success_at = stamp()
        return {"ok": failure != "upload"}
    async def sleep(seconds):
        events.append(seconds)
        if len(events) >= len(expected):
            raise asyncio.CancelledError
        now[0] += seconds
    gh.validate, gh.sync = validate, sync
    loop = load_loop(gh, tmp_path)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    kwargs = {"now": lambda: now[0], "sleep": sleep} if "now" in inspect.signature(loop).parameters else {}
    with pytest.raises(asyncio.CancelledError):
        await loop(interval, **kwargs)
    assert events == expected


@pytest.mark.asyncio
async def test_state_excluded_from_upload_buckets_and_backup(tmp_path):
    from tests.test_backup_skips_internal_dirs import _vault_with_app, _engine, _config, _META, _import
    from bucket_manager import BucketManager
    from ombrebrain.storage.backup_archive import build_export_archive, read_backup_archive
    _vault_with_app(tmp_path)
    state = tmp_path / "_github_sync_state.json"
    state.write_text('{"fake": "state"}')
    gh = instance(tmp_path)
    assert set(gh._collect_files(str(tmp_path))) == {"dynamic/general/memory_memory-1.md"}
    engine = _engine(_config(tmp_path))
    manager = BucketManager(_config(tmp_path), embedding_engine=engine)
    assert all(not p.endswith(state.name) for _, _, p in manager._iter_md_files([str(tmp_path)]))
    assert [b["metadata"]["id"] for b in await manager.list_all(include_archive=True)] == ["memory-1"]
    payload, _ = build_export_archive(str(tmp_path), engine.db_path, _META)
    assert not any(state.name in name for name in read_backup_archive(payload)["files"])
    parsed, migrate, target = await _import(tmp_path, payload)
    assert parsed["ok"]
    await migrate.apply({})
    assert migrate.get_status()["result"]["imported"] == 1
    assert not (tmp_path / "target" / state.name).exists()
    # 外部旧包即使夹带此 JSON，也不能把它导入为桶或覆盖本地状态。
    import io
    import zipfile
    foreign = io.BytesIO()
    with zipfile.ZipFile(foreign, "w") as archive:
        archive.writestr("buckets/" + state.name, state.read_bytes())
    rejected = await migrate.parse_zip(foreign.getvalue())
    assert not rejected["ok"]
    assert not (tmp_path / "target" / state.name).exists()


@pytest.mark.asyncio
async def test_repeated_short_lived_starts_and_manual_deferral(tmp_path, monkeypatch):
    now = [1800000000.0]
    gh = instance(tmp_path)
    monkeypatch.setattr(module, "_now_iso", lambda: datetime.fromtimestamp(now[0], timezone.utc).isoformat())
    await gh.sync(str(tmp_path))
    for _ in range(3):
        now[0] += 7 * 3600
        gh = instance(tmp_path)
        gh.is_validated = True
        events = []
        real_sync = gh.sync
        async def sync(root):
            events.append("sync")
            return await real_sync(root)
        gh.sync = sync
        async def sleep(seconds):
            events.append(seconds)
            raise asyncio.CancelledError
        loop = load_loop(gh, tmp_path)
        monkeypatch.setattr(asyncio, "sleep", sleep)
        kwargs = {"now": lambda: now[0], "sleep": sleep} if "now" in inspect.signature(loop).parameters else {}
        with pytest.raises(asyncio.CancelledError):
            await loop(360, **kwargs)
        assert events == ["sync", 21600]
    # 自动任务等待期间，手动成功将下一次到期时间顺延。
    events = []
    async def manual_sleep(seconds):
        events.append(seconds)
        if len(events) == 2:
            raise asyncio.CancelledError
        now[0] += 18000
        await real_sync(str(tmp_path))
        now[0] += 3600
    loop = load_loop(gh, tmp_path)
    monkeypatch.setattr(asyncio, "sleep", manual_sleep)
    kwargs = {"now": lambda: now[0], "sleep": manual_sleep} if "now" in inspect.signature(loop).parameters else {}
    with pytest.raises(asyncio.CancelledError):
        await loop(360, **kwargs)
    assert events == [21600, 18000]
