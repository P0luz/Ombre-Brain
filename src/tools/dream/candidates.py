"""
========================================
tools/dream/candidates.py — 候选桶筛选 + 软上限
========================================

dream 的第一步：从全量桶里筛出「过去 window_hours 内有变动的表层
动态桶」，超过 40 个时按 calculate_score 截断，避免一次涌进来太多
炸上下文。

关键行为：
- 排除 permanent / feel / plan / letter / pinned；protected 仅防衰减，
  既不进入 dream core，也不进入近期候选
- 排除 resolved：已经放下的事不该在近期活跃段里再次浮出来
- 排除 digested / dont_surface / anchor，避免已消化或主动隐藏的桶再次进入梦境
- 任一 last_active 或 created 在窗口内即纳入
- 默认按 last_active 倒序，让最新的修改排前面
- 软上限 40，超了就改按 decay_engine 权重排序后截断

不做什么（边界）：
- 不做 dehydrate；返回原桶 dict 由 output.py 渲染
- 不调 LLM

对外暴露：collect_candidates(all_buckets, window_hours) → list[dict]
         recent_window_cutoff(window_hours) / is_within_window(meta, cutoff)
         —— 供 hints.py 的 I 候选选取复用同一套窗口规则
========================================
"""

from datetime import datetime, timedelta

from ombrebrain.policy.surfacing import SurfacePolicyVM
from .. import _runtime as rt
from ..plan.core import is_letter_bucket
from utils import parse_bool, parse_iso_datetime

DREAM_MAX_CANDIDATES = 40
_SURFACE_POLICY = SurfacePolicyVM.default()


def _can_dream(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="dream").allowed


def _metadata_timestamp(meta: dict) -> float:
    value = meta.get("last_active") or meta.get("created", "")
    try:
        return parse_iso_datetime(value).timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


def recent_window_cutoff(window_hours: int) -> datetime:
    """近期活跃窗口的起点；I 候选选取（hints.py）复用同一个 cutoff。"""
    return datetime.now() - timedelta(hours=window_hours)


def is_within_window(meta: dict, cutoff: datetime) -> bool:
    """任一 last_active 或 created 落在窗口内即算「近期活跃」。"""
    for key in ("last_active", "created"):
        ts = meta.get(key, "")
        if not ts:
            continue
        try:
            if parse_iso_datetime(ts) >= cutoff:
                return True
        except (ValueError, TypeError):
            continue
    return False


def collect_core_context(all_buckets: list) -> list:
    """Compatibility selector for callers that still inspect dream core candidates.

    Dream dispatch intentionally no longer consumes or renders this list.  Keep
    the selector available for policy/regression callers without reintroducing
    pinned/permanent content into dream output.
    """
    core = [
        b for b in all_buckets
        if (
            b["metadata"].get("pinned", False)
            or b["metadata"].get("type") == "permanent"
        )
        and not parse_bool(b["metadata"].get("protected"), default=False)
        and _can_dream(b)
        and not is_letter_bucket(b)
        and b["metadata"].get("type") not in ("letter", "self", "i")
    ]
    core.sort(
        key=lambda b: (
            int(b["metadata"].get("importance") or 0),
            _metadata_timestamp(b["metadata"]),
            b.get("id", ""),
        ),
        reverse=True,
    )
    return core[:20]


def collect_candidates(all_buckets: list, window_hours: int) -> list:
    candidates = [
        b for b in all_buckets
        if not is_letter_bucket(b)
        and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and _can_dream(b)
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("resolved", False)
        and not parse_bool(b["metadata"].get("protected"), default=False)
    ]
    cutoff = recent_window_cutoff(window_hours)

    recent = [b for b in candidates if is_within_window(b["metadata"], cutoff)]
    recent.sort(key=lambda b: _metadata_timestamp(b["metadata"]), reverse=True)
    if len(recent) > DREAM_MAX_CANDIDATES:
        recent.sort(
            key=lambda b: rt.decay_engine.calculate_score(b["metadata"]),
            reverse=True,
        )
        recent = recent[:DREAM_MAX_CANDIDATES]
    return recent
