from __future__ import annotations

import pytest

from errors import ToolInputError


_OVERSIZE_QUERY = "找" * 100_000
_OVERSIZE_META = "领域," * 20_000


@pytest.mark.asyncio
async def test_breath_rejects_oversized_query():
    from tools import breath

    with pytest.raises(ToolInputError) as exc:
        await breath.dispatch(query=_OVERSIZE_QUERY)
    assert "查询过大" in str(exc.value)


@pytest.mark.asyncio
async def test_breath_rejects_oversized_metadata():
    from tools import breath

    with pytest.raises(ToolInputError) as exc:
        await breath.dispatch(query="正常", domain=_OVERSIZE_META)
    assert "元数据过大" in str(exc.value)


@pytest.mark.asyncio
async def test_letter_read_rejects_oversized_query():
    from tools.plan import core

    with pytest.raises(ToolInputError) as exc:
        await core.letter_read(query=_OVERSIZE_QUERY)
    assert "查询过大" in str(exc.value)


@pytest.mark.asyncio
async def test_letter_read_rejects_oversized_metadata():
    from tools.plan import core

    with pytest.raises(ToolInputError) as exc:
        await core.letter_read(query="正常", author=_OVERSIZE_META)
    assert "元数据过大" in str(exc.value)


@pytest.mark.asyncio
async def test_i_rejects_oversized_metadata():
    from tools.i import core

    with pytest.raises(ToolInputError) as exc:
        await core.i_core(content="我觉得测试", aspect="x" * 40_000)
    assert "元数据过大" in str(exc.value)


def test_no_tool_returns_a_size_error_as_normal_output():
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "tools"
    pattern = re.compile(r"^\s*return\s+(query_err|metadata_err|size_err|content_err)\b", re.M)

    offenders = []
    for path in sorted(src.rglob("*.py")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(src.parent.parent)}:{line}")

    assert not offenders, (
        "这些地方把尺寸检查的失败 return 出去了，在 MCP 侧会变成 isError=False，"
        f"模型会当成成功：{'、'.join(offenders)}。改成 raise ToolInputError(...)。"
    )
