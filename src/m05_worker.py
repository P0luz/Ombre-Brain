"""M-05 batch 2: isolated startup recovery + bounded READY worker.

This module is deliberately callable-only.  It does not wire itself into
`server.py`, does not depend on M-02, and does not touch production vaults.

Privacy invariant: the worker never stores Markdown content in logs or
outbox slots.  Only content hashes appear in outbox files.

Concurrency invariant: every outbox slot mutation uses the existing
intent/generation CAS, so a stale worker cannot overwrite a newer generation.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Awaitable, Callable

from embedding_outbox import (
    OP_UPSERT,
    STATE_CONFLICT,
    STATE_PREPARED,
    STATE_READY,
    StaleIntentError,
    content_sha256,
    is_terminal,
    list_intents,
    mark_applied,
    recover_intent,
    transition_intent,
)


# --- Thread-safe M-05 health state ---
_m05_health_lock = threading.Lock()
_m05_health: dict[str, object] = {
    "wiring": "unconfigured",
    "enabled": False,
    "startup_state": None,
    "recovered": 0,
    "skipped": 0,
    "applied": 0,
    "conflict": 0,
    "failed": 0,
    "unresolved_prepared": 0,
    "unresolved_ready": 0,
    "unresolved_conflict": 0,
}


def get_m05_health() -> dict[str, object]:
    """Return a thread-safe shallow copy of M-05 process health state."""
    with _m05_health_lock:
        return dict(_m05_health)


def _set_m05_health_disabled() -> None:
    """Mark M-05 as intentionally disabled (healthy no-op)."""
    with _m05_health_lock:
        _m05_health.update(
            wiring="active",
            enabled=False,
            startup_state="disabled",
            recovered=0,
            skipped=0,
            applied=0,
            conflict=0,
            failed=0,
            unresolved_prepared=0,
            unresolved_ready=0,
            unresolved_conflict=0,
        )


def _set_m05_health_complete(
    recovered: int,
    skipped: int,
    applied: int,
    conflict: int,
    failed: int,
    unresolved_prepared: int = 0,
    unresolved_ready: int = 0,
    unresolved_conflict: int = 0,
) -> None:
    """Record a successful bounded startup pass with aggregate counts only.

    Rejects any nonzero *failed*, *conflict*, *unresolved_prepared*,
    *unresolved_ready*, or *unresolved_conflict* with a fixed safe
    ``ValueError`` that carries no user content, bucket ID, path, or
    provider data.
    """
    if failed or conflict or unresolved_prepared or unresolved_ready or unresolved_conflict:
        raise ValueError(
            "_set_m05_health_complete rejected: nonzero error or unresolved count"
        )
    with _m05_health_lock:
        _m05_health.update(
            wiring="active",
            enabled=True,
            startup_state="complete",
            recovered=recovered,
            skipped=skipped,
            applied=applied,
            conflict=conflict,
            failed=failed,
            unresolved_prepared=unresolved_prepared,
            unresolved_ready=unresolved_ready,
            unresolved_conflict=unresolved_conflict,
        )


def _set_m05_health_failed(
    unresolved_prepared: int = 0,
    unresolved_ready: int = 0,
    unresolved_conflict: int = 0,
) -> None:
    """Record fail-closed startup (no provider/outbox detail)."""
    with _m05_health_lock:
        _m05_health.update(
            wiring="active",
            enabled=True,
            startup_state="failed",
            recovered=0,
            skipped=0,
            applied=0,
            conflict=0,
            failed=0,
            unresolved_prepared=unresolved_prepared,
            unresolved_ready=unresolved_ready,
            unresolved_conflict=unresolved_conflict,
        )


class StartupRecoveryFailed(RuntimeError):
    """Fail-closed startup barrier: recovery errors prevent READY processing.

    The message is constrained to a fixed *phase* and *error_count* so that
    no user content, file path, or provider data ever leaks through a
    raised exception.  Underlying exception details are recorded only in
    the log, never in the exception message.
    """

    __slots__ = ("phase", "error_count")

    def __init__(self, phase: str, error_count: int) -> None:
        self.phase: str = phase
        self.error_count: int = error_count
        message = f"startup_recovery_failed phase={phase} errors={error_count}"
        super().__init__(message)


class StartupUnresolvedError(RuntimeError):
    """Fail-closed startup barrier: unresolved intents remain after worker pass.

    Carries a fixed *phase*, *code*, and safe aggregate counts for PREPARED,
    READY, and CONFLICT create UPSERT intents.  No user content, file path,
    bucket ID, or provider data ever appears in the exception message.
    """

    __slots__ = ("phase", "code", "prepared", "ready", "conflict")

    def __init__(
        self,
        phase: str,
        code: str,
        *,
        prepared: int = 0,
        ready: int = 0,
        conflict: int = 0,
    ) -> None:
        self.phase: str = phase
        self.code: str = code
        self.prepared: int = prepared
        self.ready: int = ready
        self.conflict: int = conflict
        message = (
            f"startup_unresolved phase={phase} code={code} "
            f"prepared={prepared} ready={ready} conflict={conflict}"
        )
        super().__init__(message)


logger = logging.getLogger("ombre_brain.m05_worker")

BucketTurnContentRead = Callable[[str], Awaitable[str | None]]
"""Read authoritative Markdown for *bucket_id* under the canonical bucket turn.

