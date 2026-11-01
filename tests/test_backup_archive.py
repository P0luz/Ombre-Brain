import hashlib
import io
import json
import os
import sqlite3
import stat
import tracemalloc
import zipfile
from pathlib import Path

import frontmatter
import pytest

from ombrebrain.storage.backup_archive import (
    BackupArchiveError,
    build_export_archive,
    build_export_archive_file,
    extract_backup_archive_file,
    read_backup_archive,
)
from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from migrate_engine import MigrateEngine
import migrate_engine as migrate_mod
from ombrebrain.storage import backup_archive as archive_mod
from ombrebrain.storage.source_store import SourceStore
from ombrebrain.you import YouStore, YouStoreError, validate_you_snapshot_file


class _Backend:
    def vector_dim(self):
        return 2


def _config(root):
    return {
        "buckets_dir": str(root),
        "embedding": {"enabled": False},
        "storage": {"external_change_poll_seconds": 0},
    }


def _engine(config, model="test-embedding"):
    engine = EmbeddingEngine(config)
    engine.model = model
    engine._backend = _Backend()
    return engine


def _write_bucket(root, bucket_id="memory-1", content="important memory"):
    path = root / "dynamic" / "general" / f"memory_{bucket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        content,
        id=bucket_id,
        name="Memory",
        type="dynamic",
        domain=["general"],
        created="2026-07-11T12:00:00",
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _rewrite_zip(payload, updates):
    source = zipfile.ZipFile(io.BytesIO(payload), "r")
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            target.writestr(info.filename, updates.get(info.filename, source.read(info)))
    return output.getvalue()


def test_export_archive_has_verified_manifest_and_sqlite_snapshot(tmp_path):
    vault = tmp_path / "vault"
    bucket = _write_bucket(vault)
    engine = _engine(_config(vault))
    engine._store_embedding("memory-1", [0.1, 0.2], "digest")

    payload, manifest = build_export_archive(
        str(vault), engine.db_path, {"exported_at": "now", "version": "test"}
    )
    package = read_backup_archive(payload)

    assert package["integrity_verified"] is True
    assert package["integrity_warning"] == ""
    assert package["manifest"] == manifest
    assert package["files"]["buckets/dynamic/general/memory_memory-1.md"] == bucket.read_bytes()
    assert "embeddings.db" in package["files"]
    assert manifest["file_count"] == 3

    db_file = tmp_path / "snapshot.db"
    db_file.write_bytes(package["files"]["embeddings.db"])
    with sqlite3.connect(db_file) as connection:
        row = connection.execute(
            "SELECT bucket_id, content_hash FROM embeddings WHERE bucket_id = ?",
            ("memory-1",),
        ).fetchone()
    assert row == ("memory-1", "digest")


def test_export_archive_carries_valid_you_snapshot_when_feature_was_configured(tmp_path):
    vault = tmp_path / "vault"
    _write_bucket(vault)
    source_store = YouStore(vault)
    source_state = source_store.set_enabled(True, expected_revision=0)

    payload, manifest = build_export_archive(
        str(vault), "", {"exported_at": "now", "version": "test"}
    )
    package = read_backup_archive(payload)

    assert "you/you.sqlite3" in package["files"]
    assert any(item["path"] == "you/you.sqlite3" for item in manifest["files"])
    snapshot = tmp_path / "you.sqlite3"
    snapshot.write_bytes(package["files"]["you/you.sqlite3"])
    validate_you_snapshot_file(snapshot)
    assert source_state.scope is not None


@pytest.mark.asyncio
async def test_exact_full_restore_publishes_you_snapshot(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault)
    source_store = YouStore(source_vault)
    source_state = source_store.set_enabled(True, expected_revision=0)
    payload, _manifest = build_export_archive(
        str(source_vault),
        "",
        {"exported_at": "now", "version": "test"},
    )

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)

    parsed = await migrate.parse_zip(payload)
    assert parsed["ok"] is True
    await migrate.apply({})

    restored = YouStore(target_vault).get_state()
    assert restored.enabled is True
    assert restored.scope == source_state.scope


def test_you_snapshot_validator_rejects_unrecognized_sqlite_schema(tmp_path):
    path = tmp_path / "not-you.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(YouStoreError, match="schema"):
        validate_you_snapshot_file(path)


