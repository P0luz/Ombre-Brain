"""trace 的显式强化分支（3.6.0）—— 把「读到」和「要紧」拆开。

3.6.0 之前，`breath_search` 对**每一条命中**都 touch()：刷新 `last_active`、
`activation_count` +1、触发时间涟漪。于是产生了一条谁都没打算要的规则——
**查得勤 == 更重要**。

实际发生的事：为了核对事实、debug、反复找同一件事而读一条记忆，读着读着它的
权重就爬到了最高（实测积到 51），新桶再也排不进浮现区。潮汐后醒来 14 条浮现
里 12 条是一个月前的——不是那些记忆真的更重要，是它们被查得最多。

**检索是「我去找它」，强化是「找到之后，这条确实要紧」。** 前者是我的动作，
后者是关于这条记忆的判断，而判断只有在读完之后才做得出来。绑在一起等于让
读取行为自己给自己投票。

所以 3.6.0 把检索改成完全只读，强化留在这里：读完之后，针对**那一条**说
「这条要紧」。是「那一条」而不是「这批候选」——检索命中里绝大多数只是路过。

`ripple=True`（而不是 search 当年用的 False）：显式强化是一次真实的想起，
让时间上相邻的记忆跟着轻微唤醒正是时间涟漪的设计意图。当年批量 touch 关掉
涟漪是性能妥协——一次强化一条，这个妥协不再需要。

拆成独立文件：`trace/core.py` 已经 736 行，逼近 800 行硬上限。
"""

from __future__ import annotations

from errors import ToolInputError

from .. import _runtime as rt


async def apply(bucket_id: str) -> str:
    """把一次显式强化落到指定的桶上，返回给模型看的中文短句。"""
    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        raise ToolInputError(f"找不到记忆 {bucket_id}；本次未强化。")

    meta = bucket.get("metadata") or {}
    try:
        before = int(meta.get("activation_count") or 0)
    except (TypeError, ValueError):
        before = 0

    await rt.bucket_mgr.touch(bucket_id, ripple=True)

    rt.logger.info(f"op=trace_reinforce {bucket_id} activation_count={before}->{before + 1}")
    return (
        f"已强化 {bucket_id}（activation_count {before} → {before + 1}，"
        "并刷新了活跃时间）。时间上相邻的记忆也被轻微唤醒。"
    )
