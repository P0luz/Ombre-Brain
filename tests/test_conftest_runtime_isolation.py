"""conftest 的 `_restore_tool_runtime` 真的在还原 tools/_runtime 的全局装配。

直接驱动 fixture 的生成器，不依赖测试执行顺序——这条不变量本身就是用来兜住
「顺序变了才暴露」那类 bug 的，用顺序去测它没有意义。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools import _runtime as rt


def _conftest_module():
    """拿到 pytest 已经加载的那个 conftest，不重新执行它。

    conftest.py 有模块级副作用（建临时 vault、写环境变量），按路径再 import
    一次会把它们做第二遍。
    """
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if (
            path
            and Path(path).name == "conftest.py"
            and hasattr(module, "_restore_tool_runtime")
        ):
            return module
    raise AssertionError("找不到定义 _restore_tool_runtime 的 conftest")


def test_direct_assignment_to_runtime_does_not_leak():
    generator = _conftest_module()._restore_tool_runtime.__wrapped__()
    next(generator)

    original = rt.embedding_engine
    sentinel = object()
    # 二十多个测试文件就是这么写的：直接赋值，不走 monkeypatch。
    rt.embedding_engine = sentinel
    rt.freshly_invented_slot = sentinel

    with pytest.raises(StopIteration):
        next(generator)

    assert rt.embedding_engine is original
    assert not hasattr(rt, "freshly_invented_slot")
