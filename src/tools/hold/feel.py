"""
========================================
tools/hold/feel.py — hold(feel=True) 分支
========================================

把模型自己的第一人称感受作为一条 feel 桶存下来。feel 桶是独立类型，
不参与普通 breath 浮现，只能通过 breath(domain="feel") 或 dream
末尾的 feel 段落读到。

关键行为：
- 写入时打上 __feel__ 系统标签 + domain=["feel"] + type="feel"
- valence/arousal 不传则取「我此刻的情绪」默认值（V0.5/A0.3）
- iter 2.0：bucket_id 用人类可读命名 ``feel_YYYYMMDDHHMM_V<valence*100>``
  （分钟精度 + valence 后缀），冲突时由 bucket_manager.create() 自动追加秒后缀
- iter 2.0：source_tool="hold"（feel 在 hold 工具的 feel=True 分支里）
- 如果带了 source_bucket，把源记忆标为 digested 并存入「我视角的 valence」
- embedding 由 create() 尝试同步生成；不可用时仍保留逐字原文，稍后可 backfill

不做什么（边界）：
- 不做合并：feel 是「同一件事的不同视角」，不该合
- 不做 importance 校准：feel 一律 importance=5

对外暴露：store_feel(content, extra_tags, valence, arousal, source_bucket,
                     why_remembered, meaning, media) → str
========================================
"""

from datetime import datetime

from .. import _runtime as rt
from ombrebrain.storage.relation_store import MAX_RELATION_LINKS, normalize_relation_links


def _build_feel_id(valence: float) -> str:
    """构造 feel 桶的可读 id：``feel_YYYYMMDDHHMM_V085``。

    valence ∈ [0,1]，取两位整数（×100，四舍五入），保证字典序稳定可读。
    冲突回避交给 bucket_manager.create() 的 bucket_id_override 机制。
    """
    ts = datetime.now().strftime("%Y%m%d%H%M")
    v_int = max(0, min(100, round(float(valence) * 100)))
    return f"feel_{ts}_V{v_int:03d}"


async def _link_source_reverse(source_id: str, feel_id: str) -> None:
    """给源事件补 referenced_by 反边（源事件 → feel），即时、幂等、只记日志不抛错。

    让「回忆事件时顺出当时的感受」当场成立，不等 dream 兜底。feel 与源事件的
    关系是定向的：feel --references--> 源事件，源事件 --referenced_by--> feel。
    """
    def _mutation(post):
        try:
            links = normalize_relation_links(post.metadata.get("relation_links"))
        except ValueError:
            return False, 0
        if any(
            l.get("type") == "referenced_by"
            and l.get("target_bucket_id") == feel_id
            for l in links
        ):
            return False, 0
        if len(links) >= MAX_RELATION_LINKS:
            return False, 0
        links.append(
            {
                "target_bucket_id": feel_id,
                "type": "referenced_by",
                "label": "",
                "status": "active",
            }
        )
        try:
            post["relation_links"] = normalize_relation_links(links)
        except ValueError:
            return False, 0
        return True, 1

    try:
        await rt.bucket_mgr.mutate_relation_links(source_id, _mutation)
    except Exception as exc:  # noqa: BLE001 - 反边只是 hint，不该影响 feel 写入
        rt.logger.warning(
            f"feel source reverse link failed / 感受源事件反边失败 "
            f"{source_id}<-{feel_id}: {type(exc).__name__}: {exc}"
        )


async def store_feel(
    content: str,
    extra_tags: list,
    valence: float,
    arousal: float,
    source_bucket: str,
    why_remembered: str,
    title: str = "",
    meaning: str = "",
    media: list | None = None,
    source_refs: list[dict] | None = None,
    quotes: list[dict] | None = None,
    event_time: str = "",
    references: list[str] | None = None,
) -> str:
    feel_valence = valence if 0 <= valence <= 1 else 0.5
    feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
    feel_tags = list(dict.fromkeys(["__feel__"] + extra_tags))
    # source_bucket 落成 references 边（feel → 源事件）：感受牵着它来自的事件。
    refs = list(references or [])
    if source_bucket and source_bucket.strip():
        refs.append(source_bucket.strip())
    bucket_id = await rt.bucket_mgr.create(
        content=content,
        tags=feel_tags,
        importance=5,
        domain=["feel"],
        valence=feel_valence,
        arousal=feel_arousal,
        name=None,
        title=title,
        source_refs=source_refs,
        quotes=quotes,
        event_time=event_time,
        references=refs or None,
        bucket_type="feel",
        why_remembered=why_remembered,
        triggered_by=source_bucket.strip() if source_bucket else "",
        source_tool="hold",
        event_actor="llm",
        bucket_id_override=_build_feel_id(feel_valence),
        allow_embedding_fallback=True,
        meaning=meaning,
        media=media,
    )
    if source_bucket and source_bucket.strip():
        try:
            update_kwargs: dict[str, bool | float] = {"digested": True}
            if 0 <= valence <= 1:
                update_kwargs["model_valence"] = feel_valence
            await rt.bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            # 即时补反向边：源事件 → feel 的 referenced_by，让「回忆事件顺出感受」当场成立
            await _link_source_reverse(source_bucket.strip(), bucket_id)
        except Exception as e:
            rt.logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
    return f"🫧feel→{bucket_id}"
