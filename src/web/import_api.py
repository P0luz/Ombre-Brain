"""
========================================
web/import_api.py — 宿主机 vault 设置 / 历史对话导入 / 桶编辑 / 导出 / 记忆包迁移
========================================

- /api/host-vault：读写 docker-compose 挂载的宿主机记忆目录（写 .env）
- /api/import/*：上传历史对话、状态/暂停/模式/结果/复核
- /api/bucket/{id}/edit：编辑桶正文（带内容体积校验）
- /api/export：导出全部记忆 zip
- /api/migrate/*：记忆包 zip 上传 / 状态 / 应用

对外暴露：register(mcp)。
========================================
"""

import os
import time
import asyncio
import math
import threading
import tempfile
from contextlib import AsyncExitStack
from datetime import datetime as _dt
from typing import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import FileResponse, Response

from . import _shared as sh

try:
    from utils import normalize_memory_title, parse_bool, sanitize_name  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import normalize_memory_title, parse_bool, sanitize_name  # type: ignore

from ombrebrain.storage.backup_archive import (
    MAX_ARCHIVE_BYTES,
    BackupArchiveError,
    build_export_archive_file,
)

logger = sh.logger

try:
    from tools._common import (  # type: ignore
        _quota_turn,
        check_content_size as _check_content_size,
        check_pinned_quota as _check_pinned_quota,
        is_terminal_memory_metadata as _is_terminal_memory_metadata,
    )
except ImportError:  # pragma: no cover
    from ..tools._common import (  # type: ignore
        _quota_turn,
        check_content_size as _check_content_size,
        check_pinned_quota as _check_pinned_quota,
        is_terminal_memory_metadata as _is_terminal_memory_metadata,
    )

try:
    from tools.plan.core import (  # type: ignore
        is_letter_bucket,
        letter_lock_revision,
        letter_lock_state,
    )
except ImportError:  # pragma: no cover
    from ..tools.plan.core import (  # type: ignore
        is_letter_bucket,
        letter_lock_revision,
        letter_lock_state,
    )

try:
    from import_memory import preview_import  # type: ignore
except ImportError:  # pragma: no cover
    from ..import_memory import preview_import  # type: ignore


_DEFAULT_MAX_IMPORT_UPLOAD_BYTES = 4 * 1024 * 1024
_HARD_MAX_IMPORT_UPLOAD_BYTES = 8 * 1024 * 1024
_MAX_MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class _CleanupFileResponse(FileResponse):
    """A file response whose cleanup also runs on Range/send failures.

    Starlette's ``background`` hook is not reached by every early-return path
    (notably an unsatisfiable Range request).  Export files are sizeable and
    their cleanup also releases the singleton export reservation, so cleanup
    belongs in a ``finally`` around the whole ASGI response instead.
    """

    def __init__(
        self,
        *args,
        cleanup: Callable[[], Awaitable[None]],
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._export_cleanup = cleanup

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            cleanup, self._export_cleanup = self._export_cleanup, None
            if cleanup is not None:
                await cleanup()


def _max_import_upload_bytes() -> int:
    limits = (getattr(sh, "config", {}) or {}).get("limits") or {}
    try:
        configured = max(
            1,
            int(
                limits.get("max_import_upload_bytes")
                or _DEFAULT_MAX_IMPORT_UPLOAD_BYTES
            ),
        )
        return min(configured, _HARD_MAX_IMPORT_UPLOAD_BYTES)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_IMPORT_UPLOAD_BYTES


async def _read_body_limited(request: Request, limit: int) -> bytes:
    raw_length = str((getattr(request, "headers", {}) or {}).get("content-length", "") or "").strip()
    if raw_length:
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        if length > limit:
            raise ValueError(f"Upload too large ({length} bytes > {limit} byte limit)")

    stream = getattr(request, "stream", None)
    if callable(stream):
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream():
            total += len(chunk)
            if total > limit:
                raise ValueError(f"Upload too large ({total} bytes > {limit} byte limit)")
            chunks.append(chunk)
        return b"".join(chunks)

    body = await request.body()
    if len(body) > limit:
        raise ValueError(f"Upload too large ({len(body)} bytes > {limit} byte limit)")
    return body


async def _read_file_field_limited(file_field, limit: int) -> bytes:
    raw = await file_field.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"Upload too large ({len(raw)} bytes > {limit} byte limit)")
    return raw


async def _spool_chunks_to_temp(chunks, limit: int) -> str:
    """Stream async byte chunks to a private file with one bounded buffer."""

    fd, path = tempfile.mkstemp(prefix="ombre-upload-", suffix=".zip")
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            async for chunk in chunks:
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        f"Upload too large ({total} bytes > {limit} byte limit)"
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


async def _spool_body_limited(request: Request, limit: int) -> str:
    raw_length = str(request.headers.get("content-length", "") or "").strip()
    if raw_length:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0 or length > limit:
            raise ValueError(f"Upload too large ({length} bytes > {limit} byte limit)")

    stream = getattr(request, "stream", None)
    if callable(stream):
        return await _spool_chunks_to_temp(stream(), limit)

    async def one_chunk():
        yield await request.body()

    return await _spool_chunks_to_temp(one_chunk(), limit)


async def _spool_file_field_limited(file_field, limit: int) -> str:
    async def chunks():
        while chunk := await file_field.read(1024 * 1024):
            yield chunk

    return await _spool_chunks_to_temp(chunks(), limit)


