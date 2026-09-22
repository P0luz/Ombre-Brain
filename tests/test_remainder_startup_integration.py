"""Tests for remainder startup recovery and health integration."""

from __future__ import annotations

import json
import importlib
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
import remainder_integration as ri

import remainder_sidecar as rs
from remainder_sidecar import (
    STATE_CONFLICT,
    STATE_PREPARED,
    prepare_remainder,
    commit_remainder,
    load_sidecar_or_none,
    RemainderSidecarError,
    RemainderConflictError,
    RemainderQuarantineError,
)
from remainder_integration import (
    build_bucket_inventory,
    recover_remainders_before_startup,
    runtime_status,
    mark_unresolved,
    clear_unresolved,
    _set_wiring,
    _set_startup,
    _status,
)


_META_CHENG = {"tags": ["owner:cheng"]}


def _write_bucket(root: Path, subdir: str, bucket_id: str, content: str) -> Path:
    d = root / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{bucket_id}.md"
    p.write_text(
        f"---\nid: {bucket_id}\ntags:\n  - owner:cheng\n---\n{content}",
        encoding="utf-8",
    )
    return p


def _write_prepared_sidecar(
    root: Path, bucket_id: str, old_text: str, new_text: str,
    merged_text: str,
) -> rs.RemainderEntry:
    entry, gen = prepare_remainder(
        str(root), bucket_id,
        old_text=old_text, new_text=new_text,
        merged_text=merged_text,
        merge_method="llm",
        metadata=_META_CHENG,
        current_content=old_text,
    )
    return entry


