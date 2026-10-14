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
    python3 ob_hook.py --client antigravity prompt   # Antigravity（agy），实验：PreInvocation / Stop 读它的记录文件取原文
    python3 ob_hook.py --client antigravity probe Stop      # agy 结构探针：只记字段名 / 类型，不记内容（排查用）

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
_CLIENTS = frozenset({"claude", "codex", "gemini", "antigravity"})
_PROBE_ENUM_KEYS = frozenset({"type", "role", "kind", "source", "stepType", "status", "event", "author"})
_MAX_BACKFILL = 1000
_NO_WAIT = frozenset({"stop", "session-end", "pre-compact"})
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


_LOG_LIMIT = 200 * 1024
_PENDING_LIMIT = 20
_SPOOL_LIMIT = 500  # 待补发的请求最多留多少条；再多就丢最旧的


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


# ---- 断线不丢：发不出去的「存回复 / 关窗补存」先落盘，连上后补发 ----
# 照 claude-mem 的做法：先落盘再投递。这两类请求都带去重键，补发几次都不会存重。

def _spool_dir() -> Path:
    return _state_dir() / "spool"


def spool_put(path: str, body: bytes) -> None:
    """把一条发不出去的请求原样写进待发目录（0600）。超过上限丢最旧的。"""
    try:
        directory = _spool_dir()
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:6]}.json"
        temp = directory / f".{name}.tmp"
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"path": path, "body": body.decode("utf-8")}, handle, ensure_ascii=False)
        os.replace(temp, directory / name)
        files = sorted(directory.glob("*.json"))
        for old in files[:max(0, len(files) - _SPOOL_LIMIT)]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def spool_count() -> int:
    try:
        return sum(1 for _ in _spool_dir().glob("*.json"))
    except OSError:
        return 0


def spool_flush(send) -> int:
    """按时间顺序补发。send(path, body) 返回 HTTP 状态码。
    2xx / 4xx（除 429）都算处理完（4xx 重发也不会成）；5xx、429、网络错误就停下，留着下次再发。"""
    sent = 0
    for item in sorted(_spool_dir().glob("*.json")):
        claimed = item.with_name(f"{item.name}.sending-{os.getpid()}")
        try:
            os.replace(item, claimed)  # 先认领，两个补发进程不会抢同一条
        except OSError:
            continue
        try:
            record = json.loads(claimed.read_text(encoding="utf-8"))
            status = send(str(record["path"]), str(record["body"]).encode("utf-8"))
        except (OSError, ValueError, KeyError):
            claimed.unlink(missing_ok=True)  # 坏文件
            continue
        except Exception:
            status = 0
        if status and status < 500 and status != 429:
            claimed.unlink(missing_ok=True)
            sent += 1
            continue
        os.replace(claimed, item)
        break
    return sent


