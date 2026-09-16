from __future__ import annotations

import asyncio
import threading
import unittest

from runtime_control import (
    AdmissionClosed,
    AdmissionDrainTimeout,
    BackgroundComponent,
    BackgroundTaskRegistry,
    RuntimeAdmissionGate,
    RuntimeControlError,
)
from service_quiescence import ServiceQuiescenceAdapter


class FakeComponent:
    def __init__(self, name: str, events: list[str], *, running: bool = True) -> None:
        self.name = name
        self.events = events
        self.running = running
        self.fail: set[str] = set()

    def is_running(self) -> bool:
        self.events.append(f"probe:{self.name}")
        return self.running

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.running = False
        if "stop" in self.fail:
            raise RuntimeError("stop")

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        self.running = True
        if "start" in self.fail:
            raise RuntimeError("start")

    def registration(self) -> BackgroundComponent:
        return BackgroundComponent(
            name=self.name,
            is_running=self.is_running,
            stop=self.stop,
            start=self.start,
        )


class RuntimeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_and_sync_leases_share_one_count_and_close_refuses_new(self) -> None:
        gate = RuntimeAdmissionGate()
        sync_entered = threading.Event()
        sync_release = threading.Event()

        def worker() -> None:
            with gate.lease_sync():
                sync_entered.set()
                sync_release.wait(2)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(sync_entered.wait(1))
        async with gate.lease():
            self.assertEqual((await gate.active_count()), 2)
            await gate.close()
            with self.assertRaises(AdmissionClosed):
                async with gate.lease():
                    pass
        self.assertEqual((await gate.active_count()), 1)
        sync_release.set()
        thread.join(2)
        await gate.drain(timeout_seconds=1)
        await gate.open()
        self.assertEqual(gate.status().state, "OPEN")

    async def test_drain_timeout_and_cancel_do_not_change_closed_state(self) -> None:
        gate = RuntimeAdmissionGate()
        lease = gate.lease()
        await lease.__aenter__()
        await gate.close()
        with self.assertRaises(AdmissionDrainTimeout):
            await gate.drain(timeout_seconds=0.02, poll_seconds=0.005)
        task = asyncio.create_task(gate.drain(timeout_seconds=10, poll_seconds=0.01))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(gate.status().state, "CLOSED")
        await lease.__aexit__(None, None, None)
        await gate.drain(timeout_seconds=1)

    async def test_fail_closed_cannot_reopen(self) -> None:
        gate = RuntimeAdmissionGate()
        await gate.fail_closed("boom")
        with self.assertRaises(AdmissionClosed):
            async with gate.lease():
                pass
        with self.assertRaises(RuntimeControlError):
            await gate.open()


class BackgroundRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_stops_sorted_and_restarts_reverse_only_originally_running(self) -> None:
        events: list[str] = []
        registry = BackgroundTaskRegistry()
        a = FakeComponent("a", events, running=True)
        b = FakeComponent("b", events, running=False)
        c = FakeComponent("c", events, running=True)
        for component in (c, b, a):
            registry.register(component.registration())
        snapshot = await registry.freeze()
        self.assertEqual(snapshot.running_names, ("a", "c"))
        self.assertFalse(a.running)
        self.assertFalse(c.running)
        await registry.resume(snapshot)
        self.assertEqual(events[-2:], ["start:c", "start:a"])
        self.assertFalse(b.running)

    async def test_partial_stop_failure_restarts_attempted_components(self) -> None:
        events: list[str] = []
        registry = BackgroundTaskRegistry()
        a = FakeComponent("a", events)
        b = FakeComponent("b", events)
        b.fail.add("stop")
        registry.register(a.registration())
        registry.register(b.registration())
        with self.assertRaises(RuntimeControlError):
            await registry.freeze()
        self.assertTrue(a.running)
        self.assertTrue(b.running)
        self.assertEqual(registry.status().state, "ACTIVE")
        self.assertEqual(events[-2:], ["start:b", "start:a"])

    async def test_restart_failure_reasserts_stopped_and_marks_broken(self) -> None:
        events: list[str] = []
        registry = BackgroundTaskRegistry()
        a = FakeComponent("a", events)
        b = FakeComponent("b", events)
        registry.register(a.registration())
        registry.register(b.registration())
        snapshot = await registry.freeze()
        a.fail.add("start")
        with self.assertRaises(RuntimeControlError):
            await registry.resume(snapshot)
        self.assertEqual(registry.status().state, "BROKEN")
        self.assertFalse(a.running)
        self.assertFalse(b.running)
        self.assertEqual(events[-2:], ["stop:a", "stop:b"])

    async def test_cancel_during_freeze_restarts_attempted_component(self) -> None:
        events: list[str] = []
        registry = BackgroundTaskRegistry()
        entered = asyncio.Event()
        release = asyncio.Event()
        running = True

        async def stop() -> None:
            nonlocal running
            events.append("stop:slow")
            running = False
            entered.set()
            await release.wait()

        async def start() -> None:
            nonlocal running
            events.append("start:slow")
            running = True

        registry.register(
            BackgroundComponent(
                name="slow",
                is_running=lambda: running,
                stop=stop,
                start=start,
            )
        )
        task = asyncio.create_task(registry.freeze(timeout_seconds=10))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(running)
        self.assertEqual(registry.status().state, "ACTIVE")

    async def test_registration_rejected_while_frozen(self) -> None:
        registry = BackgroundTaskRegistry()
        events: list[str] = []
        a = FakeComponent("a", events)
        registry.register(a.registration())
        snapshot = await registry.freeze()
        with self.assertRaises(RuntimeControlError):
            registry.register(FakeComponent("b", events).registration())
        await registry.resume(snapshot)


class RuntimeControlIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_quiescence_drains_gate_and_freezes_registry(self) -> None:
        gate = RuntimeAdmissionGate()
        events: list[str] = []
        registry = BackgroundTaskRegistry()
        component = FakeComponent("decay", events)
        registry.register(component.registration())
        snapshot_box: dict[str, object] = {}
        derived_open = True

        async def stop_background() -> None:
            snapshot_box["value"] = await registry.freeze()

        async def start_background() -> None:
            await registry.resume(snapshot_box["value"])  # type: ignore[arg-type]

        async def close_derived() -> None:
            nonlocal derived_open
            derived_open = False

        async def reopen_derived() -> None:
            nonlocal derived_open
            derived_open = True

        adapter = ServiceQuiescenceAdapter(
            stop_admission=gate.close,
            active_writers=gate.active_count,
            stop_background=stop_background,
            close_derived=close_derived,
            reopen_derived=reopen_derived,
            start_background=start_background,
            resume_admission=gate.open,
        )
        release = asyncio.Event()

        async def writer() -> None:
            async with gate.lease():
                await release.wait()

        writer_task = asyncio.create_task(writer())
        await asyncio.sleep(0)

        async def quiesce() -> None:
            async with adapter.hold(timeout_seconds=1, poll_seconds=0.001):
                self.assertEqual(gate.status().state, "CLOSED")
                self.assertFalse(component.running)
                self.assertFalse(derived_open)
                with self.assertRaises(AdmissionClosed):
                    async with gate.lease():
                        pass

        quiet_task = asyncio.create_task(quiesce())
        await asyncio.sleep(0.02)
        self.assertFalse(quiet_task.done())
        release.set()
        await writer_task
        await quiet_task
        self.assertEqual(gate.status().state, "OPEN")
        self.assertTrue(component.running)
        self.assertTrue(derived_open)


if __name__ == "__main__":
    unittest.main()
