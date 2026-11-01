"""
========================================
tools/breath/__init__.py — breath 工具的总入口与分支调度
========================================

breath 是「我睁眼看看自己记得什么」。这个文件根据参数把请求路由到
五个分支文件之一：

- catalog.py：catalog=True → 目录模式（每桶一行元数据，0 LLM，最省 token）
- feel.py：domain="feel"（或 tags 含 feel/__feel__）→ 拉所有 feel 桶
- importance.py：importance_min >= 1 → 跳过语义，按 importance 拉前 20
- surface.py：query 为空 → 浮现模式（pinned + 加权采样未解决桶 + passive）
- search.py：有 query → 检索模式（关键词 + 向量双通道 + 随机漂浮）

关键行为：
- 入口 dispatch() 做参数 null-safe 兜底、token/result 上限归一化、
  tags/domain 解析，再交给具体分支函数
- 不在这里做实际取桶/调 LLM 的工作

不做什么（边界）：
- 不直接处理 embedding 调用，全部下放到检索分支
- 正文渲染统一走 _verbatim.py，不进入 dehydrator
- 不做权限校验，MCP 调用方默认是模型自身

对外暴露：dispatch(query, max_tokens, domain, valence, arousal, max_results,
                   importance_min, tags, catalog) → str
========================================
"""

from typing import Optional

from utils import parse_bool

from .. import _runtime as rt
from errors import ToolInputError
from .._common import (
    check_metadata_size,
    check_query_size,
)
from ._date_range import parse_created_range
from .catalog import surface_catalog
from .feel import surface_feels
from .importance import surface_by_importance
from .surface import surface_default, surface_plans
from .search import surface_search


async def _with_deletion_requests(body: str) -> str:
    store = getattr(rt, "deletion_requests", None)
    batch = await store.render_pending_batch() if store is not None else ""
    return f"{batch}\n\n{body}" if batch and body else (batch or body)


async def _with_them(body: str, query: str = "") -> str:
    """把 them 的认识追加在浮现结果**之后**。

    独立通道，不进融合打分：任何一条普通记忆的分数与名次都不因为 them 的存在
    而改变（rule.md 13.3）。them 关着时这里返回空串，输出与没有这个模块时
    逐字一致——这也是那条边界唯一可被检验的形式。

    只挂在浮现与检索两条路上。catalog / feel / plan / importance 是定向通道，
    问的是特定的东西，往里塞一段人物补注只是噪音。
    """
    service = getattr(rt, "them_service", None)
    if service is None:
        return body
    try:
        block = await service.surface(query=query)
    except Exception as exc:
        rt.logger.warning(f"them surface skipped / them 追加块跳过: {exc}")
        return body
    if not block:
        return body
    return f"{body}\n\n{block}" if body else block


