"""
========================================
tools/recall/core.py — recall（回想）实现
========================================

recall 是「沿记忆的线往回走」的入口：给一条记忆的 bucket_id，
返回它的正文 + 一个「路口」——按方向分组的邻居（← 之前 / → 之后 /
≈ 同刻 / ↔ 相关），每条邻居带标题、日期、id，让我能一层层点过去，
走到「过去的过去」。

关键行为：
- 读的是后端已经自动建好的 relation_links（3.2.0 起），只读不改。
- 路口用 ombrebrain.storage.relation_store.render_junction 渲染，与
  breath/catalog/dream 共用同一套分组与标签，不各写各的。
- 因果边（caused_by/causes）不自动建，但存量若有，读侧照常显示。
- 正文逐字返回，不摘要不截断——路口放在正文前，方便先看方向再读内容。

不做什么（边界）：
- 不建边、不改边、不写盘——recall 是纯读取。
- 不碰写入/遗忘/原文/删除。
- plan/feel/letter/i 等桶不参与关系网，和 render_junction 的排除一致。

对外暴露：recall(bucket_id) -> str
========================================
"""

from .. import _runtime as rt
from .._common import check_metadata_size
from ombrebrain.storage.relation_store import (
    EXCLUDED_RELATION_TYPES,
    bucket_date,
    bucket_title,
    bucket_type,
    render_junction,
)
from utils import strip_wikilinks


async def recall(bucket_id: str) -> str:
    bucket_id = "" if bucket_id is None else str(bucket_id).strip()
    metadata_err = check_metadata_size(bucket_id=bucket_id)
    if metadata_err:
        return metadata_err
    if not bucket_id:
        return "给我一条记忆的 bucket_id，我才能顺着它往回走。"
    if rt.mark_op:
        rt.mark_op("recall")

    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        return f"我找不到 {bucket_id} 这条记忆——它可能还没被写下，或已经归档。"
    meta = bucket.get("metadata") or {}
    btype = bucket_type(meta)
    if btype in EXCLUDED_RELATION_TYPES and btype != "feel":
        return (
            f"{bucket_id} 是 {btype} 类桶，不参与记忆的关系网——"
            f"它们各自成层，横向连起来只是噪音。"
        )

    title = bucket_title(bucket)
    date = bucket_date(meta)
    header = f"[回想] {title}"
    if date:
        header += f" 〔{date}〕"
    header += f" [bucket_id:{bucket_id}]"

    junction = await render_junction(bucket, rt.bucket_mgr.get)
    if not junction:
        junction = "这条记忆目前还没有连接——它是一颗还没被串起来的珍珠。"

    body = strip_wikilinks(str(bucket.get("content") or "")).strip()
    return f"{header}\n\n路口：\n{junction}\n\n—— 这条记忆的正文 ——\n{body}"
