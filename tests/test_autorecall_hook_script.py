"""deploy/claude-code-hooks/ob_hook.py：四种模式的请求与输出，以及出错时静默退出 0。"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "claude-code-hooks" / "ob_hook.py"


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


def _run(mode, payload, url, token="tok"):
    env = {"OB_URL": url, "PATH": "/usr/bin:/bin"}
    if token:
        env["OB_HOOK_TOKEN"] = token
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), mode], input=json.dumps(payload), capture_output=True,
        text=True, env=env, timeout=30,
    )
    return proc.returncode, proc.stdout


def test_prompt_posts_and_emits_additional_context(server):
    _Recorder.reply = {"context": "记忆：周五吃了火锅"}
    code, out = _run("prompt", {"session_id": "s", "prompt": "还记得火锅吗", "prompt_id": "p1"}, server)
    assert code == 0
    path, token, body = _Recorder.calls[0]
    assert path == "/autorecall/prompt" and token == "tok"
    assert body == {"session_id": "s", "prompt": "还记得火锅吗", "prompt_id": "p1"}
    assert json.loads(out) == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": "记忆：周五吃了火锅"}}


def test_session_start_and_empty_context_prints_nothing(server):
    _Recorder.reply = {"context": ""}
    code, out = _run("session-start", {"session_id": "s", "source": "compact"}, server)
    assert code == 0 and out == ""
    assert _Recorder.calls[0][2] == {"session_id": "s", "source": "compact"}


def test_stop_sends_last_assistant_message(server):
    code, out = _run("stop", {"session_id": "s", "prompt_id": "p1", "last_assistant_message": "好呀"}, server)
    assert code == 0 and out == ""
    assert _Recorder.calls[0][2]["messages"] == [{"key": "a:p1", "role": "assistant", "text": "好呀"}]


def test_session_end_backfills_user_text_only(server, tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "promptId": "p1", "uuid": "x1", "message": {"role": "user", "content": "今天想吃火锅"}},
        {"type": "user", "uuid": "x2", "toolUseResult": {}, "message": {"content": [{"type": "tool_result"}]}},
        {"type": "user", "uuid": "x3", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "assistant", "uuid": "x4", "message": {"content": [{"type": "text", "text": "好"}]}},
        "not json",
    ]
    transcript.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines), encoding="utf-8")
    code, _ = _run("session-end", {"session_id": "s", "transcript_path": str(transcript)}, server)
    assert code == 0
    assert _Recorder.calls[0][2]["messages"] == [{"key": "u:p1", "role": "user", "text": "今天想吃火锅"}]


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
    proc = subprocess.run([sys.executable, str(SCRIPT), "prompt"], input="{not json",
                          capture_output=True, text=True, env={"OB_HOOK_TOKEN": "t", "OB_URL": server}, timeout=30)
    assert proc.returncode == 0 and proc.stdout == ""
