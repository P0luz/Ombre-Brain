import asyncio
import os
from pathlib import Path

import frontmatter
import pytest
from starlette.responses import JSONResponse

from embedding_outbox import STATE_PREPARED, load_slot, prepare_upsert
from ombrebrain.eventsourcing.footprint import system_origin
from tools._common import restore_archived_letters
from web import _shared as sh, meta


windows_safe_commit_only = pytest.mark.skipif(
    os.name != "nt", reason="historical Letter safe commit is Windows-only"
)


def rewrite(path: Path, **updates):
    post = frontmatter.load(path)
    for key, value in updates.items():
        if value is None:
            post.metadata.pop(key, None)
        else:
            post[key] = value
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


async def archived_letter(bucket_mgr, content="historical", **updates):
    bucket_id = await bucket_mgr.create(
        content=content, tags=["__letter__", "owner:cheng"], domain=["letter"],
        bucket_type="letter", source_tool="letter",
        footprint_origin=system_origin(),
    )
    assert await bucket_mgr.archive(bucket_id)
    rows = await bucket_mgr.list_all(include_archive=True, fresh=True)
    path = Path(next(row["path"] for row in rows if row["id"] == bucket_id))
    if updates:
        rewrite(path, **updates)
    return bucket_id, path


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_audit_is_zero_write_and_restore_preserves_public_fields(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr)
    rewrite(source, lock_type=None, unlock_date=None, locked_by_principal=None)
    before = source.read_bytes()
    audit = await restore_archived_letters(bucket_mgr)
    assert audit["candidate_ids"] == [bucket_id]
    assert source.read_bytes() == before

    result = await restore_archived_letters(
        bucket_mgr, ids=[bucket_id],
        revisions={bucket_id: audit["candidate_revisions"][bucket_id]}, apply=True,
    )
    assert result["restored_count"] == 1
    target = Path((await bucket_mgr.get(bucket_id))["path"])
    restored = frontmatter.load(target)
    original = frontmatter.loads(before.decode("utf-8"))
    assert target.parent == Path(bucket_mgr.letter_dir) / "history"
    assert restored.content == original.content
    assert restored.get("last_active") == original.get("last_active")
    assert restored.get("type") == "letter"
    for field in ("lock_type", "unlock_date", "locked_by_principal"):
        assert field not in restored.metadata


@pytest.mark.asyncio
@windows_safe_commit_only
@pytest.mark.parametrize("principal", ["cheng", "huaiyin", "huaiyin_cc", "human"])
async def test_restore_preserves_four_principal_lock(bucket_mgr, principal):
    bucket_id, _source = await archived_letter(
        bucket_mgr, lock_type="permanent", unlock_date="9999-12-31",
        locked_by_principal=principal,
    )
    outcome = await bucket_mgr.recover_archived_letter(bucket_id)
    assert outcome["reason"] == "restored"
    post = frontmatter.load((await bucket_mgr.get(bucket_id))["path"])
    assert (post["lock_type"], post["unlock_date"], post["locked_by_principal"]) == (
        "permanent", "9999-12-31", principal,
    )


