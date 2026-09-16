from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backup_archive import (  # noqa: E402
    BackupArchiveError,
    build_isolated_archive,
    extract_verified_isolated_archive,
    verify_isolated_archive,
)


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
    path = root / directory / f"{bucket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class M04BatchOneArchiveTests(unittest.TestCase):
    def test_round_trip_carries_all_bucket_categories_and_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"
            write_bucket(root, "dynamic", "event-1", "event")
            write_bucket(root, "permanent", "anchor-1", "anchor")
            write_bucket(
                root,
                "feel",
                "profile-1",
                "profile",
                source_bucket="event-1",
                evidence_id="codex:session:L8",
                source_refs=["salon:message:9", "bucket:anchor-1"],
            )
            write_bucket(root, "archive", "old-1", "old")
            write_bucket(root, "plans", "plan-1", "plan")
            write_bucket(root, "letters", "letter-1", "letter")
            archive = Path(temp) / "snapshot.zip"

            manifest = build_isolated_archive(root, archive, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["file_count"], 6)
            self.assertFalse(manifest["policy"]["contains_secrets"])
            self.assertFalse(manifest["reference_closure"]["raw_evidence_included"])
            self.assertEqual(
                manifest["reference_closure"]["external_evidence_refs"],
                ["codex:session:L8", "salon:message:9"],
            )
            self.assertEqual(verify_isolated_archive(archive), manifest)

            restored = Path(temp) / "restored"
            self.assertEqual(extract_verified_isolated_archive(archive, restored), manifest)
            original = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*.md")}
            copied = {
                p.relative_to(restored / "buckets").as_posix(): p.read_bytes()
                for p in (restored / "buckets").rglob("*.md")
            }
            self.assertEqual(copied, original)

    def test_rejects_dangling_internal_source_bucket_before_writing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"
            write_bucket(root, "dynamic", "profile-1", "profile", source_bucket="missing")
            archive = Path(temp) / "snapshot.zip"
            with self.assertRaisesRegex(BackupArchiveError, "dangling internal"):
                build_isolated_archive(root, archive)
            self.assertFalse(archive.exists())

    def test_tampered_member_fails_manifest_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"
            write_bucket(root, "dynamic", "event-1", "original")
            archive = Path(temp) / "good.zip"
            build_isolated_archive(root, archive)
            tampered = Path(temp) / "tampered.zip"
            with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(tampered, "w") as target:
                for info in source.infolist():
                    data = source.read(info)
                    if info.filename.endswith("event-1.md"):
                        data = data.replace(b"original", b"tampered")
                    target.writestr(info.filename, data)
            with self.assertRaisesRegex(BackupArchiveError, "manifest hash mismatch"):
                verify_isolated_archive(tampered)

    def test_rejects_unsafe_member_before_manifest_is_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as target:
                target.writestr("buckets/dynamic/a.md", b"---\nid: a\n---\na")
                target.writestr("../escape.md", b"no")
                target.writestr("backup_manifest.json", json.dumps({}))
            with self.assertRaisesRegex(BackupArchiveError, "unsafe path"):
                verify_isolated_archive(archive)

    def test_rejects_raw_source_refs_shape_without_importing_raw_evidence_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "snapshot"
            write_bucket(root, "dynamic", "event-1", "event", source_refs=[{"ref": "src_x"}])
            with self.assertRaisesRegex(BackupArchiveError, "list\\[str\\]"):
                build_isolated_archive(root, Path(temp) / "snapshot.zip")


if __name__ == "__main__":
    unittest.main()
