"""M-04 isolated runtime admission and background lifecycle primitives.

These process-local controls are not wired into the server.  Cross-process
Markdown exclusion remains the responsibility of ``snapshot_barrier``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import inspect
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator


class RuntimeControlError(RuntimeError):
    """A runtime gate or registered background component failed closed."""


class AdmissionClosed(RuntimeControlError):
    """New work was refused because runtime admission is not open."""


class AdmissionDrainTimeout(RuntimeControlError):
    """Existing admitted work did not drain before its deadline."""


@dataclass(frozen=True)
class AdmissionStatus:
    state: str
    active: int
    generation: int
    failure: str


class RuntimeAdmissionGate:
    """One process-local admission gate shared by async and sync entrypoints."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "OPEN"
        self._active = 0
        self._generation = 0
        self._failure = ""

    def status(self) -> AdmissionStatus:
        with self._lock:
            return AdmissionStatus(
                state=self._state,
                active=self._active,
                generation=self._generation,
                failure=self._failure,
            )

    def _enter(self) -> int:
        with self._lock:
            if self._state != "OPEN":
                raise AdmissionClosed(f"runtime admission is {self._state.lower()}")
            generation = self._generation
            self._active += 1
            return generation

    def _exit(self, generation: int) -> None:
        with self._lock:
            if self._active <= 0:
                self._state = "BROKEN"
                self._failure = "admission lease underflow"
                raise RuntimeControlError(self._failure)
            # A lease from an earlier generation remains valid until release;
            # generation is retained for diagnostics and stale-release checks.
            if generation > self._generation:
                self._state = "BROKEN"
                self._failure = "admission lease generation is from the future"
                raise RuntimeControlError(self._failure)
            self._active -= 1

    @contextmanager
    def lease_sync(self) -> Iterator[None]:
        generation = self._enter()
        try:
            yield
        finally:
            self._exit(generation)

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        generation = self._enter()
        try:
            yield
        finally:
            self._exit(generation)

    async def close(self) -> None:
        with self._lock:
            if self._state == "BROKEN":
                raise RuntimeControlError("broken admission gate cannot close")
            if self._state != "OPEN":
                raise RuntimeControlError("runtime admission is already closed")
            self._state = "CLOSED"
            self._generation += 1

    async def active_count(self) -> int:
        with self._lock:
            return self._active

    async def drain(
        self, *, timeout_seconds: float = 30.0, poll_seconds: float = 0.01
    ) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("drain timeout and poll interval must be positive")
        with self._lock:
            if self._state != "CLOSED":
                raise RuntimeControlError("admission must be closed before drain")
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                active = self._active
                state = self._state
            if state != "CLOSED":
                raise RuntimeControlError("admission state changed during drain")
            if active == 0:
                return
            if time.monotonic() >= deadline:
                raise AdmissionDrainTimeout("timed out draining admitted work")
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    async def open(self) -> None:
        with self._lock:
            if self._state == "BROKEN":
                raise RuntimeControlError("broken admission gate cannot reopen")
            if self._state != "CLOSED":
                raise RuntimeControlError("runtime admission is not closed")
            if self._active:
                raise RuntimeControlError("cannot reopen admission with active leases")
            self._state = "OPEN"
            self._failure = ""

    async def fail_closed(self, reason: str) -> None:
        with self._lock:
            self._state = "BROKEN"
            self._failure = str(reason or "runtime admission failed closed")[:200]
            self._generation += 1


AsyncAction = Callable[[], Awaitable[Any]]
RunningProbe = Callable[[], bool]


@dataclass(frozen=True)
class BackgroundComponent:
    name: str
    is_running: RunningProbe
    stop: AsyncAction
    start: AsyncAction


@dataclass(frozen=True)
class BackgroundSnapshot:
    generation: int
    running_names: tuple[str, ...]


@dataclass(frozen=True)
class BackgroundRegistryStatus:
    state: str
    generation: int
    registered: tuple[str, ...]
    failure: str


