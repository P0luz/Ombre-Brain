import json

import pytest
from mcp.server.fastmcp import FastMCP

from ombrebrain.you import YouService, YouStore, YouStoreError, YouToolGate
from tools import _runtime as tools_runtime
from tools.you import dispatch
from web import you as you_web


class FakeBucketManager:
    async def get(self, _bucket_id):
        return None


class FakeDehydrator:
    pass


class FakeSourceStore:
    def read(self, _source_id):
        return ""


class RouteMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    def __init__(self, body=None):
        self.body = body

    async def json(self):
        return self.body


def make_service(tmp_path):
    return YouService(
        store=YouStore(tmp_path),
        bucket_mgr=FakeBucketManager(),
        dehydrator=FakeDehydrator(),
        source_store=FakeSourceStore(),
    )


@pytest.mark.asyncio
async def test_tool_gate_adds_and_removes_only_you(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(tools_runtime, "you_service", service)
    mcp = FastMCP("test")

    async def baseline() -> str:
        return "ok"

    mcp._tool_manager.add_tool(baseline, name="breath")
    gate = YouToolGate(mcp, dispatch)

    baseline_manifest = [
        tool.model_dump(mode="json") for tool in await mcp.list_tools()
    ]
    assert [item["name"] for item in baseline_manifest] == ["breath"]
    state = service.set_enabled(True, expected_revision=0)
    assert gate.sync(state.enabled) is True
    enabled_manifest = [
        tool.model_dump(mode="json") for tool in await mcp.list_tools()
    ]
    assert [item["name"] for item in enabled_manifest] == ["breath", "You"]
    assert [item for item in enabled_manifest if item["name"] != "You"] == baseline_manifest

    cached_handler = dispatch
    state = service.set_enabled(False, expected_revision=state.state_revision)
    assert gate.sync(state.enabled) is False
    assert [
        tool.model_dump(mode="json") for tool in await mcp.list_tools()
    ] == baseline_manifest
    with pytest.raises(YouStoreError, match="unknown tool"):
        await cached_handler()


@pytest.mark.asyncio
async def test_authenticated_toggle_api_drives_persisted_state_and_mcp_visibility(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    tool_mcp = FastMCP("tools")
    gate = YouToolGate(tool_mcp, dispatch)
    route_mcp = RouteMCP()
    monkeypatch.setattr(you_web.sh, "you_service", service)
    monkeypatch.setattr(you_web.sh, "you_tool_gate", gate)
    monkeypatch.setattr(you_web.sh, "_require_auth", lambda _request: None)
    you_web.register(route_mcp)

    get_route = route_mcp.routes[("GET", "/api/settings/you")]
    post_route = route_mcp.routes[("POST", "/api/settings/you")]
    initial = await get_route(JsonRequest())
    assert json.loads(initial.body) == {"enabled": False, "state_revision": 0}
    assert not (tmp_path / ".you").exists()

    enabled = await post_route(JsonRequest({"enabled": True, "state_revision": 0}))
    assert enabled.status_code == 200
    assert json.loads(enabled.body) == {"enabled": True, "state_revision": 1}
    assert [tool.name for tool in await tool_mcp.list_tools()] == ["You"]

    stale = await post_route(JsonRequest({"enabled": False, "state_revision": 0}))
    assert stale.status_code == 409
    assert [tool.name for tool in await tool_mcp.list_tools()] == ["You"]

    disabled = await post_route(JsonRequest({"enabled": False, "state_revision": 1}))
    assert disabled.status_code == 200
    assert json.loads(disabled.body) == {"enabled": False, "state_revision": 2}
    assert await tool_mcp.list_tools() == []


@pytest.mark.asyncio
async def test_toggle_api_requires_auth_and_rejects_extra_control_fields(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    gate = YouToolGate(FastMCP("tools"), dispatch)
    route_mcp = RouteMCP()
    denied = type("Denied", (), {"status_code": 401})()
    monkeypatch.setattr(you_web.sh, "you_service", service)
    monkeypatch.setattr(you_web.sh, "you_tool_gate", gate)
    monkeypatch.setattr(you_web.sh, "_require_auth", lambda _request: denied)
    you_web.register(route_mcp)

    response = await route_mcp.routes[("POST", "/api/settings/you")](
        JsonRequest({"enabled": True, "state_revision": 0})
    )
    assert response is denied
    assert not (tmp_path / ".you").exists()

    monkeypatch.setattr(you_web.sh, "_require_auth", lambda _request: None)
    response = await route_mcp.routes[("POST", "/api/settings/you")](
        JsonRequest({"enabled": True, "state_revision": 0, "review": True})
    )
    assert response.status_code == 400
    assert not (tmp_path / ".you").exists()
