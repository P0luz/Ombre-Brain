"""You：模型自己写下、也只由模型读回的长期认识。

这里**不调用任何 LLM**。写什么由模型决定，什么时候算数由两道结构性的闸决定：
三个不同自然日的重申，以及至少两个真实记忆桶的显式关系。

不要在这条路径上加"自动抽取""自动复核""自动摘要"——那正是 3.4.x 拿掉的东西。
"""

from typing import Optional

from .. import _runtime as rt


async def dispatch(
    query: Optional[str] = "",
    aspect: Optional[str] = "",
    content: Optional[str] = "",
    bucket_ids: Optional[list] = None,
    concept_key: Optional[str] = "",
    concept_value: Optional[str] = "",
    basis: Optional[str] = "observed_pattern",
    explicit: Optional[bool] = False,
    long_term: Optional[bool] = False,
    delete_id: Optional[str] = "",
    with_ids: Optional[bool] = False,
    max_results: Optional[int] = None,
) -> str:
    """读回（默认）、写入/重申（给 content）、或撤回（给 delete_id）。"""

    if rt.mark_op:
        rt.mark_op("You")

    removing = str(delete_id or "").strip()
    if removing:
        return await rt.you_service.delete(removing)

    writing = str(content or "").strip()
    if writing:
        _, message = await rt.you_service.write(
            content=writing,
            bucket_ids=list(bucket_ids or []),
            aspect="" if aspect is None else str(aspect),
            concept_key="" if concept_key is None else str(concept_key),
            concept_value="" if concept_value is None else str(concept_value),
            basis="observed_pattern" if basis is None else str(basis),
            explicit=bool(explicit),
            long_term=bool(long_term),
        )
        return message

    # 条数和 token 预算由用户在面板上定；模型传的 max_results 只能往小里收（#124）。
    surfacing = (getattr(rt, "config", None) or {}).get("surfacing") or {}
    results_cap = _config_int(surfacing.get("you_max_results"), 6)
    token_budget = _config_int(surfacing.get("you_max_tokens"), 0)
    requested = results_cap if max_results is None else _config_int(max_results, results_cap)
    return await rt.you_service.recall(
        query="" if query is None else str(query),
        aspect="" if aspect is None else str(aspect),
        max_results=max(1, min(results_cap, requested)),
        max_tokens=token_budget,
        with_ids=bool(with_ids),
    )


def _config_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default