@pytest.mark.asyncio
async def test_audit_excludes_weak_terminal_protected_duplicate_and_malformed(bucket_mgr):
    partial, ppath = await archived_letter(bucket_mgr)
    rewrite(ppath, lock_type="permanent", unlock_date=None, locked_by_principal=None)
    weak, wpath = await archived_letter(bucket_mgr)
    rewrite(wpath, source_tool=None, tags=[], domain=["letter"])
    terminal, tpath = await archived_letter(bucket_mgr)
    rewrite(tpath, tombstone=True)
    protected, xpath = await archived_letter(bucket_mgr)
    rewrite(xpath, protected=True)
    duplicate, dpath = await archived_letter(bucket_mgr)
    clone = Path(bucket_mgr.dynamic_dir) / "dup" / f"copy_{duplicate}.md"
    clone.parent.mkdir(parents=True)
    clone.write_bytes(dpath.read_bytes())
    malformed, mpath = await archived_letter(bucket_mgr)
    rewrite(mpath, protected=["false"])

    audit = await restore_archived_letters(bucket_mgr)
    reasons = {row["id"]: row["reason"] for row in audit["exclusions"]}
    assert reasons == {
        partial: "invalid_lock_state", weak: "ambiguous_letter_marker",
        terminal: "terminal_state", protected: "protected_state",
        duplicate: "duplicate_source", malformed: "invalid_terminal_marker",
    }


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_collision_leaves_one_unchanged_truth(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr)
    target = Path(bucket_mgr.letter_dir) / "history" / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("occupied", encoding="utf-8")
    assert (await bucket_mgr.recover_archived_letter(bucket_id))["reason"] == "target_collision"
    assert source.exists() and target.read_text(encoding="utf-8") == "occupied"


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_concurrent_restore_commits_once(bucket_mgr):
    bucket_id, _source = await archived_letter(bucket_mgr)
    outcomes = await asyncio.gather(
        bucket_mgr.recover_archived_letter(bucket_id),
        bucket_mgr.recover_archived_letter(bucket_id),
    )
    assert sorted(row["reason"] for row in outcomes) == ["already_restored", "restored"]


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_restore_changes_only_unique_type_scalar(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr)
    raw = source.read_bytes()
    expected = raw.replace(b"type: archived", b"type: letter", 1)
    assert (await bucket_mgr.recover_archived_letter(bucket_id))["reason"] == "restored"
    target = Path((await bucket_mgr.get(bucket_id))["path"])
    assert target.read_bytes() == expected


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_stale_audit_revision_conflicts_without_write(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr)
    audit = await restore_archived_letters(bucket_mgr)
    source.write_bytes(source.read_bytes() + b"\n")
    before = source.read_bytes()
    result = await restore_archived_letters(
        bucket_mgr, ids=[bucket_id],
        revisions={bucket_id: audit["candidate_revisions"][bucket_id]}, apply=True,
    )
    assert result["results"] == [{"id": bucket_id, "reason": "revision_conflict"}]
    assert source.read_bytes() == before


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_source_path_swap_commits_pinned_original_not_decoy(bucket_mgr, monkeypatch):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    original = source.read_bytes()
    decoy = original + b"\nattacker-decoy"
    held_source_path = Path(str(source) + ".held")
    actual = wsr._nt_rename_no_replace

    def swap_then_commit(source_handle, target_handle, target_name):
        __import__("os").replace(source, held_source_path)
        source.write_bytes(decoy)
        actual(source_handle, target_handle, target_name)

    monkeypatch.setattr(wsr, "_nt_rename_no_replace", swap_then_commit)
    outcome = await bucket_mgr.recover_archived_letter(bucket_id)
    assert outcome["reason"] == "ambiguous_state"
    target = Path(bucket_mgr.letter_dir) / "history" / source.name
    assert target.read_bytes() == original.replace(b"type: archived", b"type: letter", 1)
    assert source.read_bytes() == decoy
    assert not held_source_path.exists()


@pytest.mark.asyncio
async def test_archive_revision_rejects_change_while_waiting(bucket_mgr):
    bucket_id = await bucket_mgr.create(
        content="live", bucket_type="letter", tags=["owner:cheng"],
        footprint_origin=system_origin()
    )
    path = Path((await bucket_mgr.get(bucket_id))["path"])
    async with bucket_mgr._bucket_turn(bucket_id):
        pending = asyncio.create_task(bucket_mgr.archive(bucket_id))
        await asyncio.sleep(0.05)
        path.write_bytes(path.read_bytes() + b"\nchanged-before-commit")
    assert await pending is False
    assert path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["archive", "delete"])
