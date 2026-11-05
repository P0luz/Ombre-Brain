"""src/ombrebrain/autorecall/client/ob_hook.py：四种模式的请求与输出，以及出错时静默退出 0。"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client" / "ob_hook.py"
# 日志 / 待补报 / 中继状态一律写进临时目录，绝不碰真实的 ~/.claude/hooks。
_STATE_DIR = Path(tempfile.mkdtemp(prefix="ob-hook-test-"))


class _Recorder(BaseHTTPRequestHandler):
    calls: list = []
    reply: dict = {}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        type(self).calls.append((self.path, self.headers.get("x-ombre-hook-token"), json.loads(body)))
        raw = json.dumps(type(self).reply).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Recorder.calls = []
    _Recorder.reply = {}
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _wait_calls(n=1, timeout=10):
    """不用等的请求交给脱离的子进程发，测试要等它到。"""
    end = time.time() + timeout
    while len(_Recorder.calls) < n and time.time() < end:
        time.sleep(0.05)
    return _Recorder.calls


def _run(mode, payload, url, token="tok", extra_env=None):
    env = {"OB_URL": url, "PATH": "/usr/bin:/bin", "OB_RELAY": "0",
           "OB_RELAY_STATE": str(_STATE_DIR / "relay.json"), **(extra_env or {})}
    if token:
        env["OB_HOOK_TOKEN"] = token
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), mode], input=json.dumps(payload), capture_output=True,
        text=True, env=env, timeout=30,
    )
    return proc.returncode, proc.stdout


def test_prompt_posts_and_emits_additional_context(server):
    _Recorder.reply = {"context": "记忆：周五吃了火锅"}
    code, out = _run("prompt", {"session_id": "s", "prompt": "还记得火锅吗", "prompt_id": "p1", "cwd": "/w"}, server)
    assert code == 0
    path, token, body = _Recorder.calls[0]
    assert path == "/autorecall/prompt" and token == "tok"
    script = body.pop("script")  # 钩子脚本自身的内容摘要，OB 用它判断脚本是否过期
    assert len(script) == 12 and int(script, 16) >= 0
    spec = body.pop("injection")
    assert spec["version"] == 2 and spec["mode"] == "hook" and spec["request_id"] == "p1"
    assert body == {"session_id": "s", "prompt": "还记得火锅吗", "prompt_id": "p1", "cwd": "/w", "client": "claude"}
    assert json.loads(out) == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "记忆：周五吃了火锅"}}


def test_session_start_and_empty_context_prints_nothing(server):
    _Recorder.reply = {"context": ""}
    code, out = _run("session-start", {"session_id": "s", "source": "compact"}, server)
    assert code == 0 and out == ""
    body = dict(_Recorder.calls[0][2])
    assert len(body.pop("script")) == 12
    assert body.pop("injection")["delivered"] == []
    assert body == {"session_id": "s", "source": "compact", "cwd": "", "client": "claude"}


def test_stop_sends_last_assistant_message(server):
    code, out = _run("stop", {"session_id": "s", "prompt_id": "p1", "last_assistant_message": "好呀"}, server)
    assert code == 0 and out == ""
    [message] = _wait_calls()[0][2]["messages"]
    assert message["key"].startswith("a:p1:") and message["text"] == "好呀"


def test_session_end_backfills_user_text_only(server, tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "promptId": "p1", "uuid": "x1", "timestamp": "2026-10-14T05:00:00.000Z",
         "origin": {"kind": "human"}, "message": {"role": "user", "content": "今天想吃火锅"}},
        {"type": "user", "uuid": "x5", "isCompactSummary": True,
         "message": {"content": "This session is being continued from a previous conversation"}},
        {"type": "user", "uuid": "x6", "origin": {"kind": "task-notification"}, "message": {"content": "后台任务完成"}},
        {"type": "user", "uuid": "x2", "toolUseResult": {}, "message": {"content": [{"type": "tool_result"}]}},
        {"type": "user", "uuid": "x3", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "assistant", "uuid": "x4", "message": {"content": [{"type": "text", "text": "好"}]}},
        "not json",
    ]
    transcript.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines), encoding="utf-8")
    code, _ = _run("session-end", {"session_id": "s", "transcript_path": str(transcript)}, server)
    assert code == 0
    assert _wait_calls()[0][2]["messages"] == [
        {"key": "u:p1", "role": "user", "text": "今天想吃火锅", "at": "2026-10-14T05:00:00.000Z"}]


@pytest.mark.parametrize("payload", [
    {"session_id": "s", "prompt": "还记得吗", "source": "loop_wakeup"},
    {"session_id": "s", "prompt": "还记得吗", "agent_id": "sub-1"},
])
def test_non_human_prompts_are_ignored(server, payload):
    code, out = _run("prompt", payload, server)
    assert code == 0 and out == "" and _Recorder.calls == []


def test_missing_prompt_id_gets_random_key_not_content_hash(server):
    for _ in range(2):
        _run("prompt", {"session_id": "s", "prompt": "继续"}, server)
    keys = [c[2]["prompt_id"] for c in _Recorder.calls]
    assert len(set(keys)) == 2 and all(k.startswith("rand-") for k in keys)


@pytest.mark.parametrize("mode,payload", [
    ("prompt", {"session_id": "s", "prompt": "hi"}),
    ("unknown", {"session_id": "s"}),
])
def test_silent_without_token_or_unknown_mode(server, mode, payload):
    code, out = _run(mode, payload, server, token="" if mode == "prompt" else "tok")
    assert code == 0 and out == "" and _Recorder.calls == []


def test_unreachable_server_is_silent():
    code, out = _run("prompt", {"session_id": "s", "prompt": "还记得吗"}, "http://127.0.0.1:9")
    assert code == 0 and out == ""


def test_garbage_stdin_is_silent(server):
    proc = subprocess.run([sys.executable, str(SCRIPT), "prompt"], input="{not json", capture_output=True,
                          text=True, env={"OB_HOOK_TOKEN": "t", "OB_URL": server, "OB_RELAY": "0",
                                          "OB_RELAY_STATE": str(_STATE_DIR / "relay.json")}, timeout=30)
    assert proc.returncode == 0 and proc.stdout == ""


# ---------------- 本地中继 ----------------

def test_stop_returns_before_upstream_finishes(server):
    """Stop 不等上游：上游慢 2 秒，钩子自己也应该马上退出。"""
    _Recorder.reply = {}
    slow = _Recorder.do_POST

    def delayed(self):
        time.sleep(2)
        slow(self)

    _Recorder.do_POST = delayed
    try:
        t0 = time.time()
        code, _ = _run("stop", {"session_id": "s", "prompt_id": "p", "last_assistant_message": "好"}, server)
        assert code == 0 and time.time() - t0 < 1.5
        assert _wait_calls(timeout=10)
    finally:
        _Recorder.do_POST = slow


def test_relay_spawns_then_serves_both_modes(server, tmp_path):
    state = tmp_path / "relay.json"
    env = {"OB_RELAY": "1", "OB_RELAY_STATE": str(state)}
    _Recorder.reply = {"context": "记忆"}
    # 第一次：中继不在，直连 + 拉起中继
    code, out = _run("prompt", {"session_id": "s", "prompt": "还记得吗", "prompt_id": "p1"}, server, extra_env=env)
    assert code == 0 and "记忆" in out
    end = time.time() + 10
    while not state.exists() and time.time() < end:
        time.sleep(0.05)
    relay = json.loads(state.read_text())
    assert oct(state.stat().st_mode & 0o777) == "0o600"
    try:
        # 第二次：经中继，结果一样
        code, out = _run("prompt", {"session_id": "s", "prompt": "还记得吗", "prompt_id": "p2"}, server, extra_env=env)
        assert code == 0 and "记忆" in out
        assert [c[2]["prompt_id"] for c in _wait_calls(2)] == ["p1", "p2"]
        # 不用等的：交给中继就回
        code, _ = _run("stop", {"session_id": "s", "prompt_id": "p2", "last_assistant_message": "好"}, server,
                       extra_env=env)
        assert code == 0 and _wait_calls(3)[2][0] == "/autorecall/stop"
        # 中继只认带密钥的本机请求
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{relay['port']}/prompt", data=b"{}", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("relay accepted a request without secret")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
    finally:
        try:
            os.kill(relay["pid"], 15)
        except OSError:
            pass


# ---------------- 报错不再悄悄：本地日志 + 下次补报 ----------------

def test_errors_are_logged_then_reported_and_cleared(server, tmp_path):
    env = {"OB_RELAY_STATE": str(tmp_path / "relay.json")}
    code, _ = _run("prompt", {"session_id": "s", "prompt": "还记得吗"}, "http://127.0.0.1:9", extra_env=env)
    assert code == 0
    log = (tmp_path / "ob_hook.log").read_text(encoding="utf-8")
    assert "[prompt]" in log and "连不上 OB" in log
    assert json.loads((tmp_path / ".ob_hook_errors.json").read_text(encoding="utf-8"))
    # 下一次连上：带着之前的错误一起报，报完清空
    _run("prompt", {"session_id": "s", "prompt": "你好"}, server, extra_env=env)
    [call] = _wait_calls()
    assert call[2]["client_errors"][0]["where"] == "prompt"
    assert not (tmp_path / ".ob_hook_errors.json").exists()


def test_401_is_reported_as_token_problem(tmp_path):
    class Deny(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Deny)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        _run("prompt", {"session_id": "s", "prompt": "x"}, f"http://127.0.0.1:{httpd.server_address[1]}",
             extra_env={"OB_RELAY_STATE": str(tmp_path / "relay.json")})
        assert "token" in (tmp_path / "ob_hook.log").read_text(encoding="utf-8")
    finally:
        httpd.shutdown()


def test_relay_upstream_error_does_not_spawn_second_relay(tmp_path):
    """中继活着、只是连不上 OB（502）：直连一次即可，不能再拉一个中继（会留下孤儿进程）。"""
    class Relay502(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(502)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Relay502)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    import hashlib
    url, token = "http://127.0.0.1:9", "tok"
    state = tmp_path / "relay.json"
    state.write_text(json.dumps({"port": httpd.server_address[1], "secret": "s", "pid": 1,
                                 "fp": hashlib.sha256(f"{url}\x1f{token}".encode()).hexdigest()[:16]}))
    try:
        code, _ = _run("prompt", {"session_id": "s", "prompt": "x"}, url,
                       extra_env={"OB_RELAY": "1", "OB_RELAY_STATE": str(state)})
        assert code == 0
        assert not (tmp_path / "relay.spawn").exists()
        assert json.loads(state.read_text())["port"] == httpd.server_address[1]
    finally:
        httpd.shutdown()


def test_v2_output_receipts_survive_process_restart_and_reset_on_compact(server, tmp_path):
    env = {"OB_RELAY_STATE": str(tmp_path / "relay.json")}
    _Recorder.reply = {"protocol": 2, "context": "记忆内容", "receipt_id": "a" * 64}
    payload = {"session_id": "s", "prompt_id": "p1", "prompt": "记得吗"}
    assert "记忆内容" in _run("prompt", payload, server, extra_env=env)[1]
    _run("prompt", {**payload, "prompt_id": "p2"}, server, extra_env=env)
    assert _Recorder.calls[1][2]["injection"]["delivered"] == ["a" * 64]
    old_epoch = _Recorder.calls[1][2]["injection"]["epoch"]
    _Recorder.reply = {"protocol": 2, "context": ""}
    _run("session-start", {"session_id": "s", "source": "compact"}, server, extra_env=env)
    spec = _Recorder.calls[-1][2]["injection"]
    assert spec["epoch"] != old_epoch and spec["delivered"] == []
    _run("prompt", {**payload, "prompt_id": "p3"}, server, extra_env=env)
    assert _Recorder.calls[-1][2]["injection"]["delivered"] == []
    files = list((tmp_path / "injections").glob("*.json"))
    assert len(files) == 1 and "记忆内容" not in files[0].read_text()
    assert files[0].stat().st_mode & 0o777 == 0o600


def test_v2_concurrent_process_receipts_are_merged(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"epoch": "e", "delivered": []}))
    worker = '''
import importlib.util, json, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("hook", sys.argv[1])
hook = importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)
path, name = Path(sys.argv[2]), sys.argv[3]
original = hook._write_delivery
def slow_write(*args):
    time.sleep(0.2)
    original(*args)
hook._write_delivery = slow_write
(path.parent / name).touch()
deadline = time.monotonic() + 5
while not (path.parent / "go").exists():
    assert time.monotonic() < deadline
    time.sleep(0.01)
hook._delivery_output((path, {"epoch": "e"}), {"protocol": 2, "receipt_id": name * 64})
'''
    env = {**os.environ, "OB_RELAY_STATE": str(tmp_path / "relay.json")}
    processes = [subprocess.Popen([sys.executable, "-c", worker, str(SCRIPT), str(path), name], env=env)
                 for name in ("a", "b")]
    try:
        deadline = time.monotonic() + 5
        while not all((tmp_path / name).exists() for name in ("a", "b")):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        (tmp_path / "go").touch()
        for process in processes:
            assert process.wait(timeout=5) == 0
        assert set(json.loads(path.read_text())["delivered"]) == {"a" * 64, "b" * 64}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_v2_compact_discards_late_receipt_and_request_keeps_current_receipts(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("hook", SCRIPT)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.setenv("OB_RELAY_STATE", str(tmp_path / "relay.json"))
    payload = {"session_id": "s", "client": "claude", "cwd": "/work"}
    old = hook._delivery_request(dict(payload), "prompt")
    current = hook._delivery_request({**payload, "source": "compact"}, "session-start")
    assert old[1]["epoch"] != current[1]["epoch"]
    hook._delivery_output(current, {"protocol": 2, "receipt_id": "b" * 64})
    hook._delivery_output(old, {"protocol": 2, "receipt_id": "a" * 64})
    read = hook._delivery_request(dict(payload), "prompt")
    assert read[1]["delivered"] == ["b" * 64]
    # 普通 prompt 只读回执，不再把之前读取的整份状态重写回去。
    monkeypatch.setattr(hook, "_write_delivery", lambda *args: pytest.fail("普通请求不应重写回执"))
    assert hook._delivery_request(dict(payload), "prompt")[1] == read[1]


def test_v2_receipt_lock_is_released_when_process_exits(tmp_path):
    worker = '''
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("hook", sys.argv[1])
hook = importlib.util.module_from_spec(spec); spec.loader.exec_module(hook)
with hook._delivery_lock(Path(sys.argv[2])):
    (Path(sys.argv[2]) / "ready").touch()
    time.sleep(30)
'''
    process = subprocess.Popen([sys.executable, "-c", worker, str(SCRIPT), str(tmp_path)])
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.kill()
        process.wait(timeout=5)
        import importlib.util
        spec = importlib.util.spec_from_file_location("hook", SCRIPT)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with hook._delivery_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
