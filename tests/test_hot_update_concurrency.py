"""Red-team regressions for hot-update concurrency and event-loop safety."""

import asyncio
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest
import yaml
from starlette.responses import JSONResponse, StreamingResponse

import web._shared as sh
import web.meta as meta


class _MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


@pytest.fixture(autouse=True)
def _unlocked_update_job():
    assert not meta._UPDATE_JOB_LOCK.locked()
    yield
    # Avoid cascading failures if an assertion aborts before a test closes its
    # synthetic streaming response.
    if meta._UPDATE_JOB_LOCK.locked():
        meta._UPDATE_JOB_LOCK.release()


def _handler(monkeypatch):
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(sh, "config", {"update": {"channel": "branch"}})
    mcp = _MCP()
    meta.register(mcp)
    return mcp.routes[("POST", "/api/do-update")]


async def _close_after_first_event(response):
    await response.body_iterator.__anext__()
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_second_update_is_rejected_across_event_loop_threads(monkeypatch):
    handler = _handler(monkeypatch)
    first = await handler(object())

    # asyncio.Lock would not provide this process-wide guarantee when FastMCP
    # dispatches through a different thread/event loop.
    second = await asyncio.to_thread(lambda: asyncio.run(handler(object())))

    assert second.status_code == 409
    assert json.loads(second.body)["busy"] is True

    await _close_after_first_event(first)
    third = await handler(object())
    assert third.status_code == 200
    await _close_after_first_event(third)


@pytest.mark.asyncio
async def test_disconnect_reaps_worker_before_unlock_and_cleans_temp(
    monkeypatch,
):
    handler = _handler(monkeypatch)
    inspect_started = threading.Event()
    inspect_release = threading.Event()
    inspect_thread = []
    downloaded_to = []
    loop_thread = threading.get_ident()

    async def fake_download(_client, _url, destination):
        downloaded_to.append(destination)
        await asyncio.to_thread(Path(destination).touch)
        return 0

    def slow_inspect(_archive_path):
        inspect_thread.append(threading.get_ident())
        inspect_started.set()
        inspect_release.wait(timeout=5)
        return {
            "target_version": "",
            "version_bytes": None,
            "requirements_bytes": None,
            "plan": {
                "files": {},
                "skipped_unsafe": 0,
                "skipped_unlisted": 0,
                "verified": False,
                "abort": "synthetic stop",
            },
        }

    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_inspect_update_archive", slow_inspect)

    response = await handler(object())
    consume = asyncio.create_task(
        _consume(response)
    )
    while not inspect_started.is_set():
        await asyncio.sleep(0.005)

    # The ZIP worker is blocked, yet this loop still schedules normally.
    await asyncio.sleep(0.02)
    assert inspect_thread
    assert inspect_thread[0] != loop_thread

    consume.cancel()
    await asyncio.sleep(0.02)
    assert not consume.done()

    # Cancellation cannot free the job slot while to_thread is still reading
    # the archive; otherwise a new request could race its eventual result.
    busy = await handler(object())
    assert busy.status_code == 409

    inspect_release.set()
    with pytest.raises(asyncio.CancelledError):
        await consume

    assert downloaded_to
    assert not os.path.exists(os.path.dirname(downloaded_to[0]))

    available = await handler(object())
    assert available.status_code == 200
    await _close_after_first_event(available)


@pytest.mark.asyncio
async def test_cancelled_resource_worker_cleans_late_result_before_return():
    started = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()
    token = object()

    def produce_resource():
        started.set()
        release.wait(timeout=5)
        return token

    def cleanup_resource(value):
        assert value is token
        cleaned.set()

    task = asyncio.create_task(
        meta._await_update_worker(
            produce_resource,
            _cancel_result_cleanup=cleanup_resource,
        )
    )
    while not started.is_set():
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def _consume(response):
    return [chunk async for chunk in response.body_iterator]


