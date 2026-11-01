"""
========================================
tools/plan/core.py — plan / letter_write / letter_read 实现
========================================

plan 桶记录我答应过、答应自己或想完成的事；letter 桶是她/他与 OB
之间的长信件。它们都是独立类型，永久保存、不衰减、不出现在普通
breath 中。

关键行为：
- plan_create：去重（同正文 + status=active 已存在 → 直接返回原 ID），
  写入 type=plan + status + weight + change_log 起点
- letter_write：原文永久保存，author 接受任意字符串署名（"ai" 或等于
  ai_name 时统一存为 ai_name 的值，其它字符串原样存为署名；"user" 为
  用户侧），写入 type=letter + author/title/letter_date 元数据
- letter_read：默认按时间倒序；带 query 时走向量近邻；支持 author /
  date_from / date_to 过滤；author 字段原样返回存储的署名，不做转换

不做什么（边界）：
- plan 不做向量去重，只做精确文本去重
- letter 永不合并、永不压缩、永不被衰减归档

对外暴露：plan_create / letter_write / letter_read
========================================
"""

import json
import math
from datetime import datetime, timezone
from typing import Optional

from .. import _runtime as rt
from .._common import (
    check_content_size,
    check_metadata_size,
    check_query_size,
)
from utils import strip_wikilinks, get_ai_name, get_owner_name, get_tzinfo, get_timezone_name
from errors import ToolInputError, safe_error_detail
# 锁语义 3.6.5 下沉到 ombrebrain/storage/letter_lock.py：you / them 的证据闸
# 也要判「这封信对 AI 开没开」，而 ombrebrain 不能反向 import tools。
# 这里按原名再导出，所有既有调用点不变。
from ombrebrain.storage.letter_lock import (  # noqa: F401
    LETTER_LOCK_TYPES,
    is_letter_bucket,
    letter_lock_state,
    normalize_lock_type,
)


PERMANENT_UNLOCK_DATE = "9999-12-31"
_GENERIC_RELATION_NAMES = {
    "ai", "a.i.", "assistant", "claude", "bot", "model",
    "user", "human", "human-side", "ai-side", "you", "me",
}
_HUMAN_AUTHOR_ALIASES = {"user", "human", "human-side"}
_AI_AUTHOR_ALIASES = {"ai", "ai-side", "claude"}


