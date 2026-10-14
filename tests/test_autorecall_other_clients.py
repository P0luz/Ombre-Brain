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
    assert stop["messages"][0]["text"] == "记得，是阿树" and stop["cwd"] == "/w" and stop["client"] == "gemini"


def test_codex_turn_id_and_no_transcript_backfill(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "codex", ob.url)
    out = _run(hooks_dir, tmp_path, ["--client", "codex", "session-start"], {
        "session_id": "c1", "cwd": "/w", "hook_event_name": "SessionStart", "source": "startup", "model": "m"})
    assert json.loads(out.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    _run(hooks_dir, tmp_path, ["--client", "codex", "prompt"], {
        "session_id": "c1", "cwd": "/w", "turn_id": "t7", "prompt": "嗨", "hook_event_name": "UserPromptSubmit"})
    prompt = [c for c in ob.calls if c[0] == "prompt"][0][1]
    assert prompt["prompt_id"] == "t7" and prompt["client"] == "codex"
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


def test_antigravity_named_hooks_install_and_uninstall(tmp_path, ob):
    path = tmp_path / "antigravity-settings.json"
    path.write_text(json.dumps({"lint": {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "x"}]}]}}))
    settings, hooks_dir = _install(tmp_path, "antigravity", ob.url)
    ours = settings["ombre-brain"]
    assert set(ours) == {"SessionStart", "PreInvocation", "Stop"} and "lint" in settings
    assert "--client antigravity session-start" in ours["SessionStart"][0]["command"]
    assert ours["SessionStart"][0]["timeout"] == 45  # 秒
    _install(tmp_path, "antigravity", ob.url)  # 重复安装只留一份
    assert list(json.loads(path.read_text())).count("ombre-brain") == 1
    assert install.main(["--client", "antigravity", "--uninstall", "--settings", str(path),
                         "--hooks-dir", str(hooks_dir)]) == 0
    assert json.loads(path.read_text()) == {"lint": {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "x"}]}]}}


def test_antigravity_session_start_injects_steps(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "antigravity", ob.url)
    out = _run(hooks_dir, tmp_path, ["--client", "antigravity", "session-start"], {
        "conversationId": "ag1", "workspacePaths": ["/w"], "transcriptPath": "", "modelName": "auto"})
    assert json.loads(out.stdout) == {"injectSteps": [{"systemMessage": {"systemMessage": "[接续] 上次聊到猫"}}]}
    start = [c[1] for c in ob.calls if c[0] == "session-start"][-1]
    assert start["session_id"] == "ag1" and start["cwd"] == "/w" and start["client"] == "antigravity"


def test_antigravity_probe_records_shape_not_content(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "antigravity", ob.url)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "USER_INPUT", "content": {"text": "我的秘密是阿树"}}, ensure_ascii=False)
                          + "\n" + json.dumps({"type": "PLANNER_RESPONSE", "text": "好的"}, ensure_ascii=False) + "\n")
    n = len(ob.calls)
    out = _run(hooks_dir, tmp_path, ["--client", "antigravity", "probe", "Stop"], {
        "conversationId": "ag2", "transcriptPath": str(transcript), "terminationReason": "NO_TOOL_CALL"})
    assert json.loads(out.stdout) == {} and len(ob.calls) == n  # 探针不连 OB
    probe = (hooks_dir / "ob_agy_probe.json").read_text(encoding="utf-8")
    assert "阿树" not in probe and "好的" not in probe and "ag2" not in probe
    entry = json.loads(probe)[-1]
    assert entry["event"] == "Stop" and entry["transcript"]["lines"] == 2
    assert entry["transcript"]["paths"]["content.text"] == ["str"]
    assert entry["transcript"]["enum_values"]["type"] == ["PLANNER_RESPONSE", "USER_INPUT"]


