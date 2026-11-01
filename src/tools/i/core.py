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
from utils import parse_iso_datetime
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
    supersedes: Optional[str] = "",
) -> str:
    content = "" if content is None else str(content)
    aspect = "" if aspect is None else str(aspect)
    promote = "" if promote is None else str(promote).strip()
    supersedes = "" if supersedes is None else str(supersedes).strip()
    if read is None:
        read = False
    try:
        limit = max(1, min(100, int(limit if limit is not None else 20)))
    except (TypeError, ValueError, OverflowError):
        limit = 20
    aspect = aspect.strip().lower()

    metadata_err = check_metadata_size(
        aspect=aspect, promote=promote, supersedes=supersedes
    )
    if metadata_err:
        raise ToolInputError(metadata_err)

    if rt.mark_op:
        rt.mark_op("I")

    await rt.decay_engine.ensure_started()

    if promote:
        # 两处 size 检查都在写入之前，超限时一个桶都没建。
        size_err = check_content_size(content) if content.strip() else ""
        if size_err:
            raise ToolInputError(size_err)
        return await _promote_candidate(promote, content.strip(), supersedes)
    if read or not content.strip():
        return await _read_i(limit)
    if aspect and aspect not in _VALID_ASPECTS:
        choices = ", ".join(sorted(_VALID_ASPECTS))
        raise ToolInputError(f"aspect 无效：{aspect}。可选值: {choices}")
    size_err = check_content_size(content)
    if size_err:
        raise ToolInputError(size_err)
    # 校验放在建桶之前：supersedes 不合法时，一条候选都不该留下来。
    if supersedes:
        await _resolve_supersedes(supersedes, aspect)
    return await _write_candidate(content.strip(), aspect, supersedes)


def _aspect_of(meta: dict) -> str:
    tags = meta.get("tags") or []
    return next(
        (t.replace("aspect:", "") for t in tags if isinstance(t, str) and t.startswith("aspect:")),
        "",
    )


def _aspect_label_of(meta: dict) -> str:
    aspect = _aspect_of(meta)
    return f"[{aspect}] " if aspect else ""


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


def superseded_by(bucket: dict) -> str:
    """这条正式 I 条目被哪条取代了；没有就是空串。"""
    return str((bucket.get("metadata") or {}).get("i_superseded_by") or "").strip()


def disputing_candidates(bucket: dict, buckets_by_id: dict) -> list[str]:
    """此刻真的在质疑这条 I 条目的候选。

    **动态算，不存 flag。** 一条候选声明 supersedes 之后，旧条目就不再被当成
    当前信念读出去；但如果那条候选后来衰减归档了、或者模型再没管它，质疑就该
    自己解除——否则一条旧认知会被一个早已不存在的念头永久悬着，那比它继续被
    当成真理更糟：模型会既没有旧的、也没有新的。

    所以这里每次都回头看：声明过的那些 id，现在还挂在候选区的才算数。
    """
    raw = (bucket.get("metadata") or {}).get("i_disputed_by") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    live: list[str] = []
    for candidate_id in raw:
        candidate_id = str(candidate_id or "").strip()
        if not candidate_id or candidate_id in live:
            continue
        candidate = buckets_by_id.get(candidate_id)
        if candidate and is_pending_candidate(candidate):
            live.append(candidate_id)
    return live


async def _resolve_supersedes(target_id: str, aspect: str) -> dict:
    """校验 supersedes 指向的正式 I 条目。

    在任何写入之前调用——校验不过时一个桶都不该建出来。
    """
    try:
        target = await rt.bucket_mgr.get(target_id)
    except Exception as e:
        raise ToolInputError(f"读取失败: {safe_error_detail(e)}")
    if not target:
        raise ToolInputError(f"supersedes 指向的 {target_id} 不存在。")

    meta = target.get("metadata") or {}
    if meta.get("type") != "i" or is_letter_bucket(target):
        raise ToolInputError(
            f"{target_id} 不是正式 I 条目，不能被取代。supersedes 只能指向已经"
            "沉淀进 I 的自我认知——还在候选区的念头不需要取代，让它自己沉下去。"
        )

    already = superseded_by(target)
    if already:
        raise ToolInputError(
            f"{target_id} 已经被 {already} 取代过了。要继续改这条认识，"
            f"应该指向链尾的 {already}。"
        )

    # 跨 aspect 的取代不是迭代，是拿一个维度盖掉另一个维度。两边都标了 aspect
    # 才管——早期直写条目很多没有 aspect，不该因此没法被修正。
    target_aspect = _aspect_of(meta)
    if aspect and target_aspect and aspect != target_aspect:
        raise ToolInputError(
            f"aspect 对不上：新认识是 [{aspect}]，{target_id} 是 [{target_aspect}]。"
            "取代是同一个维度上的迭代。如果想说的确实是另一件事，"
            "直接写成新候选就好，不要用 supersedes。"
        )
    return target


