"""桶变动不许再有「没人订阅的广播」。

3.4.x 拆掉 You 的自动派生流水线时，只删了订阅方：`bucket_manager` 里的
`bucket_change_observers` 列表、`attach_bucket_change_observer()` 和
`_notify_bucket_change()` 原封不动留着，5 个 CRUD 路径每次都调一遍，
而列表永远是空的、函数第一行就 return。

空转一整个大版本，真正的代价不是那点开销——是读代码的人会以为
桶变动有钩子可挂，照着它去接自己的东西，然后发现永远收不到事件。
3.5.0 整套删除。

这条测试守的是「别再长回来」：要么有真实订阅者，要么就别留广播端。
"""

import re
from pathlib import Path


_SRC = Path(__file__).parents[1] / "src"


def test_观察者机制没有复活():
    残留 = []
    for 文件 in _SRC.rglob("*.py"):
        if "__pycache__" in str(文件):
            continue
        for i, 行 in enumerate(文件.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            # docstring 和注释里作为历史提一句是可以的，代码里不行
            剥离 = 行.split("#")[0]
            if re.search(r"(?<!`)\b(bucket_change_observers|attach_bucket_change_observer|_notify_bucket_change)\b", 剥离):
                if 剥离.strip().startswith(("*", '"""', "'''")) or "`" in 行:
                    continue
                残留.append(f"{文件.relative_to(_SRC.parent)}:{i}")
    assert not 残留, (
        f"观察者机制又出现在代码里：{残留}。"
        "要接回来就得先有真实订阅者——只留广播端，等于给人一个永远收不到事件的钩子。"
    )


def test_没有别的空转广播():
    """更一般的形状：`self._notify_xxx(...)` 被调用，却没有任何东西注册进去。

    只查 bucket_manager——它是桶写入的唯一入口，也是上一次出事的地方。
    """
    源 = (_SRC / "bucket_manager.py").read_text(encoding="utf-8")
    广播方法 = set(re.findall(r"def (_notify_\w+)\s*\(", 源))
    无订阅 = []
    for 名 in 广播方法:
        # 广播方法通常遍历一个 self.xxx 列表；那个列表必须有人 append
        主体 = 源.split(f"def {名}(", 1)[1]
        列表们 = set(re.findall(r"self\.(\w*observer\w*|\w*listener\w*|\w*subscriber\w*)", 主体))
        for 列表 in 列表们:
            if f"self.{列表}.append" not in 源:
                无订阅.append(f"{名} 遍历 self.{列表}，但全仓库没人 append")
    assert not 无订阅, 无订阅
