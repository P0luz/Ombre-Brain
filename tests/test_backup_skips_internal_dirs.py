"""#131：官方导出的 ZIP 带着 _app 运行时文档，重新导入时被自身预检拒绝。

_app/ 是播种进 vault 的代码树，docs/INTERNALS.md 超过记忆桶的读取上限；
_app/_prev/ 里还有同名副本，会撞「重复 bucket_id」。它们都不是记忆。
"""

import frontmatter
import pytest

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from migrate_engine import MigrateEngine
from ombrebrain.storage import backup_archive as archive_mod
from ombrebrain.storage.backup_archive import (
    build_export_archive,
    build_export_archive_file,
    read_backup_archive,
)

# 比导入时记忆桶的读取上限（51200 + 16384 + 65536 = 133120）大，复现社区报告的 160557。
_BIG_DOC = "# INTERNALS\n\n" + "运行时文档。" * 9000


class _Backend:
    def vector_dim(self):
        return 2


def _config(root):
    return {
        "buckets_dir": str(root),
        "embedding": {"enabled": False},
        "storage": {"external_change_poll_seconds": 0},
    }


def _engine(config):
    engine = EmbeddingEngine(config)
    engine.model = "test-embedding"
    engine._backend = _Backend()
    return engine


def _vault_with_app(root):
    bucket = root / "dynamic" / "general" / "memory_memory-1.md"
    bucket.parent.mkdir(parents=True, exist_ok=True)
    bucket.write_text(
        frontmatter.dumps(frontmatter.Post(
            "真正的记忆", id="memory-1", name="Memory", type="dynamic",
            domain=["general"], created="2026-07-11T12:00:00",
        )),
        encoding="utf-8",
    )
    for doc_dir in (root / "_app" / "docs", root / "_app" / "_prev" / "docs"):
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "INTERNALS.md").write_text(_BIG_DOC, encoding="utf-8")
        (doc_dir / "CLAUDE_PROMPT.md").write_text("# prompt\n", encoding="utf-8")
    assert len(_BIG_DOC.encode("utf-8")) > 133120


_META = {
    "exported_at": "2026-07-11T12:00:00",
    "version": "test",
    "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
}


def test_in_memory_export_leaves_out_app_tree(tmp_path):
    _vault_with_app(tmp_path)
    engine = _engine(_config(tmp_path))
    payload, manifest = build_export_archive(str(tmp_path), engine.db_path, _META)

    names = set(read_backup_archive(payload)["files"])
    assert "buckets/dynamic/general/memory_memory-1.md" in names
    assert not [n for n in names if n.startswith("buckets/_")]
    assert not [f for f in manifest["files"] if f["path"].startswith("buckets/_")]


def test_disk_export_leaves_out_app_tree(tmp_path):
    _vault_with_app(tmp_path)
    engine = _engine(_config(tmp_path))
    path, manifest = build_export_archive_file(str(tmp_path), engine.db_path, _META)
    try:
        paths = {f["path"] for f in manifest["files"]}
        assert "buckets/dynamic/general/memory_memory-1.md" in paths
        assert not [p for p in paths if p.startswith("buckets/_")]
    finally:
        import os
        os.remove(path)


async def _import(tmp_path, payload):
    target = tmp_path / "target"
    config = _config(target)
    engine = _engine(config)
    manager = BucketManager(config, embedding_engine=engine)
    migrate = MigrateEngine(config, manager, engine)
    parsed = await migrate.parse_zip(payload)
    return parsed, migrate, manager


@pytest.mark.asyncio
async def test_new_export_round_trips(tmp_path):
    source = tmp_path / "source"
    _vault_with_app(source)
    payload, _ = build_export_archive(str(source), _engine(_config(source)).db_path, _META)

    parsed, migrate, manager = await _import(tmp_path, payload)
    assert parsed["ok"] is True
    await migrate.apply({})
    assert (await manager.get("memory-1"))["content"] == "真正的记忆"


@pytest.mark.asyncio
async def test_legacy_export_with_app_tree_still_imports(tmp_path, monkeypatch):
    # 造一个 3.8.0 之前的官方包：导出时不跳过 _app，清单里也登记了它们。
    source = tmp_path / "source"
    _vault_with_app(source)
    monkeypatch.setattr(archive_mod, "is_internal_vault_path", lambda _relative: False, raising=False)
    payload, manifest = build_export_archive(str(source), _engine(_config(source)).db_path, _META)
    monkeypatch.undo()
    assert any(f["path"] == "buckets/_app/docs/INTERNALS.md" for f in manifest["files"])

    parsed, migrate, manager = await _import(tmp_path, payload)
    assert parsed["ok"] is True
    await migrate.apply({})
    assert migrate.get_status()["result"] == {"imported": 1, "skipped": 0}
    assert (await manager.get("memory-1"))["content"] == "真正的记忆"
    assert await manager.get("INTERNALS") is None
