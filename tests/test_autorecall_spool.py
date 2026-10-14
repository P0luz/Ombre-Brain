"""断线不丢：存回复发不出去时落盘，连上后补发（照 claude-mem「先落盘再投递」）。"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_autorecall_gateway import FakeOB, _wait  # noqa: E402

_HOOK = Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client" / "ob_hook.py"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(tmp_path, url):
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "OB_URL": url, "OB_HOOK_TOKEN": "tok",
            "OB_RELAY": "0", "OB_RELAY_STATE": str(tmp_path / "hooks" / ".ob_relay.json")}


def _spool(tmp_path):
    return sorted((tmp_path / "hooks" / "spool").glob("*.json"))


def _deliver(tmp_path, url, payload):
    return subprocess.run([sys.executable, str(_HOOK), "--deliver", "stop"], input=json.dumps(payload),
                          capture_output=True, text=True, env=_env(tmp_path, url), timeout=30)


STOP = {"session_id": "s1", "cwd": "/w", "client": "claude",
        "messages": [{"key": "a:p1:abcd", "role": "assistant", "text": "这句回复不能丢"}]}


def test_unreachable_ob_spools_then_flush_delivers(tmp_path):
    down = f"http://127.0.0.1:{_free_port()}"  # 没人听的端口 = OB 断了
    _deliver(tmp_path, down, STOP)
    files = _spool(tmp_path)
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    assert json.loads(files[0].read_text())["path"] == "stop"
    assert "已暂存" in (tmp_path / "hooks" / "ob_hook.log").read_text(encoding="utf-8")

    ob = FakeOB()
    try:
        subprocess.run([sys.executable, str(_HOOK), "--flush"], env=_env(tmp_path, ob.url), timeout=30)
        stops = [c[1] for c in ob.calls if c[0] == "stop"]
        assert stops == [STOP] and _spool(tmp_path) == []
    finally:
        ob.server.shutdown()


def test_server_error_spools(tmp_path):
    bad = FakeOB(down=True)  # 一律 500
    try:
        _deliver(tmp_path, bad.url, STOP)
        assert len(_spool(tmp_path)) == 1
    finally:
        bad.server.shutdown()


def test_flush_stops_at_first_failure_and_keeps_order(tmp_path, monkeypatch):
    monkeypatch.setenv("OB_RELAY_STATE", str(tmp_path / "hooks" / ".ob_relay.json"))
    sys.path.insert(0, str(_HOOK.parent))
    import ob_hook

    for i in range(3):
        ob_hook.spool_put("stop", json.dumps({"n": i}).encode())
    seen = []

    def flaky(path, body):
        seen.append(json.loads(body)["n"])
        return 200 if len(seen) < 2 else 503

    assert ob_hook.spool_flush(flaky) == 1
    assert seen == [0, 1] and ob_hook.spool_count() == 2  # 第二条失败就停，留着下次
    assert ob_hook.spool_flush(lambda p, b: 401) == 2  # 4xx 重发也不会成，算处理完
    assert ob_hook.spool_count() == 0


def test_spool_is_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("OB_RELAY_STATE", str(tmp_path / "hooks" / ".ob_relay.json"))
    sys.path.insert(0, str(_HOOK.parent))
    import ob_hook

    monkeypatch.setattr(ob_hook, "_SPOOL_LIMIT", 5)
    for i in range(8):
        ob_hook.spool_put("stop", json.dumps({"n": i}).encode())
    kept = [json.loads(json.loads(p.read_text())["body"])["n"] for p in sorted((tmp_path / "hooks" / "spool").glob("*.json"))]
    assert kept == [3, 4, 5, 6, 7]  # 丢最旧的


def test_successful_prompt_triggers_background_flush(tmp_path):
    down = f"http://127.0.0.1:{_free_port()}"
    _deliver(tmp_path, down, STOP)
    assert len(_spool(tmp_path)) == 1
    ob = FakeOB()
    try:
        subprocess.run([sys.executable, str(_HOOK), "prompt"], input=json.dumps({"session_id": "s1", "prompt": "嗨"}),
                       capture_output=True, text=True, env=_env(tmp_path, ob.url), timeout=30)
        assert _wait(lambda: any(c[0] == "stop" for c in ob.calls), 10)
        assert _wait(lambda: _spool(tmp_path) == [], 5)
    finally:
        ob.server.shutdown()