def _agy_transcript(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def test_antigravity_prompt_and_stop_read_transcript(tmp_path, ob):
    """记录文件格式照 lidge-ai/cli-jaw 的 agy-transcript 测试用例：step_index / type / status / content / tool_calls。"""
    _settings, hooks_dir = _install(tmp_path, "antigravity", ob.url)
    transcript = tmp_path / "transcript.jsonl"
    user = {"step_index": 0, "type": "USER_INPUT", "status": "DONE",
            "content": "<USER_REQUEST>\n你还记得阿树吗\n</USER_REQUEST>"}
    _agy_transcript(transcript, [user])
    base = {"conversationId": "ag3", "workspacePaths": ["/w"], "transcriptPath": str(transcript)}
    out = _run(hooks_dir, tmp_path, ["--client", "antigravity", "prompt"], {**base, "invocationNum": 1})
    assert json.loads(out.stdout) == {"injectSteps": [{"systemMessage": {"systemMessage": "[记忆] 猫叫阿树"}}]}
    prompt = [c[1] for c in ob.calls if c[0] == "prompt"][-1]
    assert prompt["prompt"] == "你还记得阿树吗" and prompt["prompt_id"] == "agy:ag3:0"

    # 工具调用之后的再次调用：这一轮模型已经回过话，不再召回
    tool_turn = [user,
                 {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "我先看看", "tool_calls": [{"name": "list_dir"}]},
                 {"step_index": 2, "type": "LIST_DIRECTORY", "status": "DONE", "content": "a.txt"}]
    _agy_transcript(transcript, tool_turn)
    n = len(ob.calls)
    out = _run(hooks_dir, tmp_path, ["--client", "antigravity", "prompt"], {**base, "invocationNum": 2})
    assert json.loads(out.stdout) == {} and len(ob.calls) == n

    _agy_transcript(transcript, tool_turn + [
        {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "my_tool_call_analysis: 看完了", "tool_calls": []},
        {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "记得，是橘猫阿树", "tool_calls": [],
         "thinking": "不该被存"}])
    _run(hooks_dir, tmp_path, ["--client", "antigravity", "stop"], {**base, "terminationReason": "NO_TOOL_CALL"})
    assert _wait(lambda: "stop" in ob.names(), 10)
    stop = [c[1] for c in ob.calls if c[0] == "stop"][-1]
    assert stop["messages"] == [
        {"key": "u:agy:ag3:0", "role": "user", "text": "你还记得阿树吗"},
        {"key": "a:agy:ag3:4", "role": "assistant", "text": "记得，是橘猫阿树"}]
    assert stop["client"] == "antigravity" and stop["cwd"] == "/w"


def test_antigravity_stop_skips_cancelled_and_unreadable(tmp_path, ob):
    _settings, hooks_dir = _install(tmp_path, "antigravity", ob.url)
    transcript = tmp_path / "t.jsonl"
    _agy_transcript(transcript, [{"step_index": 0, "type": "USER_INPUT", "content": "嗨"},
                                 {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "你好", "tool_calls": []}])
    n = len(ob.calls)
    for payload in ({"terminationReason": "USER_CANCELED", "transcriptPath": str(transcript)},
                    {"terminationReason": "NO_TOOL_CALL", "transcriptPath": str(tmp_path / "missing.jsonl")}):
        out = _run(hooks_dir, tmp_path, ["--client", "antigravity", "stop"], {"conversationId": "ag4", **payload})
        assert out.returncode == 0 and json.loads(out.stdout) == {}
    import time as _time
    _time.sleep(0.5)
    assert len(ob.calls) == n


def test_codex_hooks_feature_flag_detection(tmp_path):
    cfg = tmp_path / "config.toml"
    assert install.codex_hooks_disabled(cfg) is False  # 没有文件 = 默认开
    cfg.write_text('model = "x"\n[features]\ngoals = true\nhooks = true\n')
    assert install.codex_hooks_disabled(cfg) is False
    cfg.write_text('[features]\nhooks = false  # 关了\n[other]\nhooks = true\n')
    assert install.codex_hooks_disabled(cfg) is True
    cfg.write_text('[features]\ncodex_hooks = false\n')
    assert install.codex_hooks_disabled(cfg) is True
    cfg.write_text('[profiles.a]\nhooks = false\n')
    assert install.codex_hooks_disabled(cfg) is False


def test_codex_internal_helper_prompts_are_not_stored(tmp_path, ob):
    """开头文字照 claude-mem 的 CODEX_INTERNAL_PROMPT_OPENINGS；同样的话从 Claude Code 来就是用户本人说的。"""
    _settings, hooks_dir = _install(tmp_path, "codex", ob.url)
    helpers = [
        "You are a helpful assistant. You will be presented with a user prompt, and your job is to provide a short title for it.",
        "## Memory Writing Agent: Phase 2 (Consolidation)\n...",
        "# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for the user.",
    ]
    for text in helpers:
        _run(hooks_dir, tmp_path, ["--client", "codex", "prompt"], {"session_id": "c9", "turn_id": "t", "prompt": text})
    assert not [c for c in ob.calls if c[0] == "prompt"]
    _settings, claude_dir = _install(tmp_path, "claude", ob.url)
    install.write_env_file(claude_dir / "ob_hook.py", ob.url, "tok")  # Claude Code 平时从 settings.json 的 env 拿
    _run(claude_dir, tmp_path, ["prompt"], {"session_id": "c9", "prompt": helpers[0]})
    assert [c for c in ob.calls if c[0] == "prompt"]
