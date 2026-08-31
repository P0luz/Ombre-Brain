"""
========================================
tools/i/core.py — AI 自我认知的沉淀与存取
========================================

I 是 OB 的自我感知层，但它不是日记，是沉淀物。

一条「我觉得……」先以**普通记忆**的形式写下来（候选），跟别的记忆一起
浮现、一起衰减、一起进 dream。每次 dream 都会把它和语义相关的材料摆在
一起——支持它的、反驳它的、跟它撞车的其它候选——碰撞过程留在记忆里。
被不同日期的 dream 见证够 3 次之后，模型才可以把它升级成正式 I 条目。

关键行为：
- 写入模式（content 非空）：创建 **普通 dynamic 桶**，tag `__i_candidate__`，
  i_stage="candidate"，会正常浮现、正常衰减——站不住的自然沉下去
- 升级模式（promote 非空）：见证次数够了才创建 type="i" 桶（dont_surface=True），
  候选桶保留痕迹并标 i_stage="promoted" + resolved
- 读取模式（read=True 或全空）：正式 I 条目 + 待沉淀候选清单；早期直写的
  历史条目显式标注「未经沉淀」
- record_dream_pass()：dream 渲染完所有可见候选后回调，按天去重记一次见证

不做什么（边界）：
- 不判断「矛盾 / 重复 / 该加权」——那是认知层，rule.md 第 5 条。系统只摆材料，
  结论由模型自己下
- 不提供绕过候选阶段直写正式 I 的后门；I 是沉淀的结果，不是输入
- 升级不删除候选桶（rule.md 第 1 条：记忆可以淡去，不能被抹去）
- aspect 只允许固定维度，防止把任意控制文本混入标签

对外暴露：
- i_core(content, aspect, read, limit, promote) → str
- record_dream_pass(bucket_ids) → int（本次新增见证的候选数）
- I_CANDIDATE_TAG / I_PROMOTE_THRESHOLD（dream 侧复用）
========================================
"""

from errors import ToolInputError
from datetime import datetime
from typing import Optional

from .. import _runtime as rt
from .._common import check_content_size, check_metadata_size
from ..plan.core import is_letter_bucket
from errors import safe_error_detail

_VALID_ASPECTS = {"nature", "values", "patterns", "limits", "becoming", "uncertainty", "stance"}

# 候选桶的标记 tag。刻意不叫 `__i__`：SessionStart 注入和 Dashboard 都按
# `__i__` / type=="i" 认正式条目，候选不能混进去被当成已成立的自我认知。
I_CANDIDATE_TAG = "__i_candidate__"

# 升级门槛：被多少个**不同日期**的 dream 见证过。同一天做几次梦只算一次，
# 避免连续调用 dream 把一个刚写下的念头刷成「沉淀」。
I_PROMOTE_THRESHOLD = 3


async def i_core(
    content: Optional[str] = "",
    aspect: Optional[str] = "",
    read: Optional[bool] = False,
    limit: Optional[int] = 20,
    promote: Optional[str] = "",
) -> str:
    content = "" if content is None else str(content)
    aspect = "" if aspect is None else str(aspect)
    promote = "" if promote is None else str(promote).strip()
    if read is None:
        read = False
    try:
        limit = max(1, min(100, int(limit if limit is not None else 20)))
    except (TypeError, ValueError, OverflowError):
        limit = 20
    aspect = aspect.strip().lower()

    metadata_err = check_metadata_size(aspect=aspect, promote=promote)
    if metadata_err:
        return metadata_err

    if rt.mark_op:
        rt.mark_op("I")

    await rt.decay_engine.ensure_started()

    if promote:
        # 两处 size 检查都在写入之前，超限时一个桶都没建。
        size_err = check_content_size(content) if content.strip() else ""
        if size_err:
            raise ToolInputError(size_err)
        return await _promote_candidate(promote, content.strip())
    if read or not content.strip():
        return await _read_i(limit)
    if aspect and aspect not in _VALID_ASPECTS:
        choices = ", ".join(sorted(_VALID_ASPECTS))
        raise ToolInputError(f"aspect 无效：{aspect}。可选值: {choices}")
    size_err = check_content_size(content)
    if size_err:
        raise ToolInputError(size_err)
    return await _write_candidate(content.strip(), aspect)


def _aspect_of(meta: dict) -> str:
    tags = meta.get("tags") or []
    return next(
        (t.replace("aspect:", "") for t in tags if isinstance(t, str) and t.startswith("aspect:")),
        "",
    )


