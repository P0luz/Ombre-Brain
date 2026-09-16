"""Temporary-vault coverage for M-04 short-freeze Markdown staging copies."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import backup_archive as archive  # noqa: E402
from backup_archive import BackupArchiveError  # noqa: E402
from snapshot_barrier import markdown_writer_turn, markdown_writer_turn_sync  # noqa: E402


def write_bucket(root: Path, directory: str, bucket_id: str, body: str, **metadata: object) -> Path:
    fields = {"id": bucket_id, "type": "dynamic", "tags": ["owner:cheng"], **metadata}
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", body, ""])
    target = root / directory / "nested" / f"{bucket_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("\n".join(lines).encode("utf-8"))
    return target


class M04StagingCopyTests(unittest.IsolatedAsyncioTestCase):
    def make_vault(self, temp: str) -> tuple[Path, Path, dict[str, Path]]:
        root = Path(temp) / "vault"
        root.mkdir()
        paths = {
            "permanent": write_bucket(root, "permanent", "anchor", "anchor"),
            "dynamic": write_bucket(root, "dynamic", "event", "event"),
            "feel": write_bucket(root, "feel", "feel", "feel", source_bucket="event"),
            "plans": write_bucket(root, "plans", "plan", "plan", source_refs=["bucket:anchor"]),
            "letters": write_bucket(root, "letters", "letter", "letter"),
            "archive": write_bucket(root, "archive", "old", "old"),
        }
        parent = Path(temp) / "staging-parent"
        parent.mkdir()
        return root, parent, paths

    async def stage(self, root: Path, parent: Path, **kwargs):
        return await archive.create_authoritative_markdown_staging(
            root,
            parent,
            created_at="2026-08-01T00:00:00Z",
            **kwargs,
        )

    async def test_byte_exact_copy_all_six_roots_manifest_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, _paths = self.make_vault(temp)
            (root / "embeddings.db").write_bytes(b"derived sqlite is excluded")
            (root / "config.yaml").write_text("secret: excluded", encoding="utf-8")
            (root / "raw-evidence").mkdir()
            (root / "raw-evidence" / "body.txt").write_text("excluded", encoding="utf-8")
            (root / ".embedding-publish.json").write_text(
                json.dumps({"schema": "emig-1", "state": "COMMITTED"}), encoding="utf-8"
            )

            result = await self.stage(root, parent)
            stage = Path(result["staging_root"])
            original = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*.md")}
            copied = {path.relative_to(stage).as_posix(): path.read_bytes() for path in stage.rglob("*.md")}
            self.assertEqual(copied, original)
            self.assertEqual(result["backup_manifest"]["file_count"], 6)
            self.assertEqual(result["backup_manifest"]["reference_closure"]["bucket_ids"], [
                "anchor", "event", "feel", "letter", "old", "plan"
            ])
            self.assertEqual(result["sqlite_policy"], "derived:not-captured:rebuild-required")
            self.assertEqual(
                {child.name for child in stage.iterdir()},
                {"permanent", "dynamic", "feel", "plans", "letters", "archive", archive.STAGING_MARKER_NAME},
            )
            self.assertEqual(archive.verify_authoritative_markdown_staging(stage), result)

    async def test_exclusive_blocks_async_and_sync_writers_without_interleaving(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, paths = self.make_vault(temp)
            locks = Path(temp) / "locks"
            async_entered = asyncio.Event()
            sync_entered = threading.Event()
            started = False
            sync_thread: threading.Thread | None = None

            async def async_writer() -> None:
                async with markdown_writer_turn(root, lock_root=locks):
                    async_entered.set()
                    paths["dynamic"].write_bytes(paths["dynamic"].read_bytes() + b"after-freeze")

            def sync_writer() -> None:
                with markdown_writer_turn_sync(root, lock_root=locks):
                    sync_entered.set()

            def inject(phase: str) -> None:
                nonlocal started, sync_thread
                if phase == "copy.after" and not started:
                    started = True
                    asyncio.create_task(async_writer())
                    sync_thread = threading.Thread(target=sync_writer)
                    sync_thread.start()
                elif phase == "copy.before" and started:
                    self.assertFalse(async_entered.is_set())
                    self.assertFalse(sync_entered.is_set())

            result = await self.stage(root, parent, lock_root=locks, fault_injector=inject)
            self.assertTrue(started)
            await asyncio.wait_for(async_entered.wait(), 1)
            assert sync_thread is not None
            sync_thread.join(timeout=1)
            self.assertFalse(sync_thread.is_alive())
            self.assertTrue(sync_entered.is_set())
            staged_dynamic = Path(result["staging_root"]) / paths["dynamic"].relative_to(root)
            self.assertNotIn(b"after-freeze", staged_dynamic.read_bytes())
            self.assertIn(b"after-freeze", paths["dynamic"].read_bytes())

    async def test_barrier_is_released_before_full_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, _paths = self.make_vault(temp)
            locks = Path(temp) / "locks"
            loop = asyncio.get_running_loop()
            writer_entered = asyncio.Event()
            original_verify = archive.verify_authoritative_markdown_staging

            async def writer() -> None:
                async with markdown_writer_turn(root, lock_root=locks):
                    writer_entered.set()

            def verify_with_writer(stage: str | Path):
                future = asyncio.run_coroutine_threadsafe(writer(), loop)
                future.result(timeout=1)
                return original_verify(stage)

            with patch.object(archive, "verify_authoritative_markdown_staging", verify_with_writer):
                await self.stage(root, parent, lock_root=locks)
            self.assertTrue(writer_entered.is_set())

    async def test_active_or_corrupt_journals_refuse_before_staging_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, _paths = self.make_vault(temp)
            txdir = root / ".import-transactions" / ("a" * 32)
            txdir.mkdir(parents=True)
            (txdir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "txid": "a" * 32, "state": "PUBLISHING"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "active M-03"):
                await self.stage(root, parent)
            self.assertEqual(list(parent.iterdir()), [])

            (txdir / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "txid": "a" * 32, "state": "COMMITTED"}),
                encoding="utf-8",
            )
            (root / ".embedding-publish.json").write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "E-MIG manifest"):
                await self.stage(root, parent)
            self.assertEqual(list(parent.iterdir()), [])

    async def test_path_safety_and_identity_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, paths = self.make_vault(temp)
            (root / "dynamic" / "nested" / "not-markdown.txt").write_text("no", encoding="utf-8")
            with self.assertRaisesRegex(BackupArchiveError, "non-Markdown"):
                await self.stage(root, parent)
            quarantines = list(parent.iterdir())
            self.assertEqual(len(quarantines), 1)
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.verify_authoritative_markdown_staging(quarantines[0])
            (root / "dynamic" / "nested" / "not-markdown.txt").unlink()

            expected = archive._identity(paths["dynamic"].stat())
            broken = (*expected[:-1], expected[-1] + 1)
            with self.assertRaisesRegex(BackupArchiveError, "identity changed"):
                archive._copy_source_file(paths["dynamic"], parent / "copy.md", broken)
            with self.assertRaisesRegex(BackupArchiveError, "unsafe path"):
                archive._normalize_member_path("buckets/dynamic/../escape.md")
            with self.assertRaisesRegex(BackupArchiveError, "non-portable"):
                archive._normalize_member_path("buckets/dynamic/name:.md")
            folded: set[str] = set()
            archive._claim_member_path("buckets/dynamic/A.md", folded)
            with self.assertRaisesRegex(BackupArchiveError, "case-insensitive duplicate"):
                archive._claim_member_path("buckets/dynamic/a.md", folded)

            unexpected_directory = root / "dynamic" / "nested" / "folder.md"
            unexpected_directory.mkdir()
            with self.assertRaisesRegex(BackupArchiveError, "unexpected Markdown directory"):
                await self.stage(root, parent)
            unexpected_directory.rmdir()

            link = root / "dynamic" / "nested" / "link.md"
            try:
                link.symlink_to(paths["dynamic"])
            except OSError as exc:  # pragma: no cover - depends on Windows symlink policy
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(BackupArchiveError, "symlink/reparse"):
                await self.stage(root, parent)

    async def test_copy_failure_and_cancellation_leave_only_incomplete_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, _paths = self.make_vault(temp)

            def disk_full(phase: str) -> None:
                if phase == "copy.before":
                    raise OSError(errno.ENOSPC, "disk full")

            with self.assertRaises(OSError):
                await self.stage(root, parent, fault_injector=disk_full)
            quarantines = list(parent.iterdir())
            self.assertEqual(len(quarantines), 1)
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.verify_authoritative_markdown_staging(quarantines[0])

            def cancelled(phase: str) -> None:
                if phase == "copy.before":
                    raise asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                await self.stage(root, parent, fault_injector=cancelled)
            self.assertEqual(len(list(parent.iterdir())), 2)
            for quarantine in parent.iterdir():
                with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                    archive.verify_authoritative_markdown_staging(quarantine)

    async def test_source_mutation_after_release_cannot_change_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, parent, paths = self.make_vault(temp)
            result = await self.stage(root, parent)
            stage = Path(result["staging_root"])
            staged = stage / paths["feel"].relative_to(root)
            before = staged.read_bytes()
            paths["feel"].write_bytes(b"---\nid: feel\n---\nchanged after release\n")
            self.assertEqual(staged.read_bytes(), before)
            self.assertEqual(archive.verify_authoritative_markdown_staging(stage), result)


if __name__ == "__main__":
    unittest.main()
