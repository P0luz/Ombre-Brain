"""Spawn-safe helpers for the M-01 multiprocessing acceptance tests.

This module intentionally imports only the standard library at module import
time.  A spawned child first points both supported vault environment variables
at the pytest-owned temporary vault, and only then imports Ombre Brain modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys
import traceback
from typing import Any


class FakeEmbedding:
    """Deterministic, process-local embedding provider; never uses the network."""

    enabled = True

    def __init__(self) -> None:
        self._values: dict[str, list[float]] = {}

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        self._values[bucket_id] = [float(len(content)), 1.0]
        return True

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        return self._values.get(bucket_id)

    async def search_similar(
        self,
        _query: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        del top_k
        return []

    def delete_embedding(self, bucket_id: str) -> None:
        self._values.pop(bucket_id, None)


class FakeDehydrator:
    """Fixed metadata and merge behavior; never uses an LLM."""

    async def analyze(self, _content: str) -> dict[str, Any]:
        return {
            "domain": ["m01"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
        }

    async def merge(self, old: str, new: str) -> str:
        if new.strip() and new.strip() not in old:
            return f"{old.rstrip()}\n\n---\n{new.strip()}"
        return old

    async def dehydrate(self, content: str, meta: Any = None) -> str:
        del meta
        return content

    async def judge_same_event(self, _old: str, _new: str) -> dict[str, Any]:
        return {"same_event": True, "confidence": 1.0, "reason": "test exact"}

    def invalidate_cache(self, _content: str) -> None:
        return None


def _prepare_runtime(vault: str, *, high_cap: int = 24, pinned_cap: int = 20):
    """Set the isolated vault before importing and configure a local runtime."""

    resolved_vault = str(Path(vault).resolve())
    os.environ["OMBRE_VAULT_DIR"] = resolved_vault
    os.environ["OMBRE_BUCKETS_DIR"] = resolved_vault
    os.environ["OMBRE_EMBED_API_KEY"] = "__m01_test_only__"
    os.environ["OMBRE_COMPRESS_API_KEY"] = "__m01_test_only__"

    repo_root = Path(__file__).resolve().parents[2]
    src_dir = str(repo_root / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    # These imports must remain below the environment assignments above.
    from bucket_manager import BucketManager
    from tools import _common as common
    from tools import _identity
    from tools import _runtime as rt

    for directory in (
        "permanent",
        "dynamic",
        "archive",
        "feel",
        "plans",
        "letters",
    ):
        (Path(resolved_vault) / directory).mkdir(parents=True, exist_ok=True)

    config = {
        "buckets_dir": resolved_vault,
        "merge_threshold": 75,
        "matching": {"fuzzy_threshold": 50, "max_results": 50},
        "wikilink": {"enabled": False},
        "limits": {"max_pinned": pinned_cap},
        "scoring_weights": {
            "topic_relevance": 4.0,
            "emotion_resonance": 2.0,
            "time_proximity": 1.5,
            "importance": 1.0,
            "content_weight": 1.0,
        },
        "dehydration": {"timeout_seconds": 1},
        "embedding": {"enabled": True},
    }
    embedding = FakeEmbedding()
    manager = BucketManager(config, embedding_engine=embedding)
    rt.config = config
    rt.bucket_mgr = manager
    rt.embedding_engine = embedding
    rt.dehydrator = FakeDehydrator()
    rt.logger = logging.getLogger(f"m01-worker-{os.getpid()}")
    rt.fire_webhook = None
    rt.mark_op = None
    _identity.set_caller("")

    # The selective backport keeps this philosophical limit as a module-level
    # constant.  Patching it in every spawned interpreter makes the race small.
    common._HIGH_IMP_HARD_CAP = high_cap
    common._HIGH_IMP_SOFT_WARN = high_cap
    return manager, common, rt


async def _run_write(
    vault: str,
    action: str,
    content: str,
    caller: str,
    high_cap: int,
    pinned_cap: int,
    ready,
    start,
    coordination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manager, common, _rt = _prepare_runtime(
        vault,
        high_cap=high_cap,
        pinned_cap=pinned_cap,
    )
    from tools import _identity

    _identity.set_caller(caller)

    # Deliberately retain an empty/old process-local snapshot.  The mutation
    # path must use authoritative disk scans after it acquires its turns.
    warmed = await manager.list_all(include_archive=False)
    ready.put({"kind": "ready", "pid": os.getpid(), "cached": len(warmed)})
    if not start.wait(timeout=20):
        raise TimeoutError("parent never released the M-01 worker start gate")

    first_review_outcome = ""
    if coordination is not None:
        original_select = common._select_merge_target
        original_review = common._create_after_merge_review
        first_selection = True
        first_review = True

        async def gated_select(*args, **kwargs):
            nonlocal first_selection
            selected = await original_select(*args, **kwargs)
            if first_selection:
                first_selection = False
                coordination["selection_observed"].set()
                if selected is not None:
                    coordination["first_review_finished"].set()
                    raise AssertionError(
                        "ordered race expected an empty first merge selection"
                    )
            return selected

        async def gated_review(**kwargs):
            nonlocal first_review, first_review_outcome
            if not first_review:
                return await original_review(**kwargs)
            first_review = False
            try:
                if not coordination["allow_first_review"].wait(timeout=20):
                    raise TimeoutError(
                        "parent never released the ordered first-review gate"
                    )
                reviewed = await original_review(**kwargs)
                first_review_outcome = (
                    "raced_exact" if reviewed is None else "created"
                )
                return reviewed
            finally:
                coordination["first_review_finished"].set()

        common._select_merge_target = gated_select
        common._create_after_merge_review = gated_review

    owner_tag = f"owner:{caller}"
    if action in {"merge", "merge_high"}:
        extra_tags = [owner_tag]
        importance = 5
        domain = ["m01"]
        if action == "merge_high":
            extra_tags.append("race:high")
            importance = 9
            domain = ["race:incoming"]
        bucket_id, merged, warning = await common.merge_or_create(
            content=content,
            tags=extra_tags,
            importance=importance,
            domain=domain,
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        )
        return {
            "kind": "result",
            "pid": os.getpid(),
            "action": action,
            "bucket_id": bucket_id,
            "merged": merged,
            "warning": warning,
            "first_review_outcome": first_review_outcome,
        }

    if action == "pinned":
        from tools.hold.pinned import store_pinned

        response = await store_pinned(
            content=content,
            extra_tags=[owner_tag],
            valence=0.5,
            arousal=0.3,
            why_remembered="",
        )
        return {"kind": "result", "pid": os.getpid(), "response": response}

    if action == "high":
        bucket_id, merged, warning = await common.merge_or_create(
            content=content,
            tags=[owner_tag],
            importance=9,
            domain=["m01"],
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        )
        return {
            "kind": "result",
            "pid": os.getpid(),
            "bucket_id": bucket_id,
            "merged": merged,
            "warning": warning,
        }

    raise ValueError(f"unknown M-01 worker action: {action}")


def run_write_worker(
    vault: str,
    action: str,
    content: str,
    caller: str,
    high_cap: int,
    pinned_cap: int,
    ready,
    start,
    results,
    coordination=None,
) -> None:
    """Spawn target for one coordinated durable write."""

    # Pytest's parent process captures output as UTF-8, while a Windows
    # ``spawn`` child can reopen the inherited handles with the active console
    # code page.  Any non-ASCII log line would then leave undecodable bytes in
    # the parent's capture file.  Pin the child streams before importing the
    # application modules that may configure logging.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")

    try:
        result = asyncio.run(
            _run_write(
                vault,
                action,
                content,
                caller,
                high_cap,
                pinned_cap,
                ready,
                start,
                coordination,
            )
        )
    except BaseException as exc:
        results.put(
            {
                "kind": "error",
                "pid": os.getpid(),
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }
        )
        raise
    else:
        results.put(result)


def exit_while_holding_worker(vault: str, key: str, entered) -> None:
    """Acquire the kernel lease and exit without running context cleanup."""

    os.environ["OMBRE_VAULT_DIR"] = str(Path(vault).resolve())
    os.environ["OMBRE_BUCKETS_DIR"] = str(Path(vault).resolve())
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root / "src"))

    async def _hold_and_exit() -> None:
        from bucket_manager import _filesystem_turn

        async with _filesystem_turn(vault, key):
            entered.set()
            os._exit(0)

    asyncio.run(_hold_and_exit())
