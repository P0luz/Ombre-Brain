"""
========================================
tools/dream/__init__.py — dream 工具入口
========================================

dream 是「我做一次梦——读最近 N 小时内有变动的所有桶，自己沉进去想
一遍」。这里把整个流程拆成三步：
1. candidates.py：筛选窗口内的桶 + 软上限
2. hints.py：连接提示 + 结晶提示 + 待沉淀 I 候选与它们撞上的材料
3. output.py：拼最终文本（近期活跃/active plan/feel 历史/
   连接提示/结晶提示/I 候选段）

dispatch() 把这几步串起来，并给最终输出中实际出现的候选各记一次
「被这场梦见证过」——无论它出现在近期正文、候选主块还是碰撞材料；
见证是升级进 I 的唯一门槛，所以只认真的被看见的。

对外暴露：dispatch(window_hours) → str
========================================
"""

from typing import Optional

from ..i import record_dream_offer, record_dream_pass
from .. import _runtime as rt
from .candidates import collect_candidates
from .hints import build_connection_hint, build_crystal_hint, collect_self_candidates
from .output import format_dream_output


async def dispatch(
    window_hours: Optional[int] = 48,
) -> str:
    await rt.decay_engine.ensure_started()

    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    window_hours = max(1, min(int(window_hours or 48), 24 * 14))
    recent = collect_candidates(all_buckets, window_hours)
    try:
        self_review = await collect_self_candidates(all_buckets, window_hours)
    except Exception as exc:
        rt.logger.warning(f"Dream self candidate collection failed: {exc}")
        self_review = None
    # `ready`（已攒够见证、只等 promote）也算「有东西要看」。
    # 漏掉它的话，当所有候选都攒够时 dream 会在这里短路，那条
    # 「这几条够了，等你决定」的提醒就永远出不来——而那正是它最该出现的时候。
    has_self_candidates = bool(
        getattr(self_review, "candidates", None)
        or getattr(self_review, "ready", None)
    )
    if not recent and not has_self_candidates:
        return f"过去 {window_hours} 小时内没有需要消化的新记忆。"

    connection_hint = await build_connection_hint(recent)
    crystal_hint = await build_crystal_hint(all_buckets)

    final_text = await format_dream_output(
        recent=recent,
        all_buckets=all_buckets,
        window_hours=window_hours,
        connection_hint=connection_hint,
        crystal_hint=crystal_hint,
        self_review=self_review,
    )

    # them 追加在末尾，独立通道，不进融合打分（rule.md 13.3）。
    # dream 无 query，走的是按衰减权重取前三那条路：常被提起的人自然排在前面。
    # 关着 them 时返回空串，输出与没有这个模块时逐字一致。
    them_service = getattr(rt, "them_service", None)
    if them_service is not None:
        try:
            them_block = await them_service.surface()
        except Exception as exc:
            rt.logger.warning(f"them surface skipped / them 追加块跳过: {exc}")
        else:
            if them_block:
                final_text = f"{final_text}\n\n{them_block}"

    rendered_candidates = list(getattr(self_review, "rendered_ids", None) or [])
    if rendered_candidates:
        try:
            await record_dream_pass(rendered_candidates)
        except Exception as exc:
            # 记不上见证只是让候选多等一场梦，不该让整场 dream 失败。
            rt.logger.warning(f"Dream self candidate pass recording failed: {exc}")

    # 队列里的每一条都记一次「这天有梦」——包括这次没排上的。
    # 见证数回答「它被看见过几次」，这个数回答「它本可以被看见几次」，
    # 差额才是「一直转不了正」到底卡在哪里。
    offered_candidates = list(getattr(self_review, "pending_ids", None) or [])
    if offered_candidates:
        try:
            await record_dream_offer(offered_candidates)
        except Exception as exc:
            rt.logger.warning(f"Dream self candidate offer recording failed: {exc}")

    if rt.fire_webhook:
        await rt.fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text