def dream_dates(meta: dict) -> list[str]:
    """Return unique witness dates in their original order."""
    raw = meta.get("i_dream_dates") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    dates: list[str] = []
    seen: set[str] = set()
    for value in raw:
        date = str(value).strip()[:10]
        if date and date not in seen:
            seen.add(date)
            dates.append(date)
    return dates
def is_pending_candidate(bucket: dict) -> bool:
    """一条桶是否是「还在等待沉淀」的 I 候选。"""
    meta = bucket.get("metadata") or {}
    if str(meta.get("i_stage") or "") != "candidate":
        return False
    return I_CANDIDATE_TAG in (meta.get("tags") or [])


async def _write_candidate(content: str, aspect: str) -> str:
    tags = [I_CANDIDATE_TAG]
    if aspect:
        tags.append(f"aspect:{aspect}")

    try:
        bucket_id = await rt.bucket_mgr.create(
            content=content,
            tags=tags,
            importance=6,
            domain=["self"],
            valence=0.5,
            arousal=0.3,
            name=None,
            # 刻意是 dynamic 而不是 "i"：候选就该跟普通记忆一样浮现、
            # 一样衰减、一样进 dream 窗口，才可能真的被碰撞到。
            bucket_type="dynamic",
            why_remembered="",
            weight=0.8,
            source_tool="I",
            event_actor="llm",
        )
    except Exception as e:
        raise ToolInputError(f"写入失败: {safe_error_detail(e)}")

    try:
        marked = await rt.bucket_mgr.update(
            bucket_id, i_stage="candidate", i_dream_dates=[]
        )
    except Exception as e:
        rt.logger.warning(f"I candidate stage marking failed for {bucket_id}: {e}")
        return (
            f"⚠️ 候选已落盘（{bucket_id}），但候选状态标记失败："
            f"{safe_error_detail(e)}。"
            "它现在是一条普通记忆，不会进入沉淀流程。"
        )
    if not marked:
        rt.logger.warning("I candidate stage marking returned false for %s", bucket_id)
        return (
            f"⚠️ 候选已落盘（{bucket_id}），但候选状态标记失败。"
            "它现在是一条普通记忆，不会进入沉淀流程。"
        )

    aspect_label = f"[{aspect}] " if aspect else ""
    return (
        f"🌱 我觉得 {aspect_label}→{bucket_id}\n"
        f"这还只是一个念头，不是自我认知。它现在是一条普通记忆，会浮现也会衰减。\n"
        f"接下来 {I_PROMOTE_THRESHOLD} 次 dream 会把它和相关记忆摆在一起给你看；"
        f"如果它还站得住，用 I(promote=\"{bucket_id}\") 让它进 I。"
    )


async def _promote_candidate(bucket_id: str, content_override: str) -> str:
    try:
        bucket = await rt.bucket_mgr.get(bucket_id)
    except Exception as e:
        raise ToolInputError(f"读取失败: {safe_error_detail(e)}")
    if not bucket:
        raise ToolInputError(
            f"找不到候选 {bucket_id}（可能已经衰减归档——那本身就是一种答案）。"
        )

    meta = bucket.get("metadata") or {}
    stage = str(meta.get("i_stage") or "")
    if stage == "promoted":
        target = meta.get("i_promoted_to") or "?"
        return f"{bucket_id} 已经沉淀过了 → {target}。"
    if not is_pending_candidate(bucket):
        raise ToolInputError(f"{bucket_id} 不是 I 候选，不能直接进 I。"
            "自我认知要先写成「我觉得……」的候选，经过 dream 才可能沉淀。")

    dates = dream_dates(meta)
    if len(dates) < I_PROMOTE_THRESHOLD:
        missing = I_PROMOTE_THRESHOLD - len(dates)
        seen = "、".join(dates) if dates else "还没有"
        raise ToolInputError(
            f"还不够。{bucket_id} 被 dream 见证过 {len(dates)}/{I_PROMOTE_THRESHOLD} 次"
            f"（{seen}），还差 {missing} 次。"
            "不是形式——没有在几次不同的梦里都站得住，它就还只是一时的想法。"
        )

    body = content_override or str(bucket.get("content") or "").strip()
    if not body:
        raise ToolInputError(f"{bucket_id} 正文是空的，没有可以沉淀的内容。")

    aspect = _aspect_of(meta)
    tags = ["__i__"]
    if aspect:
        tags.append(f"aspect:{aspect}")

    try:
        new_id = await rt.bucket_mgr.create(
            content=body,
            tags=tags,
            importance=6,
            domain=["self"],
            valence=0.5,
            arousal=0.3,
            name=None,
            bucket_type="i",
            why_remembered="",
            weight=0.8,
            source_tool="I",
            event_actor="llm",
        )
    except Exception as e:
        raise ToolInputError(f"沉淀失败: {safe_error_detail(e)}")

    try:
        await rt.bucket_mgr.update(
            new_id,
            dont_surface=True,
            i_from_candidate=bucket_id,
            i_dream_dates=list(dates),
        )
    except Exception as e:
        rt.logger.warning(f"I promoted bucket metadata write failed for {new_id}: {e}")

    # 候选桶留着，只改状态——升级不是搬走，是这条张力闭合了。
    try:
        await rt.bucket_mgr.update(
            bucket_id,
            i_stage="promoted",
            i_promoted_to=new_id,
            resolved=True,
        )
    except Exception as e:
        rt.logger.warning(f"I candidate close-out failed for {bucket_id}: {e}")

    aspect_label = f"[{aspect}] " if aspect else ""
    return (
        f"🪞I {aspect_label}→{new_id}\n"
        f"经过 {len(dates)} 次 dream 沉淀，从候选 {bucket_id} 升上来。候选原样留着。"
    )


