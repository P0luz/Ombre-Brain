"""Docker 集成套件里那张工具参数表，本地也要能校验。

那份表是工具形状的第二处真源，而整个模块在没有 MCP_URL 时被 pytestmark 跳过
——于是给某个工具加一个参数时，本地全套 2900 条全绿，推上去 CI 才红。
3.6.13 给 breath_advanced 补 quotes 时就这么来回了一趟。

这里只读那张表、跟 list_tools 对，不需要 Docker，也不需要起服务。
"""

import ast
import pathlib

import pytest

_TABLE = pathlib.Path(__file__).with_name("test_mcp_tools_docker_integration.py")


def _constants() -> dict:
    """从 Docker 套件里取出常量，不 import 它——import 会连带跑它的模块级跳过。"""
    out = {}
    for node in ast.parse(_TABLE.read_text(encoding="utf-8", errors="replace")).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.startswith("EXPECTED"):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return out


@pytest.mark.asyncio
async def test_docker_tool_table_matches_the_live_schema():
    import server

    constants = _constants()
    expected = constants["EXPECTED_TOOL_PROPERTIES"]
    live = {
        tool.name: set(tool.inputSchema.get("properties") or {})
        for tool in await server.mcp.list_tools()
    }

    drift = []
    for name, want in sorted(expected.items()):
        got = live.get(name)
        if got is None:
            drift.append(name + ": 表里有，实际没有这个工具")
        elif got != set(want):
            drift.append(
                name
                + ": 实际多了 " + repr(sorted(got - set(want)))
                + "，少了 " + repr(sorted(set(want) - got))
            )

    assert not drift, (
        "工具参数与 Docker 套件里的期望表不符（那张表只在 CI 跑，所以本地不改它"
        "就会推上去才红）："
        + chr(10) + "  " + (chr(10) + "  ").join(drift)
    )


@pytest.mark.asyncio
async def test_docker_tool_roster_matches_the_live_schema():
    import server

    constants = _constants()
    live = {tool.name for tool in await server.mcp.list_tools()}
    for key in ("EXPECTED_TOOLS", "EXPECTED_TOOL_ORDER"):
        if key in constants:
            assert live == set(constants[key]), key + " 与实际注册的工具不符"
