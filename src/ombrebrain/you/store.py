from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from typing import Any, Callable

from .models import ModuleState, Scope, YouClaim, utc_now


class YouStoreError(RuntimeError):
    pass


def validate_you_snapshot_file(path: str | os.PathLike[str]) -> None:
    """Validate a read-only You snapshot before publishing it into a vault."""

    snapshot = Path(path)
    if not snapshot.is_file() or snapshot.stat().st_size <= 0:
        raise YouStoreError("You snapshot is empty")
    try:
        connection = sqlite3.connect(
            f"file:{snapshot.as_posix()}?mode=ro",
            uri=True,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise YouStoreError("You snapshot integrity check failed")
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = {row["name"] for row in objects if row["type"] == "table"}
        indexes = {row["name"] for row in objects if row["type"] == "index"}
        other_objects = [
            row for row in objects if row["type"] not in {"table", "index"}
        ]
        required = {"module_state", "claims", "projections"}
        # outbox 是自动派生时代的遗留表：那时每次桶变动都要入队等后台处理。
        # 现在 you 由模型显式写入，没有队列可言，新库不再建这张表。但升级前
        # 导出的快照里还有它，所以允许存在、不要求存在——两边的备份都得能恢复。
        legacy = {"outbox"}
        if (
            not required.issubset(tables)
            or not tables.issubset(required | legacy)
            or indexes != {"claims_scope_concept"}
            or other_objects
        ):
            raise YouStoreError("You snapshot schema is not allowed")

        state_rows = connection.execute(
            "SELECT enabled, scope_json, state_revision FROM module_state"
        ).fetchall()
        if len(state_rows) != 1:
            raise YouStoreError("You snapshot state is invalid")
        state_row = state_rows[0]
        if state_row["enabled"] not in (0, 1) or int(state_row["state_revision"]) < 1:
            raise YouStoreError("You snapshot state is invalid")
        scope_data = json.loads(state_row["scope_json"])
        if not isinstance(scope_data, dict):
            raise YouStoreError("You snapshot scope is invalid")
        scope = Scope.from_dict(scope_data)

        for row in connection.execute("SELECT scope_key, payload_json FROM claims"):
            payload = json.loads(row["payload_json"])
            claim = YouClaim.from_dict(payload)
            if row["scope_key"] != scope.key or claim.scope != scope:
                raise YouStoreError("You snapshot claim scope mismatch")
        for row in connection.execute("SELECT scope_key, payload_json FROM projections"):
            payload = json.loads(row["payload_json"])
            if row["scope_key"] != scope.key or not isinstance(payload, dict):
                raise YouStoreError("You snapshot projection is invalid")
        # 旧快照里的 outbox 内容不再校验：那张表已经没有消费者，里面残留的
        # 待处理事件在恢复后也不会被执行。
    except YouStoreError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise YouStoreError("You snapshot is invalid") from exc
    finally:
        if "connection" in locals():
            connection.close()


def validate_you_snapshot_bytes(data: bytes) -> None:
    if not data:
        raise YouStoreError("You snapshot is empty")
    descriptor, temp_path = tempfile.mkstemp(prefix="ombre-you-validate-", suffix=".db")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        validate_you_snapshot_file(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS module_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    scope_json TEXT NOT NULL,
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    concept_key TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_scope_concept
    ON claims(scope_key, concept_key, lifecycle);
CREATE TABLE IF NOT EXISTS projections (
    scope_key TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    stale INTEGER NOT NULL CHECK (stale IN (0, 1)),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class YouStore:
    """SQLite-backed authority for the internal You derived state.

    The database is created lazily on the first explicit enable. Merely starting
    Ombre with the default-off feature does not create You files or mutate config.
    """

    def __init__(self, buckets_dir: str | os.PathLike[str]) -> None:
        self.root = Path(buckets_dir).resolve() / ".you"
        self.path = self.root / "you.sqlite3"
        self._lock = threading.RLock()
        # (stat 戳记, 状态)。get_state 在每次桶变化时都被调用一次，原来每次都
        # 开一条 SQLite 连接、查一行、再关掉——模块默认关闭时这笔开销照付，
        # 纯属白给。改成先 stat 比戳记：没变就直接用缓存。
        self._state_cache: tuple[tuple[int, int], ModuleState] | None = None

    def invalidate_state_cache(self) -> None:
        """丢掉状态缓存。备份恢复整库替换后必须调一次。"""
        with self._lock:
            self._state_cache = None

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        if not self.path.exists() and not create:
            raise FileNotFoundError(self.path)
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            if create:
                connection.executescript(_SCHEMA)
            return connection
        except sqlite3.Error as exc:
            raise YouStoreError("You state is unavailable") from exc

    def get_state(self) -> ModuleState:
        try:
            stat = self.path.stat()
        except OSError:
            # 库还没建（默认关闭的常态）：一次 stat 就够，不碰 SQLite。
            self._state_cache = None
            return ModuleState.disabled()
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._state_cache
        if cached is not None and cached[0] == stamp:
            return cached[1]
        with self._lock:
            try:
                connection = self._connect()
                try:
                    row = connection.execute(
                        "SELECT enabled, scope_json, state_revision, changed_at, changed_by "
                        "FROM module_state WHERE singleton=1"
                    ).fetchone()
                finally:
                    connection.close()
                if row is None:
                    state = ModuleState.disabled()
                else:
                    scope_raw = json.loads(row["scope_json"])
                    if not isinstance(scope_raw, dict):
                        raise ValueError("invalid scope")
                    state = ModuleState(
                        enabled=bool(row["enabled"]),
                        scope=Scope.from_dict(scope_raw),
                        state_revision=row["state_revision"],
                        changed_at=row["changed_at"],
                        changed_by=row["changed_by"],
                    )
                # 戳记在读库之后重新取：读的过程中若有别的写入落盘，这里拿到的
                # 新戳记会和刚读到的内容对不上，宁可下次再读一遍，也不缓存一个
                # 可能已经过期的状态。
                try:
                    fresh = self.path.stat()
                except OSError:
                    self._state_cache = None
                else:
                    if (fresh.st_mtime_ns, fresh.st_size) == stamp:
                        self._state_cache = (stamp, state)
                    else:
                        self._state_cache = None
                return state
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._state_cache = None
                raise YouStoreError("You state is unavailable") from exc

    def set_enabled(
        self,
        enabled: bool,
        *,
        expected_revision: int | None = None,
    ) -> ModuleState:
        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT enabled, scope_json, state_revision FROM module_state WHERE singleton=1"
                ).fetchone()
                if row is None:
                    current_revision = 0
                    scope = Scope.new()
                else:
                    current_revision = int(row["state_revision"])
                    scope_raw = json.loads(row["scope_json"])
                    if not isinstance(scope_raw, dict):
                        raise YouStoreError("You scope is invalid")
                    scope = Scope.from_dict(scope_raw)
                if expected_revision is not None and int(expected_revision) != current_revision:
                    raise YouStoreError("You state revision conflict")
                revision = current_revision + 1
                changed_at = utc_now()
                changed_by = scope.subject_user_id
                connection.execute(
                    "INSERT INTO module_state(singleton, enabled, scope_json, state_revision, changed_at, changed_by) "
                    "VALUES(1, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET enabled=excluded.enabled, "
                    "scope_json=excluded.scope_json, state_revision=excluded.state_revision, "
                    "changed_at=excluded.changed_at, changed_by=excluded.changed_by",
                    (
                        int(bool(enabled)),
                        json.dumps(scope.to_dict(), ensure_ascii=False, sort_keys=True),
                        revision,
                        changed_at,
                        changed_by,
                    ),
                )
                connection.execute("COMMIT")
                # 写盘会改 mtime，戳记本来就会自动失效；这里再显式清一次，
                # 免得依赖文件系统时间戳的精度。
                self._state_cache = None
                return ModuleState(bool(enabled), scope, revision, changed_at, changed_by)
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def list_claims(
        self,
        scope: Scope,
        *,
        concept_key: str = "",
        callable_only: bool = False,
    ) -> list[YouClaim]:
        state = self.get_state()
        if not state.enabled or state.scope != scope:
            return []
        with self._lock:
            connection = self._connect()
            try:
                sql = "SELECT payload_json FROM claims WHERE scope_key=?"
                params: list[Any] = [scope.key]
                if concept_key:
                    sql += " AND concept_key=?"
                    params.append(str(concept_key).strip().lower())
                sql += " ORDER BY updated_at DESC, id ASC"
                rows = connection.execute(sql, params).fetchall()
            finally:
                connection.close()
        claims: list[YouClaim] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                claim = YouClaim.from_dict(payload)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise YouStoreError("You claim record is invalid") from exc
            if claim.scope != scope:
                raise YouStoreError("You claim scope mismatch")
            if not callable_only or claim.callable_at():
                claims.append(claim)
        return claims

    def get_claim(self, scope: Scope, claim_id: str) -> YouClaim | None:
        claims = [claim for claim in self.list_claims(scope) if claim.id == claim_id]
        return claims[0] if claims else None

    def put_claim(self, claim: YouClaim, *, expected_revision: int | None = None) -> YouClaim:
        state = self.get_state()
        if not state.enabled or state.scope != claim.scope:
            raise YouStoreError("You is disabled or the scope does not match")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT payload_json FROM claims WHERE id=?", (claim.id,)
                ).fetchone()
                current_revision = 0
                if row is not None:
                    current_revision = YouClaim.from_dict(
                        json.loads(row["payload_json"])
                    ).revision
                if expected_revision is not None and current_revision != expected_revision:
                    raise YouStoreError("You claim revision conflict")
                next_revision = max(1, current_revision + 1)
                stored = replace(claim, revision=next_revision, updated_at=utc_now())
                connection.execute(
                    "INSERT INTO claims(id, scope_key, concept_key, lifecycle, updated_at, payload_json) "
                    "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "scope_key=excluded.scope_key, concept_key=excluded.concept_key, "
                    "lifecycle=excluded.lifecycle, updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                    (
                        stored.id,
                        stored.scope.key,
                        stored.concept_key,
                        stored.lifecycle,
                        stored.updated_at,
                        json.dumps(stored.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ),
                )
                connection.execute(
                    "INSERT INTO projections(scope_key, revision, stale, payload_json, updated_at) "
                    "VALUES(?, 0, 1, '{}', ?) ON CONFLICT(scope_key) DO UPDATE SET stale=1, updated_at=excluded.updated_at",
                    (stored.scope.key, utc_now()),
                )
                connection.execute("COMMIT")
                return stored
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def mutate_claims_for_bucket(
        self,
        scope: Scope,
        bucket_id: str,
        mutation: Callable[[YouClaim], YouClaim],
    ) -> int:
        changed = 0
        for claim in self.list_claims(scope):
            if not any(edge.bucket_id == bucket_id for edge in claim.evidence):
                continue
            updated = mutation(claim)
            self.put_claim(updated, expected_revision=claim.revision)
            changed += 1
        return changed

    def put_projection(self, scope: Scope, revision: int, payload: dict[str, Any]) -> None:
        state = self.get_state()
        if not state.enabled or state.scope != scope:
            raise YouStoreError("You is disabled or the scope does not match")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO projections(scope_key, revision, stale, payload_json, updated_at) "
                    "VALUES(?, ?, 0, ?, ?) ON CONFLICT(scope_key) DO UPDATE SET "
                    "revision=excluded.revision, stale=0, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
                    (scope.key, int(revision), encoded, utc_now()),
                )
            finally:
                connection.close()

    def get_projection(self, scope: Scope) -> dict[str, Any] | None:
        state = self.get_state()
        if not state.enabled or state.scope != scope:
            return None
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT revision, stale, payload_json FROM projections WHERE scope_key=?",
                    (scope.key,),
                ).fetchone()
            finally:
                connection.close()
        if row is None or bool(row["stale"]):
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError as exc:
            raise YouStoreError("You projection is invalid") from exc
        return payload if isinstance(payload, dict) else None

    def snapshot_to(self, target: str | os.PathLike[str]) -> bool:
        if not self.path.exists():
            return False
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            source = self._connect()
            destination = sqlite3.connect(str(target_path))
            try:
                source.backup(destination)
                result = destination.execute("PRAGMA quick_check").fetchone()
                if not result or str(result[0]).lower() != "ok":
                    raise YouStoreError("You snapshot integrity check failed")
            finally:
                destination.close()
                source.close()
        return True

    def integrity_report(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"ok": True, "exists": False, "enabled": False}
        try:
            with self._lock:
                connection = self._connect()
                try:
                    check = connection.execute("PRAGMA quick_check").fetchone()
                    # 表名来自下面这个**字面量元组**，不接受任何外部输入，
                    # 没有注入面。改动这里之前先确认这一点仍然成立——
                    # 一旦表名变成参数，这行 nosec 就会掩盖一个真的注入口。
                    counts = {
                        table: int(
                            connection.execute(
                                f"SELECT COUNT(*) FROM {table}"  # nosec B608
                            ).fetchone()[0]
                        )
                        for table in ("claims", "projections")
                    }
                finally:
                    connection.close()
            state = self.get_state()
            return {
                "ok": bool(check and str(check[0]).lower() == "ok"),
                "exists": True,
                "enabled": state.enabled,
                "state_revision": state.state_revision,
                "counts": counts,
            }
        except Exception:
            return {"ok": False, "exists": True, "enabled": False}
