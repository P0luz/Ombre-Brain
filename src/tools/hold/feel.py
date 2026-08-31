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


def _build_feel_id(valence: float) -> str:
    """构造 feel 桶的可读 id：``feel_YYYYMMDDHHMM_V085``。

    valence ∈ [0,1]，取两位整数（×100，四舍五入），保证字典序稳定可读。
    冲突回避交给 bucket_manager.create() 的 bucket_id_override 机制。
    """
    ts = datetime.now().strftime("%Y%m%d%H%M")
    v_int = max(0, min(100, round(float(valence) * 100)))
    return f"feel_{ts}_V{v_int:03d}"


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
) -> str:
    feel_valence = valence if 0 <= valence <= 1 else 0.5
    feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
    feel_tags = list(dict.fromkeys(["__feel__"] + extra_tags))
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
        except Exception as e:
            rt.logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
    return f"🫧feel→{bucket_id}"
