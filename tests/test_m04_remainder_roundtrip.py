"""R-02 专项: M-04 manifest schema v2 + remainder enumerator + round-trip."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import asyncio
from unittest import mock

import backup_archive as archive_mod
from backup_archive import (  # noqa: E402
    BackupArchiveError,
    build_authoritative_root_manifest,
    build_isolated_archive,
    create_authoritative_markdown_staging,
    restore_verified_isolated_archive,
    verify_authoritative_markdown_staging,
    verify_isolated_archive,
    verify_isolated_roundtrip_restore,
)

try:
    from restore_derived_adapter import collect_embedding_inputs  # noqa: E402
except ImportError:
    from src.restore_derived_adapter import collect_embedding_inputs


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_bucket(root: Path, directory: str, bucket_id: str, body: str, **metadata: object) -> Path:
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


def _write_remainder(root: Path, name: str, content: bytes) -> Path:
    path = root / ".remainders" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_remainder_archive(root: Path, name: str, content: bytes) -> Path:
    path = root / ".remainders" / "archive" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_remainder_quarantine(root: Path, name: str, content: bytes) -> Path:
    path = root / ".remainders" / "quarantine" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_lock(root: Path, name: str) -> Path:
    path = root / ".remainders" / ".locks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0")
    return path


def _build_v1_archive(temp: str, root: Path, created_at: str = "2026-08-01T00:00:00Z") -> tuple[Path, dict]:
    archive_path = Path(temp) / "v1.zip"
    manifest = build_isolated_archive(root, archive_path, created_at=created_at)
    v1_manifest = dict(manifest)
    v1_manifest["schema_version"] = 1
    v1_manifest["files"] = [e for e in manifest["files"] if e["type"] == "bucket_markdown"]
    v1_manifest["file_count"] = len(v1_manifest["files"])
    v1_manifest["total_bytes"] = sum(e["size"] for e in v1_manifest["files"])
    manifest_bytes = json.dumps(v1_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    v1_path = Path(temp) / "v1_final.zip"
    with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(v1_path, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename.startswith(".remainders"):
                continue
            if info.filename == "backup_manifest.json":
                dst.writestr(info, manifest_bytes)
            else:
                dst.writestr(info, src.read(info))
    return v1_path, v1_manifest


def _populate_vault(root: Path) -> None:
    _write_bucket(root, "dynamic", "event-1", "event body")
    _write_bucket(root, "permanent", "anchor-1", "anchor body")


def _populate_vault_with_remainder(root: Path) -> None:
    _populate_vault(root)
    sc = json.dumps({"version": 1, "entries": [{"state": "COMMITTED"}]}).encode("utf-8")
    _write_remainder(root, "event-1.json", sc)
    arc = json.dumps({"version": 1, "entries": [{"state": "ABORTED"}]}).encode("utf-8")
    _write_remainder_archive(root, "event-1_20260801T000000Z_abc123.json", arc)
    qua = b'{"corrupt":true'
    _write_remainder_quarantine(root, "event-1_20260801T000000Z_def456.json.corrupt", qua)


# ──────────────────────────────────────────────────────────────────────
# Cat 1: active/archive/quarantine non-ASCII + schema-v2 byte-exact round-trip
# ──────────────────────────────────────────────────────────────────────

class TestSchemaV2ByteExactRoundTrip(unittest.TestCase):

    def test_v2_all_three_remainder_types_byte_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            sc_content = "余数日志：中文内容 🎵".encode("utf-8")
            arc_content = "归档数据：日本語テスト".encode("utf-8")
            qua_content = b'\xff\xfe{"broken"'
            _write_remainder(root, "event-1.json", sc_content)
            _write_remainder_archive(root, "event-1_20260801T000000Z_aaa.json", arc_content)
            _write_remainder_quarantine(root, "event-1_20260801T000000Z_bbb.json.corrupt", qua_content)

            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["schema_version"], 2)

            with zipfile.ZipFile(archive_path, "r") as zf:
                self.assertEqual(zf.read(".remainders/event-1.json"), sc_content)
                self.assertEqual(
                    zf.read(".remainders/archive/event-1_20260801T000000Z_aaa.json"), arc_content
                )
                self.assertEqual(
                    zf.read(".remainders/quarantine/event-1_20260801T000000Z_bbb.json.corrupt"),
                    qua_content,
                )

            restored_dir = Path(temp) / "restore_parent"
            restored_dir.mkdir()
            result = restore_verified_isolated_archive(archive_path, restored_dir)
            restored = Path(result["restored_root"])
            self.assertEqual((restored / ".remainders" / "event-1.json").read_bytes(), sc_content)
            self.assertEqual(
                (restored / ".remainders" / "archive" / "event-1_20260801T000000Z_aaa.json").read_bytes(),
                arc_content,
            )
            self.assertEqual(
                (restored / ".remainders" / "quarantine" / "event-1_20260801T000000Z_bbb.json.corrupt").read_bytes(),
                qua_content,
            )

    def test_v2_non_ascii_bucket_and_remainder_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _write_bucket(root, "dynamic", "日記-1", "日記の内容")
            content = json.dumps({"v": 1}).encode("utf-8")
            _write_remainder(root, "日記-1.json", content)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["schema_version"], 2)
            verified = verify_isolated_archive(archive_path)
            self.assertEqual(verified, manifest)


# ──────────────────────────────────────────────────────────────────────
# Cat 2: manifest type/path/order/count/bytes/hash; closure Markdown only
# ──────────────────────────────────────────────────────────────────────

class TestManifestV2Structure(unittest.TestCase):

    def test_manifest_entries_sorted_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            paths = [e["path"] for e in manifest["files"]]
            self.assertEqual(paths, sorted(paths))

    def test_manifest_types_correct(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            types_by_path = {e["path"]: e["type"] for e in manifest["files"]}
            for p, t in types_by_path.items():
                if p.startswith("buckets/"):
                    self.assertEqual(t, "bucket_markdown")
                elif p == ".remainders/event-1.json":
                    self.assertEqual(t, "remainder_sidecar")
                elif "archive/" in p:
                    self.assertEqual(t, "remainder_archive")
                elif "quarantine/" in p:
                    self.assertEqual(t, "remainder_quarantine")

    def test_manifest_count_bytes_hash_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            total = sum(e["size"] for e in manifest["files"])
            self.assertEqual(manifest["total_bytes"], total)
            self.assertEqual(manifest["file_count"], len(manifest["files"]))
            with zipfile.ZipFile(archive_path, "r") as zf:
                for entry in manifest["files"]:
                    data = zf.read(entry["path"])
                    self.assertEqual(len(data), entry["size"])
                    self.assertEqual(_sha256(data), entry["sha256"])

    def test_closure_only_from_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _write_bucket(root, "dynamic", "e1", "body", source_bucket="e1")
            sc = json.dumps({"source_bucket": "FAKE_SHOULD_NOT_APPEAR"}).encode("utf-8")
            _write_remainder(root, "e1.json", sc)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            closure = manifest["reference_closure"]
            self.assertNotIn("FAKE_SHOULD_NOT_APPEAR", json.dumps(closure))


# ──────────────────────────────────────────────────────────────────────
# Cat 3: .remainders absent, only active, all three types
# ──────────────────────────────────────────────────────────────────────

class TestRemainderPresenceVariants(unittest.TestCase):

    def test_no_remainders_dir_still_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["schema_version"], 2)
            types = {e["type"] for e in manifest["files"]}
            self.assertEqual(types, {"bucket_markdown"})

    def test_only_active_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            _write_remainder(root, "event-1.json", b'{"v":1}')
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            types = {e["type"] for e in manifest["files"]}
            self.assertEqual(types, {"bucket_markdown", "remainder_sidecar"})

    def test_all_three_remainder_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            types = {e["type"] for e in manifest["files"]}
            self.assertEqual(types, {"bucket_markdown", "remainder_sidecar", "remainder_archive", "remainder_quarantine"})


# ──────────────────────────────────────────────────────────────────────
# Cat 4: v1 fixture verify + empty-vault restore; pseudo-v1 remainder reject
# ──────────────────────────────────────────────────────────────────────

class TestV1Compatibility(unittest.TestCase):

    def test_v1_archive_verify_and_restore_no_remainders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_bucket(root, "dynamic", "event-1", "event body")
            archive_path, manifest = _build_v1_archive(temp, root)
            verified = verify_isolated_archive(archive_path)
            self.assertEqual(verified["schema_version"], 1)
            self.assertEqual(verified, manifest)

            restore_parent = Path(temp) / "restore"
            restore_parent.mkdir()
            result = restore_verified_isolated_archive(archive_path, restore_parent)
            restored = Path(result["restored_root"])
            self.assertFalse((restored / ".remainders").exists())
            self.assertTrue((restored / "buckets" / "dynamic" / "event-1.md").exists())

    def test_v1_with_remainder_entries_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_bucket(root, "dynamic", "event-1", "event body")
            md_bytes = (root / "dynamic" / "event-1.md").read_bytes()
            sc_bytes = b'{"v":1}'
            manifest = {
                "kind": "ombre-brain-m04-isolated-bucket-backup",
                "schema_version": 1,
                "created_at": "2026-08-01T00:00:00Z",
                "file_count": 2,
                "total_bytes": len(md_bytes) + len(sc_bytes),
                "files": [
                    {"path": ".remainders/x.json", "type": "remainder_sidecar", "size": len(sc_bytes), "sha256": _sha256(sc_bytes)},
                    {"path": "buckets/dynamic/event-1.md", "type": "bucket_markdown", "size": len(md_bytes), "sha256": _sha256(md_bytes)},
                ],
                "reference_closure": {"internal_source_buckets": [], "external_evidence_refs": [], "raw_evidence_included": False},
                "policy": {"contains_markdown_authority": True, "contains_embeddings_db": False, "contains_runtime_journals": False, "contains_secrets": False, "contains_raw_evidence": False},
            }
            manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            archive_path = Path(temp) / "fake_v1.zip"
            with zipfile.ZipFile(archive_path, "w") as zf:
                zf.writestr("buckets/dynamic/event-1.md", md_bytes)
                zf.writestr(".remainders/x.json", sc_bytes)
                zf.writestr("backup_manifest.json", manifest_bytes)
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(archive_path)


# ──────────────────────────────────────────────────────────────────────
# Cat 5: tamper/missing/extra/wrong type rejection
# ──────────────────────────────────────────────────────────────────────

class TestRemainderTamperDetection(unittest.TestCase):

    def _build_good_archive(self, temp: str) -> tuple[Path, dict]:
        root = Path(temp) / "vault"
        _populate_vault_with_remainder(root)
        archive_path = Path(temp) / "backup.zip"
        manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
        return archive_path, manifest

    def test_remainder_content_tampered_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path, manifest = self._build_good_archive(temp)
            tampered = Path(temp) / "tampered.zip"
            with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(tampered, "w") as dst:
                for info in src.infolist():
                    data = src.read(info)
                    if info.filename == ".remainders/event-1.json":
                        data = b'{"tampered":true}'
                    dst.writestr(info, data)
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(tampered)

    def test_remainder_missing_from_archive_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path, manifest = self._build_good_archive(temp)
            stripped = Path(temp) / "stripped.zip"
            with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(stripped, "w") as dst:
                for info in src.infolist():
                    if info.filename == ".remainders/event-1.json":
                        continue
                    dst.writestr(info, src.read(info))
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(stripped)

    def test_extra_remainder_in_archive_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            archive_path, manifest = self._build_good_archive(temp)
            extra = Path(temp) / "extra.zip"
            with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(extra, "w") as dst:
                for info in src.infolist():
                    dst.writestr(info, src.read(info))
                dst.writestr(".remainders/sneaky.json", b'{"extra":true}')
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(extra)


# ──────────────────────────────────────────────────────────────────────
# Cat 6: symlink/reparse/nonregular/enumeration fail-closed
# ──────────────────────────────────────────────────────────────────────

class TestRemainderPathSafety(unittest.TestCase):

    def test_remainder_root_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            real_rem = Path(temp) / "real_rem"
            real_rem.mkdir()
            (real_rem / "event-1.json").write_bytes(b'{"v":1}')
            link = root / ".remainders"
            try:
                link.symlink_to(real_rem, target_is_directory=True)
            except OSError:
                self.skipTest("cannot create directory symlink on this platform")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_remainder_member_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            rem_dir = root / ".remainders"
            rem_dir.mkdir(parents=True, exist_ok=True)
            real_file = Path(temp) / "real.json"
            real_file.write_bytes(b'{"v":1}')
            link = rem_dir / "event-1.json"
            try:
                link.symlink_to(real_file)
            except OSError:
                self.skipTest("cannot create file symlink on this platform")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_remainder_root_is_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            rem = root / ".remainders"
            rem.write_bytes(b"not a directory")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_unexpected_subdir_in_remainder_root_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            bad = root / ".remainders" / "unknown_dir"
            bad.mkdir(parents=True)
            (bad / "file.json").write_bytes(b"{}")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_archive_subdir_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            rem_dir = root / ".remainders"
            rem_dir.mkdir(parents=True, exist_ok=True)
            real_archive = Path(temp) / "real_archive"
            real_archive.mkdir()
            (real_archive / "a.json").write_bytes(b'{}')
            link = rem_dir / "archive"
            try:
                link.symlink_to(real_archive, target_is_directory=True)
            except OSError:
                self.skipTest("cannot create directory symlink on this platform")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")


# ──────────────────────────────────────────────────────────────────────
# Cat 7: .locks exclusion + abnormal locks rejection
# ──────────────────────────────────────────────────────────────────────

class TestLocksHandling(unittest.TestCase):

    def test_locks_excluded_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            _write_remainder(root, "event-1.json", b'{"v":1}')
            _write_lock(root, "abc123.lock")
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            paths = [e["path"] for e in manifest["files"]]
            self.assertTrue(all(".locks" not in p for p in paths))

    def test_locks_bad_suffix_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            locks_dir = root / ".remainders" / ".locks"
            locks_dir.mkdir(parents=True, exist_ok=True)
            (locks_dir / "bad.txt").write_bytes(b"\0")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_locks_wrong_size_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            locks_dir = root / ".remainders" / ".locks"
            locks_dir.mkdir(parents=True, exist_ok=True)
            (locks_dir / "abc.lock").write_bytes(b"\0\0")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_locks_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            locks_dir = root / ".remainders" / ".locks"
            locks_dir.mkdir(parents=True, exist_ok=True)
            real = Path(temp) / "real.lock"
            real.write_bytes(b"\0")
            link = locks_dir / "abc.lock"
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("cannot create file symlink on this platform")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")

    def test_locks_dir_is_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            rem_dir = root / ".remainders"
            rem_dir.mkdir(parents=True, exist_ok=True)
            (rem_dir / ".locks").write_bytes(b"not a dir")
            with self.assertRaises(BackupArchiveError):
                build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")


# ──────────────────────────────────────────────────────────────────────
# Cat 8: staging freeze + source mutation detection
# ──────────────────────────────────────────────────────────────────────

class TestStagingFreezeIntegrity(unittest.TestCase):

    def test_staging_with_remainder_verifies_correctly(self) -> None:
        """Full staging → archive → restore → round-trip with remainder."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            _write_lock(root, "abc.lock")

            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["schema_version"], 2)
            verified = verify_isolated_archive(archive_path)
            self.assertEqual(verified, manifest)

            restore_parent = Path(temp) / "restore"
            restore_parent.mkdir()
            result = restore_verified_isolated_archive(archive_path, restore_parent)
            restored = Path(result["restored_root"])
            self.assertTrue((restored / ".remainders" / "event-1.json").exists())
            self.assertFalse((restored / ".remainders" / ".locks").exists())


