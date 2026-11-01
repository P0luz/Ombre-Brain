"""进程内存读数：RSS、容器上限、以及 GC 里对象的分布。

## 为什么要有这个

上游报了 Render 512MB 上 OOM，而报告里只有「内存持续增长」——没有 RSS 曲线、
没有上限确认、没有哪一类对象在涨。照着这种描述改代码就是猜：本仓库里几个
最可疑的地方（语义检索、模块级缓存、metrics）查下来都是有界的
（检索走 fetchmany(32) + O(top_k) 堆），再往下只能靠运气。

所以先把读数装上。下一次同样的报告能带上 `/api/system/diagnostics` 的
memory 段，才谈得上定位。

## 取数方式

- RSS 走 `/proc/self/status` 的 VmRSS：Linux 上免费且准确，容器里也是真值。
  psutil 不是本项目的依赖，为一个诊断字段引入它不值得。
- 容器上限走 cgroup。v2 是 `memory.max`，v1 是 `memory.limit_in_bytes`；
  没有限制时 v2 写 "max"、v1 写一个极大的数，两种都当作「没有上限」。
  **这一条很重要**：容器里 `/proc/meminfo` 报的是宿主机的内存，照着它算
  百分比会得出「用了 2%」而进程正在被 OOM killer 杀掉。
- 对象分布走 `gc.get_objects()` 的类型计数，只取前几名。它本身有开销，
  所以默认不算，要显式要（`include_objects=True`）。

Windows / macOS 上 `/proc` 不存在，全部返回 available=False，不抛错——
诊断页少一块，不该让整页 500。
"""

from __future__ import annotations

import collections
import gc
import os
from typing import Any

_STATUS = "/proc/self/status"
_CGROUP_V2_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_MAX = "/sys/fs/cgroup/memory/memory.limit_in_bytes"

# cgroup v1 「没有限制」写的是一个接近 2^63 的数。超过这个量级一律当无限制，
# 不去精确匹配那个魔数——不同内核写出来的值并不一致。
_NO_LIMIT_ABOVE = 1 << 50


def _read_first_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except (OSError, ValueError):
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def rss_bytes() -> int | None:
    """当前常驻内存。拿不到就 None，不猜。"""
    try:
        with open(_STATUS, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def memory_limit_bytes() -> int | None:
    """容器的内存上限。没有限制、或读不到，都返回 None。

    不回退到宿主机内存：容器里那个数跟进程会不会被杀没有关系。
    """
    for path in (_CGROUP_V2_MAX, _CGROUP_V1_MAX):
        value = _read_first_int(path)
        if value is not None and 0 < value < _NO_LIMIT_ABOVE:
            return value
    return None


def top_object_types(limit: int = 12) -> list[dict[str, Any]]:
    """GC 里对象数量最多的几类。开销不小，只在显式要的时候算。"""
    counter: collections.Counter = collections.Counter()
    for obj in gc.get_objects():
        counter[type(obj).__name__] += 1
    return [{"type": name, "count": count} for name, count in counter.most_common(limit)]


def snapshot(*, include_objects: bool = False) -> dict[str, Any]:
    """一份可以直接贴进 issue 的内存读数。"""
    rss = rss_bytes()
    if rss is None:
        return {
            "available": False,
            "reason": "这个平台没有 /proc/self/status（Windows / macOS）",
        }
    limit = memory_limit_bytes()
    report: dict[str, Any] = {
        "available": True,
        "rss_bytes": rss,
        "rss_mb": round(rss / 1048576, 1),
        "limit_bytes": limit,
        "limit_mb": round(limit / 1048576, 1) if limit else None,
        "used_percent": round(rss / limit * 100, 1) if limit else None,
        "pid": os.getpid(),
        "gc_counts": list(gc.get_count()),
        "gc_tracked_objects": len(gc.get_objects()),
    }
    if include_objects:
        report["top_object_types"] = top_object_types()
    return report