def test_disk_export_streams_sources_without_path_read_bytes(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    bucket = _write_bucket(vault, content="streamed memory " * 10_000)
    source_text = "流式备份中的原文证据\n"
    source_ref = SourceStore(vault).put(source_text)
    with bucket.open("rb") as handle:
        expected_bucket = handle.read()

    def forbid_materializing(_path):
        raise AssertionError("disk export must stream files instead of Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbid_materializing)
    archive_path, manifest = build_export_archive_file(
        str(vault),
        "",
        {"exported_at": "now", "version": "test"},
    )
    try:
        with open(archive_path, "rb") as handle:
            package = read_backup_archive(handle.read())
        member = f"buckets/dynamic/general/{bucket.name}"
        assert package["files"][member] == expected_bucket
        assert package["files"][f"sources/{source_ref}.source"] == source_text.encode()
        assert package["manifest"] == manifest
    finally:
        os.unlink(archive_path)


def test_new_exports_refuse_dangling_source_references(tmp_path):
    vault = tmp_path / "vault"
    bucket = _write_bucket(vault)
    post = frontmatter.load(bucket)
    missing_ref = "src_" + "a" * 64
    post.metadata["source_refs"] = [{"ref": missing_ref, "ranges": [[1, 1]]}]
    bucket.write_text(frontmatter.dumps(post), encoding="utf-8")

    with pytest.raises(BackupArchiveError, match="悬空原文证据引用"):
        build_export_archive(str(vault), "", {"version": "test"})
    with pytest.raises(BackupArchiveError, match="悬空原文证据引用"):
        build_export_archive_file(str(vault), "", {"version": "test"})


def test_disk_export_aborts_and_cleans_temp_files_at_compressed_cap(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    path = vault / "dynamic" / "general" / "large.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(os.urandom(4096))
    created = []
    original_mkstemp = archive_mod.tempfile.mkstemp

    def tracked_mkstemp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        fd, temp_path = original_mkstemp(*args, **kwargs)
        created.append(temp_path)
        return fd, temp_path

    monkeypatch.setattr(archive_mod.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(archive_mod, "MAX_ARCHIVE_BYTES", 512)

    with pytest.raises(BackupArchiveError, match="压缩后"):
        build_export_archive_file(str(vault), "", {"version": "test"})

    assert created
    assert all(not os.path.exists(temp_path) for temp_path in created)


def test_manifest_rejects_tampered_member(tmp_path):
    vault = tmp_path / "vault"
    path = _write_bucket(vault)
    engine = _engine(_config(vault))
    payload, _ = build_export_archive(
        str(vault), engine.db_path, {"exported_at": "now", "version": "test"}
    )
    member = f"buckets/dynamic/general/{path.name}"
    tampered = _rewrite_zip(payload, {member: b"changed after manifest"})

    with pytest.raises(BackupArchiveError, match="不一致|校验失败"):
        read_backup_archive(tampered)


def test_reader_rejects_traversal_and_normalizes_legacy_windows_paths():
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("buckets/../../outside.md", b"bad")
    with pytest.raises(BackupArchiveError, match="不安全路径"):
        read_backup_archive(malicious.getvalue())

    legacy = io.BytesIO()
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr("buckets\\dynamic\\general\\old.md", b"legacy")
    package = read_backup_archive(legacy.getvalue())
    assert package["integrity_verified"] is False
    assert package["files"] == {"buckets/dynamic/general/old.md": b"legacy"}
    assert "旧版备份" in package["integrity_warning"]


def test_reader_rejects_symbolic_link_member():
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        info = zipfile.ZipInfo("buckets/dynamic/link.md")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"../../outside.md")

    with pytest.raises(BackupArchiveError, match="符号链接"):
        read_backup_archive(malicious.getvalue())


def test_disk_extractor_does_not_materialize_large_member_in_memory(tmp_path):
    archive_path = tmp_path / "large.zip"
    chunk = b"z" * (1024 * 1024)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        with archive.open("embeddings.db", "w") as member:
            for _ in range(32):
                member.write(chunk)

    destination = tmp_path / "extracted"
    tracemalloc.start()
    package = extract_backup_archive_file(str(archive_path), str(destination))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    extracted = package["files"]["embeddings.db"]
    assert os.path.getsize(extracted) == 32 * 1024 * 1024
    assert peak < 8 * 1024 * 1024


@pytest.mark.parametrize("with_manifest", [False, True], ids=["legacy", "manifest"])
def test_production_extractor_rejects_every_unused_member(tmp_path, with_manifest):
    archive_path = tmp_path / "junk.zip"
    files = {
        "buckets/dynamic/valid.md": b"---\nid: valid\n---\nbody\n",
        "junk.bin": b"0" * (2 * 1024 * 1024),
    }
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        if with_manifest:
            manifest = {
                "schema_version": 1,
                "kind": "ombre-brain-backup",
                "created_at": "now",
                "version": "test",
                "file_count": len(files),
                "total_bytes": sum(len(data) for data in files.values()),
                "files": [
                    {
                        "path": name,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for name, data in files.items()
                ],
            }
            archive.writestr("backup_manifest.json", json.dumps(manifest))

    destination = tmp_path / "extracted"
    with pytest.raises(BackupArchiveError, match="不支持的成员"):
        extract_backup_archive_file(str(archive_path), str(destination))

    assert not list(destination.glob("*"))


def test_production_extractor_enforces_path_specific_member_caps(
    tmp_path,
    monkeypatch,
):
    archive_path = tmp_path / "oversized-bucket.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("buckets/dynamic/too-large.md", b"x" * 65)

    monkeypatch.setattr(archive_mod, "MIGRATE_MAX_BUCKET_BYTES", 64)
    with pytest.raises(BackupArchiveError, match="成员过大"):
        extract_backup_archive_file(str(archive_path), str(tmp_path / "extracted"))


def test_production_extractor_does_not_fsync_each_temporary_member(
    tmp_path,
    monkeypatch,
):
    archive_path = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("buckets/dynamic/valid.md", b"---\nid: valid\n---\nbody\n")

    monkeypatch.setattr(
        archive_mod.os,
        "fsync",
        lambda _fd: pytest.fail("temporary extraction must not fsync per member"),
    )
    package = extract_backup_archive_file(
        str(archive_path),
        str(tmp_path / "extracted"),
    )

    assert set(package["files"]) == {"buckets/dynamic/valid.md"}


def test_embedding_merge_fetches_large_vectors_in_bounded_batches(tmp_path):
    source_db = tmp_path / "source.db"
    payload = "[0.1,0.2]" + (" " * (512 * 1024 - len("[0.1,0.2]")))
    row_count = 128
    with sqlite3.connect(source_db) as connection:
        connection.execute(
            """CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content_hash TEXT NOT NULL
            )"""
        )
        connection.executemany(
            "INSERT INTO embeddings VALUES (?, ?, ?, ?)",
            (
                (f"memory-{index}", payload, "now", f"hash-{index}")
                for index in range(row_count)
            ),
        )

    config = _config(tmp_path / "target")
    target_engine = _engine(config)
    migrate = MigrateEngine(
        config,
        BucketManager(config, embedding_engine=target_engine),
        target_engine,
    )
    migrate._import_model_dim = 2
    id_map = {f"memory-{index}": f"memory-{index}" for index in range(row_count)}

    tracemalloc.start()
    merged = migrate._merge_embeddings_path(str(source_db), id_map)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(merged) == row_count
    assert peak < 32 * 1024 * 1024
    with sqlite3.connect(target_engine.db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert count == row_count


def test_embedding_merge_rejects_untrusted_cells_and_reindexes_them(tmp_path):
    source_db = tmp_path / "source.db"
    oversized = "[0.1,0.2]" + (" " * (1024 * 1024))
    rows = [
        ("good", "[0.1, 0.2]", "now", "digest"),
        ("oversized", oversized, "now", "digest"),
        ("wrong-element", '["not-a-number", 0.2]', "now", "digest"),
        ("nonfinite", "[NaN, 0.2]", "now", "digest"),
        ("wrong-dimension", "[0.1]", "now", "digest"),
        ("wrong-type", 42, "now", "digest"),
    ]
    with sqlite3.connect(source_db) as connection:
        connection.execute(
            """CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding,
                updated_at,
                content_hash
            )"""
        )
        connection.executemany("INSERT INTO embeddings VALUES (?, ?, ?, ?)", rows)

    config = _config(tmp_path / "target")
    target_engine = _engine(config)
    migrate = MigrateEngine(
        config,
        BucketManager(config, embedding_engine=target_engine),
        target_engine,
    )
    migrate._import_model_dim = 2
    id_map = {source_id: source_id for source_id, *_rest in rows}

    merged = migrate._merge_embeddings_path(str(source_db), id_map)

    assert merged == {"good"}
    with sqlite3.connect(target_engine.db_path) as connection:
        stored = connection.execute(
            "SELECT bucket_id, embedding FROM embeddings"
        ).fetchall()
    assert stored == [("good", "[0.1,0.2]")]


@pytest.mark.asyncio
async def test_export_to_empty_vault_restores_markdown_and_current_embedding_schema(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="restore this exact text")
    source_engine = _engine(_config(source_vault))
    source_engine._store_embedding("memory-1", [0.3, 0.4], "source-hash")
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "2026-07-11T12:00:00",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)

    parsed = await migrate.parse_zip(payload)
    assert parsed["ok"] is True
    assert parsed["integrity_verified"] is True
    await migrate.apply({})

    restored = await manager.get("memory-1")
    assert restored is not None
    assert restored["content"] == "restore this exact text"
    assert await target_engine.get_embedding("memory-1") == pytest.approx([0.3, 0.4])
    assert target_engine.get_content_hash("memory-1") == "source-hash"
    assert migrate.get_status()["result"] == {"imported": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_export_restore_round_trip_preserves_source_evidence(tmp_path):
    source_vault = tmp_path / "source"
    source_config = _config(source_vault)
    source_engine = _engine(source_config)
    source_manager = BucketManager(source_config, embedding_engine=source_engine)
    source_store = SourceStore(source_vault)
    raw = "开场\n需要核对的原话\n尾声\n"
    ref = source_store.put(raw)
    bucket_id = await source_manager.create(
        content="整理后事件",
        title="核对标题",
        source_refs=[{"ref": ref, "ranges": [[2, 2]]}],
    )
    payload, manifest = build_export_archive(
        str(source_vault), "", {"exported_at": "now", "version": "test"}
    )

    assert any(item["path"] == f"sources/{ref}.source" for item in manifest["files"])

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    target_manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, target_manager, target_engine)

    parsed = await migrate.parse_zip(payload)
    assert parsed["ok"] is True
    await migrate.apply({})

    restored = await target_manager.get(bucket_id)
    assert restored["metadata"]["source_refs"] == [
        {"ref": ref, "ranges": [[2, 2]]}
    ]
    assert SourceStore(target_vault).read(ref) == raw


@pytest.mark.asyncio
async def test_legacy_backup_with_dangling_source_ref_warns_but_restores_bucket(
    tmp_path,
):
    source_vault = tmp_path / "source"
    source_config = _config(source_vault)
    source_engine = _engine(source_config)
    source_manager = BucketManager(source_config, embedding_engine=source_engine)
    source_store = SourceStore(source_vault)
    ref = source_store.put("旧版没有打包这份原文")
    bucket_id = await source_manager.create(
        content="旧版事件",
        title="旧版标题",
        source_refs=[{"ref": ref, "ranges": [[1, 1]]}],
    )
    # Recreate the v2.10.0 package shape directly: its manifest was valid for
    # the files it carried, but source evidence was not part of that file set.
    bucket_path = Path(source_manager._find_bucket_file(bucket_id))
    bucket_member = f"buckets/{bucket_path.relative_to(source_vault).as_posix()}"
    export_meta = json.dumps({
        "exported_at": "now",
        "version": "2.10.0",
    }).encode()
    legacy_files = {
        bucket_member: bucket_path.read_bytes(),
        "export_meta.json": export_meta,
    }
    entries = [
        {
            "path": path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in sorted(legacy_files.items())
    ]
    manifest = {
        "schema_version": 1,
        "kind": "ombre-brain-backup",
        "created_at": "now",
        "version": "2.10.0",
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
        "files": entries,
    }
    legacy_buffer = io.BytesIO()
    with zipfile.ZipFile(legacy_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in legacy_files.items():
            archive.writestr(path, data)
        archive.writestr("backup_manifest.json", json.dumps(manifest).encode())
    payload = legacy_buffer.getvalue()

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    target_manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, target_manager, target_engine)
    parsed = await migrate.parse_zip(payload)

    assert parsed["ok"] is True
    assert "原文证据引用没有对应文件" in parsed["integrity_warning"]
    await migrate.apply({})
    assert (await target_manager.get(bucket_id))["content"] == "旧版事件"


@pytest.mark.asyncio
async def test_source_filename_hash_binding_survives_recomputed_manifest(tmp_path):
    vault = tmp_path / "source"
    _write_bucket(vault)
    ref = SourceStore(vault).put("原始证据")
    payload, _ = build_export_archive(
        str(vault), "", {"exported_at": "now", "version": "test"}
    )
    member = f"sources/{ref}.source"
    changed = "攻击者替换的证据".encode()
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        manifest = json.loads(archive.read("backup_manifest.json"))
    for entry in manifest["files"]:
        if entry["path"] == member:
            manifest["total_bytes"] += len(changed) - int(entry["size"])
            entry["size"] = len(changed)
            entry["sha256"] = hashlib.sha256(changed).hexdigest()
            break
    tampered = _rewrite_zip(
        payload,
        {
            member: changed,
            "backup_manifest.json": json.dumps(manifest).encode(),
        },
    )

    target_config = _config(tmp_path / "target")
    target_engine = _engine(target_config)
    migrate = MigrateEngine(
        target_config,
        BucketManager(target_config, embedding_engine=target_engine),
        target_engine,
    )
    parsed = await migrate.parse_zip(tampered)

    assert parsed["ok"] is False
    assert "SHA-256" in parsed["error"]


@pytest.mark.asyncio
async def test_keep_both_maps_imported_vector_to_new_id(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    source_engine = _engine(_config(source_vault))
    source_engine._store_embedding("memory-1", [0.7, 0.8], "imported-hash")
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "now",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)

    parsed = await migrate.parse_zip(payload)
    assert parsed["conflicts_count"] == 1
    await migrate.apply({"memory-1": "keep_both"})

    buckets = await manager.list_all()
    assert {bucket["content"] for bucket in buckets} == {"local version", "imported version"}
    imported = next(bucket for bucket in buckets if bucket["content"] == "imported version")
    assert imported["id"] != "memory-1"
    assert await target_engine.get_embedding(imported["id"]) == pytest.approx([0.7, 0.8])


def test_keep_both_relation_target_remap_preserves_other_ledger_fields(tmp_path):
    path = _write_bucket(tmp_path, bucket_id="source")
    post = frontmatter.load(path)
    post["relation_links"] = [{
        "target_bucket_id": "old-target", "type": "causes",
        "label": "", "status": "detached",
    }]
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    migrate = object.__new__(MigrateEngine)
    migrate._atomic_write = lambda target, content: Path(target).write_text(content, encoding="utf-8")

    migrate._remap_imported_relation_targets(
        {"source": str(path)},
        {"old-target": "new-target"},
        frozenset({"source", "old-target"}),
    )

    remapped = frontmatter.load(path).metadata["relation_links"][0]
    assert remapped == {"target_bucket_id": "new-target", "type": "causes", "label": "", "status": "detached"}


@pytest.mark.asyncio
async def test_overwrite_preserves_old_memory_under_unique_archived_id(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "now",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)

    await migrate.parse_zip(payload)
    await migrate.apply({"memory-1": "overwrite"})

    buckets = await manager.list_all(include_archive=True)
    assert {bucket["content"] for bucket in buckets} == {"local version", "imported version"}
    assert len({bucket["id"] for bucket in buckets}) == 2
    archived = next(bucket for bucket in buckets if bucket["content"] == "local version")
    assert archived["id"].startswith("memory-1-superseded-")
    assert archived["metadata"]["superseded_by"] == "memory-1"


@pytest.mark.asyncio
async def test_overwrite_leaves_old_memory_untouched_when_new_content_write_fails(tmp_path, monkeypatch):
    """回归锁死找茬会话发现的 bug：overwrite 冲突原来是「先删旧、再写新」，

    写新内容失败时旧桶已经被移进 archive/ 改名，两边都没有=数据丢失。
    修复后顺序反过来：新内容先完整落盘到暂存文件，写失败旧桶必须完全不受影响。
    """
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "now",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)

    def _boom(self, pb, target_id, buckets_dir):
        raise OSError("simulated disk failure while staging new content")

    monkeypatch.setattr(MigrateEngine, "_write_bucket_file_staged", _boom)

    await migrate.parse_zip(payload)
    await migrate.apply({"memory-1": "overwrite"})

    # 新内容落盘先炸：旧桶必须还在原地、原样、原 ID，完全没被删/被改名。
    buckets = await manager.list_all(include_archive=True)
    assert len(buckets) == 1
    survivor = buckets[0]
    assert survivor["id"] == "memory-1"
    assert survivor["content"] == "local version"
    assert survivor["metadata"].get("type") != "archived"
    assert migrate._apply_errors, "写入失败应该被记录成一条 apply error"


@pytest.mark.asyncio
async def test_overwrite_cleans_up_staged_file_when_old_bucket_handling_fails(tmp_path, monkeypatch):
    """写新内容成功，但处理旧桶（delete+rekey）失败：暂存文件必须被清理掉，

    不能留下一个既不是新桶也不是旧桶、谁都不认的孤儿 .staging 文件。
    """
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "now",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)

    def _boom(self, existing_path, bucket_id, buckets_dir):
        raise OSError("simulated failure while archiving the old bucket")

    monkeypatch.setattr(MigrateEngine, "_write_historical_copy", _boom)

    await migrate.parse_zip(payload)
    await migrate.apply({"memory-1": "overwrite"})

    staged_leftovers = list((target_vault / "dynamic").rglob("*.staging-*"))
    assert staged_leftovers == [], f"暂存文件没被清理: {staged_leftovers}"
    assert migrate._apply_errors, "旧桶处理失败应该被记录成一条 apply error"
    buckets = await manager.list_all(include_archive=True)
    assert [(bucket["id"], bucket["content"]) for bucket in buckets] == [
        ("memory-1", "local version")
    ]


@pytest.mark.asyncio
async def test_missing_snapshot_vector_is_durably_queued(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault)
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "now",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    class Outbox:
        def __init__(self):
            self.queued = []

        def enqueue(self, bucket_id, content):
            self.queued.append((bucket_id, content))
            return True

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    outbox = Outbox()
    manager.attach_embedding_outbox(outbox)
    migrate = MigrateEngine(target_config, manager, target_engine)

    assert (await migrate.parse_zip(payload))["ok"] is True
    await migrate.apply({})
    assert outbox.queued == [("memory-1", "important memory")]
    assert migrate.get_status()["reindex_progress"] == {"done": 1, "total": 1, "errors": 0}


@pytest.mark.asyncio
async def test_disk_backed_parse_releases_extracted_payload_after_apply(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault)
    source_engine = _engine(_config(source_vault))
    archive_path, _manifest = build_export_archive_file(
        str(source_vault),
        source_engine.db_path,
        {
            "exported_at": "now",
            "version": "test",
            "embedding": {"model": "test-embedding", "dim": 2, "backend": "api"},
        },
    )

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)
    reservation = migrate.reserve_parse()
    try:
        parsed = await migrate.parse_zip_file(
            archive_path,
            reservation_id=reservation,
        )
    finally:
        os.unlink(archive_path)

    assert parsed["ok"] is True
    workspace = migrate._parse_temp_dir
    assert os.path.isdir(workspace)
    assert migrate._parsed_buckets[0].md_bytes is None
    assert os.path.isfile(migrate._parsed_buckets[0].md_path)
    assert migrate._zip_db_bytes is None

    await migrate.apply({})

    assert not os.path.exists(workspace)
    assert migrate._parsed_buckets == []
    assert migrate._zip_db_path == ""
    assert (await manager.get("memory-1"))["content"] == "important memory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        {},
        {"memory-1": "overwrite"},
        {"memory-1": "keep_both"},
    ],
    ids=["default-skip", "forged-overwrite", "forged-keep-both"],
)
async def test_apply_rechecks_conflict_created_after_parse(tmp_path, decision):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {"embedding": {"model": "test-embedding", "dim": 2}},
    )

    target_vault = tmp_path / "target"
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)
    parsed = await migrate.parse_zip(payload)
    assert parsed["conflicts_count"] == 0

    _write_bucket(target_vault, content="created after parse")
    await migrate.apply(decision)

    buckets = await manager.list_all(include_archive=True)
    assert [(bucket["id"], bucket["content"]) for bucket in buckets] == [
        ("memory-1", "created after parse")
    ]
    assert migrate.get_status()["result"] == {"imported": 0, "skipped": 1}
    assert "新冲突" in " ".join(migrate._apply_errors)


