"""Durable human deletion requests for formal memory buckets."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from bucket_manager import _filesystem_turn
from tools.plan.core import is_letter_bucket, letter_lock_state
from utils import atomic_write_text


class HumanDeleteExecutor:
    """Execute the deletion behavior exposed to a human through the web UI."""

    def __init__(self, bucket_mgr: Any, embedding_engine: Any):
        self.bucket_mgr = bucket_mgr
        self.embedding_engine = embedding_engine

    async def execute(
        self,
        bucket_id: str,
        *,
        action: str = "delete",
        is_letter: bool = False,
    ) -> dict:
        if action == "archive":
            archived = await self.bucket_mgr.archive(bucket_id)
            return (
                {"ok": True, "archived": True}
                if archived
                else {"ok": False, "error": "bucket archive failed"}
            )
        bucket = await self.bucket_mgr.get(bucket_id)
        archived = bool(bucket) and await self.bucket_mgr.delete(bucket_id)
        if bucket and not archived:
            return {"ok": False, "error": "bucket deletion failed"}

        if not is_letter:
            if not archived:
                return {"ok": False, "error": "bucket deletion failed"}
            return {"ok": True, "deleted": True}

        # Letters historically repair all derived layers even when their
        # Markdown has already disappeared. Keep that web/human behavior
        # distinct from MCP/AI deletion semantics.
        outbox = getattr(self.bucket_mgr, "embedding_outbox", None)
        if outbox is not None:
            try:
                outbox.discard(bucket_id)
            except Exception:
                pass
        if self.embedding_engine is not None:
            try:
                self.embedding_engine.delete_embedding(bucket_id)
            except Exception:
                pass
        invalidate = getattr(self.bucket_mgr, "_invalidate_bm25", None)
        if callable(invalidate):
            invalidate()
        return {
            "ok": True,
            "deleted": archived,
            "cleaned": True,
            "already_missing": not bool(bucket),
        }


class DeletionRequestStore:
    DAILY_LIMIT = 10
    LIFETIME_LIMIT = 5

    def __init__(self, buckets_dir: str, bucket_mgr: Any, embedding_engine: Any = None):
        self.path = Path(buckets_dir) / ".human_deletion_requests.json"
        self.base_dir = str(buckets_dir)
        self.bucket_mgr = bucket_mgr
        self.human_delete = HumanDeleteExecutor(bucket_mgr, embedding_engine)

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("requests"), list):
                return value
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "requests": []}

    def _save(self, state: dict) -> None:
        atomic_write_text(self.path, json.dumps(state, ensure_ascii=False, indent=2))

    @staticmethod
    def is_test_bucket(bucket: dict) -> bool:
        provenance = (bucket.get("metadata") or {}).get("provenance")
        return bool(
            isinstance(provenance, dict)
            and provenance.get("kind") == "test"
            and provenance.get("erasable") is True
        )

    def status(self, bucket_id: str) -> dict | None:
        return self.status_snapshot().get(bucket_id)

    def status_snapshot(self) -> dict[str, dict]:
        grouped: dict[str, list[dict]] = {}
        for record in self._load()["requests"]:
            grouped.setdefault(str(record.get("bucket_id") or ""), []).append(record)
        return {
            bucket_id: {
                "request_id": records[-1]["id"],
                "status": records[-1]["status"],
                "human_reason": records[-1].get("human_reason", ""),
                "ai_reason": records[-1].get("ai_reason", ""),
                "submitted_at": records[-1].get("submitted_at", ""),
                "lifetime_count": len(records),
            }
            for bucket_id, records in grouped.items()
        }

    @staticmethod
    def _is_active_target(bucket: dict | None) -> bool:
        if not bucket:
            return False
        meta = bucket.get("metadata") or {}
        return not meta.get("deleted_at") and str(meta.get("type") or "").lower() != "archived"

    async def submit(
        self,
        bucket_id: str,
        reason: str,
        *,
        action: str = "delete",
        is_letter: bool = False,
    ) -> dict:
        if action not in {"archive", "delete"}:
            return {"ok": False, "error": "action must be archive or delete", "code": "invalid_action"}
        reason = str(reason or "").strip()
        async with _filesystem_turn(self.base_dir, "human-deletion-requests"):
            bucket = await self.bucket_mgr.get(bucket_id)
            if not bucket:
                if is_letter and action == "delete":
                    return await self.human_delete.execute(
                        bucket_id, action=action, is_letter=True
                    )
                return {"ok": False, "error": "bucket not found", "code": "not_found"}
            is_letter = is_letter or is_letter_bucket(bucket)
            if self.is_test_bucket(bucket):
                result = await self.human_delete.execute(
                    bucket_id, action=action, is_letter=is_letter
                )
                result["exempt_test_data"] = True
                return result
            if not reason:
                return {"ok": False, "error": "deletion reason is required", "code": "reason_required"}
            state = self._load()
            records = state["requests"]
            related = [r for r in records if r.get("bucket_id") == bucket_id]
            if any(r.get("status") == "pending" for r in related):
                return {"ok": False, "error": "a deletion request is already pending", "code": "pending_exists"}
            if len(related) >= self.LIFETIME_LIMIT:
                return {"ok": False, "error": "bucket lifetime deletion request limit reached", "code": "lifetime_limit"}
            today = datetime.now().astimezone().date().isoformat()
            if sum(1 for r in records if r.get("local_date") == today) >= self.DAILY_LIMIT:
                return {"ok": False, "error": "daily deletion request limit reached", "code": "daily_limit"}
            now = datetime.now().astimezone().isoformat()
            record = {
                "id": uuid.uuid4().hex,
                "bucket_id": bucket_id,
                "status": "pending",
                "human_reason": reason,
                "ai_reason": "",
                "submitted_at": now,
                "local_date": today,
                "is_letter": is_letter,
                "action": action,
            }
            records.append(record)
            self._save(state)
            return {"ok": True, "pending": True, "request": self.status(bucket_id)}

    async def submit_batch(
        self, bucket_ids: list[str], reason: str, *, action: str = "delete"
    ) -> dict:
        """Submit unique human requests under one quota/accounting turn."""
        if action not in {"archive", "delete"}:
            return {"ok": False, "error": "action must be archive or delete", "code": "invalid_action"}
        reason = str(reason or "").strip()
        submitted: list[dict] = []
        refused: list[dict] = []
        missing: list[str] = []
        errors: list[dict] = []
        async with _filesystem_turn(self.base_dir, "human-deletion-requests"):
            state = self._load()
            records = state["requests"]
            today = datetime.now().astimezone().date().isoformat()
            daily_count = sum(1 for r in records if r.get("local_date") == today)
            changed = False

            for bucket_id in dict.fromkeys(bucket_ids):
                try:
                    bucket = await self.bucket_mgr.get(bucket_id)
                    if not bucket:
                        missing.append(bucket_id)
                        continue
                    if self.is_test_bucket(bucket):
                        result = await self.human_delete.execute(
                            bucket_id,
                            action=action,
                            is_letter=is_letter_bucket(bucket),
                        )
                        if result.get("ok"):
                            submitted.append({
                                "id": bucket_id,
                                "pending": False,
                                "exempt_test_data": True,
                            })
                        else:
                            errors.append({"id": bucket_id, "error": result.get("error", "archive failed")})
                        continue

                    if not reason:
                        refused.append({
                            "id": bucket_id,
                            "code": "reason_required",
                            "error": "deletion reason is required",
                        })
                        continue

                    related = [r for r in records if r.get("bucket_id") == bucket_id]
                    if any(r.get("status") == "pending" for r in related):
                        refused.append({"id": bucket_id, "code": "pending_exists", "error": "a deletion request is already pending"})
                        continue
                    if len(related) >= self.LIFETIME_LIMIT:
                        refused.append({"id": bucket_id, "code": "lifetime_limit", "error": "bucket lifetime deletion request limit reached"})
                        continue
                    if daily_count >= self.DAILY_LIMIT:
                        refused.append({"id": bucket_id, "code": "daily_limit", "error": "daily deletion request limit reached"})
                        continue

                    now = datetime.now().astimezone().isoformat()
                    record = {
                        "id": uuid.uuid4().hex,
                        "bucket_id": bucket_id,
                        "status": "pending",
                        "human_reason": reason,
                        "ai_reason": "",
                        "submitted_at": now,
                        "local_date": today,
                        "is_letter": is_letter_bucket(bucket),
                        "action": action,
                    }
                    records.append(record)
                    daily_count += 1
                    changed = True
                    submitted.append({
                        "id": bucket_id,
                        "pending": True,
                        "request_id": record["id"],
                    })
                except Exception as exc:
                    errors.append({"id": bucket_id, "error": str(exc)})

            if changed:
                self._save(state)
        return {
            "ok": not refused and not missing and not errors,
            "submitted": submitted,
            "refused": refused,
            "missing": missing,
            "errors": errors,
        }

    async def withdraw(self, bucket_id: str) -> dict:
        async with _filesystem_turn(self.base_dir, "human-deletion-requests"):
            state = self._load()
            pending = next((r for r in reversed(state["requests"]) if r.get("bucket_id") == bucket_id and r.get("status") == "pending"), None)
            if not pending:
                return {"ok": False, "error": "no pending deletion request", "code": "not_pending"}
            pending["status"] = "withdrawn"
            pending["decided_at"] = datetime.now().astimezone().isoformat()
            self._save(state)
            return {"ok": True, "withdrawn": True, "request": self.status(bucket_id)}

    def pending_with_buckets(self) -> list[dict]:
        result = []
        for record in self._load()["requests"]:
            if record.get("status") != "pending":
                continue
            result.append(dict(record))
        return result

    async def reconcile_pending(self) -> int:
        """Durably supersede requests whose target left the active bucket set."""
        async with _filesystem_turn(self.base_dir, "human-deletion-requests"):
            state = self._load()
            changed = 0
            now = datetime.now().astimezone().isoformat()
            for record in state["requests"]:
                if record.get("status") != "pending":
                    continue
                bucket = await self.bucket_mgr.get(str(record.get("bucket_id") or ""))
                if self._is_active_target(bucket):
                    continue
                record["status"] = "superseded"
                record["decided_at"] = now
                record["superseded_reason"] = "target is no longer active"
                changed += 1
            if changed:
                self._save(state)
            return changed

    async def decide(
        self,
        request_id: str,
        decision: str,
        ai_reason: str = "",
        *,
        expected_bucket_id: str = "",
    ) -> dict:
        decision = str(decision or "").strip().lower()
        if decision not in {"approve", "reject"}:
            return {"ok": False, "error": "decision must be approve or reject"}
        async with _filesystem_turn(self.base_dir, "human-deletion-requests"):
            state = self._load()
            record = next((r for r in state["requests"] if r.get("id") == request_id), None)
            if not record or record.get("status") != "pending":
                return {"ok": False, "error": "pending request not found"}
            if expected_bucket_id and str(expected_bucket_id) != str(record.get("bucket_id") or ""):
                return {
                    "ok": False,
                    "error": "deletion request does not match bucket_id",
                    "code": "bucket_mismatch",
                }
            if not self._is_active_target(await self.bucket_mgr.get(str(record["bucket_id"]))):
                record["status"] = "superseded"
                record["decided_at"] = datetime.now().astimezone().isoformat()
                record["superseded_reason"] = "target is no longer active"
                self._save(state)
                return {"ok": False, "error": "deletion request target is no longer active"}
            if decision == "approve":
                result = await self.human_delete.execute(
                    str(record["bucket_id"]),
                    action=str(record.get("action") or "delete"),
                    is_letter=record.get("is_letter") is True,
                )
                if not result.get("ok"):
                    return {"ok": False, "error": "bucket deletion failed; request remains pending"}
                record["status"] = "approved"
            else:
                record["status"] = "rejected"
            record["ai_reason"] = str(ai_reason or "").strip()
            record["decided_at"] = datetime.now().astimezone().isoformat()
            self._save(state)
            return {"ok": True, "decision": decision, "bucket_id": record["bucket_id"]}

    async def render_pending_batch(self) -> str:
        await self.reconcile_pending()
        items = []
        for record in self.pending_with_buckets():
            bucket = await self.bucket_mgr.get(str(record["bucket_id"]))
            if bucket:
                items.append((record, bucket))
        if not items:
            return ""
        lines = [
            "=== Pending human deletion requests (decide each independently) ===",
            "Think carefully whether you truly no longer need each memory: is it wrong or inaccurate, no longer meaningful, or merely uncomfortable? Decide whether the human's stated reason genuinely persuades you. Do not approve merely because a human asked.",
            "For each explicit decision, call trace(bucket_id=..., deletion_request_id=..., deletion_decision=\"approve\" or \"reject\", deletion_ai_reason=...). If you make no explicit valid decision for an item, it remains pending and will appear on a later breath.",
        ]
        for record, bucket in items:
            meta = bucket.get("metadata") or {}
            header = (
                f"\n[request_id:{record['id']}] [bucket_id:{record['bucket_id']}] "
                f"[type:{meta.get('type', 'dynamic')}] "
                f"[requested_action:{record.get('action') or 'delete'}]\n"
                f"Human reason: {record['human_reason']}\n"
            )
            if is_letter_bucket(bucket):
                lock_state = letter_lock_state(bucket, "ai")
                if lock_state["locked"]:
                    lines.append(
                        header
                        + "Letter content: [LOCKED — unreadable from the AI side until unlock]\n"
                        + f"Letter lock metadata: locked=true; lock_type={lock_state['lock_type']}; "
                        + f"unlock_date={lock_state['unlock_date'] or ''}"
                    )
                    continue
            lines.append(header + f"Bucket content:\n{bucket.get('content', '')}")
        return "\n".join(lines)
