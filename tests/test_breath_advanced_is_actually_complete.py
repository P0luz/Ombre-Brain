"""breath_advanced 自称「完整参数版」，那它就必须真的是完整的。

真机报错：

    Error executing tool breath_advanced: 1 validation error for
    breath_advancedArguments
    Extra inputs are not permitted [type=extra_forbidden, input_value=True,
    input_type=bool]

那个 bool 是 `quotes`。它在 dispatch 里有、在 breath_search 里有，唯独
breath_advanced 漏了；而 breath_search 的文档串又写着「需要 tags/
importance_min/valence/arousal/max_tokens/catalog 等更多过滤维度用
breath_advanced(...)」——于是想同时要引语和标签的模型被指到一个接不住的工具上。

extra=forbid 是对的（拼错的参数必须响亮地失败，不能静默降级成另一种检索），
所以修的是补齐参数，不是放松校验。
"""

import inspect

import pytest


def _params(fn) -> set:
    return set(inspect.signature(fn).parameters)


def test_breath_advanced_accepts_everything_breath_search_does():
    import server

    advanced = _params(server.breath_advanced)
    search = _params(server.breath_search)

    missing = sorted(search - advanced)
    assert not missing, (
        "breath_advanced 自称完整参数版，却接不住 breath_search 的这些参数："
        + repr(missing)
        + "。模型照文档串从 search 转到 advanced 时会撞 extra_forbidden。"
    )


def test_breath_advanced_forwards_everything_dispatch_takes():
    """签名接住了还不够，得真的透传下去——漏传是静默失效，比报错更难发现。"""
    import server
    from tools import breath as t_breath

    advanced = _params(server.breath_advanced)
    dispatch = _params(t_breath.dispatch)

    missing = sorted(dispatch - advanced)
    assert not missing, "dispatch 支持但 breath_advanced 没开出来：" + repr(missing)

    source = inspect.getsource(server.breath_advanced)
    not_forwarded = [p for p in dispatch if (p + "=") not in source]
    assert not not_forwarded, (
        "这些参数在签名里，却没有透传给 dispatch：" + repr(sorted(not_forwarded))
    )


@pytest.mark.asyncio
async def test_quotes_is_advertised_on_both_search_tools():
    """schema 那一侧也要有——模型看的是工具声明，不是 Python 签名。"""
    import server

    listed = {t.name: t.inputSchema for t in await server.mcp.list_tools()}
    for name in ("breath_search", "breath_advanced"):
        prop = listed[name]["properties"].get("quotes")
        assert prop is not None, name + " 的声明里没有 quotes"
        assert prop["type"] == "boolean"