async def _write_candidate(content: str, aspect: str, supersedes: str = "") -> str:
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

    marks: dict = {"i_stage": "candidate", "i_dream_dates": []}
    if supersedes:
        marks["i_supersedes"] = supersedes
    try:
        marked = await rt.bucket_mgr.update(bucket_id, **marks)
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
    dispute_note = ""
    if supersedes:
        # 挂起旧条目**现在就生效**，不等这条候选攒够见证。
        #
        # 两件事本来被绑在同一个门槛上：「新认识站不站得住」该慢慢验，而
        # 「一条已经被自己质疑的旧认识还该不该当成当前信念读出去」是此刻就该
        # 回答的。绑在一起的结果是慢的那个拖着快的那个——旧的继续当真理用了
        # 十几天，只因为新的还在排队。拆开之后，新条目的门槛一次都没少。
        if await _mark_disputed(supersedes, bucket_id):
            dispute_note = (
                f"\n同时 {supersedes} 已挂起：它不再作为当前的自我认知读出去，"
                "但一个字都没删，你随时能看到它，质疑撤了它就回来。"
            )
        else:
            dispute_note = (
                f"\n⚠️ 但 {supersedes} 挂起失败，它现在仍会被当成当前的自我认知。"
            )

    return (
        f"🌱 我觉得 {aspect_label}→{bucket_id}\n"
        f"这还只是一个念头，不是自我认知。它现在是一条普通记忆，会浮现也会衰减。\n"
        f"接下来 {I_PROMOTE_THRESHOLD} 次 dream 会把它和相关记忆摆在一起给你看；"
        f"如果它还站得住，用 I(promote=\"{bucket_id}\") 让它进 I。"
        f"{dispute_note}"
    )


async def _mark_disputed(target_id: str, candidate_id: str) -> bool:
    """在被取代的 I 条目上记一笔「这条候选正在质疑我」。"""
    try:
        target = await rt.bucket_mgr.get(target_id)
        raw = (target.get("metadata") or {}).get("i_disputed_by") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        existing = [str(v or "").strip() for v in raw if str(v or "").strip()]
        if candidate_id not in existing:
            existing.append(candidate_id)
        return bool(await rt.bucket_mgr.update(target_id, i_disputed_by=existing))
    except Exception as e:
        rt.logger.warning(f"I dispute marking failed for {target_id}: {e}")
        return False


async def _promote_candidate(
    bucket_id: str, content_override: str, supersedes: str = ""
) -> str:
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

    # 取代目标：显式传参优先，否则用候选写下时声明的那条。
    #
    # 两者的失败处理刻意不同。显式传的参数错了就该报错——那是这次调用的输入。
    # 而从候选继承来的目标可能在它排队的这些天里被别的条目取代了，这时候
    # 把整个 promote 挡掉是错的惩罚：这条自我认知本身是有效的，只是链接不上了。
    # 降级成「照常升，链没接上，告诉你为什么」。
    chain_target = supersedes or str(meta.get("i_supersedes") or "").strip()
    chain_note = ""
    if chain_target:
        try:
            await _resolve_supersedes(chain_target, aspect)
        except ToolInputError as exc:
            if supersedes:
                raise
            chain_note = f"\n（原本要取代的 {chain_target} 现在接不上：{exc}）"
            chain_target = ""

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

    promoted_marks: dict = {
        "dont_surface": True,
        "i_from_candidate": bucket_id,
        "i_dream_dates": list(dates),
    }
    if chain_target:
        promoted_marks["i_supersedes"] = chain_target
    try:
        await rt.bucket_mgr.update(new_id, **promoted_marks)
    except Exception as e:
        rt.logger.warning(f"I promoted bucket metadata write failed for {new_id}: {e}")

    # 旧条目从「被质疑」转成「已被取代」——一个字没删，只是不再是链尾。
    # 质疑标记不用清：它是按「质疑者是否还挂在候选区」动态算的，而这条候选
    # 下面就会被标成 promoted，于是自动失效。
    if chain_target:
        try:
            await rt.bucket_mgr.update(chain_target, i_superseded_by=new_id)
            chain_note = f"\n{chain_target} 从此不再是当前的自我认知，但原样留着。"
        except Exception as e:
            rt.logger.warning(f"I supersede link failed for {chain_target}: {e}")
            chain_note = f"\n⚠️ {chain_target} 的取代标记写入失败，它仍会被当成当前信念。"

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
        f"{chain_note}"
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


