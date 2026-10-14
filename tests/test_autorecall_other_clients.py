"""Codex CLI / Gemini CLI：安装进临时目录，按官方文档的输入字段真跑 ob_hook.py。"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_autorecall_gateway import FakeOB, _wait  # noqa: E402

_CLIENT = Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client"
sys.path.insert(0, str(_CLIENT))
import install  # noqa: E402


@pytest.fixture
def ob():
    server = FakeOB()
    yield server
    server.server.shutdown()


def _install(tmp_path, client, url):
    settings = tmp_path / f"{client}-settings.json"
    hooks_dir = tmp_path / f"{client}-hooks"
    rc = install.main(["--client", client, "--url", url, "--token", "tok",
                       "--settings", str(settings), "--hooks-dir", str(hooks_dir)])
    assert rc == 0
    return json.loads(settings.read_text()), hooks_dir


def _run(hooks_dir, tmp_path, args, payload):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "OB_RELAY": "0"}
    return subprocess.run([sys.executable, str(hooks_dir / "ob_hook.py"), *args], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=30)


def test_codex_install_layout(tmp_path, ob):
    settings, hooks_dir = _install(tmp_path, "codex", ob.url)
    assert set(settings["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}
    command = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "--client codex prompt" in command and "env" not in settings
    env_file = hooks_dir / "ob_hook.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "OB_HOOK_TOKEN=tok" in env_file.read_text()
    assert f"OB_RELAY_STATE={hooks_dir / '.ob_relay.json'}" in env_file.read_text()


def test_gemini_install_uses_milliseconds_and_keeps_other_settings(tmp_path, ob):
    path = tmp_path / "gemini-settings.json"
    path.write_text(json.dumps({"theme": "dark", "hooks": {"BeforeTool": [{"hooks": [{"command": "x"}]}]}}))
    settings, _hooks_dir = _install(tmp_path, "gemini", ob.url)
    assert settings["theme"] == "dark" and "BeforeTool" in settings["hooks"]
    assert settings["hooks"]["AfterAgent"][0]["hooks"][0]["timeout"] == 20000
    assert list(tmp_path.glob("gemini-settings.json.bak-*"))
    assert install.main(["--client", "gemini", "--uninstall", "--settings", str(path),
                         "--hooks-dir", str(tmp_path / "gemini-hooks")]) == 0
    left = json.loads(path.read_text())
    assert left == {"theme": "dark", "hooks": {"BeforeTool": [{"hooks": [{"command": "x"}]}]}}
    assert not (tmp_path / "gemini-hooks" / "ob_hook.env").exists()


def test_gemini_before_and_after_agent(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "gemini", ob.url)
    out = _run(hooks_dir, tmp_path, ["--client", "gemini", "prompt"], {
        "session_id": "g1", "cwd": "/w", "hook_event_name": "BeforeAgent", "prompt": "你还记得吗",
        "transcript_path": "", "timestamp": "2026-10-14T00:00:00Z"})
    assert json.loads(out.stdout) == {"hookSpecificOutput": {"hookEventName": "BeforeAgent",
                                                            "additionalContext": "[记忆] 猫叫阿树"}}
    _run(hooks_dir, tmp_path, ["--client", "gemini", "stop"], {
        "session_id": "g1", "cwd": "/w", "hook_event_name": "AfterAgent", "prompt": "你还记得吗",
        "prompt_response": "记得，是阿树", "stop_hook_active": False})
    assert _wait(lambda: "stop" in ob.names(), 10)
    stop = [c[1] for c in ob.calls if c[0] == "stop"][0]
    assert stop["messages"][0]["text"] == "记得，是阿树" and stop["cwd"] == "/w"


def test_codex_turn_id_and_no_transcript_backfill(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "codex", ob.url)
    out = _run(hooks_dir, tmp_path, ["--client", "codex", "session-start"], {
        "session_id": "c1", "cwd": "/w", "hook_event_name": "SessionStart", "source": "startup", "model": "m"})
    assert json.loads(out.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    _run(hooks_dir, tmp_path, ["--client", "codex", "prompt"], {
        "session_id": "c1", "cwd": "/w", "turn_id": "t7", "prompt": "嗨", "hook_event_name": "UserPromptSubmit"})
    assert [c for c in ob.calls if c[0] == "prompt"][0][1]["prompt_id"] == "t7"
    _run(hooks_dir, tmp_path, ["--client", "codex", "stop"], {
        "session_id": "c1", "cwd": "/w", "turn_id": "t7", "last_assistant_message": "你好",
        "stop_hook_active": False})
    assert _wait(lambda: "stop" in ob.names(), 10)
    assert [c for c in ob.calls if c[0] == "stop"][0][1]["messages"][0]["key"].startswith("a:t7:")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    n = len(ob.calls)
    _run(hooks_dir, tmp_path, ["--client", "codex", "session-end"], {
        "session_id": "c1", "transcript_path": str(transcript), "reason": "other"})
    assert len(ob.calls) == n  # 别家的记录文件不解析


def test_unknown_client_is_a_noop(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "codex", ob.url)
    out = _run(hooks_dir, tmp_path, ["--client", "nope", "prompt"], {"session_id": "x", "prompt": "你还记得吗"})
    assert out.returncode == 0 and out.stdout == "" and not ob.calls[1:]