async def _read_multipart_form_limited(request: Request, payload_limit: int):
    """Parse one upload while bounding the raw multipart stream itself.

    UploadFile spooling happens inside Starlette's parser, so checking the file
    after ``request.form()`` is too late for a chunked disk-filling request.
    Count ASGI receive bytes while the parser consumes them and cap auxiliary
    fields/header overhead separately from the allowed file payload.
    """
    request_limit = payload_limit + _MAX_MULTIPART_OVERHEAD_BYTES
    raw_length = str(request.headers.get("content-length", "") or "").strip()
    if raw_length:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if declared_length < 0 or declared_length > request_limit:
            raise ValueError(
                f"Upload too large ({declared_length} bytes > {request_limit} byte request limit)"
            )

    original_receive = request._receive
    received = 0

    async def limited_receive():
        nonlocal received
        message = await original_receive()
        if isinstance(message, dict) and message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > request_limit:
                raise ValueError(
                    f"Upload too large ({received} bytes > {request_limit} byte request limit)"
                )
        return message

    request._receive = limited_receive
    try:
        return await request.form(
            max_files=1,
            max_fields=8,
            max_part_size=64 * 1024,
        )
    finally:
        request._receive = original_receive


async def _read_import_upload_text(request: Request) -> tuple[str, str, int]:
    limit = _max_import_upload_bytes()
    content_type = request.headers.get("content-type", "")
    filename = ""
    if "multipart/form-data" in content_type:
        form = await _read_multipart_form_limited(request, limit)
        file_field = form.get("file")
        if not file_field or isinstance(file_field, str):
            raise ValueError("No file field")
        raw_bytes = await _read_file_field_limited(file_field, limit)
        filename = getattr(file_field, "filename", "upload")
        return raw_bytes.decode("utf-8", errors="replace"), filename, len(raw_bytes)

    body = await _read_body_limited(request, limit)
    filename = request.query_params.get("filename", "upload")
    return body.decode("utf-8", errors="replace"), filename, len(body)


def _import_llm_ready() -> bool:
    engine_dehydrator = getattr(getattr(sh, "import_engine", None), "dehydrator", None)
    if engine_dehydrator is not None:
        return bool(getattr(engine_dehydrator, "api_available", False))
    return bool(getattr(getattr(sh, "dehydrator", None), "api_available", False))


async def _await_history_worker(func, *args, **kwargs):
    """Run preview parsing off-loop and reap it before releasing admission."""

    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        try:
            worker.result()
        except BaseException:
            pass
        raise


async def _await_export_worker(worker: asyncio.Task):
    """Reap a ZIP builder through repeated cancellation and remove its result."""

    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        try:
            orphan_path, _manifest = worker.result()
        except BaseException:
            orphan_path = ""
        if orphan_path:
            try:
                os.unlink(orphan_path)
            except OSError:
                pass
        raise


