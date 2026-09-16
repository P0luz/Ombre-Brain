"""MCP-boundary regressions for the split breath tool.

The public schema intentionally remains parameter-free so clients auto-load
the default surfacing tool.  A client may nevertheless keep the pre-2.6.8
schema cached across an upgrade and send the former breath arguments.  Those
arguments must reach dispatch instead of being silently discarded by
FastMCP/Pydantic's default ``extra=ignore`` validation.
"""

import pytest
from mcp.server.fastmcp.exceptions import ToolError


QUERY = "两个都是你 怎么还让我选"


@pytest.mark.asyncio
async def test_public_breath_schema_stays_empty_but_cached_query_args_are_forwarded(
    monkeypatch,
):
    import server

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "query-dispatched"

    monkeypatch.setattr(server._t_breath, "dispatch", fake_dispatch)
    tool = server.mcp._tool_manager.get_tool("breath")

    listed = next(item for item in await server.mcp.list_tools() if item.name == "breath")
    assert listed.inputSchema["properties"] == {}
    assert set(tool.fn_metadata.arg_model.model_fields) == {
        "query",
        "max_tokens",
        "domain",
        "valence",
        "arousal",
        "max_results",
        "importance_min",
        "tags",
        "catalog",
        "mode",
    }

    output = await tool.run(
        {
            "query": QUERY,
            "max_results": 1,
            "max_tokens": 6000,
        }
    )

    assert output == "query-dispatched"
    assert seen == {
        "query": QUERY,
        "max_tokens": 6000,
        "domain": "",
        "valence": -1,
        "arousal": -1,
        "max_results": 1,
        "importance_min": -1,
        "tags": "",
        "catalog": False,
        "mode": "",
    }


@pytest.mark.asyncio
async def test_cached_catalog_arg_reaches_breath_dispatch(monkeypatch):
    import server

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "catalog-dispatched"

    monkeypatch.setattr(server._t_breath, "dispatch", fake_dispatch)
    tool = server.mcp._tool_manager.get_tool("breath")

    output = await tool.run(
        {
            "query": QUERY,
            "catalog": True,
            "max_results": 3,
            "max_tokens": 6000,
        }
    )

    assert output == "catalog-dispatched"
    assert seen["query"] == QUERY
    assert seen["catalog"] is True
    assert seen["max_results"] == 3
    assert seen["max_tokens"] == 6000


@pytest.mark.asyncio
async def test_parameter_free_breath_still_dispatches_with_all_defaults(monkeypatch):
    import server

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "default-dispatched"

    monkeypatch.setattr(server._t_breath, "dispatch", fake_dispatch)
    tool = server.mcp._tool_manager.get_tool("breath")

    assert await tool.run({}) == "default-dispatched"
    assert seen == {
        "query": "",
        "max_tokens": 0,
        "domain": "",
        "valence": -1,
        "arousal": -1,
        "max_results": 0,
        "importance_min": -1,
        "tags": "",
        "catalog": False,
        "mode": "",
    }


@pytest.mark.asyncio
async def test_cached_handoff_mode_reaches_hidden_breath_callable(monkeypatch):
    import server

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "handoff-dispatched"

    monkeypatch.setattr(server._t_breath, "dispatch", fake_dispatch)
    tool = server.mcp._tool_manager.get_tool("breath")

    assert await tool.run({"mode": "handoff"}) == "handoff-dispatched"
    assert seen["mode"] == "handoff"


@pytest.mark.asyncio
async def test_fresh_clients_can_request_handoff_through_breath_advanced(monkeypatch):
    import server

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "handoff-dispatched"

    monkeypatch.setattr(server._t_breath, "dispatch", fake_dispatch)
    listed = next(
        item for item in await server.mcp.list_tools()
        if item.name == "breath_advanced"
    )
    assert "mode" in listed.inputSchema["properties"]

    tool = server.mcp._tool_manager.get_tool("breath_advanced")
    assert await tool.run({"mode": "handoff"}) == "handoff-dispatched"
    assert seen["mode"] == "handoff"


@pytest.mark.asyncio
async def test_unknown_cached_breath_argument_is_rejected_instead_of_ignored():
    import server

    tool = server.mcp._tool_manager.get_tool("breath")

    with pytest.raises(ToolError, match="extra_forbidden"):
        await tool.run({"query": QUERY, "max_result": 1})
