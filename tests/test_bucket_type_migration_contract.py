"""Regression contract for editing a bucket's storage-backed type.

The bucket type is not merely presentation metadata: it selects the tree in
which the Markdown source of truth lives.  Editing ``metadata.type`` without
relocating that file makes later scans disagree about what the bucket is.
"""

from pathlib import Path

import frontmatter
import pytest


def _bucket_files(bucket_mgr, bucket_id: str) -> list[Path]:
    """Return every Markdown source whose frontmatter owns ``bucket_id``."""
    matches: list[Path] = []
    for path in Path(bucket_mgr.base_dir).rglob("*.md"):
        try:
            if frontmatter.load(path).get("id") == bucket_id:
                matches.append(path)
        except Exception:
            continue
    return matches


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_type", "source_type", "expected_tree"),
    [
        ("dynamic", "permanent", ("dynamic", "migration-domain")),
        ("permanent", "dynamic", ("permanent", "migration-domain")),
        ("feel", "dynamic", ("feel", "沉淀物")),
        ("plan", "dynamic", ("plans", "active")),
        ("letter", "dynamic", ("letters", "history")),
        ("i", "permanent", ("dynamic", "migration-domain")),
        ("self", "permanent", ("dynamic", "migration-domain")),
    ],
)
async def test_type_update_relocates_source_to_the_canonical_tree(
    bucket_mgr,
    target_type,
    source_type,
    expected_tree,
):
    bucket_id = await bucket_mgr.create(
        content=f"move to {target_type}",
        domain=["migration-domain"],
        bucket_type=source_type,
    )
    before = Path((await bucket_mgr.get(bucket_id))["path"])

    assert await bucket_mgr.update(bucket_id, type=target_type) is True

    updated = await bucket_mgr.get(bucket_id)
    assert updated is not None
    assert updated["metadata"]["type"] == target_type
    after = Path(updated["path"])
    assert after.parent == Path(bucket_mgr.base_dir).joinpath(*expected_tree)
    assert after.exists()
    assert not before.exists() or before == after
    assert _bucket_files(bucket_mgr, bucket_id) == [after]


@pytest.mark.asyncio
async def test_type_update_rejects_archived_and_keeps_source_unchanged(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="archive is a lifecycle action",
        domain=["migration-domain"],
        bucket_type="dynamic",
    )
    before = await bucket_mgr.get(bucket_id)
    before_path = Path(before["path"])
    before_bytes = before_path.read_bytes()

    assert await bucket_mgr.update(bucket_id, type="archived") is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert unchanged["metadata"]["type"] == "dynamic"
    assert Path(unchanged["path"]) == before_path
    assert before_path.read_bytes() == before_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [before_path]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"name": "must not edit archived"},
        {"pinned": True},
        {"type": "dynamic"},
    ],
)
async def test_archived_bucket_is_terminal_for_all_regular_updates(
    bucket_mgr,
    updates,
):
    bucket_id = await bucket_mgr.create(
        content="terminal archived memory",
        domain=["migration-domain"],
        bucket_type="dynamic",
    )
    assert await bucket_mgr.archive(bucket_id) is True
    archived = await bucket_mgr.get(bucket_id)
    archived_path = Path(archived["path"])
    archived_bytes = archived_path.read_bytes()

    assert await bucket_mgr.update(bucket_id, **updates) is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert unchanged["metadata"]["type"] == "archived"
    assert Path(unchanged["path"]) == archived_path
    assert archived_path.read_bytes() == archived_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]


@pytest.mark.asyncio
async def test_soft_deleted_tombstone_cannot_be_resurrected_by_pin_update(
    bucket_mgr,
):
    bucket_id = await bucket_mgr.create(
        content="terminal tombstone memory",
        domain=["migration-domain"],
        bucket_type="dynamic",
    )
    assert await bucket_mgr.delete(bucket_id) is True
    tombstone_files = _bucket_files(bucket_mgr, bucket_id)
    assert len(tombstone_files) == 1
    tombstone_path = tombstone_files[0]
    tombstone_bytes = tombstone_path.read_bytes()

    assert await bucket_mgr.update(bucket_id, pinned=True) is False

    unchanged = frontmatter.load(tombstone_path)
    assert unchanged.get("deleted_at")
    assert unchanged.get("tombstone") is True
    assert unchanged.get("pinned") is not True
    assert tombstone_path.read_bytes() == tombstone_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [tombstone_path]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_field", ["pinned", "protected"])
