"""子进程崩溃时的 traceback 必须是 UTF-8，否则会拖垮整场测试。

Windows 上子进程按控制台代码页（这台机器是 GBK）写 stderr。用户名或路径里有
中文就会产生 UTF-8 解不开的字节；pytest 的捕获层按 UTF-8 解，那个
IncrementalDecoder 卡住之后还会被继续复用——此后每个测试的 setup 和 teardown
各报一次 error，整场剩下的全灭。实测抓到过 `1 failed, 332 passed, 4911 errors`。
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHILD = "raise RuntimeError('路径里有中文：C:\\\\Users\\\\孙立人\\\\记忆')\n"


def test_conftest_pins_child_io_encoding():
    assert os.environ.get("PYTHONIOENCODING", "").lower().startswith("utf-8")


def test_a_crashing_child_writes_decodable_stderr():
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD], capture_output=True
    )

    assert proc.returncode != 0
    # 解不开就说明子进程没继承到 UTF-8——那正是会卡死捕获层的那种字节。
    text = proc.stderr.decode("utf-8")
    assert "路径里有中文" in text
