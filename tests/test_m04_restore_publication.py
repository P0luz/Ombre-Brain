from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import frontmatter

from backup_archive import RESTORE_MARKER_NAME, _restore_payload, _write_marker, build_authoritative_root_manifest
from restore_publication import (
    JOURNAL_NAME,
    RestorePublicationError,
    STATE_COMMITTED,
    STATE_MARKDOWN_PUBLISHED,
    STATE_OLD_STAGED,
    STATE_PREPARED,
    STATE_ROLLED_BACK,
    TRANSACTION_ROOT_NAME,
    publish_verified_restore,
    recover_restore_publication,
    recover_restore_publications_before_startup,
)


def write_bucket(root: Path, bucket_id: str, body: str) -> bytes:
    raw = frontmatter.dumps(frontmatter.Post(body, id=bucket_id, tags=["owner:cheng"])).encode("utf-8")
    path = root / "dynamic" / f"{bucket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def make_restore(parent: Path, bucket_id: str, body: str, name: str = "restored") -> tuple[Path, bytes]:
    restored = parent / name
    buckets = restored / "buckets"
    raw = write_bucket(buckets, bucket_id, body)
    manifest = build_authoritative_root_manifest(buckets, created_at="2026-08-02T00:00:00+00:00")
    _write_marker(restored, RESTORE_MARKER_NAME, _restore_payload(state="complete", manifest=manifest))
    return restored, raw


def rebuild_receipt(_live: Path, manifest: dict) -> dict:
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "status": "verified",
        "sqlite_policy": "rebuilt-from-authoritative-markdown",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


class Quiescence:
    def __init__(self) -> None:
        self.active = False
        self.entries = 0

    @contextmanager
    def hold(self):
        self.entries += 1
        self.active = True
        try:
            yield
        finally:
            self.active = False


