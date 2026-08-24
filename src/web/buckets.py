"""
========================================
web/buckets.py — 记忆桶管理 + 设置 + 锚点 + 自我认知读取
========================================

仪表板「记忆」页的后端：列表/详情、pin/resolve/archive/forget、批量遗忘、
采样与 human 名设置（持久化 config.yaml）、锚点、/api/self。
应用层只允许归档/淡忘，不提供物理删除记忆桶的能力。

对外暴露：register(mcp)。
========================================
"""

import math
import threading
import unicodedata
from contextlib import AsyncExitStack

from starlette.requests import Request
from starlette.responses import Response

from ombrebrain.domain.memory_messages import resolved_hint
from . import _shared as sh

logger = sh.logger

try:
    from utils import (  # type: ignore
        atomic_update_config_yaml,
        parse_bool,
        parse_iso_datetime,
        strip_wikilinks,
    )
except ImportError:  # pragma: no cover
    from ..utils import (  # type: ignore
        atomic_update_config_yaml,
        parse_bool,
        parse_iso_datetime,
        strip_wikilinks,
    )

try:
    from tools._common import (  # type: ignore
        _quota_turn,
        check_pinned_quota as _check_pinned_quota,
        enforce_high_importance_quota as _enforce_high_importance_quota,
        is_terminal_memory_metadata as _is_terminal_memory_metadata,
        occupies_high_importance_quota_slot as _occupies_high_importance_slot,
    )
except ImportError:  # pragma: no cover
    from ..tools._common import (  # type: ignore
        _quota_turn,
        check_pinned_quota as _check_pinned_quota,
        enforce_high_importance_quota as _enforce_high_importance_quota,
        is_terminal_memory_metadata as _is_terminal_memory_metadata,
        occupies_high_importance_quota_slot as _occupies_high_importance_slot,
    )


def _datetime_epoch_ms(value) -> int | None:
    """Return one server-normalized instant for Dashboard sorting/display."""
    try:
        return round(parse_iso_datetime(value).timestamp() * 1000)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


async def rename_human_in_buckets(old: str, new: str) -> dict:
    """把所有桶里字面量 `old` 正则替换成 `new`（name / content / why_remembered /
    letter user_name 四处都换）。

    用途：她/他改了称呼后，改名前就存在的老桶仍写着旧词（默认「用户」），breath 里
    新桶显示新名、老桶还是旧名，看起来"批量替换没生效"。这里一次性补齐。

    每个桶都走 BucketManager 的正常事务边界，但不刷新 ``last_active``；
    content 改变时派生索引也会按普通更新流程重建。

    返回 {buckets_changed, replacements}。old 为空 / old==new 时直接 no-op。"""
    result = await sh.bucket_mgr.replace_text_fields(old, new)
    logger.info(
        "rename_human_in_buckets: %r->%r changed=%s replacements=%s",
        old,
        new,
        result["buckets_changed"],
        result["replacements"],
    )
    return result


