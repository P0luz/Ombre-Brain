"""`them` 的 SQLite 存储。

结构照 `you.store`，两处不同：

- 多一张 `persons` 表。人是 them 的分份单位，也是姓名命中的入口。
- 没有 `projections` 表。you 的投影是给"抽象语义提示"那层用的缓存，
  那一层在 3.4.x 已经删了；them 从一开始就直接查，不建一张只会
  过期的缓存表。

库和 you 分开（`.them/them.sqlite3`），不是为了隔离性能，是为了
「关掉 them 不影响 you」能在文件层面成立：两个模块各有各的总开关，
共用一个库的话，任何一次 schema 迁移都会同时动到另一个模块的数据。
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from ombrebrain.storage.module_sqlite import connect_module_db, module_integrity_report

import sqlite3
import tempfile
import threading
from typing import Any

from ..you.models import ModuleState, Scope, utc_now
from .models import Person, ThemClaim


class ThemStoreError(RuntimeError):
    pass


def validate_them_snapshot_file(path: str | os.PathLike[str]) -> None:
    """恢复一份 them 快照之前，先确认它确实是一份 them 库。

    与 `validate_you_snapshot_file` 同一套做法：只读打开、`quick_check`、
    把 schema 逐个对上，再把每条记录反序列化一遍并核对 scope。

    严格到「表和索引必须恰好是这几个」，是因为这条路径的输入来自
    备份文件与 GitHub 仓库——那是外部内容。一份带额外表或触发器的
    SQLite 文件放进 vault，就等于让别人往这台实例里塞了一段可执行的东西。
    """
    snapshot = Path(path)
    if not snapshot.is_file() or snapshot.stat().st_size <= 0:
        raise ThemStoreError("them snapshot is empty")
    try:
        connection = sqlite3.connect(
            f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=30
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise ThemStoreError("them snapshot integrity check failed")
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = {row["name"] for row in objects if row["type"] == "table"}
        indexes = {row["name"] for row in objects if row["type"] == "index"}
        others = [row for row in objects if row["type"] not in {"table", "index"}]
        if (
            tables != {"module_state", "persons", "claims"}
            or indexes != {"persons_scope", "claims_person_concept"}
            or others
        ):
            raise ThemStoreError("them snapshot schema is not allowed")

        state_rows = connection.execute(
            "SELECT enabled, scope_json, state_revision FROM module_state"
        ).fetchall()
        if len(state_rows) != 1:
            raise ThemStoreError("them snapshot state is invalid")
        row = state_rows[0]
        if row["enabled"] not in (0, 1) or int(row["state_revision"]) < 1:
            raise ThemStoreError("them snapshot state is invalid")
        scope_data = json.loads(row["scope_json"])
        if not isinstance(scope_data, dict):
            raise ThemStoreError("them snapshot scope is invalid")
        scope = Scope.from_dict(scope_data)

        for person_row in connection.execute(
            "SELECT scope_key, payload_json FROM persons"
        ):
            Person.from_dict(json.loads(person_row["payload_json"]))
            if person_row["scope_key"] != scope.key:
                raise ThemStoreError("them snapshot person scope mismatch")
        for claim_row in connection.execute(
            "SELECT scope_key, payload_json FROM claims"
        ):
            claim = ThemClaim.from_dict(json.loads(claim_row["payload_json"]))
            if claim_row["scope_key"] != scope.key or claim.scope != scope:
                raise ThemStoreError("them snapshot claim scope mismatch")
    except ThemStoreError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ThemStoreError("them snapshot is invalid") from exc
    finally:
        if "connection" in locals():
            connection.close()


def validate_them_snapshot_bytes(data: bytes) -> None:
    if not data:
        raise ThemStoreError("them snapshot is empty")
    descriptor, temp_path = tempfile.mkstemp(prefix="ombre-them-validate-", suffix=".db")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        validate_them_snapshot_file(temp_path)
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
CREATE TABLE IF NOT EXISTS persons (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    last_active TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS persons_scope ON persons(scope_key, last_active);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    scope_key TEXT NOT NULL,
    person_id TEXT NOT NULL,
    concept_key TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS claims_person_concept
    ON claims(scope_key, person_id, concept_key, lifecycle);
"""


