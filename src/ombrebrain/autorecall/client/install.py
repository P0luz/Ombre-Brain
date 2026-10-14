#!/usr/bin/env python3
"""一键给 Claude Code 装上 Ombre Brain 自动召回钩子（只用标准库）。

在装了 Claude Code 的那台电脑上运行：

    curl -fsSL <OB地址>/autorecall/client/install.py | python3 - --url <OB地址>

或者先下载再运行（Windows 也行）：

    curl -o install.py <OB地址>/autorecall/client/install.py
    python install.py --url <OB地址>

它会：
1. 把 ob_hook.py 放进 ~/.claude/hooks/（同目录有就复制，没有就从 OB 下载）；
2. 备份 ~/.claude/settings.json，再把四个钩子合并进去——只增改 ob_hook.py 自己的条目，你原有的别的钩子和设置不动；
3. 把 OB_URL / OB_HOOK_TOKEN 写进 settings.json 的 env（加 --no-env 则不写，改为提示你自己 export）；
4. 连一下 OB，告诉你 token 对不对、开关开没开。

重复运行是安全的（同样的条目只保留一份）。卸载：加 --uninstall。
自检：加 --check——按 settings.json 里登记的命令把四个钩子真跑一遍，再去 OB 确认每个都到了。
Claude Code 升级后、或者觉得「好像没在存」时跑一下。
token 不放进命令行参数也行：不给 --token 时会在终端里隐藏输入。
"""

import argparse
import getpass
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_MARKER = "ob_hook.py"
_EVENTS = {
    # 事件: (钩子模式, 额外字段)
    "SessionStart": ("session-start", {"timeout": 45}),
    "UserPromptSubmit": ("prompt", {"timeout": 15}),
    # Stop 必须同步：后台（async）钩子在会话退出时会被 Claude Code 直接杀掉，
    # 2026-10-14 实测 claude -p 下回复一条都没存上。同步只多等一次本地请求（实测约 60ms）。
    "Stop": ("stop", {"timeout": 20}),
    "SessionEnd": ("session-end", {"timeout": 10}),
    # 压缩前抢救（照 basic-memory）：compact 之前把本会话补存完整。
    "PreCompact": ("pre-compact", {"timeout": 10}),
}
_ENV_KEYS = ("OB_URL", "OB_HOOK_TOKEN")


def _say(message: str) -> None:
    print(message, flush=True)


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(request, timeout: float):
    """本机地址绕过系统代理（macOS 上 urllib 会把 127.0.0.1 也交给代理）；远端照常。"""
    target = request if isinstance(request, str) else request.full_url
    host = urllib.parse.urlsplit(target).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return _DIRECT.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _fetch(url: str, timeout: float = 15) -> bytes:
    with _open(url, timeout) as response:
        return response.read()


def _client_source(base_url: str, name: str, must_contain: bytes) -> bytes:
    sibling = Path(__file__).resolve().parent / name if "__file__" in globals() else None
    if sibling and sibling.is_file():
        return sibling.read_bytes()
    raw = _fetch(f"{base_url}/autorecall/client/{name}")
    if must_contain not in raw:
        raise RuntimeError(f"下载到的 {name} 内容不对，请确认 --url 是 Ombre Brain 的地址")
    return raw


def _load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"{path} 不是合法 JSON，为了不弄坏它，这次什么都没改：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} 顶层不是对象，这次什么都没改")
    return data


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _strip_ours(settings: dict) -> None:
    """去掉 settings 里所有指向 ob_hook.py 的钩子条目；空掉的组和事件一并去掉。"""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"] if _MARKER not in str((h or {}).get("command", ""))]
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def _command(hook_path: Path, mode: str) -> str:
    return f'"{sys.executable}" "{hook_path}" {mode}'


