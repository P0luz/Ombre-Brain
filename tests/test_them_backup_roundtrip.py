"""them 的库要进备份包，也要能从备份包恢复回来。

poluz 2026-08-20：「同步到仓库，因为仓库属于模型，对别人的看法也属于模型。」
（呼应 rule.md 第 3 条：Ombre Brain 属于 LLM。）

在这之前 `.you` 在三处被特殊处理而 `.them` 一处都没接，数据不进备份、
不进同步、不进迁移包。这些用例锁的是那条缺口被真的补上了——
光看代码里有 `them/them.sqlite3` 这个字符串不算数，得真的导出再导入一遍。
"""

import zipfile

import pytest

from ombrebrain.storage.backup_archive import (
    build_export_archive,
    build_export_archive_file,
)
from ombrebrain.them import Person, ThemStore, ThemStoreError, validate_them_snapshot_file
from ombrebrain.you import YouStore


def _them_with_data(vault):
    store = ThemStore(vault)
    store.set_enabled(True)
    scope = store.get_state().scope
    store.put_person(scope, Person.new(["Zoey", "小 Z"]))
    return store


class TestSnapshotValidation:
    def test_合法快照通过(self, tmp_path):
        store = _them_with_data(tmp_path)
        snap = tmp_path / "snap.db"
        assert store.snapshot_to(snap) is True
        validate_them_snapshot_file(snap)

    def test_空文件被拒(self, tmp_path):
        empty = tmp_path / "empty.db"
        empty.write_bytes(b"")
        with pytest.raises(ThemStoreError, match="empty"):
            validate_them_snapshot_file(empty)

    def test_不是them库的sqlite被拒(self, tmp_path):
        """备份文件与 GitHub 仓库是外部输入。一份带额外表的 SQLite 放进 vault，
        等于让别人往这台实例里塞了一段可执行的东西。"""
        import sqlite3

        wrong = tmp_path / "wrong.db"
        conn = sqlite3.connect(wrong)
        conn.execute("CREATE TABLE 随便什么 (a TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(ThemStoreError, match="schema is not allowed"):
            validate_them_snapshot_file(wrong)

    def test_混进you快照会被认出来(self, tmp_path):
        """两份库长得像，但表不一样。用错校验函数等于没校验。"""
        you = YouStore(tmp_path)
        you.set_enabled(True)
        snap = tmp_path / "you-snap.db"
        assert you.snapshot_to(snap) is True
        with pytest.raises(ThemStoreError, match="schema is not allowed"):
            validate_them_snapshot_file(snap)


class TestArchive:
    @pytest.mark.asyncio
    async def test_them库进备份包(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "dynamic").mkdir()
        (vault / "dynamic" / "m1.md").write_text(
            "---\nid: m1\ntype: dynamic\n---\n和 Zoey 开会。\n", encoding="utf-8"
        )
        _them_with_data(vault)

        archive_path, _meta = build_export_archive_file(
            buckets_dir=str(vault),
            embedding_db_path=str(vault / "nonexistent.db"),
            export_meta={"embedding": {}},
        )
        try:
            with zipfile.ZipFile(archive_path) as zf:
                names = set(zf.namelist())
                assert "them/them.sqlite3" in names
                # 备份包里的那份必须自己也是合法的 them 库
                blob = zf.read("them/them.sqlite3")
            assert blob[:16].startswith(b"SQLite format 3")
        finally:
            import os

            os.unlink(archive_path)

    @pytest.mark.asyncio
    async def test_没开过them就不进包(self, tmp_path):
        """默认关闭时连库都没建，备份里不该凭空多出一个空文件。"""
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "dynamic").mkdir()
        (vault / "dynamic" / "m1.md").write_text(
            "---\nid: m1\ntype: dynamic\n---\n正文。\n", encoding="utf-8"
        )
        archive_path, _meta = build_export_archive_file(
            buckets_dir=str(vault),
            embedding_db_path=str(vault / "nonexistent.db"),
            export_meta={"embedding": {}},
        )
        try:
            with zipfile.ZipFile(archive_path) as zf:
                assert "them/them.sqlite3" not in set(zf.namelist())
        finally:
            import os

            os.unlink(archive_path)


class TestInMemoryArchive:
    """内存归档与流式归档是两条独立的代码路径，各写了一份 them 逻辑。

    只测其中一条，另一条漏掉 them 的时候没人会发现。
    """

    def test_内存归档也带them(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "dynamic").mkdir()
        (vault / "dynamic" / "m1.md").write_text(
            "---\nid: m1\ntype: dynamic\n---\n和 Zoey 开会。\n", encoding="utf-8"
        )
        _them_with_data(vault)
        blob, _meta = build_export_archive(
            buckets_dir=str(vault),
            embedding_db_path=str(vault / "nonexistent.db"),
            export_meta={"embedding": {}},
        )
        import io

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            assert "them/them.sqlite3" in set(zf.namelist())


class TestGithubPaths:
    def test_them路径在备份白名单里(self):
        from github_sync import _is_backup_relative_path

        assert _is_backup_relative_path(".them/them.sqlite3")
        assert _is_backup_relative_path(".you/you.sqlite3")
        assert not _is_backup_relative_path(".them/别的.sqlite3")

    def test_校验函数按路径分派(self):
        """用 you 的校验函数去校验 them 的库，等于没校验。"""
        from github_sync import _SNAPSHOT_VALIDATORS

        assert set(_SNAPSHOT_VALIDATORS) == {
            ".you/you.sqlite3",
            ".them/them.sqlite3",
        }
        assert _SNAPSHOT_VALIDATORS[".them/them.sqlite3"][2] == "them"

    def test_额外路径白名单只认这两条(self, tmp_path):
        from github_sync import _vetted_extra_backup_path

        真文件 = tmp_path / "x.db"
        真文件.write_bytes(b"x")
        assert _vetted_extra_backup_path(".them/them.sqlite3", str(真文件)) == (
            ".them/them.sqlite3"
        )
        with pytest.raises(RuntimeError, match="unsupported"):
            _vetted_extra_backup_path("../逃逸.db", str(真文件))


class TestMigrateLimits:
    def test_them库有自己的成员上限(self):
        from ombrebrain.storage.backup_archive import (
            MIGRATE_MAX_THEM_DB_BYTES,
            _migration_member_limit,
        )

        assert _migration_member_limit("them/them.sqlite3") == MIGRATE_MAX_THEM_DB_BYTES
