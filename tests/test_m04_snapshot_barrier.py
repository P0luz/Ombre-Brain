from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from snapshot_barrier import (  # noqa: E402
    SnapshotBarrierError,
    SnapshotBarrierTimeout,
    SnapshotPreconditionError,
    authoritative_markdown_snapshot_turn,
    future_markdown_writer_lock_order,
    markdown_writer_turn,
    markdown_writer_turn_sync,
)
import snapshot_barrier as barrier  # noqa: E402


def _process_shared_writer(
    root: str,
    locks: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    """Spawn-safe peer used to prove the sync adapter is process-wide."""

    with markdown_writer_turn_sync(root, lock_root=locks):
        entered.set()
        release.wait(timeout=5)


class SnapshotBarrierTests(unittest.IsolatedAsyncioTestCase):
    def make_vault(self, temp: str) -> tuple[Path, Path]:
        root = Path(temp) / "vault"
        marker = root / "dynamic" / "one.md"
        marker.parent.mkdir(parents=True)
        marker.write_text("---\nid: one\n---\nbody\n", encoding="utf-8")
        return root, Path(temp) / "runtime-locks"

    async def test_shared_writers_overlap_and_exclusive_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            both_entered = asyncio.Event()
            release_writers = asyncio.Event()
            active = 0

            async def writer() -> None:
                nonlocal active
                async with markdown_writer_turn(root, lock_root=locks):
                    active += 1
                    if active == 2:
                        both_entered.set()
                    await release_writers.wait()

            first = asyncio.create_task(writer())
            second = asyncio.create_task(writer())
            await asyncio.wait_for(both_entered.wait(), 1)
            self.assertEqual(active, 2)
            exclusive_entered = asyncio.Event()

            async def snapshot() -> None:
                async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                    exclusive_entered.set()

            pending = asyncio.create_task(snapshot())
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(exclusive_entered.wait(), 0.08)
            release_writers.set()
            await asyncio.gather(first, second, pending)
            self.assertTrue(exclusive_entered.is_set())

    async def test_exclusive_snapshot_blocks_a_future_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            writer_entered = asyncio.Event()
            async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                task = asyncio.create_task(self._writer(root, locks, writer_entered))
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(writer_entered.wait(), 0.08)
            await asyncio.wait_for(task, 1)
            self.assertTrue(writer_entered.is_set())

    async def test_timeout_and_cancellation_release_no_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                with self.assertRaises(SnapshotBarrierTimeout):
                    async with markdown_writer_turn(root, lock_root=locks, timeout_seconds=0.03):
                        self.fail("writer must not enter while exclusive is held")
                blocked = asyncio.create_task(markdown_writer_turn(root, lock_root=locks).__aenter__())
                await asyncio.sleep(0.03)
                blocked.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await blocked
            async with markdown_writer_turn(root, lock_root=locks, timeout_seconds=0.5):
                pass

    async def test_sync_and_async_writers_share_one_barrier_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            sync_entered = threading.Event()
            release_sync = threading.Event()

            def sync_holder() -> None:
                with markdown_writer_turn_sync(root, lock_root=locks):
                    sync_entered.set()
                    release_sync.wait(timeout=2)

            thread = threading.Thread(target=sync_holder)
            thread.start()
            self.assertTrue(sync_entered.wait(timeout=1))
            async_entered = asyncio.Event()
            async with markdown_writer_turn(root, lock_root=locks):
                async_entered.set()
            self.assertTrue(async_entered.is_set())
            release_sync.set()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())

            blocked_sync_entered = threading.Event()

            def blocked_sync_writer() -> None:
                with markdown_writer_turn_sync(root, lock_root=locks):
                    blocked_sync_entered.set()

            async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                task = asyncio.create_task(asyncio.to_thread(blocked_sync_writer))
                await asyncio.sleep(0.08)
                self.assertFalse(blocked_sync_entered.is_set())
            await asyncio.wait_for(task, 1)
            self.assertTrue(blocked_sync_entered.is_set())

    async def test_cross_process_sync_writer_uses_the_async_barrier_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            context = multiprocessing.get_context("spawn")
            entered = context.Event()
            release = context.Event()
            process = context.Process(
                target=_process_shared_writer,
                args=(str(root), str(locks), entered, release),
            )
            process.start()
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                async with markdown_writer_turn(root, lock_root=locks):
                    pass
            finally:
                release.set()
                await asyncio.to_thread(process.join, 3)
            self.assertEqual(process.exitcode, 0)

            blocked_entered = context.Event()
            blocked_release = context.Event()
            async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                blocked = context.Process(
                    target=_process_shared_writer,
                    args=(str(root), str(locks), blocked_entered, blocked_release),
                )
                blocked.start()
                await asyncio.sleep(0.12)
                self.assertFalse(blocked_entered.is_set())
            try:
                self.assertTrue(await asyncio.to_thread(blocked_entered.wait, 2))
            finally:
                blocked_release.set()
                await asyncio.to_thread(blocked.join, 3)
            self.assertEqual(blocked.exitcode, 0)

    async def test_corrupt_lock_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            path = barrier._barrier_lock_path(root.resolve(), locks)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"corrupt")
            with self.assertRaises(SnapshotBarrierError):
                async with markdown_writer_turn(root, lock_root=locks):
                    self.fail("unsafe lock state must not enter")
            with self.assertRaises(SnapshotBarrierError):
                with markdown_writer_turn_sync(root, lock_root=locks):
                    self.fail("sync writer must also fail closed")

    async def test_future_lock_order_is_fixed_and_documented(self) -> None:
        self.assertEqual(
            future_markdown_writer_lock_order(),
            (
                "m04-markdown-snapshot(shared)",
                "m03-import-apply (M-03 only)",
                "m01-content-quota-pinned (M-03 only)",
                "m01-content-quota-high_importance (M-03 only)",
                "m01-sorted-bucket-leases (M-03 only)",
            ),
        )

    async def test_active_m03_journal_refuses_without_vault_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            txid = "a" * 32
            txdir = root / ".import-transactions" / txid
            txdir.mkdir(parents=True)
            (txdir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "txid": txid, "state": "PUBLISHING", "entries": []}),
                encoding="utf-8",
            )
            before = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(SnapshotPreconditionError, "active M-03"):
                async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                    pass
            after = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    async def test_active_emig_journal_refuses_and_terminal_markers_allow_coordination(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, locks = self.make_vault(temp)
            manifest = root / ".embedding-publish.json"
            manifest.write_text(json.dumps({"schema": "emig-1", "state": "DB_PUBLISHED"}), encoding="utf-8")
            with self.assertRaisesRegex(SnapshotPreconditionError, "active E-MIG"):
                async with authoritative_markdown_snapshot_turn(root, lock_root=locks):
                    pass
            manifest.write_text(json.dumps({"schema": "emig-1", "state": "COMMITTED"}), encoding="utf-8")
            txid = "b" * 32
            txdir = root / ".import-transactions" / txid
            txdir.mkdir(parents=True)
            (txdir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "txid": txid, "state": "COMMITTED", "entries": []}),
                encoding="utf-8",
            )
            async with authoritative_markdown_snapshot_turn(root, lock_root=locks) as marker:
                self.assertEqual(marker.sqlite_policy, "derived:not-captured:rebuild-required")
                self.assertEqual(marker.emig_state, "COMMITTED")
                self.assertEqual(marker.terminal_m03_transactions, (txid,))
            self.assertFalse((root / ".locks").exists())

    async def _writer(self, root: Path, locks: Path, entered: asyncio.Event) -> None:
        async with markdown_writer_turn(root, lock_root=locks):
            entered.set()


if __name__ == "__main__":
    unittest.main()