def register(mcp) -> None:
    # Keep the lock until FileResponse finishes.  Concurrent full-vault exports
    # otherwise multiply disk scan, SQLite snapshot, compression and temp space.
    # FastMCP may serve routes from different event loops/threads, so this must
    # not be an asyncio.Lock bound to the loop that happened to call register().
    export_lock = threading.Lock()
    # Preview and upload parsing can expand JSON/Markdown many times in memory.
    # One cross-loop process-wide admission lock prevents concurrent requests
    # from multiplying that peak.  Upload keeps it until the background import
    # has released its engine reservation.
    history_ingest_lock = threading.Lock()

    @mcp.custom_route("/api/host-vault", methods=["GET"])
    async def api_host_vault_get(request: Request) -> Response:
        """Read the host-side vault path without pretending a container can change its mount."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        compose_managed = sh.in_docker()
        if compose_managed:
            # A container-local .env cannot affect the host-side volume source used
            # before this container starts. Only report the value Compose injected.
            value = os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip()
            source = "env" if value else ""
            env_file = None
        else:
            value = sh._read_env_var("OMBRE_HOST_VAULT_DIR")
            source = "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else "")
            env_file = sh._project_env_path()
        return JSONResponse({
            "value": value,
            "source": source,
            "env_file": env_file,
            "compose_managed": compose_managed,
            "message": (
                "该挂载由宿主机 Compose 管理。请在 compose 文件旁的 .env 设置 "
                "OMBRE_HOST_VAULT_DIR，然后执行 docker compose up -d --force-recreate。"
                if compose_managed else ""
            ),
        })


    @mcp.custom_route("/api/host-vault", methods=["POST"])
    async def api_host_vault_set(request: Request) -> Response:
        """
        Persist OMBRE_HOST_VAULT_DIR for non-container deployments.
        Body: {"value": "/path/to/vault"}  (empty string clears the entry)

        Docker mounts are resolved by Compose before the container starts. Writing
        /app/src/.env from inside that container cannot change the host mount, so
        Docker callers receive an explicit host-managed response instead.
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if sh.in_docker():
            return JSONResponse({
                "error": (
                    "容器无法修改宿主机的 Compose 挂载。请在 compose 文件旁的 .env 设置 "
                    "OMBRE_HOST_VAULT_DIR，然后执行 docker compose up -d --force-recreate。"
                ),
                "compose_managed": True,
                "restart_required": True,
                "env_var": "OMBRE_HOST_VAULT_DIR",
            }, status_code=409)
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        raw = body.get("value", "")
        if not isinstance(raw, str):
            return JSONResponse({"error": "value must be a string"}, status_code=400)
        value = raw.strip()

        # Reject characters that would break .env / shell parsing
        if "\n" in value or "\r" in value or '"' in value or "'" in value:
            return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

        try:
            sh._write_env_var("OMBRE_HOST_VAULT_DIR", value)
        except Exception as e:
            return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

        return JSONResponse({
            "ok": True,
            "value": value,
            "env_file": sh._project_env_path(),
            "restart_required": True,
            "message": "已保存 OMBRE_HOST_VAULT_DIR；需要重启容器/服务后挂载才会生效。",
            "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
        })


    # =============================================================
    # Import API — conversation history import
    # 导入 API — 对话历史导入
    # =============================================================

    @mcp.custom_route("/api/import/preflight", methods=["POST"])
    async def api_import_preflight(request: Request) -> Response:
        """Preview an import file without writing buckets or starting a job."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        if not history_ingest_lock.acquire(blocking=False):
            return JSONResponse(
                {
                    "ok": False,
                    "error": "History import processing already active",
                    "job_id": getattr(sh.import_engine, "active_job_id", ""),
                },
                status_code=409,
            )
        try:
            if bool(getattr(sh.import_engine, "is_running", False)):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Import already running",
                        "job_id": getattr(sh.import_engine, "active_job_id", ""),
                    },
                    status_code=409,
                )
            try:
                raw_content, filename, size_bytes = await _read_import_upload_text(
                    request
                )
            except Exception as e:
                return JSONResponse(
                    {"ok": False, "error": f"Failed to read upload: {e}"},
                    status_code=400,
                )

            if not raw_content or not any(not char.isspace() for char in raw_content):
                return JSONResponse(
                    {"ok": False, "error": "Empty file"}, status_code=400
                )

            human_label = str((sh.config or {}).get("human") or "用户")
            try:
                preview = await _await_history_worker(
                    preview_import,
                    raw_content,
                    filename,
                    human_label,
                )
            except Exception as exc:
                logger.warning(
                    "import preflight rejected malformed input: err_type=%s detail=hidden",
                    type(exc).__name__,
                )
                return JSONResponse(
                    {"ok": False, "error": "Import preview could not parse file"},
                    status_code=400,
                )
            raw_content = ""
            llm_ready = _import_llm_ready()
            requires_llm = bool(preview.get("requires_llm", True))
            return JSONResponse({
                **preview,
                "filename": filename,
                "size_bytes": size_bytes,
                "import_running": False,
                "llm_ready": llm_ready,
                "can_start": bool(preview.get("ok"))
                and (llm_ready or not requires_llm),
            })
        finally:
            history_ingest_lock.release()


    @mcp.custom_route("/api/import/upload", methods=["POST"])
    async def api_import_upload(request: Request) -> Response:
        """Upload a conversation file and start import."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        if not history_ingest_lock.acquire(blocking=False):
            return JSONResponse(
                {
                    "error": "History import processing already active",
                    "job_id": getattr(sh.import_engine, "active_job_id", ""),
                },
                status_code=409,
            )

        job_id = sh.import_engine.reserve_start()
        if job_id is None:
            history_ingest_lock.release()
            return JSONResponse(
                {
                    "error": "Import already running",
                    "job_id": sh.import_engine.active_job_id,
                },
                status_code=409,
            )

        release_guard = threading.Lock()
        released = False

        def release_job() -> None:
            nonlocal released
            with release_guard:
                if released:
                    return
                released = True
            sh.import_engine.release_start_reservation(job_id)
            history_ingest_lock.release()

        try:
            raw_content, filename, size_bytes = await _read_import_upload_text(request)

            if not raw_content or not any(not char.isspace() for char in raw_content):
                release_job()
                return JSONResponse({"error": "Empty file"}, status_code=400)

            preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
            resume = request.query_params.get("resume", "").lower() in ("1", "true")

        except asyncio.CancelledError:
            release_job()
            raise
        except Exception as e:
            release_job()
            return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

        # Start import in background
        async def _run_import():
            nonlocal raw_content
            try:
                start_coro = sh.import_engine.start(
                    raw_content,
                    filename,
                    preserve_raw,
                    resume,
                    reservation_id=job_id,
                )
                raw_content = ""
                result = await start_coro
                if result.get("error"):
                    logger.warning(
                        "Import job %s did not start: %s",
                        job_id,
                        result["error"],
                    )
            except Exception as e:
                logger.error(
                    "Import job %s failed: err_type=%s detail=hidden",
                    job_id,
                    type(e).__name__,
                )
            finally:
                release_job()

        import_coro = _run_import()
        try:
            import_task = asyncio.create_task(import_coro)
            import_task.add_done_callback(lambda _task: release_job())
        except Exception as e:
            import_coro.close()
            release_job()
            logger.error(f"Failed to schedule import job {job_id}: {e}")
            return JSONResponse(
                {"error": "Failed to schedule import", "job_id": job_id},
                status_code=500,
            )

        return JSONResponse({
            "status": "started",
            "job_id": job_id,
            "filename": filename,
            "size_bytes": size_bytes,
        })


    @mcp.custom_route("/api/import/status", methods=["GET"])
    async def api_import_status(request: Request) -> Response:
        """Get current import progress."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(sh.import_engine.get_status())


    @mcp.custom_route("/api/import/pause", methods=["POST"])
    async def api_import_pause(request: Request) -> Response:
        """Pause the running import."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if not sh.import_engine.is_running:
            return JSONResponse({"error": "No import running"}, status_code=400)
        sh.import_engine.pause()
        return JSONResponse({"status": "pause_requested"})


    @mcp.custom_route("/api/import/patterns", methods=["GET"])
    async def api_import_patterns(request: Request) -> Response:
        """Detect high-frequency patterns after import."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            patterns = await sh.import_engine.detect_patterns()
            return JSONResponse({"patterns": patterns})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/import/results", methods=["GET"])
    async def api_import_results(request: Request) -> Response:
        """按有界分页列出待复核的导入桶。"""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "limit 必须是 [1,200] 范围内的整数"}, status_code=400)
        try:
            offset = int(request.query_params.get("offset", "0"))
            if offset < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"error": "offset 必须是非负整数"}, status_code=400)
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=False)
            imported_buckets = []
            for bucket in all_buckets:
                metadata = bucket.get("metadata", {})
                if not isinstance(metadata, dict):
                    continue
                if not (
                    parse_bool(metadata.get("imported"), default=False)
                    or str(metadata.get("source_tool") or "").strip() == "import"
                ):
                    continue
                imported_buckets.append(bucket)

            # 使用稳定的最新优先顺序；即使有新导入追加，分页顺序仍可预测。
            imported_buckets.sort(
                key=lambda b: (
                    str(b.get("metadata", {}).get("created", "")),
                    str(b.get("id", "")),
                ),
                reverse=True,
            )
            page = imported_buckets[offset:offset + limit]
            results = []
            for b in page:
                metadata = b["metadata"]
                logical_letter = is_letter_bucket(b)
                lock_state = letter_lock_state(b, "human")
                letter_locked = bool(lock_state["locked"])
                results.append({
                    "id": b["id"],
                    "name": (
                        "一封上锁的信"
                        if letter_locked else metadata.get("name", "")
                    ),
                    "content": (
                        "这封信尚未向你开放。"
                        if letter_locked else b["content"][:300]
                    ),
                    "type": metadata.get("type", ""),
                    "domain": (
                        ["letter"] if letter_locked else metadata.get("domain", [])
                    ),
                    "tags": (
                        ["__letter__"] if letter_locked else metadata.get("tags", [])
                    ),
                    "importance": metadata.get("importance", 5),
                    "created": metadata.get("created", ""),
                    "imported": True,
                    "letter_item": logical_letter,
                    "letter_locked": letter_locked,
                })
            next_offset = offset + len(results)
            has_more = next_offset < len(imported_buckets)
            return JSONResponse({
                "buckets": results,
                "total": len(imported_buckets),
                "offset": offset,
                "limit": limit,
                "has_more": has_more,
                "next_offset": next_offset if has_more else None,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/import/review", methods=["POST"])
    async def api_import_review(request: Request) -> Response:
        """Apply review decisions: mark buckets as important/noise/pinned."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        decisions = body.get("decisions", [])
        if not isinstance(decisions, list) or not decisions:
            return JSONResponse({"error": "No decisions provided"}, status_code=400)
        if len(decisions) > 1000:
            return JSONResponse({"error": "Too many review decisions (max 1000)"}, status_code=400)
        if any(not isinstance(decision, dict) for decision in decisions):
            return JSONResponse({"error": "Each decision must be an object"}, status_code=400)

        applied = 0
        errors = 0
        for d in decisions:
            bid = d.get("bucket_id", "")
            action = d.get("action", "")
            if (
                not isinstance(bid, str)
                or not isinstance(action, str)
                or len(bid) > 128
            ):
                errors += 1
                continue
            if not bid.strip() or not action.strip():
                errors += 1
                continue
            try:
                if action == "important":
                    bucket = await sh.bucket_mgr.get(bid)
                    if not bucket:
                        errors += 1
                        continue
                    if is_letter_bucket(bucket):
                        errors += 1
                        continue
                    metadata = bucket.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    try:
                        current_importance = int(
                            metadata.get("importance") or 5
                        )
                    except (TypeError, ValueError):
                        current_importance = 5
                    pinned_or_protected = (
                        parse_bool(metadata.get("pinned"), default=False)
                        or parse_bool(metadata.get("protected"), default=False)
                    )
                    target_importance = 9
                    if pinned_or_protected and current_importance >= 9:
                        # Importance is locked, but the requested semantic is
                        # already satisfied; keep the idempotent action.
                        target_importance = current_importance

                    if target_importance != current_importance:
                        ok = await sh.bucket_mgr.update(
                            bid, importance=target_importance
                        )
                        if not ok:
                            errors += 1
                            continue
                        persisted = await sh.bucket_mgr.get(bid)
                        try:
                            actual_importance = int(
                                (persisted or {}).get("metadata", {}).get(
                                    "importance"
                                )
                            )
                        except (TypeError, ValueError):
                            actual_importance = -1
                        if actual_importance != target_importance:
                            errors += 1
                            continue
                elif action == "pin":
                    async with AsyncExitStack() as quota_stack:
                        await quota_stack.enter_async_context(
                            _quota_turn("pinned")
                        )
                        bucket = await sh.bucket_mgr.get(bid)
                        if not bucket:
                            errors += 1
                            continue
                        if is_letter_bucket(bucket):
                            errors += 1
                            continue
                        metadata = bucket.get("metadata", {})
                        if _is_terminal_memory_metadata(metadata):
                            errors += 1
                            continue
                        already_pinned = parse_bool(
                            metadata.get("pinned")
                            if isinstance(metadata, dict) else None,
                            default=False,
                        )
                        if not already_pinned:
                            quota_err = await _check_pinned_quota()
                            if quota_err:
                                logger.warning(
                                    f"Review pin rejected for {bid}: {quota_err}"
                                )
                                errors += 1
                                continue
                            ok = await sh.bucket_mgr.update(bid, pinned=True)
                            if not ok:
                                errors += 1
                                continue
                        persisted = await sh.bucket_mgr.get(bid)
                        actual_pinned = parse_bool(
                            (persisted or {}).get("metadata", {}).get("pinned"),
                            default=False,
                        )
                        if not persisted or not actual_pinned:
                            errors += 1
                            continue
                elif action == "noise":
                    # A pin can otherwise land between the read and update,
                    # causing BucketManager to apply resolved=True but silently
                    # ignore importance=1. Hold quota locks in global order and
                    # verify both fields before reporting this decision applied.
                    async with AsyncExitStack() as quota_stack:
                        await quota_stack.enter_async_context(
                            _quota_turn("pinned")
                        )
                        bucket = await sh.bucket_mgr.get(bid)
                        if not bucket:
                            errors += 1
                            continue
                        if is_letter_bucket(bucket):
                            errors += 1
                            continue
                        metadata = bucket.get("metadata", {})
                        if not isinstance(metadata, dict):
                            metadata = {}
                        if (
                            parse_bool(metadata.get("pinned"), default=False)
                            or parse_bool(
                                metadata.get("protected"), default=False
                            )
                        ):
                            errors += 1
                            continue
                        ok = await sh.bucket_mgr.update(
                            bid, resolved=True, importance=1
                        )
                        if not ok:
                            errors += 1
                            continue
                        persisted = await sh.bucket_mgr.get(bid)
                        persisted_metadata = (persisted or {}).get(
                            "metadata", {}
                        )
                        try:
                            actual_importance = int(
                                persisted_metadata.get("importance")
                            )
                        except (AttributeError, TypeError, ValueError):
                            actual_importance = -1
                        actual_resolved = parse_bool(
                            persisted_metadata.get("resolved")
                            if isinstance(persisted_metadata, dict) else None,
                            default=False,
                        )
                        if (
                            not persisted
                            or not actual_resolved
                            or actual_importance != 1
                        ):
                            errors += 1
                            continue
                elif action == "delete":
                    bucket = await sh.bucket_mgr.get(bid)
                    if not bucket or is_letter_bucket(bucket):
                        errors += 1
                        continue
                    result = await sh.deletion_requests.submit(
                        bid, str(d.get("reason") or "")
                    )
                    if not result.get("ok"):
                        errors += 1
                        continue
                else:
                    errors += 1
                    continue
                applied += 1
            except Exception as e:
                logger.warning(f"Review action failed for {bid}: {e}")
                errors += 1

        return JSONResponse({"applied": applied, "errors": errors})


    # =============================================================
    # /api/bucket/{id}/edit  — iter 1.6 §6 trace 前端
    # 让 Dashboard 直接修改桶元数据：name / tags / importance / resolved /
    # pinned / digested / domain / dont_surface / why_remembered / plan weight。
    # content 也支持，会同步重建 embedding。
    # 内容大小受 §5 limits.max_bucket_bytes 约束；钉选量受 max_pinned 约束。
    # =============================================================
    @mcp.custom_route("/api/bucket/{bucket_id}/edit", methods=["PATCH", "POST"])
    async def api_bucket_edit(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "bucket not found"}, status_code=404)
        logical_letter = is_letter_bucket(bucket)
        lock_precondition = (
            {"expected_lock_state": letter_lock_revision(bucket)}
            if logical_letter else {}
        )
        if letter_lock_state(bucket, "human")["locked"]:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "locked letter cannot be edited from the bucket API",
                    "updated": [],
                },
                status_code=403,
            )
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        field_order = (
            "name", "title", "type", "tags", "domain", "importance", "resolved",
            "pinned", "digested", "dont_surface", "why_remembered",
            "weight", "content",
        )
        allowed_fields = set(field_order)

        def reject(message: str, *, status_code: int = 400, **details) -> Response:
            payload = {"ok": False, "error": message, "updated": []}
            payload.update(details)
            return JSONResponse(payload, status_code=status_code)

        unknown_fields = sorted(str(key) for key in body if key not in allowed_fields)
        if unknown_fields:
            return reject(
                "unknown edit fields: " + ", ".join(unknown_fields),
                unknown_fields=unknown_fields,
            )

        metadata = bucket.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        if _is_terminal_memory_metadata(metadata):
            return reject(
                "archived buckets cannot be edited",
                status_code=409,
                conflict="archived",
            )

        bool_fields = {"resolved", "pinned", "digested", "dont_surface"}

        def bucket_value(source: dict, field: str):
            source_meta = source.get("metadata", {})
            if not isinstance(source_meta, dict):
                source_meta = {}
            if field == "content":
                return source.get("content", "")
            raw = source_meta.get(field)
            if field in bool_fields:
                return parse_bool(raw, default=False)
            if field in ("tags", "domain"):
                if isinstance(raw, str):
                    return [part.strip() for part in raw.split(",") if part.strip()]
                return list(raw or []) if isinstance(raw, (list, tuple, set)) else []
            if field == "importance":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return 5
            if field == "weight":
                if raw is None:
                    return None
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None
            if field == "why_remembered":
                return str(raw or "")
            if field == "title":
                return str(raw or "")
            if field == "type":
                return str(raw or "dynamic").strip().lower()
            return raw

        before_values = {field: bucket_value(bucket, field) for field in field_order}
        current_type = before_values["type"]

        # BucketManager.update() owns the atomic metadata + directory migration.
        # The web boundary still normalizes and allow-lists types so archived or
        # future internal storage classes cannot be reached through Dashboard.
        valid_types = {
            "dynamic", "permanent", "feel", "plan", "letter", "i", "self",
        }
        requested_type = current_type
        if "type" in body:
            raw_type = body["type"]
            if not isinstance(raw_type, str):
                return reject("invalid bucket type")
            requested_type = raw_type.strip().lower()
            if requested_type not in valid_types:
                return reject("invalid bucket type")

        def normalize_list_field(raw, *, field: str, max_items: int) -> list[str]:
            if isinstance(raw, str):
                values = raw.split(",")
            elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
                values = raw
            else:
                raise ValueError(f"{field} must be a string or a list of strings")
            normalized: list[str] = []
            for item in values:
                text = item.strip()[:128]
                if text and text not in normalized:
                    normalized.append(text)
                if len(normalized) >= max_items:
                    break
            return normalized

        updates: dict = {}

        explicit_name_changed = False
        if "name" in body:
            if not isinstance(body["name"], str) or not body["name"].strip():
                return reject("name must be a non-empty string")
            name = sanitize_name(body["name"].strip())
            if name != before_values["name"]:
                updates["name"] = name
                explicit_name_changed = True

        if "title" in body:
            if not isinstance(body["title"], str):
                return reject("title must be a string")
            try:
                title = normalize_memory_title(body["title"])
            except ValueError as exc:
                return reject(str(exc))
            if not title:
                return reject("title must be a non-empty string")
            if title != before_values["title"]:
                updates["title"] = title
                # name 是带时间前缀的展示/文件名兼容字段。若调用方没有
                # 同时明确修改 name，则与 title 在同一次 update 中同步。
                if not explicit_name_changed:
                    current_name = str(before_values["name"] or "")
                    prefix = current_name[:19]
                    has_timestamp = bool(
                        len(prefix) == 19
                        and prefix[4:5] == "-"
                        and prefix[7:8] == "-"
                        and prefix[10:11] == " "
                        and prefix[13:14] == "-"
                        and prefix[16:17] == "-"
                    )
                    derived_name = sanitize_name(
                        f"{prefix} {title}" if has_timestamp else title
                    )
                    if derived_name != before_values["name"]:
                        updates["name"] = derived_name

        for field, max_items in (("tags", 64), ("domain", 16)):
            if field not in body:
                continue
            try:
                values = normalize_list_field(
                    body[field], field=field, max_items=max_items
                )
            except ValueError as e:
                return reject(str(e))
            if field == "domain" and not values:
                values = ["未分类"]
            if values != before_values[field]:
                updates[field] = values

        requested_importance = None
        if "importance" in body:
            raw_importance = body["importance"]
            if isinstance(raw_importance, bool):
                return reject("importance must be an integer from 1 to 10")
            try:
                requested_importance = int(raw_importance)
            except (TypeError, ValueError):
                return reject("importance must be an integer from 1 to 10")
            if (
                isinstance(raw_importance, float)
                and not raw_importance.is_integer()
            ) or not 1 <= requested_importance <= 10:
                return reject("importance must be an integer from 1 to 10")

        for flag in ("resolved", "digested", "dont_surface"):
            if flag not in body:
                continue
            try:
                value = parse_bool(body[flag])
            except ValueError as e:
                return reject(str(e))
            if value != before_values[flag]:
                updates[flag] = value

        if "why_remembered" in body:
            raw_why = body["why_remembered"]
            if not isinstance(raw_why, str):
                return reject("why_remembered must be a string")
            why_remembered = raw_why.strip()
            if len(why_remembered) > 500:
                return reject("why_remembered exceeds the 500 character limit")
            if why_remembered != before_values["why_remembered"]:
                updates["why_remembered"] = why_remembered

        if "weight" in body:
            raw_weight = body["weight"]
            if isinstance(raw_weight, bool):
                return reject("weight must be a number from 0.0 to 1.0")
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError):
                return reject("weight must be a number from 0.0 to 1.0")
            if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
                return reject("weight must be a number from 0.0 to 1.0")
            if weight != before_values["weight"]:
                if requested_type != "plan":
                    return reject("weight can only be edited on plan buckets")
                updates["weight"] = weight

        if "content" in body:
            new_content = body["content"]
            if not isinstance(new_content, str) or not new_content.strip():
                return reject("content must be a non-empty string")
            if new_content != before_values["content"]:
                size_err = _check_content_size(new_content)
                if size_err:
                    return reject(size_err)
                updates["content"] = new_content

        current_pinned = before_values["pinned"]
        protected = parse_bool(metadata.get("protected"), default=False)
        requested_pinned = current_pinned
        if "pinned" in body:
            try:
                requested_pinned = parse_bool(body["pinned"])
            except ValueError as e:
                return reject(str(e))

        if logical_letter and requested_type != current_type:
            return reject(
                "letter type cannot be changed from the bucket API",
                status_code=409,
                field="type",
            )
        if logical_letter and requested_pinned != current_pinned:
            return reject(
                "letter pin state cannot be changed from the bucket API",
                status_code=409,
                field="pinned",
            )

        unpinning_now = current_pinned and not requested_pinned and not protected
        if unpinning_now and requested_importance is None:
            return reject(
                "unpinning requires importance from 1 to 10 in the same edit",
                field="importance",
            )

        type_changed = requested_type != current_type
        if type_changed:
            if protected and requested_type != "permanent":
                return reject(
                    "protected buckets cannot change to a non-permanent type",
                    status_code=409,
                    field="type",
                    current_type=current_type,
                    requested_type=requested_type,
                )
            if requested_pinned and requested_type != "permanent":
                return reject(
                    "a bucket cannot be pinned and changed to a non-permanent type "
                    "in the same edit",
                    status_code=409,
                    field="type",
                    requested_type=requested_type,
                )
            if (
                current_pinned
                and not requested_pinned
                and requested_type != "dynamic"
            ):
                return reject(
                    "unpinning a pinned bucket can only change its type to dynamic",
                    status_code=409,
                    field="type",
                    current_type=current_type,
                    requested_type=requested_type,
                )
            # Pass only a real change. BucketManager performs the storage move
            # and rejects/rolls back unsafe or conflicting migrations.  An
            # explicit pinned=False + type=dynamic request is one atomic
            # transition: it must not require a separate unpin save first.
            updates["type"] = requested_type

        # BucketManager 会静默丢弃 pinned/protected 桶的 importance 更新。
        # 在 Web 边界明确拒绝，避免响应把未落盘字段列为 updated。
        if (
            requested_importance is not None
            and (protected or (current_pinned and requested_pinned))
            and requested_importance != before_values["importance"]
        ):
            return reject(
                "pinned/protected buckets lock importance while they remain "
                "pinned/protected",
                status_code=409,
                field="importance",
                locked_importance=before_values["importance"],
            )

        pin_state_changed = requested_pinned != current_pinned
        pinning_now = requested_pinned and not current_pinned
        if pin_state_changed:
            # BucketManager's bucket lock atomically maintains importance=10 and
            # the permanent/dynamic directory transition.
            updates["pinned"] = requested_pinned

        # 新钉选会强制 importance=10，前端同时发来的旧滑杆值不应覆盖它。
        if (
            requested_importance is not None
            and not pinning_now
            and requested_importance != before_values["importance"]
        ):
            updates["importance"] = requested_importance

        if not updates:
            return JSONResponse({
                "ok": True,
                "id": bucket_id,
                "updated": [],
                "unchanged": True,
            })

        try:
            async with AsyncExitStack() as quota_stack:
                if pin_state_changed:
                    await quota_stack.enter_async_context(_quota_turn("pinned"))

                # The route classified quota transitions from the first read.
                # Revalidate pin-relevant state after acquiring the lock so a
                # concurrent edit cannot write from a stale pin snapshot.
                if pin_state_changed:
                    locked_bucket = await sh.bucket_mgr.get(bucket_id)
                    if not locked_bucket:
                        return reject(
                            "bucket changed concurrently; reload and retry",
                            status_code=409,
                            conflict="concurrent_change",
                        )
                    locked_metadata = locked_bucket.get("metadata", {})
                    if not isinstance(locked_metadata, dict):
                        locked_metadata = {}
                    before_quota_state = {
                        "pinned": current_pinned,
                        "protected": protected,
                        "type": current_type,
                        "importance": before_values["importance"],
                        "dont_surface": before_values["dont_surface"],
                    }
                    locked_quota_state = {
                        "pinned": bucket_value(locked_bucket, "pinned"),
                        "protected": parse_bool(
                            locked_metadata.get("protected"), default=False
                        ),
                        "type": bucket_value(locked_bucket, "type"),
                        "importance": bucket_value(locked_bucket, "importance"),
                        "dont_surface": bucket_value(
                            locked_bucket, "dont_surface"
                        ),
                    }
                    changed_quota_fields = [
                        field for field in before_quota_state
                        if before_quota_state[field] != locked_quota_state[field]
                    ]
                    if changed_quota_fields:
                        return reject(
                            "bucket changed concurrently; reload and retry",
                            status_code=409,
                            conflict="concurrent_change",
                            changed_fields=changed_quota_fields,
                        )

                if pinning_now:
                    quota_err = await _check_pinned_quota()
                    if quota_err:
                        return reject(quota_err)

                expected_values = dict(updates)
                if updates.get("pinned") is True:
                    expected_values["importance"] = 10
                    expected_values["type"] = "permanent"
                elif (
                    "pinned" in updates
                    and updates["pinned"] is False
                    and current_pinned
                    and not protected
                ):
                    expected_values["type"] = "dynamic"

                latest_bucket = await sh.bucket_mgr.get(bucket_id)
                if not latest_bucket:
                    return reject(
                        "bucket changed concurrently; reload and retry",
                        status_code=409,
                        conflict="concurrent_change",
                    )
                if letter_lock_state(latest_bucket, "human")["locked"]:
                    return reject(
                        "letter was locked concurrently",
                        status_code=409,
                        conflict="concurrent_lock",
                    )

                ok = await sh.bucket_mgr.update(
                    bucket_id,
                    event_actor="human",
                    **lock_precondition,
                    **updates,
                )
                if not ok:
                    latest = await sh.bucket_mgr.get(bucket_id)
                    if _is_terminal_memory_metadata(
                        (latest or {}).get("metadata", {})
                    ):
                        return reject(
                            "bucket was archived concurrently",
                            status_code=409,
                            conflict="archived",
                        )
                    return reject("update failed", status_code=500)
                persisted_bucket = await sh.bucket_mgr.get(bucket_id)
                if not persisted_bucket:
                    return reject(
                        "updated bucket could not be reloaded", status_code=500
                    )
        except Exception as e:
            return reject(str(e), status_code=500)

        after_values = {
            field: bucket_value(persisted_bucket, field) for field in field_order
        }
        actual_updated = [
            field for field in field_order
            if field in expected_values
            and before_values[field] != after_values[field]
        ]
        not_applied = [
            field for field, expected in expected_values.items()
            if after_values[field] != expected
        ]

        if "content" in actual_updated:
            try:
                sh.dehydrator.invalidate_cache(before_values["content"])
            except Exception:
                pass

        if not_applied:
            return JSONResponse({
                "ok": False,
                "partial": bool(actual_updated),
                "error": "some fields were not persisted: " + ", ".join(not_applied),
                "id": bucket_id,
                "updated": actual_updated,
                "not_applied": not_applied,
            }, status_code=409)

        payload = {
            "ok": True,
            "id": bucket_id,
            "updated": actual_updated,
        }
        return JSONResponse(payload)


    # =============================================================
    # /api/export  — 完整记忆打包导出
    # 导出内容：所有 bucket markdown + SQLite 一致性快照 + meta + SHA-256 清单
    # 不导出 config（避免 api_key 等密钥泄露）
    # export_meta.json 中的 embedding 字段供导入端检查模型一致性。
    # =============================================================
    @mcp.custom_route("/api/export", methods=["GET"])
    async def api_export(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        buckets_dir = sh.config.get("buckets_dir", "")
        if not buckets_dir or not os.path.isdir(buckets_dir):
            return JSONResponse({"error": f"buckets_dir not found: {buckets_dir}"}, status_code=500)

        if not export_lock.acquire(blocking=False):
            return JSONResponse(
                {"error": "已有导出任务正在生成或传输，请稍后重试"},
                status_code=409,
            )
        archive_path = ""

        try:
            emb_backend = getattr(sh.embedding_engine, "_backend", None)
            try:
                emb_dim = int(emb_backend.vector_dim()) if emb_backend else 0
            except Exception:
                emb_dim = 0
            meta: dict = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "version": sh.version,
                "embedding": {
                    "model": str(getattr(sh.embedding_engine, "model", "") or ""),
                    "dim": emb_dim,
                    "backend": str(getattr(sh.embedding_engine, "backend", "") or ""),
                },
            }
            try:
                meta["stats"] = await sh.bucket_mgr.get_stats()
            except Exception as exc:
                logger.warning("export: stats unavailable: %s", exc)

            emb_path = str(getattr(sh.embedding_engine, "db_path", "") or "")
            build_task = asyncio.create_task(
                asyncio.to_thread(
                    build_export_archive_file,
                    buckets_dir,
                    emb_path,
                    meta,
                )
            )
            archive_path, manifest = await _await_export_worker(build_task)
        except BackupArchiveError as e:
            export_lock.release()
            return JSONResponse({"error": f"export failed: {e}"}, status_code=500)
        except asyncio.CancelledError:
            export_lock.release()
            raise
        except Exception as e:
            export_lock.release()
            logger.error("export failed", exc_info=True)
            return JSONResponse({"error": f"export failed: {e}"}, status_code=500)

        fname = f"ombre_export_{int(time.time())}.zip"

        async def cleanup_export() -> None:
            try:
                if archive_path and os.path.exists(archive_path):
                    os.unlink(archive_path)
            finally:
                if export_lock.locked():
                    export_lock.release()

        try:
            return _CleanupFileResponse(
                archive_path,
                media_type="application/zip",
                filename=fname,
                headers={
                    "X-Ombre-Backup-Verified": "true",
                    "X-Ombre-Backup-Files": str(manifest["file_count"]),
                },
                cleanup=cleanup_export,
            )
        except Exception:
            await cleanup_export()
            raise


    # =============================================================
    # /api/migrate/* — 完整记忆包（zip）导入
    # 流程：POST /upload → GET /status（含冲突列表） → POST /apply（带决策）→ 轮询 GET /status
    # =============================================================

    @mcp.custom_route("/api/migrate/upload", methods=["POST"])
    async def api_migrate_upload(request: Request) -> Response:
        """上传 ombre_export_*.zip，解析内容并识别冲突，不实际写入。

        Body: multipart/form-data，字段名 'file'；或直接 POST zip 字节（Content-Type: application/zip）。
        成功返回解析状态（含冲突列表、embedding 模型匹配情况）。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        reservation_id = sh.migrate_engine.reserve_parse()
        if reservation_id is None:
            return JSONResponse({"error": "已有迁移任务正在进行，请等待完成后再上传"}, status_code=409)

        content_type = request.headers.get("content-type", "")
        upload_path = ""
        try:
            if "multipart/form-data" in content_type:
                form = await _read_multipart_form_limited(
                    request, MAX_ARCHIVE_BYTES
                )
                try:
                    file_field = form.get("file")
                    if not file_field or isinstance(file_field, str):
                        raise ValueError("缺少 file 字段")
                    upload_path = await _spool_file_field_limited(
                        file_field, MAX_ARCHIVE_BYTES
                    )
                finally:
                    close = getattr(form, "close", None)
                    if callable(close):
                        await close()
            else:
                upload_path = await _spool_body_limited(request, MAX_ARCHIVE_BYTES)

            if not upload_path or os.path.getsize(upload_path) == 0:
                raise ValueError("文件为空")
        except asyncio.CancelledError:
            sh.migrate_engine.abandon_parse(
                reservation_id,
                "读取上传内容已取消",
            )
            if upload_path:
                try:
                    os.unlink(upload_path)
                except OSError:
                    pass
            raise
        except Exception as e:
            sh.migrate_engine.abandon_parse(reservation_id, f"读取上传内容失败: {e}")
            if upload_path:
                try:
                    os.unlink(upload_path)
                except OSError:
                    pass
            return JSONResponse({"error": f"读取上传内容失败: {e}"}, status_code=400)

        try:
            result = await sh.migrate_engine.parse_zip_file(
                upload_path,
                reservation_id=reservation_id,
            )
        finally:
            try:
                os.unlink(upload_path)
            except OSError:
                pass
        if not result.get("ok"):
            return JSONResponse(result, status_code=409 if result.get("busy") else 422)
        return JSONResponse(result)


    @mcp.custom_route("/api/migrate/status", methods=["GET"])
    async def api_migrate_status(request: Request) -> Response:
        """查询当前迁移任务状态（解析结果、冲突列表、执行进度、重新向量化进度）。"""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(sh.migrate_engine.get_status())


    @mcp.custom_route("/api/migrate/apply", methods=["POST"])
    async def api_migrate_apply(request: Request) -> Response:
        """执行导入，携带冲突决策。

        Body (JSON):
            decisions: {bucket_id: "skip" | "overwrite" | "keep_both"}

        无冲突的 bucket 自动导入，无需出现在 decisions 中。
        冲突但未在 decisions 中的 bucket 默认 skip（安全优先）。
        成功启动后台任务返回 202；任务完成前轮询 GET /api/migrate/status。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        decisions: dict[str, str] = {}
        raw_decisions = body.get("decisions", {})
        if not isinstance(raw_decisions, dict):
            return JSONResponse({"error": "decisions must be an object"}, status_code=400)
        if len(raw_decisions) > 10_000:
            return JSONResponse({"error": "too many migration decisions"}, status_code=400)
        valid_opts = {"skip", "overwrite", "keep_both"}
        for bid, decision in raw_decisions.items():
            if isinstance(bid, str) and isinstance(decision, str) and decision in valid_opts:
                decisions[bid] = decision

        job_id = str(body.get("job_id") or "").strip()
        if not job_id or job_id != sh.migrate_engine.job_id:
            return JSONResponse(
                {
                    "error": "迁移 job_id 已过期或缺失，请重新上传并确认当前解析结果",
                    "current_job_id": sh.migrate_engine.job_id,
                },
                status_code=409,
            )
        reservation_id = sh.migrate_engine.reserve_apply(job_id)
        if reservation_id is None:
            return JSONResponse(
                {
                    "error": f"当前状态为 '{sh.migrate_engine.phase}'，apply 需要先完成 upload 解析（phase=parsed）"
                },
                status_code=409,
            )

        # 后台执行（apply 可能耗时较长，含重新向量化）
        async def _run_apply():
            try:
                await sh.migrate_engine.apply(
                    decisions,
                    reservation_id=reservation_id,
                )
            except Exception as e:
                logger.error(f"[migrate] background apply error: {e}", exc_info=True)

        apply_coro = _run_apply()
        try:
            asyncio.create_task(apply_coro)
        except Exception as exc:
            apply_coro.close()
            abandon = getattr(sh.migrate_engine, "abandon_apply", None)
            if callable(abandon):
                abandon(reservation_id, f"task scheduling failed: {exc}")
            logger.error("[migrate] failed to schedule apply: %s", exc)
            return JSONResponse(
                {"error": "无法调度迁移任务，请重试", "job_id": job_id},
                status_code=503,
            )

        return JSONResponse(
            {
                "ok": True,
                "job_id": job_id,
                "message": "导入任务已启动，请轮询 GET /api/migrate/status 查看进度",
            },
            status_code=202,
        )