@pytest.mark.asyncio
async def test_overwrite_preserves_latest_version_changed_after_parse(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {"embedding": {"model": "test-embedding", "dim": 2}},
    )

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local at parse")
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)
    await migrate.parse_zip(payload)
    assert await manager.update("memory-1", content="latest concurrent edit") is True

    await migrate.apply({"memory-1": "overwrite"})

    buckets = await manager.list_all(include_archive=True)
    assert {bucket["content"] for bucket in buckets} == {
        "imported version",
        "latest concurrent edit",
    }
    historical = next(bucket for bucket in buckets if bucket["content"] == "latest concurrent edit")
    assert historical["metadata"]["superseded_by"] == "memory-1"


@pytest.mark.asyncio
async def test_migrate_rejects_bucket_over_runtime_content_limit(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="x" * (51 * 1024))
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {"embedding": {}},
    )
    target_config = _config(tmp_path / "target")
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)

    result = await MigrateEngine(target_config, manager, target_engine).parse_zip(payload)

    assert result["ok"] is False
    assert "正文过大" in result["error"]


@pytest.mark.asyncio
async def test_migrate_rejects_non_json_safe_yaml_metadata(tmp_path):
    source_vault = tmp_path / "source"
    path = source_vault / "dynamic" / "general" / "unsafe.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nid: unsafe-metadata\npayload: !!set\n  ? value\n---\nbody\n",
        encoding="utf-8",
    )
    payload, _ = build_export_archive(
        str(source_vault),
        "",
        {"embedding": {}},
    )
    target_config = _config(tmp_path / "target")
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)

    result = await MigrateEngine(target_config, manager, target_engine).parse_zip(payload)

    assert result["ok"] is False
    assert "JSON-safe" in result["error"]


