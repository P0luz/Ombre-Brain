"""一键安装 install.py：合并不伤原有设置、可重复运行、可卸载、坏 JSON 不动；以及下载路由。"""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client"
INSTALL = CLIENT / "install.py"


class _OB(BaseHTTPRequestHandler):
    reply = (200, {})
    hook_source = (CLIENT / "ob_hook.py").read_bytes()
    relay_source = (CLIENT / "ob_relay.py").read_bytes()

    def do_GET(self):
        files = {"/autorecall/client/ob_hook.py": type(self).hook_source,
                 "/autorecall/client/ob_relay.py": type(self).relay_source}
        if self.path in files:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(files[self.path])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        code, body = type(self).reply
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def ob():
    _OB.reply = (200, {})
    httpd = HTTPServer(("127.0.0.1", 0), _OB)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _install(tmp_path, url, *extra, script=INSTALL, stdin=None):
    cmd = [sys.executable, "-"] if script is None else [sys.executable, str(script)]
    args = ["--settings", str(tmp_path / "settings.json"), "--hooks-dir", str(tmp_path / "hooks")]
    if url:
        args += ["--url", url]
    proc = subprocess.run(cmd + args + list(extra), input=stdin, capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout


def _ours(settings):
    return [h for groups in settings.get("hooks", {}).values() for g in groups for h in g["hooks"]
            if "ob_hook.py" in h["command"]]


def test_merge_keeps_existing_settings_and_is_idempotent(tmp_path, ob):
    existing = {"model": "opus", "env": {"FOO": "1"}, "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "say done"}]}]}}
    (tmp_path / "settings.json").write_text(json.dumps(existing))
    for _ in range(2):
        code, out = _install(tmp_path, ob, "--token", "tok")
        assert code == 0 and "已连通" in out
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["model"] == "opus" and settings["env"]["FOO"] == "1"
    assert settings["env"]["OB_URL"] == ob and settings["env"]["OB_HOOK_TOKEN"] == "tok"
    assert any(h["command"] == "say done" for g in settings["hooks"]["Stop"] for h in g["hooks"])
    ours = _ours(settings)
    assert len(ours) == 4  # 装两遍也只有一份
    stop = next(h for h in ours if h["command"].endswith(" stop"))
    assert "async" not in stop and stop["timeout"] == 20  # 后台钩子退出时会被杀，回复存不上
    assert (tmp_path / "hooks" / "ob_hook.py").read_bytes() == (CLIENT / "ob_hook.py").read_bytes()
    assert (tmp_path / "hooks" / "ob_relay.py").read_bytes() == (CLIENT / "ob_relay.py").read_bytes()
    assert list(tmp_path.glob("settings.json.bak-*"))


def test_uninstall_removes_only_ours(tmp_path, ob):
    (tmp_path / "settings.json").write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "say done"}]}]}, "env": {"FOO": "1"}}))
    _install(tmp_path, ob, "--token", "tok")
    code, out = _install(tmp_path, None, "--uninstall")
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert code == 0 and _ours(settings) == []
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "say done"
    assert settings["env"] == {"FOO": "1"}
    assert not (tmp_path / "hooks" / "ob_hook.py").exists() and not (tmp_path / "hooks" / "ob_relay.py").exists()


def test_broken_settings_json_is_left_untouched(tmp_path, ob):
    (tmp_path / "settings.json").write_text("{not json")
    code, out = _install(tmp_path, ob, "--token", "tok")
    assert code == 1 and "什么都没改" in out
    assert (tmp_path / "settings.json").read_text() == "{not json"
    assert not (tmp_path / "hooks").exists()


def test_no_env_keeps_token_out_of_settings(tmp_path, ob):
    _install(tmp_path, ob, "--token", "tok", "--no-env")
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "env" not in settings and len(_ours(settings)) == 4


@pytest.mark.parametrize("reply,expect", [
    ((401, {}), "token 不对"),
    ((200, {"disabled": True}), "开关还关着"),
])
def test_connection_check_reports_problems(tmp_path, ob, reply, expect):
    _OB.reply = reply
    code, out = _install(tmp_path, ob, "--token", "tok")
    assert code == 0 and expect in out


def test_piped_install_downloads_hook_and_requires_url(tmp_path, ob):
    source = INSTALL.read_text(encoding="utf-8")
    code, out = _install(tmp_path, ob, "--token", "tok", script=None, stdin=source)
    assert code == 0 and (tmp_path / "hooks" / "ob_hook.py").read_bytes() == _OB.hook_source
    assert (tmp_path / "hooks" / "ob_relay.py").read_bytes() == _OB.relay_source
    code, out = _install(tmp_path, None, "--token", "tok", script=None, stdin=source)
    assert code == 1 and "--url" in out


def test_bad_url_rejected(tmp_path):
    code, out = _install(tmp_path, "ftp://x", "--token", "tok")
    assert code == 1 and not (tmp_path / "settings.json").exists()


# ---------------- 下载路由 ----------------

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
    def __init__(self, name):
        self.path_params = {"name": name}


@pytest.mark.asyncio
async def test_client_download_route_serves_only_whitelist(monkeypatch):
    from web import autorecall
    mcp = _MCP()
    autorecall.register(mcp)
    route = mcp.routes[("GET", "/autorecall/client/{name}")]
    ok = await route(_Req("install.py"))
    assert ok.status_code == 200 and b"def main(" in ok.body
    for bad in ("../../server.py", "config.yaml", "__init__.py", ""):
        assert (await route(_Req(bad))).status_code == 404