async def test_guarded_permanent_bucket_cannot_be_retyped_out_of_permanent(
    bucket_mgr,
    guard_field,
):
    create_kwargs = {guard_field: True}
    bucket_id = await bucket_mgr.create(
        content=f"guarded by {guard_field}",
        domain=["migration-domain"],
        bucket_type="permanent",
        **create_kwargs,
    )
    before = await bucket_mgr.get(bucket_id)
    before_path = Path(before["path"])
    before_bytes = before_path.read_bytes()

    assert await bucket_mgr.update(bucket_id, type="dynamic") is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert unchanged["metadata"]["type"] == "permanent"
    assert unchanged["metadata"][guard_field] is True
    assert Path(unchanged["path"]) == before_path
    assert before_path.read_bytes() == before_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [before_path]


@pytest.mark.asyncio
async def test_create_rejects_pinned_and_protected_without_writing(bucket_mgr):
    before = set(Path(bucket_mgr.base_dir).rglob("*.md"))

    with pytest.raises(ValueError, match="pinned 与 protected"):
        await bucket_mgr.create(
            content="互斥保护状态不能成为新的持久化脏数据。",
            pinned=True,
            protected=True,
        )

    assert set(Path(bucket_mgr.base_dir).rglob("*.md")) == before


@pytest.mark.asyncio
async def test_type_move_collision_never_overwrites_existing_target(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="source must survive a collision",
        domain=["collision-domain"],
        bucket_type="permanent",
    )
    before = await bucket_mgr.get(bucket_id)
    source_path = Path(before["path"])
    source_bytes = source_path.read_bytes()

    collision_path = (
        Path(bucket_mgr.dynamic_dir) / "collision-domain" / source_path.name
    )
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_bytes = b"pre-existing target must not be replaced\n"
    collision_path.write_bytes(collision_bytes)

    moved = await bucket_mgr.update(bucket_id, type="dynamic")

    assert collision_path.read_bytes() == collision_bytes
    current = await bucket_mgr.get(bucket_id)
    assert current is not None
    if moved:
        # A collision-safe suffix is a valid successful implementation.
        assert current["metadata"]["type"] == "dynamic"
        assert Path(current["path"]).parent == collision_path.parent
        assert Path(current["path"]) != collision_path
        assert not source_path.exists()
    else:
        # Rejecting the move is also valid, provided the source rolls back.
        assert current["metadata"]["type"] == "permanent"
        assert Path(current["path"]) == source_path
        assert source_path.read_bytes() == source_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [Path(current["path"])]


@pytest.mark.asyncio
async def test_type_move_failure_rolls_back_metadata_and_path(bucket_mgr, monkeypatch):
    import bucket_manager as bucket_manager_module

    bucket_id = await bucket_mgr.create(
        content="move failure must be atomic",
        domain=["migration-domain"],
        bucket_type="dynamic",
    )
    before = await bucket_mgr.get(bucket_id)
    source_path = Path(before["path"])
    source_bytes = source_path.read_bytes()

    real_remove = bucket_manager_module.os.remove

    def fail_source_removal(path, *_args, **_kwargs):
        if Path(path) == source_path:
            raise OSError("simulated source removal failure")
        return real_remove(path, *_args, **_kwargs)

    # The migration has already written its destination when source removal
    # runs.  Failing this exact step exercises the hard rollback boundary:
    # the new copy must be removed and the untouched source kept canonical.
    monkeypatch.setattr(bucket_manager_module.os, "remove", fail_source_removal)

    assert await bucket_mgr.update(bucket_id, type="permanent") is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert unchanged["metadata"]["type"] == "dynamic"
    assert Path(unchanged["path"]) == source_path
    assert source_path.read_bytes() == source_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [source_path]


