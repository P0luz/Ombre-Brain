from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remainder_sidecar as rs
from remainder_sidecar import (
    MAX_ACTIVE_BYTES,
    SCHEMA_VERSION,
    RemainderConflictError,
    RemainderQuarantineError,
    RemainderSidecarError,
    StaleSidecarError,
    STATE_ABORTED,
    STATE_COMMITTED,
    STATE_CONFLICT,
    STATE_PREPARED,
    abort_remainder,
    archive_if_needed,
    commit_remainder,
    content_sha256,
    extract_unmatched,
    health_check,
    list_entries,
    load_sidecar,
    load_sidecar_or_none,
    prepare_remainder,
    recover_prepared_entries,
    strict_owner_from_metadata,
    transition_entry,
    validate_archive_member,
    validate_lock_member,
)

_META_CHENG = {"tags": ["owner:cheng"]}
_META_HUAIYIN = {"tags": ["owner:huaiyin"]}
_META_HUAIYIN_CC = {"tags": ["owner:huaiyin_cc"]}
_META_UNTAGGED: dict = {}


def _prepare_in_child(root: str, new_content: str, barrier, result) -> None:
    barrier.wait()
    try:
        entry, gen = prepare_remainder(
            root, "shared-bucket", "old", new_content,
            f"merged-{new_content}", "llm", _META_CHENG,
            current_content="old",
        )
        result.put(("ok", gen, entry.entry_id))
    except BaseException as exc:
        import traceback
        result.put(("error", type(exc).__name__, traceback.format_exc()))


class RemainderSidecarContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    # ---- unmatched algorithm (T-E1-1 through T-E1-4) ----

    def test_unmatched_single_overlap_keeps_new_copy(self) -> None:
        result = extract_unmatched("A", "A", "A")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "new")
        self.assertEqual(result[0]["text"], "A")

    def test_unmatched_old_duplicate_keeps_unconsumed(self) -> None:
        result = extract_unmatched("A\nA", "", "A")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "old")
        self.assertEqual(result[0]["line_index"], 1)

    def test_unmatched_combined_provenance_queue(self) -> None:
        result = extract_unmatched("A\nB", "B\nC", "A\nB")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], {"source": "new", "line_index": 0, "text": "B"})
        self.assertEqual(result[1], {"source": "new", "line_index": 1, "text": "C"})

    def test_unmatched_empty_merged_returns_all(self) -> None:
        result = extract_unmatched("A\nB", "C", "")
        self.assertEqual(len(result), 3)
        sources = [r["source"] for r in result]
        self.assertEqual(sources, ["old", "old", "new"])

    def test_unmatched_blank_lines_ignored(self) -> None:
        result = extract_unmatched("A\n\nB", "C\n  \n", "A")
        texts = [r["text"] for r in result]
        self.assertEqual(texts, ["B", "C"])

    # ---- prepare / commit lifecycle ----

    def test_prepare_is_durable_and_contains_hashes(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        self.assertEqual(entry.state, STATE_PREPARED)
        self.assertEqual(gen, 1)
        self.assertEqual(entry.owner, "cheng")

        path = self.root / ".remainders" / "bucket-01.json"
        raw = path.read_text(encoding="utf-8")
        self.assertIn(content_sha256("new"), raw)
        self.assertIn(content_sha256("old"), raw)
        self.assertIn(content_sha256("merged"), raw)
        self.assertRegex(entry.old_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(list(self.root.glob("**/*.tmp")), [])

    def test_normal_commit_lifecycle(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        committed = commit_remainder(self.root, "bucket-01", entry.entry_id, gen)
        self.assertEqual(committed.state, STATE_COMMITTED)
        self.assertIsNotNone(committed.committed_at)

    def test_abort_lifecycle(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        aborted = abort_remainder(self.root, "bucket-01", entry.entry_id, gen)
        self.assertEqual(aborted.state, STATE_ABORTED)
        self.assertIsNone(aborted.committed_at)

    # ---- recovery three branches ----

    def test_recovery_committed_when_merged_matches(self) -> None:
        prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transitions = recover_prepared_entries(self.root, "bucket-01", "merged")
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0][1], STATE_COMMITTED)

    def test_recovery_aborted_when_old_matches(self) -> None:
        prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transitions = recover_prepared_entries(self.root, "bucket-01", "old")
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0][1], STATE_ABORTED)

    def test_recovery_conflict_when_neither_matches(self) -> None:
        prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transitions = recover_prepared_entries(self.root, "bucket-01", "something else")
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0][1], STATE_CONFLICT)

    def test_recovery_idempotent(self) -> None:
        prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        first = recover_prepared_entries(self.root, "bucket-01", "merged")
        second = recover_prepared_entries(self.root, "bucket-01", "merged")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_recovery_content_none_yields_conflict(self) -> None:
        prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transitions = recover_prepared_entries(self.root, "bucket-01", None)
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0][1], STATE_CONFLICT)

    def test_recovery_quarantines_corrupt_sidecar(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        corrupt = rem / "bucket-01.json"
        corrupt.write_text("NOT VALID JSON", encoding="utf-8")
        with self.assertRaises(RemainderQuarantineError):
            recover_prepared_entries(self.root, "bucket-01", "anything")
        self.assertFalse(corrupt.exists())
        q_dir = rem / "quarantine"
        self.assertTrue(q_dir.exists())
        quarantined = list(q_dir.glob("bucket-01_*.json.corrupt"))
        self.assertEqual(len(quarantined), 1)

    # ---- CONFLICT is terminal and blocks ----

    def test_conflict_is_terminal_and_blocks_new_prepare(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transition_entry(
            self.root, "bucket-01", entry.entry_id, gen, STATE_CONFLICT,
        )
        with self.assertRaises(RemainderConflictError):
            prepare_remainder(
                self.root, "bucket-01", "old2", "new2", "merged2",
                "llm", _META_CHENG, current_content="merged",
            )

    def test_conflict_entry_immutable(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transition_entry(
            self.root, "bucket-01", entry.entry_id, gen, STATE_CONFLICT,
        )
        sc = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid state transition"):
            transition_entry(
                self.root, "bucket-01", entry.entry_id,
                sc.generation, STATE_ABORTED,
            )

    # ---- illegal state transitions ----

    def test_committed_cannot_transition(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        commit_remainder(self.root, "bucket-01", entry.entry_id, gen)
        sc = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        for target in (STATE_PREPARED, STATE_ABORTED, STATE_CONFLICT):
            with self.assertRaisesRegex(
                RemainderSidecarError, "invalid state transition"
            ):
                transition_entry(
                    self.root, "bucket-01", entry.entry_id,
                    sc.generation, target,
                )

    def test_aborted_cannot_transition(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        abort_remainder(self.root, "bucket-01", entry.entry_id, gen)
        sc = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        with self.assertRaisesRegex(
            RemainderSidecarError, "invalid state transition"
        ):
            transition_entry(
                self.root, "bucket-01", entry.entry_id,
                sc.generation, STATE_COMMITTED,
            )

    # ---- generation CAS ----

    def test_generation_cas_prevents_stale_commit(self) -> None:
        entry1, gen1 = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        entry2, gen2 = prepare_remainder(
            self.root, "bucket-01", "old2", "new2", "merged2",
            "llm", _META_CHENG, current_content="merged",
        )
        self.assertEqual(gen2, gen1 + 1)
        with self.assertRaises(StaleSidecarError):
            commit_remainder(self.root, "bucket-01", entry1.entry_id, gen1)

    # ---- cross-process serialization ----

    def test_cross_process_serializes_same_bucket(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        result = ctx.Queue()
        processes = [
            ctx.Process(
                target=_prepare_in_child,
                args=(str(self.root), content, barrier, result),
            )
            for content in ("from-process-one", "from-process-two")
        ]
        for p in processes:
            p.start()
        outcomes = [result.get(timeout=30) for _ in processes]
        for p in processes:
            p.join(timeout=30)
            self.assertEqual(p.exitcode, 0)

        result.close()
        result.join_thread()
        for p in processes:
            p.close()

        self.assertTrue(
            all(o[0] == "ok" for o in outcomes),
            f"unexpected outcomes: {outcomes}",
        )
        self.assertEqual(sorted(o[1] for o in outcomes), [1, 2])

        sc = load_sidecar_or_none(self.root, "shared-bucket")
        self.assertIsNotNone(sc)
        self.assertEqual(sc.generation, 2)

    # ---- atomic replace failure ----

    def test_failed_replace_preserves_previous_and_cleans_temp(self) -> None:
        previous, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        real_replace = rs.os.replace

        def fail_replace(source, target):
            if Path(target).name == "bucket-01.json":
                raise OSError("injected replace failure")
            return real_replace(source, target)

        with mock.patch.object(rs.os, "replace", side_effect=fail_replace):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                commit_remainder(
                    self.root, "bucket-01", previous.entry_id, gen,
                )

        sc = load_sidecar_or_none(self.root, "bucket-01")
        self.assertIsNotNone(sc)
        self.assertEqual(sc.generation, gen)
        found = [e for e in sc.entries if e.entry_id == previous.entry_id]
        self.assertEqual(found[0].state, STATE_PREPARED)

        rem = self.root / ".remainders"
        self.assertEqual(list(rem.glob("*.tmp")), [])

    # ---- corrupt JSON / quarantine ----

    def test_corrupt_sidecar_quarantined_during_prepare(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        corrupt = rem / "bucket-01.json"
        corrupt.write_text("NOT VALID JSON {{{", encoding="utf-8")

        with self.assertRaises(RemainderQuarantineError):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

        self.assertFalse(corrupt.exists())
        q_dir = rem / "quarantine"
        self.assertTrue(q_dir.exists())
        quarantined = list(q_dir.glob("bucket-01_*.json.corrupt"))
        self.assertEqual(len(quarantined), 1)

    def test_quarantined_bucket_blocks_subsequent_prepare(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        corrupt = rem / "bucket-01.json"
        corrupt.write_text("{bad}", encoding="utf-8")

        with self.assertRaises(RemainderQuarantineError):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

        with self.assertRaises(RemainderQuarantineError):
            prepare_remainder(
                self.root, "bucket-01", "old2", "new2", "merged2",
                "llm", _META_CHENG,
            )

    def test_corrupt_schema_version_quarantined(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        bad = rem / "bucket-01.json"
        bad.write_text(json.dumps({"schema": 99, "bucket_id": "bucket-01",
                                    "generation": 1, "entries": []}),
                       encoding="utf-8")

        with self.assertRaises(RemainderQuarantineError):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    # ---- rotation ----

    def test_archive_only_moves_committed_and_aborted(self) -> None:
        for i in range(55):
            entry, gen = prepare_remainder(
                self.root, "bucket-01", f"old-{i}", f"new-{i}",
                f"merged-{i}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}" if i > 0 else None,
            )
            commit_remainder(self.root, "bucket-01", entry.entry_id, gen)

        conflict_entry, cgen = prepare_remainder(
            self.root, "bucket-01", "old-c", "new-c", "merged-c",
            "llm", _META_CHENG, current_content="merged-54",
        )
        sc = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        transition_entry(
            self.root, "bucket-01", conflict_entry.entry_id,
            sc.generation, STATE_CONFLICT,
        )

        sc_before = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        self.assertGreater(len(sc_before.entries), 50)

        moved = archive_if_needed(self.root, "bucket-01")
        self.assertGreater(moved, 0)

        sc_after = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        conflicts = [e for e in sc_after.entries if e.state == STATE_CONFLICT]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].entry_id, conflict_entry.entry_id)

        archive_dir = self.root / ".remainders" / "archive"
        self.assertTrue(archive_dir.exists())
        archive_files = list(archive_dir.glob("bucket-01_*.json"))
        self.assertGreaterEqual(len(archive_files), 1)

    def test_archive_failure_preserves_active_and_cleans_orphans(self) -> None:
        for i in range(55):
            entry, gen = prepare_remainder(
                self.root, "bucket-01", f"old-{i}", f"new-{i}",
                f"merged-{i}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}" if i > 0 else None,
            )
            commit_remainder(self.root, "bucket-01", entry.entry_id, gen)

        sc_before = load_sidecar(self.root / ".remainders" / "bucket-01.json")

        real_durable = rs._durable_json

        def fail_archive_write(path, value):
            if "archive" in str(path):
                raise OSError("injected archive failure")
            return real_durable(path, value)

        with mock.patch.object(
            rs, "_durable_json", side_effect=fail_archive_write
        ):
            with self.assertRaises(OSError):
                archive_if_needed(self.root, "bucket-01")

        sc_after = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        self.assertEqual(sc_after.generation, sc_before.generation)
        self.assertEqual(len(sc_after.entries), len(sc_before.entries))

        archive_dir = self.root / ".remainders" / "archive"
        if archive_dir.exists():
            orphans = list(archive_dir.glob("bucket-01_*.json"))
            self.assertEqual(orphans, [], "orphan archive files should be cleaned up")

    def test_archive_write_then_raise_no_orphan(self) -> None:
        for i in range(55):
            entry, gen = prepare_remainder(
                self.root, "bucket-01", f"old-{i}", f"new-{i}",
                f"merged-{i}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}" if i > 0 else None,
            )
            commit_remainder(self.root, "bucket-01", entry.entry_id, gen)

        real_durable = rs._durable_json
        call_count = [0]

        def write_then_raise(path, value):
            if "archive" in str(path):
                call_count[0] += 1
                real_durable(path, value)
                raise OSError("post-replace failure")
            return real_durable(path, value)

        sc_before = load_sidecar(self.root / ".remainders" / "bucket-01.json")

        with mock.patch.object(rs, "_durable_json", side_effect=write_then_raise):
            with self.assertRaises(OSError):
                archive_if_needed(self.root, "bucket-01")

        self.assertGreater(call_count[0], 0)
        sc_after = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        self.assertEqual(sc_after.generation, sc_before.generation)

        archive_dir = self.root / ".remainders" / "archive"
        if archive_dir.exists():
            orphans = list(archive_dir.glob("bucket-01_*.json"))
            self.assertEqual(orphans, [], "write-then-raise should leave no orphans")

    def test_archive_cleanup_failure_raises(self) -> None:
        for i in range(55):
            entry, gen = prepare_remainder(
                self.root, "bucket-01", f"old-{i}", f"new-{i}",
                f"merged-{i}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}" if i > 0 else None,
            )
            commit_remainder(self.root, "bucket-01", entry.entry_id, gen)

        real_durable = rs._durable_json

        def write_then_raise(path, value):
            if "archive" in str(path):
                real_durable(path, value)
                raise OSError("post-replace failure")
            return real_durable(path, value)

        real_unlink = Path.unlink

        def fail_unlink(path_self, *args, **kwargs):
            if "archive" in str(path_self):
                raise OSError("cannot remove archive")
            return real_unlink(path_self, *args, **kwargs)

        with mock.patch.object(rs, "_durable_json", side_effect=write_then_raise):
            with mock.patch.object(Path, "unlink", fail_unlink):
                with self.assertRaisesRegex(
                    RemainderSidecarError, "cleanup also failed"
                ):
                    archive_if_needed(self.root, "bucket-01")

    def test_archive_preserves_original_entry_order(self) -> None:
        ids_in_order = []
        for i in range(55):
            entry, gen = prepare_remainder(
                self.root, "bucket-01", f"old-{i}", f"new-{i}",
                f"merged-{i}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}" if i > 0 else None,
            )
            if i % 3 == 0:
                abort_remainder(self.root, "bucket-01", entry.entry_id, gen)
            else:
                commit_remainder(self.root, "bucket-01", entry.entry_id, gen)
            ids_in_order.append(entry.entry_id)

        archive_if_needed(self.root, "bucket-01")

        sc_after = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        after_ids = [e.entry_id for e in sc_after.entries]
        for i in range(len(after_ids) - 1):
            pos_a = ids_in_order.index(after_ids[i])
            pos_b = ids_in_order.index(after_ids[i + 1])
            self.assertLess(pos_a, pos_b,
                f"entry order violated: index {pos_a} vs {pos_b}")

    # ---- symlink / reparse / nonregular ----

    def test_symlink_root_fails_closed(self) -> None:
        real_root = self.root / "real"
        real_root.mkdir()
        linked = self.root / "linked"
        try:
            os.symlink(real_root, linked, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

        with self.assertRaisesRegex(RemainderSidecarError, "not a regular directory"):
            prepare_remainder(
                linked, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    def test_symlink_sidecar_fails_closed(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        try:
            os.symlink(target, rem / "bucket-01.json")
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlink unavailable: {exc}")

        with self.assertRaisesRegex(RemainderSidecarError, "not a regular file"):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    def test_nonregular_root_fails_closed(self) -> None:
        not_dir = self.root / ".remainders"
        not_dir.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "not a regular directory"):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    def test_sidecar_is_directory_fails_closed(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        (rem / "bucket-01.json").mkdir()
        with self.assertRaisesRegex(RemainderSidecarError, "not a regular file"):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    def test_lock_file_symlink_fails_closed(self) -> None:
        import hashlib
        rem = self.root / ".remainders"
        rem.mkdir()
        lock_dir = rem / ".locks"
        lock_dir.mkdir()
        lock_id = hashlib.sha256(
            "remainder-sidecar-bucket-01".encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        lock_file = lock_dir / f"{lock_id}.lock"
        real_target = self.root / "real_lock"
        real_target.write_bytes(b"\0")
        try:
            os.symlink(real_target, lock_file)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlink unavailable: {exc}")
        with self.assertRaisesRegex(RemainderSidecarError, "symlink|reparse|not a regular"):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    def test_zero_byte_lock_file_rejected(self) -> None:
        import hashlib
        rem = self.root / ".remainders"
        rem.mkdir()
        lock_dir = rem / ".locks"
        lock_dir.mkdir()
        lock_id = hashlib.sha256(
            "remainder-sidecar-bucket-01".encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        lock_file = lock_dir / f"{lock_id}.lock"
        lock_file.write_bytes(b"")
        with mock.patch.object(rs, "_LOCK_INIT_WAIT_SECONDS", 0.05), \
             mock.patch.object(rs, "_LOCK_INIT_POLL_INTERVAL", 0.005):
            with self.assertRaisesRegex(RemainderSidecarError, "stuck at size 0"):
                prepare_remainder(
                    self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
                )
        self.assertEqual(lock_file.stat().st_size, 0)

    def test_oversized_lock_file_rejected(self) -> None:
        import hashlib
        rem = self.root / ".remainders"
        rem.mkdir()
        lock_dir = rem / ".locks"
        lock_dir.mkdir()
        lock_id = hashlib.sha256(
            "remainder-sidecar-bucket-01".encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        lock_file = lock_dir / f"{lock_id}.lock"
        lock_file.write_bytes(b"\0" * 100)
        with self.assertRaisesRegex(RemainderSidecarError, "invalid size 100"):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    def test_lock_file_toctou_swap_rejected(self) -> None:
        import hashlib
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        rem = self.root / ".remainders"
        lock_dir = rem / ".locks"
        lock_id = hashlib.sha256(
            "remainder-sidecar-bucket-01".encode("utf-8", errors="surrogatepass")
        ).hexdigest()
        lock_file = lock_dir / f"{lock_id}.lock"
        self.assertTrue(lock_file.exists())

        swap_target = self.root / "swap_target.lock"
        swap_target.write_bytes(b"\0")

        real_os_open = os.open

        def swap_before_open(path, flags, *args, **kwargs):
            p = str(path)
            if p.endswith(".lock") and ".locks" in p and (flags & os.O_RDWR):
                try:
                    os.unlink(p)
                    os.symlink(str(swap_target), p)
                except (OSError, NotImplementedError):
                    pass
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch.object(rs.os, "open", side_effect=swap_before_open):
            with self.assertRaisesRegex(RemainderSidecarError, "identity changed"):
                prepare_remainder(
                    self.root, "bucket-01", "old2", "new2", "merged2",
                    "llm", _META_CHENG, current_content="merged",
                )

    def test_lock_dir_is_file_fails_closed(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        (rem / ".locks").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "lock directory"):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )

    # ---- list is read-only ----

    def test_list_entries_absent_root_returns_empty(self) -> None:
        absent = self.root / "absent"
        result = list_entries(absent)
        self.assertEqual(result, [])
        self.assertFalse((absent / ".remainders").exists())

    def test_list_entries_filters_by_state(self) -> None:
        entry1, gen1 = prepare_remainder(
            self.root, "bucket-a", "old", "new", "merged-a", "llm", _META_CHENG,
        )
        commit_remainder(self.root, "bucket-a", entry1.entry_id, gen1)

        prepare_remainder(
            self.root, "bucket-b", "old", "new", "merged-b", "llm", _META_CHENG,
        )

        all_entries = list_entries(self.root)
        self.assertEqual(len(all_entries), 2)

        committed = list_entries(self.root, STATE_COMMITTED)
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0].bucket_id, "bucket-a")

        prepared = list_entries(self.root, STATE_PREPARED)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].bucket_id, "bucket-b")

    def test_list_entries_invalid_filter_raises(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "invalid state filter"):
            list_entries(self.root, "UNKNOWN")

    def test_list_entries_raises_on_symlink_sidecar(self) -> None:
        prepare_remainder(
            self.root, "real-bucket", "old", "new", "merged", "llm", _META_CHENG,
        )
        rem = self.root / ".remainders"
        real_path = rem / "real-bucket.json"
        link_path = rem / "linked-bucket.json"
        try:
            os.symlink(real_path, link_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"file symlink unavailable: {exc}")

        with self.assertRaisesRegex(RemainderSidecarError, "not a regular file"):
            list_entries(self.root)

    # ---- strict_owner_from_metadata ----

    def test_strict_owner_known_cheng(self) -> None:
        owner = strict_owner_from_metadata({"tags": ["owner:cheng"]})
        self.assertEqual(owner, "cheng")

    def test_strict_owner_known_huaiyin_cc(self) -> None:
        owner = strict_owner_from_metadata({"tags": ["owner:huaiyin_cc"]})
        self.assertEqual(owner, "huaiyin_cc")

    def test_strict_owner_normalises_case_and_dash(self) -> None:
        owner = strict_owner_from_metadata({"tags": ["owner:Huaiyin-CC"]})
        self.assertEqual(owner, "huaiyin_cc")

    def test_strict_owner_untagged_allowed(self) -> None:
        owner = strict_owner_from_metadata({})
        self.assertEqual(owner, "")
        owner = strict_owner_from_metadata({"tags": []})
        self.assertEqual(owner, "")

    def test_strict_owner_untagged_required_raises(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "required"):
            strict_owner_from_metadata({}, allow_untagged=False)

    def test_strict_owner_multi_owner_rejected(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "multiple owner"):
            strict_owner_from_metadata({"tags": ["owner:cheng", "owner:huaiyin"]})

    def test_strict_owner_unknown_owner_rejected(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "unknown owner"):
            strict_owner_from_metadata({"tags": ["owner:nobody"]})

    def test_strict_owner_tags_not_list_rejected(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "must be a list"):
            strict_owner_from_metadata({"tags": "owner:cheng"})

    def test_strict_owner_tag_not_string_rejected(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "strings only"):
            strict_owner_from_metadata({"tags": [123]})

    def test_strict_owner_empty_owner_value_rejected(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "must not be empty"):
            strict_owner_from_metadata({"tags": ["owner:"]})

    def test_strict_owner_via_prepare(self) -> None:
        entry, _ = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged",
            "llm", {"tags": ["owner:cheng"]},
        )
        self.assertEqual(entry.owner, "cheng")

    def test_prepare_rejects_raw_owner_string(self) -> None:
        with self.assertRaises((TypeError, RemainderSidecarError, AttributeError)):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged",
                "llm", "cheng",
            )

    def test_prepare_untagged_owner(self) -> None:
        entry, _ = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged",
            "llm", _META_UNTAGGED,
        )
        self.assertEqual(entry.owner, "")

    def test_strict_owner_extra_known(self) -> None:
        owner = strict_owner_from_metadata(
            {"tags": ["owner:custom_bot"]},
            extra_known=frozenset({"custom_bot"}),
        )
        self.assertEqual(owner, "custom_bot")

    def test_strict_owner_shared_values_accepted(self) -> None:
        for val in ("shared", "shared_core", "shared_context", "shared_resource"):
            owner = strict_owner_from_metadata({"tags": [f"owner:{val}"]})
            self.assertEqual(owner, val, f"owner:{val} should be accepted")

    def test_strict_owner_shared_via_prepare(self) -> None:
        for val in ("shared", "shared_core", "shared_context", "shared_resource"):
            entry, _ = prepare_remainder(
                self.root, f"bucket-shared-{val}", "old", "new", "merged",
                "llm", {"tags": [f"owner:{val}"]},
            )
            self.assertEqual(entry.owner, val)

    def test_strict_owner_known_set_matches_identity(self) -> None:
        expected = {
            "cheng", "huaiyin", "huaiyin_cc",
            "shared", "shared_core", "shared_context", "shared_resource",
        }
        self.assertEqual(rs._KNOWN_OWNERS, expected)

    # ---- state/committed_at invariants ----

    def test_committed_entry_must_have_committed_at(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        path = self.root / ".remainders" / "bucket-01.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["entries"][0]["state"] = "COMMITTED"
        raw["entries"][0]["committed_at"] = None
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "COMMITTED entry must have committed_at"):
            load_sidecar(path)

    def test_prepared_entry_must_not_have_committed_at(self) -> None:
        prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        path = self.root / ".remainders" / "bucket-01.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["entries"][0]["committed_at"] = "2026-01-01T00:00:00Z"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "must not have committed_at"):
            load_sidecar(path)

    def test_aborted_entry_must_not_have_committed_at(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        abort_remainder(self.root, "bucket-01", entry.entry_id, gen)
        path = self.root / ".remainders" / "bucket-01.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["entries"][0]["committed_at"] = "2026-01-01T00:00:00Z"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "must not have committed_at"):
            load_sidecar(path)

    def test_conflict_entry_must_not_have_committed_at(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        transition_entry(self.root, "bucket-01", entry.entry_id, gen, STATE_CONFLICT)
        path = self.root / ".remainders" / "bucket-01.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["entries"][0]["committed_at"] = "2026-01-01T00:00:00Z"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "must not have committed_at"):
            load_sidecar(path)

    # ---- byte-cap trigger rotation ----

    def test_byte_cap_triggers_archive(self) -> None:
        big_text = "X" * 8000
        for i in range(80):
            entry, gen = prepare_remainder(
                self.root, "bucket-01",
                f"old-{i}-{big_text}", f"new-{i}-{big_text}",
                f"merged-{i}-{big_text}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}-{big_text}" if i > 0 else None,
            )
            commit_remainder(self.root, "bucket-01", entry.entry_id, gen)

        path = self.root / ".remainders" / "bucket-01.json"
        sc = load_sidecar(path)
        sc_bytes = len(json.dumps(
            rs._sidecar_to_dict(sc), ensure_ascii=False
        ).encode("utf-8"))
        self.assertGreater(sc_bytes, MAX_ACTIVE_BYTES)

        moved = archive_if_needed(self.root, "bucket-01", max_active=999)
        self.assertGreater(moved, 0)

        sc_after = load_sidecar(path)
        after_bytes = len(json.dumps(
            rs._sidecar_to_dict(sc_after), ensure_ascii=False
        ).encode("utf-8"))
        self.assertLess(after_bytes, sc_bytes)

    def test_oversized_unresolvable_shown_in_health(self) -> None:
        big_text = "X" * 8000
        n_entries = 80
        rem = self.root / ".remainders"
        rem.mkdir()
        entries_data = []
        for i in range(n_entries):
            entries_data.append({
                "entry_id": f"{i:032x}",
                "state": STATE_CONFLICT,
                "bucket_id": "bucket-01",
                "old_sha256": content_sha256(f"old-{i}-{big_text}"),
                "new_sha256": content_sha256(f"new-{i}-{big_text}"),
                "merged_sha256": content_sha256(f"merged-{i}-{big_text}"),
                "unmatched_verbatim_lines": [
                    {"source": "new", "line_index": 0, "text": big_text}
                ],
                "merge_method": "llm",
                "owner": "cheng",
                "created_at": "2026-01-01T00:00:00Z",
                "committed_at": None,
            })
        sidecar_data = {
            "schema": 2,
            "bucket_id": "bucket-01",
            "generation": n_entries,
            "entries": entries_data,
        }
        path = rem / "bucket-01.json"
        path.write_text(
            json.dumps(sidecar_data, ensure_ascii=False), encoding="utf-8"
        )
        sc_bytes = len(path.read_bytes())
        self.assertGreater(sc_bytes, MAX_ACTIVE_BYTES)

        h = health_check(self.root)
        self.assertTrue(h["oversized_unresolvable"])
        self.assertFalse(h["ok"])
        self.assertGreater(h["total_active_bytes"], MAX_ACTIVE_BYTES)
        self.assertEqual(h["conflict_count"], n_entries)
        self.assertTrue(any("oversized" in e for e in h["errors"]))

        moved = archive_if_needed(self.root, "bucket-01")
        self.assertEqual(moved, 0)

    # ---- invalid bucket_id ----

    def test_path_traversal_bucket_id_rejected(self) -> None:
        with self.assertRaisesRegex(RemainderSidecarError, "invalid bucket_id"):
            prepare_remainder(
                self.root, "../escape", "old", "new", "merged", "llm", _META_CHENG,
            )

    # ---- health check ----

    def test_health_check_reflects_state(self) -> None:
        h = health_check(self.root)
        self.assertFalse(h["root_exists"])
        self.assertTrue(h["ok"])

        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        h = health_check(self.root)
        self.assertTrue(h["root_exists"])
        self.assertTrue(h["ok"])
        self.assertEqual(h["sidecar_count"], 1)
        self.assertEqual(h["prepared_count"], 1)
        self.assertEqual(h["errors"], [])

        commit_remainder(self.root, "bucket-01", entry.entry_id, gen)
        h = health_check(self.root)
        self.assertEqual(h["prepared_count"], 0)

    def test_health_check_reports_errors_on_corrupt(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        corrupt = rem / "broken.json"
        corrupt.write_text("NOT VALID", encoding="utf-8")
        h = health_check(self.root)
        self.assertFalse(h["ok"])
        self.assertGreater(len(h["errors"]), 0)
        self.assertIn("broken.json", h["errors"][0])

    def test_health_check_reports_quarantined_buckets(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        corrupt = rem / "bucket-01.json"
        corrupt.write_text("{bad}", encoding="utf-8")
        with self.assertRaises(RemainderQuarantineError):
            prepare_remainder(
                self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
            )
        h = health_check(self.root)
        self.assertIn("bucket-01", h["quarantined_buckets"])

    # ---- prepare recovers stale PREPARED ----

    def test_prepare_recovers_stale_prepared(self) -> None:
        entry1, gen1 = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        self.assertEqual(entry1.state, STATE_PREPARED)

        entry2, gen2 = prepare_remainder(
            self.root, "bucket-01", "old2", "new2", "merged2",
            "llm", _META_CHENG, current_content="old",
        )
        self.assertEqual(gen2, gen1 + 1)

        sc = load_sidecar(self.root / ".remainders" / "bucket-01.json")
        states = [e.state for e in sc.entries]
        self.assertIn(STATE_ABORTED, states)
        self.assertIn(STATE_PREPARED, states)
        prepared = [e for e in sc.entries if e.state == STATE_PREPARED]
        self.assertEqual(len(prepared), 1)
        self.assertEqual(prepared[0].entry_id, entry2.entry_id)

    # ---- quarantine filename uniqueness ----

    def test_quarantine_filenames_unique(self) -> None:
        rem = self.root / ".remainders"
        rem.mkdir()
        for i in range(3):
            bid = f"bucket-q{i}"
            corrupt = rem / f"{bid}.json"
            corrupt.write_text(f"corrupt-{i}", encoding="utf-8")
            with self.assertRaises(RemainderQuarantineError):
                prepare_remainder(
                    self.root, bid, "old", "new", "merged", "llm", _META_CHENG,
                )
        q_dir = rem / "quarantine"
        quarantined = list(q_dir.glob("*.json.corrupt"))
        self.assertEqual(len(quarantined), 3)
        names = [f.name for f in quarantined]
        self.assertEqual(len(set(names)), 3)


def _creator_delayed_init(root: str, event_created, result) -> None:
    """Creator: O_EXCL create lock file, signal event, sleep, then write init byte + prepare."""
    import hashlib
    import time
    from pathlib import Path
    lock_dir = Path(root) / ".remainders" / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_id = hashlib.sha256(
        "remainder-sidecar-shared-bucket".encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    lock_path = lock_dir / f"{lock_id}.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(lock_path, flags, 0o600)
        event_created.set()
        time.sleep(0.15)
        try:
            os.write(fd, b"\0")
        finally:
            os.close(fd)
        from remainder_sidecar import prepare_remainder
        entry, gen = prepare_remainder(
            root, "shared-bucket", "old", "creator", "merged-creator",
            "llm", {"tags": ["owner:cheng"]},
            current_content="old",
        )
        result.put(("ok", gen, entry.entry_id))
    except BaseException as exc:
        import traceback
        result.put(("error", type(exc).__name__, traceback.format_exc()))


def _follower_after_create(root: str, event_created, result) -> None:
    """Follower: wait for creator to create 0-byte lock, then prepare (bounded wait should succeed)."""
    event_created.wait(timeout=10)
    try:
        from remainder_sidecar import prepare_remainder
        entry, gen = prepare_remainder(
            root, "shared-bucket", "old", "follower", "merged-follower",
            "llm", {"tags": ["owner:cheng"]},
            current_content="old",
        )
        result.put(("ok", gen, entry.entry_id))
    except BaseException as exc:
        import traceback
        result.put(("error", type(exc).__name__, traceback.format_exc()))


def _prepare_with_bad_lock(root: str, bucket_id: str, barrier, result) -> None:
    barrier.wait()
    try:
        from remainder_sidecar import prepare_remainder
        entry, gen = prepare_remainder(
            root, bucket_id, "old", "new", "merged", "llm",
            {"tags": ["owner:cheng"]},
        )
        result.put(("ok", gen, entry.entry_id))
    except BaseException as exc:
        result.put(("error", type(exc).__name__, str(exc)))


class CrossProcessBarrierTests(unittest.TestCase):
    """Deterministic concurrent lock initialization — 20 rounds.

    Uses multiprocessing.Barrier so both processes enter _slot_turn at
    the same instant, forcing the O_EXCL lock-init race on every round.
    """

    def test_concurrent_lock_init_twenty_rounds(self) -> None:
        for round_num in range(20):
            tempdir = tempfile.TemporaryDirectory()
            root = Path(tempdir.name)
            ctx = multiprocessing.get_context("spawn")
            barrier = ctx.Barrier(2)
            result = ctx.Queue()
            processes = [
                ctx.Process(
                    target=_prepare_in_child,
                    args=(str(root), f"content-{round_num}-{i}", barrier, result),
                )
                for i in range(2)
            ]
            try:
                for p in processes:
                    p.start()
                outcomes = [result.get(timeout=30) for _ in processes]
                for p in processes:
                    p.join(timeout=30)
                    self.assertEqual(
                        p.exitcode, 0,
                        f"round {round_num}: process exited {p.exitcode}, "
                        f"outcomes: {outcomes}",
                    )
                self.assertTrue(
                    all(o[0] == "ok" for o in outcomes),
                    f"round {round_num}: {outcomes}",
                )
                self.assertEqual(
                    sorted(o[1] for o in outcomes), [1, 2],
                    f"round {round_num}: generations {[o[1] for o in outcomes]}",
                )
            finally:
                result.close()
                result.join_thread()
                for p in processes:
                    p.close()
                tempdir.cleanup()


    def test_creator_paused_follower_waits_then_succeeds(self) -> None:
        for round_num in range(5):
            tempdir = tempfile.TemporaryDirectory()
            root = Path(tempdir.name)
            ctx = multiprocessing.get_context("spawn")
            event_created = ctx.Event()
            result = ctx.Queue()

            creator = ctx.Process(
                target=_creator_delayed_init,
                args=(str(root), event_created, result),
            )
            follower = ctx.Process(
                target=_follower_after_create,
                args=(str(root), event_created, result),
            )
            try:
                creator.start()
                follower.start()
                outcomes = [result.get(timeout=30) for _ in range(2)]
                creator.join(timeout=30)
                follower.join(timeout=30)
                self.assertEqual(creator.exitcode, 0,
                    f"round {round_num}: creator exit {creator.exitcode}")
                self.assertEqual(follower.exitcode, 0,
                    f"round {round_num}: follower exit {follower.exitcode}")
                self.assertTrue(
                    all(o[0] == "ok" for o in outcomes),
                    f"round {round_num}: both should succeed, got {outcomes}",
                )
                gens = sorted(o[1] for o in outcomes)
                self.assertEqual(gens, [1, 2],
                    f"round {round_num}: expected [1,2], got {gens}")
            finally:
                result.close()
                result.join_thread()
                creator.close()
                follower.close()
                tempdir.cleanup()

    def test_dual_repairer_zero_lock_both_reject(self) -> None:
        import hashlib
        for round_num in range(20):
            tempdir = tempfile.TemporaryDirectory()
            root = Path(tempdir.name)
            rem = root / ".remainders"
            rem.mkdir()
            lock_dir = rem / ".locks"
            lock_dir.mkdir()
            lock_id = hashlib.sha256(
                "remainder-sidecar-shared-bucket".encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            lock_file = lock_dir / f"{lock_id}.lock"
            lock_file.write_bytes(b"")

            ctx = multiprocessing.get_context("spawn")
            barrier = ctx.Barrier(2)
            result = ctx.Queue()
            processes = [
                ctx.Process(
                    target=_prepare_with_bad_lock,
                    args=(str(root), "shared-bucket", barrier, result),
                )
                for _ in range(2)
            ]
            try:
                for p in processes:
                    p.start()
                outcomes = [result.get(timeout=30) for _ in processes]
                for p in processes:
                    p.join(timeout=30)
                self.assertTrue(
                    all(o[0] == "error" for o in outcomes),
                    f"round {round_num}: both should reject, got {outcomes}",
                )
                self.assertTrue(
                    all("stuck at size 0" in o[2] or "invalid size" in o[2]
                        for o in outcomes),
                    f"round {round_num}: expected lock rejection, got {outcomes}",
                )
                self.assertEqual(lock_file.stat().st_size, 0,
                    f"round {round_num}: lock file should not be modified")
            finally:
                result.close()
                result.join_thread()
                for p in processes:
                    p.close()
                tempdir.cleanup()


def _make_valid_entry(bucket_id: str, state: str = STATE_COMMITTED,
                      entry_id: str | None = None) -> dict:
    eid = entry_id or "a" * 32
    return {
        "entry_id": eid,
        "state": state,
        "bucket_id": bucket_id,
        "old_sha256": "b" * 64,
        "new_sha256": "c" * 64,
        "merged_sha256": "d" * 64,
        "unmatched_verbatim_lines": [],
        "merge_method": "llm",
        "owner": "cheng",
        "created_at": "2026-01-01T00:00:00.000+00:00",
        "committed_at": "2026-01-01T00:00:01.000+00:00" if state == STATE_COMMITTED else None,
    }


def _make_valid_archive(bucket_id: str, **overrides) -> dict:
    data = {
        "schema": SCHEMA_VERSION,
        "bucket_id": bucket_id,
        "archived_at": "2026-01-01T00:00:00.000+00:00",
        "entries": [_make_valid_entry(bucket_id)],
    }
    data.update(overrides)
    return data


def _archive_filename(bucket_id: str, offset: int | None = None) -> str:
    suffix = f"_{offset}" if offset is not None else ""
    return f"{bucket_id}_20260101T000000Z_aabbccddeeff{suffix}.json"


class ValidateLockMemberTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _lock_id(self, bucket_id: str) -> str:
        import hashlib
        return hashlib.sha256(
            f"remainder-sidecar-{bucket_id}".encode("utf-8", errors="surrogatepass")
        ).hexdigest()

    def test_emitter_generated_lock_passes(self) -> None:
        entry, gen = prepare_remainder(
            self.root, "bucket-01", "old", "new", "merged", "llm", _META_CHENG,
        )
        lock_dir = self.root / ".remainders" / ".locks"
        lock_id = self._lock_id("bucket-01")
        lock_file = lock_dir / f"{lock_id}.lock"
        self.assertTrue(lock_file.exists())
        validate_lock_member(lock_file)

    def test_arbitrary_filename_rejected(self) -> None:
        f = self.root / "anything.lock"
        f.write_bytes(b"\0")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid filename"):
            validate_lock_member(f)

    def test_uppercase_hash_rejected(self) -> None:
        name = "A" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid filename"):
            validate_lock_member(f)

    def test_size_zero_rejected(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid content"):
            validate_lock_member(f)

    def test_size_two_rejected(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0\0")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid content"):
            validate_lock_member(f)

    def test_single_byte_x_rejected(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"X")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid content"):
            validate_lock_member(f)

    def test_nonexistent_rejected(self) -> None:
        f = self.root / ("a" * 64 + ".lock")
        with self.assertRaisesRegex(RemainderSidecarError, "does not exist"):
            validate_lock_member(f)

    def test_symlink_rejected(self) -> None:
        name = "a" * 64 + ".lock"
        real = self.root / "real.lock"
        real.write_bytes(b"\0")
        link = self.root / name
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        with self.assertRaisesRegex(RemainderSidecarError, "not a regular file"):
            validate_lock_member(link)

    def test_directory_rejected(self) -> None:
        name = "a" * 64 + ".lock"
        d = self.root / name
        d.mkdir()
        with self.assertRaisesRegex(RemainderSidecarError, "not a regular file"):
            validate_lock_member(d)

    def test_swap_aba_rejected(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")

        real_os_open = os.open
        swapped = [False]

        def swap_before_open(path, flags, *args, **kwargs):
            p = str(path)
            if (p.endswith(name) and not swapped[0]
                    and not (flags & (os.O_WRONLY | os.O_RDWR))):
                swapped[0] = True
                try:
                    os.unlink(p)
                    with open(p, "wb") as tmp:
                        tmp.write(b"\0")
                except OSError:
                    pass
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch.object(rs.os, "open", side_effect=swap_before_open):
            try:
                validate_lock_member(f)
                if not swapped[0]:
                    self.skipTest("swap did not execute")
                self.fail("should have raised RemainderSidecarError")
            except RemainderSidecarError as exc:
                self.assertIn("identity changed", str(exc))

    def test_read_only_no_modification(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")
        tree_before = sorted(
            (str(p.relative_to(self.root)), p.read_bytes())
            for p in self.root.rglob("*") if p.is_file()
        )
        validate_lock_member(f)
        tree_after = sorted(
            (str(p.relative_to(self.root)), p.read_bytes())
            for p in self.root.rglob("*") if p.is_file()
        )
        self.assertEqual(tree_before, tree_after)


class ValidateArchiveMemberTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _write_archive(self, filename: str, data: dict) -> Path:
        f = self.root / filename
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return f

    def test_emitter_generated_archive_passes(self) -> None:
        for i in range(55):
            entry, gen = prepare_remainder(
                self.root, "bucket-01", f"old-{i}", f"new-{i}",
                f"merged-{i}", "llm", _META_CHENG,
                current_content=f"merged-{i - 1}" if i > 0 else None,
            )
            commit_remainder(self.root, "bucket-01", entry.entry_id, gen)
        archive_if_needed(self.root, "bucket-01")
        archive_dir = self.root / ".remainders" / "archive"
        archive_files = list(archive_dir.glob("bucket-01_*.json"))
        self.assertGreaterEqual(len(archive_files), 1)
        for af in archive_files:
            validate_archive_member(af)

    def test_schema_999_rejected(self) -> None:
        data = _make_valid_archive("bucket-01", schema=999)
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "unsupported schema"):
            validate_archive_member(f)

    def test_filename_bucket_mismatch_rejected(self) -> None:
        data = _make_valid_archive("bucket-01")
        f = self._write_archive(_archive_filename("bucket-02"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "does not match filename"):
            validate_archive_member(f)

    def test_traversal_bucket_rejected(self) -> None:
        f = self.root / "..%2F..%2Fescape_20260101T000000Z_aabbccddeeff.json"
        data = _make_valid_archive("..%2F..%2Fescape")
        f.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid bucket_id"):
            validate_archive_member(f)

    def test_bad_timestamp_rejected(self) -> None:
        f = self.root / "bucket-01_BADTIME_aabbccddeeff.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid filename"):
            validate_archive_member(f)

    def test_bad_uid_rejected(self) -> None:
        f = self.root / "bucket-01_20260101T000000Z_ZZZZZZZZZZZZ.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid filename"):
            validate_archive_member(f)

    def test_bad_offset_leading_zero_rejected(self) -> None:
        f = self.root / "bucket-01_20260101T000000Z_aabbccddeeff_00.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid batch offset"):
            validate_archive_member(f)

    def test_offset_zero_rejected(self) -> None:
        f = self.root / "bucket-01_20260101T000000Z_aabbccddeeff_0.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid batch offset"):
            validate_archive_member(f)

    def test_offset_1_rejected(self) -> None:
        f = self.root / "bucket-01_20260101T000000Z_aabbccddeeff_1.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid batch offset"):
            validate_archive_member(f)

    def test_offset_199_rejected(self) -> None:
        f = self.root / "bucket-01_20260101T000000Z_aabbccddeeff_199.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid batch offset"):
            validate_archive_member(f)

    def test_offset_201_rejected(self) -> None:
        f = self.root / "bucket-01_20260101T000000Z_aabbccddeeff_201.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid batch offset"):
            validate_archive_member(f)

    def test_offset_200_passes(self) -> None:
        data = _make_valid_archive("bucket-01")
        f = self._write_archive(_archive_filename("bucket-01", offset=200), data)
        validate_archive_member(f)

    def test_offset_400_passes(self) -> None:
        data = _make_valid_archive("bucket-01")
        f = self._write_archive(_archive_filename("bucket-01", offset=400), data)
        validate_archive_member(f)

    def test_impossible_timestamp_rejected(self) -> None:
        f = self.root / "bucket-01_20261399T996099Z_aabbccddeeff.json"
        f.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid timestamp"):
            validate_archive_member(f)

    def test_invalid_json_rejected(self) -> None:
        f = self.root / _archive_filename("bucket-01")
        f.write_text("NOT JSON {{{", encoding="utf-8")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid JSON"):
            validate_archive_member(f)

    def test_invalid_utf8_rejected(self) -> None:
        f = self.root / _archive_filename("bucket-01")
        f.write_bytes(b"\xff\xfe invalid")
        with self.assertRaisesRegex(RemainderSidecarError, "invalid UTF-8"):
            validate_archive_member(f)

    def test_empty_entries_rejected(self) -> None:
        data = _make_valid_archive("bucket-01", entries=[])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "must not be empty"):
            validate_archive_member(f)

    def test_prepared_state_rejected(self) -> None:
        entry = _make_valid_entry("bucket-01", STATE_PREPARED)
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "non-archivable state"):
            validate_archive_member(f)

    def test_conflict_state_rejected(self) -> None:
        entry = _make_valid_entry("bucket-01", STATE_CONFLICT)
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "non-archivable state"):
            validate_archive_member(f)

    def test_duplicate_entry_id_rejected(self) -> None:
        e1 = _make_valid_entry("bucket-01", STATE_COMMITTED, "a" * 32)
        e2 = _make_valid_entry("bucket-01", STATE_ABORTED, "a" * 32)
        data = _make_valid_archive("bucket-01", entries=[e1, e2])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "duplicate entry_id"):
            validate_archive_member(f)

    def test_entry_bucket_mismatch_rejected(self) -> None:
        entry = _make_valid_entry("other-bucket")
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "does not match"):
            validate_archive_member(f)

    def test_bad_entry_shape_rejected(self) -> None:
        data = _make_valid_archive("bucket-01", entries=[{"bad": True}])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "invalid"):
            validate_archive_member(f)

    def test_bad_entry_hash_rejected(self) -> None:
        entry = _make_valid_entry("bucket-01")
        entry["old_sha256"] = "ZZZZ"
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "invalid old_sha256"):
            validate_archive_member(f)

    def test_bad_entry_owner_rejected(self) -> None:
        entry = _make_valid_entry("bucket-01")
        entry["owner"] = "INVALID OWNER!!"
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "invalid owner"):
            validate_archive_member(f)

    def test_committed_at_invariant_committed(self) -> None:
        entry = _make_valid_entry("bucket-01", STATE_COMMITTED)
        entry["committed_at"] = None
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "COMMITTED entry must have committed_at"):
            validate_archive_member(f)

    def test_committed_at_invariant_aborted(self) -> None:
        entry = _make_valid_entry("bucket-01", STATE_ABORTED)
        entry["committed_at"] = "2026-01-01T00:00:00Z"
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "must not have committed_at"):
            validate_archive_member(f)

    def test_unknown_top_level_keys_rejected(self) -> None:
        data = _make_valid_archive("bucket-01")
        data["extra_key"] = "surprise"
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "unknown top-level keys"):
            validate_archive_member(f)

    def test_symlink_rejected(self) -> None:
        real = self.root / "real.json"
        real.write_text(json.dumps(_make_valid_archive("bucket-01")), encoding="utf-8")
        link = self.root / _archive_filename("bucket-01")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        with self.assertRaisesRegex(RemainderSidecarError, "not a regular file"):
            validate_archive_member(link)

    def test_swap_aba_rejected(self) -> None:
        fname = _archive_filename("bucket-01")
        f = self.root / fname
        payload = json.dumps(_make_valid_archive("bucket-01"))
        f.write_text(payload, encoding="utf-8")

        real_os_open = os.open
        swapped = [False]

        def swap_before_open(path, flags, *args, **kwargs):
            p = str(path)
            if (p.endswith(fname) and not swapped[0]
                    and not (flags & (os.O_WRONLY | os.O_RDWR))):
                swapped[0] = True
                try:
                    os.unlink(p)
                    with open(p, "w", encoding="utf-8") as tmp:
                        tmp.write(payload)
                except OSError:
                    pass
            return real_os_open(path, flags, *args, **kwargs)

        with mock.patch.object(rs.os, "open", side_effect=swap_before_open):
            try:
                validate_archive_member(f)
                if not swapped[0]:
                    self.skipTest("swap did not execute")
                self.fail("should have raised RemainderSidecarError")
            except RemainderSidecarError as exc:
                self.assertIn("identity changed", str(exc))

    def test_read_only_no_modification(self) -> None:
        fname = _archive_filename("bucket-01")
        data = _make_valid_archive("bucket-01")
        f = self._write_archive(fname, data)
        content_before = f.read_bytes()
        validate_archive_member(f)
        content_after = f.read_bytes()
        self.assertEqual(content_before, content_after)
        tree_after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(tree_after, [fname])

    def test_nonexistent_rejected(self) -> None:
        f = self.root / _archive_filename("bucket-01")
        with self.assertRaisesRegex(RemainderSidecarError, "does not exist"):
            validate_archive_member(f)

    def test_empty_archived_at_rejected(self) -> None:
        data = _make_valid_archive("bucket-01", archived_at="")
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "empty archived_at"):
            validate_archive_member(f)

    def test_non_utc_archived_at_rejected(self) -> None:
        data = _make_valid_archive("bucket-01", archived_at="2026-01-01T00:00:00+05:00")
        f = self._write_archive(_archive_filename("bucket-01"), data)
        with self.assertRaisesRegex(RemainderSidecarError, "must be UTC"):
            validate_archive_member(f)

    def test_aborted_entry_passes(self) -> None:
        entry = _make_valid_entry("bucket-01", STATE_ABORTED)
        data = _make_valid_archive("bucket-01", entries=[entry])
        f = self._write_archive(_archive_filename("bucket-01"), data)
        validate_archive_member(f)

    def test_valid_offset_passes(self) -> None:
        data = _make_valid_archive("bucket-01")
        f = self._write_archive(_archive_filename("bucket-01", offset=200), data)
        validate_archive_member(f)

    def test_short_read_still_passes(self) -> None:
        fname = _archive_filename("bucket-01")
        data = _make_valid_archive("bucket-01")
        f = self._write_archive(fname, data)
        real_os_read = os.read
        call_count = [0]

        def chunked_read(fd, size):
            call_count[0] += 1
            if call_count[0] == 1:
                return real_os_read(fd, min(10, size))
            return real_os_read(fd, size)

        with mock.patch.object(rs.os, "read", side_effect=chunked_read):
            validate_archive_member(f)
        self.assertGreater(call_count[0], 1)

    def test_open_oserror_wrapped(self) -> None:
        fname = _archive_filename("bucket-01")
        f = self._write_archive(fname, _make_valid_archive("bucket-01"))

        def fail_open(path, flags, *a, **kw):
            raise PermissionError("SECRET_ARCHIVE_OPEN_789")

        with mock.patch.object(rs.os, "open", side_effect=fail_open):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_archive_member(f)
            self.assertNotIn("SECRET_ARCHIVE_OPEN_789", str(ctx.exception))
            self.assertIn("PermissionError", str(ctx.exception))

    def test_read_oserror_wrapped(self) -> None:
        fname = _archive_filename("bucket-01")
        f = self._write_archive(fname, _make_valid_archive("bucket-01"))

        real_os_open = os.open

        def fail_read(fd, size):
            raise OSError("SECRET_ARCHIVE_READ_789")

        def pass_open(path, flags, *a, **kw):
            return real_os_open(path, flags, *a, **kw)

        with mock.patch.object(rs.os, "open", side_effect=pass_open), \
             mock.patch.object(rs.os, "read", side_effect=fail_read):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_archive_member(f)
            self.assertNotIn("SECRET_ARCHIVE_READ_789", str(ctx.exception))

    def test_post_lstat_oserror_wrapped(self) -> None:
        fname = _archive_filename("bucket-01")
        f = self._write_archive(fname, _make_valid_archive("bucket-01"))
        real_lstat = os.lstat
        call_count = [0]

        def fail_post_lstat(path):
            call_count[0] += 1
            if call_count[0] >= 2 and str(path).endswith(fname):
                raise OSError("SECRET_POST_LSTAT_789")
            return real_lstat(path)

        with mock.patch.object(rs.os, "lstat", side_effect=fail_post_lstat):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_archive_member(f)
            self.assertNotIn("SECRET_POST_LSTAT_789", str(ctx.exception))

    def test_close_oserror_wrapped(self) -> None:
        fname = _archive_filename("bucket-01")
        f = self._write_archive(fname, _make_valid_archive("bucket-01"))
        real_os_close = os.close

        def fail_close(fd):
            real_os_close(fd)
            raise OSError("SECRET_ARCHIVE_CLOSE_789")

        with mock.patch.object(rs.os, "close", side_effect=fail_close):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_archive_member(f)
            self.assertNotIn("SECRET_ARCHIVE_CLOSE_789", str(ctx.exception))
            self.assertIn("close", str(ctx.exception).lower())

    def test_read_fail_plus_close_fail_combined(self) -> None:
        fname = _archive_filename("bucket-01")
        f = self._write_archive(fname, _make_valid_archive("bucket-01"))
        real_os_open = os.open
        real_os_close = os.close

        def fail_read(fd, size):
            raise OSError("SECRET_ARCHIVE_READ_COMBO_789")

        def pass_open(path, flags, *a, **kw):
            return real_os_open(path, flags, *a, **kw)

        def fail_close(fd):
            real_os_close(fd)
            raise OSError("SECRET_ARCHIVE_CLOSE_COMBO_789")

        with mock.patch.object(rs.os, "open", side_effect=pass_open), \
             mock.patch.object(rs.os, "read", side_effect=fail_read), \
             mock.patch.object(rs.os, "close", side_effect=fail_close):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_archive_member(f)
            msg = str(ctx.exception)
            self.assertNotIn("SECRET_ARCHIVE_READ_COMBO_789", msg)
            self.assertNotIn("SECRET_ARCHIVE_CLOSE_COMBO_789", msg)


class ValidateLockIOTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_open_oserror_wrapped(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")

        def fail_open(path, flags, *a, **kw):
            raise PermissionError("SECRET_LOCK_OPEN_789")

        with mock.patch.object(rs.os, "open", side_effect=fail_open):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_lock_member(f)
            self.assertNotIn("SECRET_LOCK_OPEN_789", str(ctx.exception))
            self.assertIn("PermissionError", str(ctx.exception))

    def test_read_oserror_wrapped(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")
        real_os_open = os.open

        def fail_read(fd, size):
            raise OSError("SECRET_LOCK_READ_789")

        def pass_open(path, flags, *a, **kw):
            return real_os_open(path, flags, *a, **kw)

        with mock.patch.object(rs.os, "open", side_effect=pass_open), \
             mock.patch.object(rs.os, "read", side_effect=fail_read):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_lock_member(f)
            self.assertNotIn("SECRET_LOCK_READ_789", str(ctx.exception))

    def test_post_lstat_oserror_wrapped(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")
        real_lstat = os.lstat
        call_count = [0]

        def fail_post_lstat(path):
            call_count[0] += 1
            if call_count[0] >= 2 and str(path).endswith(".lock"):
                raise OSError("SECRET_POST_LOCK_789")
            return real_lstat(path)

        with mock.patch.object(rs.os, "lstat", side_effect=fail_post_lstat):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_lock_member(f)
            self.assertNotIn("SECRET_POST_LOCK_789", str(ctx.exception))

    def test_close_oserror_wrapped(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")
        real_os_close = os.close

        def fail_close(fd):
            real_os_close(fd)
            raise OSError("SECRET_LOCK_CLOSE_789")

        with mock.patch.object(rs.os, "close", side_effect=fail_close):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_lock_member(f)
            self.assertNotIn("SECRET_LOCK_CLOSE_789", str(ctx.exception))
            self.assertIn("close", str(ctx.exception).lower())

    def test_read_fail_plus_close_fail_combined(self) -> None:
        name = "a" * 64 + ".lock"
        f = self.root / name
        f.write_bytes(b"\0")
        real_os_open = os.open
        real_os_close = os.close

        def fail_read(fd, size):
            raise OSError("SECRET_LOCK_READ_COMBO_789")

        def pass_open(path, flags, *a, **kw):
            return real_os_open(path, flags, *a, **kw)

        def fail_close(fd):
            real_os_close(fd)
            raise OSError("SECRET_LOCK_CLOSE_COMBO_789")

        with mock.patch.object(rs.os, "open", side_effect=pass_open), \
             mock.patch.object(rs.os, "read", side_effect=fail_read), \
             mock.patch.object(rs.os, "close", side_effect=fail_close):
            with self.assertRaises(RemainderSidecarError) as ctx:
                validate_lock_member(f)
            msg = str(ctx.exception)
            self.assertNotIn("SECRET_LOCK_READ_COMBO_789", msg)
            self.assertNotIn("SECRET_LOCK_CLOSE_COMBO_789", msg)


if __name__ == "__main__":
    unittest.main()
