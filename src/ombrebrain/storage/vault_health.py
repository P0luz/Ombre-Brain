"""Read-only integrity inspection shared by Dashboard and CLI diagnostics."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Iterable

import frontmatter


_ACTIVE_TOP_LEVELS = {"dynamic", "permanent", "feel", "plans", "letters"}


def _limited(items: list[Any], limit: int = 20) -> list[Any]:
    return items[:limit]


# 每次写这种 health check 都想到体检。
# 不想去医院。
def inspect_vault(
    buckets_dir: str,
    embedding_db_path: str = "",
    pending_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Inspect durable source files and the rebuildable vector projection."""
    root = Path(buckets_dir).resolve()
    pending = {str(item) for item in pending_ids}
    parse_errors: list[dict[str, str]] = []
    unsafe_paths: list[str] = []
    ids_to_paths: dict[str, list[str]] = {}
    active_ids: set[str] = set()
    markdown_count = 0

    if root.is_dir():
        for path in sorted(root.rglob("*.md")):
            try:
                resolved = path.resolve()
                if not resolved.is_file() or not resolved.is_relative_to(root):
                    unsafe_paths.append(str(path))
                    continue
                relative = resolved.relative_to(root).as_posix()
                # 跳过 vault 内以 _ 开头的内部目录，它们不是记忆：
                #   _app/      —— 播种进来的代码树，自 3.0.0 起含 docs/ 与 kernel/ 的 .md
                #   _app/_prev/ —— entrypoint 的崩溃回滚点，又留着上一版的同名副本
                #   _sources/  —— 原文证据层
                # 不跳过的话，同一份 CLAUDE_PROMPT.md 会在 _app/docs 与 _app/_prev/docs
                # 各出现一次，被判成「同一个 id 的重复桶」，完整性诊断整体报 error。
                # 记忆桶只住在 dynamic / permanent / feel / plans / letters / archive 下。
                # 这个找了两个小时。一直以为是 id 生成炸了，翻了一遍生成逻辑，
                # 最后发现是自己把 docs 播进了 vault。是我。凶手是我。
                if relative.split("/", 1)[0].startswith("_"):
                    continue
                markdown_count += 1
                post = frontmatter.loads(resolved.read_text(encoding="utf-8"))
                bucket_id = str(post.get("id") or resolved.stem).strip()
                if not bucket_id:
                    raise ValueError("missing bucket id")
                ids_to_paths.setdefault(bucket_id, []).append(relative)
                if relative.split("/", 1)[0] in _ACTIVE_TOP_LEVELS:
                    active_ids.add(bucket_id)
            except Exception as exc:
                parse_errors.append({"path": str(path), "error": str(exc)})

    duplicates = {
        bucket_id: paths for bucket_id, paths in ids_to_paths.items() if len(paths) > 1
    }
    markdown = {
        "directory_exists": root.is_dir(),
        "file_count": markdown_count,
        "unique_ids": len(ids_to_paths),
        "active_ids": len(active_ids),
        "parse_error_count": len(parse_errors),
        "parse_errors": _limited(parse_errors),
        "duplicate_id_count": len(duplicates),
        "duplicate_ids": dict(list(duplicates.items())[:20]),
        "unsafe_path_count": len(unsafe_paths),
        "unsafe_paths": _limited(unsafe_paths),
    }

    db_path = Path(embedding_db_path).resolve() if embedding_db_path else None
    db_exists = bool(db_path and db_path.is_file())
    db_ok = True
    db_error = ""
    vector_ids: set[str] = set()
    schema: list[str] = []
    if db_exists and db_path is not None:
        try:
            connection = sqlite3.connect(str(db_path), timeout=5)
            try:
                check = connection.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise sqlite3.DatabaseError(str(check[0] if check else "quick_check failed"))
                tables = {
                    str(row[0]) for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "embeddings" in tables:
                    schema = [
                        str(row[1])
                        for row in connection.execute("PRAGMA table_info(embeddings)").fetchall()
                    ]
                    id_column = "bucket_id" if "bucket_id" in schema else "id" if "id" in schema else ""
                    if id_column:
                        # 列名是上面自己从 schema 里挑的，不是外面传进来的。
                        # bandit 不信。我也懒得跟它吵，贴张条走人。
                        vector_ids = {
                            str(row[0]) for row in connection.execute(
                                f"SELECT {id_column} FROM embeddings"  # nosec B608
                            ).fetchall()
                        }
            finally:
                connection.close()
        except Exception as exc:
            db_ok = False
            db_error = str(exc)

    known_ids = set(ids_to_paths)
    orphan_ids = sorted(vector_ids - known_ids)
    missing_ids = sorted(active_ids - vector_ids)
    missing_unqueued = sorted(set(missing_ids) - pending)
    sqlite_report = {
        "path": str(db_path) if db_path else "",
        "exists": db_exists,
        "quick_check_ok": db_ok,
        "error": db_error,
        "schema": schema,
        "vector_count": len(vector_ids),
        "orphan_count": len(orphan_ids),
        "orphan_ids": _limited(orphan_ids),
        "missing_active_count": len(missing_ids),
        "missing_active_ids": _limited(missing_ids),
        "missing_unqueued_count": len(missing_unqueued),
        "missing_unqueued_ids": _limited(missing_unqueued),
        "pending_count": len(pending),
    }

    errors = (
        not root.is_dir()
        or bool(parse_errors)
        or bool(duplicates)
        or bool(unsafe_paths)
        or not db_ok
    )
    warnings = bool(orphan_ids or missing_unqueued)
    status = "error" if errors else "warning" if warnings else "ok"
    return {
        "status": status,
        "markdown": markdown,
        "sqlite": sqlite_report,
    }
