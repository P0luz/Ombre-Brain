"""Gemini 的 functionDeclaration 要求 Schema.type 必填（v1beta discovery 文档
里那一条写的是 "Required. Data type."）。`Optional[X]` 生成的 anyOf 那一层没有
它，而 Gemini 校验到第一个不合格的就整批工具被拒——所以这不是某一个工具的事，
任何一个属性漏了 type，整个实例在 Gemini 上都用不了。

这份测试走 list_tools（真正发出去的那条路），递归查每一层。漏掉一个压平调用
点、或者将来新加一个 Optional 参数，都会在这里红。
"""

import pytest

_NL = chr(10)


def _offenders(name: str, schema, path: str = "") -> list[str]:
    """返回所有缺 type 的位置。"""
    if not isinstance(schema, dict):
        return []
    bad: list[str] = []
    for key, prop in (schema.get("properties") or {}).items():
        here = f"{path}.{key}" if path else key
        if not isinstance(prop, dict):
            continue
        if "type" not in prop:
            bad.append(f"{name}.{here}")
            continue
        if prop["type"] == "array":
            items = prop.get("items")
            if not isinstance(items, dict) or "type" not in items:
                bad.append(f"{name}.{here}[]")
            else:
                bad += _offenders(name, items, f"{here}[]")
        bad += _offenders(name, prop, here)
    return bad


@pytest.mark.asyncio
async def test_every_advertised_property_declares_a_type():
    import server

    bad: list[str] = []
    for tool in await server.mcp.list_tools():
        bad += _offenders(tool.name, tool.inputSchema)

    assert not bad, (
        "这些属性没有 type，Gemini 会拒绝**整批**工具（不只是它们所在的那个）："
        + _NL + "  " + (_NL + "  ").join(bad) + _NL
        + "给它们在 ombrebrain/protocol/strict_schema.py 里补一条压平规则。"
    )


@pytest.mark.asyncio
async def test_hardening_does_not_touch_the_validator():
    """对外 schema 压平了，运行时照样收 null。

    这是整个做法成立的前提：`Optional` 是承重的（breath 里有一整段 Null-safe
    coercion，说明真的有客户端在传 null）。压平如果连校验器一起改了，就是用一个
    兼容性问题换另一个。
    """
    import server

    listed = next(
        t for t in await server.mcp.list_tools() if t.name == "breath_advanced"
    )
    assert listed.inputSchema["properties"]["domain"]["type"] == "string"

    model = server.mcp._tool_manager.get_tool("breath_advanced").fn_metadata.arg_model
    assert model(domain=None).domain is None


@pytest.mark.asyncio
async def test_breath_stays_parameter_free():
    """breath 是故意 0 参数的（claude.ai 按需加载会跳过参数复杂的工具）。

    压平是遍历所有工具的，必须不能给它凭空加出属性来。
    """
    import server

    listed = next(t for t in await server.mcp.list_tools() if t.name == "breath")
    assert listed.inputSchema["properties"] == {}


@pytest.mark.asyncio
async def test_dynamic_you_them_tools_are_hardened_too():
    """You / Them 是动态挂载的，走不到 server.py 里那一次性的压平。

    它们默认关着，所以不会出现在 list_tools 里——上面那份全量检查看不到它们，
    这条单独把门打开验一次。少了各自门里那句 harden_tool，开着 You/Them 的实例
    在 Gemini 上会整批工具被拒，而全量检查一路绿灯。
    """
    import server

    for gate, name in ((server.you_tool_gate, "You"), (server.them_tool_gate, "Them")):
        gate.sync(True)
        try:
            tool = server.mcp._tool_manager.get_tool(name)
            assert tool is not None
            assert not _offenders(name, tool.parameters)
        finally:
            gate.sync(False)