async def test_lifecycle_absent_to_present_is_rejected(bucket_mgr, operation):
    bucket_id = f"late-{operation}-arrival"
    path = Path(bucket_mgr.dynamic_dir) / f"{bucket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        "arrived while waiting",
        id=bucket_id,
        name=bucket_id,
        type="dynamic",
        domain=["test"],
    )

    async with bucket_mgr._bucket_turn(bucket_id):
        pending = asyncio.create_task(getattr(bucket_mgr, operation)(bucket_id))
        await asyncio.sleep(0.05)
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    assert await pending is False
    assert path.exists()
    assert frontmatter.load(path).get("type") == "dynamic"


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_crash_intermediate_does_not_claim_unowned_outbox_intent(
    bucket_mgr, tmp_path
):
    bucket_id, source = await archived_letter(bucket_mgr)
    bucket_mgr.embedding_outbox_root = str(tmp_path / "outbox")
    post = frontmatter.load(source)
    before = prepare_upsert(
        bucket_mgr.embedding_outbox_root, bucket_id, post.content, post.content
    )
    history = Path(bucket_mgr.letter_dir) / "history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / source.name
    source.rename(target)

    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["derived_state"] == "applied"
    after = load_slot(bucket_mgr.embedding_outbox_root, bucket_id)
    assert after.state == STATE_PREPARED
    assert (after.intent_id, after.generation) == (before.intent_id, before.generation)


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_ambiguous_type_rejected_before_move(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr)
    source.write_bytes(
        source.read_bytes().replace(b"type: archived", b"type: archived\ntype: archived")
    )
    target = Path(bucket_mgr.letter_dir) / "history" / source.name
    assert (await bucket_mgr.recover_archived_letter(bucket_id))["reason"] == "invalid_archived_type"
    assert source.exists() and not target.exists()


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_revision_to_handle_rebind_is_rejected(bucket_mgr, monkeypatch, tmp_path):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    audit = await restore_archived_letters(bucket_mgr)
    original = source.read_bytes()
    moved = tmp_path / "original-outside-vault.md"
    decoy = original.replace(b"name: ", b"name: ATTACKER-", 1)
    actual = wsr.safe_transform_rename_no_replace

    def rebind_before_pinned_commit(*args, **kwargs):
        __import__("os").replace(source, moved)
        source.write_bytes(decoy)
        return actual(*args, **kwargs)

    monkeypatch.setattr(wsr, "safe_transform_rename_no_replace", rebind_before_pinned_commit)
    outcome = await bucket_mgr.recover_archived_letter(
        bucket_id, expected_revision=audit["candidate_revisions"][bucket_id]
    )

    assert outcome["reason"] == "revision_conflict"
    assert moved.read_bytes() == original
    assert source.read_bytes() == decoy
    assert not (Path(bucket_mgr.letter_dir) / "history" / source.name).exists()


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_explicit_yaml_string_tag_cannot_redirect_rewrite_into_body(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr, content="body\ntype: archived")
    raw = source.read_bytes().replace(b"type: archived", b"type: !!str archived", 1)
    source.write_bytes(raw)

    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "restored"
    target = Path((await bucket_mgr.get(bucket_id))["path"])
    restored = frontmatter.load(target)
    assert restored.get("type") == "letter"
    assert restored.content == "body\ntype: archived"


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_precommit_failure_creates_no_noop_embedding_intent(
    bucket_mgr, monkeypatch, tmp_path
):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    bucket_mgr.embedding_outbox_root = str(tmp_path / "outbox")

    def fail_before_authority(*_args, **_kwargs):
        raise OSError("injected precommit failure")

    monkeypatch.setattr(wsr, "safe_transform_rename_no_replace", fail_before_authority)
    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "commit_failed"
    assert source.exists()
    assert frontmatter.load(source).get("type") == "archived"
    assert load_slot(bucket_mgr.embedding_outbox_root, bucket_id) is None


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_crash_intermediate_is_auditable_and_applyable(bucket_mgr):
    bucket_id, source = await archived_letter(bucket_mgr)
    history = Path(bucket_mgr.letter_dir) / "history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / source.name
    source.rename(target)

    audit = await restore_archived_letters(bucket_mgr)

    assert bucket_id in audit["candidate_ids"]
    revision = audit["candidate_revisions"][bucket_id]
    result = await restore_archived_letters(
        bucket_mgr,
        ids=[bucket_id],
        revisions={bucket_id: revision},
        apply=True,
    )
    assert result["results"] == [{"id": bucket_id, "reason": "restored"}]
    assert frontmatter.load(target).get("type") == "letter"


