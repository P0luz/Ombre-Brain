"""Them：模型自己写下、也只由模型读回的、关于其他人的长期认识。

这里**不调用任何 LLM**，理由同 You：我对一个人的认识不该经别人之口总结。
什么时候算数由两道结构性的闸决定——三个不同自然日的重申，
以及至少两个真实记忆桶的显式关系。

多守一条 you 没有的：只记这个人本身，不描述任何关系（rule.md 13.3）。
"""

from typing import Optional

from .. import _runtime as rt


async def dispatch(
    query: Optional[str] = "",
    content: Optional[str] = "",
    names: Optional[list] = None,
    person_id: Optional[str] = "",
    bucket_ids: Optional[list] = None,
    aspect: Optional[str] = "",
    concept_key: Optional[str] = "",
    concept_value: Optional[str] = "",
    basis: Optional[str] = "observed_pattern",
    known_via: Optional[str] = "",
    delete_id: Optional[str] = "",
    max_results: Optional[int] = 12,
) -> str:
    """读回（默认）、写入/重申（给 content + names），或撤回（给 delete_id）。"""

    if rt.mark_op:
        rt.mark_op("Them")

    removing = str(delete_id or "").strip()
    if removing:
        return await rt.them_service.delete(removing)

    writing = str(content or "").strip()
    if writing:
        _, message = await rt.them_service.write(
            content=writing,
            bucket_ids=list(bucket_ids or []),
            aspect="" if aspect is None else str(aspect),
            concept_key="" if concept_key is None else str(concept_key),
            concept_value="" if concept_value is None else str(concept_value),
            names=[str(item) for item in (names or [])],
            person_id="" if person_id is None else str(person_id),
            basis="observed_pattern" if basis is None else str(basis),
            known_via="" if known_via is None else str(known_via),
        )
        return message

    return await rt.them_service.recall(
        query="" if query is None else str(query),
        max_results=12 if max_results is None else max_results,
    )