class TestBucketInventory(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_empty_root_returns_empty(self) -> None:
        inv = build_bucket_inventory(self.root)
        self.assertEqual(inv, {})

    def test_single_bucket(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        inv = build_bucket_inventory(self.root)
        self.assertIn("b1", inv)
        self.assertEqual(inv["b1"], "hello")

    def test_multiple_dirs(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "a")
        _write_bucket(self.root, "dynamic", "b2", "b")
        _write_bucket(self.root, "feel", "b3", "c")
        inv = build_bucket_inventory(self.root)
        self.assertEqual(len(inv), 3)

    def test_duplicate_id_raises(self) -> None:
        _write_bucket(self.root, "permanent", "dup", "a")
        _write_bucket(self.root, "dynamic", "dup", "b")
        with self.assertRaises(RemainderSidecarError):
            build_bucket_inventory(self.root)

    def test_non_regular_file_raises(self) -> None:
        d = self.root / "permanent"
        d.mkdir(parents=True)
        sub = d / "badlink.md"
        if os.name == "nt":
            sub.mkdir()
        else:
            os.symlink("/dev/null", str(sub))
        with self.assertRaises(RemainderSidecarError):
            build_bucket_inventory(self.root)

    def test_invalid_utf8_raises(self) -> None:
        d = self.root / "permanent"
        d.mkdir(parents=True)
        p = d / "bad.md"
        p.write_bytes(b"---\nid: bad\n---\n\xff\xfe")
        with self.assertRaises(RemainderSidecarError):
            build_bucket_inventory(self.root)

    def test_missing_frontmatter_raises(self) -> None:
        d = self.root / "permanent"
        d.mkdir(parents=True)
        p = d / "plain.md"
        p.write_text("plain body without frontmatter", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError) as ctx:
            build_bucket_inventory(self.root)
        self.assertIn("frontmatter", str(ctx.exception).lower())

    def test_missing_id_in_frontmatter_raises(self) -> None:
        d = self.root / "permanent"
        d.mkdir(parents=True)
        p = d / "noid.md"
        p.write_text("---\ntags:\n  - owner:cheng\n---\nbody", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError) as ctx:
            build_bucket_inventory(self.root)
        self.assertIn("id", str(ctx.exception).lower())

    # blocker 1: existing-but-not-directory root must raise, not skip
    def test_authoritative_root_is_file_raises(self) -> None:
        p = self.root / "permanent"
        p.write_text("I am a file", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError) as ctx:
            build_bucket_inventory(self.root)
        self.assertIn("not a regular directory", str(ctx.exception).lower())

    def test_authoritative_root_junction_raises(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-only test")
        import subprocess
        target = self.root / "real_permanent"
        target.mkdir()
        _write_bucket(target, "", "b1", "hello")
        link = self.root / "permanent"
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False, capture_output=True,
        )
        if r.returncode != 0 or not link.exists():
            self.skipTest("Junction creation requires elevated privileges")
        with self.assertRaises(RemainderSidecarError):
            build_bucket_inventory(self.root)

    def test_nested_reparse_subdir_raises(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-only test")
        import subprocess
        d = self.root / "permanent"
        d.mkdir(parents=True)
        _write_bucket(self.root, "permanent", "b1", "ok")
        target = self.root / "real_sub"
        target.mkdir()
        link = d / "nested_link"
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False, capture_output=True,
        )
        if r.returncode != 0 or not link.exists():
            self.skipTest("Junction creation requires elevated privileges")
        with self.assertRaises(RemainderSidecarError):
            build_bucket_inventory(self.root)


class TestStartupRecovery(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def tearDown(self) -> None:
        self._tempdir.cleanup()
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def test_no_sidecar_zero_write(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        result = recover_remainders_before_startup(self.root)
        self.assertEqual(result["recovered"], 0)
        self.assertEqual(result["skipped_no_sidecar"], 1)
        status = runtime_status(str(self.root))
        self.assertEqual(status["wiring"], "active")
        self.assertEqual(status["startup_state"], "complete")

    # blocker 3: no-sidecar path clears unresolved
    def test_no_sidecar_clears_unresolved(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        mark_unresolved(code="commit_failed", bucket_id="old")
        self.assertEqual(_status.unresolved, 1)
        recover_remainders_before_startup(self.root)
        status = runtime_status()
        self.assertEqual(status["unresolved"], 0)

    # blocker 3: no-sidecar still validates inventory
    def test_no_sidecar_bad_markdown_still_fails(self) -> None:
        d = self.root / "permanent"
        d.mkdir(parents=True)
        p = d / "plain.md"
        p.write_text("no frontmatter", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        status = runtime_status()
        self.assertEqual(status["startup_state"], "failed")

    def test_current_equals_merged_committed(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old content",
            new_text="new content",
            merged_text=merged,
        )
        result = recover_remainders_before_startup(self.root)
        self.assertTrue(result["committed"] >= 1)
        sc = load_sidecar_or_none(str(self.root), "b1")
        if sc:
            for e in sc.entries:
                self.assertNotEqual(e.state, STATE_PREPARED)

    def test_current_equals_old_aborted(self) -> None:
        old = "old content"
        _write_bucket(self.root, "permanent", "b1", old)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text=old,
            new_text="new content",
            merged_text="merged content",
        )
        result = recover_remainders_before_startup(self.root)
        self.assertTrue(result["aborted"] >= 1)

    def test_current_neither_conflict_persisted_and_fails(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "something completely different")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old content",
            new_text="new content",
            merged_text="merged content",
        )
        with self.assertRaises(RemainderConflictError):
            recover_remainders_before_startup(self.root)
        sc = load_sidecar_or_none(str(self.root), "b1")
        self.assertIsNotNone(sc)
        conflict_entries = [e for e in sc.entries if e.state == STATE_CONFLICT]
        self.assertTrue(len(conflict_entries) >= 1)
        status = runtime_status()
        self.assertEqual(status["startup_state"], "failed")

    def test_terminal_idempotent(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old",
            new_text="new",
            merged_text=merged,
        )
        recover_remainders_before_startup(self.root)
        _set_wiring("inactive")
        _set_startup("pending")
        r2 = recover_remainders_before_startup(self.root)
        self.assertEqual(r2["recovered"], 0)

    def test_multi_bucket_preflight_all_before_any(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "merged1")
        _write_bucket(self.root, "permanent", "b2", "something else")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old1",
            new_text="new1",
            merged_text="merged1",
        )
        _write_prepared_sidecar(
            self.root, "b2",
            old_text="old2",
            new_text="new2",
            merged_text="merged2",
        )
        with self.assertRaises(RemainderConflictError):
            recover_remainders_before_startup(self.root)
        sc1 = load_sidecar_or_none(str(self.root), "b1")
        if sc1:
            prepared = [e for e in sc1.entries if e.state == STATE_PREPARED]
            self.assertTrue(len(prepared) >= 1)

    def test_missing_bucket_id_fails(self) -> None:
        _write_prepared_sidecar(
            self.root, "nonexistent",
            old_text="old",
            new_text="new",
            merged_text="merged",
        )
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_corrupt_sidecar_fails(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "b1.json").write_text("NOT JSON", encoding="utf-8")
        with self.assertRaises(Exception):
            recover_remainders_before_startup(self.root)

    def test_quarantine_not_empty_fails(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        q = rem / "quarantine"
        q.mkdir()
        (q / "evidence.json.corrupt").write_text("{}", encoding="utf-8")
        with self.assertRaises(RemainderQuarantineError):
            recover_remainders_before_startup(self.root)
        status = runtime_status()
        self.assertEqual(status["startup_state"], "failed")

    def test_unexpected_non_json_file_fails(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "unexpected.txt").write_text("wat", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    # blocker 2: .locks as file instead of dir must fail
    def test_locks_as_file_fails(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / ".locks").write_text("I am a file", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError) as ctx:
            recover_remainders_before_startup(self.root)
        self.assertIn(".locks", str(ctx.exception))

    def test_archive_fail_startup_fails(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old",
            new_text="new",
            merged_text=merged,
        )
        with patch.object(
            ri, "archive_if_needed",
            side_effect=RuntimeError("archive boom"),
        ):
            with self.assertRaises(RemainderSidecarError):
                recover_remainders_before_startup(self.root)
            status = runtime_status()
            self.assertEqual(status["startup_state"], "failed")

    def test_recovery_mid_write_restart_continues(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old",
            new_text="new",
            merged_text=merged,
        )
        with patch.object(
            ri, "archive_if_needed",
            side_effect=RuntimeError("archive fail"),
        ):
            with self.assertRaises(RemainderSidecarError):
                recover_remainders_before_startup(self.root)

        _set_wiring("inactive")
        _set_startup("pending")
        result = recover_remainders_before_startup(self.root)
        self.assertEqual(result["recovered"], 0)
        status = runtime_status()
        self.assertEqual(status["startup_state"], "complete")

    # blocker 1: dangling symlink remainder root must fail
    def test_dangling_remainder_symlink_fails(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        if os.name == "nt":
            import subprocess
            target = self.root / "nonexistent_target"
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(rem), str(target)],
                check=False, capture_output=True,
            )
            if r.returncode != 0:
                self.skipTest("Junction creation requires elevation")
        else:
            os.symlink("/nonexistent_target_dir", str(rem))
        if not (rem.is_symlink() or os.path.lexists(str(rem))):
            self.skipTest("Dangling junction not detectable via pathlib on Windows")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        status = runtime_status()
        self.assertEqual(status["startup_state"], "failed")

    # ---- R-03 final: sidecar tree validator wiring ----

    def test_emitter_lock_archive_startup_passes(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        entry, gen = prepare_remainder(
            str(self.root), "b1",
            old_text="old", new_text="new",
            merged_text=merged,
            merge_method="llm",
            metadata=_META_CHENG,
            current_content="old",
        )
        commit_remainder(str(self.root), "b1", entry.entry_id, gen)
        from remainder_sidecar import archive_if_needed
        archive_if_needed(str(self.root), "b1")
        _set_wiring("inactive")
        _set_startup("pending")
        recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "complete")

    def test_lock_arbitrary_name_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        locks = rem / ".locks"
        locks.mkdir()
        (locks / "arbitrary.lock").write_bytes(b"\0")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")

    def test_lock_single_byte_x_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        locks = rem / ".locks"
        locks.mkdir()
        name = "a" * 64 + ".lock"
        (locks / name).write_bytes(b"X")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_lock_wrong_size_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        locks = rem / ".locks"
        locks.mkdir()
        name = "a" * 64 + ".lock"
        (locks / name).write_bytes(b"\0\0")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_archive_schema_999_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        arc = rem / "archive"
        arc.mkdir()
        arc_data = {
            "schema": 999,
            "bucket_id": "b1",
            "archived_at": "2026-01-01T00:00:00Z",
            "entries": [{"dummy": True}],
        }
        fname = "b1_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text(json.dumps(arc_data), encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_archive_bucket_filename_mismatch_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        arc = rem / "archive"
        arc.mkdir()
        arc_data = {
            "schema": rs.SCHEMA_VERSION,
            "bucket_id": "b1",
            "archived_at": "2026-01-01T00:00:00Z",
            "entries": [{"dummy": True}],
        }
        fname = "wrong_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text(json.dumps(arc_data), encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_archive_bad_entry_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        arc = rem / "archive"
        arc.mkdir()
        arc_data = {
            "schema": rs.SCHEMA_VERSION,
            "bucket_id": "b1",
            "archived_at": "2026-01-01T00:00:00Z",
            "entries": [{"state": "COMMITTED"}],
        }
        fname = "b1_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text(json.dumps(arc_data), encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_archive_prepared_state_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        arc = rem / "archive"
        arc.mkdir()
        arc_data = {
            "schema": rs.SCHEMA_VERSION,
            "bucket_id": "b1",
            "archived_at": "2026-01-01T00:00:00Z",
            "entries": [{"state": "PREPARED"}],
        }
        fname = "b1_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text(json.dumps(arc_data), encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_archive_corrupt_json_rejected(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        arc = rem / "archive"
        arc.mkdir()
        fname = "b1_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text("NOT JSON", encoding="utf-8")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)

    def test_validator_error_sets_failed_no_prepared_transition(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old", new_text="new", merged_text=merged,
        )
        rem = self.root / ".remainders"
        arc = rem / "archive"
        arc.mkdir(exist_ok=True)
        fname = "b1_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text("CORRUPT", encoding="utf-8")
        sc_before = load_sidecar_or_none(str(self.root), "b1")
        self.assertIsNotNone(sc_before)
        entry_before = sc_before.entries[0]
        self.assertEqual(entry_before.state, STATE_PREPARED)
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        sc_after = load_sidecar_or_none(str(self.root), "b1")
        self.assertIsNotNone(sc_after)
        self.assertEqual(sc_after.entries[0].state, STATE_PREPARED)

    def test_validator_readonly_no_tree_change(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        locks = rem / ".locks"
        locks.mkdir()
        lock_f = locks / "arbitrary.lock"
        lock_f.write_bytes(b"\0")
        lock_bytes_before = lock_f.read_bytes()
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        self.assertEqual(lock_f.read_bytes(), lock_bytes_before)
        self.assertTrue(lock_f.exists())

    def test_monkeypatch_lock_validator_fails_startup(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        locks = rem / ".locks"
        locks.mkdir()
        import hashlib as _hl
        lock_id = _hl.sha256(
            "remainder-sidecar-b1".encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        (locks / f"{lock_id}.lock").write_bytes(b"\0")
        with patch.object(
            ri, "validate_lock_member",
            side_effect=RemainderSidecarError("injected lock fail"),
        ):
            with self.assertRaises(RemainderSidecarError):
                recover_remainders_before_startup(self.root)
            self.assertEqual(runtime_status()["startup_state"], "failed")

    def test_monkeypatch_archive_validator_fails_startup(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        arc = rem / "archive"
        arc.mkdir()
        fname = "b1_20260101T000000Z_aabbccddeeff.json"
        (arc / fname).write_text("{}", encoding="utf-8")
        with patch.object(
            ri, "validate_archive_member",
            side_effect=RemainderSidecarError("injected archive fail"),
        ):
            with self.assertRaises(RemainderSidecarError):
                recover_remainders_before_startup(self.root)
            self.assertEqual(runtime_status()["startup_state"], "failed")

    def test_startup_order_assertion(self) -> None:
        server_path = Path(__file__).resolve().parent.parent / "src" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        rem_pos = source.find("recover_remainders_before_startup")
        emb_pos = source.find("EmbeddingEngine(config)")
        m03_pos = source.find("recover_import_transactions")
        self.assertGreater(rem_pos, 0, "remainder recovery not found in server.py")
        self.assertGreater(emb_pos, 0)
        self.assertGreater(m03_pos, 0)
        self.assertGreater(rem_pos, m03_pos, "remainder must come after M-03")
        self.assertLess(rem_pos, emb_pos, "remainder must come before EmbeddingEngine")


class TestHealthIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def tearDown(self) -> None:
        self._tempdir.cleanup()
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def test_clean_ok(self) -> None:
        _set_wiring("active")
        _set_startup("complete")
        status = runtime_status(str(self.root))
        self.assertEqual(status["wiring"], "active")
        self.assertEqual(status["startup_state"], "complete")
        self.assertEqual(status["unresolved"], 0)

    def test_prepared_in_sidecar_health(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "old")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old", new_text="new", merged_text="merged",
        )
        _set_wiring("active")
        _set_startup("complete")
        status = runtime_status(str(self.root))
        sh = status.get("sidecar_health", {})
        self.assertTrue(sh.get("prepared_count", 0) >= 1)

    def test_unresolved_reported(self) -> None:
        mark_unresolved(code="commit_failed", bucket_id="b1", exc_type="RuntimeError")
        status = runtime_status()
        self.assertEqual(status["unresolved"], 1)
        self.assertIn("commit_failed:b1:RuntimeError", status["errors"])

    def test_conflict_in_health(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "different")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old", new_text="new", merged_text="merged",
        )
        sc_path = self.root / ".remainders" / "b1.json"
        data = json.loads(sc_path.read_text(encoding="utf-8"))
        data["entries"][-1]["state"] = STATE_CONFLICT
        sc_path.write_text(json.dumps(data), encoding="utf-8")
        _set_wiring("active")
        _set_startup("complete")
        status = runtime_status(str(self.root))
        sh = status.get("sidecar_health", {})
        self.assertTrue(sh.get("conflict_count", 0) >= 1)

    def test_health_no_content_leak(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "SECRET_DATA_123")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="SECRET_DATA_123",
            new_text="new", merged_text="merged",
        )
        _set_wiring("active")
        _set_startup("complete")
        status = runtime_status(str(self.root))
        status_str = json.dumps(status)
        self.assertNotIn("SECRET_DATA_123", status_str)

    # blocker 3+4: mark_unresolved rejects arbitrary strings
    def test_mark_unresolved_rejects_positional_string(self) -> None:
        with self.assertRaises(TypeError):
            mark_unresolved("SECRET_BODY_789")

    def test_mark_unresolved_rejects_invalid_code(self) -> None:
        with self.assertRaises(ValueError):
            mark_unresolved(code="SECRET_BODY_789")

    def test_mark_unresolved_rejects_secret_bucket_id(self) -> None:
        with self.assertRaises(ValueError):
            mark_unresolved(code="commit_failed", bucket_id="SECRET BODY 789")

    def test_mark_unresolved_rejects_secret_entry_id(self) -> None:
        with self.assertRaises(ValueError):
            mark_unresolved(code="commit_failed", entry_id="SECRET BODY 789")

    def test_mark_unresolved_rejects_secret_exc_type(self) -> None:
        with self.assertRaises(ValueError):
            mark_unresolved(code="commit_failed", exc_type="SECRET BODY 789")

    def test_mark_unresolved_valid_fields_no_leak(self) -> None:
        mark_unresolved(code="commit_failed", bucket_id="b1", exc_type="RuntimeError")
        status = runtime_status()
        status_str = json.dumps(status)
        self.assertNotIn("SECRET", status_str)
        self.assertIn("commit_failed", status_str)

    def test_runtime_status_health_exception_no_leak(self) -> None:
        _set_wiring("active")
        _set_startup("complete")
        with patch.object(
            rs, "health_check",
            side_effect=RuntimeError("SECRET_BODY_789 leaked"),
        ):
            status = runtime_status(str(self.root))
            sh = status.get("sidecar_health", {})
            self.assertFalse(sh.get("ok", True))
            self.assertNotIn("SECRET_BODY_789", json.dumps(status))
            self.assertEqual(sh.get("error"), "RuntimeError")

    def test_error_text_no_content_leak(self) -> None:
        mark_unresolved(
            code="md_abort_double_fail",
            bucket_id="target",
            exc_type="RemainderSidecarError",
        )
        status = runtime_status()
        for err in status["errors"]:
            self.assertNotIn("SECRET", err)


class TestHealthHTTPRoute(unittest.TestCase):
    """Public /health stays a constant-time liveness probe."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            # Starlette currently resolves AnyIO's deprecated compatibility
            # alias while importing TestClient.  Keep strict warning mode for
            # our code, but do not turn that third-party import warning into a
            # collection failure.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=(
                        r"The anyio\.abc\.BlockingPortal alias is deprecated, "
                        r"use anyio\.from_thread\.BlockingPortal instead\."
                    ),
                    category=DeprecationWarning,
                )
                importlib.import_module("starlette.testclient")
            cls._has_starlette = True
        except ImportError:
            cls._has_starlette = False

    def setUp(self) -> None:
        if not self._has_starlette:
            self.skipTest("starlette not available")
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        if hasattr(self, "_tempdir"):
            self._tempdir.cleanup()
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def _make_client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient
        from unittest.mock import MagicMock
        import web._shared as sh_mod

        app = Starlette()

        class FakeMCP:
            def custom_route(self, path, methods=None):
                def decorator(func):
                    for method in (methods or ["GET"]):
                        app.add_route(path, func, methods=[method])
                    return func
                return decorator

        self._saved_sh = {}
        for attr in ("repo_root", "version", "config", "bucket_mgr", "decay_engine"):
            self._saved_sh[attr] = getattr(sh_mod, attr, None)

        sh_mod.repo_root = str(self.root)
        sh_mod.version = "test"
        sh_mod.config = {"buckets_dir": str(self.root)}
        sh_mod.bucket_mgr = MagicMock()
        async def fake_stats():
            return {"permanent_count": 1, "dynamic_count": 0}
        sh_mod.bucket_mgr.get_stats = fake_stats
        sh_mod.decay_engine = MagicMock()
        sh_mod.decay_engine.is_running = True

        import web.dashboard as dash_mod
        mcp = FakeMCP()
        dash_mod.register(mcp)

        self.addCleanup(self._restore_sh, sh_mod)
        return TestClient(app)

    def _restore_sh(self, sh_mod):
        for attr, val in self._saved_sh.items():
            if val is not None:
                setattr(sh_mod, attr, val)

    def test_health_clean_200(self) -> None:
        _set_wiring("active")
        _set_startup("complete")
        client = self._make_client()
        with patch("config_transaction.get_config_health", return_value={"ok": True}), \
             patch("embedding_publish.get_publish_health", return_value={"ok": True}), \
             patch("runtime_owner.runtime_status", return_value={"wiring": "active"}):
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body, {"status": "ok"})
            self.assertEqual(r.headers["cache-control"], "no-store")

    def test_health_does_not_scan_prepared_sidecars(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "old")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old", new_text="new", merged_text="merged",
        )
        _set_wiring("active")
        _set_startup("complete")
        client = self._make_client()
        with patch("config_transaction.get_config_health", return_value={"ok": True}), \
             patch("embedding_publish.get_publish_health", return_value={"ok": True}), \
             patch("runtime_owner.runtime_status", return_value={"wiring": "active"}):
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"status": "ok"})

    def test_health_does_not_expose_unresolved_state(self) -> None:
        _set_wiring("active")
        _set_startup("complete")
        mark_unresolved(code="commit_failed", bucket_id="b1", exc_type="RuntimeError")
        client = self._make_client()
        with patch("config_transaction.get_config_health", return_value={"ok": True}), \
             patch("embedding_publish.get_publish_health", return_value={"ok": True}), \
             patch("runtime_owner.runtime_status", return_value={"wiring": "active"}):
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"status": "ok"})

    def test_health_response_no_content(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "SECRET_HEALTH_BODY")
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="SECRET_HEALTH_BODY", new_text="new", merged_text="merged",
        )
        _set_wiring("active")
        _set_startup("complete")
        client = self._make_client()
        with patch("config_transaction.get_config_health", return_value={"ok": True}), \
             patch("embedding_publish.get_publish_health", return_value={"ok": True}), \
             patch("runtime_owner.runtime_status", return_value={"wiring": "active"}):
            r = client.get("/health")
            self.assertNotIn("SECRET_HEALTH_BODY", r.text)


class TestR03FailClosedGaps(unittest.TestCase):
    """澄 2026-08-09 R-03 最终复核提出的 3 个 fail-closed 缺口的反例。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    def tearDown(self) -> None:
        self._tempdir.cleanup()
        _set_wiring("inactive")
        _set_startup("pending")
        clear_unresolved()

    # ---- gap 1: single os.read truncates authoritative content ----

    def test_short_read_does_not_truncate_authoritative_content(self) -> None:
        """合法短读必须循环读完，不得把截断内容当完整权威内容。"""
        content = "AUTHORITATIVE-CONTENT-MUST-NOT-TRUNCATE"
        _write_bucket(self.root, "permanent", "b1", content)

        real_read = os.read
        state = {"first": True}

        def short_first_read(fd: int, n: int) -> bytes:
            if state["first"]:
                state["first"] = False
                return real_read(fd, min(5, n))
            return real_read(fd, n)

        with patch("remainder_integration.os.read", side_effect=short_first_read):
            inventory = build_bucket_inventory(self.root)

        self.assertEqual(inventory["b1"], content)
        self.assertNotEqual(inventory["b1"], "AUTHO")

    def test_short_read_chunked_still_complete(self) -> None:
        """连续多次短读（每次 3 字节）也必须拼回完整内容。"""
        content = "AUTHORITATIVE-CONTENT-MUST-NOT-TRUNCATE"
        _write_bucket(self.root, "permanent", "b1", content)

        real_read = os.read

        def chunked_read(fd: int, n: int) -> bytes:
            return real_read(fd, min(3, n))

        with patch("remainder_integration.os.read", side_effect=chunked_read):
            inventory = build_bucket_inventory(self.root)

        self.assertEqual(inventory["b1"], content)

    def test_short_read_startup_still_classifies_correctly(self) -> None:
        """短读若截断，PREPARED 会被误判；确认 startup 走到 complete 且判对。"""
        merged = "merged content that is long enough to be short-read"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old content",
            new_text="new content",
            merged_text=merged,
        )

        real_read = os.read
        state = {"first": True}

        def short_first_read(fd: int, n: int) -> bytes:
            if state["first"]:
                state["first"] = False
                return real_read(fd, min(4, n))
            return real_read(fd, n)

        _set_startup("pending")
        with patch("remainder_integration.os.read", side_effect=short_first_read):
            result = recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "complete")
        self.assertTrue(result["committed"] >= 1)

    def test_read_oserror_wrapped_not_leaked(self) -> None:
        """读取失败必须归一化为 RemainderSidecarError，且不泄露原始正文。"""
        _write_bucket(self.root, "permanent", "b1", "hello")

        def boom(fd: int, n: int) -> bytes:
            raise OSError(5, "SECRET_READ_DETAIL_123")

        with patch("remainder_integration.os.read", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                build_bucket_inventory(self.root)
        self.assertNotIn("SECRET_READ_DETAIL_123", str(ctx.exception))

    def test_read_eintr_retried(self) -> None:
        """EINTR 必须重试而不是失败。"""
        import errno as _errno
        content = "EINTR-RETRY-CONTENT"
        _write_bucket(self.root, "permanent", "b1", content)

        real_read = os.read
        state = {"raised": False}

        def eintr_once(fd: int, n: int) -> bytes:
            if not state["raised"]:
                state["raised"] = True
                raise OSError(_errno.EINTR, "interrupted")
            return real_read(fd, n)

        with patch("remainder_integration.os.read", side_effect=eintr_once):
            inventory = build_bucket_inventory(self.root)
        self.assertEqual(inventory["b1"], content)

    # ---- 澄 10:19 复验：_read_file_fd_identity 全部 OSError 出口不得泄露 ----

    def test_open_oserror_wrapped_not_leaked(self) -> None:
        """os.open 失败：现场就是这条打出 SECRET_OPEN_DETAIL_987 的。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        real_open = os.open

        def boom(p, *a, **k):
            if str(p).endswith("b1.md"):
                raise OSError(5, "SECRET_OPEN_DETAIL_987")
            return real_open(p, *a, **k)

        with patch("remainder_integration.os.open", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                build_bucket_inventory(self.root)
        msg = str(ctx.exception)
        self.assertNotIn("SECRET_OPEN_DETAIL_987", msg)
        self.assertIn("OSError", msg)

    def test_open_oserror_startup_sets_failed_no_leak(self) -> None:
        """同一条经 startup 路径：状态 failed，且 fatal 文本不含 SECRET。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        real_open = os.open

        def boom(p, *a, **k):
            if str(p).endswith("b1.md"):
                raise OSError(5, "SECRET_OPEN_STARTUP_987")
            return real_open(p, *a, **k)

        with patch("remainder_integration.os.open", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_OPEN_STARTUP_987", str(ctx.exception))

    def test_fstat_oserror_wrapped_not_leaked(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")

        def boom(fd):
            raise OSError(9, "SECRET_FSTAT_DETAIL_987")

        with patch("remainder_integration.os.fstat", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                build_bucket_inventory(self.root)
        self.assertNotIn("SECRET_FSTAT_DETAIL_987", str(ctx.exception))

    def test_close_oserror_wrapped_not_leaked(self) -> None:
        """close-only failure：主流程无异常，fd 仍只关一次。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        real_close = os.close

        def fail_close(fd):
            real_close(fd)  # 真关一次，证明不会 double-close
            raise OSError(9, "SECRET_CLOSE_DETAIL_987")

        with patch("remainder_integration.os.close", side_effect=fail_close):
            with self.assertRaises(RemainderSidecarError) as ctx:
                build_bucket_inventory(self.root)
        self.assertNotIn("SECRET_CLOSE_DETAIL_987", str(ctx.exception))

    def test_read_fail_plus_close_fail_combined_not_leaked(self) -> None:
        """read 与 close 同时失败：两个 SECRET 都不得泄露，且不覆盖主契约错误。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        real_close = os.close

        def fail_read(fd, n):
            raise OSError(5, "SECRET_MAIN_READ_987")

        def fail_close(fd):
            real_close(fd)
            raise OSError(9, "SECRET_CLOSE_COMBINED_987")

        with patch("remainder_integration.os.read", side_effect=fail_read), \
             patch("remainder_integration.os.close", side_effect=fail_close):
            with self.assertRaises(RemainderSidecarError) as ctx:
                build_bucket_inventory(self.root)
        msg = str(ctx.exception)
        self.assertNotIn("SECRET_MAIN_READ_987", msg)
        self.assertNotIn("SECRET_CLOSE_COMBINED_987", msg)

    def test_pre_and_post_lstat_oserror_wrapped_not_leaked(self) -> None:
        """pre-lstat / post-lstat 的 OSError 也必须归一化，不得裸奔。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        real_lstat = Path.lstat
        state = {"calls": 0}

        def flaky_lstat(self_p, *a, **k):
            if str(self_p).endswith("b1.md"):
                state["calls"] += 1
                if state["calls"] >= 2:  # 放过 _is_regular_nofollow，炸 pre-lstat
                    raise OSError(13, "SECRET_LSTAT_DETAIL_987")
            return real_lstat(self_p, *a, **k)

        with patch.object(Path, "lstat", flaky_lstat):
            with self.assertRaises(RemainderSidecarError) as ctx:
                build_bucket_inventory(self.root)
        self.assertNotIn("SECRET_LSTAT_DETAIL_987", str(ctx.exception))

    def test_invalid_utf8_does_not_leak_byte_detail(self) -> None:
        """UnicodeDecodeError 正文含字节值与偏移，属桶内容细节，只留类型。"""
        d = self.root / "permanent"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bad.md").write_bytes(b"---\nid: bad\n---\n\xff\xfe\xfd")
        with self.assertRaises(RemainderSidecarError) as ctx:
            build_bucket_inventory(self.root)
        msg = str(ctx.exception)
        self.assertIn("UnicodeDecodeError", msg)
        self.assertNotIn("0xff", msg)
        self.assertNotIn("invalid start byte", msg)

    def test_no_raw_oserror_escapes_read_helper(self) -> None:
        """兜底：六个出口逐个注入，一律 RemainderSidecarError，绝无裸 OSError。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        real = {
            "open": os.open, "fstat": os.fstat,
            "read": os.read, "close": os.close,
        }

        def make(name, token):
            def boom(*a, **k):
                # close 必须先真关，否则 fd 泄漏、Windows 上 tempdir 删不掉
                if name == "close":
                    real["close"](*a, **k)
                raise OSError(5, token)
            return boom

        for name in ("open", "fstat", "read", "close"):
            token = f"SECRET_SYSCALL_{name.upper()}"
            with self.subTest(syscall=name):
                # 自检：确认注入的异常真的带着这个 token，否则下面的
                # assertNotIn 会变成一句空话。
                self.assertIn(token, str(OSError(5, token)))
                with patch(f"remainder_integration.os.{name}",
                           side_effect=make(name, token)):
                    try:
                        build_bucket_inventory(self.root)
                    except RemainderSidecarError as exc:
                        self.assertNotIn(token, str(exc))
                    except OSError as exc:  # pragma: no cover
                        self.fail(f"raw OSError escaped from {name}: {exc}")
                    else:
                        self.fail(f"{name} injection did not raise")

    # ---- 澄 10:40 复验：startup 直接调用链的同族泄露 ----

    def test_corrupt_utf8_sidecar_no_byte_leak_and_failed(self) -> None:
        """损坏 UTF-8 sidecar：不得泄露字节值/偏移，且不得停在 recovering。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "b1.json").write_bytes(b'{"bucket_id": "b1", "x": "\xff\xfe"}')
        with self.assertRaises(RemainderSidecarError) as ctx:
            recover_remainders_before_startup(self.root)
        msg = str(ctx.exception)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("0xff", msg)
        self.assertNotIn("invalid start byte", msg)
        self.assertNotIn("position", msg)

    def test_load_sidecar_arbitrary_exception_no_leak(self) -> None:
        """load_sidecar 抛任意异常：只留类型，状态 failed。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "b1.json").write_text("{}", encoding="utf-8")

        def boom(*a, **k):
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "SECRET_DECODE_DETAIL_321"
            )

        with patch.object(rs, "load_sidecar", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_DECODE_DETAIL_321", str(ctx.exception))

    def test_load_sidecar_oserror_no_leak(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "b1.json").write_text("{}", encoding="utf-8")

        def boom(*a, **k):
            raise OSError(5, "SECRET_LOADSC_OSERROR_321")

        with patch.object(rs, "load_sidecar", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_LOADSC_OSERROR_321", str(ctx.exception))

    def _prepared_fixture(self) -> str:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old content", new_text="new content",
            merged_text=merged,
        )
        return merged

    def test_recover_apply_oserror_no_leak_and_failed(self) -> None:
        """durable apply 阶段 OSError：现场那条 SECRET_DURABLE_DETAIL_321。"""
        self._prepared_fixture()

        def boom(*a, **k):
            raise OSError(5, "SECRET_DURABLE_DETAIL_321")

        _set_startup("pending")
        with patch.object(ri, "recover_prepared_entries", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_DURABLE_DETAIL_321", str(ctx.exception))

    def test_recover_apply_sidecar_error_text_not_relayed(self) -> None:
        """澄的反驳点：RemainderSidecarError 自身也可能带正文，不得转发。"""
        self._prepared_fixture()

        def boom(*a, **k):
            raise RemainderSidecarError("SECRET_RSE_DETAIL_654")

        _set_startup("pending")
        with patch.object(ri, "recover_prepared_entries", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        msg = str(ctx.exception)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_RSE_DETAIL_654", msg)
        self.assertIn("RemainderSidecarError", msg)

    def test_recover_apply_exception_chain_preserved(self) -> None:
        """脱敏不能以丢失 exception chain 为代价。"""
        self._prepared_fixture()
        sentinel = RemainderSidecarError("SECRET_CHAIN_654")

        def boom(*a, **k):
            raise sentinel

        _set_startup("pending")
        with patch.object(ri, "recover_prepared_entries", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertIs(ctx.exception.__cause__, sentinel)

    def test_recover_apply_failure_leaves_prepared_untouched(self) -> None:
        """apply 失败后 PREPARED 不得被改写（只读/恢复契约不被破坏）。"""
        self._prepared_fixture()
        before = (self.root / ".remainders" / "b1.json").read_bytes()

        def boom(*a, **k):
            raise OSError(5, "SECRET_DURABLE_CONTRACT_321")

        _set_startup("pending")
        with patch.object(ri, "recover_prepared_entries", side_effect=boom):
            with self.assertRaises(RemainderSidecarError):
                recover_remainders_before_startup(self.root)
        after = (self.root / ".remainders" / "b1.json").read_bytes()
        self.assertEqual(before, after)
        sc = load_sidecar_or_none(str(self.root), "b1")
        self.assertTrue(any(e.state == STATE_PREPARED for e in sc.entries))

    def test_fatal_log_lines_carry_type_only(self) -> None:
        """两条 logger.error 也是 fatal-log 路径，不得写入原始异常正文。"""
        self._prepared_fixture()

        def boom(*a, **k):
            raise OSError(5, "SECRET_LOGLINE_321")

        _set_startup("pending")
        with patch.object(ri, "archive_if_needed", side_effect=boom):
            with self.assertLogs("remainder_integration", level="ERROR") as cm:
                with self.assertRaises(RemainderSidecarError):
                    recover_remainders_before_startup(self.root)
        joined = "\n".join(cm.output)
        self.assertNotIn("SECRET_LOGLINE_321", joined)
        self.assertIn("OSError", joined)

    # ---- 澄 10:53 复验：health precheck 边界 ----

    def test_health_errors_text_not_leaked(self) -> None:
        """health_check 返回 ok=false 时，errors 正文不得进异常。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        fake = {"ok": False, "errors": ["SECRET_HEALTH_DETAIL_987"]}
        with patch.object(rs, "health_check", return_value=fake):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        msg = str(ctx.exception)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_HEALTH_DETAIL_987", msg)
        self.assertIn("error_count=1", msg)

    def test_health_errors_count_only_multiple(self) -> None:
        """多条 errors 只报计数，一条正文都不带。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        fake = {
            "ok": False,
            "errors": ["SECRET_A_987", "SECRET_B_987", "SECRET_C_987"],
        }
        with patch.object(rs, "health_check", return_value=fake):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        msg = str(ctx.exception)
        for tok in ("SECRET_A_987", "SECRET_B_987", "SECRET_C_987"):
            self.assertNotIn(tok, msg)
        self.assertIn("error_count=3", msg)

    def test_health_check_oserror_wrapped_and_failed(self) -> None:
        """health_check 直接抛 OSError：不得裸逃逸，不得停在 recovering。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)

        def boom(*a, **k):
            raise OSError(5, "SECRET_HEALTH_OSERROR_654")

        with patch.object(rs, "health_check", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        state = runtime_status()["startup_state"]
        self.assertEqual(state, "failed")
        self.assertNotEqual(state, "recovering")
        self.assertNotIn("SECRET_HEALTH_OSERROR_654", str(ctx.exception))
        self.assertIn("OSError", str(ctx.exception))

    def test_health_check_exception_chain_preserved(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        sentinel = OSError(5, "SECRET_HEALTH_CHAIN_654")

        def boom(*a, **k):
            raise sentinel

        with patch.object(rs, "health_check", side_effect=boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertIs(ctx.exception.__cause__, sentinel)

    def test_health_check_keyboardinterrupt_not_swallowed(self) -> None:
        """except Exception 不得捕 BaseException。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)

        with patch.object(rs, "health_check", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                recover_remainders_before_startup(self.root)

    def test_startup_never_ends_in_recovering(self) -> None:
        """兜底：startup 直接调用链的各类失败，一律不得停在 recovering。"""
        # token 与注入的异常绑在同一个元组里，不再由 name 推导——上一版
        # 用 name.split("_")[0] 推出 "RECOVER"，而实际注入的是 "APPLY"，
        # 那条 assertNotIn 在检查一个从不存在的字符串，永远通过。
        cases = {
            "load_sidecar": "SECRET_SWEEP_LOAD",
            "recover_apply": "SECRET_SWEEP_APPLY",
            "archive": "SECRET_SWEEP_ARCHIVE",
            "health": "SECRET_SWEEP_HEALTH",
        }
        for name, token in cases.items():
            err = OSError(5, token)
            with self.subTest(stage=name):
                tmp = tempfile.TemporaryDirectory()
                try:
                    root = Path(tmp.name)
                    merged = "merged content"
                    _write_bucket(root, "permanent", "b1", merged)
                    _write_prepared_sidecar(
                        root, "b1", old_text="old", new_text="new",
                        merged_text=merged,
                    )
                    target = {
                        "load_sidecar": (rs, "load_sidecar"),
                        "recover_apply": (ri, "recover_prepared_entries"),
                        "archive": (ri, "archive_if_needed"),
                        "health": (rs, "health_check"),
                    }[name]

                    def boom(*a, **k):
                        raise err

                    _set_startup("pending")
                    with patch.object(target[0], target[1], side_effect=boom):
                        with self.assertRaises(Exception) as ctx:
                            recover_remainders_before_startup(root)
                    state = runtime_status()["startup_state"]
                    self.assertNotEqual(state, "recovering")
                    self.assertEqual(state, "failed")
                    # 自检：token 必须真的出现在注入的异常里，否则下面那条
                    # assertNotIn 是在检查一个不存在的字符串（永远通过）。
                    self.assertIn(token, str(err))
                    self.assertNotIn(token, str(ctx.exception))
                finally:
                    tmp.cleanup()

    # ---- gap 2: rogue *.lock at remainder root silently skipped ----

    def test_root_level_rogue_lock_rejected(self) -> None:
        """.remainders/ 根级 *.lock 必须 fail-closed，不得静默跳过。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "rogue.lock").write_bytes(b"\0")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")

    def test_root_level_sha256_named_lock_also_rejected(self) -> None:
        """即使命名合法（64 hex + .lock），根级也不是锁该待的地方。"""
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / ("a" * 64 + ".lock")).write_bytes(b"\0")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")

    def test_root_level_rogue_lock_blocks_recovery_completion(self) -> None:
        """有 rogue.lock 时不得报 complete（复核现场就是这条漏的）。"""
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        _write_prepared_sidecar(
            self.root, "b1",
            old_text="old content", new_text="new content",
            merged_text=merged,
        )
        rem = self.root / ".remainders"
        (rem / "rogue.lock").write_bytes(b"\0")
        _set_startup("pending")
        with self.assertRaises(RemainderSidecarError):
            recover_remainders_before_startup(self.root)
        self.assertNotEqual(runtime_status()["startup_state"], "complete")
        self.assertEqual(runtime_status()["startup_state"], "failed")

    # ---- gap 3: subtree enumeration I/O error leaves state at recovering ----

    def _iterdir_raiser(self, target_name: str):
        real_iterdir = Path.iterdir

        def fake_iterdir(p: Path):
            if p.name == target_name:
                raise OSError(13, "SECRET_ENUM_DETAIL_456")
            return real_iterdir(p)

        return fake_iterdir

    def test_locks_enumeration_oserror_sets_failed(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / ".locks").mkdir()
        with patch.object(Path, "iterdir", self._iterdir_raiser(".locks")):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_ENUM_DETAIL_456", str(ctx.exception))

    def test_archive_enumeration_oserror_sets_failed(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "archive").mkdir()
        with patch.object(Path, "iterdir", self._iterdir_raiser("archive")):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_ENUM_DETAIL_456", str(ctx.exception))

    def test_quarantine_enumeration_oserror_sets_failed(self) -> None:
        _write_bucket(self.root, "permanent", "b1", "hello")
        rem = self.root / ".remainders"
        rem.mkdir(exist_ok=True)
        (rem / "quarantine").mkdir()
        with patch.object(Path, "iterdir", self._iterdir_raiser("quarantine")):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_ENUM_DETAIL_456", str(ctx.exception))

    def test_enumeration_oserror_never_leaves_recovering(self) -> None:
        """三处枚举失败都不得把状态停在 recovering。"""
        for target in (".locks", "archive", "quarantine"):
            with self.subTest(target=target):
                tmp = tempfile.TemporaryDirectory()
                try:
                    root = Path(tmp.name)
                    _write_bucket(root, "permanent", "b1", "hello")
                    rem = root / ".remainders"
                    rem.mkdir(exist_ok=True)
                    (rem / target).mkdir()
                    _set_startup("pending")
                    with patch.object(
                        Path, "iterdir", self._iterdir_raiser(target)
                    ):
                        with self.assertRaises(RemainderSidecarError):
                            recover_remainders_before_startup(root)
                    self.assertNotEqual(
                        runtime_status()["startup_state"], "recovering"
                    )
                    self.assertEqual(
                        runtime_status()["startup_state"], "failed"
                    )
                finally:
                    tmp.cleanup()

    def test_bucket_inventory_oserror_sets_failed(self) -> None:
        """inventory 阶段的裸 OSError 也必须归一化为 failed。"""
        _write_bucket(self.root, "permanent", "b1", "hello")

        def boom(self_p: Path, *a, **k):
            raise OSError(13, "SECRET_LSTAT_789")

        with patch.object(Path, "lstat", boom):
            with self.assertRaises(RemainderSidecarError) as ctx:
                recover_remainders_before_startup(self.root)
        self.assertEqual(runtime_status()["startup_state"], "failed")
        self.assertNotIn("SECRET_LSTAT_789", str(ctx.exception))

    # ---- read-only guarantee still holds after the fixes ----

    def test_validators_still_readonly_after_fix(self) -> None:
        merged = "merged content"
        _write_bucket(self.root, "permanent", "b1", merged)
        entry, gen = prepare_remainder(
            str(self.root), "b1",
            old_text="old", new_text="new",
            merged_text=merged,
            merge_method="llm",
            metadata=_META_CHENG,
            current_content="old",
        )
        commit_remainder(str(self.root), "b1", entry.entry_id, gen)
        from remainder_sidecar import archive_if_needed
        archive_if_needed(str(self.root), "b1")

        rem = self.root / ".remainders"
        before = {
            str(p): p.read_bytes()
            for p in sorted(rem.rglob("*")) if p.is_file()
        }
        _set_startup("pending")
        recover_remainders_before_startup(self.root)
        after = {
            str(p): p.read_bytes()
            for p in sorted(rem.rglob("*")) if p.is_file()
        }
        self.assertEqual(set(before), set(after))
        for k in before:
            self.assertEqual(before[k], after[k], f"bytes changed: {k}")


if __name__ == "__main__":
    unittest.main()