# ──────────────────────────────────────────────────────────────────────
# Cat 9: archive/restore failure semantics
# ──────────────────────────────────────────────────────────────────────

class TestArchiveRestoreFailureSemantics(unittest.TestCase):

    def test_restore_fault_leaves_incomplete_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            archive_path = Path(temp) / "backup.zip"
            build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")

            restore_parent = Path(temp) / "restore"
            restore_parent.mkdir()
            call_count = [0]

            def fault(phase: str) -> None:
                if phase == "restore.before_member":
                    call_count[0] += 1
                    if call_count[0] == 2:
                        raise BackupArchiveError("injected fault")

            with self.assertRaises(BackupArchiveError):
                restore_verified_isolated_archive(archive_path, restore_parent, fault_injector=fault)
            children = list(restore_parent.iterdir())
            self.assertEqual(len(children), 1)
            marker = children[0] / "m04_restored_manifest.json"
            self.assertTrue(marker.exists())
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["state"], "incomplete")


# ──────────────────────────────────────────────────────────────────────
# Cat 10: derived adapter v2 only produces Markdown embedding inputs
# ──────────────────────────────────────────────────────────────────────

class TestDerivedAdapterV2(unittest.TestCase):

    def test_collect_embedding_inputs_skips_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _write_bucket(root, "dynamic", "event-1", "event body")
            _write_bucket(root, "permanent", "anchor-1", "anchor body")
            _write_remainder(root, "event-1.json", b'{"v":1}')

            manifest = build_authoritative_root_manifest(root, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["schema_version"], 2)
            remainder_entries = [e for e in manifest["files"] if e["type"] != "bucket_markdown"]
            self.assertTrue(len(remainder_entries) > 0)

            inputs = collect_embedding_inputs(root, manifest)
            bucket_ids = [bid for bid, _ in inputs]
            self.assertIn("event-1", bucket_ids)
            self.assertIn("anchor-1", bucket_ids)
            self.assertEqual(len(inputs), 2)


