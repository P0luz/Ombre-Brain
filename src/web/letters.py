"""
========================================
web/letters.py — 信件（letter）读写
========================================

- /api/letters：列出信件
- /api/letter (POST)：写信
- /letters：信件页（兼容入口）
- /api/letter/{id} (PATCH/DELETE)：编辑 / 删除信件

对外暴露：register(mcp)。
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from tools._common import check_content_size, check_metadata_size
from tools.plan.core import (
    author_side,
    is_letter_bucket,
    letter_lock_revision,
    letter_lock_state,
    normalize_expired_lock,
    normalize_lock_type,
    normalize_unlock_date,
    resolve_writer_name,
    safe_letter_metadata,
)

try:
    from utils import get_ai_name  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import get_ai_name  # type: ignore


def _normalize_author(raw: str) -> str:
    """把传入署名归一化为存储值，与 tools/plan/core.letter_write 同一套规则：
    "user"→"user"；"ai"/"claude"(历史)/等于 ai_name→ai_name 的值；其它原样。"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    ai = get_ai_name()
    low = raw.lower()
    if low == "user":
        return "user"
    if low in ("ai", "claude") or raw == ai:
        return ai
    return raw


def register(mcp) -> None:

    @mcp.custom_route("/api/letters", methods=["GET"])
    async def api_letters(request: Request) -> Response:
        """List all letters, newest first. Supports ?author=user|ai|<署名> filter."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        author = request.query_params.get("author", "").strip()
        metadata_error = check_metadata_size(author=author)
        if metadata_error:
            return JSONResponse({"error": metadata_error}, status_code=400)
        try:
            all_b = await sh.bucket_mgr.list_all(include_archive=False)
            letters = [b for b in all_b if is_letter_bucket(b)]
            if author:
                af_low = author.lower()
                if af_low == "user":
                    letters = [b for b in letters if b["metadata"].get("author") == "user"]
                elif af_low in ("ai", "claude") or author == get_ai_name():
                    ai_aliases = {get_ai_name(), "claude"}
                    letters = [b for b in letters if b["metadata"].get("author") in ai_aliases]
                else:
                    letters = [b for b in letters if b["metadata"].get("author") == author]
            letters.sort(
                key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""),
                reverse=True,
            )
            store = getattr(sh, "deletion_requests", None)
            deletion_statuses = store.status_snapshot() if store else {}
            result = []
            for b in letters:
                state = letter_lock_state(b, "human")
                if state["expired"]:
                    b, state = await normalize_expired_lock(
                        b,
                        state,
                        "human",
                        bucket_mgr=sh.bucket_mgr,
                    )
                    if not b:
                        continue
                item = safe_letter_metadata(b, "human")
                item["deletion_request"] = deletion_statuses.get(str(b["id"]))
                result.append(item)
            return JSONResponse({"letters": result, "total": len(result)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/letter", methods=["POST"])
    async def api_letter_create(request: Request) -> Response:
        """Create a letter from the dashboard."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        string_fields = (
            "author", "content", "user_name", "title", "date", "ai_name",
            "lock_type", "unlock_date",
        )
        if any(key in body and not isinstance(body[key], str) for key in string_fields):
            return JSONResponse({"error": "letter fields must be strings"}, status_code=400)
        raw_author = (body.get("author") or "").strip()
        content = (body.get("content") or "").strip()
        if not raw_author:
            return JSONResponse({"error": "author required"}, status_code=400)
        if not content:
            return JSONResponse({"error": "content required"}, status_code=400)
        size_err = check_content_size(content)
        if size_err:
            return JSONResponse({"error": size_err}, status_code=400)
        metadata_err = check_metadata_size(
            author=raw_author,
            user_name=body.get("user_name", ""),
            title=body.get("title", ""),
            date=body.get("date", ""),
            ai_name=body.get("ai_name", ""),
        )
        if metadata_err:
            return JSONResponse({"error": metadata_err}, status_code=400)
        # ai_name：请求体显式传入优先，否则取环境变量 AI_NAME（回退 "AI"）。
        ai = (body.get("ai_name") or "").strip() or get_ai_name()
        low = raw_author.lower()
        if low == "user":
            author = "user"
        elif low in ("ai", "claude") or raw_author == ai:
            author = ai
        else:
            author = raw_author
        user_name = (body.get("user_name") or "").strip()
        title = (body.get("title") or "").strip()[:120]
        date = (body.get("date") or "").strip()
        try:
            lock_type = normalize_lock_type(body.get("lock_type", "none"))
            unlock_date = normalize_unlock_date(lock_type, body.get("unlock_date", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if lock_type != "none" and author_side(raw_author, ai_name=ai) == "ai":
            return JSONResponse({
                "error": (
                    "当前 Dashboard 入口不能替对方创建带锁 Letter；"
                    "普通无锁代存仍然可用。"
                )
            }, status_code=400)
        writer_name = resolve_writer_name(
            "human",
            author=raw_author,
            user_name=user_name,
            ai_name=body.get("ai_name", ""),
        )
        if lock_type != "none" and not writer_name:
            return JSONResponse({
                "error": (
                    "无法创建带锁 Letter：未能从现有 user_name / OMBRE_OWNER_NAME / author "
                    "取得当前写信人的实际关系名。请先完善现有名称配置。"
                )
            }, status_code=400)
        extra = {
            "author": author,
            "lock_type": lock_type,
            "unlock_date": unlock_date,
            "locked_by": "human",
        }
        if writer_name:
            extra["writer_name"] = writer_name
        if user_name:
            extra["user_name"] = user_name
        if title:
            extra["title"] = title
        if date:
            extra["letter_date"] = date
        try:
            bid = await sh.bucket_mgr.create(
                content=content,
                tags=["__letter__"],
                importance=10,
                domain=["letter"],
                valence=0.5,
                arousal=0.3,
                name=(title[:60] or f"{author}_{date or 'letter'}"),
                bucket_type="letter",
                source_tool="letter",
                event_actor="human",
                lock_type=lock_type,
                unlock_date=unlock_date,
                locked_by="human",
                writer_name=writer_name or "",
            )
            await sh.bucket_mgr.update(bid, **extra)
            created = await sh.bucket_mgr.get(bid)
            created_at = ((created or {}).get("metadata") or {}).get("created", "")
            response = {
                "ok": True,
                "id": bid,
                "created_at": created_at,
                "lock_type": lock_type,
                "unlock_date": unlock_date,
                "stored": True,
            }
            return JSONResponse(response)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/letters", methods=["GET"])
    async def letters_page(request: Request) -> Response:
        """Legacy alias: /letters 永久跳到 dashboard 的「信」分页。

        我把 letters 合并进 dashboard 的一个 tab 后，这条老路径只保留 301 软迁移，
        避免独立维护两套 HTML/JS。
        """
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/#letters", status_code=301)


    @mcp.custom_route("/api/letter/{letter_id}", methods=["PATCH"])
    async def api_letter_edit(request: Request) -> Response:
        """Edit Letter fields or lock metadata, never both in one request."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        letter_id = request.path_params["letter_id"]
        bucket = await sh.bucket_mgr.get(letter_id)
        if not bucket or not is_letter_bucket(bucket):
            return JSONResponse({"error": "letter not found"}, status_code=404)
        expected_lock_state = letter_lock_revision(bucket)
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        content_fields = {"content", "title", "author", "user_name", "date"}
        lock_fields = {"lock_type", "unlock_date"}
        conversion_fields = {"convert_to_lockable"}
        has_content_edit = bool(content_fields.intersection(body))
        has_lock_edit = bool(lock_fields.intersection(body))
        has_conversion = bool(conversion_fields.intersection(body))
        if sum((has_content_edit, has_lock_edit, has_conversion)) > 1:
            return JSONResponse({
                "error": "正文编辑、锁管理与历史格式转换必须分开提交"
            }, status_code=400)
        if not has_content_edit and not has_lock_edit and not has_conversion:
            return JSONResponse({"error": "nothing to update"}, status_code=400)

        state = letter_lock_state(bucket, "human")
        if has_conversion:
            if body.get("convert_to_lockable") is not True:
                return JSONResponse({
                    "error": "convert_to_lockable must be true"
                }, status_code=400)
            if state["locked_by"]:
                return JSONResponse({
                    "error": "这封 Letter 已有可信锁所有者，不能重新分配"
                }, status_code=409)
            if state["stored_lock_type"] != "none":
                return JSONResponse({
                    "error": "只有当前公开且从未建立可信锁所有权的历史 Letter 可以转换"
                }, status_code=409)
            # ai_name：本次转换请求显式传入优先（Dashboard 在 AI_NAME 未配置时会
            # 弹窗让用户当场填一个，只用于这一次转换，不改全局配置），否则回退
            # 环境变量 AI_NAME——与 letter_write 创建带锁 Letter 的取值顺序一致。
            explicit_ai_name = str(body.get("ai_name") or "").strip()
            ai_writer_name = resolve_writer_name(
                "ai", author="", ai_name=explicit_ai_name or get_ai_name()
            )
            if not ai_writer_name:
                return JSONResponse({
                    "error": (
                        "无法转换历史 Letter：未能从现有 AI_NAME 取得当前 AI 的实际关系名。"
                        "请先完善现有名称配置。"
                    )
                }, status_code=400)
            try:
                ok = await sh.bucket_mgr.update(
                    letter_id,
                    locked_by="ai",
                    lock_owner_source="legacy_ai_conversion",
                    writer_name=ai_writer_name,
                    lock_type="none",
                    unlock_date=None,
                    expected_lock_state=expected_lock_state,
                )
                if not ok:
                    latest = await sh.bucket_mgr.get(letter_id)
                    if latest and letter_lock_revision(latest) != expected_lock_state:
                        return JSONResponse(
                            {"error": "letter lock changed concurrently"},
                            status_code=409,
                        )
                    return JSONResponse({"error": "conversion failed"}, status_code=500)
                return JSONResponse({
                    "ok": True,
                    "id": letter_id,
                    "converted": True,
                    "lock_type": "none",
                    "locked_by": "ai",
                    "writer_name": ai_writer_name,
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        if has_content_edit:
            # The Dashboard keeps its historical editing behavior.  The only
            # additional boundary is that an incoming, still-locked Letter
            # must not expose or accept changes to hidden fields.
            if state["locked"]:
                return JSONResponse({
                    "error": "这封 Letter 当前尚未向你开放，不能读取或修改隐藏内容"
                }, status_code=403)
            if any(
                key in body and not isinstance(body[key], str)
                for key in content_fields
            ):
                return JSONResponse({"error": "letter fields must be strings"}, status_code=400)
            updates: dict = {}
            if "content" in body and body["content"].strip():
                size_err = check_content_size(body["content"])
                if size_err:
                    return JSONResponse({"error": size_err}, status_code=400)
                updates["content"] = body["content"].strip()
            if "title" in body:
                updates["title"] = body["title"].strip()[:120]
            if "author" in body:
                normalized_author = _normalize_author(body["author"])
                if normalized_author:
                    updates["author"] = normalized_author
            if "user_name" in body:
                updates["user_name"] = body["user_name"].strip()
            if "date" in body:
                updates["letter_date"] = body["date"].strip()
            if not updates:
                return JSONResponse({"error": "nothing to update"}, status_code=400)
            try:
                ok = await sh.bucket_mgr.update(
                    letter_id,
                    expected_lock_state=expected_lock_state,
                    **updates,
                )
                if not ok:
                    latest = await sh.bucket_mgr.get(letter_id)
                    if latest and letter_lock_revision(latest) != expected_lock_state:
                        return JSONResponse(
                            {"error": "letter lock changed concurrently"},
                            status_code=409,
                        )
                    return JSONResponse({"error": "update failed"}, status_code=500)
                if "content" in updates:
                    try:
                        sh.dehydrator.invalidate_cache(bucket["content"])
                    except Exception:
                        pass
                return JSONResponse({
                    "ok": True,
                    "id": letter_id,
                    "updated": list(updates.keys()),
                })
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # Lock management is deliberately separate from original-field edits.
        if "lock_type" not in body or not isinstance(body.get("lock_type"), str):
            return JSONResponse({"error": "lock_type required"}, status_code=400)
        if "unlock_date" in body and not isinstance(body["unlock_date"], str):
            return JSONResponse({"error": "unlock_date must be a string"}, status_code=400)
        if not state["locked_by"]:
            return JSONResponse({"error": "历史 Letter 没有可信锁所有者，不能补设锁"}, status_code=403)
        if not state["owner"]:
            return JSONResponse({"error": "只有创建这把锁的一方可以修改锁状态"}, status_code=403)
        try:
            lock_type = normalize_lock_type(body["lock_type"])
            unlock_date = normalize_unlock_date(lock_type, body.get("unlock_date", ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        meta = bucket.get("metadata") or {}
        claimed_side = author_side(meta.get("author"))
        if lock_type != "none" and claimed_side and claimed_side != "human":
            return JSONResponse({
                "error": "这封无锁 Letter 的署名方向与当前可信入口不一致；代存信不能事后转换为锁信"
            }, status_code=400)
        writer_name = str(meta.get("writer_name") or "").strip()
        if lock_type != "none" and not writer_name:
            return JSONResponse({
                "error": "这封 Letter 创建时没有记录实际关系名，请新写一封带锁 Letter"
            }, status_code=400)
        updates = {"lock_type": lock_type, "unlock_date": unlock_date}

        try:
            ok = await sh.bucket_mgr.update(
                letter_id,
                expected_lock_state=expected_lock_state,
                **updates,
            )
            if not ok:
                latest = await sh.bucket_mgr.get(letter_id)
                if latest and letter_lock_revision(latest) != expected_lock_state:
                    return JSONResponse(
                        {"error": "letter lock changed concurrently"},
                        status_code=409,
                    )
                return JSONResponse({"error": "update failed"}, status_code=500)
            return JSONResponse({
                "ok": True,
                "id": letter_id,
                "lock_type": lock_type,
                "unlock_date": unlock_date,
                "updated": True,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)


    @mcp.custom_route("/api/letter/{letter_id}", methods=["DELETE"])
    async def api_letter_delete(request: Request) -> Response:
        """Delete a letter to archive. Requires ?confirm=true."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if request.query_params.get("confirm", "").lower() not in ("true", "1", "yes"):
            return JSONResponse({"error": "confirm=true required for delete-to-archive"}, status_code=400)
        letter_id = request.path_params["letter_id"]
        bucket = await sh.bucket_mgr.get(letter_id)
        if bucket and not is_letter_bucket(bucket):
            return JSONResponse({"error": "letter not found"}, status_code=404)
        try:
            try:
                body = await sh._read_json_object(request)
            except Exception:
                body = {}
            result = await sh.deletion_requests.submit(
                letter_id, body.get("reason", ""), is_letter=True
            )
            if not result.get("ok"):
                status = 404 if result.get("code") == "not_found" else 409 if result.get("code") in {"pending_exists", "daily_limit", "lifetime_limit"} else 400
                return JSONResponse(result, status_code=status)
            return JSONResponse(result)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