Every call to this callback must acquire the caller-provided bucket-turn lock
before reading the authoritative Markdown, then release it before returning.
Under no circumstances may the worker trust an unlocked read.

Concrete callers wire this as::

    async with manager._bucket_turn(bucket_id):
        bucket = await manager.get(bucket_id)
        return None if bucket is None else str(bucket["content"] or "")

The bucket-turn lock is the *only* mechanism that guarantees the worker
observes a consistent Markdown snapshot.  Omitting the lock opens the
worker to the full TOCTOU window between create, provider work, and the
post-check hash verification.

Returns *None* when the authoritative bucket does not exist.

This callback is the single canonical point at which the bucket turn enters
the worker's control flow.  Neither ``recover_all_intents`` nor
``process_ready_intents`` acquires the turn internally."""

Provider = Callable[[str, str], Awaitable[bool]]
"""Run the embedding provider (and any SQLite work) for (*bucket_id*, *content*)."""


async def recover_all_intents(
    outbox_root: str,
    read_content: BucketTurnContentRead,
) -> dict[str, int]:
    """Enumerate every outbox intent and classify PREPARED create UPSERTs.

    Each classification reads authoritative Markdown under the caller-supplied
    *read_content* callback, which MUST hold the canonical bucket turn.
    Terminal intents are skipped without a read.

    Returns `{"recovered": N, "skipped": N, "errors": N}`.
    """

    intents = list_intents(outbox_root)
    recovered = 0
    skipped = 0
    errors = 0

    for intent in intents:
        if is_terminal(intent):
            skipped += 1
            continue
        if intent.state != STATE_PREPARED:
            skipped += 1
            continue
        if intent.operation != OP_UPSERT:
            # DELETE intents are not wired into the create-only path.
            # A future batch (after update/delete wiring) should classify them.
            skipped += 1
            continue

        try:
            content = await read_content(intent.bucket_id)
            recover_intent(outbox_root, intent, content)
            recovered += 1
        except StaleIntentError:
            # Another worker or recovery pass already processed this slot.
            recovered += 1
        except Exception as exc:
            logger.warning(
                "M-05 recovery read failed phase=recovery bucket_id=%s intent_id=%s gen=%d exc_type=%s",
                intent.bucket_id,
                intent.intent_id,
                intent.generation,
                type(exc).__name__,
            )
            errors += 1

    return {"recovered": recovered, "skipped": skipped, "errors": errors}


async def process_ready_intents(
    outbox_root: str,
    read_content: BucketTurnContentRead,
    provider: Provider,
    limit: int = 16,
) -> dict[str, int]:
    """Process at most *limit* READY create UPSERT intents in one bounded pass.

    The pass is deterministic (sorted by bucket_id).  Provider work runs
    outside every M-04, content/quota, bucket, and outbox-slot lock.  Each
    slot transition uses intent/generation CAS.

    Returns `{"applied": N, "conflict": N, "failed": N}`.
    """

    if limit < 1:
        raise ValueError("limit must be positive")

    readies = list_intents(outbox_root, STATE_READY)
    applied = 0
    conflict = 0
    failed = 0

    for intent in readies[:limit]:
        # Only create UPSERTs are wired.  DELETE/UPDATE stay as future work.
        if intent.operation != OP_UPSERT:
            continue

        # ---- pre-provider hash-check ----
        try:
            content = await read_content(intent.bucket_id)
        except Exception as exc:
            logger.warning(
                "M-05 worker pre-check read failed phase=pre-check bucket_id=%s intent_id=%s gen=%d exc_type=%s",
                intent.bucket_id, intent.intent_id, intent.generation, type(exc).__name__,
            )
            failed += 1
            continue

        if content is None or content_sha256(content) != intent.target_sha256:
            try:
                transition_intent(outbox_root, intent, STATE_CONFLICT)
            except StaleIntentError:
                pass
            conflict += 1
            continue

        # ---- provider work (outside every lock) ----
        try:
            provider_ok = await provider(intent.bucket_id, content)
        except Exception as exc:
            logger.warning(
                "M-05 provider failed phase=provider bucket_id=%s intent_id=%s gen=%d exc_type=%s",
                intent.bucket_id, intent.intent_id, intent.generation, type(exc).__name__,
            )
            # Preserve READY so a future pass can retry.
            failed += 1
            continue

        if not provider_ok:
            # Provider returned False: preserve READY, do not post-check/mark APPLIED.
            failed += 1
            continue

        # ---- post-provider hash-check ----
        try:
            current = await read_content(intent.bucket_id)
        except Exception as exc:
            logger.warning(
                "M-05 worker post-check read failed phase=post-check bucket_id=%s intent_id=%s gen=%d exc_type=%s",
                intent.bucket_id, intent.intent_id, intent.generation, type(exc).__name__,
            )
            # Markdown survived (provider succeeded), but we cannot verify.
            # Leave READY rather than guessing.
            failed += 1
            continue

        if current is not None and content_sha256(current) == intent.target_sha256:
            try:
                mark_applied(outbox_root, intent)
            except StaleIntentError:
                conflict += 1
                continue
            applied += 1
        else:
            try:
                transition_intent(outbox_root, intent, STATE_CONFLICT)
            except StaleIntentError:
                pass
            conflict += 1

    return {"applied": applied, "conflict": conflict, "failed": failed}


def enumerate_unresolved(outbox_root: str) -> dict[str, int]:
    """Re-enumerate outbox and count PREPARED/READY/CONFLICT create UPSERT intents.

    This inspection never reads, stores, or logs user content, bucket IDs,
    file paths, or provider data.  It only returns safe aggregate counts.
    """
    intents = list_intents(outbox_root)
    prepared = 0
    ready = 0
    conflict = 0
    for intent in intents:
        if intent.operation != OP_UPSERT:
            continue
        if intent.state == STATE_PREPARED:
            prepared += 1
        elif intent.state == STATE_READY:
            ready += 1
        elif intent.state == STATE_CONFLICT:
            conflict += 1
    return {"prepared": prepared, "ready": ready, "conflict": conflict}


async def startup_recover_and_process(
    outbox_root: str,
    read_content: BucketTurnContentRead,
    provider: Provider,
    worker_limit: int = 16,
) -> dict[str, Any]:
    """Sequential recovery + bounded worker suitable for server startup wiring.

    The caller is responsible for wiring the *read_content* and *provider*
    callbacks into the runtime container (e.g. `BucketManager.get` and
    `EmbeddingEngine.generate_and_store`).

    This function is async-callable but does not import or depend on
    `server.py`, `BucketManager`, or any production vault.
    """

    recovery = await recover_all_intents(outbox_root, read_content)
    if recovery["errors"] > 0:
        raise StartupRecoveryFailed("recovery", recovery["errors"])
    worker = await process_ready_intents(
        outbox_root, read_content, provider, worker_limit
    )

    # Post-pass: re-enumerate outbox for unresolved create UPSERT intents.
    # This is the authoritative gate — a bounded worker limit, provider False,
    # conflict, or stale intent can all leave READY/CONFLICT behind.
    unresolved = enumerate_unresolved(outbox_root)

    if (
        worker["conflict"] > 0
        or worker["failed"] > 0
        or unresolved["prepared"] > 0
        or unresolved["ready"] > 0
        or unresolved["conflict"] > 0
    ):
        raise StartupUnresolvedError(
            "worker",
            "m05_embedding_outbox_unresolved",
            prepared=unresolved["prepared"],
            ready=unresolved["ready"],
            conflict=unresolved["conflict"],
        )

    return {
        "recovery": recovery,
        "worker": worker,
        "unresolved": unresolved,
    }
