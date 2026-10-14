"""钩子体检：面板上的「最后到达」与告警、报错补报、自检会话不入库、install.py --check。"""

import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ombrebrain.autorecall import AutoRecallService
from web import autorecall

CLIENT = Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client"


class _Buckets:
    async def list_all(self, include_archive=False):
        return []

    async def get(self, bucket_id):
        return None


def _svc(tmp_path, enabled=True):
    return AutoRecallService(_Buckets(), str(tmp_path), {"autorecall": {"enabled": enabled}})


def _at(svc, event, minutes_ago, now):
    svc.store.mark_seen(event)
    when = (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    conn = svc.store._connect()
    conn.execute("UPDATE module_state SET value = ? WHERE key = ?", (when, f"seen:{event}"))
    conn.close()


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def test_no_hooks_ever_warns(tmp_path):
    assert "还没收到任何钩子" in " ".join(_svc(tmp_path).hook_health(NOW)["warnings"])
    assert _svc(tmp_path, enabled=False).hook_health(NOW)["warnings"] == []


def test_prompt_without_later_stop_warns_only_after_a_while(tmp_path):
    svc = _svc(tmp_path)
    _at(svc, "session-start", 120, NOW)
    _at(svc, "stop", 90, NOW)
    _at(svc, "prompt", 10, NOW)  # 刚问完，回答可能还在写：不报
    assert svc.hook_health(NOW)["warnings"] == []
    _at(svc, "prompt", 45, NOW)  # 45 分钟前问的，之后没有存回复：报
    assert any("Stop" in w for w in svc.hook_health(NOW)["warnings"])
    _at(svc, "stop", 40, NOW)  # 存回复到了：不报
    assert svc.hook_health(NOW)["warnings"] == []


def test_missing_session_start_or_prompt_warns(tmp_path):
    svc = _svc(tmp_path)
    _at(svc, "prompt", 1, NOW)
    _at(svc, "stop", 1, NOW)
    assert any("SessionStart" in w for w in svc.hook_health(NOW)["warnings"])
    svc2 = _svc(tmp_path / "b")
    _at(svc2, "session-start", 5, NOW)
    _at(svc2, "stop", 1, NOW)
    assert any("UserPromptSubmit" in w for w in svc2.hook_health(NOW)["warnings"])


def test_recent_error_surfaces(tmp_path):
    svc = _svc(tmp_path)
    for event in ("session-start", "prompt", "stop"):
        _at(svc, event, 1, NOW)
    svc.store.add_errors([{"at": (NOW - timedelta(hours=1)).isoformat(), "where": "client:stop",
                           "message": "连不上 OB"}])
    health = svc.hook_health(NOW)
    assert any("连不上 OB" in w for w in health["warnings"]) and health["errors"][0]["where"] == "client:stop"
    svc.store.add_errors([{"at": "2026-10-01T00:00:00+00:00", "where": "x", "message": "旧的"}])
    assert len(svc.store.recent_errors()) == 2


# ---------------- 端点：记到达、收补报、自检会话不入库、服务端失败记一笔 ----------------

class _MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler
        return decorator


class _Req:
    def __init__(self, body):
        self._raw = json.dumps(body, ensure_ascii=False).encode()
        self.headers = {"content-length": str(len(self._raw)), "x-ombre-hook-token": "secret"}
        self.source = "c"

    async def body(self):
        return self._raw


def _routes(monkeypatch, svc):
    monkeypatch.setenv("OMBRE_HOOK_TOKEN", "secret")
    monkeypatch.setattr(autorecall.sh, "config", {"hooks": {}}, raising=False)
    monkeypatch.setattr(autorecall.sh, "_client_key", lambda r: r.source)
    monkeypatch.setattr(autorecall.sh, "autorecall_service", svc, raising=False)
    with autorecall._rate_lock:
        autorecall._source_events.clear()
        autorecall._global_events.clear()
    mcp = _MCP()
    autorecall.register(mcp)
    return mcp.routes


@pytest.mark.asyncio
async def test_arrivals_errors_and_check_sessions(monkeypatch, tmp_path):
    svc = _svc(tmp_path)
    routes = _routes(monkeypatch, svc)
    await routes[("POST", "/autorecall/prompt")](_Req({
        "session_id": "s", "prompt": "你好呀今天", "client_errors": [{"where": "stop", "message": "连不上 OB"}]}))
    assert "prompt" in svc.store.seen()
    assert svc.store.recent_errors()[0]["where"] == "client:stop"
    before = svc.store.counts()
    resp = await routes[("POST", "/autorecall/stop")](_Req({
        "session_id": "ob-check-abc", "messages": [{"key": "a", "role": "assistant", "text": "自检"}]}))
    assert json.loads(resp.body)["check"] == "ok"
    assert svc.store.counts() == before and "stop" in svc.store.seen()
    status = await routes[("POST", "/autorecall/status")](_Req({}))
    assert set(json.loads(status.body)["seen"]) >= {"prompt", "stop"}


@pytest.mark.asyncio
async def test_server_side_failure_is_recorded(monkeypatch, tmp_path):
    svc = _svc(tmp_path)
    routes = _routes(monkeypatch, svc)

    async def boom(*a, **k):
        raise RuntimeError("disk")

    svc.on_stop = boom
    resp = await routes[("POST", "/autorecall/stop")](_Req({"session_id": "s", "messages": []}))
    assert resp.status_code == 500
    assert svc.store.recent_errors()[-1]["where"] == "server:stop"


# ---------------- install.py --check 端到端 ----------------

class _FakeOB(BaseHTTPRequestHandler):
    seen: dict = {}
    files = {f"/autorecall/client/{n}": (CLIENT / n).read_bytes() for n in ("ob_hook.py", "ob_relay.py", "install.py")}

    def do_GET(self):
        body = type(self).files.get(self.path)
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write(body or b"")

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        name = self.path.rsplit("/", 1)[-1]
        try:
            event = json.loads(raw or b"{}").get("event")
        except ValueError:
            event = None
        if name == "session-end" and event == "pre-compact":
            name = "pre-compact"
        if name == "status":
            out = {"enabled": True, "seen": type(self).seen}
        else:
            type(self).seen[name] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            out = {"context": ""}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _check(tmp_path, url, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "OB_RELAY": "0",
           "OB_RELAY_STATE": str(tmp_path / "hooks" / ".ob_relay.json")}
    # --check 用装进 hooks 目录的那份（和用户实际跑的一样）；安装用仓库里的。
    script = tmp_path / "hooks" / "install.py" if "--check" in args else CLIENT / "install.py"
    return subprocess.run([sys.executable, str(script), "--settings", str(tmp_path / "settings.json"),
                           "--hooks-dir", str(tmp_path / "hooks"), *args],
                          capture_output=True, text=True, env=env, timeout=120)


def test_install_then_check_passes_and_detects_missing_stop(tmp_path):
    _FakeOB.seen = {}
    httpd = HTTPServer(("127.0.0.1", 0), _FakeOB)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert _check(tmp_path, url, "--url", url, "--token", "tok").returncode == 0
        assert (tmp_path / "hooks" / "install.py").exists()  # 自检用的副本装好了
        ok = _check(tmp_path, url, "--check")
        assert ok.returncode == 0 and ok.stdout.count("✓") >= 5, ok.stdout
        settings = json.loads((tmp_path / "settings.json").read_text())
        del settings["hooks"]["Stop"]
        (tmp_path / "settings.json").write_text(json.dumps(settings))
        bad = _check(tmp_path, url, "--check")
        assert bad.returncode == 1 and "存回复 Stop：settings.json 里没挂" in bad.stdout
    finally:
        httpd.shutdown()



@pytest.mark.asyncio
async def test_client_switch_blocks_only_that_client(monkeypatch, tmp_path):
    """面板上关掉某一家：那一家的请求只记到达、返回 disabled，不存不注入；别家照常。"""
    from ombrebrain.autorecall import normalize_client

    assert normalize_client(None) == "claude" and normalize_client("Codex") == "codex"
    assert normalize_client("whatever") == "sdk"
    svc = AutoRecallService(_Buckets(), str(tmp_path), {"autorecall": {"enabled": True, "clients": {"codex": False}}})
    assert svc.settings["clients"]["codex"] is False and svc.settings["clients"]["claude"] is True
    routes = _routes(monkeypatch, svc)
    blocked = await routes[("POST", "/autorecall/prompt")](_Req({"session_id": "s1", "client": "codex", "prompt": "嗨"}))
    assert json.loads(blocked.body) == {"disabled": True, "client": "codex"}
    assert not svc.store.session_turns("s1")
    ok = await routes[("POST", "/autorecall/prompt")](_Req({"session_id": "s2", "prompt": "嗨"}))
    assert ok.status_code == 200 and "disabled" not in json.loads(ok.body)
    assert svc.store.session_turns("s2")
    health = svc.hook_health()
    assert "prompt" in health["clients"]["codex"] and "prompt" in health["clients"]["claude"]
    assert not any("/" in key for key in health["seen"])


def test_clients_setting_validation_merges(monkeypatch):
    monkeypatch.setattr(autorecall.sh, "config", {"autorecall": {"clients": {"gemini": False}}}, raising=False)
    out = autorecall._validate_settings({"clients": {"codex": False}})
    assert out["clients"]["codex"] is False and out["clients"]["gemini"] is False and out["clients"]["claude"] is True
    for bad in ({"clients": {"nope": True}}, {"clients": {"codex": "yes"}}, {"clients": []}):
        with pytest.raises(ValueError):
            autorecall._validate_settings(bad)


@pytest.mark.asyncio
async def test_stale_hook_script_warns(monkeypatch, tmp_path):
    """钩子带上自己脚本的摘要；和 OB 自带的不一样就在面板上提示重跑安装，一样就不提示。"""
    from ombrebrain.autorecall.service import bundled_hook_hash

    bundled = bundled_hook_hash()
    assert len(bundled) == 12
    svc = _svc(tmp_path)
    routes = _routes(monkeypatch, svc)
    await routes[("POST", "/autorecall/prompt")](_Req({"session_id": "s", "prompt": "嗨", "script": bundled}))
    health = svc.hook_health()
    assert health["scripts"] == {"claude": bundled}
    assert not any("钩子脚本" in w for w in health["warnings"])
    assert not any(k.startswith("script@") for k in health["clients"]["claude"])
    await routes[("POST", "/autorecall/prompt")](_Req({"session_id": "s", "prompt": "嗨", "client": "codex",
                                                       "script": "0123456789ab"}))
    warnings = svc.hook_health()["warnings"]
    assert any("Codex CLI 用的钩子脚本和 OB 自带的不一样" in w for w in warnings)
    await routes[("POST", "/autorecall/prompt")](_Req({"session_id": "s", "prompt": "嗨", "script": "../../x"}))
    assert set(svc.hook_health()["scripts"]) == {"claude", "codex"}  # 不是 12 位十六进制的不记