def normalize_unlock_date(lock_type: str, value: object, *, now: datetime | None = None) -> str | None:
    if lock_type == "none":
        return None
    if lock_type == "permanent":
        return PERMANENT_UNLOCK_DATE
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(
            "定时锁需要 unlock_date，且必须是未来的时间。"
            "可以写日期（2027-01-01），也可以写完整时刻（2027-01-01T09:00:00+08:00）。"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"解锁时间「{raw}」看不懂。需要 YYYY-MM-DD 或 ISO 8601，"
            "例如 2027-01-01 或 2027-01-01T09:00:00+08:00。"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # 没写时区就按配置时区理解——「2027-01-01 解锁」指的是本地那一天，
        # 不该因为部署在哪台机器上而漂移。时区配置见 config.yaml 的 timezone。
        parsed = parsed.replace(tzinfo=get_tzinfo())
    current = now or datetime.now(timezone.utc)
    if parsed.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        raise ValueError(
            f"解锁时间「{parsed.isoformat()}」不在未来。"
            f"未写时区的日期按 {get_timezone_name()} 理解，请给一个更晚的时间。"
        )
    return parsed.isoformat()


def _is_actual_relation_name(value: object) -> bool:
    name = str(value or "").strip()
    return bool(name) and name.casefold() not in _GENERIC_RELATION_NAMES


def author_side(author: object, *, ai_name: str | None = None) -> str | None:
    raw = str(author or "").strip()
    low = raw.casefold()
    if low in _HUMAN_AUTHOR_ALIASES:
        return "human"
    configured_ai = str(ai_name or get_ai_name() or "").strip()
    if low in _AI_AUTHOR_ALIASES or (configured_ai and raw == configured_ai):
        return "ai"
    return None


def resolve_writer_name(
    caller_side: str | None,
    *,
    author: object,
    user_name: object = "",
    ai_name: object = "",
) -> str | None:
    raw_author = str(author or "").strip()
    if caller_side == "human":
        candidates = (user_name, get_owner_name(), raw_author)
    else:
        candidates = (ai_name, get_ai_name(), raw_author)
    return next((str(v).strip() for v in candidates if _is_actual_relation_name(v)), None)


def letter_lock_revision(bucket: dict) -> tuple[str, str, str]:
    """返回可用于原子写入前置校验的锁字段快照。"""
    meta = bucket.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    return (
        str(meta.get("lock_type") or "").strip().casefold(),
        str(meta.get("unlock_date") or "").strip(),
        str(meta.get("locked_by") or "").strip().casefold(),
    )


async def normalize_expired_lock(
    bucket: dict,
    state: dict,
    caller_side: str | None,
    *,
    bucket_mgr=None,
) -> tuple[dict | None, dict]:
    if not state.get("expired"):
        return bucket, state
    manager = bucket_mgr or rt.bucket_mgr
    try:
        committed = await manager.update(
            bucket["id"],
            lock_type="none",
            unlock_date=None,
            expected_lock_state=letter_lock_revision(bucket),
        )
        get_bucket = getattr(manager, "get", None)
        latest = await get_bucket(bucket["id"]) if callable(get_bucket) else None
    except Exception:
        return None, state
    if latest is None and committed:
        # 极简兼容 manager 可能没有 get()；正式 BucketManager 始终走上面的
        # 原子前置校验和回读路径。
        latest = bucket
    if not latest:
        return None, state
    return latest, letter_lock_state(latest, caller_side)


def safe_letter_metadata(bucket: dict, caller_side: str | None) -> dict:
    meta = bucket.get("metadata") or {}
    state = letter_lock_state(bucket, caller_side)
    writer_name = str(meta.get("writer_name") or "").strip()
    payload = {
        "id": bucket.get("id", ""),
        "author": meta.get("author", ""),
        "user_name": meta.get("user_name", ""),
        "writer_name": writer_name,
        "date": meta.get("letter_date") or str(meta.get("created", ""))[:10],
        "created": meta.get("created", ""),
        "lock_type": state["lock_type"],
        "unlock_date": state["unlock_date"],
        "locked": state["locked"],
        "lock_owner": state["owner"],
        "lock_upgrade_available": (
            state["locked_by"] is None
            and state["stored_lock_type"] == "none"
        ),
    }
    if not state["locked"]:
        payload["title"] = meta.get("title", "") or meta.get("name", "")
        payload["content"] = strip_wikilinks(bucket.get("content", ""))
    return payload


async def plan_create(
    content: str,
    status: Optional[str] = "active",
    related_bucket: Optional[str] = "",
    weight: Optional[float] = 0.5,
    why_remembered: Optional[str] = "",
) -> str:
    if status is None:
        status = "active"
    if related_bucket is None:
        related_bucket = ""
    if weight is None:
        weight = 0.5
    if why_remembered is None:
        why_remembered = ""
    try:
        parsed_weight = float(weight)
    except (TypeError, ValueError, OverflowError):
        parsed_weight = 0.5
    if not math.isfinite(parsed_weight):
        parsed_weight = 0.5
    weight = max(0.0, min(1.0, parsed_weight))
    why_remembered = str(why_remembered).strip()[:500]
    await rt.decay_engine.ensure_started()
    if not content or not content.strip():
        raise ToolInputError("内容为空，无法登记计划。")
    size_err = check_content_size(content)
    if size_err:
        raise ToolInputError(size_err)
    metadata_err = check_metadata_size(
        status=status,
        related_bucket=related_bucket,
        why_remembered=why_remembered,
    )
    if metadata_err:
        raise ToolInputError(metadata_err)
    status = status.strip().lower()
    if status not in ("active", "resolved", "abandoned"):
        status = "active"

    norm = content.strip()
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        for b in all_buckets:
            m = b.get("metadata", {})
            if (
                m.get("type") == "plan"
                and m.get("status", "active") == "active"
                and (b.get("content") or "").strip() == norm
            ):
                return f"跟原有 active plan 完全重复→{b['id']}（未重复登记）"
    except Exception as e:
        rt.logger.warning(f"plan() dedup scan failed: {e}")

    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__plan__"],
        importance=7,
        domain=["plan"],
        valence=0.5,
        arousal=0.4,
        name=None,
        bucket_type="plan",
        why_remembered=why_remembered,
        weight=weight,
        source_tool="plan",
        event_actor="llm",
    )
    from .._common import append_plan_change_log
    initial_log = append_plan_change_log([], "created", to=status, by="plan")
    update_kwargs = {"status": status, "change_log": initial_log}
    if related_bucket.strip():
        update_kwargs["related_bucket"] = related_bucket.strip()
    try:
        await rt.bucket_mgr.update(bucket_id, **update_kwargs)
    except Exception as e:
        rt.logger.warning(f"plan() failed to set status/related: {e}")
    # 注意：bucket_mgr.create() 已在 content 落盘后投递 embedding outbox
    # 向量，这里不需要也不应该重复调用 generate_and_store（见 hold/feel.py 同类注释）。
    return f"📋plan→{bucket_id} [{status}]"