@pytest.mark.asyncio
async def test_type_metadata_write_failure_does_not_move_source(
    bucket_mgr,
    monkeypatch,
):
    import bucket_manager as bucket_manager_module

    bucket_id = await bucket_mgr.create(
        content="write failure must be atomic",
        domain=["migration-domain"],
        bucket_type="dynamic",
    )
    before = await bucket_mgr.get(bucket_id)
    source_path = Path(before["path"])
    source_bytes = source_path.read_bytes()

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated metadata write failure")

    monkeypatch.setattr(bucket_manager_module, "_atomic_write_text", fail_write)

    assert await bucket_mgr.update(bucket_id, type="permanent") is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert unchanged["metadata"]["type"] == "dynamic"
    assert Path(unchanged["path"]) == source_path
    assert source_path.read_bytes() == source_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [source_path]


@pytest.mark.asyncio
async def test_archive_move_failure_keeps_active_type_and_source(
    bucket_mgr,
    monkeypatch,
):
    """Archive uses the same copy-on-commit boundary as an explicit type move."""
    import bucket_manager as bucket_manager_module

    bucket_id = await bucket_mgr.create(
        content="archive failure must not split metadata from storage",
        domain=["migration-domain"],
        bucket_type="dynamic",
    )
    before = await bucket_mgr.get(bucket_id)
    source_path = Path(before["path"])
    source_bytes = source_path.read_bytes()
    real_remove = bucket_manager_module.os.remove

    def fail_source_removal(path, *_args, **_kwargs):
        if Path(path) == source_path:
            raise OSError("simulated archive source removal failure")
        return real_remove(path, *_args, **_kwargs)

    monkeypatch.setattr(bucket_manager_module.os, "remove", fail_source_removal)

    assert await bucket_mgr.archive(bucket_id) is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert unchanged["metadata"]["type"] == "dynamic"
    assert Path(unchanged["path"]) == source_path
    assert source_path.read_bytes() == source_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [source_path]


@pytest.mark.asyncio
async def test_restore_archived_pin_is_one_atomic_storage_transition(bucket_mgr):
    """恢复清 pin，但保留 protection，并同时刷新活跃时间与唯一真源。"""
    bucket_id = await bucket_mgr.create(
        content="restore lifecycle state atomically",
        domain=["migration-domain"],
        pinned=True,
    )
    active_path = Path((await bucket_mgr.get(bucket_id))["path"])
    stale_last_active = "2000-01-01T00:00:00+00:00"
    stale_post = frontmatter.load(active_path)
    # 模拟历史脏数据：新写入会拒绝 pinned+protected，
    # 但恢复流程不能抹掉旧记忆已经持久化的保护状态。
    stale_post["protected"] = True
    stale_post["last_active"] = stale_last_active
    active_path.write_text(frontmatter.dumps(stale_post), encoding="utf-8")

    assert await bucket_mgr.archive(bucket_id) is True
    archived = await bucket_mgr.get_including_archive(bucket_id)
    archived_path = Path(archived["path"])

    assert archived["metadata"]["type"] == "archived"
    assert archived["metadata"]["pinned"] is True
    assert archived["metadata"]["protected"] is True
    assert archived["metadata"]["last_active"] == stale_last_active
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]

    result = await bucket_mgr.restore_archived(bucket_id)
    restored = await bucket_mgr.get(bucket_id)
    restored_path = Path(restored["path"])

    assert result == {"ok": True, "restored": bucket_id, "type": "permanent"}
    assert restored["metadata"]["type"] == "permanent"
    assert restored["metadata"].get("pinned", False) is False
    assert restored["metadata"]["protected"] is True
    assert restored["metadata"]["last_active"] != stale_last_active
    assert restored_path.exists()
    assert not archived_path.exists()
    assert _bucket_files(bucket_mgr, bucket_id) == [restored_path]