class BackgroundTaskRegistry:
    """Freeze and restore registered component lifecycles deterministically."""

    def __init__(self) -> None:
        self._components: dict[str, BackgroundComponent] = {}
        self._state = "ACTIVE"
        self._generation = 0
        self._failure = ""
        self._serial = asyncio.Lock()

    def status(self) -> BackgroundRegistryStatus:
        return BackgroundRegistryStatus(
            state=self._state,
            generation=self._generation,
            registered=tuple(sorted(self._components)),
            failure=self._failure,
        )

    def register(self, component: BackgroundComponent) -> None:
        name = str(component.name or "").strip()
        if not name or name in self._components or "/" in name or "\\" in name:
            raise RuntimeControlError("background component name is invalid or duplicate")
        if self._state != "ACTIVE":
            raise RuntimeControlError("background registry is not accepting registrations")
        self._components[name] = BackgroundComponent(
            name=name,
            is_running=component.is_running,
            stop=component.stop,
            start=component.start,
        )

    async def _call(
        self, action: AsyncAction, *, label: str, timeout_seconds: float
    ) -> Any:
        try:
            value = action()
            if not inspect.isawaitable(value):
                raise TypeError("callback did not return an awaitable")
            return await asyncio.wait_for(value, timeout=timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeControlError(f"{label} failed: {type(exc).__name__}") from exc

    async def _restart_names(
        self, names: list[str], *, timeout_seconds: float
    ) -> list[str]:
        errors: list[str] = []
        for name in reversed(names):
            try:
                await self._call(
                    self._components[name].start,
                    label=f"background restart {name}",
                    timeout_seconds=timeout_seconds,
                )
            except BaseException as exc:
                errors.append(f"{name}:{type(exc).__name__}")
        return errors

    async def _stop_attempted_names(
        self, names: list[str], *, timeout_seconds: float
    ) -> list[str]:
        errors: list[str] = []
        for name in reversed(names):
            try:
                await self._call(
                    self._components[name].stop,
                    label=f"background fail-closed stop {name}",
                    timeout_seconds=timeout_seconds,
                )
            except BaseException as exc:
                errors.append(f"{name}:{type(exc).__name__}")
        return errors

    async def freeze(self, *, timeout_seconds: float = 30.0) -> BackgroundSnapshot:
        if timeout_seconds <= 0:
            raise ValueError("background timeout must be positive")
        async with self._serial:
            if self._state != "ACTIVE":
                raise RuntimeControlError("background registry is not active")
            running: list[str] = []
            for name in sorted(self._components):
                try:
                    is_running = self._components[name].is_running()
                except Exception as exc:
                    raise RuntimeControlError(
                        f"background running probe {name} failed: {type(exc).__name__}"
                    ) from exc
                if not isinstance(is_running, bool):
                    raise RuntimeControlError(
                        f"background running probe {name} returned non-boolean"
                    )
                if is_running:
                    running.append(name)
            self._state = "FREEZING"
            stopped: list[str] = []
            try:
                for name in running:
                    # Mark the attempted component so a partial stop is always
                    # compensated with an idempotent start callback.
                    stopped.append(name)
                    await self._call(
                        self._components[name].stop,
                        label=f"background stop {name}",
                        timeout_seconds=timeout_seconds,
                    )
            except BaseException as exc:
                task = asyncio.create_task(
                    self._restart_names(stopped, timeout_seconds=timeout_seconds)
                )
                try:
                    errors = await asyncio.shield(task)
                except asyncio.CancelledError:
                    errors = await task
                self._state = "BROKEN" if errors else "ACTIVE"
                self._failure = (
                    f"background freeze failed: {type(exc).__name__}"
                    + (f"; rollback={','.join(errors)}" if errors else "")
                )
                if errors:
                    raise RuntimeControlError(self._failure) from exc
                raise
            self._generation += 1
            self._state = "FROZEN"
            self._failure = ""
            return BackgroundSnapshot(
                generation=self._generation, running_names=tuple(running)
            )

    async def resume(
        self, snapshot: BackgroundSnapshot, *, timeout_seconds: float = 30.0
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("background timeout must be positive")
        async with self._serial:
            if self._state != "FROZEN" or snapshot.generation != self._generation:
                raise RuntimeControlError("background snapshot is stale or registry is not frozen")
            names = list(snapshot.running_names)
            if any(name not in self._components for name in names):
                raise RuntimeControlError("background snapshot references an unknown component")
            self._state = "RESUMING"
            started: list[str] = []
            try:
                for name in reversed(names):
                    started.append(name)
                    await self._call(
                        self._components[name].start,
                        label=f"background start {name}",
                        timeout_seconds=timeout_seconds,
                    )
            except BaseException as exc:
                # Reassert stopped postconditions in the exact reverse of the
                # attempted restart order. Shield cleanup from cancellation.
                task = asyncio.create_task(
                    self._stop_attempted_names(
                        started, timeout_seconds=timeout_seconds
                    )
                )
                try:
                    errors = await asyncio.shield(task)
                except asyncio.CancelledError:
                    errors = await task
                self._state = "BROKEN"
                self._failure = (
                    f"background resume failed: {type(exc).__name__}"
                    + (f"; fail-closed={','.join(errors)}" if errors else "")
                )
                raise RuntimeControlError(self._failure) from exc
            self._state = "ACTIVE"
            self._failure = ""