async def letter_write(
    author: str,
    content: str,
    user_name: Optional[str] = "",
    title: Optional[str] = "",
    date: Optional[str] = "",
    ai_name: Optional[str] = "",
    lock_type: Optional[str] = "none",
    unlock_date: Optional[str] = "",
) -> str:
    if user_name is None:
        user_name = ""
    if title is None:
        title = ""
    if date is None:
        date = ""
    try:
        normalized_lock = normalize_lock_type(lock_type)
        normalized_unlock = normalize_unlock_date(normalized_lock, unlock_date)
    except ValueError as exc:
        raise ToolInputError(f"无法创建带锁 Letter：{exc}")
    # ai_name：显式传入优先，否则取环境变量 AI_NAME（回退 "AI"）。
    ai = (ai_name or "").strip() or get_ai_name()
    if not author or not author.strip():
        raise ToolInputError("author 不能为空。")
    if not content or not content.strip():
        raise ToolInputError("信件内容不能为空。")
    size_err = check_content_size(content)
    if size_err:
        raise ToolInputError(size_err)
    metadata_err = check_metadata_size(
        author=author,
        user_name=user_name,
        title=title,
        date=date,
        ai_name=ai_name,
    )
    if metadata_err:
        raise ToolInputError(metadata_err)

    # 署名归一化：
    #   - "user" → 用户侧，存 "user"（用户名另存 user_name，逻辑不变）
    #   - "ai" / 等于 ai_name / 旧值 "claude"（历史兼容）→ 统一存 ai_name 的值
    #   - 其它任意字符串 → 原样作为署名
    raw = author.strip()
    low = raw.lower()
    if low == "user":
        a = "user"
    elif low in ("ai", "claude") or raw == ai:
        a = ai
    else:
        a = raw

    if normalized_lock != "none":
        claimed_side = author_side(raw, ai_name=ai)
        if claimed_side == "human":
            raise ToolInputError("无法创建带锁 Letter：当前 MCP/stdio 入口不能替对方创建带锁信。无锁代存仍然可用。")
        writer_name = resolve_writer_name(
            "ai", author=raw, user_name=user_name, ai_name=ai_name
        )
        if not writer_name:
            raise ToolInputError("无法创建带锁 Letter：未能从现有 ai_name / AI_NAME / author 中取得"
                "当前写信人的实际关系名。请先完善现有名称配置；普通无锁 Letter 不受影响。")
    else:
        writer_name = resolve_writer_name(
            "ai", author=raw, user_name=user_name, ai_name=ai_name
        )

    extra_meta = {"author": a}
    if user_name.strip():
        extra_meta["user_name"] = user_name.strip()
    if title.strip():
        extra_meta["title"] = title.strip()[:120]
    if date.strip():
        extra_meta["letter_date"] = date.strip()
    extra_meta.update({
        "lock_type": normalized_lock,
        "unlock_date": normalized_unlock,
        "locked_by": "ai",
    })
    if writer_name:
        extra_meta["writer_name"] = writer_name

    bucket_id = await rt.bucket_mgr.create(
        content=content.strip(),
        tags=["__letter__"],
        importance=10,
        domain=["letter"],
        valence=0.5,
        arousal=0.3,
        name=(title.strip()[:60] or f"{a}_{date.strip() or 'letter'}"),
        bucket_type="letter",
        source_tool="letter",
        event_actor="llm",
        lock_type=normalized_lock,
        unlock_date=normalized_unlock,
        locked_by="ai",
        writer_name=writer_name or "",
    )
    try:
        await rt.bucket_mgr.update(bucket_id, **extra_meta)
    except Exception as e:
        rt.logger.warning(f"letter_write update meta failed: {e}")
    # 注意：bucket_mgr.create() 已在 content 落盘后投递 embedding outbox
    # 向量，这里不需要也不应该重复调用 generate_and_store。
    if normalized_lock != "none":
        lock_suffix = f" 🔒{normalized_lock}"
        if normalized_unlock:
            lock_suffix += f" 解锁:{normalized_unlock}"
        return f"💌letter→{bucket_id} [{a}]{lock_suffix}"
    return f"💌letter→{bucket_id} [{a}]"