@pytest.mark.asyncio
async def test_non_windows_safe_commit_fails_closed(bucket_mgr, monkeypatch):
    import historical_letter_restore as restore

    bucket_id, source = await archived_letter(bucket_mgr)
    monkeypatch.setattr(restore, "WINDOWS_SAFE_COMMIT", False)

    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "unsupported_safe_commit"
    assert source.exists()
    assert frontmatter.load(source).get("type") == "archived"


@pytest.mark.asyncio
async def test_audit_rejects_source_with_multiple_hardlinks(bucket_mgr, tmp_path):
    bucket_id, source = await archived_letter(bucket_mgr)
    alias = tmp_path / "outside-alias.md"
    __import__("os").link(source, alias)

    audit = await restore_archived_letters(bucket_mgr)

    assert bucket_id not in audit["candidate_ids"]
    assert {row["id"]: row["reason"] for row in audit["exclusions"]}[bucket_id] == (
        "unsupported_safe_commit"
    )
    assert source.exists() and alias.exists()


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_apply_rejects_hardlink_added_after_audit(bucket_mgr, tmp_path):
    bucket_id, source = await archived_letter(bucket_mgr)
    audit = await restore_archived_letters(bucket_mgr)
    alias = tmp_path / "outside-alias.md"
    __import__("os").link(source, alias)

    result = await restore_archived_letters(
        bucket_mgr,
        ids=[bucket_id],
        revisions={bucket_id: audit["candidate_revisions"][bucket_id]},
        apply=True,
    )

    assert result["results"] == [
        {"id": bucket_id, "reason": "revision_conflict"}
    ]
    assert source.exists() and alias.exists()


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_hardlink_added_at_rename_boundary_returns_ambiguous(
    bucket_mgr, monkeypatch, tmp_path
):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    alias = tmp_path / "boundary-alias.md"
    actual = wsr._nt_rename_no_replace

    def link_then_rename(source_handle, target_handle, target_name):
        __import__("os").link(source, alias)
        actual(source_handle, target_handle, target_name)

    monkeypatch.setattr(wsr, "_nt_rename_no_replace", link_then_rename)
    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "ambiguous_state"
    assert alias.exists()
    assert frontmatter.load(alias).get("type") == "archived"


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_hardlink_added_before_target_replace_returns_ambiguous(
    bucket_mgr, monkeypatch, tmp_path
):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    target = Path(bucket_mgr.letter_dir) / "history" / source.name
    alias = tmp_path / "post-rename-alias.md"
    actual = wsr._atomic_replace_relative

    def link_then_replace(directory_handle, target_name, data):
        __import__("os").link(target, alias)
        actual(directory_handle, target_name, data)

    monkeypatch.setattr(wsr, "_atomic_replace_relative", link_then_replace)
    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "ambiguous_state"
    assert frontmatter.load(target).get("type") == "letter"
    assert frontmatter.load(alias).get("type") == "archived"


@pytest.mark.asyncio
@windows_safe_commit_only
async def test_crash_intermediate_post_commit_hardlink_is_ambiguous(
    bucket_mgr, monkeypatch, tmp_path
):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    history = Path(bucket_mgr.letter_dir) / "history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / source.name
    source.rename(target)
    alias = tmp_path / "crash-intermediate-alias.md"
    actual = wsr._atomic_replace_relative

    def link_then_replace(directory_handle, target_name, data):
        __import__("os").link(target, alias)
        actual(directory_handle, target_name, data)

    monkeypatch.setattr(wsr, "_atomic_replace_relative", link_then_replace)
    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "ambiguous_state"
    assert frontmatter.load(target).get("type") == "letter"
    assert frontmatter.load(alias).get("type") == "archived"


