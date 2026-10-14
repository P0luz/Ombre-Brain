"""原文库：Claude Code 对话的逐轮原文（用户 + 助手），以及原文和桶的连线。

存在 ``<vault>/.autorecall/transcript.sqlite3``，点开头：不是 Markdown，
不进 list_all / 主 BM25 / vault_health；备份走模块快照通道。

- 每条原文用钩子给的消息 uuid 做幂等键：Stop 每轮存一次，SessionEnd 再整段补一遍，重复的自动跳过。
- 原文只增不改；桶删掉或归档只影响读，不删原文。
- 连线（原文 → 桶）不设门槛，把相似度分数一起存下，以后读的时候再按扫出来的门槛筛。
- 会话状态记三件事：项目目录 cwd（「上一段对话」只在同一个目录里找，两个窗口 / 两个项目不串）、
  上次 hold 提醒在第几轮（每满 N 轮提醒一次）、模块级的上次注入 dream 的日期（一天一次）。
- 每条原文记发生时间 ``at``（UTC）；读的时候按它排，关窗补存进来的漏网句子能回到原来的位置。

对外暴露：TranscriptStore / TranscriptStoreError / validate_transcript_snapshot_file。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ombrebrain.storage.module_sqlite import connect_module_db

MAX_TURN_CHARS = 4000
MAX_LINKS_PER_BUCKET = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, seq);
CREATE TABLE IF NOT EXISTS turn_bucket (
    turn_id TEXT NOT NULL REFERENCES turns(id),
    bucket_id TEXT NOT NULL,
    score REAL NOT NULL,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (turn_id, bucket_id)
);
CREATE INDEX IF NOT EXISTS turn_bucket_by_bucket ON turn_bucket(bucket_id);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL DEFAULT '',
    user_turns INTEGER NOT NULL DEFAULT 0,
    last_reminder_turn INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS module_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
_TABLES = {"turns", "turn_bucket", "sessions", "module_state"}
_INDEXES = {"turns_by_session", "turn_bucket_by_bucket"}


class TranscriptStoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc(value: object, default: str) -> str:
    """钩子给的时间（记录文件里是 ...Z，脚本里是 +00:00）统一成 UTC 秒级 ISO，读不懂就用 default。"""
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def turn_id(session_id: str, key: str, role: str, text: str) -> str:
    """有钩子给的消息 uuid 就用它；没有就退回内容哈希。"""
    basis = key or hashlib.sha256(text.encode("utf-8")).hexdigest()
    raw = f"{session_id}\x1f{role}\x1f{basis}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def validate_transcript_snapshot_file(path: str | os.PathLike[str]) -> None:
    """备份恢复前只读校验快照：只允许本模块的表和索引。"""
    snapshot = Path(path)
    if not snapshot.is_file() or snapshot.stat().st_size <= 0:
        raise TranscriptStoreError("transcript snapshot is empty")
    try:
        connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            check = connection.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise TranscriptStoreError("transcript snapshot integrity check failed")
            objects = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise TranscriptStoreError("transcript snapshot is unreadable") from exc
    tables = {name for kind, name in objects if kind == "table"}
    indexes = {name for kind, name in objects if kind == "index"}
    others = [name for kind, name in objects if kind not in {"table", "index"}]
    if tables != _TABLES or indexes != _INDEXES or others:
        raise TranscriptStoreError("transcript snapshot schema is not allowed")


def validate_transcript_snapshot_bytes(raw: bytes) -> None:
    """内存里的快照（小备份走内存路径时）：落一份临时文件再按同一套规则校验。"""
    import tempfile

    descriptor, temp_path = tempfile.mkstemp(prefix="ombre-autorecall-", suffix=".db")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
        validate_transcript_snapshot_file(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


class TranscriptStore:
    def __init__(self, buckets_dir: str | os.PathLike[str]) -> None:
        self.root = Path(buckets_dir).resolve() / ".autorecall"
        self.path = self.root / "transcript.sqlite3"
        self._lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        return connect_module_db(
            self.path, self.root, _SCHEMA,
            create=create,
            error_factory=TranscriptStoreError,
            unavailable_message="transcript store is unavailable",
        )

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.exists:
            return []
        with self._lock:
            connection = self._connect()
            try:
                return connection.execute(sql, params).fetchall()
            finally:
                connection.close()

    # ---------------- 写 ----------------

    def append(self, session_id: str, messages: list[dict], cwd: str = "") -> dict[str, int]:
        """按顺序写一批消息：``{"key": uuid, "role": "user"|"assistant", "text": str, "at"?: ISO}``。

        幂等：已存在的消息跳过，不占新序号。返回新增条数和本会话用户轮数。
        ``cwd`` 是这个会话所在的项目目录；第一次知道就记下，之后不改。
        """
        session = str(session_id or "").strip()
        if not session:
            raise TranscriptStoreError("session_id is required")
        now = _now()
        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT OR IGNORE INTO sessions(session_id, cwd, started_at, updated_at) VALUES (?, ?, ?, ?)",
                    (session, str(cwd or ""), now, now),
                )
                if cwd:
                    connection.execute(
                        "UPDATE sessions SET cwd = ? WHERE session_id = ? AND cwd = ''", (str(cwd), session)
                    )
                seq = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM turns WHERE session_id = ?", (session,)
                ).fetchone()[0]
                added = new_user = 0
                for message in messages:
                    role = str(message.get("role") or "")
                    text = str(message.get("text") or "").strip()
                    if role not in ("user", "assistant") or not text:
                        continue
                    truncated = len(text) > MAX_TURN_CHARS
                    text = text[:MAX_TURN_CHARS]
                    tid = turn_id(session, str(message.get("key") or ""), role, text)
                    if connection.execute("SELECT 1 FROM turns WHERE id = ?", (tid,)).fetchone():
                        continue
                    seq += 1
                    connection.execute(
                        "INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (tid, session, seq, role, text, int(truncated), _utc(message.get("at"), now), now),
                    )
                    added += 1
                    new_user += role == "user"
                connection.execute(
                    "UPDATE sessions SET user_turns = user_turns + ?, updated_at = ? WHERE session_id = ?",
                    (new_user, now, session),
                )
                user_turns = connection.execute(
                    "SELECT user_turns FROM sessions WHERE session_id = ?", (session,)
                ).fetchone()[0]
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise TranscriptStoreError("transcript write failed") from exc
            finally:
                connection.close()
        return {"added": added, "user_turns": int(user_turns)}

    def take_reminder(self, session_id: str, every: int) -> bool:
        """本会话自上次提醒以来满 ``every`` 轮用户发言，就记下这次提醒并返回 True。"""
        if every <= 0 or not self.exists:
            return False
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT user_turns, last_reminder_turn FROM sessions WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
                due = bool(row) and row["user_turns"] - row["last_reminder_turn"] >= every
                if due:
                    connection.execute(
                        "UPDATE sessions SET last_reminder_turn = user_turns WHERE session_id = ?",
                        (str(session_id),),
                    )
                connection.execute("COMMIT")
                return due
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise TranscriptStoreError("reminder update failed") from exc
            finally:
                connection.close()

    def claim_daily(self, key: str, today: str) -> bool:
        """``key`` 今天还没用过就记成 today 并返回 True（一天一次）。"""
        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT value FROM module_state WHERE key = ?", (key,)).fetchone()
                fresh = row is None or row["value"] != today
                if fresh:
                    connection.execute(
                        "INSERT INTO module_state VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, today),
                    )
                connection.execute("COMMIT")
                return fresh
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise TranscriptStoreError("module state update failed") from exc
            finally:
                connection.close()

    def daily_used(self, key: str, today: str) -> bool:
        rows = self._read("SELECT value FROM module_state WHERE key = ?", (key,))
        return bool(rows) and rows[0]["value"] == today

    def release_daily(self, key: str, today: str) -> None:
        """今天这次没用成（比如 dream 失败）就把额度还回去。"""
        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute("DELETE FROM module_state WHERE key = ? AND value = ?", (key, today))
            finally:
                connection.close()

    # ---------------- 钩子体检：最后到达时间、最近报错 ----------------

    def mark_seen(self, event: str) -> None:
        """记下某个钩子事件最后一次到达的时间（UTC）。面板据此判断哪一环断了。"""
        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute(
                    "INSERT INTO module_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (f"seen:{event}", _now()),
                )
            finally:
                connection.close()

    def seen(self) -> dict[str, str]:
        rows = self._read("SELECT key, value FROM module_state WHERE key LIKE 'seen:%'")
        return {row["key"][5:]: row["value"] for row in rows}

    def add_errors(self, errors: list[dict], keep: int = 20) -> None:
        """追加钩子报错（客户端补报的、服务端自己的），只留最近 ``keep`` 条。"""
        if not errors:
            return
        import json as _json

        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT value FROM module_state WHERE key = 'errors'").fetchone()
                try:
                    current = _json.loads(row["value"]) if row else []
                except ValueError:
                    current = []
                for item in errors:
                    current.append({
                        "at": _utc(item.get("at"), _now()),
                        "where": str(item.get("where") or "")[:40],
                        "message": str(item.get("message") or "")[:300],
                    })
                current = sorted(current, key=lambda e: e["at"])[-keep:]
                connection.execute(
                    "INSERT INTO module_state VALUES ('errors', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_json.dumps(current, ensure_ascii=False),),
                )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise TranscriptStoreError("error log write failed") from exc
            finally:
                connection.close()

    def recent_errors(self) -> list[dict]:
        import json as _json

        rows = self._read("SELECT value FROM module_state WHERE key = 'errors'")
        try:
            return _json.loads(rows[0]["value"]) if rows else []
        except ValueError:
            return []

    def link(self, links: list[tuple[str, str, float]]) -> int:
        """写连线 (turn_id, bucket_id, score)；同一对再连一次只更新分数。"""
        if not links:
            return 0
        now = _now()
        with self._lock:
            connection = self._connect(create=True)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT INTO turn_bucket VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(turn_id, bucket_id) DO UPDATE SET score = excluded.score",
                    [(t, b, float(s), now) for t, b, s in links],
                )
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                connection.execute("ROLLBACK")
                raise TranscriptStoreError("link write failed") from exc
            finally:
                connection.close()
        return len(links)

    # ---------------- 读 ----------------

    def session_turns(self, session_id: str, *, roles: tuple[str, ...] = ("user", "assistant")) -> list[dict]:
        rows = self._read(
            "SELECT id, seq, role, text, at, created_at FROM turns WHERE session_id = ? ORDER BY at, seq",
            (str(session_id),),
        )
        return [dict(r) for r in rows if r["role"] in roles]

    def session_started_at(self, session_id: str) -> str:
        rows = self._read("SELECT started_at FROM sessions WHERE session_id = ?", (str(session_id),))
        return rows[0]["started_at"] if rows else ""

    def last_turns(self, session_id: str, rounds: int) -> list[dict]:
        """某会话最后 ``rounds`` 轮（一轮 = 一条用户消息起，到下一条用户消息前）。"""
        turns = self.session_turns(session_id)
        starts = [i for i, t in enumerate(turns) if t["role"] == "user"]
        if not starts or rounds <= 0:
            return turns[-rounds * 2:] if rounds > 0 else []
        return turns[starts[max(0, len(starts) - rounds)]:]

    def latest_session(self, exclude: str = "", cwd: str = "") -> str:
        """同一个项目目录里、除当前会话外最近的一段对话。不知道 cwd 就不猜，返回空。"""
        if not cwd:
            return ""
        rows = self._read(
            "SELECT session_id FROM sessions WHERE session_id != ? AND cwd = ? AND user_turns > 0 "
            "ORDER BY updated_at DESC LIMIT 1",
            (str(exclude), str(cwd)),
        )
        return rows[0]["session_id"] if rows else ""

    def links_for_bucket(self, bucket_id: str) -> list[dict]:
        rows = self._read(
            "SELECT t.id, t.text, t.role, t.session_id, l.score FROM turn_bucket l "
            "JOIN turns t ON t.id = l.turn_id WHERE l.bucket_id = ? ORDER BY l.score DESC",
            (str(bucket_id),),
        )
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        if not self.exists:
            return {"turns": 0, "links": 0, "sessions": 0}
        return {
            "turns": self._read("SELECT COUNT(*) FROM turns")[0][0],
            "links": self._read("SELECT COUNT(*) FROM turn_bucket")[0][0],
            "sessions": self._read("SELECT COUNT(*) FROM sessions")[0][0],
        }