async def letter_lock_update(
    letter_id: str,
    lock_type: str,
    unlock_date: Optional[str] = "",
    *,
    caller_side: str = "ai",
) -> str:
    """Change only lock metadata; caller identity is supplied by trusted entrypoints."""
    if not letter_id or not letter_id.strip():
        raise ToolInputError("letter_id 不能为空")
    try:
        normalized_lock = normalize_lock_type(lock_type)
        normalized_unlock = normalize_unlock_date(normalized_lock, unlock_date)
    except ValueError as exc:
        raise ToolInputError(f"无法修改 Letter 锁：{exc}")
    bucket = await rt.bucket_mgr.get(letter_id.strip())
    if not bucket or not is_letter_bucket(bucket):
        raise ToolInputError("未找到该 Letter")
    state = letter_lock_state(bucket, caller_side)
    if not state["locked_by"]:
        raise ToolInputError("历史无锁 Letter 没有锁所有者，不能通过锁管理入口补设锁。请新写一封带锁 Letter。")
    if not state["owner"]:
        raise ToolInputError("只有创建这把锁的一方可以修改 Letter 锁状态。")
    meta = bucket.get("metadata") or {}
    claimed_side = author_side(meta.get("author"))
    legacy_ai_conversion = (
        meta.get("lock_owner_source") == "legacy_ai_conversion"
        and state["locked_by"] == "ai"
    )
    if (
        normalized_lock != "none"
        and claimed_side
        and claimed_side != caller_side
        and not legacy_ai_conversion
    ):
        raise ToolInputError("无法上锁：这封无锁 Letter 的署名方向与当前可信入口不一致；代存信不能事后转换为锁信。")
    writer_name = str(meta.get("writer_name") or "").strip()
    if normalized_lock != "none" and not _is_actual_relation_name(writer_name):
        raise ToolInputError("无法上锁：这封 Letter 创建时没有记录实际关系名，请新写一封带锁 Letter。")
    updates = {
        "lock_type": normalized_lock,
        "unlock_date": normalized_unlock,
    }
    expected_lock_state = letter_lock_revision(bucket)
    ok = await rt.bucket_mgr.update(
        bucket["id"],
        expected_lock_state=expected_lock_state,
        **updates,
    )
    if not ok:
        latest = await rt.bucket_mgr.get(bucket["id"])
        if latest and letter_lock_revision(latest) != expected_lock_state:
            raise ToolInputError("Letter 锁状态已被并发修改，请重新读取后再试")
        raise ToolInputError("Letter 锁状态修改失败")
    if normalized_lock == "none":
        return f"🔓 已解锁 {bucket['id']}，恢复默认可读。"
    if normalized_lock == "permanent":
        return f"🔒 已将 {bucket['id']} 设为永久锁。"
    return f"🔒 已将 {bucket['id']} 设为定时锁，解锁日期：{normalized_unlock}。"