@pytest.mark.asyncio
@windows_safe_commit_only
@pytest.mark.parametrize("crash_intermediate", [False, True], ids=["ordinary", "crash"])
async def test_post_replace_info_failure_is_ambiguous(
    bucket_mgr, monkeypatch, crash_intermediate
):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    history = Path(bucket_mgr.letter_dir) / "history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / source.name
    if crash_intermediate:
        source.rename(target)
    actual_replace = wsr._atomic_replace_relative
    actual_info = wsr._file_info
    replacement_complete = False

    def replace_then_mark(*args, **kwargs):
        nonlocal replacement_complete
        actual_replace(*args, **kwargs)
        replacement_complete = True

    def fail_final_information_query(handle):
        if replacement_complete:
            raise OSError("injected post-replacement information-query failure")
        return actual_info(handle)

    monkeypatch.setattr(wsr, "_atomic_replace_relative", replace_then_mark)
    monkeypatch.setattr(wsr, "_file_info", fail_final_information_query)

    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "ambiguous_state"
    assert frontmatter.load(target).get("type") == "letter"


@pytest.mark.asyncio
@windows_safe_commit_only
@pytest.mark.parametrize("crash_intermediate", [False, True], ids=["ordinary", "crash"])
async def test_replacement_hardlink_at_rename_boundary_is_ambiguous(
    bucket_mgr, monkeypatch, tmp_path, crash_intermediate
):
    import windows_safe_rename as wsr

    bucket_id, source = await archived_letter(bucket_mgr)
    history = Path(bucket_mgr.letter_dir) / "history"
    history.mkdir(parents=True, exist_ok=True)
    target = history / source.name
    if crash_intermediate:
        source.rename(target)
    alias = tmp_path / f"replacement-{'crash' if crash_intermediate else 'ordinary'}.md"
    actual_rename = wsr._nt_rename
    linked = False

    def link_replacement_then_rename(source_handle, target_handle, target_name, *, replace):
        nonlocal linked
        if replace and not linked:
            temporary = next(history.glob(".ob-restore-*.tmp"))
            __import__("os").link(temporary, alias)
            linked = True
        actual_rename(source_handle, target_handle, target_name, replace=replace)

    monkeypatch.setattr(wsr, "_nt_rename", link_replacement_then_rename)

    outcome = await bucket_mgr.recover_archived_letter(bucket_id)

    assert outcome["reason"] == "ambiguous_state"
    assert linked and alias.exists()
    assert frontmatter.load(target).get("type") == "letter"
    assert frontmatter.load(alias).get("type") == "letter"


class MCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorate(function):
            for method in methods:
                self.routes[(method, path)] = function
            return function
        return decorate


class Request:
    def __init__(self, method, body=None, broken=False):
        self.method, self.body, self.broken = method, body, broken

    async def json(self):
        if self.broken:
            raise ValueError("bad json")
        return self.body


@pytest.mark.asyncio
async def test_route_auth_json_dedupe_revision_and_no_store(monkeypatch):
    calls = []

    async def fake(*_args, **kwargs):
        calls.append(kwargs)
        return {"candidate_count": 0, "candidate_ids": [], "exclusions": []}

    monkeypatch.setattr(sh, "bucket_mgr", object())
    monkeypatch.setattr("tools._common.restore_archived_letters", fake)
    denied = JSONResponse({}, status_code=401)
    monkeypatch.setattr(sh, "_require_auth", lambda _request: denied)
    mcp = MCP()
    meta.register(mcp)
    route = "/api/maintenance/restore-archived-letters"
    response = await mcp.routes[("GET", route)](Request("GET"))
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert calls == []

    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    mcp = MCP()
    meta.register(mcp)
    handler = mcp.routes[("POST", route)]
    assert (await handler(Request("POST", broken=True))).status_code == 400
    revisions = {"a": "a" * 64, "b": "b" * 64}
    response = await handler(Request("POST", {"ids": [" a ", "a", "b"], "revisions": revisions}))
    assert response.headers["Cache-Control"] == "no-store"
    assert calls[-1] == {"ids": ["a", "b"], "revisions": revisions, "apply": True}
