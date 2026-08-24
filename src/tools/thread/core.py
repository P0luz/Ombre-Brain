"""
========================================
tools/thread/core.py — thread（串珠）实现
========================================

thread 是「按话题串起散落的珍珠」的动作：给一个关键词，把相关的记忆
按时间排成一条线。这是现查现排、无状态的——话题线不预先「建」，什么时候
想串，什么时候现场串给你看。

关键行为：
- 用 bucket_mgr.search 检索相关桶，再按创建时间升序排成一条线。
- 每站一行：序号 + 日期 + 标题/首句 + id。0 LLM 调用，不摘要不截断正文。
- 想读某站全文，用它的 id 去 recall / breath_search 点开——thread 只给线，不给正文。
- 特殊类型桶（feel/plan/letter/i）不参与，和 recall / relation_hint 的排除一致。

不做什么（边界）：
- 不调 LLM 打摘要——「每站一句话」用标题/正文首行，不是模型改写。
- 不写盘、不建结构——话题线是检索 + 排序的产物，不是存起来的东西。
- 不碰写入/遗忘/原文/删除。

对外暴露：thread(query, max_results) -> str
========================================
"""

from datetime import datetime

from .. import _runtime as rt
from .._common import check_metadata_size, check_query_size
from ombrebrain.storage.relation_store import (
    EXCLUDED_RELATION_TYPES,
    bucket_date,
    bucket_title,
)

_DEFAULT_MAX = 20
_MAX = 50


def _is_archived(meta: dict) -> bool:
    return bool((meta or {}).get("deleted_at") or (meta or {}).get("tombstone"))


def _parse_created(meta: dict):
    """优先 event_time（事情发生时间），回退 created（记下时间）。"""
    raw = str((meta or {}).get("event_time") or "").strip()
    if not raw:
        raw = str((meta or {}).get("created") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # now_iso() 存的是本地 naive 时间，导入数据可能带时区；统一成 naive 再排序，
    # 否则 aware/naive 混排会在比较时抛 TypeError。
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


async def thread(query: str, max_results: int = 0) -> str:
    query = "" if query is None else str(query).strip()
    query_err = check_query_size(query)
    if query_err:
        return query_err
    if not query:
        return "给我一个关键词，我才能把相关的记忆串成一条线。"
    metadata_err = check_metadata_size(query=query)
    if metadata_err:
        return metadata_err
    if rt.mark_op:
        rt.mark_op("thread")

    try:
        max_results = int(max_results or 0)
    except (TypeError, ValueError):
        max_results = 0
    if max_results <= 0:
        max_results = _DEFAULT_MAX
    max_results = min(max_results, _MAX)

    try:
        matches = await rt.bucket_mgr.search(query, limit=max(max_results, 30))
    except Exception as exc:
        rt.logger.error(f"thread search failed / 串珠检索失败: {type(exc).__name__}: {exc}")
        return "检索过程出错，请稍后重试。"

    stations = []
    for bucket in matches:
        meta = bucket.get("metadata") or {}
        btype = str(meta.get("type") or "dynamic").strip().lower()
        if btype in EXCLUDED_RELATION_TYPES or _is_archived(meta):
            continue
        stations.append(bucket)
    if len(stations) > max_results:
        stations = stations[:max_results]

    if not stations:
        return f"我没有找到和「{query}」串得起来的记忆——它可能还没被记下，或者我还说不上这条线。"

    # 按事件时间升序：最早发生 → 现在。event_time 缺失时回退 created；
    # 两者都缺的放最后（不猜时间）。
    def _sort_key(bucket):
        dt = _parse_created(bucket.get("metadata") or {})
        return (dt is None, dt or datetime.max.replace(tzinfo=None))

    stations.sort(key=_sort_key)

    lines = [f"🧵 话题「{query}」串珠，共 {len(stations)} 站（按时间）：", ""]
    for i, bucket in enumerate(stations, 1):
        meta = bucket.get("metadata") or {}
        date = bucket_date(meta)
        title = bucket_title(bucket)
        head = f"{i}. " + (f"{date}  " if date else "")
        head += f"{title}  [{bucket.get('id')}]"
        lines.append(head)

    lines.append("")
    lines.append("想看某一站的全文，用它的 id 去 recall 或 breath_search 点开。")
    return "\n".join(lines)
