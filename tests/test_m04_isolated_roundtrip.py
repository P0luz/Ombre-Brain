"""M-04 schema-1 staging -> archive -> empty-vault isolated proof tests."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import backup_archive as archive  # noqa: E402
from backup_archive import BackupArchiveError  # noqa: E402
from snapshot_barrier import markdown_writer_turn  # noqa: E402


def write_bucket(root: Path, directory: str, relative: str, bucket_id: str, body: str, **metadata: object) -> Path:
    fields = {"id": bucket_id, "type": "dynamic", "tags": ["owner:cheng"], **metadata}
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", body, ""])
    target = root / directory / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes("\n".join(lines).encode("utf-8"))
    return target


class M04IsolatedRoundTripTests(unittest.IsolatedAsyncioTestCase):
    def make_source(self, temp: str, *, all_roots: bool = True) -> tuple[Path, Path, Path, Path]:
        root = Path(temp) / "source"
        root.mkdir()
        write_bucket(root, "dynamic", "工作/事件.md", "event", "你好，世界")
        if all_roots:
            write_bucket(root, "permanent", "anchors/锚.md", "anchor", "anchor")
            write_bucket(root, "feel", "情绪/feel.md", "feel", "feel", source_bucket="event")
            write_bucket(root, "plans", "nested/计划.md", "plan", "plan", source_refs=["bucket:anchor"])
            write_bucket(root, "letters", "历史/信.md", "letter", "letter", evidence_id="salon:42")
            write_bucket(root, "archive", "old/旧.md", "old", "old", source_refs=["external:evidence:9"])
        staging_parent = Path(temp) / "staging-parent"
        staging_parent.mkdir()
        archive_path = Path(temp) / "snapshot.zip"
        restore_parent = Path(temp) / "restore-parent"
        restore_parent.mkdir()
        return root, staging_parent, archive_path, restore_parent

    async def test_complete_six_root_non_ascii_round_trip_is_byte_and_manifest_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, staging_parent, archive_path, restore_parent = self.make_source(temp)
            (source / "config.yaml").write_text("secret: excluded", encoding="utf-8")
            (source / "embeddings.db").write_bytes(b"derived")
            (source / "raw-evidence").mkdir()
            (source / "raw-evidence" / "body.bin").write_bytes(b"excluded")

            result = await archive.perform_isolated_markdown_round_trip(
                source, staging_parent, archive_path, restore_parent,
                created_at="2026-08-01T00:00:00Z",
            )
            staging_root = Path(result["staging"]["staging_root"])
            restored_root = Path(result["restore"]["restored_root"])
            staged = {p.relative_to(staging_root).as_posix(): p.read_bytes() for p in staging_root.rglob("*.md")}
            restored = {
                p.relative_to(restored_root / "buckets").as_posix(): p.read_bytes()
                for p in (restored_root / "buckets").rglob("*.md")
            }
            self.assertEqual(staged, restored)
            self.assertEqual(result["file_count"], 6)
            self.assertEqual(result["staging"]["backup_manifest"], result["archive_manifest"])
            self.assertEqual(result["archive_manifest"], result["restore"]["backup_manifest"])
            closure = result["archive_manifest"]["reference_closure"]
            self.assertEqual(closure["bucket_ids"], ["anchor", "event", "feel", "letter", "old", "plan"])
            self.assertEqual(closure["internal_edges"], [
                {"from": "feel", "field": "source_bucket", "to": "event"},
                {"from": "plan", "field": "source_refs", "to": "anchor"},
            ])
            self.assertEqual(closure["external_evidence_refs"], ["external:evidence:9", "salon:42"])
            self.assertEqual(result["restore"]["sqlite_policy"], "derived:not-captured:rebuild-required")
            self.assertEqual({item.name for item in restored_root.iterdir()}, {"buckets", archive.RESTORE_MARKER_NAME})
            self.assertFalse((restored_root / "buckets" / "config.yaml").exists())
            self.assertTrue(staging_root.is_relative_to(Path(temp)))
            self.assertTrue(restored_root.is_relative_to(Path(temp)))

    async def test_missing_optional_roots_and_deterministic_manifest_member_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, staging_parent, archive_one, _restore_parent = self.make_source(temp, all_roots=False)
            staging = await archive.create_authoritative_markdown_staging(
                source, staging_parent, created_at="2026-08-01T00:00:00Z"
            )
            archive_two = Path(temp) / "snapshot-two.zip"
            first = archive.package_verified_authoritative_staging(staging["staging_root"], archive_one)
            second = archive.package_verified_authoritative_staging(staging["staging_root"], archive_two)
            self.assertEqual(first, second)
            self.assertEqual(first["file_count"], 1)
            with zipfile.ZipFile(archive_one) as one, zipfile.ZipFile(archive_two) as two:
                self.assertEqual(one.namelist(), two.namelist())
                self.assertEqual(one.namelist()[:-1], sorted(one.namelist()[:-1]))
                self.assertEqual(one.namelist()[-1], archive.MANIFEST_NAME)

    async def test_packaging_happens_after_exclusive_barrier_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, staging_parent, archive_path, restore_parent = self.make_source(temp)
            locks = Path(temp) / "locks"
            loop = asyncio.get_running_loop()
            writer_entered = asyncio.Event()
            original_builder = archive.build_isolated_archive

            async def writer() -> None:
                async with markdown_writer_turn(source, lock_root=locks):
                    writer_entered.set()

            def builder_with_writer(*args, **kwargs):
                future = asyncio.run_coroutine_threadsafe(writer(), loop)
                future.result(timeout=1)
                return original_builder(*args, **kwargs)

            with patch.object(archive, "build_isolated_archive", builder_with_writer):
                await archive.perform_isolated_markdown_round_trip(
                    source, staging_parent, archive_path, restore_parent,
                    created_at="2026-08-01T00:00:00Z", lock_root=locks,
                )
            self.assertTrue(writer_entered.is_set())

    async def test_incomplete_staging_and_nonempty_or_wrong_restore_target_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, staging_parent, archive_path, restore_parent = self.make_source(temp)
            staging = await archive.create_authoritative_markdown_staging(
                source, staging_parent, created_at="2026-08-01T00:00:00Z"
            )
            archive.package_verified_authoritative_staging(staging["staging_root"], archive_path)

            incomplete = Path(temp) / "incomplete-stage"
            incomplete.mkdir()
            archive._write_staging_marker(
                incomplete,
                archive._staging_payload(state="incomplete", manifest=None, coordination=None),
            )
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.package_verified_authoritative_staging(incomplete, Path(temp) / "forbidden.zip")

            (restore_parent / "not-empty.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(BackupArchiveError, "empty"):
                archive.restore_verified_isolated_archive(archive_path, restore_parent)
            self.assertEqual({p.name for p in restore_parent.iterdir()}, {"not-empty.txt"})

            not_directory = Path(temp) / "not-directory"
            not_directory.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(BackupArchiveError, "regular directory"):
                archive.restore_verified_isolated_archive(archive_path, not_directory)

            empty_real_parent = Path(temp) / "real-empty-parent"
            empty_real_parent.mkdir()
            linked_parent = Path(temp) / "linked-parent"
            try:
                linked_parent.symlink_to(empty_real_parent, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - depends on Windows symlink policy
                self.skipTest(f"Windows symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(BackupArchiveError, "regular directory"):
                archive.restore_verified_isolated_archive(archive_path, linked_parent)

    async def test_malicious_truncated_and_tampered_archives_refuse_before_restore_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, staging_parent, good, _restore_parent = self.make_source(temp)
            staging = await archive.create_authoritative_markdown_staging(
                source, staging_parent, created_at="2026-08-01T00:00:00Z"
            )
            archive.package_verified_authoritative_staging(staging["staging_root"], good)

            for name, build_bad, expected in (
                ("truncated.zip", lambda target: target.write_bytes(good.read_bytes()[:12]), "valid ZIP"),
                ("traversal.zip", self._write_traversal_zip, "unsafe path"),
                ("duplicate.zip", self._write_duplicate_zip, "duplicate member"),
                ("unexpected.zip", self._write_unexpected_zip, "non-bucket"),
            ):
                bad = Path(temp) / name
                build_bad(bad)
                parent = Path(temp) / f"{name}-restore"
                parent.mkdir()
                with self.assertRaisesRegex(BackupArchiveError, expected):
                    archive.restore_verified_isolated_archive(bad, parent)
                self.assertEqual(list(parent.iterdir()), [])

            tampered = Path(temp) / "tampered.zip"
            with zipfile.ZipFile(good) as source_zip, zipfile.ZipFile(tampered, "w") as output:
                for info in source_zip.infolist():
                    data = source_zip.read(info)
                    if info.filename.endswith("事件.md"):
                        data = data.replace("你好".encode("utf-8"), "再见".encode("utf-8"))
                    output.writestr(info.filename, data)
            parent = Path(temp) / "tampered-restore"
            parent.mkdir()
            with self.assertRaisesRegex(BackupArchiveError, "manifest hash mismatch"):
                archive.restore_verified_isolated_archive(tampered, parent)
            self.assertEqual(list(parent.iterdir()), [])

            missing = Path(temp) / "missing-member.zip"
            with zipfile.ZipFile(good) as source_zip, zipfile.ZipFile(missing, "w") as output:
                for info in source_zip.infolist():
                    if info.filename.endswith("旧.md"):
                        continue
                    output.writestr(info.filename, source_zip.read(info))
            parent = Path(temp) / "missing-restore"
            parent.mkdir()
            with self.assertRaisesRegex(BackupArchiveError, "member set"):
                archive.restore_verified_isolated_archive(missing, parent)
            self.assertEqual(list(parent.iterdir()), [])

            unsupported = Path(temp) / "unsupported.zip"
            with zipfile.ZipFile(unsupported, "w") as output:
                output.writestr("buckets/dynamic/a.md", b"---\nid: a\n---\na\n")
                output.writestr(
                    archive.MANIFEST_NAME,
                    json.dumps({"kind": archive.MANIFEST_KIND, "schema_version": 99, "created_at": "x", "files": []}),
                )
            parent = Path(temp) / "unsupported-restore"
            parent.mkdir()
            with self.assertRaisesRegex(BackupArchiveError, "unsupported batch-1 manifest"):
                archive.restore_verified_isolated_archive(unsupported, parent)
            self.assertEqual(list(parent.iterdir()), [])

    async def test_limits_schema_and_restore_failures_leave_only_incomplete_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source, staging_parent, archive_path, restore_parent = self.make_source(temp)
            staging = await archive.create_authoritative_markdown_staging(
                source, staging_parent, created_at="2026-08-01T00:00:00Z"
            )
            archive.package_verified_authoritative_staging(staging["staging_root"], archive_path)

            with patch.object(archive, "MAX_MEMBERS", 1):
                parent = Path(temp) / "limit-restore"
                parent.mkdir()
                with self.assertRaisesRegex(BackupArchiveError, "too many members"):
                    archive.restore_verified_isolated_archive(archive_path, parent)
                self.assertEqual(list(parent.iterdir()), [])

            with patch.object(archive, "MAX_COMPRESSION_RATIO", 0.5):
                parent = Path(temp) / "ratio-restore"
                parent.mkdir()
                with self.assertRaisesRegex(BackupArchiveError, "compression ratio"):
                    archive.restore_verified_isolated_archive(archive_path, parent)
                self.assertEqual(list(parent.iterdir()), [])

            def disk_full(phase: str) -> None:
                if phase == "restore.before_member":
                    raise OSError(errno.ENOSPC, "disk full")

            with self.assertRaises(OSError):
                archive.restore_verified_isolated_archive(archive_path, restore_parent, fault_injector=disk_full)
            quarantines = list(restore_parent.iterdir())
            self.assertEqual(len(quarantines), 1)
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.verify_isolated_roundtrip_restore(quarantines[0])

            second_parent = Path(temp) / "cancel-restore"
            second_parent.mkdir()

            def cancelled(phase: str) -> None:
                if phase == "restore.before_member":
                    raise asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                archive.restore_verified_isolated_archive(archive_path, second_parent, fault_injector=cancelled)
            self.assertEqual(len(list(second_parent.iterdir())), 1)
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.verify_isolated_roundtrip_restore(next(second_parent.iterdir()))

            handle_parent = Path(temp) / "handle-restore"
            handle_parent.mkdir()
            with patch.object(archive, "_write_restore_member", side_effect=OSError(32, "handle busy")):
                with self.assertRaises(OSError):
                    archive.restore_verified_isolated_archive(archive_path, handle_parent)
            self.assertEqual(len(list(handle_parent.iterdir())), 1)
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.verify_isolated_roundtrip_restore(next(handle_parent.iterdir()))

            marker_parent = Path(temp) / "marker-restore"
            marker_parent.mkdir()

            def marker_failure(phase: str) -> None:
                if phase == "restore.before_complete_marker":
                    raise OSError(errno.EIO, "fsync failed")

            with self.assertRaises(OSError):
                archive.restore_verified_isolated_archive(
                    archive_path, marker_parent, fault_injector=marker_failure
                )
            self.assertEqual(len(list(marker_parent.iterdir())), 1)
            with self.assertRaisesRegex(BackupArchiveError, "incomplete"):
                archive.verify_isolated_roundtrip_restore(next(marker_parent.iterdir()))

    @staticmethod
    def _write_traversal_zip(target: Path) -> None:
        with zipfile.ZipFile(target, "w") as output:
            output.writestr("../escape.md", b"bad")
            output.writestr(archive.MANIFEST_NAME, json.dumps({}))

    @staticmethod
    def _write_duplicate_zip(target: Path) -> None:
        with zipfile.ZipFile(target, "w") as output:
            output.writestr("buckets/dynamic/a.md", b"x")
            output.writestr("buckets/dynamic/A.md", b"x")
            output.writestr(archive.MANIFEST_NAME, json.dumps({}))

    @staticmethod
    def _write_unexpected_zip(target: Path) -> None:
        with zipfile.ZipFile(target, "w") as output:
            output.writestr("buckets/dynamic/a.md", b"x")
            output.writestr("buckets/dynamic/extra.txt", b"x")
            output.writestr(archive.MANIFEST_NAME, json.dumps({}))


if __name__ == "__main__":
    unittest.main()