async def record_dream_offer(bucket_ids: list) -> int:
    """给这些候选各记一次「这天做过梦，而它在队列里」，按天去重。

    和 record_dream_pass 的区别是**这里不问它有没有被渲染出来**。

    只有见证数的话，「等了 13 天还是 0/3」是个没法解读的数字：既可能是这 13 天
    里根本没做几次梦（那不是 bug，是用法），也可能是梦做了十几场、它每场都在
    队列里却从来没排到（那是 bug）。两个数摆在一起，这个问题就有答案了。

    只记天数不记日期列表：一条候选可能挂几个月，存全量日期会让 metadata 无界
    增长，而回答上面那个问题只需要「几天」。
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
            rt.logger.warning(f"I dream offer lookup failed for {bucket_id}: {e}")
            continue
        if not bucket or not is_pending_candidate(bucket):
            continue
        meta = bucket.get("metadata") or {}
        if str(meta.get("i_dream_offered_last") or "")[:10] == today:
            continue
        try:
            offered = int(meta.get("i_dream_offered") or 0)
        except (TypeError, ValueError):
            offered = 0
        try:
            updated = await rt.bucket_mgr.update(
                bucket_id,
                i_dream_offered=offered + 1,
                i_dream_offered_last=today,
            )
            if not updated:
                rt.logger.warning(
                    "I dream offer write returned false for %s", bucket_id
                )
                continue
            recorded += 1
        except Exception as e:
            rt.logger.warning(f"I dream offer write failed for {bucket_id}: {e}")
    return recorded


def _stall_note(meta: dict, passes: int) -> str:
    """候选的滞留诊断：等了多久、经历过几场梦、被见证几次。

    「等了很久」本身不说明问题，「经历过 N 场梦却一次都没被见证」才说明问题。
    """
    offered = meta.get("i_dream_offered")
    try:
        offered = int(offered or 0)
    except (TypeError, ValueError):
        offered = 0

    waited = ""
    created = str(meta.get("created") or "")
    if created:
        try:
            # 走 parse_iso_datetime：它会把带时区的值归一成本地 naive，
            # 直接 fromisoformat 碰上带偏移的 created 会和 naive 的 now() 相减报错。
            days = (datetime.now() - parse_iso_datetime(created)).days
        except (TypeError, ValueError):
            days = None
        if days is not None and days >= 1:
            waited = f"已等 {days} 天"

    if offered and not passes:
        diag = f"经历 {offered} 场梦，一次都没排到"
    elif offered:
        diag = f"经历 {offered} 场梦"
    else:
        diag = "还没经历过 dream"

    return "、".join(part for part in (waited, diag) if part)


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

    # 分三层：当前信念 / 正在被自己质疑 / 已经被取代。
    # 后两层一条都不删，只是从「我现在认为」里挪出来——rule.md 第 1 条，
    # 记忆可以淡去，不能被抹去。
    buckets_by_id = {str(b.get("id") or ""): b for b in all_buckets}
    current, disputed, superseded = [], [], []
    for b in i_buckets:
        if superseded_by(b):
            superseded.append(b)
        elif disputing_candidates(b, buckets_by_id):
            disputed.append(b)
        else:
            current.append(b)
    # limit 管的是「我现在认为什么」；折叠的两段各自也要有上限，否则攒了几十条
    # 被取代的条目之后，当前信念会被历史淹掉——那正好是这次要修的毛病的镜像。
    i_buckets = current[:limit]
    disputed = disputed[:limit]
    superseded = superseded[:limit]

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

    if disputed:
        lines.append(f"\n=== 我正在改的主意（{len(disputed)} 条）===")
        lines.append(
            "这些条目现在不作为当前的自我认知读出去——你自己写下了质疑它们的"
            "候选。等那条候选沉淀下来，它们会正式转成「已被取代」；"
            "如果那条候选自己沉下去了，这些会自动回到上面。"
        )
        for b in disputed:
            meta = b.get("metadata", {})
            by = "、".join(disputing_candidates(b, buckets_by_id))
            text = (b.get("content") or "").strip().replace("\n", " ")[:80]
            lines.append(
                f"\n{_aspect_label_of(meta)}{b['id']}（被 {by} 质疑中）\n{text}"
            )

    if superseded:
        lines.append(f"\n=== 已经被取代的（{len(superseded)} 条）===")
        lines.append("原样留着，只是不再是我现在的看法。")
        for b in superseded:
            meta = b.get("metadata", {})
            text = (b.get("content") or "").strip().replace("\n", " ")[:80]
            lines.append(
                f"\n{_aspect_label_of(meta)}{b['id']} → 被 {superseded_by(b)} 取代\n{text}"
            )

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
            stall = _stall_note(meta, passes)
            payload = (
                f"{created} {aspect_label}{b['id']} "
                f"（{passes}/{I_PROMOTE_THRESHOLD} 次 dream"
                f"{f'，{stall}' if stall else ''}）\n{text}"
            )
            lines.append("\n" + payload)

    return "\n".join(lines)
