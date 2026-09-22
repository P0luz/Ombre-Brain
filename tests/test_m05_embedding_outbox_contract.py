from __future__ import annotations

import errno
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import embedding_outbox as eo
from embedding_outbox import (
    EmbeddingOutboxError,
    OP_DELETE,
    OP_UPSERT,
    STATE_ABORTED,
    STATE_APPLIED,
    STATE_CONFLICT,
    STATE_PREPARED,
    STATE_READY,
    StaleIntentError,
    classify_intent,
    load_intent,
    load_slot,
    list_intents,
    mark_applied,
    prepare_delete,
    prepare_upsert,
    recover_intent,
    transition_intent,
)


def _prepare_in_child(root: str, content: str, start, result) -> None:
    start.wait(10)
    try:
        intent = prepare_upsert(root, "shared-bucket", None, content)
        result.put(("ok", intent.generation, intent.intent_id))
    except BaseException as exc:
        result.put(("error", type(exc).__name__, str(exc)))


class EmbeddingOutboxContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_upsert_prepare_is_private_and_durable(self) -> None:
        private_content = "只应存在于权威 Markdown 的正文"
        intent = prepare_upsert(self.root, "bucket-01", None, private_content)
        path = self.root / "bucket-01.json"

        self.assertEqual(load_intent(path), intent)
        self.assertEqual(intent.operation, OP_UPSERT)
        self.assertEqual(intent.state, STATE_PREPARED)
        self.assertNotIn(private_content, path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_recovery_distinguishes_commit_abort_and_conflict(self) -> None:
        committed = prepare_upsert(self.root, "commit", "old", "new")
        ready = recover_intent(self.root, committed, "new")
        self.assertEqual(ready.state, STATE_READY)

        not_committed = prepare_upsert(self.root, "abort", "old", "new")
        aborted = recover_intent(self.root, not_committed, "old")
        self.assertEqual(aborted.state, STATE_ABORTED)

        unexpected = prepare_upsert(self.root, "conflict", "old", "new")
        conflict = recover_intent(self.root, unexpected, "third version")
        self.assertEqual(conflict.state, STATE_CONFLICT)

    def test_create_recovery_uses_missing_before_state(self) -> None:
        committed = prepare_upsert(self.root, "created", None, "new")
        self.assertEqual(classify_intent(committed, "new"), STATE_READY)

        not_committed = prepare_upsert(self.root, "missing", None, "new")
        self.assertEqual(classify_intent(not_committed, None), STATE_ABORTED)

    def test_delete_is_a_durable_operation(self) -> None:
        committed = prepare_delete(self.root, "deleted", "old")
        self.assertEqual(committed.operation, OP_DELETE)
        self.assertEqual(classify_intent(committed, None), STATE_READY)

        not_committed = prepare_delete(self.root, "kept", "old")
        self.assertEqual(classify_intent(not_committed, "old"), STATE_ABORTED)

        raced = prepare_delete(self.root, "raced", "old")
        self.assertEqual(classify_intent(raced, "newer"), STATE_CONFLICT)

    def test_conflict_is_terminal_and_requires_a_fresh_intent(self) -> None:
        prepared = prepare_upsert(self.root, "bucket-01", "old", "new")
        conflict = recover_intent(self.root, prepared, "unexpected")
        with self.assertRaisesRegex(EmbeddingOutboxError, "invalid state transition"):
            transition_intent(self.root, conflict, STATE_READY)

        fresh = prepare_upsert(self.root, "bucket-01", "unexpected", "fixed")
        self.assertEqual(fresh.generation, conflict.generation + 1)

    def test_generation_cas_prevents_old_worker_clobbering_latest(self) -> None:
        old = prepare_upsert(self.root, "bucket-01", "a", "b")
        latest = prepare_upsert(self.root, "bucket-01", "b", "c")

        self.assertEqual(latest.generation, old.generation + 1)
        with self.assertRaisesRegex(StaleIntentError, "newer generation"):
            transition_intent(self.root, old, STATE_READY)
        self.assertEqual(load_slot(self.root, "bucket-01"), latest)

    def test_slot_lease_serializes_two_spawned_processes(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        result = ctx.Queue()
        processes = [
            ctx.Process(
                target=_prepare_in_child,
                args=(str(self.root), content, start, result),
            )
            for content in ("from process one", "from process two")
        ]
        for process in processes:
            process.start()
        start.set()
        outcomes = [result.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            self.assertEqual(process.exitcode, 0)

        self.assertTrue(all(outcome[0] == "ok" for outcome in outcomes), outcomes)
        self.assertEqual(sorted(outcome[1] for outcome in outcomes), [1, 2])
        self.assertEqual(load_slot(self.root, "shared-bucket").generation, 2)

    @unittest.skipUnless(os.name == "nt", "Windows byte-lease regression")
    def test_slot_file_initialization_occurs_after_lease_acquisition(self) -> None:
        import msvcrt

        events: list[str] = []

        class FakeHandle:
            def __init__(self) -> None:
                self.position = 0
                self.size = 0

            def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
                self.position = self.size + offset if whence == os.SEEK_END else offset
                return self.position

            def tell(self) -> int:
                return self.position

            def write(self, payload: bytes) -> int:
                events.append("write")
                self.position += len(payload)
                self.size = max(self.size, self.position)
                return len(payload)

            def fileno(self) -> int:
                return 123

            def close(self) -> None:
                events.append("close")

        attempts = 0

        def fake_locking(_fd: int, mode: int, _length: int) -> None:
            nonlocal attempts
            if mode == msvcrt.LK_NBLCK:
                attempts += 1
                if attempts == 1:
                    events.append("lock_busy")
                    raise PermissionError(errno.EACCES, "simulated busy lease")
                events.append("lock_acquired")
            else:
                self.assertEqual(mode, msvcrt.LK_UNLCK)
                events.append("unlock")

        handle = FakeHandle()
        with (
            mock.patch.object(eo.os, "open", return_value=123),
            mock.patch.object(eo.os, "fdopen", return_value=handle),
            mock.patch.object(msvcrt, "locking", side_effect=fake_locking),
            mock.patch.object(eo.time, "sleep"),
        ):
            with eo._slot_turn(self.root, "shared-bucket"):
                events.append("yield")

        self.assertEqual(
            events,
            ["lock_busy", "lock_acquired", "write", "yield", "unlock", "close"],
        )

    def test_applied_is_only_reached_after_ready(self) -> None:
        prepared = prepare_upsert(self.root, "bucket-01", None, "new")
        with self.assertRaisesRegex(EmbeddingOutboxError, "invalid state transition"):
            mark_applied(self.root, prepared)

        ready = recover_intent(self.root, prepared, "new")
        applied = mark_applied(self.root, ready)
        self.assertEqual(applied.state, STATE_APPLIED)

    def test_empty_upsert_is_rejected(self) -> None:
        with self.assertRaisesRegex(EmbeddingOutboxError, "non-empty"):
            prepare_upsert(self.root, "bucket-01", None, "  \n")

    def test_failed_replace_preserves_previous_authority_and_cleans_temp(self) -> None:
        previous = prepare_upsert(self.root, "bucket-01", "a", "b")
        real_replace = eo.os.replace

        def fail_outbox_replace(source, target):
            if Path(target).name == "bucket-01.json":
                raise OSError("injected replace failure")
            return real_replace(source, target)

        with mock.patch.object(eo.os, "replace", side_effect=fail_outbox_replace):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                prepare_upsert(self.root, "bucket-01", "b", "c")

        self.assertEqual(load_slot(self.root, "bucket-01"), previous)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_corrupt_or_path_like_inputs_fail_closed(self) -> None:
        with self.assertRaisesRegex(EmbeddingOutboxError, "invalid bucket_id"):
            prepare_upsert(self.root, "../escape", None, "content")

        corrupt = self.root / "corrupt.json"
        corrupt.write_text(json.dumps({"schema": 2}), encoding="utf-8")
        with self.assertRaisesRegex(
            EmbeddingOutboxError, "invalid embedding intent shape"
        ):
            load_intent(corrupt)

    def test_root_lock_dir_and_intent_must_be_regular(self) -> None:
        root_file = self.root / "not-a-directory"
        root_file.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(EmbeddingOutboxError, "not a regular directory"):
            prepare_upsert(root_file, "bucket-01", None, "content")

        bad_locks_root = self.root / "bad-locks"
        bad_locks_root.mkdir()
        (bad_locks_root / ".locks").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(EmbeddingOutboxError, "lock directory"):
            prepare_upsert(bad_locks_root, "bucket-01", None, "content")

        bad_intent_root = self.root / "bad-intent"
        bad_intent_root.mkdir()
        (bad_intent_root / "bucket-01.json").mkdir()
        with self.assertRaisesRegex(EmbeddingOutboxError, "not a regular file"):
            prepare_upsert(bad_intent_root, "bucket-01", None, "content")

    def test_symlink_root_and_intent_fail_closed_when_supported(self) -> None:
        target_root = self.root / "target-root"
        target_root.mkdir()
        linked_root = self.root / "linked-root"
        try:
            os.symlink(target_root, linked_root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaisesRegex(EmbeddingOutboxError, "not a regular directory"):
            prepare_upsert(linked_root, "bucket-01", None, "content")

        intent_root = self.root / "intent-root"
        intent_root.mkdir()
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        os.symlink(target, intent_root / "bucket-01.json")
        with self.assertRaisesRegex(EmbeddingOutboxError, "not a regular file"):
            prepare_upsert(intent_root, "bucket-01", None, "content")

    def test_list_intents_is_read_only_sorted_and_filterable(self) -> None:
        absent = self.root / "absent"
        self.assertEqual(list_intents(absent), ())
        self.assertFalse(absent.exists())

        first = prepare_upsert(self.root, "b-slot", None, "b")
        second = prepare_upsert(self.root, "a-slot", None, "a")
        ready = recover_intent(self.root, second, "a")

        self.assertEqual(
            [intent.bucket_id for intent in list_intents(self.root)],
            ["a-slot", "b-slot"],
        )
        self.assertEqual(list_intents(self.root, STATE_READY), (ready,))
        self.assertEqual(list_intents(self.root, STATE_PREPARED), (first,))
        with self.assertRaisesRegex(EmbeddingOutboxError, "invalid state filter"):
            list_intents(self.root, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