async def dispatch(
    query: Optional[str] = "",
    max_tokens: Optional[int] = 0,
    domain: Optional[str] = "",
    valence: Optional[float] = -1,
    arousal: Optional[float] = -1,
    max_results: Optional[int] = 0,
    importance_min: Optional[int] = -1,
    tags: Optional[str] = "",
    catalog: Optional[bool] = False,
    date_from: Optional[str] = "",
    date_to: Optional[str] = "",
    quotes: Optional[bool] = False,
    mode: Optional[str] = "manual",
    with_ids: Optional[bool] = False,
) -> str:
    # --- Null-safe coercion ---
    query = "" if query is None else str(query)
    if max_tokens is None:
        max_tokens = 0
    domain = "" if domain is None else str(domain)
    if valence is None:
        valence = -1
    if arousal is None:
        arousal = -1
    if max_results is None:
        max_results = 0
    if importance_min is None:
        importance_min = -1
    tags = "" if tags is None else str(tags)
    if catalog is None:
        catalog = False
    date_from = "" if date_from is None else str(date_from)
    date_to = "" if date_to is None else str(date_to)
    quotes = parse_bool(quotes, default=False)
    mode = "manual" if mode is None else str(mode)
    with_ids = parse_bool(with_ids, default=False)

    # 抛而不是 return：return 出去在 MCP 侧是 isError=False，模型会以为
    # 「查过了，没结果」，然后据此得出「这件事没记过」——比报错糟得多。
    # hold / anchor / trace 对同类失败一直是抛的，这里过去不是。
    query_err = check_query_size(query)
    if query_err:
        raise ToolInputError(query_err)
    metadata_err = check_metadata_size(domain=domain, tags=tags)
    if metadata_err:
        raise ToolInputError(metadata_err)

    # 3.6.0：日期区间在这里统一解析并校验一次，五条分支拿到同一对边界。
    # 此前只有 search 分支接了 date_from/date_to，`breath_advanced(date_to=...)`
    # 不带 query 时参数被静默丢弃——收下了、schema 也认，就是没人用它。
    created_from, created_to = parse_created_range(date_from, date_to)

    if rt.mark_op:
        rt.mark_op("breath")
    rt.record_v3_tool_event("breath", {
        "query": query,
        "max_tokens": max_tokens,
        "domain": domain,
        "valence": valence,
        "arousal": arousal,
        "max_results": max_results,
        "importance_min": importance_min,
        "tags": tags,
        "catalog": catalog,
        "date_from": date_from,
        "date_to": date_to,
        "quotes": quotes,
    })
    await rt.decay_engine.ensure_started()

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    default_results = int(surfacing_cfg.get("breath_max_results") or 20)
    default_tokens = int(surfacing_cfg.get("breath_max_tokens") or 20000)
    if max_results <= 0:
        max_results = default_results
    if max_tokens <= 0:
        max_tokens = default_tokens
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 40000)
    tag_filter = [t.strip() for t in tags.split(",") if t.strip()]
    memory_max_tokens = max_tokens

    # --- catalog 目录模式：最先短路，0 LLM、只读元数据、每桶一行 ---
    # 开新窗省 token 的推荐姿势：先 breath(catalog=True) 看目录，
    # 再 breath(query=...) 精准拉取正文。
    if catalog:
        domain_filter = [d.strip() for d in domain.split(",") if d.strip()]
        return await _with_deletion_requests(await surface_catalog(
            domain_filter=domain_filter or None,
            tag_filter=tag_filter,
            max_results=max_results,
            created_from=created_from,
            created_to=created_to,
        ))

    # --- 解析 tags 过滤；feel/__feel__ 映射到 feel 通道 ---
    if any(t in ("feel", "__feel__") for t in tag_filter):
        domain = "feel"
        tag_filter = [t for t in tag_filter if t not in ("feel", "__feel__")]

    # --- Feel 通道：3.0.0 起必须带关键词，不再全量返回（见 feel.py） ---
    if domain.strip().lower() == "feel":
        return await _with_deletion_requests(
            await surface_feels(
                query=query,
                max_tokens=memory_max_tokens,
                created_from=created_from,
                created_to=created_to,
            )
        )

    # --- Plan 通道：与 feel 同构。plan 不参与普通浮现，没有这个分流时
    # domain="plan" 会落到下面的浮现模式，返回核心准则而不是 plan。 ---
    if domain.strip().lower() == "plan":
        return await _with_deletion_requests(await surface_plans(max_tokens=memory_max_tokens))

    # --- importance_min 模式：跳过语义，按 importance 降序 ---
    if importance_min >= 1:
        return await _with_deletion_requests(await surface_by_importance(
            importance_min=importance_min,
            max_tokens=memory_max_tokens,
            tag_filter=tag_filter,
            created_from=created_from,
            created_to=created_to,
        ))

    # --- 无 query：浮现模式 ---
    if not query or not query.strip():
        return await _with_deletion_requests(await _with_them(await surface_default(
            max_results=max_results,
            max_tokens=memory_max_tokens,
            tag_filter=tag_filter,
            created_from=created_from,
            created_to=created_to,
        )))

    # --- 有 query：检索模式 ---
    return await _with_deletion_requests(await _with_them(await surface_search(
        query=query,
        max_results=max_results,
        max_tokens=memory_max_tokens,
        domain=domain,
        valence=valence,
        arousal=arousal,
        tag_filter=tag_filter,
        date_from=date_from,
        date_to=date_to,
        with_quotes=quotes,
        created_from=created_from,
        created_to=created_to,
        mode=mode,
        with_ids=with_ids,
    ), query))
