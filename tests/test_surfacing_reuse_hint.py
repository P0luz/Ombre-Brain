"""#126：只在 MCP 成功浮现正文后追加可关闭的复用说明。"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

BREATH_HINT = "ℹ️ 这份浮现已在本次对话历史里；同一对话中想找具体内容，用 breath_search 带关键词检索，不必再调 breath。"
DREAM_HINT = "ℹ️ 这份 dream 结果已在本次对话历史里；同一对话中不必重复调用 dream。"


@pytest.fixture
def runtime(monkeypatch):
    import server
    from tests.test_breath_call_mode import SearchBucketManager, _bucket, _install
    from tools import _runtime as rt

    now = datetime.now().isoformat()
    manager = SearchBucketManager([_bucket("fiction", "虚构的纸船漂过蓝色小溪。", created=now, last_active=now)])
    _install(monkeypatch, manager)
    monkeypatch.setattr(server, "config", rt.config)
    monkeypatch.setattr(server, "_pop_deletion_notice", lambda: "")
    monkeypatch.setattr(server, "format_warnings_suffix", lambda _warnings: "")
    return server, rt, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,hint", [
    ("breath", {}, BREATH_HINT),
    ("breath", {"domain": "回归测试", "max_results": 1}, BREATH_HINT),
    ("breath", {"max_tokens": 500}, BREATH_HINT),
    ("dream", {}, DREAM_HINT),
])
async def test_default_hint_and_hot_disable_preserve_body(runtime, name, args, hint):
    server, rt, _ = runtime
    dispatch = getattr(server, "_t_" + name).dispatch
    baseline = await dispatch(**args)
    assert "虚构的纸船" in baseline
    tool = server.mcp._tool_manager.get_tool(name)
    assert await tool.run(args) == baseline + "\n" + hint
    rt.config["surfacing"]["reuse_hint"] = False
    assert await tool.run(args) == baseline
    rt.config["surfacing"]["reuse_hint"] = True
    assert await tool.run(args) == baseline + "\n" + hint


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["breath", "dream"])
@pytest.mark.parametrize("mode", ["empty", "unavailable", "exception", "blank"])
async def test_empty_and_errors_have_no_hint(runtime, monkeypatch, name, mode):
    server, rt, manager = runtime
    if mode == "empty":
        manager.buckets.clear()
    elif mode == "unavailable":
        monkeypatch.setattr(manager, "list_all", AsyncMock(side_effect=RuntimeError("虚构故障")))
    elif mode == "exception":
        monkeypatch.setattr(rt.decay_engine, "ensure_started", AsyncMock(side_effect=RuntimeError("虚构故障")))
    else:
        monkeypatch.setattr(getattr(server, "_t_" + name), "dispatch", AsyncMock(return_value=""))
    out = await server.mcp._tool_manager.get_tool(name).run({})
    assert BREATH_HINT not in out and DREAM_HINT not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("breath_search", {"query": "纸船"}),
    ("breath_advanced", {}),
    ("breath", {"query": "纸船"}),
    ("breath", {"catalog": True}),
])
async def test_other_modes_keep_exact_body(runtime, name, args):
    server, _, _ = runtime
    baseline = await server._t_breath.dispatch(**args)
    out = await server.mcp._tool_manager.get_tool(name).run(args)
    assert out == baseline
    assert BREATH_HINT not in out and DREAM_HINT not in out


@pytest.mark.asyncio
async def test_hint_follows_warning_and_notice(runtime, monkeypatch):
    server, _, _ = runtime
    baseline = await server._t_breath.dispatch()
    monkeypatch.setattr(server, "_pop_deletion_notice", lambda: "虚构通知\n")
    monkeypatch.setattr(server, "format_warnings_suffix", lambda _warnings: "\n虚构警告")
    assert await server.breath() == "虚构通知\n" + baseline + "\n虚构警告\n" + BREATH_HINT


@pytest.mark.asyncio
async def test_hook_never_adds_hint(monkeypatch):
    from tests.test_breath_hook_security import _handler, _bucket, _EchoDehydrator, _Request
    from web import hooks
    monkeypatch.setattr(hooks, "_is_hook_request_authorized", lambda _: True)
    monkeypatch.setattr(hooks, "_admit_hook_request", lambda _: True)
    handler = _handler(monkeypatch, [_bucket("fiction", "虚构纸船", pinned=True)], _EchoDehydrator())
    hooks.sh.config["surfacing"] = {"reuse_hint": True}
    response = await handler(_Request())
    assert response.status_code == 200
    text = response.body.decode()
    assert "虚构纸船" in text
    assert BREATH_HINT not in text and DREAM_HINT not in text


@pytest.mark.asyncio
async def test_config_api_hot_update_and_persistence(runtime, monkeypatch, tmp_path):
    import json
    import yaml
    from tests.test_github_config_api import FakeMCP, JsonRequest
    from web import config_api

    _, rt, _ = runtime
    config_path = tmp_path / "config.yaml"
    config_path.write_text("surfacing:\n  breath_max_tokens: 500\n", encoding="utf-8")
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config_api.sh, "config", rt.config)
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda _: None)
    routes = FakeMCP()
    config_api.register(routes)
    get = routes.routes[("GET", "/api/config")]
    post = routes.routes[("POST", "/api/config")]
    response = await get(JsonRequest())
    assert json.loads(response.body)["surfacing"]["reuse_hint"] is True
    for enabled in (False, True):
        response = await post(JsonRequest({"surfacing": {"reuse_hint": enabled}, "persist": True}))
        assert response.status_code == 200, response.body
        assert rt.config["surfacing"]["reuse_hint"] is enabled
        assert yaml.safe_load(config_path.read_text())["surfacing"]["reuse_hint"] is enabled
        assert yaml.safe_load(config_path.read_text())["surfacing"]["breath_max_tokens"] == 500
        assert json.loads((await get(JsonRequest())).body)["surfacing"]["reuse_hint"] is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("name,budget", [("breath", 500), ("dream", 1000)])
async def test_budget_omits_whole_bucket_but_keeps_complete_hint(runtime, name, budget):
    server, rt, manager = runtime
    from tests.test_breath_call_mode import _bucket
    now = datetime.now().isoformat()
    manager.buckets.append(_bucket("oversized", "虚构长正文" * 3000, created=now, last_active=now))
    rt.config["surfacing"][name + "_max_tokens"] = budget
    baseline = await getattr(server, "_t_" + name).dispatch()
    assert "虚构的纸船" in baseline
    assert "虚构长正文" not in baseline
    hint = BREATH_HINT if name == "breath" else DREAM_HINT
    assert await getattr(server, name)() == baseline + "\n" + hint


@pytest.mark.asyncio
async def test_success_does_not_leak_to_next_empty_call(runtime):
    server, _, manager = runtime
    assert (await server.breath()).endswith(BREATH_HINT)
    manager.buckets.clear()
    assert BREATH_HINT not in await server.breath()
    assert DREAM_HINT not in await server.dream()


@pytest.mark.asyncio
async def test_concurrent_success_and_empty_are_isolated(runtime, monkeypatch):
    import asyncio
    server, _, _ = runtime
    entered = asyncio.Event()
    release = asyncio.Event()
    dispatch = server._t_breath.dispatch

    async def interleaved(**kwargs):
        if kwargs.get("domain") == "empty":
            entered.set()
            await release.wait()
            return ""
        await entered.wait()
        result = await dispatch(**kwargs)
        release.set()
        return result

    monkeypatch.setattr(server._t_breath, "dispatch", interleaved)
    empty, success = await asyncio.gather(server.breath(domain="empty"), server.breath())
    assert empty == ""
    assert success.endswith(BREATH_HINT)


@pytest.mark.asyncio
async def test_auth_denial_has_no_hint(runtime):
    from server_app import MCPAuthMiddleware
    from tests.test_mcp_static_token_auth import _scope, _empty_receive, _collect_into
    server, _, _ = runtime
    downstream = AsyncMock()
    middleware = MCPAuthMiddleware(downstream, auth_required=True, auth_mode="token", token_validator=lambda _: False, static_token_validator=lambda _: False)
    messages = []
    await middleware(_scope([]), _empty_receive, _collect_into(messages))
    assert messages[0]["status"] == 401
    downstream.assert_not_called()
    body = messages[1]["body"].decode()
    assert BREATH_HINT not in body and DREAM_HINT not in body


def test_dashboard_reuse_hint_load_and_save_contract():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "frontend/dashboard.html").read_text()
    assert 'id="cfg-sf-reuse-hint"' in html
    assert "document.getElementById('cfg-sf-reuse-hint').value = sf.reuse_hint !== false ? 'true' : 'false'" in html
    assert "reuse_hint: document.getElementById('cfg-sf-reuse-hint').value === 'true'" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("name,hint", [("breath", BREATH_HINT), ("dream", DREAM_HINT)])
async def test_protocol_keeps_one_hint_per_text_copy(runtime, compact, name, hint):
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import create_connected_server_and_client_session
    server, _, _ = runtime
    baseline = await getattr(server, "_t_" + name).dispatch()
    probe = FastMCP("虚构记忆协议回归")
    if compact:
        server._use_unstructured_tool_results(probe)
    probe.add_tool(getattr(server, name), name=name)
    async with create_connected_server_and_client_session(probe) as client:
        result = await client.call_tool(name, {})
    expected = baseline + "\n" + hint
    assert not result.isError
    assert len(result.content) == 1
    assert result.content[0].text == expected
    assert result.content[0].text.count(hint) == 1
    if compact:
        assert result.structuredContent is None
    else:
        # 建议二原有协议：关闭 compact 时全文副本仍存在，不在本任务删除。
        assert result.structuredContent == {"result": expected}
        assert result.structuredContent["result"].count(hint) == 1
