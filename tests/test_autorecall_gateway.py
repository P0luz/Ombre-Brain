"""OpenAI 兼容记忆网关：假 OB + 假模型商，真 HTTP 走一遍。"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "ombrebrain" / "autorecall" / "client"))
import ob_gateway  # noqa: E402

_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class FakeOB:
    def __init__(self, down=False):
        self.calls = []
        self.down = down
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                name = self.path.rsplit("/", 1)[-1]
                outer.calls.append((name, body, self.headers.get("X-Ombre-Hook-Token")))
                if outer.down:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                reply = {"session-start": {"context": "[接续] 上次聊到猫"},
                         "prompt": {"context": "[记忆] 猫叫阿树" if "记得" in body.get("prompt", "") else ""},
                         }.get(name, {"added": 1})
                data = json.dumps(reply, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server, self.url = _serve(H)

    def names(self):
        return [c[0] for c in self.calls]


class FakeUpstream:
    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append((self.path, None, dict(self.headers)))
                data = b'{"data":[{"id":"m"}]}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, dict(self.headers)))
                if body.get("model") == "bad":
                    data = b'{"error":{"message":"nope"}}'
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for piece in ["你", "好", "呀"]:
                        chunk = {"choices": [{"delta": {"content": piece}}]}
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.close_connection = True
                    return
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": "收到"}}]},
                                  ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server, self.url = _serve(H)


@pytest.fixture
def stack():
    ob, up = FakeOB(), FakeUpstream()
    gw = ob_gateway.Gateway(up.url + "/v1", ob_gateway.OB(ob.url, "tok", "前端A"))
    server, url = _serve(ob_gateway.make_handler(gw))
    yield ob, up, gw, url
    for s in (ob.server, up.server, server):
        s.shutdown()


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with _DIRECT.open(req, timeout=10) as r:
        return r.status, r.read().decode()


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_new_chat_injects_start_and_recall_and_stores_reply(stack):
    ob, up, _gw, url = stack
    status, text = _post(url + "/v1/chat/completions", {
        "model": "m", "messages": [{"role": "system", "content": "你是助手"},
                                   {"role": "user", "content": "你还记得我的猫吗"}]},
        {"Authorization": "Bearer sk-upstream-key"})
    assert status == 200 and "收到" in text
    path, sent, headers = up.requests[-1]
    assert path == "/v1/chat/completions"
    system = sent["messages"][0]["content"]
    assert system.startswith("你是助手") and "[接续] 上次聊到猫" in system and "[记忆] 猫叫阿树" in system
    assert sent["messages"][1] == {"role": "user", "content": "你还记得我的猫吗"}
    assert headers.get("Authorization") == "Bearer sk-upstream-key"  # 透传
    assert _wait(lambda: "stop" in ob.names())
    stop = [c for c in ob.calls if c[0] == "stop"][0][1]
    assert stop["messages"][0]["text"] == "收到" and stop["cwd"] == "前端A"
    assert ob.names()[:2] == ["session-start", "prompt"]
    assert all(c[2] == "tok" for c in ob.calls)


def test_stream_is_relayed_and_full_text_stored(stack):
    ob, _up, _gw, url = stack
    status, text = _post(url + "/v1/chat/completions", {
        "model": "m", "stream": True, "messages": [{"role": "user", "content": "嗨"}]})
    assert status == 200 and text.count("data:") == 4 and "[DONE]" in text
    assert _wait(lambda: "stop" in ob.names())
    stop = [c for c in ob.calls if c[0] == "stop"][0][1]
    assert stop["messages"][0]["text"] == "你好呀"


def test_followup_turn_keeps_session_and_skips_session_start(stack):
    ob, up, _gw, url = stack
    first = [{"role": "user", "content": "嗨"}]
    _post(url + "/v1/chat/completions", {"model": "m", "messages": first})
    _post(url + "/v1/chat/completions", {"model": "m", "messages": first + [
        {"role": "assistant", "content": "收到"}, {"role": "user", "content": "再说一句"}]})
    assert ob.names().count("session-start") == 1
    sids = {c[1]["session_id"] for c in ob.calls}
    assert len(sids) == 1
    assert "[接续] 上次聊到猫" in up.requests[-1][1]["messages"][0]["content"]  # 接续每轮都带着


def test_tool_round_and_errors_are_not_stored(stack):
    ob, _up, _gw, url = stack
    _post(url + "/v1/chat/completions", {"model": "m", "messages": [
        {"role": "user", "content": "查天气"}, {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "content": "晴"}]})
    with pytest.raises(urllib.error.HTTPError) as err:
        _post(url + "/v1/chat/completions", {"model": "bad", "messages": [{"role": "user", "content": "x"}]})
    assert err.value.code == 400
    time.sleep(0.3)
    assert "stop" not in ob.names()


def test_other_paths_pass_through(stack):
    _ob, up, _gw, url = stack
    with _DIRECT.open(url + "/v1/models", timeout=5) as r:
        assert json.loads(r.read())["data"][0]["id"] == "m"
    assert up.requests[-1][0] == "/v1/models"


def test_ob_down_still_answers():
    ob, up = FakeOB(down=True), FakeUpstream()
    gw = ob_gateway.Gateway(up.url + "/v1", ob_gateway.OB(ob.url, "tok", "s"), recall_timeout=2)
    server, url = _serve(ob_gateway.make_handler(gw))
    try:
        status, text = _post(url + "/v1/chat/completions", {"model": "m", "messages": [
            {"role": "user", "content": "你还记得吗"}]})
        assert status == 200 and "收到" in text
        assert up.requests[-1][1]["messages"] == [{"role": "user", "content": "你还记得吗"}]
    finally:
        for s in (ob.server, up.server, server):
            s.shutdown()


def test_gateway_key_and_upstream_key():
    ob, up = FakeOB(), FakeUpstream()
    gw = ob_gateway.Gateway(up.url + "/v1", ob_gateway.OB(ob.url, "tok", "s"), key="gk", upstream_key="sk-real")
    server, url = _serve(ob_gateway.make_handler(gw))
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _post(url + "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "x"}]},
                  {"Authorization": "Bearer wrong"})
        assert err.value.code == 401 and not up.requests
        _post(url + "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "x"}]},
              {"Authorization": "Bearer gk"})
        assert up.requests[-1][2]["Authorization"] == "Bearer sk-real"
    finally:
        for s in (ob.server, up.server, server):
            s.shutdown()


def test_refuses_public_bind_without_key():
    with pytest.raises(SystemExit):
        ob_gateway.main(["--upstream", "http://x/v1", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        ob_gateway.main(["--upstream", "http://x/v1", "--upstream-key", "sk"])


def test_helpers():
    assert ob_gateway.text_of([{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {}}]) == "a"
    msgs = [{"role": "system", "content": [{"type": "text", "text": "s"}]}, {"role": "user", "content": "u"}]
    out = ob_gateway.inject(msgs, "M")
    assert out[0]["content"][-1] == {"type": "text", "text": "M"} and msgs[0]["content"] == [{"type": "text", "text": "s"}]
    assert ob_gateway.inject([{"role": "user", "content": "u"}], "M")[0] == {"role": "system", "content": "M"}
    assert ob_gateway._sse_delta(b"data: [DONE]") is None
    assert ob_gateway._sse_delta(b": ping") == ""
