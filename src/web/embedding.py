"""
========================================
web/embedding.py — 向量化后端摘要 / 迁移重算 / 本地 Ollama 模型管理
========================================
- /api/embedding/info、/api/embedding/migrate(+status)、/api/embedding/local/*
- 迁移成功后通过共享发布函数热替换所有 embedding 运行时引用，全局一致。
对外暴露：register(mcp)。
========================================
"""

import asyncio
import contextvars
import functools
import os
import hmac
import httpx
import json as _json_lib
import threading

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from runtime_owner import spawn_background

logger = sh.logger

try:
    from errors import OBStartupError  # type: ignore
except ImportError:  # pragma: no cover
    from ..errors import OBStartupError  # type: ignore

try:
    from embedding_publish import (  # type: ignore
        create_shadow_path,
        embedding_db_turn,
        reserve_migration,
    )
except ImportError:  # pragma: no cover
    from ..embedding_publish import (  # type: ignore
        create_shadow_path,
        embedding_db_turn,
        reserve_migration,
    )


def _persist_embedding_yaml(updates: dict) -> None:
    """把 embedding 配置写进 config.yaml（bind mount，重启/重建不丢）。

    迁移完成后必须调用：否则切到本地/云端只改了进程内 sh.config，重启后 config.yaml
    还是旧的 → 与 embeddings.db 里已重算的向量维度不一致 → OB-W005 / 检索失效。
    走 utils.atomic_update_config_yaml（加锁 + 原子写 + 读回校验），不再是
    「open(w) 整份覆盖、失败只 logger.error」——半份写坏或和其它保存接口并发写
    互相覆盖，都会让这里辛苦写的 dim/backend 悄悄丢回旧值，正是 OB-W005 反复复发的成因。
    """
    try:
        from utils import atomic_update_config_yaml
    except ImportError:  # pragma: no cover - 包模式
        from ..utils import atomic_update_config_yaml

    def _mutate(save_config: dict) -> None:
        sec = save_config.setdefault("embedding", {})
        if not isinstance(sec, dict):
            sec = {}
            save_config["embedding"] = sec
        sec.update(updates)

    # 写失败必须传播给迁移状态机。静默记录后返回会让已失败的配置发布被标记为
    # completed，用户随后重启才发现仍在使用旧模型。
    atomic_update_config_yaml(_mutate)


_DEFAULT_OLLAMA_BASE = "http://ombre-ollama:11434"
# 模型下载镜像前缀（registry）。空 = ollama 官方。国内慢/不通时可换。
_OLLAMA_MIRRORS = {
    "official": "",
    "modelscope": "modelscope.cn/",   # 形如 modelscope.cn/<ns>/bge-m3，需该源确有此模型
}

_ollama_pull_state: dict = {"running": False, "model": "", "percent": 0, "status": "idle", "error": ""}
_ollama_pull_task: "asyncio.Task | None" = None  # 持有引用防止被 GC
_ollama_pull_lock = threading.Lock()
_ollama_pull_owner_guard = threading.Lock()
_ollama_pull_owner: object | None = None
_ollama_pull_request_state: contextvars.ContextVar[dict | None] = (
    contextvars.ContextVar("ombre_ollama_pull_request_state", default=None)
)
_migration_request_state: contextvars.ContextVar[dict | None] = (
    contextvars.ContextVar("ombre_embedding_migration_request_state", default=None)
)

def _reserve_ollama_pull() -> object | None:
    """Atomically reserve the one process-wide Ollama pull slot."""

    global _ollama_pull_owner
    if not _ollama_pull_lock.acquire(blocking=False):
        return None
    owner = object()
    with _ollama_pull_owner_guard:
        _ollama_pull_owner = owner
    return owner


def _owns_ollama_pull(owner: object) -> bool:
    with _ollama_pull_owner_guard:
        return _ollama_pull_owner is owner


def _release_ollama_pull(owner: object) -> bool:
    global _ollama_pull_owner
    with _ollama_pull_owner_guard:
        if _ollama_pull_owner is not owner:
            return False
        _ollama_pull_owner = None
        _ollama_pull_lock.release()
    return True


