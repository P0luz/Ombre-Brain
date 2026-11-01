from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "src" / "server.py"

# 参数名 -> 豁免理由
EXEMPT = {
    ("anchor", "bucket_id"): "工具只有这一个参数，正文即说明",
    ("release", "bucket_id"): "同上",
    ("pulse", "include_archive"): "正文有「include_archive=True 同时返回归档区」",
    ("plan", "content"): "正文通篇在讲写什么内容",
    ("letter_write", "content"): "同上",
    ("breath_search", "query"): "正文通篇在讲检索什么",
    ("hold", "test_data"): "测试数据标记，不是给模型的能力",
    ("grow", "test_data"): "同上",
}


def _tools() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("mcp.tool" in ast.unparse(d) for d in node.decorator_list)
    ]


def test_registered_tool_count_is_stable():
    assert len(_tools()) == 16


@pytest.mark.parametrize("tool", _tools(), ids=lambda t: t.name)
def test_every_parameter_is_mentioned_in_the_docstring(tool):
    doc = ast.get_docstring(tool) or ""
    assert doc.strip(), f"{tool.name} 没有 docstring"

    missing = [
        arg.arg
        for arg in tool.args.args
        if (tool.name, arg.arg) not in EXEMPT
        and not re.search(rf"\b{re.escape(arg.arg)}\b", doc)
    ]

    assert not missing, (
        f"{tool.name} 的这些参数在 docstring 里一个字都没提：{'、'.join(missing)}。"
        "模型能传，却无从知道什么时候该传。要么补进 docstring，"
        "要么在 EXEMPT 里写明为什么不需要。"
    )
