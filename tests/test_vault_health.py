import sqlite3

import frontmatter

from ombrebrain.storage.vault_health import inspect_vault


def _write(path, bucket_id, content="memory"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content, id=bucket_id, type="dynamic")),
        encoding="utf-8",
    )


def _db(path, ids=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content_hash TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.executemany(
            "INSERT INTO embeddings VALUES (?, '[0.1]', 'now', 'hash')",
            [(item,) for item in ids],
        )


def test_vault_health_reports_clean_source_and_projection(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    db = vault / "embeddings.db"
    _db(db, ["one"])

    report = inspect_vault(str(vault), str(db))

    assert report["status"] == "ok"
    assert report["markdown"]["file_count"] == 1
    assert report["sqlite"]["quick_check_ok"] is True
    assert report["sqlite"]["missing_unqueued_count"] == 0


def test_vault_health_distinguishes_pending_missing_and_orphan_vectors(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "one")
    _write(vault / "dynamic" / "general" / "two.md", "two")
    db = vault / "embeddings.db"
    _db(db, ["one", "gone"])

    queued = inspect_vault(str(vault), str(db), pending_ids={"two"})
    assert queued["status"] == "warning"
    assert queued["sqlite"]["orphan_ids"] == ["gone"]
    assert queued["sqlite"]["missing_active_ids"] == ["two"]
    assert queued["sqlite"]["missing_unqueued_count"] == 0

    unqueued = inspect_vault(str(vault), str(db))
    assert unqueued["sqlite"]["missing_unqueued_ids"] == ["two"]


def test_vault_health_reports_parse_errors_and_duplicate_ids(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "one.md", "same")
    _write(vault / "archive" / "general" / "old.md", "same")
    bad = vault / "dynamic" / "general" / "bad.md"
    bad.write_bytes(b"\xff\xfe")

    report = inspect_vault(str(vault), str(vault / "missing.db"))

    assert report["status"] == "error"
    assert report["markdown"]["duplicate_id_count"] == 1
    assert report["markdown"]["parse_error_count"] == 1


# ============================================================
# 代码树不是记忆：vault 内以 _ 开头的目录必须跳过
#
# CODE_DIR 默认就在 <vault>/_app。3.0.0 起 entrypoint 会把 docs/ 与 kernel/
# 一起播种进去，而 _app/_prev 这个崩溃回滚点又保留了上一版的同名副本——
# 同一份 CLAUDE_PROMPT.md 于是在两个路径各出现一次。不跳过的话它们会被当成
# 「同一个 id 的重复桶」，让完整性诊断整体报 error，掩盖真正的记忆损坏。
# ============================================================

def test_code_tree_markdown_is_not_counted_as_memory(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "Memory_aaa.md", "aaa")

    # 播种进来的代码树：docs 与 kernel 里的 .md，外加回滚点里的同名副本
    for base in ("_app", "_app/_prev"):
        for rel in ("docs/CLAUDE_PROMPT.md", "docs/adr/ADR-0001.md",
                    "kernel/rust/ombre-kernel/README.md"):
            target = vault / base / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# 文档，不是记忆\n", encoding="utf-8")

    health = inspect_vault(str(vault), "")
    markdown = health["markdown"]

    assert markdown["duplicate_id_count"] == 0, markdown["duplicate_ids"]
    assert markdown["file_count"] == 1, "只有真正的记忆桶应计入"
    assert markdown["unique_ids"] == 1
    assert markdown["parse_error_count"] == 0


def test_source_evidence_dir_is_also_skipped(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "Memory_bbb.md", "bbb")
    stray = vault / "_sources" / "notes.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("# 原文层里的散落文件\n", encoding="utf-8")

    markdown = inspect_vault(str(vault), "")["markdown"]

    assert markdown["file_count"] == 1
    assert markdown["duplicate_id_count"] == 0


def test_real_duplicate_memory_ids_are_still_reported(tmp_path):
    """跳过内部目录不能把真正的重复记忆一起放过。"""
    vault = tmp_path / "vault"
    _write(vault / "dynamic" / "general" / "Memory_ccc.md", "ccc")
    _write(vault / "archive" / "Memory_ccc_copy.md", "ccc")

    markdown = inspect_vault(str(vault), "")["markdown"]

    assert markdown["duplicate_id_count"] == 1
    assert "ccc" in markdown["duplicate_ids"]
