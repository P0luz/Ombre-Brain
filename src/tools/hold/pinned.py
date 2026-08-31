"""
========================================
tools/hold/pinned.py — hold(pinned=True) 分支
========================================

把这条桶钉为「永久核心准则」：跳过合并，强制 importance=10，写到
permanent 目录，不衰减、不会被合并掉。

关键行为：
- 先做 pinned 数量配额检查（默认 20 个上限），超了就拒绝并提示
- 仍然走 LLM analyze 拿 domain/valence/arousal/tags/suggested_name；
  她/他显式传入的 valence/arousal 优先
- type="permanent" + pinned=True 双重标记
- embedding 由 create() 尝试同步生成；不可用时仍保留逐字原文，稍后可 backfill

不做什么（边界）：
- 不做合并尝试：pinned 桶之间互不合并，分别保留
- 不允许 importance < 10：钉选意味着最高重要度

对外暴露：store_pinned(content, extra_tags, valence, arousal,
                       why_remembered, meaning, media) → str
========================================
"""

from utils import normalize_memory_title

from .. import _runtime as rt
from .._common import check_pinned_quota, _quota_turn


async def store_pinned(
    content: str,
    extra_tags: list,
    valence: float,
    arousal: float,
    why_remembered: str,
    title: str = "",
    meaning: str = "",
    media: list | None = None,
    explicit_domain: list[str] | None = None,
    source_refs: list[dict] | None = None,
    quotes: list[dict] | None = None,
) -> str:
    try:
        analysis = await rt.dehydrator.analyze(content)
    except Exception as e:
        rt.logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    analyzed_domain = analysis.get("domain") or ["未分类"]
    if not isinstance(analyzed_domain, list):
        analyzed_domain = ["未分类"]
    final_domain = explicit_domain or analyzed_domain
    _v = analysis.get("valence", 0.5)
    _a = analysis.get("arousal", 0.3)
    final_valence = valence if 0 <= valence <= 1 else (float(_v) if _v is not None else 0.5)
    final_arousal = arousal if 0 <= arousal <= 1 else (float(_a) if _a is not None else 0.3)
    _raw_tags = analysis.get("tags") or []
    model_tags = _raw_tags if isinstance(_raw_tags, list) else []
    all_tags = list(dict.fromkeys(extra_tags if extra_tags else model_tags))
    suggested_name = analysis.get("suggested_name", "")
    final_title = title or normalize_memory_title(suggested_name)

    # 配额判定 + 落盘必须在同一把锁里：两个并发 hold(pinned=True) 都可能在
    # 对方提交前读到同一个「未满」快照，检查和创建隔着一次 await 就会互相看不见。
    async with _quota_turn("pinned"):
        err = await check_pinned_quota()
        if err:
            return err

        bucket_id = await rt.bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=final_domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            title=final_title,
            source_refs=source_refs,
            quotes=quotes,
            bucket_type="permanent",
            pinned=True,
            why_remembered=why_remembered,
            source_tool="hold",
            event_actor="llm",
            allow_embedding_fallback=True,
            meaning=meaning,
            media=media,
            defer_derived_index=True,
        )
    post_index = getattr(rt.bucket_mgr, "_index_after_update", None)
    if callable(post_index):
        await post_index(
            bucket_id,
            content_changed=True,
            meaning_changed=bool(meaning),
        )
    return f"📌钉选→{bucket_id} {','.join(str(d) for d in final_domain if d is not None)}"