@pytest.mark.asyncio
async def test_overwrite_rolls_back_when_old_source_cannot_be_removed(tmp_path, monkeypatch):
    source_vault = tmp_path / "source"
    source_path = _write_bucket(source_vault, content="imported permanent")
    source_post = frontmatter.load(source_path)
    source_post["type"] = "permanent"
    source_path.write_text(frontmatter.dumps(source_post), encoding="utf-8")
    source_engine = _engine(_config(source_vault))
    payload, _ = build_export_archive(
        str(source_vault),
        source_engine.db_path,
        {"embedding": {"model": "test-embedding", "dim": 2}},
    )

    target_vault = tmp_path / "target"
    old_path = _write_bucket(target_vault, content="local survivor")
    target_config = _config(target_vault)
    target_engine = _engine(target_config)
    manager = BucketManager(target_config, embedding_engine=target_engine)
    migrate = MigrateEngine(target_config, manager, target_engine)
    await migrate.parse_zip(payload)

    original_unlink = os.unlink
    expected = os.path.normcase(os.path.abspath(str(old_path)))

    def fail_old_source(path, *args, **kwargs):
        normalized = str(path)
        if normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
        if os.path.normcase(os.path.abspath(normalized)) == expected:
            raise OSError("simulated old source unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(migrate_mod.os, "unlink", fail_old_source)
    await migrate.apply({"memory-1": "overwrite"})

    buckets = await manager.list_all(include_archive=True)
    assert [(bucket["id"], bucket["content"]) for bucket in buckets] == [
        ("memory-1", "local survivor")
    ]
    assert migrate._apply_errors
    assert not list(target_vault.rglob("*.staging-*"))
