"""/autorecall/* 四个钩子端点：鉴权、跨站、开关、请求体校验、正常往返。"""

import json

import pytest

from ombrebrain.autorecall import AutoRecallService
from web import autorecall


class _MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler
        return decorator


class _Request:
    def __init__(self, body, *, token="secret", origin="", source="client"):
        self.source = source
        self._raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.headers = {"content-length": str(len(self._raw))}
        if token:
            self.headers["x-ombre-hook-token"] = token
        if origin:
            self.headers["origin"] = origin

    async def body(self):
        return self._raw


class _Buckets:
    def __init__(self, buckets):
        self.buckets = {b["id"]: b for b in buckets}

    async def list_all(self, include_archive=False):
        return list(self.buckets.values())

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


def _routes(monkeypatch, tmp_path, enabled=True, buckets=()):
    monkeypatch.setenv("OMBRE_HOOK_TOKEN", "secret")
    monkeypatch.delenv("OMBRE_HOOK_ALLOW_PUBLIC", raising=False)
    monkeypatch.setattr(autorecall.sh, "config", {"hooks": {}}, raising=False)
    monkeypatch.setattr(autorecall.sh, "_client_key", lambda request: request.source)
    monkeypatch.setattr(autorecall.sh, "_is_authenticated", lambda request: False, raising=False)
    with autorecall._rate_lock:
        autorecall._source_events.clear()
        autorecall._global_events.clear()
    service = AutoRecallService(
        _Buckets([{"id": b, "content": c, "metadata": {"name": b, "type": "dynamic"}} for b, c in buckets]),
        str(tmp_path),
        {"autorecall": {"enabled": enabled, "agree_top": 0}},
    )
    monkeypatch.setattr(autorecall.sh, "autorecall_service", service, raising=False)
    mcp = _MCP()
    autorecall.register(mcp)
    return {
        path.rsplit("/", 1)[-1]: h for (m, path), h in mcp.routes.items()
        if m == "POST" and path.startswith("/autorecall/")
    }, service


def _json(response):
    return json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_routes_registered(monkeypatch, tmp_path):
    routes, _ = _routes(monkeypatch, tmp_path)
    assert set(routes) == {"session-start", "prompt", "stop", "session-end", "status"}


@pytest.mark.asyncio
async def test_auth_and_cross_site(monkeypatch, tmp_path):
    routes, service = _routes(monkeypatch, tmp_path)
    body = {"session_id": "s", "messages": [{"key": "1", "role": "user", "text": "你好呀今天"}]}
    assert (await routes["stop"](_Request(body, token=""))).status_code == 401
    assert (await routes["stop"](_Request(body, token="wrong"))).status_code == 401
    assert not service.store.exists
    # 带着 token 的跨站请求放行（token 本身就是授权）；Dashboard cookie 走跨站则拒绝
    monkeypatch.setattr(autorecall.sh, "_is_authenticated", lambda request: True, raising=False)
    assert (await routes["stop"](_Request(body, token="", origin="https://evil.example"))).status_code == 403


@pytest.mark.asyncio
async def test_disabled_is_silent(monkeypatch, tmp_path):
    routes, service = _routes(monkeypatch, tmp_path, enabled=False)
    for name, body in (
        ("session-start", {"session_id": "s", "source": "startup"}),
        ("prompt", {"session_id": "s", "prompt": "还记得吗"}),
        ("stop", {"session_id": "s", "messages": []}),
    ):
        resp = await routes[name](_Request(body))
        assert resp.status_code == 200 and _json(resp) == {"disabled": True}
    assert not service.store.exists


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"[]", b"not json", json.dumps({"messages": []}).encode(),
                                 json.dumps({"session_id": "s", "messages": "x"}).encode()])
async def test_bad_bodies_are_400(monkeypatch, tmp_path, raw):
    routes, _ = _routes(monkeypatch, tmp_path)
    assert (await routes["stop"](_Request(raw))).status_code == 400


@pytest.mark.asyncio
async def test_oversized_body_rejected(monkeypatch, tmp_path):
    routes, _ = _routes(monkeypatch, tmp_path)
    req = _Request({"session_id": "s", "messages": []})
    req.headers["content-length"] = str(autorecall._MAX_BODY_BYTES + 1)
    assert (await routes["stop"](req)).status_code == 400


@pytest.mark.asyncio
async def test_round_trip(monkeypatch, tmp_path):
    routes, service = _routes(monkeypatch, tmp_path, buckets=[("hotpot", "重庆火锅辣到流泪")])
    stop = await routes["stop"](_Request({"session_id": "old", "cwd": "/p", "messages": [
        {"key": "u1", "role": "user", "text": "今天想吃火锅"},
        {"key": "a1", "role": "assistant", "text": "好呀"},
    ]}))
    assert stop.status_code == 200 and _json(stop)["added"] == 2
    start = await routes["session-start"](_Request({"session_id": "new", "source": "startup", "cwd": "/p"}))
    assert "今天想吃火锅" in _json(start)["context"]
    prompt = await routes["prompt"](_Request({"session_id": "new", "prompt": "还记得那家火锅吗"}))
    assert "hotpot" in _json(prompt)["context"]
    end = await routes["session-end"](_Request({"session_id": "old", "messages": [
        {"key": "u1", "role": "user", "text": "今天想吃火锅"},
    ]}))
    assert _json(end)["added"] == 0


@pytest.mark.asyncio
async def test_rate_limit(monkeypatch, tmp_path):
    routes, _ = _routes(monkeypatch, tmp_path)
    monkeypatch.setattr(autorecall, "_RATE_SOURCE_LIMIT", 2)
    body = {"session_id": "s", "prompt": "hi"}
    assert (await routes["prompt"](_Request(body))).status_code == 200
    assert (await routes["prompt"](_Request(body))).status_code == 200
    assert (await routes["prompt"](_Request(body))).status_code == 429