@pytest.mark.asyncio
async def test_restore_archived_protected_anchor_requires_atomic_unprotect(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="dirty archived protection must be resolved atomically",
        domain=["migration-domain"],
        protected=True,
    )
    active = await bucket_mgr.get(bucket_id)
    active_path = Path(active["path"])
    dirty_post = frontmatter.load(active_path)
    dirty_post["anchor"] = True
    active_path.write_text(frontmatter.dumps(dirty_post), encoding="utf-8")

    assert await bucket_mgr.archive(bucket_id) is True
    archived = await bucket_mgr.get_including_archive(bucket_id)
    archived_path = Path(archived["path"])

    rejected = await bucket_mgr.restore_archived(bucket_id)
    missing_importance = await bucket_mgr.restore_archived(
        bucket_id,
        protected_override=False,
    )

    assert rejected == {
        "ok": False,
        "error": "incompatible_protected_anchor",
    }
    assert missing_importance == {
        "ok": False,
        "error": "missing_importance_override",
    }
    assert archived_path.exists()
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]

    result = await bucket_mgr.restore_archived(
        bucket_id,
        protected_override=False,
        importance_override=7,
    )
    restored = await bucket_mgr.get(bucket_id)

    assert result == {"ok": True, "restored": bucket_id, "type": "dynamic"}
    assert restored["metadata"]["anchor"] is True
    assert restored["metadata"].get("protected", False) is False
    assert restored["metadata"]["pinned"] is False
    assert restored["metadata"]["importance"] == 7
    assert restored["metadata"]["type"] == "dynamic"


@pytest.mark.asyncio
async def test_restore_move_failure_keeps_archived_pin_and_single_source(
    bucket_mgr,
    monkeypatch,
):
    """恢复提交失败时不得提前清 pin，也不得留下活跃区副本。"""
    import bucket_manager as bucket_manager_module

    bucket_id = await bucket_mgr.create(
        content="restore failure preserves archived state",
        domain=["migration-domain"],
        pinned=True,
    )
    assert await bucket_mgr.archive(bucket_id) is True
    archived = await bucket_mgr.get_including_archive(bucket_id)
    archived_path = Path(archived["path"])
    archived_bytes = archived_path.read_bytes()
    real_remove = bucket_manager_module.os.remove

    def fail_archived_source_removal(path, *_args, **_kwargs):
        if Path(path) == archived_path:
            raise OSError("simulated restore source removal failure")
        return real_remove(path, *_args, **_kwargs)

    monkeypatch.setattr(
        bucket_manager_module.os,
        "remove",
        fail_archived_source_removal,
    )

    result = await bucket_mgr.restore_archived(bucket_id)

    assert result["ok"] is False
    assert result["error"].startswith("restore_failed:")
    unchanged = frontmatter.load(archived_path)
    assert unchanged["type"] == "archived"
    assert unchanged["pinned"] is True
    assert archived_path.read_bytes() == archived_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]


