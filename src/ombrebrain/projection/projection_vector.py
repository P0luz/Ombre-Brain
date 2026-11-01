"""Read-only manifest for drift in the derived vector projection."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from ombrebrain.projection.projection_mirror import TraceCatalogProjection
from ombrebrain.storage.vector_codec import is_valid_stored_vector


class TraceVectorProjectionManifest:
    """Read-only shadow manifest for the existing embeddings SQLite store."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._report: dict[str, Any] | None = None

    def rebuild(self, events: Iterable[dict[str, Any]]) -> dict[str, Any]:
        catalog = TraceCatalogProjection()
        catalog.rebuild(events)
        expected_ids = {
            trace_id for trace_id, trace in catalog.traces.items()
            if _expects_vector(trace)
        }
        db_report = self._read_db()
        valid_ids = set(db_report["valid_vector_ids"])
        stored_ids = set(db_report["stored_vector_ids"])
        missing_ids = sorted(expected_ids - valid_ids)
        orphan_ids = sorted(stored_ids - expected_ids)
        source_latest_seq = catalog.applied_seq
        self._report = {
            "projection_name": "trace_vector_manifest",
            "projection_role": "shadow",
            "canonical": False,
            "path": str(self.db_path),
            "db_exists": db_report["db_exists"],
            "model_name": db_report["model_name"],
            "vector_dim": db_report["vector_dim"],
            "expected_trace_count": len(expected_ids),
            "vector_count": len(valid_ids),
            "stored_vector_count": len(stored_ids),
            "missing_vector_count": len(missing_ids),
            "orphan_vector_count": len(orphan_ids),
            "malformed_vector_count": len(db_report["malformed_vector_ids"]),
            "missing_vector_ids": missing_ids[:20],
            "orphan_vector_ids": orphan_ids[:20],
            "malformed_vector_ids": db_report["malformed_vector_ids"][:20],
            "applied_seq": catalog.applied_seq,
            "source_latest_seq": source_latest_seq,
            "lag": 0,
        }
        return dict(self._report)

    def to_report(self, *, source_latest_seq: int = 0) -> dict[str, Any]:
        if self._report is None:
            self._report = {
                "projection_name": "trace_vector_manifest",
                "projection_role": "shadow",
                "canonical": False,
                "path": str(self.db_path),
                "db_exists": self.db_path.exists(),
                "model_name": "",
                "vector_dim": 0,
                "expected_trace_count": 0,
                "vector_count": 0,
                "stored_vector_count": 0,
                "missing_vector_count": 0,
                "orphan_vector_count": 0,
                "malformed_vector_count": 0,
                "missing_vector_ids": [],
                "orphan_vector_ids": [],
                "malformed_vector_ids": [],
                "applied_seq": 0,
                "source_latest_seq": int(source_latest_seq or 0),
                "lag": int(source_latest_seq or 0),
            }
        report = dict(self._report)
        source_seq = int(source_latest_seq or report.get("source_latest_seq") or 0)
        report["source_latest_seq"] = source_seq
        report["lag"] = max(0, source_seq - int(report.get("applied_seq") or 0))
        return report

    def _read_db(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "db_exists": False,
                "model_name": "",
                "vector_dim": 0,
                "stored_vector_ids": [],
                "valid_vector_ids": [],
                "malformed_vector_ids": [],
            }
        stored_ids: list[str] = []
        valid_ids: list[str] = []
        malformed_ids: list[str] = []
        with sqlite3.connect(self.db_path) as conn:
            # 向量 JSON 可能远大于 ID。逐行消费游标，确保任一时刻只解析一条
            # 向量，避免 Dashboard 诊断在小内存实例上把全库正文 fetchall 进内存。
            for bucket_id, embedding_json in _safe_iter_embeddings(conn):
                stored_ids.append(bucket_id)
                if _valid_vector_json(embedding_json):
                    valid_ids.append(bucket_id)
                else:
                    malformed_ids.append(bucket_id)
            meta = _safe_fetch_meta(conn)
        return {
            "db_exists": True,
            "model_name": meta.get("model_name", ""),
            "vector_dim": _int_value(meta.get("vector_dim")),
            "stored_vector_ids": sorted(stored_ids),
            "valid_vector_ids": sorted(valid_ids),
            "malformed_vector_ids": sorted(malformed_ids),
        }


def _expects_vector(trace: dict[str, Any]) -> bool:
    state = str(trace.get("state") or "").lower()
    if state != "active":
        return False
    if trace.get("deleted") or trace.get("tombstone"):
        return False
    trace_kind = str(trace.get("trace_kind") or "").lower()
    return trace_kind != "archived"


def _safe_iter_embeddings(conn: sqlite3.Connection) -> Iterable[tuple[str, object]]:
    try:
        cursor = conn.execute("SELECT bucket_id, embedding FROM embeddings")
    except sqlite3.Error:
        return
    # 建立查询时的“表不存在”保持旧兼容语义；若游标已在途中
    # 读取失败，则应向上报告投影错误，不能把半份结果冒充完整诊断。
    for bucket_id, stored_vector in cursor:
        # 不要 str()：BLOB 被 str() 会变成 "b'\x00...'"，
        # 解码那头就再也认不出它了。
        yield str(bucket_id), stored_vector


def _safe_fetch_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute("SELECT key, value FROM embeddings_meta")
        return {str(key): str(value) for key, value in rows}
    except sqlite3.Error:
        return {}


def _valid_vector_json(value: object) -> bool:
    """单元格里是不是一个能用的向量。

    embeddings.db 里两种格式共存：float32 BLOB（现在写的）与老的 JSON 文本。
    这个诊断报的是「向量投影和活跃 trace 对不对得上」，只按 JSON 判会把每个
    BLOB 都算成 malformed，于是整份报告变成噪音。
    """
    return is_valid_stored_vector(value)


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