def merge_settings(settings: dict, hook_path: Path, url: str, token: str, write_env: bool) -> dict:
    _strip_ours(settings)
    hooks = settings.setdefault("hooks", {})
    for event, (mode, extra) in _EVENTS.items():
        entry = {"type": "command", "command": _command(hook_path, mode), **extra}
        hooks.setdefault(event, []).append({"hooks": [entry]})
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    if write_env:
        env.update({"OB_URL": url, "OB_HOOK_TOKEN": token})
    else:
        for key in _ENV_KEYS:
            env.pop(key, None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    return settings


def unmerge_settings(settings: dict) -> dict:
    _strip_ours(settings)
    env = settings.get("env")
    if isinstance(env, dict):
        for key in _ENV_KEYS:
            env.pop(key, None)
        if not env:
            settings.pop("env", None)
    return settings


def check_connection(url: str, token: str) -> str:
    """用 resume 调一次 session-start：服务端对 resume 不注入、不存任何东西，只用来验 token 和开关。"""
    request = urllib.request.Request(
        f"{url}/autorecall/session-start",
        data=json.dumps({"session_id": "ob-install-check", "source": "resume"}).encode(),
        headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": token},
        method="POST",
    )
    try:
        with _open(request, 10) as response:
            body = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "token 不对（401）：检查 OB 的 OMBRE_HOOK_TOKEN / hooks.token 和你填的是否一致"
        return f"OB 返回了 {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return f"连不上 {url}：{exc}"
    if isinstance(body, dict) and body.get("disabled"):
        return "已连通，但自动召回开关还关着：去 Dashboard「设置 → 自动召回」打开"
    return "ok"


def run_check(settings_path: Path) -> int:
    """按 settings.json 里登记的真实命令跑一遍四个钩子，再去 OB 确认都到了。"""
    import datetime as _dt
    import subprocess
    import uuid

    try:
        settings = _load_settings(settings_path)
    except RuntimeError as exc:
        _say(f"✗ {exc}")
        return 1
    env = {**os.environ, **{k: str(v) for k, v in (settings.get("env") or {}).items() if k in _ENV_KEYS}}
    url = env.get("OB_URL", "").rstrip("/")
    token = env.get("OB_HOOK_TOKEN", "")
    if not url or not token:
        _say("✗ 找不到 OB_URL / OB_HOOK_TOKEN（settings.json 的 env 里没有，当前 shell 里也没有）")
        return 1
    status = check_connection(url, token)
    if status != "ok":
        _say(f"✗ {status}")
        return 1
    commands = {}
    for event, (mode, _extra) in _EVENTS.items():
        for group in (settings.get("hooks") or {}).get(event) or []:
            for hook in (group or {}).get("hooks") or []:
                if _MARKER in str((hook or {}).get("command", "")):
                    commands[mode] = hook["command"]
    session = f"ob-check-{uuid.uuid4().hex[:12]}"
    started = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=2)
    payloads = {
        "session-start": {"source": "startup"},
        "prompt": {"prompt": "自检", "prompt_id": session},
        "stop": {"prompt_id": session, "last_assistant_message": "自检"},
        "session-end": {"transcript_path": ""},
        "pre-compact": {"transcript_path": ""},
    }
    for mode, extra in payloads.items():
        command = commands.get(mode)
        if not command:
            continue
        data = json.dumps({"session_id": session, "cwd": os.getcwd(), **extra})
        try:
            subprocess.run(command, shell=True, input=data, text=True, env=env, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            pass
    # 关窗 / 压缩前没有可补存的原文时钩子不发请求，这里替它们各补一个直连探针，只验证链路。
    for mode in ("session-end", "pre-compact"):
        if mode not in commands:
            continue
        request = urllib.request.Request(
            f"{url}/autorecall/session-end",
            data=json.dumps({"session_id": session, "messages": [], "event": mode}).encode(),
            headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": token}, method="POST")
        try:
            _open(request, 10).read()
        except Exception:
            pass
    seen: dict = {}
    for _ in range(20):  # stop 是交给后台发的，等它一会儿
        time.sleep(0.5)
        request = urllib.request.Request(f"{url}/autorecall/status", data=b"{}",
                                         headers={"Content-Type": "application/json", "X-Ombre-Hook-Token": token},
                                         method="POST")
        try:
            seen = json.loads(_open(request, 10).read().decode() or "{}").get("seen") or {}
        except Exception:
            seen = {}
        fresh = {m for m, at in seen.items()
                 if _dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")) >= started}
        if all(m in fresh for m in commands):
            break
    names = {"session-start": "开窗 SessionStart", "prompt": "提问 UserPromptSubmit",
             "stop": "存回复 Stop", "session-end": "关窗 SessionEnd", "pre-compact": "压缩前 PreCompact"}
    ok = True
    for mode, label in names.items():
        if mode not in commands:
            ok = False
            _say(f"✗ {label}：settings.json 里没挂这个钩子——重新运行安装即可")
        elif mode in fresh:
            _say(f"✓ {label}：已到达 OB")
        else:
            ok = False
            _say(f"✗ {label}：命令跑了，但 OB 没收到")
    if not ok:
        log = Path(env.get("OB_RELAY_STATE") or Path.home() / ".claude" / "hooks" / ".x").parent / "ob_hook.log"
        if log.exists():
            _say("· ob_hook.log 最后几行：")
            for line in log.read_text(encoding="utf-8").splitlines()[-5:]:
                _say(f"    {line}")
        _say("· 另外确认：装完后重开了 Claude Code 窗口；项目里的 .claude/settings.json 没有 disableAllHooks。")
        return 1
    _say("✓ 四个钩子都通。注意：这只证明命令能跑通；Claude Code 是否真的加载了这些钩子，以重开窗口后面板上的「最后到达」为准。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="给 Claude Code 装上 Ombre Brain 自动召回钩子")
    parser.add_argument("--url", help="Ombre Brain 地址，例如 http://127.0.0.1:18001")
    parser.add_argument("--token", help="hook token；不给就在终端里隐藏输入")
    parser.add_argument("--settings", default=str(Path.home() / ".claude" / "settings.json"))
    parser.add_argument("--hooks-dir", default=str(Path.home() / ".claude" / "hooks"))
    parser.add_argument("--no-env", action="store_true", help="不把地址和 token 写进 settings.json")
    parser.add_argument("--uninstall", action="store_true", help="卸载")
    parser.add_argument("--check", action="store_true", help="自检：把四个钩子真跑一遍，确认 OB 都收到了")
    args = parser.parse_args(argv)

    settings_path = Path(args.settings).expanduser()
    if args.check:
        return run_check(settings_path)
    hook_path = Path(args.hooks_dir).expanduser() / _MARKER

    try:
        settings = _load_settings(settings_path)
    except RuntimeError as exc:
        _say(f"✗ {exc}")
        return 1

    if args.uninstall:
        if settings_path.exists():
            shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak-{int(time.time())}"))
            _write_atomic(settings_path, unmerge_settings(settings))
        for leftover in (hook_path, hook_path.with_name("ob_relay.py"), hook_path.with_name(".ob_relay.json"),
                         hook_path.with_name("install.py"), hook_path.with_name(".ob_hook_errors.json")):
            if leftover.exists():
                leftover.unlink()
        _say("✓ 已卸载：settings.json 里的 Ombre Brain 钩子和 env 已去掉（原文件已备份），ob_hook.py 已删除。")
        return 0

    url = args.url
    if not url:
        try:
            url = input("Ombre Brain 地址（如 http://127.0.0.1:18001）：")
        except EOFError:  # curl | python3 - 时 stdin 是脚本本身，只能走 --url
            _say("✗ 请加 --url <Ombre Brain 地址>")
            return 1
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        _say("✗ 地址要以 http:// 或 https:// 开头")
        return 1
    token = (args.token or getpass.getpass("hook token（输入时不显示）：")).strip()
    if not token:
        _say("✗ 没有 token：先在 OB 设置 OMBRE_HOOK_TOKEN 或 config.yaml 的 hooks.token")
        return 1

    try:
        sources = {
            hook_path: _client_source(url, _MARKER, b"def main()"),
            hook_path.with_name("ob_relay.py"): _client_source(url, "ob_relay.py", b"def serve("),
            # 自己也留一份，以后 `python3 ~/.claude/hooks/install.py --check` 就能离线自检。
            hook_path.with_name("install.py"): _client_source(url, "install.py", b"def run_check("),
        }
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        _say(f"✗ 拿不到钩子脚本：{exc}")
        return 1
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    for target, source in sources.items():
        target.write_bytes(source)
        try:
            target.chmod(0o755)
        except OSError:
            pass

    if settings_path.exists():
        backup = settings_path.with_name(f"settings.json.bak-{int(time.time())}")
        shutil.copy2(settings_path, backup)
        _say(f"· 原 settings.json 已备份到 {backup}")
    _write_atomic(settings_path, merge_settings(settings, hook_path, url, token, not args.no_env))
    _say(f"✓ 钩子脚本：{hook_path}（自检：python3 {hook_path.with_name('install.py')} --check）")
    _say(f"✓ 已合并进 {settings_path}（SessionStart / UserPromptSubmit / Stop / SessionEnd）")
    if args.no_env:
        _say(f"· 记得在启动 Claude Code 的 shell 里设置：export OB_URL={url} OB_HOOK_TOKEN=…")

    status = check_connection(url, token)
    if status == "ok":
        _say("✓ 已连通 Ombre Brain，开关已开。重开一个 Claude Code 窗口就生效。")
        return 0
    _say(f"! {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
