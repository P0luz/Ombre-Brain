"""M-04 isolated service-quiescence lifecycle protocol.

This module is not wired into ``server.py``.  It freezes the ordering and
fail-closed semantics required before a production restore publication may use
the synchronous permit exposed while the async runtime is fully quiesced.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import inspect
import threading
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator


AsyncAction = Callable[[], Awaitable[Any]]
WriterCount = Callable[[], Awaitable[int]]


class ServiceQuiescenceError(RuntimeError):
    """The runtime could not enter or leave a provably safe quiescent state."""


class ServiceDrainTimeout(ServiceQuiescenceError):
    """In-flight writers did not drain before the fixed deadline."""


@dataclass(frozen=True)
class ServiceQuiescenceStatus:
    phase: str
    admission_stopped: bool
    background_stopped: bool
    derived_closed: bool
    active_publications: int
    failure: str


class PublicationPermit:
    """Synchronous bridge usable only inside one active async quiescence hold."""

    def __init__(self, owner: "ServiceQuiescenceAdapter") -> None:
        self._owner = owner

    @contextmanager
    def publication_context(self) -> Iterator[None]:
        with self._owner._permit_lock:
            if self._owner._phase != "QUIESCED":
                raise ServiceQuiescenceError("publication permit is not active")
            self._owner._active_publications += 1
        try:
            yield
        finally:
            with self._owner._permit_lock:
                self._owner._active_publications -= 1


class ServiceQuiescenceAdapter:
    """Coordinate admission, writer drain, background work, and derived handles."""

    def __init__(
        self,
        *,
        stop_admission: AsyncAction,
        active_writers: WriterCount,
        stop_background: AsyncAction,
        close_derived: AsyncAction,
        reopen_derived: AsyncAction,
        start_background: AsyncAction,
        resume_admission: AsyncAction,
    ) -> None:
        self._stop_admission = stop_admission
        self._active_writers = active_writers
        self._stop_background = stop_background
        self._close_derived = close_derived
        self._reopen_derived = reopen_derived
        self._start_background = start_background
        self._resume_admission = resume_admission
        self._serial = asyncio.Lock()
        self._permit_lock = threading.Lock()
        self._active_publications = 0
        self._phase = "IDLE"
        self._admission_stopped = False
        self._background_stopped = False
        self._derived_closed = False
        self._failure = ""

    def status(self) -> ServiceQuiescenceStatus:
        with self._permit_lock:
            publications = self._active_publications
        return ServiceQuiescenceStatus(
            phase=self._phase,
            admission_stopped=self._admission_stopped,
            background_stopped=self._background_stopped,
            derived_closed=self._derived_closed,
            active_publications=publications,
            failure=self._failure,
        )

    async def _call(self, action: AsyncAction, label: str) -> Any:
        try:
            value = action()
            if not inspect.isawaitable(value):
                raise TypeError("callback did not return an awaitable")
            return await value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise ServiceQuiescenceError(f"{label} failed: {type(exc).__name__}") from exc

    async def _drain(self, *, timeout_seconds: float, poll_seconds: float) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("drain timeout and poll interval must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                count = await self._active_writers()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ServiceQuiescenceError(
                    f"active writer probe failed: {type(exc).__name__}"
                ) from exc
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ServiceQuiescenceError("active writer probe returned an invalid count")
            if count == 0:
                return
            if time.monotonic() >= deadline:
                raise ServiceDrainTimeout("timed out draining in-flight writers")
            await asyncio.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    async def _cleanup_after_setup_failure(self) -> list[str]:
        errors: list[str] = []
        if self._derived_closed:
            try:
                await self._call(self._reopen_derived, "derived reopen rollback")
                self._derived_closed = False
            except BaseException as exc:
                errors.append(f"reopen-derived:{type(exc).__name__}")
        if self._background_stopped:
            try:
                await self._call(self._start_background, "background restart rollback")
                self._background_stopped = False
            except BaseException as exc:
                errors.append(f"restart-background:{type(exc).__name__}")
        if self._admission_stopped:
            try:
                await self._call(self._resume_admission, "admission resume rollback")
                self._admission_stopped = False
            except BaseException as exc:
                errors.append(f"resume-admission:{type(exc).__name__}")
        return errors

    async def _shielded_cleanup(self) -> list[str]:
        task = asyncio.create_task(self._cleanup_after_setup_failure())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            return await task

    async def _fail_closed_after_reopen_error(self) -> list[str]:
        """Best-effort return to closed runtime; never resume admission."""

        errors: list[str] = []
        try:
            # A failed resume may have applied its side effect before raising.
            # Reassert the closed postcondition even when our logical flag says
            # admission is already stopped.
            await self._call(self._stop_admission, "admission fail-closed stop")
            self._admission_stopped = True
        except BaseException as exc:
            errors.append(f"stop-admission:{type(exc).__name__}")
        try:
            await self._call(self._stop_background, "background fail-closed stop")
            self._background_stopped = True
        except BaseException as exc:
            errors.append(f"stop-background:{type(exc).__name__}")
        try:
            # Reopen callbacks may fail after a partial side effect.  Close is
            # intentionally called even when our logical flag still says closed.
            await self._call(self._close_derived, "derived fail-closed close")
            self._derived_closed = True
        except BaseException as exc:
            errors.append(f"close-derived:{type(exc).__name__}")
        return errors

    async def _shielded_fail_closed(self) -> list[str]:
        task = asyncio.create_task(self._fail_closed_after_reopen_error())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            return await task

    async def _reopen_runtime(self) -> None:
        with self._permit_lock:
            if self._active_publications:
                self._phase = "FAILED"
                self._failure = "publication context escaped quiescence hold"
                raise ServiceQuiescenceError(self._failure)
        try:
            self._phase = "REOPENING_DERIVED"
            await self._call(self._reopen_derived, "derived reopen")
            self._derived_closed = False
            self._phase = "STARTING_BACKGROUND"
            await self._call(self._start_background, "background restart")
            self._background_stopped = False
            self._phase = "RESUMING_ADMISSION"
            await self._call(self._resume_admission, "admission resume")
            self._admission_stopped = False
            self._phase = "IDLE"
            self._failure = ""
        except BaseException as exc:
            errors = await self._shielded_fail_closed()
            self._phase = "FAILED"
            self._failure = (
                f"runtime reopen failed: {type(exc).__name__}"
                + (f"; rollback={','.join(errors)}" if errors else "")
            )
            raise ServiceQuiescenceError(self._failure) from exc

    @asynccontextmanager
    async def hold(
        self, *, timeout_seconds: float = 30.0, poll_seconds: float = 0.01
    ) -> AsyncIterator[PublicationPermit]:
        """Enter the frozen quiescence order and reopen in exact reverse order."""

        async with self._serial:
            if self._phase != "IDLE":
                raise ServiceQuiescenceError("service quiescence adapter is not idle")
            self._failure = ""
            try:
                self._phase = "STOPPING_ADMISSION"
                self._admission_stopped = True
                await self._call(self._stop_admission, "admission stop")
                self._phase = "DRAINING_WRITERS"
                await self._drain(
                    timeout_seconds=timeout_seconds, poll_seconds=poll_seconds
                )
                self._phase = "STOPPING_BACKGROUND"
                self._background_stopped = True
                await self._call(self._stop_background, "background stop")
                self._phase = "CLOSING_DERIVED"
                self._derived_closed = True
                await self._call(self._close_derived, "derived close")
                self._phase = "QUIESCED"
            except BaseException as exc:
                errors = await self._shielded_cleanup()
                self._phase = "FAILED" if errors else "IDLE"
                self._failure = (
                    f"quiescence setup failed: {type(exc).__name__}"
                    + (f"; rollback={','.join(errors)}" if errors else "")
                )
                if errors:
                    raise ServiceQuiescenceError(self._failure) from exc
                raise

            body_error: BaseException | None = None
            try:
                yield PublicationPermit(self)
            except BaseException as exc:
                body_error = exc
            try:
                await self._reopen_runtime()
            except BaseException:
                raise
            if body_error is not None:
                raise body_error
