"""桶文件的路径判定与信件标记识别 —— 纯函数，不碰实例状态。

从 `bucket_manager.BucketManager` 里搬出来的四个静态方法。搬出来有两个理由：

1. 那个类已经太大，而这四个既不读也不写实例状态。
2. `path_is_within` 与两个信件标记判定原本是私有方法，却被 `tools/_common.py`
   跨模块调用（`bucket_mgr._path_is_within(...)`）。一个被外部依赖的私有方法，
   要么该是公开的，要么该住在别处——这里选后者。

`path_is_within` 是安全函数：它判断的是「这个文件是不是真的在那个目录里」，
用来挡符号链接与 `..` 逃逸。改它之前先想清楚 realpath 那一步为什么在。
"""

from __future__ import annotations

import os
from typing import Any


def same_path(left: str, right: str) -> bool:
    """两个路径是否指向同一个位置（大小写按平台规则归一）。"""
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def path_is_within(file_path: str, directory: str) -> bool:
    """按解析后的**真实**路径判断文件是否位于指定目录内。

    用 realpath 而不是 abspath：符号链接指向目录外时，abspath 看不出来，
    而这个函数的全部意义就是挡住那一种。commonpath 在跨盘符时抛 ValueError，
    那种情况下答案就是「不在」。
    """
    normalized_path = os.path.normcase(os.path.realpath(file_path))
    normalized_directory = os.path.normcase(os.path.realpath(directory))
    try:
        return (
            os.path.commonpath((normalized_path, normalized_directory))
            == normalized_directory
        )
    except ValueError:
        return False


def has_strong_letter_marker(post: Any) -> bool:
    """这条记忆有没有写在持久化里的强来源标记。

    强标记 = `source_tool == "letter"` 或带 `__letter__` tag。
    `domain=letter` 不算——那个太容易被普通写入误带上，见下面那个函数。
    """
    if str(post.get("source_tool") or "").strip().casefold() == "letter":
        return True
    tags = post.get("tags") or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",")]
    if not isinstance(tags, (list, tuple, set)):
        return False
    return any(str(tag).strip().casefold() == "__letter__" for tag in tags)


def has_ambiguous_letter_marker(post: Any) -> bool:
    """只靠 `domain=letter` 认出来的历史桶——需要人工判断，不能当强标记用。"""
    domains = post.get("domain") or []
    if isinstance(domains, str):
        domains = [domains]
    if not isinstance(domains, (list, tuple, set)):
        return False
    return any(str(domain).strip().casefold() == "letter" for domain in domains)