def _with_migration_reservation(handler):
    """Reserve the cross-process migration slot before the first request await."""

    @functools.wraps(handler)
    async def _wrapped(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err
        db_path = str(getattr(sh.embedding_engine, "db_path", "") or "")
        if not db_path:
            return JSONResponse(
                {"ok": False, "error": "live embedding database path is unavailable"},
                status_code=500,
            )
        reservation = reserve_migration(db_path)
        if reservation is None:
            return JSONResponse(
                {"ok": False, "error": "另一个跨进程迁移正在进行"},
                status_code=409,
            )

        state = {"reservation": reservation, "transferred": False}
        context_token = _migration_request_state.set(state)
        try:
            return await handler(request)
        finally:
            _migration_request_state.reset(context_token)
            if not state["transferred"]:
                reservation.close()

    return _wrapped


def _with_ollama_pull_reservation(handler):
    """Reserve the Ollama pull slot before parsing the request body."""

    @functools.wraps(handler)
    async def _wrapped(request: Request) -> Response:
        from starlette.responses import JSONResponse

        err = sh._require_auth(request)
        if err:
            return err
        owner = _reserve_ollama_pull()
        if owner is None:
            return JSONResponse(
                {"ok": False, "error": "已有拉取任务在进行中"},
                status_code=409,
            )

        global _ollama_pull_state
        _ollama_pull_state = {
            "running": True,
            "model": "",
            "percent": 0,
            "status": "validating",
            "error": "",
        }
        state = {"owner": owner, "transferred": False}
        context_token = _ollama_pull_request_state.set(state)
        try:
            return await handler(request)
        finally:
            _ollama_pull_request_state.reset(context_token)
            if not state["transferred"] and _owns_ollama_pull(owner):
                _ollama_pull_state["running"] = False
                _release_ollama_pull(owner)

    return _wrapped

# --- backfill（只补缺失向量，区别于 migrate 全库重算）---
# 用途：v2.2 前建的桶（尤其 permanent）可能没有向量，
# embeddings.db 里没有它们的行 → breath 语义检索查不到。migrate 能修但会重算全库、
# 浪费 API 额度；backfill 只给「文件在、向量缺」的桶补一发，幂等、便宜。
_backfill_state: dict = {
    "running": False, "scanned": 0, "missing": 0, "done": 0,
    "failed": 0, "queued": 0, "status": "idle", "error": "",
}
_backfill_task: "asyncio.Task | None" = None  # 持有引用防止被 GC


def _ollama_base() -> str:
    """Ollama 管理 API 根地址（不带 /v1）。

    取值优先级：env OMBRE_OLLAMA_URL > 按宿主类型默认。
    Docker 里默认连同网络的 ombre-ollama 容器；裸机/原生默认本机 127.0.0.1
    （否则原生用户拉模型会去连一个不存在的容器名，静默失败）。
    """
    raw = (os.environ.get("OMBRE_OLLAMA_URL", "") or "").strip()
    if not raw:
        raw = _DEFAULT_OLLAMA_BASE if sh.in_docker() else "http://127.0.0.1:11434"
    return raw.rstrip("/").removesuffix("/v1").rstrip("/")


async def _ollama_pull_run(
    ollama_url: str,
    name: str,
    *,
    reservation: object | None = None,
) -> None:
    """后台流式拉模型，进度写入 _ollama_pull_state。"""
    global _ollama_pull_state
    owner = reservation or _reserve_ollama_pull()
    if owner is None or not _owns_ollama_pull(owner):
        logger.info("[ollama] another model pull already owns the slot; skip")
        return
    _ollama_pull_state = {"running": True, "model": name, "percent": 0, "status": "starting", "error": ""}
    try:
        # trust_env=False：本地/容器 ollama 不走系统代理（否则 Clash/V2Ray 开着会 502）
        # Model pulls are long-running streams, so the read phase stays
        # unbounded while connect/write/pool waits remain finite.
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            async with c.stream("POST", f"{ollama_url}/api/pull", json={"name": name, "stream": True}) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _ollama_pull_state.update(running=False, status="error",
                                              error=f"HTTP {r.status_code}: {raw[:200].decode('utf-8','replace')}")
                    return
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = _json_lib.loads(line)
                    except Exception:
                        continue
                    if ev.get("error"):
                        _ollama_pull_state.update(running=False, status="error", error=str(ev["error"])[:200])
                        return
                    st = str(ev.get("status", ""))
                    _ollama_pull_state["status"] = st
                    total, completed = ev.get("total"), ev.get("completed")
                    if total and completed:
                        try:
                            _ollama_pull_state["percent"] = round(completed / total * 100, 1)
                        except Exception:
                            pass
                    if st == "success":
                        _ollama_pull_state.update(running=False, status="success", percent=100)
                        return
        _ollama_pull_state["running"] = False
    except asyncio.CancelledError:
        _ollama_pull_state.update(running=False, status="cancelled")
        raise
    except Exception as e:
        _ollama_pull_state.update(running=False, status="error", error=str(e)[:200])
    finally:
        if _owns_ollama_pull(owner):
            _ollama_pull_state["running"] = False
            _release_ollama_pull(owner)


async def _backfill_run() -> None:
    """后台扫全库（含 archive），给缺向量的桶补 embedding，进度写 _backfill_state。

    每补一条更新计数；失败只累加 failed、不中断（rule.md §1.5 允许降级）。
    分批间小睡，照顾云端免费额度的速率限制。"""
    import asyncio as _aio
    global _backfill_state
    engine = sh.embedding_engine
    try:
        all_buckets = await sh.bucket_mgr.list_all(include_archive=True)
        _backfill_state["scanned"] = len(all_buckets)
        for key in ("orphaned", "cleaned", "cleanup_failed"):
            _backfill_state.setdefault(key, 0)

        # Reconcile both sides of the derived index.  Historically this action
        # only queued "bucket exists, vector missing" rows, while diagnostics
        # also reported the opposite drift (vector exists, bucket missing).
        # That made the Dashboard recommend backfill for orphan vectors and
        # then report "pending 0 / queued 0" forever.
        known_ids = {
            str(bucket.get("id") or "")
            for bucket in all_buckets
            if str(bucket.get("id") or "")
        }
        try:
            indexed_ids = set(engine.list_all_ids()) if engine else set()
        except Exception as exc:
            logger.warning("[backfill] could not list indexed ids: %s", exc)
            indexed_ids = set()
        orphan_candidates = sorted(indexed_ids - known_ids)
        orphan_ids: list[str] = []
        for bucket_id in orphan_candidates:
            try:
                # ``all_buckets`` is only a snapshot. A concurrent hold may
                # publish and index a new bucket after that scan; never delete
                # its vector without confirming the Markdown is still absent.
                if await sh.bucket_mgr.get(bucket_id) is not None:
                    continue
                orphan_ids.append(bucket_id)
            except Exception as exc:
                _backfill_state["cleanup_failed"] += 1
                logger.warning(
                    "[backfill] orphan confirmation failed for %s: %s",
                    bucket_id,
                    exc,
                )
        _backfill_state["orphaned"] = len(orphan_ids)
        for bucket_id in orphan_ids:
            try:
                engine.delete_embedding(bucket_id)
                _backfill_state["cleaned"] += 1
            except Exception as exc:
                _backfill_state["cleanup_failed"] += 1
                logger.warning("[backfill] orphan cleanup failed for %s: %s", bucket_id, exc)

        # Managed server runtimes have one durable writer for the derived
        # index. Reuse it so manual backfill, startup reconciliation, and
        # decay self-healing cannot race each other or bypass retry state.
        outbox = sh.embedding_outbox
        if outbox is not None and getattr(outbox, "running", False):
            queued = await outbox.reconcile(
                buckets=all_buckets,
                include_archive=True,
            )
            outbox.retry_now()
            queue_state = outbox.status()
            _backfill_state.update(
                missing=queue_state["pending"],
                failed=queue_state["retrying"],
                queued=queued,
                status="queued",
            )
            return

        # 先扫出缺向量的桶（空内容的跳过——没法向量化）
        missing: list[tuple[str, str]] = []
        for b in all_buckets:
            content = b.get("content", "")
            if not content or not content.strip():
                continue
            if await engine.get_embedding(b["id"]) is None:
                missing.append((b["id"], content))
        _backfill_state["missing"] = len(missing)
        _backfill_state["status"] = "embedding"

        for idx, (bid, content) in enumerate(missing):
            try:
                ok = await engine.generate_and_store(bid, content)
                if ok:
                    _backfill_state["done"] += 1
                else:
                    _backfill_state["failed"] += 1
            except Exception as e:
                _backfill_state["failed"] += 1
                logger.warning(f"[backfill] embed failed for {bid}: {e}")
            # 每 20 条小憩一下，避免打爆云端速率限制
            if (idx + 1) % 20 == 0:
                await _aio.sleep(2)

        _backfill_state["status"] = "done"
    except Exception as e:
        _backfill_state["status"] = "error"
        _backfill_state["error"] = str(e)[:200]
        logger.error(f"[backfill] run failed: {e}")
    finally:
        _backfill_state["running"] = False


def register(mcp) -> None:

    @mcp.custom_route("/api/embedding/info", methods=["GET"])
    async def api_embedding_info(request: Request) -> Response:
        """返回当前 embedding 后端的运行态摘要：backend / model / dim / enabled / db 状态。

        前端设置页用这个渲染「当前模型」面板。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        backend_obj = getattr(sh.embedding_engine, "_backend", None)
        info: dict[str, object] = {
            "ok": True,
            "backend": getattr(sh.embedding_engine, "backend", ""),
            "api_format": getattr(sh.embedding_engine, "api_format", ""),
            "enabled": bool(getattr(sh.embedding_engine, "enabled", False)),
            "model": backend_obj.model_name() if backend_obj else "",
            "vector_dim": backend_obj.vector_dim() if backend_obj else 0,
            "db_path": getattr(sh.embedding_engine, "db_path", ""),
            "db_count": 0,
            "db_meta": {},
            "outbox": (
                sh.embedding_outbox.status()
                if sh.embedding_outbox is not None
                else None
            ),
        }
        # 主表行数
        try:
            import sqlite3
            if info["db_path"] and os.path.exists(str(info["db_path"])):
                with embedding_db_turn(str(info["db_path"])):
                    conn = sqlite3.connect(str(info["db_path"]))
                    try:
                        info["db_count"] = conn.execute(
                            "SELECT COUNT(*) FROM embeddings"
                        ).fetchone()[0]
                        rows = conn.execute(
                            "SELECT key, value FROM embeddings_meta"
                        ).fetchall()
                        info["db_meta"] = {k: v for k, v in rows}
                    finally:
                        conn.close()
        except Exception as e:
            info["db_error"] = str(e)
        return JSONResponse(info)

    @mcp.custom_route("/api/embedding/migrate", methods=["POST"])
    @_with_migration_reservation
    async def api_embedding_migrate(request: Request) -> Response:
        """启动后台迁移任务：用目标后端重算所有 bucket 的 embedding。

        Body (JSON):
            target_backend: 'api' | 'gemini' | 'local' | 'ollama'（底层都映射到 backend=api）
            api_format:     可选 'gemini' | 'openai_compat' | 'ollama'
            api_key:        云端必填；本地（ollama）可空，引擎会补占位符
            base_url:       可选
            model:          可选

        成功启动返回 202，body 含 {ok, status_path}；
        已有任务在跑返回 409。
        """
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse(
                {"ok": False, "error": "JSON body must be an object"},
                status_code=400,
            )

        def _safe_text(name: str, *, limit: int) -> str:
            value = body.get(name, "")
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            normalized = value.strip()
            if len(normalized) > limit:
                raise ValueError(f"{name} is too long")
            if any(ord(char) < 32 for char in normalized):
                raise ValueError(f"{name} contains control characters")
            return normalized

        try:
            target_backend_raw = _safe_text("target_backend", limit=32).lower()
            req_api_format = _safe_text("api_format", limit=64).lower()
            requested_key = _safe_text("api_key", limit=8192)
            requested_base_url = _safe_text("base_url", limit=2048)
            requested_model = _safe_text("model", limit=512)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        # local/ollama 底层也是 openai_compat（backend=api），用 api_format 区分云端/本地
        target_backend = "api" if target_backend_raw in ("api", "gemini", "local", "ollama", "") else target_backend_raw
        if target_backend != "api":
            return JSONResponse({
                "ok": False,
                "error": f"target_backend 不支持：{target_backend_raw!r}",
            }, status_code=400)

        # 解析目标 api_format：显式传入优先；否则按 target_backend 推断
        if not req_api_format:
            if target_backend_raw in ("local", "ollama"):
                req_api_format = "ollama"
            elif target_backend_raw == "gemini":
                req_api_format = "gemini"
        if req_api_format not in ("", "gemini", "openai_compat", "ollama", "local"):
            return JSONResponse(
                {"ok": False, "error": "api_format 不受支持"},
                status_code=400,
            )

        try:
            from migration_engine import (  # type: ignore
                MigrationConfig, start_migration, is_running,
                status_path_for as _mig_status_path_for,
            )
        except ImportError:
            from ..migration_engine import (  # type: ignore
                MigrationConfig, start_migration, is_running,
                status_path_for as _mig_status_path_for,
            )

        if is_running():
            return JSONResponse({
                "ok": False,
                "error": "另一个迁移任务正在进行；请稍后再试或等其完成",
            }, status_code=409)

        buckets_dir = sh.config.get("buckets_dir", "buckets")
        db_path = str(getattr(sh.embedding_engine, "db_path", "") or "")
        request_state = _migration_request_state.get()
        if request_state is None:  # pragma: no cover - decorator invariant
            raise RuntimeError("migration route lost its reservation")
        reservation = request_state["reservation"]

        shadow_path = None
        task_started = False
        try:
            shadow_path = create_shadow_path(db_path, reservation=reservation)

            # E-MIG-01 manifest must never contain a provider key or its inverse.
            # A one-off different key would make crash recovery unable to
            # reconstruct the old config without persisting a secret. Require
            # callers to establish the effective key through the existing
            # credential route before migration.
            current_embedding = sh.config.get("embedding", {}) or {}
            current_key = str(
                current_embedding.get("api_key")
                or os.environ.get("OMBRE_EMBED_API_KEY", "")
                or ""
            ).strip()
            if requested_key and (
                not current_key or not hmac.compare_digest(requested_key, current_key)
            ):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            "E-MIG-01 不在迁移事务中更换或持久化 API key；"
                            "请先通过凭据配置入口保存目标 key，再启动迁移"
                        ),
                    },
                    status_code=400,
                )
            # 构造目标引擎（不替换 global，跑完并完成可补偿发布后才替）。
            # db_path is an internally generated same-volume shadow leaf.
            target_cfg = _json_lib.loads(_json_lib.dumps(sh.config))
            target_emb_cfg = target_cfg.setdefault("embedding", {})
            target_emb_cfg["enabled"] = True
            target_emb_cfg["backend"] = target_backend
            target_emb_cfg["db_path"] = str(shadow_path)
            if req_api_format:
                target_emb_cfg["api_format"] = req_api_format
            if current_key:
                target_emb_cfg["api_key"] = current_key
            if "base_url" in body:
                target_emb_cfg["base_url"] = requested_base_url
            if requested_model:
                target_emb_cfg["model"] = requested_model

            try:
                from embedding_engine import EmbeddingEngine  # type: ignore
            except ImportError:
                from ..embedding_engine import EmbeddingEngine
            try:
                target_engine = EmbeddingEngine(target_cfg)
            except OBStartupError as oe:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"目标引擎构造失败：{oe.error_code}",
                    },
                    status_code=400,
                )
            except Exception as exc:
                logger.warning(
                    "[migration] target engine construction failed: %s",
                    type(exc).__name__,
                )
                return JSONResponse(
                    {"ok": False, "error": "目标引擎构造失败"},
                    status_code=400,
                )

            target_backend_obj = getattr(target_engine, "_backend", None)
            if target_backend_obj is None or not getattr(target_engine, "enabled", False):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": (
                            "目标 embedding 引擎不可用；"
                            "请先确认凭据、模型和本地服务状态"
                        ),
                    },
                    status_code=400,
                )

            # Provider probe only touches the private shadow engine configuration.
            # It does not open or mutate the live embeddings.db.
            try:
                probe = await target_engine._generate_async(
                    "connectivity probe / 连接性探针"
                )
            except Exception as exc:
                logger.warning(
                    "[migration] target provider probe failed: %s",
                    type(exc).__name__,
                )
                probe = []
            if not probe:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "目标后端嵌入测试失败，已取消；生产向量库未触碰",
                    },
                    status_code=400,
                )

            async def _fetch_buckets() -> list[tuple[str, str]]:
                all_buckets = await sh.bucket_mgr.list_all(include_archive=True)
                return [
                    (str(bucket["id"]), str(bucket.get("content") or ""))
                    for bucket in all_buckets
                    if str(bucket.get("content") or "").strip()
                ]

            try:
                from utils import config_file_path
            except ImportError:  # pragma: no cover
                from ..utils import config_file_path

            runtime_state: dict[str, object] = {
                "snapshot": None,
                "applied": False,
                "old_embedding_config": _json_lib.loads(
                    _json_lib.dumps(sh.config.get("embedding", {}) or {})
                ),
            }

            def _runtime_close() -> None:
                # EmbeddingEngine owns no persistent sqlite3.Connection. The
                # exclusive publish gate has already drained every leased
                # provider/read/write operation before this callback runs.
                return None

            def _runtime_apply() -> None:
                # The provider may discover/correct its effective model or
                # dimension only after generating the shadow.  The migration
                # core persists those actual values; publish the same values
                # into the in-process config instead of the route-time probe.
                published_backend = getattr(target_engine, "_backend", None)
                if published_backend is not None:
                    forward_patch["model"] = str(
                        published_backend.model_name() or forward_patch["model"]
                    )
                    forward_patch["dim"] = int(
                        published_backend.vector_dim() or forward_patch["dim"]
                    )
                target_engine.db_path = db_path
                live_embedding_config = sh.config.setdefault("embedding", {})
                if not isinstance(live_embedding_config, dict):
                    raise RuntimeError("runtime embedding config is not mutable")
                for key, value in forward_patch.items():
                    if value is None:
                        live_embedding_config.pop(key, None)
                    else:
                        live_embedding_config[key] = value
                runtime_state["snapshot"] = sh.publish_embedding_runtime(
                    target_engine
                )
                runtime_state["applied"] = True

            def _runtime_restore() -> None:
                snapshot = runtime_state.get("snapshot")
                if snapshot is not None:
                    sh.restore_embedding_runtime(snapshot)  # type: ignore[arg-type]
                previous = runtime_state["old_embedding_config"]
                live_embedding_config = sh.config.setdefault("embedding", {})
                if not isinstance(live_embedding_config, dict):
                    raise RuntimeError("runtime embedding config rollback is not mutable")
                live_embedding_config.clear()
                live_embedding_config.update(previous)  # type: ignore[arg-type]
                runtime_state["applied"] = False

            def _runtime_probe() -> None:
                selected = (
                    target_engine
                    if runtime_state.get("applied")
                    else sh.embedding_engine
                )
                if os.path.abspath(str(getattr(selected, "db_path", ""))) != os.path.abspath(db_path):
                    raise RuntimeError("runtime embedding engine points to a non-live DB")
                backend = getattr(selected, "_backend", None)
                if runtime_state.get("applied") and backend is not None:
                    if int(backend.vector_dim() or 0) <= 0:
                        raise RuntimeError("runtime embedding dimension is invalid")
                    live_embedding_config = sh.config.get("embedding", {})
                    if not isinstance(live_embedding_config, dict):
                        raise RuntimeError("runtime embedding config is not a mapping")
                    for key, value in forward_patch.items():
                        if live_embedding_config.get(key) != value:
                            raise RuntimeError(
                                f"runtime embedding config did not publish {key}"
                            )

            initial_model = str(target_backend_obj.model_name() or "")
            initial_dim = int(target_backend_obj.vector_dim() or len(probe))
            forward_patch: dict[str, object] = {
                "backend": target_backend,
                "enabled": True,
                "model": initial_model,
                "dim": initial_dim,
            }
            if req_api_format:
                forward_patch["api_format"] = req_api_format
            if "base_url" in body:
                forward_patch["base_url"] = requested_base_url
            mig_cfg = MigrationConfig(
                buckets_dir=buckets_dir,
                db_path=db_path,
                target_backend=target_backend,
                target_model=initial_model,
                target_dim=initial_dim,
                target_engine=target_engine,
                fetch_buckets=_fetch_buckets,
                config_path=config_file_path(),
                config_forward_patch=forward_patch,
                runtime_close=_runtime_close,
                runtime_apply=_runtime_apply,
                runtime_restore=_runtime_restore,
                runtime_open_probe=_runtime_probe,
            )

            task = start_migration(mig_cfg, reservation=reservation)
            if task is None:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "无法启动迁移任务（迁移 reservation 未获得）",
                    },
                    status_code=409,
                )
            task_started = True
            request_state["transferred"] = True
            return JSONResponse(
                {
                    "ok": True,
                    "accepted": True,
                    "completed": False,
                    "status_path": _mig_status_path_for(buckets_dir),
                    "target_backend": target_backend,
                    "txid": reservation.txid,
                    "message": "迁移已接受；最终结果请读取 status，202 不代表发布完成",
                },
                status_code=202,
            )
        finally:
            if not task_started:
                reservation.close()
                if shadow_path is not None:
                    for candidate in (
                        shadow_path,
                        shadow_path.with_name(shadow_path.name + "-wal"),
                        shadow_path.with_name(shadow_path.name + "-shm"),
                        shadow_path.with_name(shadow_path.name + "-journal"),
                    ):
                        try:
                            if candidate.exists():
                                candidate.unlink()
                        except OSError:
                            pass

        # Unreachable; every path above returns a response.
        return JSONResponse({"ok": False, "error": "migration setup failed"}, status_code=500)

    @mcp.custom_route("/api/embedding/migrate/status", methods=["GET"])
    async def api_embedding_migrate_status(request: Request) -> Response:
        """前端 3s 轮询：当前迁移任务状态。"""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            from migration_engine import (  # type: ignore
                status_path_for as _mig_status_path_for,
                read_status as _mig_read_status,
                is_running,
            )
        except ImportError:
            from ..migration_engine import (  # type: ignore
                status_path_for as _mig_status_path_for,
                read_status as _mig_read_status,
                is_running,
            )
        buckets_dir = sh.config.get("buckets_dir", "buckets")
        status = _mig_read_status(_mig_status_path_for(buckets_dir))
        return JSONResponse({"ok": True, "running": is_running(), "status": status})

    @mcp.custom_route("/api/embedding/backfill", methods=["POST"])
    async def api_embedding_backfill(request: Request) -> Response:
        """补齐缺失向量：只给 embeddings.db 里没有行的桶生成 embedding。

        与 /api/embedding/migrate 的区别：migrate 用（可能是新的）后端重算**全库**，
        backfill 只扫出「文件在、向量缺」的桶补一发，不动已有向量，便宜且幂等。
        典型场景：v2.2 前建的 permanent 桶让 breath 语义检索查不到。

        成功启动返回 202 + {ok, status_path}；已有 backfill/migrate 在跑返回 409。
        """
        from starlette.responses import JSONResponse
        global _backfill_task, _backfill_state
        err = sh._require_auth(request)
        if err:
            return err

        engine = sh.embedding_engine
        managed_outbox = bool(
            sh.embedding_outbox is not None
            and getattr(sh.embedding_outbox, "running", False)
        )
        if (not engine or not getattr(engine, "enabled", False)) and not managed_outbox:
            return JSONResponse({
                "ok": False,
                "error": "向量化未启用（缺 key / 本地模型未就绪），无法补齐。",
            }, status_code=400)

        # 与全库重算互斥：同时写 embeddings.db 会打架
        try:
            from migration_engine import is_running as _mig_running  # type: ignore
        except ImportError:
            try:
                from ..migration_engine import is_running as _mig_running  # type: ignore
            except Exception:
                _mig_running = lambda: False  # noqa: E731
        if _mig_running():
            return JSONResponse({
                "ok": False,
                "error": "全库重算正在进行，请等它完成再补齐。",
            }, status_code=409)
        if _backfill_state.get("running"):
            return JSONResponse({
                "ok": False, "error": "已有补齐任务在进行中。",
            }, status_code=409)

        _backfill_state = {
            "running": True, "scanned": 0, "missing": 0, "done": 0,
            "failed": 0, "queued": 0, "orphaned": 0, "cleaned": 0,
            "cleanup_failed": 0, "status": "scanning", "error": "",
        }
        _backfill_task = spawn_background(_backfill_run())
        return JSONResponse({
            "ok": True,
            "status_path": "/api/embedding/backfill/status",
        }, status_code=202)

    @mcp.custom_route("/api/embedding/backfill/status", methods=["GET"])
    async def api_embedding_backfill_status(request: Request) -> Response:
        """前端轮询：当前补齐任务进度。"""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        outbox_state = (
            sh.embedding_outbox.status()
            if sh.embedding_outbox is not None
            else None
        )
        return JSONResponse({
            "ok": True,
            "backfill": _backfill_state,
            "outbox": outbox_state,
        })

    @mcp.custom_route("/api/embedding/local/status", methods=["GET"])
    async def api_embedding_local_status(request: Request) -> Response:
        """本地 ollama 是否可达 + 已有模型列表 + 目标模型是否就绪。"""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        want = (request.query_params.get("model") or "bge-m3").strip()
        base = _ollama_base()
        out = {"ok": True, "ollama_url": base, "reachable": False, "models": [], "has_model": False, "mirrors": list(_OLLAMA_MIRRORS.keys())}
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
                r = await c.get(f"{base}/api/tags")
                r.raise_for_status()
                names = [m.get("name", "") for m in r.json().get("models", [])]
                out["reachable"] = True
                out["models"] = names
                # ollama 模型名常带 :latest 后缀
                out["has_model"] = any(n == want or n.split(":")[0] == want for n in names)
        except Exception as e:
            out["error"] = str(e)[:160]
        out["pull"] = _ollama_pull_state
        return JSONResponse(out)

    @mcp.custom_route("/api/embedding/local/pull", methods=["POST"])
    @_with_ollama_pull_reservation
    async def api_embedding_local_pull(request: Request) -> Response:
        """触发后台拉模型。body: {model?: 'bge-m3', mirror?: 'official'|'modelscope'|<自定义前缀>}。"""
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "JSON body must be an object"}, status_code=400)
        if any(
            key in body and not isinstance(body[key], str)
            for key in ("model", "mirror")
        ):
            return JSONResponse({"ok": False, "error": "model and mirror must be strings"}, status_code=400)
        model = (str(body.get("model") or "bge-m3")).strip()
        mirror_raw = (str(body.get("mirror") or "official")).strip()
        if len(model) > 512 or len(mirror_raw) > 2048:
            return JSONResponse({"ok": False, "error": "model or mirror is too large"}, status_code=400)
        prefix = _OLLAMA_MIRRORS.get(mirror_raw, mirror_raw if mirror_raw not in ("", "official") else "")
        name = f"{prefix}{model}" if prefix else model
        base = _ollama_base()
        _ollama_pull_state.update(model=name, status="checking")
        # 可达性预检，避免后台任务静默失败
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
                vr = await c.get(f"{base}/api/version")
                vr.raise_for_status()
        except Exception as e:
            _ollama_pull_state.update(
                running=False,
                status="error",
                error=str(e)[:200],
            )
            return JSONResponse({"ok": False, "error": f"无法连接 ollama（{base}）：{str(e)[:120]}"}, status_code=502)
        global _ollama_pull_task
        request_state = _ollama_pull_request_state.get()
        if request_state is None:  # pragma: no cover - decorator invariant
            raise RuntimeError("Ollama pull route lost its reservation")
        _ollama_pull_task = spawn_background(
            _ollama_pull_run(
                base,
                name,
                reservation=request_state["owner"],
            )
        )
        request_state["transferred"] = True
        return JSONResponse({"ok": True, "started": True, "pulling": name})

    @mcp.custom_route("/api/embedding/local/pull/status", methods=["GET"])
    async def api_embedding_local_pull_status(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({"ok": True, "pull": _ollama_pull_state})
