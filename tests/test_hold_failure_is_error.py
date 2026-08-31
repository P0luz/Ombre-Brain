"""hold 的参数校验失败必须以 MCP 错误呈现，不能报成功。

背景（真机复现）：

    hold(content="...", feel=True, domain="工作")
      isError = False                       ← MCP 层说「成功」
      文本   = "feel 的 domain 固定为 feel，不能显式覆盖。"
      落库   = 0 个桶

调用方（模型自己）只看 isError，就会以为这条记忆存下去了，
接着往下走。等它下次想翻出来的时候，那条记忆从来没存在过。

这类失败的共同点是**一个桶都没建**：参数不合法，函数在任何写入
之前就返回了。用 `return "错误说明"` 表达这种失败，等于把失败
伪装成一次正常返回。

本文件锁定的契约：hold 遇到这九种参数问题时抛异常，MCP 侧因此
得到 isError=True，且错误正文里带得上原因（模型要靠它自我纠正）。

不包含降级提示：`👣 Footprint：暂时无法读取` 这类是「主体成功了，
附带的东西没读到」，isError=False 才是对的，不在这里拦。
"""

import pytest
from mcp.server.fastmcp.exceptions import ToolError


# 每个 case = (说明, 入参, 错误正文里必须出现的片段)
真失败用例 = [
    (
        "feel 不接受显式 domain",
        {"content": "今天很累", "feel": True, "domain": "工作", "source_bucket": "abc123"},
        "domain",
    ),
    (
        "测试数据不能是 feel",
        {"content": "测试内容", "feel": True, "test_data": True, "source_bucket": "abc123"},
        "测试数据",
    ),
    (
        "测试数据不能是 pinned",
        {"content": "测试内容", "pinned": True, "test_data": True},
        "测试数据",
    ),
    (
        "内容为空存不进去",
        {"content": "   "},
        "内容为空",
    ),
    (
        "feel 必须指向一条原始记忆",
        {"content": "今天很累", "feel": True},
        "source_bucket",
    ),
    (
        "source_ranges 不能脱离 source_content 单独出现",
        {"content": "正文", "source_ranges": [[1, 2]]},
        "source_ranges",
    ),
    (
        "source_ranges 超出原文行数",
        {"content": "正文", "source_content": "只有一行", "source_ranges": [[1, 99]]},
        "超出原文总行数",
    ),
    (
        "引语格式非法",
        {"content": "正文", "quotes": [{"没有正文字段": 1}]},
        "引语",
    ),
]


@pytest.fixture
def hold_tool(monkeypatch):
    """拿到 MCP 注册的 hold，并掐掉它对真实记忆库的依赖。

    这些用例全部在写入之前就该失败，所以底下的 store_* 一律换成
    「被调到就算测试失败」，顺便证明确实没有落库。
    """
    import server
    from tools import _runtime as rt
    import tools.hold as hold_mod

    class 空衰减引擎:
        async def ensure_started(self):
            return None

    async def 不该被调用(**kwargs):
        raise AssertionError("参数校验失败后不应该再走到写入")

    monkeypatch.setattr(rt, "decay_engine", 空衰减引擎(), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(hold_mod, "store_feel", 不该被调用)
    monkeypatch.setattr(hold_mod, "store_pinned", 不该被调用)
    monkeypatch.setattr(hold_mod, "store_core", 不该被调用)
    return server.mcp._tool_manager.get_tool("hold")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "说明, 入参, 关键词",
    真失败用例,
    ids=[case[0] for case in 真失败用例],
)
async def test_参数校验失败必须让mcp拿到错误(hold_tool, 说明, 入参, 关键词):
    with pytest.raises(ToolError) as excinfo:
        await hold_tool.run(dict(入参))
    assert 关键词 in str(excinfo.value), f"{说明}：错误正文没说清原因，模型无从纠正"


@pytest.mark.asyncio
async def test_标题非法也是错误而不是成功(hold_tool):
    with pytest.raises(ToolError):
        await hold_tool.run({"content": "正文", "title": "x" * 5000})


@pytest.mark.asyncio
async def test_元数据超限也是错误而不是成功(hold_tool):
    with pytest.raises(ToolError):
        await hold_tool.run({"content": "正文", "tags": "标签," * 40000})


@pytest.mark.asyncio
async def test_正文超限也是错误而不是成功(hold_tool):
    with pytest.raises(ToolError):
        await hold_tool.run({"content": "字" * 5_000_000})


@pytest.mark.asyncio
async def test_降级提示不受影响仍算成功(hold_tool, monkeypatch):
    """反面：写入成功、只是附带信息缺失，必须还是 isError=False。

    没有这一条，上面那些测试可以靠「hold 一律抛异常」作弊通过。
    """
    import tools.hold as hold_mod

    async def 成功但附带提示(**kwargs):
        return "已存入记忆 abc123。\n👣 Footprint：暂时无法读取"

    monkeypatch.setattr(hold_mod, "store_core", 成功但附带提示)
    out = await hold_tool.run({"content": "一条正常的记忆"})
    assert "已存入记忆" in str(out)
