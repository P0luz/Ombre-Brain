from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from restore_acceptance import build_read_only_acceptance
from runtime_control import BackgroundRegistryStatus, AdmissionStatus
from service_quiescence import ServiceQuiescenceStatus


def statuses():
    return (
        AdmissionStatus(state="OPEN", active=0, generation=0, failure=""),
        BackgroundRegistryStatus(
            state="ACTIVE", generation=0, registered=(), failure=""
        ),
        ServiceQuiescenceStatus(
            phase="IDLE",
            admission_stopped=False,
            background_stopped=False,
            derived_closed=False,
            active_publications=0,
            failure="",
        ),
    )


class RestoreAcceptanceTests(unittest.TestCase):
    def test_read_only_go_does_not_create_transaction_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp) / "buckets"
            (live / "dynamic").mkdir(parents=True)
            (live / "dynamic" / "a.md").write_text("hello", encoding="utf-8")
            admission, background, quiet = statuses()
            result = build_read_only_acceptance(
                live_vault=live,
                transport="streamable-http",
                admission_status=admission,
                background_status=background,
                quiescence_status=quiet,
            )
            self.assertEqual(result["verdict"], "GO")
            self.assertTrue(result["read_only"])
            self.assertFalse((live.parent / ".m04-restore-transactions").exists())

    def test_non_streamable_or_busy_runtime_is_no_go(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp) / "buckets"
            live.mkdir()
            admission, background, quiet = statuses()
            busy = AdmissionStatus(
                state="OPEN", active=1, generation=0, failure=""
            )
            result = build_read_only_acceptance(
                live_vault=live,
                transport="sse",
                admission_status=busy,
                background_status=background,
                quiescence_status=quiet,
            )
            self.assertEqual(result["verdict"], "NO-GO")
            self.assertEqual(len(result["blockers"]), 2)

    def test_reparse_member_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            live = base / "buckets"
            live.mkdir()
            target = base / "target.txt"
            target.write_text("x", encoding="utf-8")
            link = live / "link.txt"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlink privilege unavailable")
            admission, background, quiet = statuses()
            with self.assertRaises(Exception):
                build_read_only_acceptance(
                    live_vault=live,
                    transport="streamable-http",
                    admission_status=admission,
                    background_status=background,
                    quiescence_status=quiet,
                )


if __name__ == "__main__":
    unittest.main()
