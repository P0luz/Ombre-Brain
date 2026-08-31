import pytest
from mcp.server.fastmcp.exceptions import ToolError


@pytest.mark.asyncio
async def test_trace_mcp_schema_and_dispatch_include_content_patch(monkeypatch):
    import server

    seen = {}

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "patched"

    monkeypatch.setattr(server._t_trace, "dispatch", fake_dispatch)
    tool = server.mcp._tool_manager.get_tool("trace")
    listed = next(item for item in await server.mcp.list_tools() if item.name == "trace")

    assert {"old_str", "new_str"} <= set(listed.inputSchema["properties"])
    assert listed.inputSchema["additionalProperties"] is False
    assert {"old_str", "new_str"} <= set(tool.fn_metadata.arg_model.model_fields)

    output = await tool.run(
        {
            "bucket_id": "bucket-1",
            "old_str": "旧正文片段",
            "new_str": "",
        }
    )

    assert output == "patched"
    assert seen["bucket_id"] == "bucket-1"
    assert seen["old_str"] == "旧正文片段"
    assert seen["new_str"] == ""


@pytest.mark.asyncio
async def test_trace_mcp_schema_dispatch_and_log_include_protected(monkeypatch):
    import server

    seen = {}
    entries = []

    async def fake_dispatch(**kwargs):
        seen.update(kwargs)
        return "protected"

    monkeypatch.setattr(server._t_trace, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        server,
        "_log_op_entry",
        lambda op, args: entries.append((op, args)),
    )
    monkeypatch.setattr(server, "_log_op_ok", lambda *_args: None)
    tool = server.mcp._tool_manager.get_tool("trace")
    listed = next(item for item in await server.mcp.list_tools() if item.name == "trace")

    assert "protected" in listed.inputSchema["properties"]
    assert "protected" in tool.fn_metadata.arg_model.model_fields

    output = await tool.run({"bucket_id": "bucket-1", "protected": 1})

    assert output == "protected"
    assert seen["protected"] == 1
    assert len(entries) == 1
    assert entries[0][0] == "trace"
    assert entries[0][1]["protected"] == 1


@pytest.mark.asyncio
async def test_trace_mcp_rejects_unknown_patch_argument_instead_of_ignoring_it():
    import server

    tool = server.mcp._tool_manager.get_tool("trace")

    with pytest.raises(ToolError, match="extra_forbidden"):
        await tool.run(
            {
                "bucket_id": "bucket-1",
                "old_string": "拼错的参数不应静默退化",
            }
        )


@pytest.mark.asyncio
async def test_trace_mcp_operation_log_records_patch_lengths_not_bodies(monkeypatch):
    import server

    entries = []
    old_secret = "old fragment must not enter logs"
    new_secret = "new fragment must not enter logs"

    async def fake_dispatch(**_kwargs):
        return "patched"

    monkeypatch.setattr(server._t_trace, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        server,
        "_log_op_entry",
        lambda op, args: entries.append((op, args)),
    )
    monkeypatch.setattr(server, "_log_op_ok", lambda *_args: None)
    tool = server.mcp._tool_manager.get_tool("trace")

    output = await tool.run(
        {
            "bucket_id": "bucket-1",
            "old_str": old_secret,
            "new_str": new_secret,
        }
    )

    assert output == "patched"
    assert len(entries) == 1
    assert entries[0][0] == "trace"
    assert entries[0][1]["old_str_len"] == len(old_secret)
    assert entries[0][1]["new_str_len"] == len(new_secret)
    assert old_secret not in repr(entries)
    assert new_secret not in repr(entries)
