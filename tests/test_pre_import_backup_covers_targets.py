"""导入前备份必须覆盖导入会写的每一类文件。

上层在备份失败时会中止导入，理由写着「为避免覆盖后无法找回记忆」——
也就是说它把这个 zip 当**完整回滚点**用。而原先它只打包 `*.md`：
`_sources/` 下的原文证据、`.you` / `.them` 两个模块库都会被导入覆盖，
却一个都没备份。

一个不完整的回滚点比没有回滚点更危险：人会依着它去做不可逆的操作。
"""

import os
import zipfile

import pytest

from web.github import (
    _pre_import_backup,
    _rollback_from_backup,
    _should_back_up_before_import,
)


# 导入实际会写的四类路径，来源见 github_sync 的安装循环与 _MODULE_SNAPSHOT_PATHS
被导入覆盖的 = [
    "2026/08/bucket_abc.md",
    "_sources/deadbeef.source",
    ".you/you.sqlite3",
    ".them/them.sqlite3",
    ".you/you.sqlite3-wal",
    ".them/them.sqlite3-shm",
]


@pytest.mark.parametrize("相对路径", 被导入覆盖的)
def test_导入会覆盖的都在备份范围里(相对路径):
    assert _should_back_up_before_import(相对路径), (
        f"{相对路径} 会被导入覆盖，却不在备份范围里——"
        "回滚点缺了它，导入失败后这份数据就找不回来了"
    )


@pytest.mark.parametrize("相对路径", [
    ".import_backups/pre_import_x.zip",   # 备份自己，不能套娃
    "embeddings.db",                      # 导入不写它，靠「重算所有向量」恢复
    "notes.txt",
])
def test_导入不碰的不必备份(相对路径):
    assert not _should_back_up_before_import(相对路径)


def test_真打出来的zip里四类文件都在(tmp_path):
    """不只测判定函数，测真跑一遍 zip 里到底有什么。"""
    for 相对路径 in 被导入覆盖的:
        目标 = tmp_path / 相对路径
        目标.parent.mkdir(parents=True, exist_ok=True)
        目标.write_bytes(b"x")
    (tmp_path / "embeddings.db").write_bytes(b"x")

    zip路径 = _pre_import_backup(str(tmp_path))
    assert zip路径, "备份没生成"
    with zipfile.ZipFile(zip路径) as z:
        打包了 = {n.replace(os.sep, "/") for n in z.namelist()}

    for 相对路径 in 被导入覆盖的:
        assert 相对路径 in 打包了, f"{相对路径} 没进备份"
    assert "embeddings.db" not in 打包了


# --- 回滚 ---


def test_失败后能把被覆盖的文件还原回去(tmp_path):
    """导入是一个文件一个文件装的，装到一半失败，前面的已经落盘。

    没有回滚时，本地就停在「一半远端、一半本地」的混合状态，
    而调用方只看到一句「失败」，很容易以为什么都没发生。
    """
    原始 = {
        "2026/08/a.md": b"local-a",
        "_sources/ref1.source": b"local-source",
        ".you/you.sqlite3": b"local-you-db",
    }
    for 相对路径, 内容 in 原始.items():
        目标 = tmp_path / 相对路径
        目标.parent.mkdir(parents=True, exist_ok=True)
        目标.write_bytes(内容)

    备份 = _pre_import_backup(str(tmp_path))
    assert 备份

    # 模拟导入装到一半：前两个被远端内容覆盖，第三个还没轮到
    (tmp_path / "2026/08/a.md").write_bytes(b"REMOTE-a")
    (tmp_path / "_sources/ref1.source").write_bytes(b"REMOTE-source")
    # 导入还新增了一个本地原本没有的文件
    新增 = tmp_path / "2026/08/from_remote.md"
    新增.write_bytes(b"REMOTE-new")

    结果 = _rollback_from_backup(str(tmp_path), 备份)

    assert 结果["ok"], 结果
    for 相对路径, 内容 in 原始.items():
        assert (tmp_path / 相对路径).read_bytes() == 内容, f"{相对路径} 没还原"
    # 新增的不删——删错一条记忆不可逆，宁可留下多余的
    assert 新增.exists()


def test_备份读不开时如实报错而不是假装还原了(tmp_path):
    结果 = _rollback_from_backup(str(tmp_path), str(tmp_path / "不存在.zip"))
    assert 结果["ok"] is False
    assert 结果["restored"] == 0
    assert "备份读不开" in 结果["error"]


def test_备份里的越界路径不会被写出去(tmp_path):
    """备份是本地生成的，但它也可能被人动过手脚。"""
    坏包 = tmp_path / "bad.zip"
    with zipfile.ZipFile(坏包, "w") as z:
        z.writestr("../../escaped.md", "x")
    结果 = _rollback_from_backup(str(tmp_path), str(坏包))
    assert 结果["ok"] is False
    assert not (tmp_path.parent.parent / "escaped.md").exists()
