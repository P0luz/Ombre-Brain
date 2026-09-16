"""
========================================
migration_engine.py — embedding 迁移引擎（2.0.3 新增）
========================================

切 embedding 后端（local ↔ api）时，需要把 embeddings.db 里所有 bucket 的向量
用新后端重算一遍。这个模块负责后台跑这件事：

- 备份 embeddings.db → embeddings.db.backup（只在第一次启动时）
- 把新向量先写入 embeddings.db.migrating，避免半截状态污染主表
- 全部跑完后 atomically swap：主 db 替成 .migrating 文件
- 单条失败跳过 + 记录到 failed_items[:50]，不中断整体
- 进度文件 _pending_migration_status.json，前端 3s 轮询
- 断点续传：_migration_checkpoint.json 记录已完成 id 集合
- 限速：每批 10 条，间隔 0.5s（避免本地推理打爆 CPU 或 API 限流）
- 失败时附最近 15 行 errors.jsonl，提示她/他「这是本地环境相关问题」

不做：
- 不做 bucket 迁移、桶文件重写
- 不切换 global embedding_engine —— 那是 server.py 调用方的事
- 不做配置写盘
========================================
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from runtime_owner import spawn_background
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

try:
    from embedding_publish import (
        MigrationReservation,
        create_shadow_path,
        publish_shadow_generation,
        reserve_migration,
        shadow_path_for_reservation,
    )
except ImportError:  # pragma: no cover
    from .embedding_publish import (  # type: ignore
        MigrationReservation,
        create_shadow_path,
        publish_shadow_generation,
        reserve_migration,
        shadow_path_for_reservation,
    )

logger = logging.getLogger("ombre_brain.migration_engine")


# ---- 常量 ----

_STATUS_FILE_NAME = "_pending_migration_status.json"
_CHECKPOINT_FILE_NAME = "_migration_checkpoint.json"

# 每批 10 条，间隔 0.5s
BATCH_SIZE = 10
BATCH_INTERVAL_SEC = 0.5

# failed_items 上限（避免 status JSON 无限膨胀）
MAX_FAILED_ITEMS = 50

# 失败时附带的 errors.jsonl 末尾行数
TAIL_LOG_LINES = 15

# 进程级锁：同一时刻只允许一个迁移任务
_migration_lock = threading.Lock()
_migration_task: asyncio.Task | None = None
_migration_reservation: MigrationReservation | None = None
_v3_runtime: Any = None


def attach_v3_runtime(runtime) -> None:
    global _v3_runtime
    _v3_runtime = runtime


def get_v3_runtime():
    return _v3_runtime


# ============================================================
# 路径与状态
# ============================================================

def status_path_for(buckets_dir: str) -> str:
    log_dir = os.path.join(buckets_dir, ".logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, _STATUS_FILE_NAME)


def checkpoint_path_for(buckets_dir: str) -> str:
    log_dir = os.path.join(buckets_dir, ".logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, _CHECKPOINT_FILE_NAME)


def _empty_status() -> dict[str, Any]:
    return {
        "phase": "idle",      # idle | running | completed | failed
        "total": 0,
        "done": 0,
        "failed_count": 0,
        "current_id": "",
        "failed_items": [],
        "started_at": "",
        "finished_at": "",
        "target_backend": "",
        "target_model": "",
        "target_dim": 0,
        "message": "",
        "error": "",
        "tail_log": [],
    }


def read_status(status_path: str) -> dict[str, Any]:
    if not os.path.exists(status_path):
        return _empty_status()
    try:
        with open(status_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty_status()
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_status()


def write_status(status_path: str, status: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        tmp = status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, status_path)
    except OSError as e:
        logger.warning(f"[migration] failed to write status: {e}")


def target_signature(target_backend: str, target_model: str, target_dim: int) -> str:
    return f"{target_backend}:{target_model}:{target_dim}"


def _read_checkpoint(path: str, signature: str | None = None) -> set[str]:
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if signature is not None and (
            not isinstance(data, dict)
            or data.get("target_signature") != signature
        ):
            return set()
        done = data.get("done_ids", []) if isinstance(data, dict) else []
        return set(done) if isinstance(done, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _write_checkpoint(path: str, done_ids: Iterable[str], signature: str | None = None) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            payload = {"done_ids": sorted(done_ids)}
            if signature is not None:
                payload["target_signature"] = signature
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"[migration] failed to write checkpoint: {e}")


def staging_db_path_for(db_path: str) -> str:
    return f"{db_path}.migrating"


def reset_stale_migration_state(buckets_dir: str, db_path: str, signature: str) -> None:
    ckpt_path = checkpoint_path_for(buckets_dir)
    if not os.path.exists(ckpt_path):
        return
    try:
        with open(ckpt_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        stale = not isinstance(data, dict) or data.get("target_signature") != signature
    except (OSError, json.JSONDecodeError):
        stale = True
    if not stale:
        return
    for path in (ckpt_path, staging_db_path_for(db_path)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("[migration] failed to remove stale state %s: %s", path, exc)


def _tail_errors_log(buckets_dir: str, n: int = TAIL_LOG_LINES) -> list[str]:
    """读 errors.jsonl 末尾 n 行。失败返回空列表。"""
    candidates = [
        os.path.join(buckets_dir, ".logs", "errors.jsonl"),
        os.path.join(buckets_dir, "errors.jsonl"),
    ]
    for p in candidates:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [ln.rstrip("\n") for ln in lines[-n:]]
        except OSError:
            continue
    return []


# ============================================================
# 备份与提交
# ============================================================

def backup_db_once(db_path: str) -> str:
    """Deprecated compatibility helper.

    E-MIG-01 never uses or creates a fixed ``.backup`` file.  Callers that still
    inspect the legacy path receive it without any filesystem mutation.
    """

    return db_path + ".backup"


# ============================================================
# 迁移核心
# ============================================================

@dataclass
class MigrationConfig:
    """迁移参数。"""
    buckets_dir: str
    db_path: str
    target_backend: str          # 'local' | 'api'
    target_model: str
    target_dim: int
    # source/target engine 都已由调用方实例化好
    target_engine: Any           # EmbeddingEngine 实例（迁移目标）
    # bucket 内容来源：返回 list[(bucket_id, content)] 的 awaitable
    fetch_buckets: Callable[[], Awaitable[list[tuple[str, str]]]]
    # E-MIG-01 publication integration.  Callbacks are zero-argument closures
    # and may be synchronous or async.
    config_path: str | None = None
    config_forward_patch: Mapping[str, Any] | None = None
    old_config_sha256: str = ""
    candidate_config_sha256: str = ""
    config_inverse_patch: Mapping[str, Any] | None = None
    config_commit: Callable[[], Any] | None = None
    config_restore: Callable[[], Any] | None = None
    runtime_close: Callable[[], Any] | None = None
    runtime_apply: Callable[[], Any] | None = None
    runtime_restore: Callable[[], Any] | None = None
    runtime_open_probe: Callable[[], Any] | None = None

async def _run_migration(
    cfg: MigrationConfig,
    reservation: MigrationReservation,
    on_complete: Callable[[bool], None] | None = None,
) -> None:
    """Build, verify, and compensating-publish one private shadow DB."""
    status_path = status_path_for(cfg.buckets_dir)
    ckpt_path = checkpoint_path_for(cfg.buckets_dir) + f".{reservation.txid}"

    live_db = Path(cfg.db_path).expanduser().resolve(strict=False)
    expected_shadow = shadow_path_for_reservation(
        live_db, reservation=reservation
    ).resolve(strict=False)
    target_db = Path(
        getattr(cfg.target_engine, "db_path", "")
    ).expanduser().resolve(strict=False)
    if target_db != expected_shadow:
        error = (
            "target engine must be constructed with the reserved private shadow "
            f"path {expected_shadow.name}"
        )
        write_status(
            status_path,
            {
                **_empty_status(),
                "phase": "failed",
                "error": error,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "message": "迁移未启动：shadow 目标不安全",
            },
        )
        if on_complete:
            on_complete(False)
        return

    # The constructor creates the private database.  It must remain empty before
    # this transaction starts; otherwise a stale or foreign staging generation
    # could be published.
    try:
        existing_ids = cfg.target_engine.list_all_ids()
        if existing_ids:
            raise RuntimeError("reserved shadow is not empty")
    except Exception as exc:
        write_status(
            status_path,
            {
                **_empty_status(),
                "phase": "failed",
                "error": f"shadow preflight failed: {type(exc).__name__}: {exc}",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "message": "迁移未启动：shadow 预检失败",
            },
        )
        if on_complete:
            on_complete(False)
        return

    # 1) Pull all migratable buckets.  Empty bodies do not have embeddings and
    # are excluded from the exact expected row count.
    try:
        fetched = await cfg.fetch_buckets()
        buckets = [
            (str(bucket_id), content)
            for bucket_id, content in fetched
            if isinstance(content, str) and content.strip()
        ]
        ids = [bucket_id for bucket_id, _ in buckets]
        if any(not bucket_id for bucket_id in ids) or len(ids) != len(set(ids)):
            raise ValueError("bucket IDs must be non-empty and unique")
    except Exception as e:
        write_status(status_path, {
            **_empty_status(),
            "phase": "failed",
            "error": f"fetch buckets failed: {type(e).__name__}: {e}",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": "迁移未启动：列出桶失败",
            "tail_log": _tail_errors_log(cfg.buckets_dir),
        })
        if on_complete:
            on_complete(False)
        return

    total = len(buckets)
    # A checkpoint belongs to one internal txid/shadow only.  It is never
    # reused by a later reservation.
    done_ids = _read_checkpoint(ckpt_path)
    failed_items: list[dict[str, str]] = []
    failed_count = 0

    write_status(status_path, {
        **_empty_status(),
        "phase": "running",
        "total": total,
        "done": len(done_ids),
        "failed_count": 0,
        "current_id": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target_backend": cfg.target_backend,
        "target_model": cfg.target_model,
        "target_dim": cfg.target_dim,
        "message": f"开始迁移 {total} 个 bucket（已完成 {len(done_ids)}）",
    })

    # 2) Generate into the private shadow.  Any provider/store failure makes
    # the whole migration unpublishable; there is no partial-success publish.
    pending = [(bid, content) for bid, content in buckets if bid not in done_ids]
    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        for bucket_id, content in batch:
            cur = read_status(status_path)
            cur["current_id"] = bucket_id
            write_status(status_path, cur)

            try:
                ok = await cfg.target_engine.generate_and_store(bucket_id, content)
                if not ok:
                    failed_count += 1
                    if len(failed_items) < MAX_FAILED_ITEMS:
                        failed_items.append({
                            "bucket_id": bucket_id,
                            "error": "generate_and_store returned False",
                        })
                else:
                    done_ids.add(bucket_id)
            except Exception as e:
                failed_count += 1
                if len(failed_items) < MAX_FAILED_ITEMS:
                    failed_items.append({
                        "bucket_id": bucket_id,
                        "error": f"{type(e).__name__}: {e}",
                    })

        # 每批写一次 checkpoint + status
        _write_checkpoint(ckpt_path, done_ids)
        cur = read_status(status_path)
        cur["done"] = len(done_ids)
        cur["failed_count"] = failed_count
        cur["failed_items"] = failed_items
        cur["message"] = f"已完成 {len(done_ids)} / {total}（失败 {failed_count}）"
        write_status(status_path, cur)

        # 限速
        if i + BATCH_SIZE < len(pending):
            await asyncio.sleep(BATCH_INTERVAL_SEC)

    success = False
    publish_error = ""
    publish_exception: BaseException | None = None
    if failed_count == 0 and len(done_ids) == total:
        try:
            backend = getattr(cfg.target_engine, "_backend", None)
            actual_dim = (
                int(backend.vector_dim())
                if backend is not None
                else int(cfg.target_dim)
            )
            if actual_dim <= 0:
                raise ValueError("target provider reported an invalid dimension")
            target_model = (
                str(backend.model_name())
                if backend is not None
                else str(cfg.target_model)
            )
            cfg.target_engine._write_meta("model_name", target_model)
            cfg.target_engine._write_meta("vector_dim", str(actual_dim))
            cfg.target_engine._write_meta("generation", reservation.txid)
            publish_config_patch = (
                dict(cfg.config_forward_patch)
                if cfg.config_forward_patch is not None
                else None
            )
            # Provider clients can discover/correct the real dimension only
            # after the first vector.  Keep config and verified shadow metadata
            # in the same generation without ever admitting api_key to the
            # manifest-backed patch.
            if publish_config_patch is not None:
                if "model" in publish_config_patch:
                    publish_config_patch["model"] = target_model
                if "dim" in publish_config_patch:
                    publish_config_patch["dim"] = actual_dim
            cur = read_status(status_path)
            cur.update(
                {
                    "phase": "publishing",
                    "target_model": target_model,
                    "target_dim": actual_dim,
                    "message": "shadow 已完成，正在可逆发布",
                }
            )
            write_status(status_path, cur)
            await publish_shadow_generation(
                db_path=live_db,
                shadow_path=expected_shadow,
                expected_model=target_model,
                expected_dim=actual_dim,
                expected_count=total,
                expected_bucket_ids=set(ids),
                config_path=cfg.config_path,
                config_forward_patch=publish_config_patch,
                old_config_sha256=cfg.old_config_sha256,
                candidate_config_sha256=cfg.candidate_config_sha256,
                config_inverse_patch=cfg.config_inverse_patch,
                config_commit=cfg.config_commit,
                config_restore=cfg.config_restore,
                runtime_close=cfg.runtime_close,
                runtime_apply=cfg.runtime_apply,
                runtime_restore=cfg.runtime_restore,
                runtime_open_probe=cfg.runtime_open_probe,
                reservation=reservation,
            )
            success = True
        except BaseException as exc:
            publish_exception = exc
            publish_error = type(exc).__name__
            logger.error(
                "[migration] E-MIG publish failed (%s); details retained in exception chain",
                publish_error,
            )

    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    if success:
        final_phase = "completed"
        final_msg = f"迁移与可逆发布完成：{len(done_ids)} / {total}"
        final_error = ""
    elif publish_error:
        final_phase = "publish_failed"
        final_msg = "shadow 已完成，但可逆发布失败；未报告成功"
        final_error = publish_error
    else:
        final_phase = "failed"
        final_msg = (
            f"迁移失败：{len(done_ids)} 成功 / {failed_count} 失败；"
            "生产库未触碰"
        )
        final_error = "one or more shadow rows failed"

    cur = read_status(status_path)
    cur.update({
        "phase": final_phase,
        "current_id": "",
        "done": len(done_ids),
        "failed_count": failed_count,
        "failed_items": failed_items,
        "finished_at": finished_at,
        "message": final_msg,
        "error": final_error,
        "tail_log": _tail_errors_log(cfg.buckets_dir)
        if not success
        else [],
    })
    write_status(status_path, cur)

    if success:
        with contextlib.suppress(OSError):
            os.remove(ckpt_path)

    if on_complete:
        try:
            on_complete(success)
        except Exception as e:
            logger.warning(f"[migration] on_complete callback failed: {e}")

    if isinstance(publish_exception, asyncio.CancelledError):
        raise publish_exception


def start_migration(
    cfg: MigrationConfig,
    loop: asyncio.AbstractEventLoop | None = None,
    on_complete: Callable[[bool], None] | None = None,
    *,
    reservation: MigrationReservation | None = None,
) -> asyncio.Task | None:
    """在指定 event loop 上启动后台迁移任务。

    同一时刻只允许一个迁移任务，重复调用返回 None。
    """
    global _migration_task, _migration_reservation
    if not _migration_lock.acquire(blocking=False):
        logger.info("[migration] another migration already in progress; skip")
        return None
    active_reservation = reservation or reserve_migration(cfg.db_path)
    if active_reservation is None:
        _migration_lock.release()
        logger.info("[migration] cross-process reservation is already held; skip")
        return None
    if active_reservation._closed:
        _migration_lock.release()
        logger.warning("[migration] rejected a closed reservation")
        return None
    _migration_reservation = active_reservation

    target_loop = loop or asyncio.get_event_loop()
    callback_called = False

    def _complete_once(success: bool) -> None:
        nonlocal callback_called
        if callback_called:
            return
        callback_called = True
        if on_complete:
            on_complete(success)

    async def _wrap():
        try:
            await _run_migration(
                cfg,
                reservation=active_reservation,
                on_complete=_complete_once,
            )
        except BaseException:
            _complete_once(False)
            raise
        finally:
            global _migration_reservation
            active_reservation.close()
            _migration_reservation = None
            _migration_lock.release()

    try:
        task = spawn_background(_wrap(), loop=target_loop)
    except BaseException:
        active_reservation.close()
        _migration_reservation = None
        _migration_lock.release()
        raise
    _migration_task = task
    return task


def is_running() -> bool:
    return _migration_lock.locked()


def reset_for_test() -> None:
    """测试用：强制释放锁。"""
    global _migration_task, _migration_reservation
    if _migration_reservation is not None:
        _migration_reservation.close()
        _migration_reservation = None
    if _migration_lock.locked():
        try:
            _migration_lock.release()
        except RuntimeError:
            pass
    _migration_task = None


__all__ = [
    "MigrationConfig",
    "status_path_for",
    "checkpoint_path_for",
    "read_status",
    "write_status",
    "backup_db_once",
    "MigrationReservation",
    "reserve_migration",
    "create_shadow_path",
    "start_migration",
    "is_running",
    "reset_for_test",
    "attach_v3_runtime",
    "get_v3_runtime",
    "BATCH_SIZE",
    "BATCH_INTERVAL_SEC",
]
