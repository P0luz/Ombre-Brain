import pytest


@pytest.mark.asyncio
async def test_public_mcp_tools_have_indexer_safe_metadata():
    import server

    tools = await server.mcp.list_tools()
    tools_by_name = {tool.name: tool for tool in tools}

    assert {"breath", "hold", "grow", "trace", "dream"}.issubset(tools_by_name)
    assert tools_by_name["hold"].description == "Store one memory entry."
    assert tools_by_name["trace"].description == "Update, confirm, restore, or delete a memory entry."
    assert tools_by_name["hold"].inputSchema["required"] == ["content"]
    assert tools_by_name["trace"].inputSchema["required"] == ["bucket_id"]
    assert "anyOf" not in str(tools_by_name["hold"].inputSchema)
    assert "anyOf" not in str(tools_by_name["trace"].inputSchema)