class M04RestorePublicationTests(unittest.TestCase):
    def fixture(self, temp: str):
        base = Path(temp)
        live = base / "live"
        old = write_bucket(live, "old", "old body")
        restored, new = make_restore(base, "new", "new body")
        transactions = base / TRANSACTION_ROOT_NAME
        transactions.mkdir()
        return live, restored, transactions, old, new

    def test_publish_commits_new_and_retains_old_without_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, new = self.fixture(temp)
            quiet = Quiescence()
            result = publish_verified_restore(restored, live, transactions, quiescence=quiet.hold, rebuild_derived=rebuild_receipt, txid="a" * 32)
            self.assertEqual(result["state"], STATE_COMMITTED)
            self.assertEqual((live / "dynamic" / "new.md").read_bytes(), new)
            self.assertEqual((Path(result["rollback_vault"]) / "dynamic" / "old.md").read_bytes(), old)
            self.assertFalse((restored / "buckets").exists())
            self.assertEqual(quiet.entries, 1)
            journal = json.loads(Path(result["journal"]).read_text(encoding="utf-8"))
            self.assertEqual(journal["cleanup_policy"], "manual:no-automatic-delete")
            self.assertEqual(journal["derived_rebuild"]["status"], "verified")

    def test_derived_rebuild_failure_stays_active_and_recovery_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, new = self.fixture(temp)
            quiet = Quiescence()

            def fail_rebuild(_live: Path, _manifest: dict) -> dict:
                self.assertTrue(quiet.active)
                raise RuntimeError("rebuild failed")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(
                    restored,
                    live,
                    transactions,
                    quiescence=quiet.hold,
                    rebuild_derived=fail_rebuild,
                    txid="5" * 32,
                )
            txdir = transactions / ("5" * 32)
            self.assertEqual(
                json.loads((txdir / JOURNAL_NAME).read_text())["state"],
                STATE_MARKDOWN_PUBLISHED,
            )
            self.assertEqual((live / "dynamic" / "new.md").read_bytes(), new)
            recovered = recover_restore_publication(txdir, quiescence=quiet.hold)
            self.assertEqual(recovered["state"], STATE_ROLLED_BACK)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)

    def test_prepared_failure_recovers_without_moving_either_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, new = self.fixture(temp)
            quiet = Quiescence()

            def fail(point: str) -> None:
                if point == "publication.prepared":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=quiet.hold, rebuild_derived=rebuild_receipt, txid="b" * 32, fault_injector=fail)
            txdir = transactions / ("b" * 32)
            self.assertEqual(json.loads((txdir / JOURNAL_NAME).read_text())["state"], STATE_PREPARED)
            recovered = recover_restore_publication(txdir, quiescence=quiet.hold)
            self.assertEqual(recovered["state"], STATE_ROLLED_BACK)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)
            self.assertEqual((restored / "buckets" / "dynamic" / "new.md").read_bytes(), new)

    def test_old_staged_failure_restores_old_live(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, _new = self.fixture(temp)
            quiet = Quiescence()

            def fail(point: str) -> None:
                if point == "publication.old_staged":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=quiet.hold, rebuild_derived=rebuild_receipt, txid="c" * 32, fault_injector=fail)
            txdir = transactions / ("c" * 32)
            self.assertFalse(live.exists())
            self.assertEqual(json.loads((txdir / JOURNAL_NAME).read_text())["state"], STATE_OLD_STAGED)
            recovered = recover_restore_publication(txdir, quiescence=quiet.hold)
            self.assertEqual(recovered["state"], STATE_ROLLED_BACK)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)

    def test_crash_after_old_rename_before_journal_update_restores_old(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, _new = self.fixture(temp)

            def fail(point: str) -> None:
                if point == "publication.old_renamed":
                    raise RuntimeError("crash")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="1" * 32, fault_injector=fail)
            txdir = transactions / ("1" * 32)
            self.assertEqual(json.loads((txdir / JOURNAL_NAME).read_text())["state"], STATE_PREPARED)
            recovered = recover_restore_publication(txdir, quiescence=Quiescence().hold)
            self.assertEqual(recovered["state"], STATE_ROLLED_BACK)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)

    def test_new_published_failure_quarantines_new_and_restores_old(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, new = self.fixture(temp)
            quiet = Quiescence()

            def fail(point: str) -> None:
                if point == "publication.new_published":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=quiet.hold, rebuild_derived=rebuild_receipt, txid="d" * 32, fault_injector=fail)
            txdir = transactions / ("d" * 32)
            self.assertEqual((live / "dynamic" / "new.md").read_bytes(), new)
            recovered = recover_restore_publication(txdir, quiescence=quiet.hold)
            self.assertEqual(recovered["state"], STATE_ROLLED_BACK)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)
            self.assertEqual((txdir / "failed-new-vault" / "dynamic" / "new.md").read_bytes(), new)

    def test_crash_after_new_rename_before_journal_update_quarantines_new(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, new = self.fixture(temp)

            def fail(point: str) -> None:
                if point == "publication.new_renamed":
                    raise RuntimeError("crash")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="2" * 32, fault_injector=fail)
            txdir = transactions / ("2" * 32)
            self.assertEqual(json.loads((txdir / JOURNAL_NAME).read_text())["state"], STATE_OLD_STAGED)
            recovered = recover_restore_publication(txdir, quiescence=Quiescence().hold)
            self.assertEqual(recovered["state"], STATE_ROLLED_BACK)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)
            self.assertEqual((txdir / "failed-new-vault" / "dynamic" / "new.md").read_bytes(), new)

    def test_tampered_candidate_refuses_before_transaction_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, _old, _new = self.fixture(temp)
            (restored / "buckets" / "dynamic" / "new.md").write_text("tampered", encoding="utf-8")
            with self.assertRaises(Exception):
                publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="e" * 32)
            self.assertEqual(list(transactions.iterdir()), [])

    def test_transaction_root_must_be_fixed_live_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, _transactions, _old, _new = self.fixture(temp)
            wrong = Path(temp) / "transactions"
            wrong.mkdir()
            with self.assertRaises(RestorePublicationError):
                publish_verified_restore(restored, live, wrong, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="3" * 32)
            self.assertEqual(list(wrong.iterdir()), [])

    def test_tampered_journal_path_fails_closed_without_moving_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, old, new = self.fixture(temp)

            def fail(point: str) -> None:
                if point == "publication.prepared":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="4" * 32, fault_injector=fail)
            txdir = transactions / ("4" * 32)
            journal = txdir / JOURNAL_NAME
            payload = json.loads(journal.read_text(encoding="utf-8"))
            payload["live_name"] = "..\\outside"
            journal.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(RestorePublicationError):
                recover_restore_publication(txdir, quiescence=Quiescence().hold)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)
            self.assertEqual((restored / "buckets" / "dynamic" / "new.md").read_bytes(), new)

    def test_terminal_journal_with_tampered_rebuild_receipt_fails_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live, restored, transactions, _old, new = self.fixture(temp)
            result = publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="c" * 31 + "1")
            journal = Path(result["journal"])
            payload = json.loads(journal.read_text(encoding="utf-8"))
            payload["derived_rebuild"]["manifest_sha256"] = "0" * 64
            journal.write_text(json.dumps(payload), encoding="utf-8")
            quiet = Quiescence()
            with self.assertRaises(RestorePublicationError):
                recover_restore_publications_before_startup(transactions, quiescence=quiet.hold)
            self.assertEqual(quiet.entries, 0)
            self.assertEqual((live / "dynamic" / "new.md").read_bytes(), new)

    def test_startup_coordinator_validates_all_before_recovering_any(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            live = base / "live-a"
            old = write_bucket(live, "old-a", "old a")
            restored, new = make_restore(base, "new-a", "new a", "restored-a")
            transactions = base / TRANSACTION_ROOT_NAME
            transactions.mkdir()

            def stop(point: str) -> None:
                if point == "publication.prepared":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="6" * 32, fault_injector=stop)
            corrupt = transactions / ("7" * 32)
            corrupt.mkdir()
            (corrupt / JOURNAL_NAME).write_text("not-json", encoding="utf-8")
            quiet = Quiescence()
            with self.assertRaises(RestorePublicationError):
                recover_restore_publications_before_startup(transactions, quiescence=quiet.hold)
            self.assertEqual(quiet.entries, 0)
            self.assertEqual((live / "dynamic" / "old-a.md").read_bytes(), old)
            self.assertEqual((restored / "buckets" / "dynamic" / "new-a.md").read_bytes(), new)
            self.assertEqual(json.loads((transactions / ("6" * 32) / JOURNAL_NAME).read_text())["state"], STATE_PREPARED)

    def test_startup_coordinator_preflights_all_filesystem_shapes_before_first_move(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            transactions = base / TRANSACTION_ROOT_NAME
            transactions.mkdir()

            live_a = base / "live-a"
            old_a = write_bucket(live_a, "old-a", "old a")
            restored_a, _ = make_restore(base, "new-a", "new a", "restored-a")

            def after_old(point: str) -> None:
                if point == "publication.old_staged":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored_a, live_a, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="b" * 31 + "1", fault_injector=after_old)
            self.assertFalse(live_a.exists())

            live_b = base / "live-b"
            write_bucket(live_b, "old-b", "old b")
            restored_b, _ = make_restore(base, "new-b", "new b", "restored-b")

            def prepared(point: str) -> None:
                if point == "publication.prepared":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored_b, live_b, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="b" * 31 + "2", fault_injector=prepared)
            (restored_b / "buckets" / "dynamic" / "new-b.md").write_text("tampered", encoding="utf-8")

            quiet = Quiescence()
            with self.assertRaises(Exception):
                recover_restore_publications_before_startup(transactions, quiescence=quiet.hold)
            self.assertEqual(quiet.entries, 1)
            self.assertFalse(live_a.exists())
            self.assertEqual(
                (transactions / ("b" * 31 + "1") / "rollback-vault" / "dynamic" / "old-a.md").read_bytes(),
                old_a,
            )

    def test_startup_coordinator_recovers_active_and_reports_terminal_under_one_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            transactions = base / TRANSACTION_ROOT_NAME
            transactions.mkdir()

            live_a = base / "live-a"
            old_a = write_bucket(live_a, "old-a", "old a")
            restored_a, _ = make_restore(base, "new-a", "new a", "restored-a")

            def stop(point: str) -> None:
                if point == "publication.old_staged":
                    raise RuntimeError("stop")

            with self.assertRaises(RuntimeError):
                publish_verified_restore(restored_a, live_a, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="8" * 32, fault_injector=stop)

            live_b = base / "live-b"
            write_bucket(live_b, "old-b", "old b")
            restored_b, _ = make_restore(base, "new-b", "new b", "restored-b")
            publish_verified_restore(restored_b, live_b, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="9" * 32)

            quiet = Quiescence()
            result = recover_restore_publications_before_startup(transactions, quiescence=quiet.hold)
            self.assertEqual(result["active_recovered"], 1)
            self.assertEqual(result["total"], 2)
            self.assertEqual(quiet.entries, 1)
            self.assertEqual((live_a / "dynamic" / "old-a.md").read_bytes(), old_a)

    def test_startup_coordinator_rejects_duplicate_active_live_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            transactions = base / TRANSACTION_ROOT_NAME
            transactions.mkdir()
            live = base / "live"
            old = write_bucket(live, "old", "old")

            def stop(point: str) -> None:
                if point == "publication.prepared":
                    raise RuntimeError("stop")

            for index, txid in enumerate(("a" * 31 + "1", "a" * 31 + "2"), start=1):
                restored, _ = make_restore(base, f"new-{index}", f"new {index}", f"restored-{index}")
                with self.assertRaises(RuntimeError):
                    publish_verified_restore(restored, live, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid=txid, fault_injector=stop)
            quiet = Quiescence()
            with self.assertRaises(RestorePublicationError):
                recover_restore_publications_before_startup(transactions, quiescence=quiet.hold)
            self.assertEqual(quiet.entries, 0)
            self.assertEqual((live / "dynamic" / "old.md").read_bytes(), old)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_symlink_live_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real = base / "real"
            write_bucket(real, "old", "old")
            restored, _ = make_restore(base, "new", "new")
            transactions = base / TRANSACTION_ROOT_NAME
            transactions.mkdir()
            link = base / "live-link"
            try:
                os.symlink(real, link, target_is_directory=True)
            except OSError:
                self.skipTest("symlink privilege unavailable")
            with self.assertRaises(RestorePublicationError):
                publish_verified_restore(restored, link, transactions, quiescence=Quiescence().hold, rebuild_derived=rebuild_receipt, txid="f" * 32)


if __name__ == "__main__":
    unittest.main()