def register(mcp) -> None:
    # 每次路由注册使用一个原生提交锁，不与 asyncio 事件循环绑定；临界区内
    # 不执行 await，因此既能跨测试事件循环串行化，也不会因持锁等待而死锁。
    sampling_commit_lock = threading.Lock()

    @mcp.custom_route("/api/buckets", methods=["GET"])
    async def api_buckets(request: Request) -> Response:
        """List buckets, optionally ordered by their first-recorded time."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        sort_mode = str(request.query_params.get("sort", "score") or "score").strip()
        allowed_sort_modes = {"score", "created_desc", "created_asc"}
        if sort_mode not in allowed_sort_modes:
            return JSONResponse(
                {
                    "error": "invalid sort mode",
                    "allowed": sorted(allowed_sort_modes),
                },
                status_code=400,
            )
        try:
            all_buckets = await sh.bucket_mgr.list_all(include_archive=True)
            result = []
            for b in all_buckets:
                meta = b.get("metadata", {})
                if meta.get("deleted_at"):
                    continue
                created_epoch_ms = _datetime_epoch_ms(meta.get("created"))
                last_active_epoch_ms = _datetime_epoch_ms(meta.get("last_active"))
                result.append({
                    "id": b["id"],
                    "name": meta.get("name", b["id"]),
                    "type": meta.get("type", "dynamic"),
                    "domain": meta.get("domain", []),
                    "tags": meta.get("tags", []),
                    "valence": meta.get("valence", 0.5),
                    "arousal": meta.get("arousal", 0.3),
                    "model_valence": meta.get("model_valence"),
                    "importance": meta.get("importance", 5),
                    "resolved": meta.get("resolved", False),
                    "pinned": meta.get("pinned", False),
                    "digested": meta.get("digested", False),
                    "imported": parse_bool(meta.get("imported"), default=False)
                    or str(meta.get("source_tool") or "").strip() == "import",
                    "created": meta.get("created", ""),
                    "created_epoch_ms": created_epoch_ms,
                    "last_active": meta.get("last_active", ""),
                    "last_active_epoch_ms": last_active_epoch_ms,
                    "activation_count": meta.get("activation_count", 0),
                    "score": sh.decay_engine.calculate_score(meta),
                    "content_preview": strip_wikilinks(b.get("content", ""))[:200],
                    # iter 1.8 新增字段（后台老桶读出默认值）
                    "why_remembered": meta.get("why_remembered", ""),
                    "dont_surface": bool(meta.get("dont_surface", False)),
                    "first_of_kind": bool(meta.get("first_of_kind", False)),
                    "weight": meta.get("weight"),  # plan 专有，非 plan 为 None
                    "triggered_by": meta.get("triggered_by", ""),
                    "erasable_test_data": bool(
                        isinstance(meta.get("provenance"), dict)
                        and meta["provenance"].get("kind") == "test"
                        and meta["provenance"].get("erasable") is True
                    ),
                })
            if sort_mode == "score":
                # Preserve the long-standing Dashboard default while making
                # equal-score ordering deterministic across os.walk refreshes.
                result.sort(key=lambda item: (-float(item["score"]), str(item["id"])))
            else:
                descending = sort_mode == "created_desc"

                def created_sort_key(item: dict) -> tuple[int, int, str]:
                    timestamp = item["created_epoch_ms"]
                    if timestamp is None:
                        # Legacy/hand-written buckets can lack a valid created
                        # value. Unknown times always stay at the end, not at
                        # an arbitrary extreme of either chronological view.
                        return (1, 0, str(item["id"]))
                    ordered_timestamp = -timestamp if descending else timestamp
                    return (0, ordered_timestamp, str(item["id"]))

                result.sort(key=created_sort_key)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
    async def api_bucket_detail(request: Request) -> Response:
        """Get full raw bucket content plus display-only derived text by ID.

        A trusted sidecar may use its service token on this exact read route so
        Xinchao can inspect every bucket type. Dashboard sessions remain valid,
        while all bucket mutation routes keep requiring Dashboard auth.
        """
        from starlette.responses import JSONResponse
        err = sh._require_service_or_dashboard_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        meta = bucket.get("metadata", {})
        # iter 1.9 D / iter 2.0 §10 U-04: 反向链——只扫 feel_dir，O(feel桶数) 而非全库扫描
        triggered_feels = []
        try:
            triggered_feels = await sh.bucket_mgr.get_triggered_feels(bucket_id)
        except Exception as e:
            logger.warning(f"triggered_feels lookup failed / 反向链查询失败: {e}")
        raw_content = bucket.get("content", "")
        return JSONResponse({
            "id": bucket["id"],
            "metadata": meta,
            # Editing must round-trip the exact stored Markdown.  Keep the
            # bracket-free presentation text separate so a metadata-only edit
            # can never write a stripped [[wikilink]] body back to disk.
            "content": raw_content,
            "display_content": strip_wikilinks(raw_content),
            "score": sh.decay_engine.calculate_score(meta),
            "triggered_feels": triggered_feels,  # iter 1.9 D
        })


    # ---- Bucket-level mutation endpoints (iter 1.4) ----
    # 桶维度变更端点：钉选/解钉、resolve toggle、归档、删除到档案
    @mcp.custom_route("/api/bucket/{bucket_id}/pin", methods=["POST"])
    async def api_bucket_pin(request: Request) -> Response:
        """Toggle pinned flag (also flips type permanent⇄dynamic when needed)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        try:
            # Both pin and unpin share the pinned turn.  Otherwise an unpin can
            # race a new pin and make the latter reject against a stale count;
            # more importantly, two new pins could both pass the same precheck.
            async with AsyncExitStack() as quota_stack:
                await quota_stack.enter_async_context(_quota_turn("pinned"))

                bucket = await sh.bucket_mgr.get(bucket_id)
                if not bucket:
                    return JSONResponse({"error": "not found"}, status_code=404)
                meta = bucket.get("metadata", {})
                if _is_terminal_memory_metadata(meta):
                    return JSONResponse(
                        {"error": "archived buckets cannot be pinned or unpinned"},
                        status_code=409,
                    )
                current_pinned = parse_bool(meta.get("pinned"), default=False)
                new_pinned = not current_pinned
                protected = parse_bool(meta.get("protected"), default=False)
                update_kwargs: dict[str, object] = {"pinned": new_pinned}
                try:
                    current_importance = int(meta.get("importance") or 0)
                except (TypeError, ValueError):
                    current_importance = 0
                unpin_importance = current_importance
                if current_pinned and not protected:
                    try:
                        body = await sh._read_json_object(request)
                        raw_importance = body.get("importance")
                        if isinstance(raw_importance, bool):
                            raise ValueError("boolean is not an importance")
                        unpin_importance = int(raw_importance)
                        if (
                            isinstance(raw_importance, float)
                            and not raw_importance.is_integer()
                        ):
                            raise ValueError("fractional importance")
                    except Exception:
                        return JSONResponse(
                            {
                                "error": "unpin requires importance=1..10 in the same request",
                                "field": "importance",
                            },
                            status_code=400,
                        )
                    if not 1 <= unpin_importance <= 10:
                        return JSONResponse(
                            {
                                "error": "unpin requires importance=1..10 in the same request",
                                "field": "importance",
                            },
                            status_code=400,
                        )
                    update_kwargs["importance"] = unpin_importance
                current_type = str(
                    meta.get("type") or "dynamic"
                ).strip().lower()
                final_type = (
                    "permanent"
                    if new_pinned
                    else "dynamic"
                    if current_pinned and not protected
                    else current_type
                )
                before_quota_meta = dict(meta)
                before_quota_meta.update({
                    "importance": current_importance,
                    "pinned": current_pinned,
                    "protected": protected,
                    "type": current_type,
                })
                after_quota_meta = dict(before_quota_meta)
                after_quota_meta.update({
                    "importance": 10 if new_pinned else unpin_importance,
                    "pinned": new_pinned,
                    "type": final_type,
                })
                occupied_high_before = _occupies_high_importance_slot(
                    before_quota_meta
                )
                occupies_high_after = _occupies_high_importance_slot(
                    after_quota_meta
                )
                await quota_stack.enter_async_context(
                    _quota_turn("high_importance")
                )

                locked_bucket = await sh.bucket_mgr.get(bucket_id)
                if not locked_bucket:
                    return JSONResponse({"error": "not found"}, status_code=404)
                locked_meta = locked_bucket.get("metadata", {})
                if not isinstance(locked_meta, dict):
                    locked_meta = {}
                if _is_terminal_memory_metadata(locked_meta):
                    return JSONResponse(
                        {"error": "bucket was archived concurrently"},
                        status_code=409,
                    )
                initial_quota_state = (
                    current_pinned,
                    protected,
                    current_importance,
                    current_type,
                    parse_bool(meta.get("dont_surface"), default=False),
                )
                try:
                    locked_importance = int(
                        locked_meta.get("importance") or 0
                    )
                except (TypeError, ValueError):
                    locked_importance = 0
                locked_quota_state = (
                    parse_bool(locked_meta.get("pinned"), default=False),
                    parse_bool(locked_meta.get("protected"), default=False),
                    locked_importance,
                    str(locked_meta.get("type") or "dynamic").strip().lower(),
                    parse_bool(
                        locked_meta.get("dont_surface"), default=False
                    ),
                )
                if locked_quota_state != initial_quota_state:
                    return JSONResponse(
                        {
                            "error": "bucket changed concurrently; reload and retry",
                            "conflict": "concurrent_change",
                        },
                        status_code=409,
                    )

                if new_pinned:
                    quota_err = await _check_pinned_quota()
                    if quota_err:
                        return JSONResponse({"error": quota_err}, status_code=400)
                else:
                    # A formerly pinned importance=10 bucket becomes an
                    # ordinary high-importance bucket after unpinning.  Reserve
                    # that quota atomically too; when full, demote to 8 in the
                    # same BucketManager transaction.
                    if occupies_high_after and not occupied_high_before:
                        adjusted_importance = (
                            await _enforce_high_importance_quota(unpin_importance)
                        )
                        if adjusted_importance != unpin_importance:
                            update_kwargs["importance"] = adjusted_importance

                ok = await sh.bucket_mgr.update(
                    bucket_id,
                    event_actor="human",
                    **update_kwargs,
                )
                if not ok:
                    latest = await sh.bucket_mgr.get(bucket_id)
                    if _is_terminal_memory_metadata(
                        (latest or {}).get("metadata", {})
                    ):
                        return JSONResponse(
                            {"error": "bucket was archived concurrently"},
                            status_code=409,
                        )
                    return JSONResponse({"error": "update failed"}, status_code=500)

                persisted = await sh.bucket_mgr.get(bucket_id)
                actual_pinned = parse_bool(
                    (persisted or {}).get("metadata", {}).get("pinned"),
                    default=False,
                )
                if not persisted or actual_pinned != new_pinned:
                    return JSONResponse(
                        {
                            "error": "pin state was not persisted",
                            "pinned": actual_pinned,
                        },
                        status_code=409,
                    )
                persisted_meta = persisted.get("metadata", {})
                return JSONResponse({
                    "ok": True,
                    "pinned": actual_pinned,
                    "importance": persisted_meta.get("importance"),
                    "type": persisted_meta.get("type"),
                })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/bucket/{bucket_id}/resolve", methods=["POST"])
    async def api_bucket_resolve(request: Request) -> Response:
        """Toggle resolved flag."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        new_resolved = not bool(bucket["metadata"].get("resolved", False))
        try:
            await sh.bucket_mgr.update(bucket_id, resolved=new_resolved)
            return JSONResponse({
                "ok": True,
                "resolved": new_resolved,
                "message": resolved_hint(new_resolved),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/bucket/{bucket_id}/archive", methods=["POST"])
    async def api_bucket_archive(request: Request) -> Response:
        """Move bucket to archive directory (soft delete)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        try:
            ok = await sh.bucket_mgr.archive(bucket_id)
            if not ok:
                return JSONResponse({"error": "archive failed or bucket not found"}, status_code=404)
            return JSONResponse({"ok": True, "archived": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # ---- iter 1.8: 主动遗忘开关 / voluntary forget toggle ---------
    # Toggle the dont_surface flag. Bucket itself stays on disk, only its
    # active push to breath() is suppressed. Search still finds it.
    # 切换 dont_surface 字段。桶仍在磁盘上，只是不再主动浮现到 breath。
    # 搜索（breath(query=...)）仍能找到它。
    @mcp.custom_route("/api/bucket/{bucket_id}/forget", methods=["POST"])
    async def api_bucket_forget(request: Request) -> Response:
        """Toggle dont_surface flag (iter 1.8 voluntary forget)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        try:
            async with _quota_turn("high_importance"):
                bucket = await sh.bucket_mgr.get(bucket_id)
                if not bucket:
                    return JSONResponse({"error": "not found"}, status_code=404)
                metadata = bucket.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                current = parse_bool(
                    metadata.get("dont_surface"), default=False
                )
                new_val = not current
                projected = dict(metadata)
                projected["dont_surface"] = new_val
                update_kwargs: dict[str, object] = {"dont_surface": new_val}
                quota_adjustment = None
                if (
                    _occupies_high_importance_slot(projected)
                    and not _occupies_high_importance_slot(metadata)
                ):
                    try:
                        requested_importance = int(
                            metadata.get("importance") or 0
                        )
                    except (TypeError, ValueError):
                        requested_importance = 0
                    applied_importance = await _enforce_high_importance_quota(
                        requested_importance
                    )
                    if applied_importance != requested_importance:
                        update_kwargs["importance"] = applied_importance
                        quota_adjustment = {
                            "requested": requested_importance,
                            "applied": applied_importance,
                        }
                ok = await sh.bucket_mgr.update(bucket_id, **update_kwargs)
                if not ok:
                    return JSONResponse({"error": "update failed"}, status_code=500)
                payload = {"ok": True, "dont_surface": new_val}
                if quota_adjustment:
                    payload["quota_adjustment"] = quota_adjustment
                return JSONResponse(payload)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # ---- iter 1.9 C: 批量主动遗忘 / batch voluntary forget ---------
    # Body: {ids: [...], dont_surface: true|false}
    # 不像单条端点那样 toggle —— 批量必须显式说成 true 还是 false，避免误反转。
    @mcp.custom_route("/api/buckets/forget", methods=["POST"])
    async def api_buckets_forget_batch(request: Request) -> Response:
        """Batch toggle dont_surface for many buckets (iter 1.9 §C)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)
        if len(ids) > 500:
            return JSONResponse({"error": "ids exceeds the 500-item batch limit"}, status_code=400)
        if any(not isinstance(bid, str) or not bid or len(bid) > 128 for bid in ids):
            return JSONResponse({"error": "each id must be a non-empty string up to 128 characters"}, status_code=400)
        if "dont_surface" not in body:
            return JSONResponse({"error": "dont_surface (bool) required"}, status_code=400)
        try:
            target = parse_bool(body["dont_surface"])
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        ok_ids, missing_ids, errors, quota_adjustments = [], [], [], []
        async with _quota_turn("high_importance"):
            for bid in dict.fromkeys(ids):
                try:
                    b = await sh.bucket_mgr.get(bid)
                    if not b:
                        missing_ids.append(bid)
                        continue
                    metadata = b.get("metadata", {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    projected = dict(metadata)
                    projected["dont_surface"] = target
                    update_kwargs: dict[str, object] = {"dont_surface": target}
                    quota_adjustment = None
                    if (
                        _occupies_high_importance_slot(projected)
                        and not _occupies_high_importance_slot(metadata)
                    ):
                        try:
                            requested_importance = int(
                                metadata.get("importance") or 0
                            )
                        except (TypeError, ValueError):
                            requested_importance = 0
                        applied_importance = (
                            await _enforce_high_importance_quota(
                                requested_importance
                            )
                        )
                        if applied_importance != requested_importance:
                            update_kwargs["importance"] = applied_importance
                            quota_adjustment = {
                                "id": bid,
                                "requested": requested_importance,
                                "applied": applied_importance,
                            }
                    ok = await sh.bucket_mgr.update(bid, **update_kwargs)
                    if ok:
                        ok_ids.append(bid)
                        if quota_adjustment:
                            quota_adjustments.append(quota_adjustment)
                    else:
                        errors.append({"id": bid, "error": "update failed"})
                except Exception as e:
                    errors.append({"id": bid, "error": str(e)})
                    logger.warning(f"batch forget failed for {bid}: {e}")
        payload = {
            "ok": not errors,
            "dont_surface": target,
            "updated": ok_ids,
            "missing": missing_ids,
            "errors": errors,
        }
        if quota_adjustments:
            payload["quota_adjustments"] = quota_adjustments
        return JSONResponse(payload)

    @mcp.custom_route("/api/buckets/batch", methods=["POST"])
    async def api_buckets_batch(request: Request) -> Response:
        """Batch ordinary memory actions; never physically deletes files."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        ids = body.get("ids") or []
        action = str(body.get("action") or "")
        if not isinstance(ids, list) or not ids or len(ids) > 500:
            return JSONResponse({"error": "ids must contain 1-500 items"}, status_code=400)
        if any(not isinstance(item, str) or not item or len(item) > 128 for item in ids):
            return JSONResponse({"error": "invalid bucket id"}, status_code=400)
        if action not in {"forget", "resolve", "archive"}:
            return JSONResponse({"error": "unsupported batch action"}, status_code=400)
        updated, missing, errors = [], [], []
        for bucket_id in dict.fromkeys(ids):
            try:
                bucket = await sh.bucket_mgr.get(bucket_id)
                if not bucket:
                    missing.append(bucket_id)
                    continue
                if action == "forget":
                    ok = await sh.bucket_mgr.update(bucket_id, dont_surface=True)
                elif action == "resolve":
                    ok = await sh.bucket_mgr.update(bucket_id, resolved=True)
                else:
                    ok = await sh.bucket_mgr.archive(bucket_id)
                if ok:
                    updated.append(bucket_id)
                else:
                    errors.append({"id": bucket_id, "error": f"{action} failed"})
            except Exception as exc:
                errors.append({"id": bucket_id, "error": str(exc)})
        return JSONResponse({"ok": not errors, "action": action,
                             "updated": updated, "missing": missing, "errors": errors})

    @mcp.custom_route("/api/developer/buckets/hard-delete", methods=["POST"])
    async def api_developer_hard_delete(request: Request) -> Response:
        """Erase explicitly erasable test buckets after a developer confirmation phrase."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        ids = body.get("ids") or []
        if body.get("confirm") != "DELETE TEST DATA":
            return JSONResponse({"error": "confirmation phrase required"}, status_code=400)
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            return JSONResponse({"error": "ids must contain 1-100 items"}, status_code=400)
        deleted, refused, errors = [], [], []
        for bucket_id in dict.fromkeys(ids):
            if not isinstance(bucket_id, str) or not bucket_id or len(bucket_id) > 128:
                errors.append({"id": str(bucket_id), "error": "invalid bucket id"})
                continue
            result = await sh.bucket_mgr.hard_delete_test_bucket(
                bucket_id, reason=str(body.get("reason") or "developer cleanup")
            )
            if result.get("ok"):
                deleted.append(bucket_id)
            elif result.get("error") == "not_erasable_test_data":
                refused.append(bucket_id)
            else:
                errors.append({"id": bucket_id, "error": result.get("error")})
        status = 200 if deleted and not errors else (403 if refused and not deleted else 400)
        return JSONResponse({"ok": bool(deleted) and not errors, "deleted": deleted,
                             "refused": refused, "errors": errors}, status_code=status)


    # ---- iter 1.9 B: dashboard 调 sampling 配置 / sampling control ----
    # GET 返回当前 surfacing.sampling；POST 接收新值并热更新内存里的 config。
    # 这里只改运行时 config，不写回 yaml—— yaml 持久化交给 1.6 已有的设置面板机制（如开发者愿意手 sync）。
    @mcp.custom_route("/api/settings/sampling", methods=["GET", "POST"])
    async def api_settings_sampling(request: Request) -> Response:
        """Get / hot-update breath weighted sampling settings (iter 1.9 §B)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        surfacing = sh.config.get("surfacing")
        if not isinstance(surfacing, dict):
            surfacing = {}
        sampling = surfacing.get("sampling")
        if not isinstance(sampling, dict):
            sampling = {}
        if request.method == "GET":
            return JSONResponse({
                "enabled": parse_bool(sampling.get("enabled", False), default=False),
                "top_k": int(sampling.get("top_k") or 5),
                "sample_k": int(sampling.get("sample_k") or 2),
                "temperature": float(sampling.get("temperature") or 0.7),
            })
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        # JSON 读取已经结束，以下原生锁临界区内没有 await。锁覆盖候选快照、
        # 落盘和运行态发布，防止并发更新丢字段或出现磁盘/运行态逆序。
        with sampling_commit_lock:
            live_surfacing = sh.config.get("surfacing")
            if not isinstance(live_surfacing, dict):
                live_surfacing = {}
            live_sampling = live_surfacing.get("sampling")
            if not isinstance(live_sampling, dict):
                live_sampling = {}

            # 先在独立候选副本上完成全部校验。若逐字段发布，后续字段无效或
            # config.yaml 写入失败时，运行态会残留一半新、一半旧的配置。
            candidate = dict(live_sampling)
            try:
                if "enabled" in body:
                    candidate["enabled"] = parse_bool(body["enabled"])
                if "top_k" in body:
                    if isinstance(body["top_k"], bool):
                        raise ValueError("top_k must be an integer")
                    tk = int(body["top_k"])
                    if isinstance(body["top_k"], float) and body["top_k"] != tk:
                        raise ValueError("top_k must be an integer")
                    if not (1 <= tk <= 50):
                        return JSONResponse(
                            {"error": "top_k must be in [1,50]"}, status_code=400
                        )
                    candidate["top_k"] = tk
                if "sample_k" in body:
                    if isinstance(body["sample_k"], bool):
                        raise ValueError("sample_k must be an integer")
                    sk = int(body["sample_k"])
                    if (
                        isinstance(body["sample_k"], float)
                        and body["sample_k"] != sk
                    ):
                        raise ValueError("sample_k must be an integer")
                    if not (1 <= sk <= 20):
                        return JSONResponse(
                            {"error": "sample_k must be in [1,20]"},
                            status_code=400,
                        )
                    candidate["sample_k"] = sk
                if "temperature" in body:
                    if isinstance(body["temperature"], bool):
                        raise ValueError("temperature must be a number")
                    temperature = float(body["temperature"])
                    if not math.isfinite(temperature) or not (
                        0.1 <= temperature <= 5.0
                    ):
                        return JSONResponse(
                            {"error": "temperature must be in [0.1,5.0]"},
                            status_code=400,
                        )
                    candidate["temperature"] = temperature
            except (OverflowError, ValueError, TypeError) as e:
                return JSONResponse(
                    {"error": f"invalid field type: {e}"}, status_code=400
                )

            # 写回 config.yaml，保证重启后设置不丢失。
            def _mutate_sampling(save_config: dict) -> None:
                sf = save_config.setdefault("surfacing", {})
                if not isinstance(sf, dict):
                    sf = {}
                    save_config["surfacing"] = sf
                samp = sf.setdefault("sampling", {})
                if not isinstance(samp, dict):
                    samp = {}
                    sf["sampling"] = samp
                samp.update({
                    "enabled": candidate.get("enabled", False),
                    "top_k": candidate.get("top_k", 5),
                    "sample_k": candidate.get("sample_k", 2),
                    "temperature": candidate.get("temperature", 0.7),
                })

            try:
                atomic_update_config_yaml(_mutate_sampling)
            except Exception as e:
                # 磁盘未落地就如实报错，不能让用户看到“已保存”。
                return JSONResponse(
                    {"error": f"采样设置写入磁盘失败，未保存：{e}"},
                    status_code=500,
                )

            # 以磁盘写入成功为提交点；尽量保留原嵌套字典对象，因为浮现逻辑
            # 可能持有这个对象的引用。
            published_surfacing = sh.config.get("surfacing")
            if not isinstance(published_surfacing, dict):
                published_surfacing = {}
                sh.config["surfacing"] = published_surfacing
            published_sampling = published_surfacing.get("sampling")
            if not isinstance(published_sampling, dict):
                published_sampling = {}
                published_surfacing["sampling"] = published_sampling
            published_sampling.clear()
            published_sampling.update(candidate)

            return JSONResponse({"ok": True, **published_sampling})


    # ---- iter 2.0: /api/settings/human — 读写通知称呼（human 宏）----
    # GET 返回当前 human 配置；POST 更新内存并写回 config.yaml。
    @mcp.custom_route("/api/settings/human", methods=["GET", "POST"])
    async def api_settings_human(request: Request) -> Response:
        """Get / update the 'human' display name used in deletion notices."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.method == "GET":
            return JSONResponse({"human": sh.config.get("human", "人类")})
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        human_raw = body.get("human", "")
        if not isinstance(human_raw, str):
            return JSONResponse({"error": "human name must be a string"}, status_code=400)
        human = human_raw.strip()
        if not human:
            human = "人类"
        if len(human) > 20:
            return JSONResponse({"error": "human name must be ≤ 20 characters"}, status_code=400)
        if any(unicodedata.category(char).startswith("C") for char in human):
            return JSONResponse(
                {"error": "human name must not contain control characters"},
                status_code=400,
            )
        # Config read/write, live runtime update and the full-vault replacement
        # are one outer transaction.  Without it, concurrent A->B and B->C
        # requests can interleave their per-bucket writes and leave mixed names.
        async with sh.bucket_mgr.human_name_change_turn():
            # 旧称呼（默认「用户」，与 dehydrator / import 的兜底同源）—— 用于把老桶里的旧词换成新名。
            old_human = (sh.config.get("human") or "用户").strip() or "用户"
            try:
                atomic_update_config_yaml(
                    lambda save_config: save_config.__setitem__("human", human)
                )
            except Exception as e:
                # Do not mutate live state unless persistence succeeded.
                return JSONResponse(
                    {"error": f"称呼写入磁盘失败，未保存：{e}"},
                    status_code=500,
                )

            sh.config["human"] = human
            # 同步活的 dehydrator.human：否则改名后、重启前，新记忆仍按旧称呼脱水。
            if getattr(sh, "dehydrator", None) is not None and hasattr(
                sh.dehydrator, "human"
            ):
                sh.dehydrator.human = human

            # 改名时把老桶里残留的旧称呼一起换成新名（name/content/why_remembered/user_name）。
            renamed = {"buckets_changed": 0, "replacements": 0}
            if old_human and old_human != human:
                try:
                    renamed = await rename_human_in_buckets(old_human, human)
                except Exception as _re:
                    logger.warning(f"human rename batch failed: {_re}")
        return JSONResponse({"ok": True, "human": human, "renamed": renamed})

    # ---- 手动「同步旧记忆」：把指定旧称呼（默认「用户」）批量换成当前称呼 ----
    # 用于：已经改过昵称、但改名前就存在的老桶仍写着旧词，breath 里新旧名并存。
    @mcp.custom_route("/api/settings/human/sync-existing", methods=["POST"])
    async def api_settings_human_sync(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        from_raw = body.get("from") or "用户"
        if not isinstance(from_raw, str):
            return JSONResponse({"error": "from must be a string"}, status_code=400)
        from_term = from_raw.strip()
        if len(from_term) > 100:
            return JSONResponse({"error": "from must be at most 100 characters"}, status_code=400)
        if not from_term:
            return JSONResponse({"error": "缺少要替换的旧称呼"}, status_code=400)
        async with sh.bucket_mgr.human_name_change_turn():
            # Re-read inside the same reservation used by name-change requests.
            cur = (sh.config.get("human") or "人类").strip() or "人类"
            if from_term == cur:
                return JSONResponse({
                    "ok": True, "from": from_term, "to": cur,
                    "renamed": {"buckets_changed": 0, "replacements": 0},
                    "note": "要替换的词与当前称呼相同，无需处理",
                })
            try:
                stats = await rename_human_in_buckets(from_term, cur)
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "from": from_term, "to": cur, "renamed": stats})


    # ---- iter 2.0: anchor 端点 / coordinate-system buckets ----
    # anchor = 「定义我们是谁」的 24 槽。不进默认 breath，硬上限。
    @mcp.custom_route("/api/anchors", methods=["GET"])
    async def api_anchors_list(request: Request) -> Response:
        """Return all anchor buckets (sorted by created asc)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            anchors = await sh.bucket_mgr.list_anchors()
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        items = []
        for b in anchors:
            m = b.get("metadata", {})
            items.append({
                "id": b["id"],
                "name": m.get("name") or b["id"],
                "created": m.get("created", ""),
                "domain": m.get("domain", []),
                "tags": m.get("tags", []),
                "type": m.get("type", "dynamic"),
                "pinned": bool(m.get("pinned", False)),
                "preview": (b.get("content", "") or "")[:80],
            })
        return JSONResponse({
            "ok": True,
            "count": len(items),
            "limit": sh.bucket_mgr.ANCHOR_LIMIT,
            "anchors": items,
        })


    @mcp.custom_route("/api/bucket/{bucket_id}/anchor", methods=["POST"])
    async def api_bucket_anchor(request: Request) -> Response:
        """Toggle anchor flag on a bucket. 409 if cap reached when setting True."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        bucket_id = request.path_params["bucket_id"]
        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "not found"}, status_code=404)
        # Allow explicit value via JSON body; default = toggle
        target = None
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
            if "value" in body:
                target = parse_bool(body["value"])
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception:
            pass  # no body → toggle
        if target is None:
            target = not bool(bucket["metadata"].get("anchor", False))
        result = await sh.bucket_mgr.set_anchor(bucket_id, target)
        if not result["ok"]:
            # Cap-reached errors → 409 Conflict; everything else → 500
            status = 409 if "上限" in result.get("error", "") or "limit" in result.get("error", "") else 500
            return JSONResponse(result, status_code=status)
        return JSONResponse(result)


    @mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
    async def api_bucket_delete(request: Request) -> Response:
        """Delete to archive (F-10): requires ?confirm=true. Moves file to archive/ + stamps deleted_at."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.query_params.get("confirm", "").lower() not in ("true", "1", "yes"):
            return JSONResponse({"error": "confirm=true required for delete-to-archive"}, status_code=400)
        bucket_id = request.path_params["bucket_id"]
        try:
            ok = await sh.bucket_mgr.delete(bucket_id)
            if not ok:
                return JSONResponse({"error": "bucket not found"}, status_code=404)
            return JSONResponse({"ok": True, "deleted": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/buckets/purge", methods=["POST"])
    async def api_buckets_purge(request: Request) -> Response:
        """Retired hard-purge endpoint: memory may be archived, never physically erased."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({
            "error": "physical_deletion_forbidden",
            "message": (
                "Ombre Brain 不提供物理删除记忆桶的能力。请使用归档或主动遗忘；"
                "Markdown 文件会继续保留。"
            ),
            "philosophy": "记忆会被遗忘，但绝不能被抹去。",
        }, status_code=410)


    # ---- letter REST endpoints (iter 1.4) ------------------------
    # =============================================================
    # /api/letters、/api/letter、/letters、/api/letter/{id} —— 已拆分到 web/letters.py
    # =============================================================


    @mcp.custom_route("/api/self", methods=["GET"])
    async def api_self(request: Request) -> Response:
        """Return all self-type (I tool) entries, newest first."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            all_b = await sh.bucket_mgr.list_all(include_archive=False)
            self_buckets = [
                b for b in all_b
                if b["metadata"].get("type") == "i"
                or "__i__" in (b["metadata"].get("tags") or [])
            ]
            self_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            result = []
            for b in self_buckets:
                meta = b["metadata"]
                tags = meta.get("tags") or []
                aspect = next((t.replace("aspect:", "") for t in tags if t.startswith("aspect:")), "")
                result.append({
                    "id": b["id"],
                    "content": b.get("content", ""),
                    "aspect": aspect,
                    "created": meta.get("created", ""),
                })
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    # =============================================================
    # /api/search、/api/duplicates、/api/network、/api/breath、/api/breath-debug
    # —— 已拆分到 web/search.py
    # =============================================================
