from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator


LEDGER_SCHEMA_VERSION = 1
LEDGER_ROLE = "mirror"


class LedgerMirror:
    """Append-only JSONL mirror for successful memory mutations.

    Phase 1 mirror only: this is an audit/replay seed beside Markdown, not the
    canonical source of truth yet.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append_event(
        self,
        *,
        event_type: str,
        trace_id: str,
        trace_kind: str,
        payload: dict[str, Any] | None = None,
        body: str = "",
    ) -> dict[str, Any]:
        body_hash = _hash_body(body)
        event = {
            "seq": self.latest_seq() + 1,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_role": LEDGER_ROLE,
            "canonical": False,
            "event_type": str(event_type),
            "trace_id": str(trace_id),
            "trace_kind": str(trace_kind),
            "body_hash": body_hash,
            "payload": _json_safe(payload or {}),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_append_starts_on_new_line()
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            f.write("\n")
        return event

    def latest_seq(self) -> int:
        """最后一条事件的 seq。

        从文件尾往回读，而不是把整个 ledger 扫一遍。`append_event` 每次都要
        调它来定下一个 seq，全扫的话每次写入的代价随已有条数线性增长——实测
        空库写 200 条 0.28 秒，已有 1000 条时同样 200 条要 1.75 秒，而每次
        记忆的创建/更新/删除/归档都会写一条 ledger 事件。

        往回读而不是缓存在内存里：这个文件可能被外部改（手工、git、恢复），
        缓存会和磁盘脱节，而 seq 冲突是不可逆的。倒读只多读几 KB，代价固定。

        尾行被崩溃截断时继续往前找：坏的那一行跳过，前一条的 seq 仍然有效。
        """
        if not self.path.exists():
            return 0
        try:
            size = self.path.stat().st_size
        except OSError:
            return 0
        if size == 0:
            return 0
        window = 4096
        with self.path.open("rb") as handle:
            while True:
                start = max(0, size - window)
                handle.seek(start)
                chunk = handle.read(size - start)
                lines = chunk.split(b"\n")
                if start > 0:
                    # 第一段可能被窗口从中间切断，丢掉它，扩大窗口重新拿
                    lines = lines[1:]
                for line in reversed(lines):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text.decode("utf-8"))
                        return max(0, int(event.get("seq", 0)))
                    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
                        continue
                if start == 0:
                    return 0
                window *= 4

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def verify_integrity(self) -> dict[str, Any]:
        valid_events = 0
        invalid_lines: list[int] = []
        latest_seq = 0
        schema_versions: set[int] = set()
        if not self.path.exists():
            return {
                "ok": True,
                "path": str(self.path),
                "ledger_role": LEDGER_ROLE,
                "canonical": False,
                "valid_events": 0,
                "invalid_lines": [],
                "latest_seq": 0,
                "schema_versions": [],
            }

        with self.path.open("r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    invalid_lines.append(lineno)
                    continue
                valid_events += 1
                try:
                    latest_seq = max(latest_seq, int(event.get("seq", 0)))
                except (TypeError, ValueError):
                    invalid_lines.append(lineno)
                try:
                    schema_versions.add(int(event.get("schema_version")))
                except (TypeError, ValueError):
                    invalid_lines.append(lineno)

        return {
            "ok": not invalid_lines,
            "path": str(self.path),
            "ledger_role": LEDGER_ROLE,
            "canonical": False,
            "valid_events": valid_events,
            "invalid_lines": invalid_lines,
            "latest_seq": latest_seq,
            "schema_versions": sorted(schema_versions),
        }

    def _ensure_append_starts_on_new_line(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("rb") as f:
            f.seek(-1, 2)
            last_byte = f.read(1)
        if last_byte != b"\n":
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write("\n")


def _hash_body(body: str) -> str:
    digest = hashlib.sha256(str(body).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