def _write_release_zip(
    destination,
    *,
    server_source="NEW_VALUE = 2\n",
    version="2.7.1\n",
    requirements="same-package==1\n",
    requirements_lock=None,
):
    top = "Ombre-Brain-main/"
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(top + "VERSION", version)
        archive.writestr(top + "requirements.txt", requirements)
        if requirements_lock is not None:
            archive.writestr(top + "requirements.lock.txt", requirements_lock)
        archive.writestr(top + "src/server.py", server_source)
        archive.writestr(top + "frontend/app.js", "// new\n")


@pytest.mark.asyncio
async def test_successful_update_keeps_blocking_stages_off_loop_and_restarts(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.7.0\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.7.0\n", encoding="utf-8")
    (repo / "requirements.txt").write_text(
        "same-package==1\n", encoding="utf-8"
    )

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.7.0")

    async def fake_download(_client, _url, destination):
        await asyncio.to_thread(_write_release_zip, destination)
        return os.path.getsize(destination)

    loop_thread = threading.get_ident()
    stage_threads = {}
    for name in (
        "_inspect_update_archive",
        "_backup_update_tree",
        "_apply_update_files",
        "_compile_check_dir",
    ):
        original = getattr(meta, name)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            stage_threads[_name] = threading.get_ident()
            return _original(*args, **kwargs)

        monkeypatch.setattr(meta, name, wrapped)

    restarted = threading.Event()
    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_restart_self", restarted.set)

    response = await handler(object())
    events = "".join(await _consume(response))

    assert "data: RESTART" in events
    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == (
        "NEW_VALUE = 2\n"
    )
    assert (repo / "frontend" / "app.js").read_text(encoding="utf-8") == (
        "// new\n"
    )
    assert (repo / "VERSION").read_text(encoding="utf-8") == "2.7.1\n"
    assert (repo / "src" / "VERSION").read_text(encoding="utf-8") == "2.7.1\n"
    assert (repo / "_prev" / "src" / "server.py").read_text(
        encoding="utf-8"
    ) == "OLD_VALUE = 1\n"
    assert set(stage_threads) == {
        "_inspect_update_archive",
        "_backup_update_tree",
        "_apply_update_files",
        "_compile_check_dir",
    }
    assert all(thread_id != loop_thread for thread_id in stage_threads.values())

    assert await asyncio.to_thread(restarted.wait, 2)
    assert not meta._UPDATE_JOB_LOCK.locked()


@pytest.mark.asyncio
async def test_284_docker_code_dir_uses_image_lock_and_updates_without_pip(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    image_root = tmp_path / "image"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    image_root.mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.8.4\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.8.4\n", encoding="utf-8")
    old_requirements = "# MCP\r\nmcp>=1.0.0\r\n"
    new_requirements = "# MCP\nmcp>=1.27,<2\n"
    old_lock = "mcp==1.28.1 \\\r\n    --hash=sha256:abc\r\n"
    new_lock = "mcp==1.28.1 \\\n    --hash=sha256:abc\n"
    (image_root / "requirements.txt").write_text(
        old_requirements, encoding="utf-8", newline=""
    )
    (image_root / "requirements.lock.txt").write_text(
        old_lock, encoding="utf-8", newline=""
    )

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.8.4")
    monkeypatch.setenv("OMBRE_IMAGE_ROOT", str(image_root))
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)

    async def fake_download(_client, _url, destination):
        await asyncio.to_thread(
            _write_release_zip,
            destination,
            version="2.8.7\n",
            requirements=new_requirements,
            requirements_lock=new_lock,
        )
        return os.path.getsize(destination)

    def unexpected_install(*_args, **_kwargs):
        raise AssertionError("发布 lock 未变化时不应调用 pip")

    restarted = threading.Event()
    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_install_update_requirements", unexpected_install)
    monkeypatch.setattr(meta, "_restart_self", restarted.set)

    response = await handler(object())
    events = "".join(await _consume(response))

    assert "data: RESTART" in events
    assert "ERROR:" not in events
    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == "NEW_VALUE = 2\n"
    assert (repo / "frontend" / "app.js").read_text(encoding="utf-8") == "// new\n"
    assert (repo / "VERSION").read_text(encoding="utf-8") == "2.8.7\n"
    assert (repo / "src" / "VERSION").read_text(encoding="utf-8") == "2.8.7\n"
    assert (repo / "requirements.txt").read_bytes() == new_requirements.encode()
    assert (repo / "requirements.lock.txt").read_bytes() == new_lock.encode()
    assert await asyncio.to_thread(restarted.wait, 2)
    assert not meta._UPDATE_JOB_LOCK.locked()


@pytest.mark.asyncio
async def test_legacy_runtime_without_lock_updates_when_installed_versions_match(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.8.8\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.8.8\n", encoding="utf-8")
    lock = "package==1 \\\n+    --hash=sha256:" + "a" * 64 + "\n"

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.8.8")
    monkeypatch.setattr(sh, "in_docker", lambda: False)
    monkeypatch.delenv("OMBRE_IMAGE_ROOT", raising=False)
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)
    monkeypatch.setattr(
        meta, "_runtime_satisfies_locked_versions", lambda data: data == lock.encode().strip()
    )

    async def fake_download(_client, _url, destination):
        await asyncio.to_thread(
            _write_release_zip,
            destination,
            version="2.8.13\n",
            requirements="package>=1\n",
            requirements_lock=lock,
        )
        return os.path.getsize(destination)

    def unexpected_install(*_args, **_kwargs):
        raise AssertionError("运行环境已满足 lock 时不应执行 pip 安装")

    restarted = threading.Event()
    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_install_update_requirements", unexpected_install)
    monkeypatch.setattr(meta, "_restart_self", restarted.set)

    response = await handler(object())
    events = "".join(await _consume(response))

    assert "data: RESTART" in events
    assert "ERROR:" not in events
    assert (repo / "requirements.lock.txt").read_text(encoding="utf-8") == lock
    assert (repo / "requirements.txt").read_text(encoding="utf-8") == "package>=1\n"
    assert (repo / "VERSION").read_text(encoding="utf-8") == "2.8.13\n"
    assert await asyncio.to_thread(restarted.wait, 2)
    assert not meta._UPDATE_JOB_LOCK.locked()


@pytest.mark.asyncio
async def test_changed_release_lock_with_pip_disabled_rejects_before_touching_disk(
    monkeypatch, tmp_path
):
    """依赖变化又不让装 pip 时，检查提前到备份/写盘之前，压根不该碰任何文件——
    不是「写完再回滚」。"""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.8.7\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.8.7\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("package==1\n", encoding="utf-8")
    (repo / "requirements.lock.txt").write_text("package==1\n", encoding="utf-8")

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.8.7")
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)

    async def fake_download(_client, _url, destination):
        await asyncio.to_thread(
            _write_release_zip,
            destination,
            version="2.8.8\n",
            requirements="package==2\n",
            requirements_lock="package==2\n",
        )
        return os.path.getsize(destination)

    install_called = False

    def unexpected_install(*_args, **_kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("pip 关闭时不应调用安装器")

    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_install_update_requirements", unexpected_install)
    restarted = threading.Event()
    monkeypatch.setattr(meta, "_restart_self", restarted.set)

    response = await handler(object())
    events = "".join(await _consume(response))

    assert "ERROR:新版依赖清单有变化" in events
    assert "data: RESTART" not in events
    assert install_called is False
    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == "OLD_VALUE = 1\n"
    assert (repo / "frontend" / "app.js").read_text(encoding="utf-8") == "// old\n"
    assert (repo / "VERSION").read_text(encoding="utf-8") == "2.8.7\n"
    assert (repo / "src" / "VERSION").read_text(encoding="utf-8") == "2.8.7\n"
    assert (repo / "requirements.txt").read_text(encoding="utf-8") == "package==1\n"
    assert (repo / "requirements.lock.txt").read_text(encoding="utf-8") == "package==1\n"
    assert restarted.is_set() is False
    assert not meta._UPDATE_JOB_LOCK.locked()
    # 拒绝发生在备份步骤之前：不该有 _prev 回滚点被创建过。
    assert not (repo / "_prev").exists()


@pytest.mark.asyncio
async def test_partial_dependency_manifest_sync_failure_rolls_back_handler_state(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.8.7\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.8.7\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("package>=1\n", encoding="utf-8")
    (repo / "requirements.lock.txt").write_text("package==1\n", encoding="utf-8")

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.8.7")

    async def fake_download(_client, _url, destination):
        await asyncio.to_thread(
            _write_release_zip,
            destination,
            version="2.8.8\n",
            requirements="package>=1  # refreshed\n",
            requirements_lock="package==1\n",
        )
        return os.path.getsize(destination)

    def partial_sync(repo_root, requirements_bytes, _requirements_lock_bytes):
        meta._atomic_write_bytes(
            os.path.join(repo_root, "requirements.txt"), requirements_bytes
        )
        raise OSError("synthetic lock manifest write failure")

    restarted = threading.Event()
    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_sync_update_dependency_manifests", partial_sync)
    monkeypatch.setattr(meta, "_restart_self", restarted.set)

    response = await handler(object())
    events = "".join(await _consume(response))

    assert "ERROR:依赖处理失败" in events
    assert "data: RESTART" not in events
    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == "OLD_VALUE = 1\n"
    assert (repo / "frontend" / "app.js").read_text(encoding="utf-8") == "// old\n"
    assert (repo / "VERSION").read_text(encoding="utf-8") == "2.8.7\n"
    assert (repo / "src" / "VERSION").read_text(encoding="utf-8") == "2.8.7\n"
    assert (repo / "requirements.txt").read_text(encoding="utf-8") == "package>=1\n"
    assert (repo / "requirements.lock.txt").read_text(encoding="utf-8") == "package==1\n"
    assert restarted.is_set() is False
    assert not meta._UPDATE_JOB_LOCK.locked()


@pytest.mark.asyncio
async def test_compile_failure_happens_before_allowed_pip_install(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.8.7\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.8.7\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("package>=1\n", encoding="utf-8")
    (repo / "requirements.lock.txt").write_text("package==1\n", encoding="utf-8")

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.8.7")
    monkeypatch.setenv("OMBRE_UPDATE_ALLOW_PIP", "1")

    async def fake_download(_client, _url, destination):
        await asyncio.to_thread(
            _write_release_zip,
            destination,
            server_source="def broken(:\n",
            version="2.8.8\n",
            requirements="package>=2\n",
            requirements_lock="package==2\n",
        )
        return os.path.getsize(destination)

    install_called = False

    def unexpected_install(*_args, **_kwargs):
        nonlocal install_called
        install_called = True
        raise AssertionError("代码自检失败后不应再执行 pip")

    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_install_update_requirements", unexpected_install)

    response = await handler(object())
    events = "".join(await _consume(response))

    assert "新代码自检未通过" in events
    assert "data: RESTART" not in events
    assert install_called is False
    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == "OLD_VALUE = 1\n"
    assert (repo / "requirements.txt").read_text(encoding="utf-8") == "package>=1\n"
    assert (repo / "requirements.lock.txt").read_text(encoding="utf-8") == "package==1\n"
    assert not meta._UPDATE_JOB_LOCK.locked()


@pytest.mark.asyncio
async def test_disconnect_during_source_write_rolls_back_before_unlock(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "frontend").mkdir()
    (repo / "src" / "server.py").write_text("OLD_VALUE = 1\n", encoding="utf-8")
    (repo / "frontend" / "app.js").write_text("// old\n", encoding="utf-8")
    (repo / "VERSION").write_text("2.7.0\n", encoding="utf-8")
    (repo / "src" / "VERSION").write_text("2.7.0\n", encoding="utf-8")
    (repo / "requirements.txt").write_text(
        "same-package==1\n", encoding="utf-8"
    )

    handler = _handler(monkeypatch)
    monkeypatch.setattr(sh, "repo_root", str(repo))
    monkeypatch.setattr(sh, "version", "2.7.0")

    downloaded_to = []

    async def fake_download(_client, _url, destination):
        downloaded_to.append(destination)
        await asyncio.to_thread(_write_release_zip, destination)
        return os.path.getsize(destination)

    original_apply = meta._apply_update_files
    apply_started = threading.Event()
    apply_release = threading.Event()

    def slow_apply(*args, **kwargs):
        updated = original_apply(*args, **kwargs)
        apply_started.set()
        apply_release.wait(timeout=5)
        return updated

    monkeypatch.setattr(meta, "_download_update_archive_to_file", fake_download)
    monkeypatch.setattr(meta, "_apply_update_files", slow_apply)

    response = await handler(object())
    consume = asyncio.create_task(_consume(response))
    while not apply_started.is_set():
        await asyncio.sleep(0.005)

    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == (
        "NEW_VALUE = 2\n"
    )
    consume.cancel()
    await asyncio.sleep(0.02)
    assert not consume.done()
    assert (await handler(object())).status_code == 409

    apply_release.set()
    with pytest.raises(asyncio.CancelledError):
        await consume

    assert (repo / "src" / "server.py").read_text(encoding="utf-8") == (
        "OLD_VALUE = 1\n"
    )
    assert (repo / "frontend" / "app.js").read_text(encoding="utf-8") == (
        "// old\n"
    )
    assert (repo / "VERSION").read_text(encoding="utf-8") == "2.7.0\n"
    assert (repo / "src" / "VERSION").read_text(encoding="utf-8") == "2.7.0\n"
    assert not meta._UPDATE_JOB_LOCK.locked()
    assert downloaded_to
    assert not os.path.exists(os.path.dirname(downloaded_to[0]))


@pytest.mark.asyncio
async def test_asgi_send_failure_releases_unstarted_stream(monkeypatch):
    reservation = meta._UpdateJobReservation()
    assert reservation.acquire()

    async def fail_before_iteration(_self, _scope, _receive, _send):
        raise RuntimeError("synthetic send failure")

    monkeypatch.setattr(StreamingResponse, "__call__", fail_before_iteration)

    async def body():
        yield "never reached"

    response = meta._UpdateStreamingResponse(body(), reservation)
    with pytest.raises(RuntimeError, match="send failure"):
        await response({}, None, None)

    next_reservation = meta._UpdateJobReservation()
    assert next_reservation.acquire()
    next_reservation.release()


class _JsonRequest:
    def __init__(self, body, method="POST"):
        self._body = body
        self.method = method

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_update_settings_endpoint_persists_and_takes_effect_immediately(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(sh, "config", {"update": {"channel": "branch"}})
    monkeypatch.setattr(sh, "version", "2.16.1")
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("OMBRE_UPDATE_ALLOW_PIP", raising=False)
    mcp = _MCP()
    meta.register(mcp)
    settings_handler = mcp.routes[("POST", "/api/update-settings")]
    info_handler = mcp.routes[("GET", "/api/update-info")]

    before = json.loads((await info_handler(object())).body)
    assert before["update_allow_pip_install"] is False
    assert meta._pip_install_allowed() is False

    saved = json.loads(
        (await settings_handler(_JsonRequest({"allow_pip_install": True}))).body
    )
    assert saved == {"ok": True, "allow_pip_install": True}

    # 立即在运行态生效，不需要重启。
    assert meta._pip_install_allowed() is True
    after = json.loads((await info_handler(object())).body)
    assert after["update_allow_pip_install"] is True

    # 真的落盘了，不是只改了内存。
    on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert on_disk["update"]["allow_pip_install"] is True

    off = json.loads(
        (await settings_handler(_JsonRequest({"allow_pip_install": False}))).body
    )
    assert off == {"ok": True, "allow_pip_install": False}
    assert meta._pip_install_allowed() is False


@pytest.mark.asyncio
async def test_update_settings_endpoint_rejects_missing_field(monkeypatch):
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(sh, "config", {})
    mcp = _MCP()
    meta.register(mcp)
    handler = mcp.routes[("POST", "/api/update-settings")]

    response = await handler(_JsonRequest({}))
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_archived_letter_maintenance_get_is_authenticated_dry_run(
    monkeypatch,
):
    import tools._common as common

    calls = []

    async def fake_restore(manager, *, ids=None, apply=False):
        calls.append((manager, ids, apply))
        return {
            "candidate_count": 1,
            "candidate_ids": ["letter-1"],
            "excluded_count": 0,
            "exclusions": [],
        }

    manager = object()
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(sh, "bucket_mgr", manager)
    monkeypatch.setattr(common, "restore_archived_letters", fake_restore)
    mcp = _MCP()
    meta.register(mcp)

    response = await mcp.routes[
        ("GET", "/api/maintenance/restore-archived-letters")
    ](_JsonRequest(None, method="GET"))

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert json.loads(response.body) == {
        "ok": True,
        "candidate_count": 1,
        "candidate_ids": ["letter-1"],
        "excluded_count": 0,
        "exclusions": [],
    }
    assert calls == [(manager, None, False)]


@pytest.mark.asyncio
async def test_archived_letter_maintenance_post_requires_bounded_exact_ids(
    monkeypatch,
):
    import tools._common as common

    calls = []

    async def fake_restore(manager, *, ids=None, apply=False):
        calls.append((manager, ids, apply))
        return {
            "requested_count": len(ids or []),
            "restored_count": len(ids or []),
            "unchanged_count": 0,
            "failed_count": 0,
            "results": [
                {"id": bucket_id, "reason": "restored"}
                for bucket_id in (ids or [])
            ],
        }

    manager = object()
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(sh, "bucket_mgr", manager)
    monkeypatch.setattr(common, "restore_archived_letters", fake_restore)
    mcp = _MCP()
    meta.register(mcp)
    handler = mcp.routes[("POST", "/api/maintenance/restore-archived-letters")]

    invalid_bodies = (
        None,
        [],
        "letter-1",
        {},
        {"ids": "letter-1"},
        {"ids": []},
        {"ids": [""]},
        {"ids": ["letter-1", 1]},
        {"ids": ["same-id"] * 101},
    )
    for body in invalid_bodies:
        response = await handler(_JsonRequest(body))
        assert response.status_code == 400
    assert calls == []

    response = await handler(
        _JsonRequest({"ids": ["letter-1", "letter-1", "letter-2"]})
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["ok"] is True
    assert payload["requested_count"] == 2
    assert payload["results"] == [
        {"id": "letter-1", "reason": "restored"},
        {"id": "letter-2", "reason": "restored"},
    ]
    assert calls == [(manager, ["letter-1", "letter-2"], True)]


@pytest.mark.asyncio
async def test_archived_letter_maintenance_stops_before_helper_on_auth_or_json_error(
    monkeypatch,
):
    import tools._common as common

    async def forbidden_helper(*_args, **_kwargs):
        pytest.fail("认证或 JSON 失败时不得进入维护 helper")

    monkeypatch.setattr(common, "restore_archived_letters", forbidden_helper)
    monkeypatch.setattr(sh, "bucket_mgr", object())

    denied = JSONResponse({"ok": False}, status_code=401)
    monkeypatch.setattr(sh, "_require_auth", lambda _request: denied)
    denied_mcp = _MCP()
    meta.register(denied_mcp)
    denied_response = await denied_mcp.routes[
        ("GET", "/api/maintenance/restore-archived-letters")
    ](_JsonRequest(None, method="GET"))
    assert denied_response.status_code == 401
    assert denied_response.headers["Cache-Control"] == "no-store"

    class BrokenJsonRequest(_JsonRequest):
        async def json(self):
            raise ValueError("malformed JSON")

    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    malformed_mcp = _MCP()
    meta.register(malformed_mcp)
    malformed_response = await malformed_mcp.routes[
        ("POST", "/api/maintenance/restore-archived-letters")
    ](BrokenJsonRequest(None))
    assert malformed_response.status_code == 400
    assert malformed_response.headers["Cache-Control"] == "no-store"
    assert json.loads(malformed_response.body)["reason"] == "invalid_json"
