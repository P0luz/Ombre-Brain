"""Just Now Context v1 ephemeral store.

This store is deliberately independent from Ombre Brain buckets. It stores a
small, expiring message ledger keyed by caller/source/task. It does not call an
LLM, create embeddings, or promote anything to long-term memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any, Callable
from uuid import uuid4


SCHEMA = "ombre-just-now-v1"
KNOWN_CALLERS = frozenset({"cheng", "huaiyin", "huaiyin_cc"})
ALLOWED_ROLES = frozenset({"user", "assistant"})
_MAX_KEY_CHARS = 240
_MAX_CURSOR_CHARS = 512
_MAX_EVENT_ID_CHARS = 200
_FUTURE_SKEW = timedelta(minutes=5)


class JustNowError(Exception):
    """Base error for the prototype."""


class JustNowValidationError(JustNowError):
    """Input failed a contract check."""


class JustNowConflict(JustNowError):
    """An idempotency key/cursor was reused with different content."""


class JustNowCorruptStore(JustNowError):
    """The on-disk ledger is malformed and must not be overwritten silently."""


@dataclass(frozen=True)
class JustNowLimits:
    ttl_seconds: int = 6 * 60 * 60
    max_items_per_stream: int = 40
    max_content_chars: int = 4000
    max_read_items: int = 40

    def __post_init__(self) -> None:
        if not 60 <= self.ttl_seconds <= 24 * 60 * 60:
            raise JustNowValidationError("ttl_seconds must be between 60 and 86400")
        if not 1 <= self.max_items_per_stream <= 200:
            raise JustNowValidationError(
                "max_items_per_stream must be between 1 and 200"
            )
        if not 1 <= self.max_content_chars <= 20000:
            raise JustNowValidationError(
                "max_content_chars must be between 1 and 20000"
            )
        if not 1 <= self.max_read_items <= self.max_items_per_stream:
            raise JustNowValidationError(
                "max_read_items must be positive and not exceed stream capacity"
            )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None, *, field: str) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise JustNowValidationError(f"{field} must be ISO-8601") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise JustNowValidationError(f"{field} must be datetime or ISO-8601")
    if parsed.tzinfo is None:
        raise JustNowValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _validate_key(value: Any, *, field: str, max_chars: int = _MAX_KEY_CHARS) -> str:
    text = str(value or "").strip()
    if not text:
        raise JustNowValidationError(f"{field} is required")
    if len(text) > max_chars:
        raise JustNowValidationError(f"{field} exceeds {max_chars} characters")
    if any(ord(char) < 32 for char in text):
        raise JustNowValidationError(f"{field} contains control characters")
    return text


def _stream_key(caller: str, source: str, task_id: str) -> str:
    # JSON array is unambiguous even when values contain punctuation.
    return json.dumps(
        [caller, source, task_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class JustNowLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        limits: JustNowLimits | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.limits = limits or JustNowLimits()
        self._now_fn = now_fn or _utc_now
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        value = self._now_fn()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise JustNowValidationError("now_fn must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _empty_state(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "next_seq": 1, "streams": {}}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()
        try:
            raw = self.path.read_text(encoding="utf-8")
            state = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JustNowCorruptStore(
                f"cannot read valid Just Now store: {self.path}"
            ) from exc
        if not isinstance(state, dict) or state.get("schema") != SCHEMA:
            raise JustNowCorruptStore("unsupported or missing Just Now schema")
        if not isinstance(state.get("next_seq"), int) or state["next_seq"] < 1:
            raise JustNowCorruptStore("invalid next_seq")
        if not isinstance(state.get("streams"), dict):
            raise JustNowCorruptStore("invalid streams object")
        max_seq = 0
        for key, stream in state["streams"].items():
            if not isinstance(key, str) or not isinstance(stream, dict):
                raise JustNowCorruptStore("invalid stream entry")
            caller = stream.get("caller")
            source = stream.get("source")
            task_id = stream.get("task_id")
            if (
                caller not in KNOWN_CALLERS
                or not isinstance(source, str)
                or not source
                or not isinstance(task_id, str)
                or not task_id
                or key != _stream_key(caller, source, task_id)
            ):
                raise JustNowCorruptStore("stream identity does not match its key")
            if not isinstance(stream.get("items"), list):
                raise JustNowCorruptStore("invalid stream items")
            for item in stream["items"]:
                if not isinstance(item, dict):
                    raise JustNowCorruptStore("invalid item entry")
                if item.get("role") not in ALLOWED_ROLES:
                    raise JustNowCorruptStore("invalid item role")
                if not isinstance(item.get("content"), str):
                    raise JustNowCorruptStore("invalid item content")
                seq = item.get("seq")
                if not isinstance(seq, int) or seq < 1:
                    raise JustNowCorruptStore("invalid item seq")
                max_seq = max(max_seq, seq)
        if state["next_seq"] <= max_seq:
            raise JustNowCorruptStore("next_seq does not advance past stored items")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_stream(
        self,
        caller: Any,
        source: Any,
        task_id: Any,
    ) -> tuple[str, str, str, str]:
        normalized_caller = str(caller or "").strip().lower().replace("-", "_")
        if normalized_caller not in KNOWN_CALLERS:
            raise JustNowValidationError("caller is missing or unknown")
        normalized_source = _validate_key(source, field="source")
        normalized_task = _validate_key(task_id, field="task_id")
        key = _stream_key(
            normalized_caller,
            normalized_source,
            normalized_task,
        )
        return normalized_caller, normalized_source, normalized_task, key

    def _new_stream(self, caller: str, source: str, task_id: str) -> dict[str, Any]:
        return {
            "caller": caller,
            "source": source,
            "task_id": task_id,
            "last_cursor": "",
            "cursor_updated_at": "",
            "cursor_expires_at": "",
            "items": [],
        }

    def _prune_state(
        self,
        state: dict[str, Any],
        now: datetime,
    ) -> tuple[int, bool]:
        removed = 0
        changed = False
        empty_keys: list[str] = []
        for key, stream in state["streams"].items():
            kept = []
            for item in stream["items"]:
                try:
                    expires_at = _as_utc(
                        item.get("expires_at"),
                        field="expires_at",
                    )
                except JustNowValidationError as exc:
                    raise JustNowCorruptStore("invalid item expiry") from exc
                if expires_at <= now:
                    removed += 1
                    changed = True
                else:
                    kept.append(item)
            stream["items"] = kept

            cursor_expiry = stream.get("cursor_expires_at") or ""
            if cursor_expiry:
                try:
                    cursor_expired = (
                        _as_utc(cursor_expiry, field="cursor_expires_at") <= now
                    )
                except JustNowValidationError as exc:
                    raise JustNowCorruptStore("invalid cursor expiry") from exc
                if cursor_expired:
                    stream["last_cursor"] = ""
                    stream["cursor_updated_at"] = ""
                    stream["cursor_expires_at"] = ""
                    changed = True

            if not stream["items"] and not stream.get("last_cursor"):
                empty_keys.append(key)

        for key in empty_keys:
            del state["streams"][key]
            changed = True
        return removed, changed

    @staticmethod
    def _equivalent(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
        # A source-cursor retry can arrive later without reproducing the exact
        # transport timestamp. Same role/content/cursor is the same event.
        keys = ("role", "content", "source_cursor")
        return all(existing.get(key) == incoming.get(key) for key in keys)

    def append(
        self,
        *,
        caller: str,
        source: str,
        task_id: str,
        role: str,
        content: str,
        occurred_at: datetime | str | None = None,
        source_cursor: str = "",
        event_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        normalized_caller, normalized_source, normalized_task, key = (
            self._validate_stream(caller, source, task_id)
        )
        normalized_role = str(role or "").strip().lower()
        if normalized_role not in ALLOWED_ROLES:
            raise JustNowValidationError("role must be user or assistant")
        if not isinstance(content, str) or not content.strip():
            raise JustNowValidationError("content must be a non-empty string")
        if len(content) > self.limits.max_content_chars:
            raise JustNowValidationError(
                f"content exceeds {self.limits.max_content_chars} characters"
            )
        if occurred_at is None:
            raise JustNowValidationError("occurred_at is required")
        normalized_cursor = str(source_cursor or "").strip()
        if normalized_cursor:
            normalized_cursor = _validate_key(
                normalized_cursor,
                field="source_cursor",
                max_chars=_MAX_CURSOR_CHARS,
            )
        supplied_event_id = str(event_id or "").strip()
        if supplied_event_id:
            normalized_event_id = _validate_key(
                supplied_event_id,
                field="event_id",
                max_chars=_MAX_EVENT_ID_CHARS,
            )
        else:
            normalized_event_id = uuid4().hex
        if not supplied_event_id and not normalized_cursor:
            raise JustNowValidationError(
                "event_id or source_cursor is required for idempotency"
            )
        normalized_session = str(session_id or "").strip()
        if normalized_session:
            normalized_session = _validate_key(
                normalized_session,
                field="session_id",
            )

        with self._lock:
            now = self._now()
            occurred = _as_utc(occurred_at, field="occurred_at")
            if occurred > now + _FUTURE_SKEW:
                raise JustNowValidationError("occurred_at is too far in the future")
            expires = occurred + timedelta(seconds=self.limits.ttl_seconds)
            state = self._load()
            _, changed = self._prune_state(state, now)
            stream = state["streams"].setdefault(
                key,
                self._new_stream(
                    normalized_caller,
                    normalized_source,
                    normalized_task,
                ),
            )
            incoming = {
                "event_id": normalized_event_id,
                "seq": state["next_seq"],
                "role": normalized_role,
                "content": content,
                "occurred_at": _iso(occurred),
                "recorded_at": _iso(now),
                "expires_at": _iso(expires),
                "source_cursor": normalized_cursor,
                "session_id": normalized_session,
            }

            for existing in stream["items"]:
                same_id = existing.get("event_id") == normalized_event_id
                same_cursor = bool(normalized_cursor) and (
                    existing.get("source_cursor") == normalized_cursor
                )
                if not same_id and not same_cursor:
                    continue
                if not self._equivalent(existing, incoming):
                    raise JustNowConflict(
                        "event_id/source_cursor was reused with different content"
                    )
                if changed:
                    self._save(state)
                return {
                    "stored": True,
                    "deduped": True,
                    "expired": False,
                    "seq": existing["seq"],
                    "event_id": existing["event_id"],
                }

            if expires <= now:
                if changed:
                    self._save(state)
                return {
                    "stored": False,
                    "deduped": False,
                    "expired": True,
                    "seq": None,
                    "event_id": normalized_event_id,
                }

            stream["items"].append(incoming)
            state["next_seq"] += 1
            evicted = 0
            if len(stream["items"]) > self.limits.max_items_per_stream:
                evicted = len(stream["items"]) - self.limits.max_items_per_stream
                stream["items"] = stream["items"][-self.limits.max_items_per_stream :]
            self._save(state)
            return {
                "stored": True,
                "deduped": False,
                "expired": False,
                "seq": incoming["seq"],
                "event_id": normalized_event_id,
                "evicted": evicted,
            }

    def read(
        self,
        *,
        caller: str,
        source: str,
        task_id: str,
        limit: int | None = None,
        after_seq: int = 0,
    ) -> dict[str, Any]:
        normalized_caller, normalized_source, normalized_task, key = (
            self._validate_stream(caller, source, task_id)
        )
        if not isinstance(after_seq, int) or after_seq < 0:
            raise JustNowValidationError("after_seq must be a non-negative integer")
        if limit is None:
            normalized_limit = self.limits.max_read_items
        else:
            if not isinstance(limit, int) or not 1 <= limit <= self.limits.max_read_items:
                raise JustNowValidationError(
                    f"limit must be between 1 and {self.limits.max_read_items}"
                )
            normalized_limit = limit

        with self._lock:
            now = self._now()
            state = self._load()
            _, changed = self._prune_state(state, now)
            stream = state["streams"].get(key)
            if not stream:
                if changed:
                    self._save(state)
                return {
                    "schema": SCHEMA,
                    "caller": normalized_caller,
                    "source": normalized_source,
                    "task_id": normalized_task,
                    "items": [],
                    "last_cursor": "",
                    "next_after_seq": after_seq,
                }
            eligible = [
                dict(item)
                for item in stream["items"]
                if int(item.get("seq") or 0) > after_seq
            ]
            # First read wants the most recent context. Incremental reads must
            # page forward from after_seq without skipping unseen middle items.
            items = (
                eligible[:normalized_limit]
                if after_seq
                else eligible[-normalized_limit:]
            )
            if changed:
                self._save(state)
            next_after = items[-1]["seq"] if items else after_seq
            return {
                "schema": SCHEMA,
                "caller": normalized_caller,
                "source": normalized_source,
                "task_id": normalized_task,
                "items": items,
                "last_cursor": stream.get("last_cursor") or "",
                "next_after_seq": next_after,
            }

    def ack_cursor(
        self,
        *,
        caller: str,
        source: str,
        task_id: str,
        cursor: str,
    ) -> dict[str, Any]:
        normalized_caller, normalized_source, normalized_task, key = (
            self._validate_stream(caller, source, task_id)
        )
        normalized_cursor = _validate_key(
            cursor,
            field="cursor",
            max_chars=_MAX_CURSOR_CHARS,
        )
        with self._lock:
            now = self._now()
            state = self._load()
            self._prune_state(state, now)
            stream = state["streams"].setdefault(
                key,
                self._new_stream(
                    normalized_caller,
                    normalized_source,
                    normalized_task,
                ),
            )
            stream["last_cursor"] = normalized_cursor
            stream["cursor_updated_at"] = _iso(now)
            stream["cursor_expires_at"] = _iso(
                now + timedelta(seconds=self.limits.ttl_seconds)
            )
            self._save(state)
            return {
                "caller": normalized_caller,
                "source": normalized_source,
                "task_id": normalized_task,
                "last_cursor": normalized_cursor,
                "cursor_expires_at": stream["cursor_expires_at"],
            }

    def clear_stream(
        self,
        *,
        caller: str,
        source: str,
        task_id: str,
    ) -> int:
        _, _, _, key = self._validate_stream(caller, source, task_id)
        with self._lock:
            state = self._load()
            stream = state["streams"].pop(key, None)
            if stream is None:
                return 0
            removed = len(stream.get("items") or [])
            self._save(state)
            return removed

    def prune(self) -> int:
        with self._lock:
            state = self._load()
            removed, changed = self._prune_state(state, self._now())
            if changed:
                self._save(state)
            return removed