@pytest.mark.asyncio
async def test_archived_letter_compat_restore_preserves_verbatim_state(bucket_mgr):
    """历史 Letter 兼容恢复只改类型与物理位置，不伪造一次活跃。"""
    bucket_id = await bucket_mgr.create(
        content="historical letter body",
        tags=["__letter__", "private"],
        importance=10,
        domain=["letter"],
        bucket_type="letter",
        source_tool="letter",
        lock_type="timed",
        unlock_date="2099-12-31T00:00:00+00:00",
        locked_by="ai",
        writer_name="Ombre",
    )
    assert await bucket_mgr.update(
        bucket_id,
        author="Ombre",
        title="原始标题",
        letter_date="2025-01-02",
    ) is True
    assert await bucket_mgr.archive(bucket_id) is True
    archived = await bucket_mgr.get_including_archive(bucket_id)
    archived_path = Path(archived["path"])
    archived_post = frontmatter.load(archived_path)
    archived_post["last_active"] = "2000-01-01T00:00:00+00:00"
    archived_path.write_text(frontmatter.dumps(archived_post), encoding="utf-8")
    archived = await bucket_mgr.get_including_archive(bucket_id)
    before_metadata = dict(archived["metadata"])

    result = await bucket_mgr.recover_archived_letter(bucket_id)

    assert result == {"ok": True, "id": bucket_id, "reason": "restored"}
    restored = await bucket_mgr.get(bucket_id)
    assert restored["content"] == "historical letter body"
    assert restored["metadata"]["type"] == "letter"
    for field, value in before_metadata.items():
        if field != "type":
            assert restored["metadata"].get(field) == value
    restored_path = Path(restored["path"])
    assert restored_path.parent == Path(bucket_mgr.letter_dir) / "history"
    assert not archived_path.exists()
    assert _bucket_files(bucket_mgr, bucket_id) == [restored_path]

    # 已完成的同一迁移可安全重试，且不再次写盘。
    restored_bytes = restored_path.read_bytes()
    repeated = await bucket_mgr.recover_archived_letter(bucket_id)
    assert repeated == {"ok": True, "id": bucket_id, "reason": "already_restored"}
    assert restored_path.read_bytes() == restored_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dirty_fields", "reason"),
    [
        ({"deleted_at": "2026-01-01T00:00:00+00:00"}, "terminal_state"),
        ({"tombstone": True}, "terminal_state"),
        ({"tombstoned_at": "2026-01-01T00:00:00+00:00"}, "terminal_state"),
        ({"erasure_mode": "tombstone_only"}, "terminal_state"),
        ({"pinned": True}, "protected_state"),
        ({"protected": True}, "protected_state"),
        ({"anchor": True}, "protected_state"),
    ],
)
async def test_archived_letter_compat_restore_rejects_terminal_and_protected_state(
    bucket_mgr,
    dirty_fields,
    reason,
):
    bucket_id = await bucket_mgr.create(
        content="must remain archived",
        tags=["__letter__"],
        domain=["letter"],
        bucket_type="letter",
        source_tool="letter",
    )
    assert await bucket_mgr.archive(bucket_id) is True
    archived = await bucket_mgr.get_including_archive(bucket_id)
    archived_path = Path(archived["path"])
    post = frontmatter.load(archived_path)
    for field, value in dirty_fields.items():
        post[field] = value
    archived_path.write_text(frontmatter.dumps(post), encoding="utf-8")
    before = archived_path.read_bytes()

    result = await bucket_mgr.recover_archived_letter(bucket_id)

    assert result == {"ok": False, "id": bucket_id, "reason": reason}
    assert archived_path.read_bytes() == before
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]


@pytest.mark.asyncio
async def test_archived_letter_compat_restore_requires_strong_marker_and_unique_source(
    bucket_mgr,
):
    ambiguous_id = await bucket_mgr.create(
        content="domain alone is ambiguous",
        domain=["letter"],
        bucket_type="dynamic",
    )
    assert await bucket_mgr.archive(ambiguous_id) is True
    ambiguous_path = Path((await bucket_mgr.get_including_archive(ambiguous_id))["path"])
    ambiguous_before = ambiguous_path.read_bytes()

    ambiguous = await bucket_mgr.recover_archived_letter(ambiguous_id)

    assert ambiguous == {
        "ok": False,
        "id": ambiguous_id,
        "reason": "ambiguous_letter_marker",
    }
    assert ambiguous_path.read_bytes() == ambiguous_before

    duplicate_id = await bucket_mgr.create(
        content="canonical archived source",
        tags=["__letter__"],
        domain=["letter"],
        bucket_type="letter",
        source_tool="letter",
    )
    assert await bucket_mgr.archive(duplicate_id) is True
    archived_path = Path((await bucket_mgr.get_including_archive(duplicate_id))["path"])
    duplicate_path = Path(bucket_mgr.dynamic_dir) / "duplicate.md"
    duplicate_path.write_bytes(archived_path.read_bytes())

    duplicate = await bucket_mgr.recover_archived_letter(duplicate_id)

    assert duplicate == {
        "ok": False,
        "id": duplicate_id,
        "reason": "duplicate_source",
    }
    assert sorted(_bucket_files(bucket_mgr, duplicate_id)) == sorted(
        [archived_path, duplicate_path]
    )