def _post_raw(path: str, body: bytes, timeout: float = 20) -> int:
    base = os.environ.get("OB_URL", "http://127.0.0.1:18001").rstrip("/")
    request = urllib.request.Request(
        f"{base}/autorecall/{path}", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": os.environ.get("OB_HOOK_TOKEN", "")})
    try:
        with _open(request, timeout) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _flush_spool() -> int:
    """--flush：脱离的子进程把待发目录补发掉。60 秒内只跑一个。"""
    lock = _spool_dir() / ".flush.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < 60:
            return 0
        lock.write_text(str(os.getpid()))
        sent = spool_flush(_post_raw)
        if sent:
            _debug(f"补发了 {sent} 条")
    except Exception as exc:
        report_error("flush", f"补发失败：{type(exc).__name__}: {exc}")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return 0


def _after_success() -> None:
    """刚刚确实连上了 OB：待发目录里有东西就交给后台补发，不拖慢这一次。"""
    if spool_count():
        try:
            _detach([str(Path(__file__).resolve()), "--flush"])
        except OSError:
            pass


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
    raw = sys.stdin.read() or "{}"
    try:
        _post(path, json.loads(raw), _TIMEOUTS.get(path, 20))
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 or exc.code == 429:
            spool_put(path, raw.encode("utf-8"))
            report_error(path, f"OB 返回 {exc.code}，已暂存，连上后补发")
        else:
            report_error(path, f"后台发送被拒：OB 返回 {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        spool_put(path, raw.encode("utf-8"))
        report_error(path, f"连不上 OB，已暂存，连上后补发：{type(exc).__name__}: {exc}")
    except Exception as exc:
        report_error(path, f"后台发送失败：{type(exc).__name__}: {exc}")
    else:
        _after_success()
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


def _shape(value, prefix: str, paths: dict, enums: dict, depth: int = 0) -> None:
    """记下 JSON 的结构：路径 → 类型；只有 type / role 这类枚举字段才记值（截 40 字），正文一律不记。"""
    if depth > 6:
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.setdefault(path, set()).add(type(inner).__name__)
            if key in _PROBE_ENUM_KEYS and isinstance(inner, str) and len(enums.setdefault(path, set())) < 20:
                enums[path].add(inner[:40])
            _shape(inner, path, paths, enums, depth + 1)
    elif isinstance(value, list):
        for inner in value[:50]:
            _shape(inner, f"{prefix}[]", paths, enums, depth + 1)


def _antigravity_probe(event: str, data: dict) -> None:
    """Antigravity 结构探针：钩子输入和记录文件长什么样。结果追加到 ob_agy_probe.json，供用户贴进 issue。"""
    paths: dict = {}
    enums: dict = {}
    _shape(data, "", paths, enums)
    entry = {"event": event, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "payload": {k: sorted(v) for k, v in paths.items()}, "transcript": None}
    transcript = str(data.get("transcriptPath") or "")
    if transcript:
        t_paths: dict = {}
        t_enums: dict = {}
        lines = 0
        try:
            with open(transcript, encoding="utf-8") as handle:
                for line in handle:
                    lines += 1
                    try:
                        _shape(json.loads(line), "", t_paths, t_enums)
                    except ValueError:
                        t_paths.setdefault("<不是 JSON 的行>", set()).add("str")
            entry["transcript"] = {"lines": lines, "paths": {k: sorted(v) for k, v in t_paths.items()},
                                   "enum_values": {k: sorted(v) for k, v in t_enums.items()}}
        except OSError as exc:
            entry["transcript"] = {"error": type(exc).__name__}
    path = _state_dir() / "ob_agy_probe.json"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps((old + [entry])[-10:], ensure_ascii=False, indent=1), encoding="utf-8")


_AGY_INTERMEDIATE = ("my_tool_call_analysis:",)


def _agy_rows(data: dict) -> list:
    """agy 的记录文件：每行一个 JSON，type 为 USER_INPUT / PLANNER_RESPONSE / CHECKPOINT / 各种工具步骤。
    格式参照两个能用的开源实现（lidge-ai/cli-jaw 的 agy-transcript.ts、heyxiaoc/not-fade-away 的规格）；读不懂就返回空。"""
    path = str(data.get("transcriptPath") or "")
    if not path:
        conv = str(data.get("conversationId") or "")
        path = str(Path.home() / ".gemini" / "antigravity-cli" / "brain" / conv / ".system_generated" / "logs"
                   / "transcript.jsonl") if conv else ""
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("type"), str):
                    rows.append(row)
    except OSError:
        return []
    return rows[-2000:]


def _agy_user_text(content) -> str:
    text = str(content or "")
    start, end = text.find("<USER_REQUEST>"), text.find("</USER_REQUEST>")
    if start != -1 and end > start:
        text = text[start + len("<USER_REQUEST>"):end]
    return text.strip()


def _agy_final_reply(row: dict) -> str:
    """一轮的最终回复：PLANNER_RESPONSE、正文非空、没有工具调用、不是中间分析。"""
    if row.get("type") != "PLANNER_RESPONSE":
        return ""
    content = str(row.get("content") or "").strip()
    if not content or content.startswith(_AGY_INTERMEDIATE) or row.get("tool_calls"):
        return ""
    return content