async def record_dream_pass(bucket_ids: list) -> int:
    """给这些候选各记一次「被 dream 见证过」，按天去重。

    只有真的在 dream 输出里被渲染出来的候选才该调这里——没被看见的，
    不算经历过。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    recorded = 0
    for bucket_id in bucket_ids or []:
        bucket_id = str(bucket_id or "").strip()
        if not bucket_id:
            continue
        try:
            bucket = await rt.bucket_mgr.get(bucket_id)
        except Exception as e:
            rt.logger.warning(f"I dream pass lookup failed for {bucket_id}: {e}")
            continue
        if not bucket or not is_pending_candidate(bucket):
            continue
        dates = dream_dates(bucket.get("metadata") or {})
        if today in dates:
            continue
        try:
            updated = await rt.bucket_mgr.update(
                bucket_id, i_dream_dates=[*dates, today]
            )
            if not updated:
                rt.logger.warning(
                    "I dream pass write returned false for %s", bucket_id
                )
                continue
            recorded += 1
        except Exception as e:
            rt.logger.warning(f"I dream pass write failed for {bucket_id}: {e}")
    return recorded


async def _read_i(limit: int) -> str:
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        raise ToolInputError(f"读取失败: {safe_error_detail(e)}")

    i_buckets = [
        b for b in all_buckets
        if b.get("metadata", {}).get("type") == "i"
        and not is_letter_bucket(b)
    ]
    pending = [
        b for b in all_buckets
        if is_pending_candidate(b) and not is_letter_bucket(b)
    ]

    if not i_buckets and not pending:
        return "还没有任何自我认知记录，也没有正在沉淀的候选。"

    i_buckets.sort(
        key=lambda b: b.get("metadata", {}).get("last_active", ""),
        reverse=True,
    )
    i_buckets = i_buckets[:limit]

    lines = []
    if i_buckets:
        lines.append(f"=== 我的自我认知（{len(i_buckets)} 条）===")
        for b in i_buckets:
            meta = b.get("metadata", {})
            aspect_tag = _aspect_of(meta)
            ts = (meta.get("last_active") or "")[:10]
            aspect_label = f"[{aspect_tag}] " if aspect_tag else ""
            # 早期条目是直接写进来的，没经过任何碰撞。标出来，别让它们
            # 和沉淀下来的东西看起来一样可靠。
            origin = (
                f"（经 {len(dream_dates(meta))} 次 dream 沉淀）"
                if meta.get("i_from_candidate")
                else "（早期直接写入，未经沉淀）"
            )
            text = (b.get("content") or "").strip()
            payload = f"{ts} {aspect_label}{b['id']} {origin}\n{text}"
            lines.append("\n" + payload)

    if pending:
        pending.sort(
            key=lambda b: b.get("metadata", {}).get("created", ""),
            reverse=True,
        )
        lines.append(f"\n=== 正在沉淀的「我觉得」（{len(pending)} 条）===")
        lines.append("这些还不是自我认知，只是念头。够 "
                     f"{I_PROMOTE_THRESHOLD} 次 dream 见证才能用 I(promote=\"...\") 升上来。")
        for b in pending:
            meta = b.get("metadata", {})
            aspect_tag = _aspect_of(meta)
            aspect_label = f"[{aspect_tag}] " if aspect_tag else ""
            passes = len(dream_dates(meta))
            created = (meta.get("created") or "")[:10]
            text = (b.get("content") or "").strip()
            payload = (
                f"{created} {aspect_label}{b['id']} "
                f"（{passes}/{I_PROMOTE_THRESHOLD} 次 dream）\n{text}"
            )
            lines.append("\n" + payload)

    return "\n".join(lines)
