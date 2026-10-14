#!/usr/bin/env python3
"""Ombre Brain × Claude Code 自动召回钩子（只用标准库）。

一般不用手动装：运行同目录的 install.py（Dashboard「自动召回」卡片里有一键命令）。

用法（install.py 会替你挂好；手动挂法见 deploy/claude-code-hooks/settings.example.json）：

    python3 ob_hook.py session-start   # SessionStart：开窗 / 压缩后接续 + 今日 dream
    python3 ob_hook.py prompt          # UserPromptSubmit：存用户原文 + hold 提醒 + 唤醒召回
    python3 ob_hook.py stop            # Stop：存这一轮的助手回复（同步挂；后台钩子退出时会被杀）
    python3 ob_hook.py session-end     # SessionEnd：尽力补存用户原文

环境变量：
    OB_URL         Ombre Brain 地址，默认 http://127.0.0.1:18001
    OB_HOOK_TOKEN  与服务端 OMBRE_HOOK_TOKEN / hooks.token 相同；没设就什么都不做
    OB_HOOK_DEBUG  设为 1 时把错误打到 stderr

任何错误都静默退出 0：记忆出问题不能卡住对话。
不解析对话记录文件取当轮内容——官方说那是内部格式、随版本会变；
用户原文取 UserPromptSubmit 的 prompt，助手回复取 Stop 的 last_assistant_message。
只有 SessionEnd 的补存会读记录文件，读不懂就跳过。
"""

import hashlib
import json
import os
import sys
import uuid
import urllib.error
import urllib.request

_TIMEOUTS = {"session-start": 40, "prompt": 12, "stop": 25, "session-end": 8}
_EVENT_NAMES = {"session-start": "SessionStart", "prompt": "UserPromptSubmit"}
_MAX_BACKFILL = 1000


def _debug(message: str) -> None:
    if os.environ.get("OB_HOOK_DEBUG") == "1":
        print(f"[ob_hook] {message}", file=sys.stderr)


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
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


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
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in _TIMEOUTS or not os.environ.get("OB_HOOK_TOKEN"):
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    session_id = str(data.get("session_id") or "")
    if not session_id:
        return 0
    prompt_id = str(data.get("prompt_id") or "")
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
        text = data.get("last_assistant_message") or ""
        if not text.strip():
            return 0
        # 同一个 prompt 可能停不止一次（Stop 钩子让它续写），带上内容摘要区分。
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        key = f"a:{prompt_id}:{digest}" if prompt_id else f"a:rand-{uuid.uuid4().hex}"
        payload = {"session_id": session_id, "cwd": cwd, "messages": [
            {"key": key, "role": "assistant", "text": text},
        ]}
    else:
        messages = _backfill_user_messages(str(data.get("transcript_path") or ""))
        if not messages:
            return 0
        payload = {"session_id": session_id, "messages": messages, "cwd": cwd}

    try:
        result = _post(mode, payload, _TIMEOUTS[mode])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _debug(f"{mode} failed: {exc}")
        return 0

    context = result.get("context") if isinstance(result, dict) else ""
    if context and mode in _EVENT_NAMES:
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": _EVENT_NAMES[mode], "additionalContext": context},
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # 兜底：钩子绝不能让对话报错
        _debug(f"unexpected: {exc!r}")
        sys.exit(0)
