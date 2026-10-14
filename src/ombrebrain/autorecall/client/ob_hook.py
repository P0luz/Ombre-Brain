#!/usr/bin/env python3
"""Ombre Brain × Claude Code 自动召回钩子（只用标准库）。

一般不用手动装：运行同目录的 install.py（Dashboard「自动召回」卡片里有一键命令）。

用法（install.py 会替你挂好；手动挂法见 deploy/claude-code-hooks/settings.example.json）：

    python3 ob_hook.py session-start   # SessionStart：开窗 / 压缩后接续 + 今日 dream
    python3 ob_hook.py prompt          # UserPromptSubmit：存用户原文 + hold 提醒 + 唤醒召回
    python3 ob_hook.py stop            # Stop：存这一轮的助手回复（同步挂；后台钩子退出时会被杀）
    python3 ob_hook.py session-end     # SessionEnd：尽力补存用户原文
    python3 ob_hook.py pre-compact     # PreCompact：压缩前同样补存一遍，压缩后的开窗接续拿得到完整原文

其他客户端（字段对照见 deploy/clients/README.md，install.py --client 会替你挂好）：

    python3 ob_hook.py --client codex prompt    # Codex CLI：事件名同 Claude Code，提问的 id 叫 turn_id
    python3 ob_hook.py --client gemini prompt   # 旧版 Gemini CLI：BeforeAgent 提问，AfterAgent 的 prompt_response 是回复

环境变量（也可以写进同目录的 ob_hook.env，一行一个 KEY=VALUE；环境里已有的优先）：
    OB_URL         Ombre Brain 地址，默认 http://127.0.0.1:18001
    OB_HOOK_TOKEN  与服务端 OMBRE_HOOK_TOKEN / hooks.token 相同；没设就什么都不做
    OB_RELAY       设为 0 时不用本地中继，每次直连
    OB_HOOK_DEBUG  设为 1 时把错误打到 stderr

出错不打扰对话，但不再「悄悄」：错误写进 ~/.claude/hooks/ob_hook.log（超过 200KB 轮转一份），
同时暂存，下一次能连上 OB 时顺带报上去，Dashboard「自动召回」卡片会标红显示。

提速：优先走同目录 ob_relay.py 拉起的本地中继（常驻、与 OB 保持长连接）。
要等结果的（开窗、发问）经中继转发；不用等的（存回复、关窗补存）交给中继就返回。
中继不在时：这次直连（不用等的就丢给一个脱离的子进程去发），同时把中继拉起来给下一次用。

任何错误都静默退出 0：记忆出问题不能卡住对话。
不解析对话记录文件取当轮内容——官方说那是内部格式、随版本会变；
用户原文取 UserPromptSubmit 的 prompt，助手回复取 Stop 的 last_assistant_message。
只有 SessionEnd 的补存会读记录文件，读不懂就跳过。
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
import urllib.error
import urllib.request

_TIMEOUTS = {"session-start": 40, "prompt": 12, "stop": 25, "session-end": 8, "pre-compact": 8}
_ENDPOINT = {"pre-compact": "session-end"}  # 压缩前抢救和关窗补存是同一件事
_EVENT_NAMES = {"session-start": "SessionStart", "prompt": "UserPromptSubmit"}
# 各客户端注入上下文时回填的事件名；没列出的同 Claude Code。
_CLIENT_EVENT_NAMES = {"gemini": {"session-start": "SessionStart", "prompt": "BeforeAgent"}}
_CLIENTS = frozenset({"claude", "codex", "gemini"})
_MAX_BACKFILL = 1000
_NO_WAIT = frozenset({"stop", "session-end", "pre-compact"})
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


_LOG_LIMIT = 200 * 1024
_PENDING_LIMIT = 20


def _load_env_file() -> None:
    """Codex / Gemini 的钩子配置里没有 env 段，install.py 把地址和 token 写进同目录的 ob_hook.env（0600）。"""
    try:
        lines = (_HERE / "ob_hook.env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip())


def _debug(message: str) -> None:
    if os.environ.get("OB_HOOK_DEBUG") == "1":
        print(f"[ob_hook] {message}", file=sys.stderr)


def _state_dir() -> Path:
    custom = os.environ.get("OB_RELAY_STATE")
    return Path(custom).parent if custom else Path.home() / ".claude" / "hooks"


def report_error(where: str, message: str) -> None:
    """记本地日志 + 暂存待补报。任何一步失败都忽略：报错本身不能再出错。"""
    _debug(f"{where}: {message}")
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / "ob_hook.log"
        if log.exists() and log.stat().st_size > _LOG_LIMIT:
            os.replace(log, directory / "ob_hook.log.1")
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{now} [{where}] {message}\n")
        pending_path = directory / ".ob_hook_errors.json"
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pending = []
        pending = (pending + [{"at": now, "where": where, "message": message[:300]}])[-_PENDING_LIMIT:]
        pending_path.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _pending_errors() -> list:
    try:
        data = json.loads((_state_dir() / ".ob_hook_errors.json").read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _clear_pending() -> None:
    try:
        (_state_dir() / ".ob_hook_errors.json").unlink()
    except OSError:
        pass


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(request, timeout: float):
    """本机地址（OB 在本机 / 本地中继）绕过系统代理——macOS 上 urllib 会读系统代理，
    实测连发往 127.0.0.1 的请求都被代理接走（返回 503）。远端地址照常走系统代理。"""
    host = urllib.request.urlparse(request.full_url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return _DIRECT.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _post(path: str, payload: dict, timeout: float) -> dict:
    base = os.environ.get("OB_URL", "http://127.0.0.1:18001").rstrip("/")
    request = urllib.request.Request(
        f"{base}/autorecall/{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Ombre-Hook-Token": os.environ.get("OB_HOOK_TOKEN", ""),
        },
        method="POST",
    )
    with _open(request, timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _relay_state():
    if os.environ.get("OB_RELAY") == "0":
        return None
    path = Path(os.environ.get("OB_RELAY_STATE") or Path.home() / ".claude" / "hooks" / ".ob_relay.json")
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    url = os.environ.get("OB_URL", "http://127.0.0.1:18001").rstrip("/")
    token = os.environ.get("OB_HOOK_TOKEN", "")
    expected = hashlib.sha256(f"{url}\x1f{token}".encode()).hexdigest()[:16]
    return state if state.get("fp") == expected else None


def _detach(args: list, stdin_bytes: bytes = b"") -> None:
    """起一个脱离的子进程：Claude Code 退出也不会把它一起杀掉。"""
    kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, *args], **kwargs)
    if stdin_bytes:
        proc.stdin.write(stdin_bytes)
    proc.stdin.close()


def _spawn_relay() -> None:
    relay = _HERE / "ob_relay.py"
    if os.environ.get("OB_RELAY") == "0" or not relay.is_file():
        return
    # 几个钩子同时发现中继不在时只拉一个：10 秒内的启动锁。
    lock = Path(os.environ.get("OB_RELAY_STATE") or Path.home() / ".claude" / "hooks" / ".ob_relay.json")
    lock = lock.with_suffix(".spawn")
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < 10:
            return
        lock.write_text(str(os.getpid()))
        _detach([str(relay)])
    except OSError:
        pass


def _via_relay(state: dict, path: str, payload: dict, wait: bool, timeout: float):
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(state['port'])}/{path}" + ("" if wait else "?nowait=1"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Relay-Secret": str(state.get("secret") or "")},
        method="POST",
    )
    with _open(request, timeout) as response:
        if response.status == 202:
            return {}
        return json.loads(response.read().decode("utf-8") or "{}")


def _send(path: str, payload: dict, timeout: float) -> dict:
    wait = path not in _NO_WAIT
    state = _relay_state()
    relay_alive = False
    if state:
        try:
            return _via_relay(state, path, payload, wait, timeout if wait else 2)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 404, 502, 503):
                raise
            # 502/503 是中继连不上 OB 或队列满——中继本身是好的，这次直连就行，不要再拉一个；
            # 403/404 说明状态文件指向的不是这一个中继了，才需要重拉。
            relay_alive = exc.code in (502, 503)
        except (urllib.error.URLError, OSError):
            pass  # 中继不在了：往下直连，并重新拉起
    if not relay_alive:
        _spawn_relay()
    if not wait:
        _detach([str(Path(__file__).resolve()), "--deliver", path],
                json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return {}
    return _post(path, payload, timeout)


def _deliver(path: str) -> int:
    """--deliver：脱离的子进程从 stdin 读 payload 直连发出。"""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        _post(path, payload, _TIMEOUTS.get(path, 20))
    except Exception as exc:
        report_error(path, f"后台发送失败：{type(exc).__name__}: {exc}")
    return 0


def _backfill_user_messages(transcript_path: str) -> list:
    """从记录文件里尽力取用户原文；格式对不上就返回空，不报错。"""
    messages = []
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta") or entry.get("toolUseResult"):
                    continue
                # 压缩摘要、只给界面看的条目都不是用户原话；有 origin 的只收真人输入。
                if entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
                    continue
                origin = entry.get("origin")
                if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                    continue
                content = (entry.get("message") or {}).get("content")
                if not isinstance(content, str) or content.lstrip().startswith("<"):
                    continue  # 工具结果、斜杠命令输出等都不是原话
                key = entry.get("promptId") or entry.get("uuid") or ""
                messages.append({
                    "key": f"u:{key}" if key else "", "role": "user", "text": content,
                    "at": entry.get("timestamp") or "",
                })
    except OSError:
        return []
    return messages[-_MAX_BACKFILL:]


def main() -> int:
    _load_env_file()
    if len(sys.argv) > 2 and sys.argv[1] == "--deliver":
        return _deliver(sys.argv[2]) if os.environ.get("OB_HOOK_TOKEN") else 0
    args = sys.argv[1:]
    client = "claude"
    if len(args) >= 2 and args[0] == "--client":
        client, args = args[1], args[2:]
    if client not in _CLIENTS:
        return 0
    mode = args[0] if args else ""
    if mode not in _TIMEOUTS or not os.environ.get("OB_HOOK_TOKEN"):
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return 0
    # Codex 管一轮叫 turn_id；Gemini 没有每轮 id。
    prompt_id = str(data.get("prompt_id") or data.get("turn_id") or "")
    cwd = str(data.get("cwd") or "")
    # 子代理里的事件、定时唤醒 / SDK 发起的「提问」都不是用户本人说的话，不存也不召回。
    if data.get("agent_id") and mode in ("prompt", "stop"):
        return 0

    if mode == "session-start":
        payload = {"session_id": session_id, "source": data.get("source") or "startup", "cwd": cwd}
    elif mode == "prompt":
        if data.get("source") not in (None, "user"):
            return 0
        # 没有 prompt_id 时给一个随机键：宁可偶尔多存一份，也不能把重复说的「继续」吞掉。
        payload = {"session_id": session_id, "prompt": data.get("prompt") or "",
                   "prompt_id": prompt_id or f"rand-{uuid.uuid4().hex}", "cwd": cwd}
    elif mode == "stop":
        text = data.get("last_assistant_message") or data.get("prompt_response") or ""
        if not text.strip():
            return 0
        # 同一个 prompt 可能停不止一次（Stop 钩子让它续写），带上内容摘要区分。
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        key = f"a:{prompt_id}:{digest}" if prompt_id else f"a:rand-{uuid.uuid4().hex}"
        payload = {"session_id": session_id, "cwd": cwd, "messages": [
            {"key": key, "role": "assistant", "text": text},
        ]}
    else:
        if client != "claude":
            return 0  # 补存要读记录文件，只认得 Claude Code 的格式
        messages = _backfill_user_messages(str(data.get("transcript_path") or ""))
        if not messages:
            return 0
        payload = {"session_id": session_id, "messages": messages, "cwd": cwd, "event": mode}

    payload["client"] = client  # 面板上各家单独的开关和到达时间靠这个区分
    pending = _pending_errors() if mode not in _NO_WAIT else []
    if pending:
        payload["client_errors"] = pending
    try:
        result = _send(_ENDPOINT.get(mode, mode), payload, _TIMEOUTS[mode])
    except urllib.error.HTTPError as exc:
        report_error(mode, f"OB 返回 {exc.code}" + ("（token 不对？）" if exc.code == 401 else ""))
        return 0
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report_error(mode, f"连不上 OB：{type(exc).__name__}: {exc}")
        return 0
    if pending:
        _clear_pending()

    context = result.get("context") if isinstance(result, dict) else ""
    names = _CLIENT_EVENT_NAMES.get(client, _EVENT_NAMES)
    if context and mode in names:
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": names[mode], "additionalContext": context},
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 兜底：钩子绝不能让对话报错
        report_error("hook", f"意外错误：{exc!r}")
        sys.exit(0)
