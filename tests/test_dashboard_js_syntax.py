"""dashboard.html 里 7000 多行内联 JS 至少得能被解析。

既有的前端契约测试全是字符串匹配（「这个函数在不在」「这个分支在不在」），
一个语法错误它们一条都不会红——页面照样发出去，然后整个 Dashboard 白屏。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_HTML = Path(__file__).resolve().parent.parent / "frontend" / "dashboard.html"
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def _blocks() -> list[str]:
    return _INLINE_SCRIPT.findall(_HTML.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_inline_dashboard_scripts_parse():
    blocks = _blocks()
    assert blocks, "没找到内联 script，正则该跟着模板一起改"

    for index, source in enumerate(blocks):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                f.write(source)
            result = subprocess.run(
                ["node", "--check", path], capture_output=True, text=True
            )
        finally:
            os.unlink(path)
        assert result.returncode == 0, (
            f"第 {index} 个内联 script 语法错误：\n{result.stderr}"
        )