# ──────────────────────────────────────────────────────────────────────
# Full round-trip: staging → archive → restore → verify
# ──────────────────────────────────────────────────────────────────────

class TestFullRoundTrip(unittest.TestCase):

    def test_v2_full_round_trip_with_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            _write_lock(root, "abc.lock")

            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            self.assertEqual(manifest["schema_version"], 2)

            verified = verify_isolated_archive(archive_path)
            self.assertEqual(verified, manifest)

            restore_parent = Path(temp) / "restore"
            restore_parent.mkdir()
            result = restore_verified_isolated_archive(archive_path, restore_parent)
            self.assertEqual(result["backup_manifest"], manifest)

            restored_root = Path(result["restored_root"])
            self.assertTrue((restored_root / ".remainders" / "event-1.json").exists())
            self.assertTrue((restored_root / "buckets" / "dynamic" / "event-1.md").exists())
            self.assertFalse((restored_root / ".remainders" / ".locks").exists())

    def test_v1_full_round_trip_no_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_bucket(root, "dynamic", "event-1", "event body")
            archive_path, manifest = _build_v1_archive(temp, root)

            restore_parent = Path(temp) / "restore"
            restore_parent.mkdir()
            result = restore_verified_isolated_archive(archive_path, restore_parent)
            restored = Path(result["restored_root"])
            self.assertFalse((restored / ".remainders").exists())
            self.assertEqual(result["backup_manifest"]["schema_version"], 1)


