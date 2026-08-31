"""trace 的引语订正分支（3.4.0）—— 改与删。

3.1.0 加了引语：写入的那一刻挑出「当时说出口就知道不想忘」的那几句，原样存进
frontmatter。但只给了写入口，没给修正口——记错了、写错字了、或者回头看觉得
这句根本不该留，就只能去改 Markdown。

**为什么走 trace 而不是新开一个工具**：和 3.3.0 的关系修正是同一个理由——
我看了一眼已有的东西，然后说它不对。trace 是 OB 唯一的「写元数据」入口，
引语也是元数据。工具数仍然是 16 个。

**为什么只能改和删，不能补录**（`_REJECT_GROWTH` / `_REJECT_EMPTY`）：

引语和已删除的原文层的区别，全部在「谁决定记住」这一条上——原文层是系统
自动存全量、事后随时可查；引语是**我在写入的那一刻**挑出来的几句。见
`ombrebrain/storage/quote_store.py` 的模块 docstring。

如果 trace 能往一个桶里加引语，这条区别当场就没了：任何一句话都可以在事后
被追认为「当时就知道重要」，引语退化成一个可以随时往里塞原文的口袋。所以：

- 桶里本来没有引语 → 拒绝。这个通道只能在写入那一刻开。
- 条数只能持平或减少 → 订正和删除是「回头看这几句」，补录不是。

这条边界与 `_relation_edit` 的「relink 不能凭空建立关系」同源：都只允许对
**已经存在的东西**说「它不对」，不允许凭空创造。

条数与长度的硬上限（3 条 / 每条 100 字，超限拒绝不截断）仍由
`BucketManager._sanitize_quotes` → `normalize_quotes` 统一把关，这里不重复。

拆成独立文件是因为 `trace/core.py` 已经逼近 800 行硬上限。
"""

from __future__ import annotations

from errors import ToolInputError
from ombrebrain.storage.attribution import names_from_config
from ombrebrain.storage.quote_store import quotes_from_metadata, render_quotes

from .. import _runtime as rt

_REJECT_EMPTY = (
    "这条记忆没有引语，trace 不能补录。"
    "引语是写入那一刻挑出来的原话——决定权只在那一刻，"
    "事后追认「当时就知道这句重要」就不是引语了，是摘要。"
    "写入时用 hold(content=..., quotes=[...])。本次未修改。"
)


def _reject_growth(before: int, after: int) -> None:
    raise ToolInputError(f"这条记忆现在有 {before} 条引语，quotes_replace 给了 {after} 条。"
        "trace 只能订正和删除已有的引语，不能加——多出来的那句不是"
        "「当时说出口就知道要记住」的，是现在才决定要记的。"
        "只想改其中几句，就把要保留的原样一起传回来。本次未修改。")


def validate(existing: list[dict[str, str]], incoming: list) -> None:
    """检查是否满足「只能改、只能删」。不合法一律抛 ToolInputError。

    为什么不再返回错误短句：见 _relation_edit.validate 的同款说明——
    返回串会被当成一次成功的调用。
    """
    if not existing:
        raise ToolInputError(_REJECT_EMPTY)
    if len(incoming) > len(existing):
        _reject_growth(len(existing), len(incoming))


async def apply(bucket_id: str, quotes_replace: list) -> str:
    """整体替换引语（空列表 = 全部删除），返回给模型看的中文短句。"""
    bucket = await rt.bucket_mgr.get(bucket_id)
    if not bucket:
        raise ToolInputError(f"找不到记忆 {bucket_id}；本次未修改。")

    # 已存在的用宽容读取——磁盘上的 frontmatter 可能被手工编辑坏，
    # 一条坏数据不该让整个订正入口失效。
    existing = quotes_from_metadata(bucket.get("metadata") or {})
    incoming = list(quotes_replace)

    validate(existing, incoming)

    try:
        # 结构、条数、长度的校验在 BucketManager 里统一做（超限拒绝不截断）。
        ok = await rt.bucket_mgr.update(bucket_id, event_actor="llm", quotes=incoming)
    except ValueError as exc:
        raise ToolInputError(f"引语未通过校验：{exc} 本次未修改。")

    if not ok:
        raise ToolInputError(f"找不到要修改的记忆文件 {bucket_id}；本次未修改。")

    if not incoming:
        rt.logger.info(f"op=trace_quotes action=clear {bucket_id} before={len(existing)}")
        return f"已删除 {bucket_id} 的全部 {len(existing)} 条引语。"

    # 回显读回磁盘上的结果，而不是回显入参：入参可能是裸字符串列表，落盘的是
    # 归一化并清洗过的结构。改引语这件事，看不到**实际存成了什么**就等于没确认。
    saved = await rt.bucket_mgr.get(bucket_id)
    stored = quotes_from_metadata((saved or {}).get("metadata") or {})
    rt.logger.info(
        f"op=trace_quotes action=replace {bucket_id} "
        f"before={len(existing)} after={len(stored)}"
    )
    removed = len(existing) - len(stored)
    suffix = f"（删掉了 {removed} 条）" if removed > 0 else ""
    rendered = render_quotes(stored, **names_from_config(getattr(rt, "config", None)))
    return f"已更新 {bucket_id} 的引语{suffix}：\n{rendered}"