async def letter_read(
    query: Optional[str] = "",
    limit: Optional[int] = 10,
    author: Optional[str] = "",
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
) -> str:
    if query is None:
        query = ""
    if limit is None:
        limit = 10
    if author is None:
        author = ""
    if date_from is None:
        date_from = ""
    if date_to is None:
        date_to = ""
    # 同文件 218/225/301/310 行对同类失败是抛的，这里过去是 return。
    query_err = check_query_size(query)
    if query_err:
        raise ToolInputError(query_err)
    metadata_err = check_metadata_size(
        author=author,
        date_from=date_from,
        date_to=date_to,
    )
    if metadata_err:
        raise ToolInputError(metadata_err)
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError, OverflowError):
        limit = 10
    try:
        all_b = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        raise ToolInputError(f"读取信件失败: {safe_error_detail(e)}")
    normalized_letters = []
    states = {}
    for bucket in (b for b in all_b if is_letter_bucket(b)):
        state = letter_lock_state(bucket, "ai")
        bucket, state = await normalize_expired_lock(bucket, state, "ai")
        if not bucket:
            continue
        normalized_letters.append(bucket)
        states[bucket["id"]] = state
    letters = normalized_letters
    visible_letters = [b for b in letters if not states[b["id"]]["locked"]]
    af = author.strip()
    if af:
        ai = get_ai_name()
        af_low = af.lower()
        if af_low == "user":
            letters = [b for b in letters if b["metadata"].get("author") == "user"]
        elif af_low in ("ai", "claude") or af == ai:
            # AI 侧：匹配新署名 ai_name + 历史遗留的 "claude"
            ai_aliases = {ai, "claude"}
            letters = [b for b in letters if b["metadata"].get("author") in ai_aliases]
        else:
            # 任意自定义署名：精确匹配存储值
            letters = [b for b in letters if b["metadata"].get("author") == af]

    def _within(b):
        d = b["metadata"].get("letter_date") or b["metadata"].get("created", "")
        if date_from and d and d < date_from:
            return False
        if date_to and d and d > date_to:
            return False
        return True

    letters = [b for b in letters if _within(b)]

    query_text = query.strip()
    if query_text:
        # A locked letter must not become discoverable from its hidden title or
        # body through either lexical or semantic recall.
        letters = [b for b in letters if not states[b["id"]]["locked"]]

    def _matches_query(b):
        if not query_text:
            return True
        meta = b.get("metadata", {})
        parts = [
            b.get("content", ""),
            str(meta.get("name") or ""),
            str(meta.get("title") or ""),
            str(meta.get("author") or ""),
        ]
        parts.extend(str(t) for t in (meta.get("tags") or []))
        return query_text.lower() in "\n".join(parts).lower()

    if query_text and rt.embedding_engine and getattr(rt.embedding_engine, "enabled", False):
        try:
            # Hidden letters are excluded before vector ranking, not after it.
            allowed_ids = {b["id"] for b in visible_letters}
            sims = await rt.embedding_engine.search_similar(
                query_text,
                top_k=limit * 3,
                allowed_bucket_ids=allowed_ids,
            )
            id_score = {bid: sc for bid, sc in sims}
            # 字面精确命中与语义相似取并集，且字面优先。
            # 此前只要向量返回任何结果就整体替换候选集，字面匹配被完全跳过；
            # search_similar 在这里不设相似度门槛，于是搜一个确切的词组时，
            # 真正含它的那封信可能被一堆低相关结果挤出 top_k——信里明明写着，
            # 却搜不到，而且只在启用向量时才犯病。
            # 救命怎么又冒出来六个 bug
            literal_matches = [b for b in letters if _matches_query(b)]
            literal_ids = {b["id"] for b in literal_matches}
            literal_matches.sort(
                key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""),
                reverse=True,
            )
            vector_matches = [
                b for b in letters if b["id"] in id_score and b["id"] not in literal_ids
            ]
            vector_matches.sort(key=lambda b: id_score.get(b["id"], 0.0), reverse=True)
            letters = literal_matches + vector_matches
        except Exception as e:
            rt.logger.warning(f"letter_read vector search failed: {e}")
            letters = [b for b in letters if _matches_query(b)]
            letters.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    else:
        if query_text:
            letters = [b for b in letters if _matches_query(b)]
        letters.sort(key=lambda b: b["metadata"].get("letter_date") or b["metadata"].get("created", ""), reverse=True)

    letters = letters[:limit]
    if not letters:
        return "没有找到匹配的信件。"
    parts = []
    for b in letters:
        m = b["metadata"]
        state = states[b["id"]]
        if state["locked"]:
            safe = {
                "letter_id": b["id"],
                "author": m.get("writer_name") or m.get("author", ""),
                "created_at": m.get("created", ""),
                "lock_type": state["lock_type"],
                "unlock_date": state["unlock_date"],
                "locked": True,
            }
            parts.append(json.dumps(safe, ensure_ascii=False, separators=(",", ":")))
            continue
        a = m.get("author", "?")
        d = (m.get("letter_date") or m.get("created", ""))[:10]
        title = m.get("title") or m.get("name", "")
        payload = (
            f"[{b['id']}] {a} · {d}{(' · ' + title) if title else ''}\n"
            + strip_wikilinks(b["content"])
        )
        parts.append(payload)
    return "=== 信件 ===\n" + "\n\n---\n\n".join(parts)