def _agy_last_turn(rows: list):
    """返回 (最后一条 USER_INPUT 的下标, 它之后的行)。没有就 (-1, [])。"""
    for index in range(len(rows) - 1, -1, -1):
        if rows[index].get("type") == "USER_INPUT":
            return index, rows[index + 1:]
    return -1, []


def _antigravity(mode: str, event: str = "") -> int:
    """Antigravity（agy）的钩子输入是 camelCase、不带用户原话和回复正文（只给记录文件路径），原文从记录文件里读：
    SessionStart 注入接续 / dream；PreInvocation 只在一轮的第一次调用时存原话 + 唤醒召回；
    Stop（正常结束）存最终回复并补存原话（同一个去重键）。输出必须是 JSON。"""
    out: dict = {}
    try:
        data = json.load(sys.stdin)
        conv = str(data.get("conversationId") or "")[:200]
        workspaces = data.get("workspacePaths") or [""]
        base = {"session_id": conv, "cwd": str(workspaces[0] or ""), "client": "antigravity"}
        payload = None
        if mode == "probe":
            _antigravity_probe(event or str(data.get("hookEventName") or ""), data)
        elif not os.environ.get("OB_HOOK_TOKEN") or not conv:
            pass
        elif mode == "session-start":
            payload = {**base, "source": "startup"}
        elif mode == "prompt":
            rows = _agy_rows(data)
            index, after = _agy_last_turn(rows)
            # 这一轮里模型已经回过话（工具调用后的再次调用）就不再处理，免得一轮召回好几次。
            if index >= 0 and not any(r.get("type") == "PLANNER_RESPONSE" for r in after):
                text = _agy_user_text(rows[index].get("content"))
                if text:
                    payload = {**base, "prompt": text, "prompt_id": f"agy:{conv}:{rows[index].get('step_index')}"}
        elif mode == "stop" and str(data.get("terminationReason") or "NO_TOOL_CALL") == "NO_TOOL_CALL":
            rows = _agy_rows(data)
            index, after = _agy_last_turn(rows)
            replies = [r for r in after if _agy_final_reply(r)]
            if index >= 0 and replies:
                step = rows[index].get("step_index")
                messages = [{"key": f"u:agy:{conv}:{step}", "role": "user",
                             "text": _agy_user_text(rows[index].get("content"))},
                            {"key": f"a:agy:{conv}:{replies[-1].get('step_index')}", "role": "assistant",
                             "text": _agy_final_reply(replies[-1])}]
                payload = {**base, "messages": [m for m in messages if m["text"]]}
        if payload is not None:
            pending = _pending_errors() if mode != "stop" else []
            if pending:
                payload["client_errors"] = pending
            result = _send(mode, payload, _TIMEOUTS[mode])
            if pending:
                _clear_pending()
            if mode not in _NO_WAIT:
                _after_success()
            context = result.get("context") if isinstance(result, dict) else ""
            if context:
                out = {"injectSteps": [{"systemMessage": {"systemMessage": context}}]}
    except urllib.error.HTTPError as exc:
        report_error(mode, f"OB 返回 {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        report_error(mode, f"{type(exc).__name__}: {exc}")
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main() -> int:
    _load_env_file()
    if len(sys.argv) > 2 and sys.argv[1] == "--deliver":
        return _deliver(sys.argv[2]) if os.environ.get("OB_HOOK_TOKEN") else 0
    if len(sys.argv) > 1 and sys.argv[1] == "--flush":
        return _flush_spool() if os.environ.get("OB_HOOK_TOKEN") else 0
    args = sys.argv[1:]
    client = "claude"
    if len(args) >= 2 and args[0] == "--client":
        client, args = args[1], args[2:]
    if client not in _CLIENTS:
        return 0
    mode = args[0] if args else ""
    if client == "antigravity":
        return _antigravity(mode, args[1] if len(args) > 1 else "")
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
    if mode not in _NO_WAIT:  # 等了结果、确实连上了
        _after_success()

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
