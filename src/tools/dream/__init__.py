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
import asyncio

from ..i import record_dream_pass
from .. import _runtime as rt
from .._relation_link import backfill_reference_reverse_links, backfill_feel_links
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
    # references 反向边补齐（幂等，fire-and-forget）：dream 是全量扫的时机，顺手把
    # references 缺的 referenced_by 反边补上，让引用线从两头都能拎起来。只补强关系，
    # 不参与 dream 输出，任何异常只记日志。
    try:
        asyncio.create_task(backfill_reference_reverse_links(all_buckets))
    except Exception as exc:
        rt.logger.warning(f"Dream reference reverse backfill schedule failed: {exc}")
    # feel 回填（幂等，fire-and-forget）：老 feel 的 triggered_by 有值但 references
    # 边没有（旧版不落关系边）。挂着自愈——正常情况白扫一遍、几乎无感，若将来某条
    # feel 又缺了边，做梦时静默补上，不用人记得手动跑一次。
    try:
        asyncio.create_task(backfill_feel_links(all_buckets))
    except Exception as exc:
        rt.logger.warning(f"Dream feel link backfill schedule failed: {exc}")
    recent = collect_candidates(all_buckets, window_hours)
    try:
        self_review = await collect_self_candidates(all_buckets, window_hours)
    except Exception as exc:
        rt.logger.warning(f"Dream self candidate collection failed: {exc}")
        self_review = None
    has_self_candidates = bool(getattr(self_review, "candidates", None))
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

    rendered_candidates = list(getattr(self_review, "rendered_ids", None) or [])
    if rendered_candidates:
        try:
            await record_dream_pass(rendered_candidates)
        except Exception as exc:
            # 记不上见证只是让候选多等一场梦，不该让整场 dream 失败。
            rt.logger.warning(f"Dream self candidate pass recording failed: {exc}")

    if rt.fire_webhook:
        await rt.fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text