class ThemStore:
    """them 内部状态的权威来源。

    库在第一次显式开启时才创建。默认关闭地跑起来，不会产生任何 them 文件，
    也不会改动配置——与 you 同一条规矩。
    """

    def __init__(self, buckets_dir: str | os.PathLike[str]) -> None:
        self.root = Path(buckets_dir).resolve() / ".them"
        self.path = self.root / "them.sqlite3"
        self._lock = threading.RLock()
        # (stat 戳记, 状态)。get_state 在每次浮现时都要问一次"them 开着吗"，
        # 模块默认关闭时这笔开销照付，纯属白给。理由同 you.store。
        self._state_cache: tuple[tuple[int, int], ModuleState] | None = None

    def invalidate_state_cache(self) -> None:
        with self._lock:
            self._state_cache = None

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        return connect_module_db(
            self.path, self.root, _SCHEMA,
            create=create,
            error_factory=ThemStoreError,
            unavailable_message="them state is unavailable",
        )

    def get_state(self) -> ModuleState:
        try:
            stat = self.path.stat()
        except OSError:
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
                # 戳记在读库之后重取：读的过程中若有写入落盘，缓存一个已经过期
                # 的状态比多读一次糟得多。
                try:
                    fresh = self.path.stat()
                except OSError:
                    self._state_cache = None
                else:
                    self._state_cache = (
                        (stamp, state)
                        if (fresh.st_mtime_ns, fresh.st_size) == stamp
                        else None
                    )
                return state
            except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._state_cache = None
                raise ThemStoreError("them state is unavailable") from exc

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
                        raise ThemStoreError("them scope is invalid")
                    scope = Scope.from_dict(scope_raw)
                if expected_revision is not None and int(expected_revision) != current_revision:
                    raise ThemStoreError("them state revision conflict")
                revision = current_revision + 1
                changed_at = utc_now()
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
                        scope.subject_user_id,
                    ),
                )
                connection.execute("COMMIT")
                self._state_cache = None
                return ModuleState(
                    bool(enabled), scope, revision, changed_at, scope.subject_user_id
                )
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    # --- persons ---

    def list_persons(self, scope: Scope) -> list[Person]:
        state = self.get_state()
        if not state.enabled or state.scope != scope:
            return []
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT payload_json FROM persons WHERE scope_key=? ORDER BY last_active DESC, id ASC",
                    (scope.key,),
                ).fetchall()
            finally:
                connection.close()
        persons: list[Person] = []
        for row in rows:
            try:
                persons.append(Person.from_dict(json.loads(row["payload_json"])))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ThemStoreError("them person record is invalid") from exc
        return persons

    def get_person(self, scope: Scope, person_id: str) -> Person | None:
        return next(
            (person for person in self.list_persons(scope) if person.id == person_id),
            None,
        )

    def find_person_by_name(self, scope: Scope, name: str) -> Person | None:
        """按名字找人：命中任一个登记的名字就算命中这个人。"""
        key = str(name or "").strip().casefold()
        if not key:
            return None
        return next(
            (person for person in self.list_persons(scope) if key in person.name_keys),
            None,
        )

    def put_person(self, scope: Scope, person: Person, *, expected_revision: int | None = None) -> Person:
        state = self.get_state()
        if not state.enabled or state.scope != scope:
            raise ThemStoreError("them is disabled or the scope does not match")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT payload_json FROM persons WHERE id=?", (person.id,)
                ).fetchone()
                current_revision = 0
                if row is not None:
                    current_revision = Person.from_dict(json.loads(row["payload_json"])).revision
                if expected_revision is not None and current_revision != expected_revision:
                    raise ThemStoreError("them person revision conflict")
                stored = replace(person, revision=max(1, current_revision + 1))
                connection.execute(
                    "INSERT INTO persons(id, scope_key, last_active, payload_json) "
                    "VALUES(?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "scope_key=excluded.scope_key, last_active=excluded.last_active, "
                    "payload_json=excluded.payload_json",
                    (
                        stored.id,
                        scope.key,
                        stored.last_active,
                        json.dumps(stored.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ),
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

    # --- claims ---

    def list_claims(
        self,
        scope: Scope,
        *,
        person_id: str = "",
        concept_key: str = "",
        callable_only: bool = False,
    ) -> list[ThemClaim]:
        state = self.get_state()
        if not state.enabled or state.scope != scope:
            return []
        with self._lock:
            connection = self._connect()
            try:
                sql = "SELECT payload_json FROM claims WHERE scope_key=?"
                params: list[Any] = [scope.key]
                if person_id:
                    sql += " AND person_id=?"
                    params.append(person_id)
                if concept_key:
                    sql += " AND concept_key=?"
                    params.append(str(concept_key).strip().lower())
                sql += " ORDER BY updated_at DESC, id ASC"
                rows = connection.execute(sql, params).fetchall()
            finally:
                connection.close()
        claims: list[ThemClaim] = []
        for row in rows:
            try:
                claim = ThemClaim.from_dict(json.loads(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ThemStoreError("them claim record is invalid") from exc
            if claim.scope != scope:
                raise ThemStoreError("them claim scope mismatch")
            if not callable_only or claim.callable_at():
                claims.append(claim)
        return claims

    def get_claim(self, scope: Scope, claim_id: str) -> ThemClaim | None:
        return next(
            (claim for claim in self.list_claims(scope) if claim.id == claim_id),
            None,
        )

    def put_claim(self, claim: ThemClaim, *, expected_revision: int | None = None) -> ThemClaim:
        state = self.get_state()
        if not state.enabled or state.scope != claim.scope:
            raise ThemStoreError("them is disabled or the scope does not match")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT payload_json FROM claims WHERE id=?", (claim.id,)
                ).fetchone()
                current_revision = 0
                if row is not None:
                    current_revision = ThemClaim.from_dict(
                        json.loads(row["payload_json"])
                    ).revision
                if expected_revision is not None and current_revision != expected_revision:
                    raise ThemStoreError("them claim revision conflict")
                stored = replace(
                    claim, revision=max(1, current_revision + 1), updated_at=utc_now()
                )
                connection.execute(
                    "INSERT INTO claims(id, scope_key, person_id, concept_key, lifecycle, updated_at, payload_json) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "scope_key=excluded.scope_key, person_id=excluded.person_id, "
                    "concept_key=excluded.concept_key, lifecycle=excluded.lifecycle, "
                    "updated_at=excluded.updated_at, payload_json=excluded.payload_json",
                    (
                        stored.id,
                        stored.scope.key,
                        stored.person_id,
                        stored.concept_key,
                        stored.lifecycle,
                        stored.updated_at,
                        json.dumps(stored.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ),
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

    def snapshot_to(self, target: str | os.PathLike[str]) -> bool:
        """导出一份库快照。

        ⚠️ **现在没有调用者。** `.you` 在三个地方被特殊处理——
        `backup_archive`（本地备份）、`github_sync`（远程同步）、
        `migrate_engine`（记忆包迁移）——them 一处都还没接上，
        所以 them 的数据目前不进备份、不进同步、不进迁移包。

        没有顺手接上去，是因为其中一条不是工程问题：them 记的是**第三方**，
        同步到 GitHub 意味着别人的信息进了远程仓库，这需要项目所有者明确决定，
        不该由「照着 you 抄一遍」带进去。

        接的时候还要补一个 `validate_them_snapshot_file`（照
        `validate_you_snapshot_file` 写），否则恢复路径没有结构校验。

        them 默认关闭、尚未上线，眼下没有会丢的存量数据；但这条缺口一旦
        上线就变成真实的数据丢失，别让它安静地留着。
        """
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
                    raise ThemStoreError("them snapshot integrity check failed")
            finally:
                destination.close()
                source.close()
        return True

    def integrity_report(self) -> dict[str, Any]:
        return module_integrity_report(
            self.path, self._lock, self._connect, ("persons", "claims"), self.get_state
        )