@pytest.mark.asyncio
async def test_archived_letter_compat_restore_rolls_back_failed_source_removal(
    bucket_mgr,
    monkeypatch,
):
    import bucket_manager as bucket_manager_module

    bucket_id = await bucket_mgr.create(
        content="compat restore rollback",
        tags=["__letter__"],
        domain=["letter"],
        bucket_type="letter",
        source_tool="letter",
    )
    assert await bucket_mgr.archive(bucket_id) is True
    archived_path = Path((await bucket_mgr.get_including_archive(bucket_id))["path"])
    archived_bytes = archived_path.read_bytes()
    real_remove = bucket_manager_module.os.remove

    def fail_archived_source_removal(path, *_args, **_kwargs):
        if Path(path) == archived_path:
            raise OSError("simulated compat restore source removal failure")
        return real_remove(path, *_args, **_kwargs)

    monkeypatch.setattr(bucket_manager_module.os, "remove", fail_archived_source_removal)

    result = await bucket_mgr.recover_archived_letter(bucket_id)

    assert result == {"ok": False, "id": bucket_id, "reason": "commit_failed"}
    assert archived_path.read_bytes() == archived_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]


@pytest.mark.asyncio
async def test_archived_letter_compat_restore_never_overwrites_target_collision(
    bucket_mgr,
):
    bucket_id = await bucket_mgr.create(
        content="archived source stays authoritative",
        tags=["__letter__"],
        domain=["letter"],
        bucket_type="letter",
        source_tool="letter",
    )
    assert await bucket_mgr.archive(bucket_id) is True
    archived_path = Path((await bucket_mgr.get_including_archive(bucket_id))["path"])
    archived_bytes = archived_path.read_bytes()
    target_path = Path(bucket_mgr.letter_dir) / "history" / archived_path.name
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        frontmatter.dumps(frontmatter.Post("unrelated letter", id="other-id")),
        encoding="utf-8",
    )
    target_bytes = target_path.read_bytes()

    result = await bucket_mgr.recover_archived_letter(bucket_id)

    assert result == {"ok": False, "id": bucket_id, "reason": "commit_failed"}
    assert archived_path.read_bytes() == archived_bytes
    assert target_path.read_bytes() == target_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [archived_path]


@pytest.mark.asyncio
async def test_soft_delete_move_failure_rolls_back_tombstone_and_copy(
    bucket_mgr,
    monkeypatch,
):
    import bucket_manager as bucket_manager_module

    bucket_id = await bucket_mgr.create(
        content="soft delete failure must keep one live source",
        domain=["migration-domain"],
    )
    before = await bucket_mgr.get(bucket_id)
    source_path = Path(before["path"])
    source_bytes = source_path.read_bytes()
    real_remove = bucket_manager_module.os.remove

    def fail_source_removal(path, *_args, **_kwargs):
        if Path(path) == source_path:
            raise OSError("simulated soft-delete source removal failure")
        return real_remove(path, *_args, **_kwargs)

    monkeypatch.setattr(bucket_manager_module.os, "remove", fail_source_removal)

    assert await bucket_mgr.delete(bucket_id) is False

    unchanged = await bucket_mgr.get(bucket_id)
    assert unchanged is not None
    assert "deleted_at" not in unchanged["metadata"]
    assert "tombstone" not in unchanged["metadata"]
    assert Path(unchanged["path"]) == source_path
    assert source_path.read_bytes() == source_bytes
    assert _bucket_files(bucket_mgr, bucket_id) == [source_path]


@pytest.mark.asyncio
async def test_legacy_scalar_domain_uses_the_whole_value_for_migration(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="legacy scalar domain",
        domain=["temporary-domain"],
    )
    source_path = Path((await bucket_mgr.get(bucket_id))["path"])
    post = frontmatter.load(source_path)
    post["domain"] = "legacy-work"
    source_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    assert await bucket_mgr.update(bucket_id, name="edited legacy bucket") is True

    updated = await bucket_mgr.get(bucket_id)
    updated_path = Path(updated["path"])
    assert updated_path.parent == Path(bucket_mgr.dynamic_dir) / "legacy-work"
    assert updated_path.parent.name != "l"
    assert _bucket_files(bucket_mgr, bucket_id) == [updated_path]
