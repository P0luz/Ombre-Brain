from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import frontmatter

from backup_archive import (
    RESTORE_MARKER_NAME,
    _restore_payload,
    _write_marker,
    build_authoritative_root_manifest,
)
from restore_publication import TRANSACTION_ROOT_NAME, publish_verified_restore

from service_quiescence import (
    ServiceDrainTimeout,
    ServiceQuiescenceAdapter,
    ServiceQuiescenceError,
)


class FakeRuntime:
    def __init__(self, writers: list[int] | None = None) -> None:
        self.events: list[str] = []
        self.writers = list(writers or [0])
        self.fail: set[str] = set()
        self.admission_open = True
        self.background_running = True
        self.derived_open = True

    async def _event(self, name: str) -> None:
        self.events.append(name)
        if name in self.fail:
            raise RuntimeError(name)

    async def stop_admission(self) -> None:
        await self._event("stop_admission")
        self.admission_open = False

    async def active_writers(self) -> int:
        self.events.append("active_writers")
        if len(self.writers) > 1:
            return self.writers.pop(0)
        return self.writers[0]

    async def stop_background(self) -> None:
        await self._event("stop_background")
        self.background_running = False

    async def close_derived(self) -> None:
        await self._event("close_derived")
        self.derived_open = False

    async def reopen_derived(self) -> None:
        await self._event("reopen_derived")
        self.derived_open = True

    async def start_background(self) -> None:
        await self._event("start_background")
        self.background_running = True

    async def resume_admission(self) -> None:
        await self._event("resume_admission")
        self.admission_open = True

    def adapter(self) -> ServiceQuiescenceAdapter:
        return ServiceQuiescenceAdapter(
            stop_admission=self.stop_admission,
            active_writers=self.active_writers,
            stop_background=self.stop_background,
            close_derived=self.close_derived,
            reopen_derived=self.reopen_derived,
            start_background=self.start_background,
            resume_admission=self.resume_admission,
        )


class ServiceQuiescenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_order_and_sync_publication_permit(self) -> None:
        runtime = FakeRuntime([2, 1, 0])
        adapter = runtime.adapter()
        async with adapter.hold(timeout_seconds=1, poll_seconds=0.001) as permit:
            self.assertEqual(adapter.status().phase, "QUIESCED")
            self.assertFalse(runtime.admission_open)
            self.assertFalse(runtime.background_running)
            self.assertFalse(runtime.derived_open)
            with permit.publication_context():
                self.assertEqual(adapter.status().active_publications, 1)
        self.assertEqual(adapter.status().phase, "IDLE")
        self.assertEqual(
            runtime.events,
            [
                "stop_admission",
                "active_writers",
                "active_writers",
                "active_writers",
                "stop_background",
                "close_derived",
                "reopen_derived",
                "start_background",
                "resume_admission",
            ],
        )

    async def test_drain_timeout_reopens_admission_without_stopping_background(self) -> None:
        runtime = FakeRuntime([1])
        adapter = runtime.adapter()
        with self.assertRaises(ServiceDrainTimeout):
            async with adapter.hold(timeout_seconds=0.02, poll_seconds=0.005):
                self.fail("unreachable")
        self.assertTrue(runtime.admission_open)
        self.assertTrue(runtime.background_running)
        self.assertTrue(runtime.derived_open)
        self.assertEqual(adapter.status().phase, "IDLE")
        self.assertNotIn("stop_background", runtime.events)

    async def test_close_derived_failure_rolls_back_background_then_admission(self) -> None:
        runtime = FakeRuntime()
        runtime.fail.add("close_derived")
        adapter = runtime.adapter()
        with self.assertRaises(ServiceQuiescenceError):
            async with adapter.hold():
                self.fail("unreachable")
        self.assertEqual(
            runtime.events,
            [
                "stop_admission",
                "active_writers",
                "stop_background",
                "close_derived",
                "reopen_derived",
                "start_background",
                "resume_admission",
            ],
        )
        self.assertEqual(adapter.status().phase, "IDLE")

    async def test_body_failure_reopens_runtime_then_reraises(self) -> None:
        runtime = FakeRuntime()
        adapter = runtime.adapter()
        with self.assertRaisesRegex(RuntimeError, "body"):
            async with adapter.hold():
                raise RuntimeError("body")
        self.assertTrue(runtime.admission_open)
        self.assertTrue(runtime.background_running)
        self.assertTrue(runtime.derived_open)

    async def test_cancel_during_drain_completes_admission_rollback(self) -> None:
        runtime = FakeRuntime([1])
        adapter = runtime.adapter()

        async def run() -> None:
            async with adapter.hold(timeout_seconds=10, poll_seconds=0.01):
                self.fail("unreachable")

        task = asyncio.create_task(run())
        for _ in range(100):
            if runtime.events.count("active_writers") >= 1:
                break
            await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(runtime.admission_open)
        self.assertEqual(adapter.status().phase, "IDLE")

    async def test_reopen_failure_fails_closed_without_resuming_admission(self) -> None:
        runtime = FakeRuntime()
        runtime.fail.add("reopen_derived")
        adapter = runtime.adapter()
        with self.assertRaises(ServiceQuiescenceError):
            async with adapter.hold():
                pass
        self.assertFalse(runtime.admission_open)
        self.assertEqual(adapter.status().phase, "FAILED")
        self.assertNotIn("resume_admission", runtime.events)
        self.assertEqual(runtime.events[-1], "close_derived")

    async def test_resume_admission_failure_reasserts_all_closed_postconditions(self) -> None:
        runtime = FakeRuntime()
        runtime.fail.add("resume_admission")
        adapter = runtime.adapter()
        with self.assertRaises(ServiceQuiescenceError):
            async with adapter.hold():
                pass
        self.assertEqual(adapter.status().phase, "FAILED")
        tail = runtime.events[-3:]
        self.assertEqual(tail, ["stop_admission", "stop_background", "close_derived"])
        self.assertFalse(runtime.admission_open)
        self.assertFalse(runtime.background_running)
        self.assertFalse(runtime.derived_open)

    async def test_permit_rejects_use_after_hold(self) -> None:
        runtime = FakeRuntime()
        adapter = runtime.adapter()
        async with adapter.hold() as permit:
            pass
        with self.assertRaises(ServiceQuiescenceError):
            with permit.publication_context():
                pass

    async def test_permit_bridges_threaded_restore_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            live = base / "live"
            live_dynamic = live / "dynamic"
            live_dynamic.mkdir(parents=True)
            live_dynamic.joinpath("old.md").write_bytes(
                frontmatter.dumps(frontmatter.Post("old", id="old", tags=["owner:cheng"])).encode("utf-8")
            )
            restored = base / "restored"
            candidate = restored / "buckets"
            candidate_dynamic = candidate / "dynamic"
            candidate_dynamic.mkdir(parents=True)
            new_bytes = frontmatter.dumps(
                frontmatter.Post("new", id="new", tags=["owner:cheng"])
            ).encode("utf-8")
            candidate_dynamic.joinpath("new.md").write_bytes(new_bytes)
            manifest = build_authoritative_root_manifest(
                candidate, created_at="2026-08-02T00:00:00+00:00"
            )
            _write_marker(
                restored,
                RESTORE_MARKER_NAME,
                _restore_payload(state="complete", manifest=manifest),
            )
            transactions = base / TRANSACTION_ROOT_NAME
            transactions.mkdir()

            def rebuild(_live: Path, exact_manifest: dict) -> dict:
                raw = json.dumps(
                    exact_manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                return {
                    "status": "verified",
                    "sqlite_policy": "rebuilt-from-authoritative-markdown",
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                }

            runtime = FakeRuntime()
            adapter = runtime.adapter()
            async with adapter.hold() as permit:
                result = await asyncio.to_thread(
                    publish_verified_restore,
                    restored,
                    live,
                    transactions,
                    quiescence=permit.publication_context,
                    rebuild_derived=rebuild,
                    txid="e" * 32,
                )
                self.assertEqual(result["state"], "COMMITTED")
            self.assertEqual((live / "dynamic" / "new.md").read_bytes(), new_bytes)
            self.assertEqual(adapter.status().phase, "IDLE")


if __name__ == "__main__":
    unittest.main()