# ──────────────────────────────────────────────────────────────────────
# Blocker 1: path/type binding + sort order validation
# ──────────────────────────────────────────────────────────────────────

class TestPathTypeBinding(unittest.TestCase):

    def test_markdown_mislabeled_as_remainder_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            _write_remainder(root, "event-1.json", b'{"v":1}')
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            bad_manifest = json.loads(json.dumps(manifest))
            for entry in bad_manifest["files"]:
                if entry["type"] == "bucket_markdown":
                    entry["type"] = "remainder_sidecar"
                    break
            bad_manifest["reference_closure"] = {"internal_source_buckets": [], "external_evidence_refs": [], "raw_evidence_included": False}
            tampered = Path(temp) / "tampered.zip"
            manifest_bytes = json.dumps(bad_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(tampered, "w") as dst:
                for info in src.infolist():
                    if info.filename == "backup_manifest.json":
                        dst.writestr(info, manifest_bytes)
                    else:
                        dst.writestr(info, src.read(info))
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(tampered)

    def test_remainder_mislabeled_as_markdown_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            _write_remainder(root, "event-1.json", b'{"v":1}')
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            bad_manifest = json.loads(json.dumps(manifest))
            for entry in bad_manifest["files"]:
                if entry["type"] == "remainder_sidecar":
                    entry["type"] = "bucket_markdown"
                    break
            tampered = Path(temp) / "tampered.zip"
            manifest_bytes = json.dumps(bad_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(tampered, "w") as dst:
                for info in src.infolist():
                    if info.filename == "backup_manifest.json":
                        dst.writestr(info, manifest_bytes)
                    else:
                        dst.writestr(info, src.read(info))
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(tampered)

    def test_unsorted_manifest_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            _write_remainder(root, "event-1.json", b'{"v":1}')
            archive_path = Path(temp) / "backup.zip"
            manifest = build_isolated_archive(root, archive_path, created_at="2026-08-01T00:00:00Z")
            bad_manifest = json.loads(json.dumps(manifest))
            bad_manifest["files"] = list(reversed(bad_manifest["files"]))
            manifest_bytes = json.dumps(bad_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            tampered = Path(temp) / "unsorted.zip"
            with zipfile.ZipFile(archive_path, "r") as src, zipfile.ZipFile(tampered, "w") as dst:
                for info in src.infolist():
                    if info.filename == "backup_manifest.json":
                        dst.writestr(info, manifest_bytes)
                    else:
                        dst.writestr(info, src.read(info))
            with self.assertRaises(BackupArchiveError):
                verify_isolated_archive(tampered)


# ──────────────────────────────────────────────────────────────────────
# Blocker 2: non-directory .remainders must be rejected
# ──────────────────────────────────────────────────────────────────────

class TestRemainderRootValidation(unittest.TestCase):

    def test_remainder_root_is_regular_file_rejected_by_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            (root / ".remainders").write_bytes(b"not a directory")
            with self.assertRaises(BackupArchiveError):
                build_isolated_archive(root, Path(temp) / "out.zip", created_at="2026-08-01T00:00:00Z")


# ──────────────────────────────────────────────────────────────────────
# Blocker 4: combined member/total limits
# ──────────────────────────────────────────────────────────────────────

class TestCombinedLimits(unittest.TestCase):

    def test_combined_bytes_exceed_limit_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _write_bucket(root, "dynamic", "event-1", "x" * 200)
            _write_remainder(root, "event-1.json", b'x' * 200)
            with mock.patch.object(archive_mod, "MAX_TOTAL_UNCOMPRESSED_BYTES", 350):
                with self.assertRaises(BackupArchiveError):
                    build_isolated_archive(root, Path(temp) / "out.zip", created_at="2026-08-01T00:00:00Z")

    def test_combined_count_exceed_limit_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _write_bucket(root, "dynamic", "event-1", "body")
            _write_remainder(root, "event-1.json", b'{"v":1}')
            with mock.patch.object(archive_mod, "MAX_MEMBERS", 1):
                with self.assertRaises(BackupArchiveError):
                    build_isolated_archive(root, Path(temp) / "out.zip", created_at="2026-08-01T00:00:00Z")


# ──────────────────────────────────────────────────────────────────────
# Blocker 5: v1 restore rejects .remainders residue
# ──────────────────────────────────────────────────────────────────────

class TestV1RestoreNoRemainders(unittest.TestCase):

    def test_v1_restore_with_remainder_residue_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            _write_bucket(root, "dynamic", "event-1", "event body")
            archive_path, manifest = _build_v1_archive(temp, root)

            restore_parent = Path(temp) / "restore"
            restore_parent.mkdir()
            result = restore_verified_isolated_archive(archive_path, restore_parent)
            restored = Path(result["restored_root"])
            ghost_dir = restored / ".remainders"
            ghost_dir.mkdir()
            (ghost_dir / "ghost.json").write_bytes(b'{"ghost":true}')
            with self.assertRaises(BackupArchiveError):
                verify_isolated_roundtrip_restore(restored)


# ──────────────────────────────────────────────────────────────────────
# Blocker 6: real async staging with remainder + barrier
# ──────────────────────────────────────────────────────────────────────

class TestAsyncStagingWithRemainder(unittest.TestCase):

    def test_staging_copies_remainder_under_barrier_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            _write_lock(root, "abc.lock")
            staging_parent = Path(temp) / "staging"
            staging_parent.mkdir()

            result = asyncio.run(create_authoritative_markdown_staging(
                root, staging_parent, created_at="2026-08-01T00:00:00Z",
            ))
            staging_root = Path(result["staging_root"])
            self.assertTrue((staging_root / ".remainders" / "event-1.json").exists())
            self.assertFalse((staging_root / ".remainders" / ".locks").exists())
            verified = verify_authoritative_markdown_staging(staging_root)
            self.assertEqual(verified["backup_manifest"]["schema_version"], 2)

    def test_staging_complete_marker_binds_v2_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            staging_parent = Path(temp) / "staging"
            staging_parent.mkdir()

            result = asyncio.run(create_authoritative_markdown_staging(
                root, staging_parent, created_at="2026-08-01T00:00:00Z",
            ))
            staging_root = Path(result["staging_root"])
            marker_path = staging_root / "m04_staging_manifest.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            manifest = marker["backup_manifest"]
            self.assertEqual(manifest["schema_version"], 2)
            rem_types = {e["type"] for e in manifest["files"] if e["type"] != "bucket_markdown"}
            self.assertTrue(len(rem_types) > 0)

    def test_source_mutation_after_barrier_release_does_not_affect_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            staging_parent = Path(temp) / "staging"
            staging_parent.mkdir()

            result = asyncio.run(create_authoritative_markdown_staging(
                root, staging_parent, created_at="2026-08-01T00:00:00Z",
            ))
            staging_root = Path(result["staging_root"])
            original_sc = (staging_root / ".remainders" / "event-1.json").read_bytes()
            (root / ".remainders" / "event-1.json").write_bytes(b'{"mutated":true}')
            self.assertEqual(
                (staging_root / ".remainders" / "event-1.json").read_bytes(),
                original_sc,
            )

    def test_staging_identity_swap_during_copy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            staging_parent = Path(temp) / "staging"
            staging_parent.mkdir()

            swap_done = [False]
            original_copy = archive_mod._copy_source_file

            def swapping_copy(source, target, expected):
                if not swap_done[0] and ".remainders" in str(source):
                    swap_done[0] = True
                    source.write_bytes(b'{"swapped":true}')
                return original_copy(source, target, expected)

            with mock.patch.object(archive_mod, "_copy_source_file", swapping_copy):
                with self.assertRaises(BackupArchiveError):
                    asyncio.run(create_authoritative_markdown_staging(
                        root, staging_parent, created_at="2026-08-01T00:00:00Z",
                    ))


# ──────────────────────────────────────────────────────────────────────
# Blocker 7 (2nd review): ABA identity swap on remainder read
# ──────────────────────────────────────────────────────────────────────

class TestRemainderABASwap(unittest.TestCase):

    def test_aba_swap_during_remainder_read_rejected(self) -> None:
        """ABA swap: replace→read external→restore original; inode unchanged."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            original_content = b'{"original":true}'
            _write_remainder(root, "event-1.json", original_content)

            victim = root / ".remainders" / "event-1.json"
            parked = Path(temp) / "parked.json"
            external = Path(temp) / "external.json"
            external.write_bytes(b"SECRET-OUTSIDE")

            original_os_open = os.open
            swapped = [False]

            def hijacked_open(path, flags, *args, **kwargs):
                path_str = str(path)
                if not swapped[0] and "event-1.json" in path_str and ".remainders" in path_str:
                    swapped[0] = True
                    os.replace(victim, parked)
                    try:
                        victim.symlink_to(external)
                    except OSError:
                        os.replace(parked, victim)
                        raise
                    fd = original_os_open(path, flags, *args, **kwargs)
                    try:
                        victim.unlink()
                    except OSError:
                        pass
                    os.replace(parked, victim)
                    return fd
                return original_os_open(path, flags, *args, **kwargs)

            with mock.patch("os.open", side_effect=hijacked_open):
                with self.assertRaises(BackupArchiveError):
                    build_isolated_archive(root, Path(temp) / "out.zip", created_at="2026-08-01T00:00:00Z")

    def test_simple_swap_before_read_rejected(self) -> None:
        """Simple swap: replace file content between enumerate and read."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            _write_remainder(root, "event-1.json", b'{"original":true}')

            original_read_source = archive_mod._read_source_bytes
            swapped = [False]

            def swapping_read(source, expected):
                if not swapped[0] and "event-1.json" in str(source):
                    swapped[0] = True
                    source.write_bytes(b'{"swapped":true}')
                return original_read_source(source, expected)

            with mock.patch.object(archive_mod, "_read_source_bytes", swapping_read):
                with self.assertRaises(BackupArchiveError):
                    build_isolated_archive(root, Path(temp) / "out.zip", created_at="2026-08-01T00:00:00Z")


# ──────────────────────────────────────────────────────────────────────
# Blocker 8 (2nd review): v1 staging verifier rejects remainder residue
# ──────────────────────────────────────────────────────────────────────

class TestV1StagingNoRemainders(unittest.TestCase):

    def test_v1_staging_with_remainder_residue_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault_with_remainder(root)
            staging_parent = Path(temp) / "staging"
            staging_parent.mkdir()

            result = asyncio.run(create_authoritative_markdown_staging(
                root, staging_parent, created_at="2026-08-01T00:00:00Z",
            ))
            staging_root = Path(result["staging_root"])
            marker_path = staging_root / "m04_staging_manifest.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            manifest = marker["backup_manifest"]
            v1_manifest = dict(manifest)
            v1_manifest["schema_version"] = 1
            v1_manifest["files"] = [e for e in manifest["files"] if e["type"] == "bucket_markdown"]
            v1_manifest["file_count"] = len(v1_manifest["files"])
            v1_manifest["total_bytes"] = sum(e["size"] for e in v1_manifest["files"])
            marker["backup_manifest"] = v1_manifest
            marker_path.write_text(
                json.dumps(marker, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            with self.assertRaises(BackupArchiveError) as ctx:
                verify_authoritative_markdown_staging(staging_root)
            self.assertIn("unexpected top-level member", str(ctx.exception))

    def test_v2_staging_without_remainder_still_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            _populate_vault(root)
            staging_parent = Path(temp) / "staging"
            staging_parent.mkdir()

            result = asyncio.run(create_authoritative_markdown_staging(
                root, staging_parent, created_at="2026-08-01T00:00:00Z",
            ))
            staging_root = Path(result["staging_root"])
            verified = verify_authoritative_markdown_staging(staging_root)
            self.assertEqual(verified["backup_manifest"]["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
