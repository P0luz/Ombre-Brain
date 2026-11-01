"""硬链接不可用时，「创建」不能退化成「覆盖」。

真机反馈：termux 上 `hold` 报 [OB-E004]，用户自己打了个补丁——`os.link` 不可用
时改用 `os.replace`。那样能跑，但把「已存在就失败」悄悄换成了「已存在就覆盖」，
而这个函数存在的全部意义就是不许发生后者。
"""

from __future__ import annotations

import os

import pytest

from bucket_manager import _atomic_create_text
from utils import publish_new_file


@pytest.fixture
def no_hardlinks(monkeypatch):
    """把这台机器伪装成不支持硬链接的文件系统。"""
    def _refuse(_source, _target):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", _refuse)


@pytest.fixture
def no_link_attribute(monkeypatch):
    """连 os.link 这个属性都没有的运行时。"""
    monkeypatch.delattr(os, "link", raising=False)


def test_create_still_works_without_hardlinks(tmp_path, no_hardlinks):
    target = tmp_path / "memory.md"

    _atomic_create_text(str(target), "正文\n")

    assert target.read_text(encoding="utf-8") == "正文\n"


def test_create_still_works_when_os_link_is_missing(tmp_path, no_link_attribute):
    target = tmp_path / "memory.md"

    _atomic_create_text(str(target), "正文\n")

    assert target.read_text(encoding="utf-8") == "正文\n"


def test_existing_file_is_still_refused_without_hardlinks(tmp_path, no_hardlinks):
    """兜底路径必须仍然拒绝覆盖——退回 os.replace 就是在这里出事。"""
    target = tmp_path / "memory.md"
    target.write_text("先来的\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _atomic_create_text(str(target), "后来的\n")

    assert target.read_text(encoding="utf-8") == "先来的\n"


def test_existing_file_is_refused_on_the_hardlink_path(tmp_path):
    """有硬链接时的老行为一个字没变。"""
    target = tmp_path / "memory.md"
    target.write_text("先来的\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _atomic_create_text(str(target), "后来的\n")

    assert target.read_text(encoding="utf-8") == "先来的\n"


def test_no_temp_file_is_left_behind(tmp_path, no_hardlinks):
    target = tmp_path / "memory.md"

    _atomic_create_text(str(target), "正文\n")

    assert [p.name for p in tmp_path.iterdir()] == ["memory.md"]


def test_publish_new_file_refuses_an_existing_target_directly(tmp_path, no_hardlinks):
    source = tmp_path / "staged.tmp"
    source.write_text("新的\n", encoding="utf-8")
    target = tmp_path / "taken.md"
    target.write_text("旧的\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        publish_new_file(str(source), str(target), "新的\n")

    assert target.read_text(encoding="utf-8") == "旧的\n"
