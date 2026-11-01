"""
========================================
tools/dream/hints.py — dream 的连接提示、结晶提示与 I 候选碰撞材料
========================================

三样东西，都是帮模型「看见」它自己没注意到的关联，都不下结论：

- 连接提示：在 recent 桶里找余弦相似度最高的一对（>0.5）→ 提示
  「这两个似乎有关联，不替你下结论，你自己想」
- 结晶提示：低频触发——扫所有 feel，只有凑够 5 条互相相似（>0.7）的
  feel 聚成一簇才提示「你已经写过 N 条相似的 feel，可以考虑
  hold(pinned=True) 升级它」，避免每次 dream 都刷同一条提示
- I 候选碰撞材料：把每条待沉淀的「我觉得……」和语义上最挨着的几条记忆
  摆在一起——支持它的、反驳它的、跟它撞车的其它候选，都只是材料

关键行为：
- 都依赖 embedding_engine.enabled；未启用时返回空串 / 空材料并明说
- protected 只防衰减：连接、结晶、I 候选及碰撞材料一律不读取其正文
- I 候选不受普通近期窗口限制；否则候选一旦老于 window_hours，就再也无法
  获得三次跨日见证。仍排除 pinned / resolved / protected
- 任意异常都吞掉，只 warning，不影响 dream 主流程
- 只读已落盘向量，不发新的 embedding 请求

不做什么（边界）：
- 不写桶，不修改任何状态（候选的见证计数由 tools/i 在 dream 渲染后写）
- 不判断谁对谁错、谁跟谁矛盾——那是认知层，rule.md 第 5 条
- 不替模型决定，只给「不替你下结论」的提示

对外暴露：build_connection_hint(recent) / build_crystal_hint(all_buckets)
         / collect_self_candidates(all_buckets, window_hours)
========================================
"""

from dataclasses import dataclass, field

from ..i import I_PROMOTE_THRESHOLD, dream_dates, is_pending_candidate
from .. import _runtime as rt
from ..plan.core import is_letter_bucket
from utils import parse_bool, strip_wikilinks

# 结晶提示低频触发：不是随便 2 条相似就提示，要凑够一簇 5 条（自己 + 4 条
# 相似 feel）才值得打断一次；避免同一批 feel 每场梦都刷同样的提示。
_CRYSTAL_CLUSTER_MIN = 5

# 每场梦最多给几条候选完整的碰撞材料。
#
# 不设上限时，一次梦要为所有未 promote 的候选各取一轮 embedding 并渲染整块；
# 候选只增不减的话，这一段会无限膨胀，而预算是先到先得的——最后谁都拿不到。
# 有了上限 + 「缺得最多的先来」的排序，名额才会在候选之间轮转。
_MAX_SELF_CANDIDATES_PER_DREAM = 5

# 对照池上限：正式 I 条目和其它候选永远全量参与碰撞，普通记忆只取最近这么多条。
# get_embedding 每条一次 sqlite 查询，全库几千条会让 dream 明显变慢，而更老的
# 记忆本来也很难说是「此刻还在场」的对照。
_COLLISION_POOL_RECENT = 200
# 每条候选最多摆几条材料，以及低于多少相似度就不值得摆出来。
_COLLISION_TOP_K = 3
_COLLISION_MIN_SIM = 0.35


async def build_connection_hint(recent: list) -> str:
    # 调用方通常已过滤 protected；这里再次收口，避免未来复用时把受保护正文带入提示。
    recent = [
        b for b in recent
        if not parse_bool((b.get("metadata") or {}).get("protected"), default=False)
    ]
    if not (rt.embedding_engine and rt.embedding_engine.enabled and len(recent) >= 2):
        return ""
    try:
        best_pair = None
        best_sim = 0.0
        ids = [b["id"] for b in recent]
        names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
        embeddings: dict = {}
        for bid in ids:
            emb = await rt.embedding_engine.get_embedding(bid)
            if emb is not None:
                embeddings[bid] = emb
        for i, id_a in enumerate(ids):
            for id_b in ids[i + 1:]:
                if id_a in embeddings and id_b in embeddings:
                    sim = rt.embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                    if sim > best_sim:
                        best_sim = sim
                        best_pair = (id_a, id_b)
        if best_pair and best_sim > 0.5:
            return (
                f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
            )
    except Exception as e:
        rt.logger.warning(f"Dream connection hint failed: {e}")
    return ""


async def build_crystal_hint(all_buckets: list) -> str:
    if not (rt.embedding_engine and rt.embedding_engine.enabled):
        return ""
    try:
        feels = [
            b for b in all_buckets
            if b["metadata"].get("type") == "feel"
            and not is_letter_bucket(b)
            and not parse_bool(
                (b.get("metadata") or {}).get("protected"), default=False
            )
        ]
        if len(feels) < _CRYSTAL_CLUSTER_MIN:
            return ""
        feel_embeddings: dict = {}
        for f in feels:
            emb = await rt.embedding_engine.get_embedding(f["id"])
            if emb is not None:
                feel_embeddings[f["id"]] = emb
        for fid, femb in feel_embeddings.items():
            similar_feels = []
            for oid, oemb in feel_embeddings.items():
                if oid != fid:
                    sim = rt.embedding_engine._cosine_similarity(femb, oemb)
                    if sim > 0.7:
                        similar_feels.append(oid)
            if len(similar_feels) >= _CRYSTAL_CLUSTER_MIN - 1:
                feel_bucket = next((f for f in feels if f["id"] == fid), None)
                if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                    content_preview = strip_wikilinks(feel_bucket["content"][:80])
                    return (
                        f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                        f"（围绕「{content_preview}…」）。"
                        f"如果这已经是确信而不只是感受了，"
                        f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                        f"不急，你自己决定。\n"
                    )
    except Exception as e:
        rt.logger.warning(f"Dream crystallization hint failed: {e}")
    return ""


@dataclass
class SelfCandidate:
    """一条待沉淀的「我觉得……」，和这次梦里跟它撞上的材料。"""

    bucket: dict
    passes: list[str]
    collisions: list[tuple[dict, float]] = field(default_factory=list)


@dataclass
class SelfReview:
    """dream 里的 I 候选段。

    ``candidates`` 是**还缺见证**的候选，只有它们需要完整的碰撞材料。
    ``ready`` 是见证已经攒够、只等模型去 promote 的，压成一行提醒就够——
    再给它们完整块是纯浪费：一条 3/3 的候选再被见证一百次也不会有任何变化，
    却会占着预算，把真正还缺见证的挤出去。

    ``rendered_ids`` 由 output.py 回写真正出现在最终文本中的候选（近期正文、
    候选主块或碰撞材料），dream 的 dispatch 只给它们记「被见证过一次」。
    压成一行的 ``ready`` 不算见证——那不是「和材料摆在一起看过」。

    ``pending_ids`` 是这场梦**考虑过**的全部待沉淀候选，不管有没有被渲染。
    它和 ``rendered_ids`` 的差额就是「这场梦它在队列里，但没被看见」。两个数
    分开记，才能回答「一条候选攒不到见证，是因为梦做得少，还是因为梦做了
    但它从来没排到」——前者不是 bug，后者是。
    """

    candidates: list[SelfCandidate] = field(default_factory=list)
    ready: list[SelfCandidate] = field(default_factory=list)
    vectors_available: bool = False
    threshold: int = I_PROMOTE_THRESHOLD
    rendered_ids: list[str] = field(default_factory=list)
    starved: int = 0
    pending_ids: list[str] = field(default_factory=list)


def _timestamp_key(bucket: dict) -> str:
    meta = bucket.get("metadata") or {}
    return str(meta.get("last_active") or meta.get("created") or "")


async def collect_self_candidates(all_buckets: list, window_hours: int) -> SelfReview:
    """收集待沉淀的 I 候选，并为每条取几条语义上最挨着的对照材料。

    I 候选需要三次跨日见证才能升级，因此不能复用普通记忆的近期窗口；
    否则过期候选会永久卡住。``window_hours`` 只约束普通 dream 记忆，保留
    在参数中是为了维持调用契约。候选仍排除 pinned / resolved / protected。
    材料只是材料：支持、反驳、撞车都可能，这里不做任何判定。
    """
    pending = [
        b for b in all_buckets
        if is_pending_candidate(b) and not is_letter_bucket(b)
        and not parse_bool(
            (b.get("metadata") or {}).get("protected"), default=False
        )
        and not (b.get("metadata") or {}).get("pinned", False)
        and not (b.get("metadata") or {}).get("resolved", False)
    ]
    if not pending:
        return SelfReview()

    # 在 `pending` 被下面的取舍改写之前先留一份全量 id：谁被展开是这场梦的
    # 结果，谁在队列里是这场梦的事实，后者才是「它到底等了几场梦」的分母。
    all_pending_ids = [
        str(b.get("id") or "").strip() for b in pending if str(b.get("id") or "").strip()
    ]

    # 按「还差几次见证」排，不按 created。
    #
    # 原先是 created 升序（最旧在前）+ 无上限，而 output.py 逐条撞预算、撞满即丢
    # 且不计见证。两件事合起来就是队首阻塞：最旧的永远排在前面吃预算，新写的候选
    # 排在队尾，拿不到见证 → 永远转不了正 → 永远留在队列里继续挡着后面的。
    #
    # 攒够的（passes >= threshold）单独拆出去：它们只等模型去 promote，再给完整
    # 碰撞材料是纯浪费，一行提醒就够。
    threshold = I_PROMOTE_THRESHOLD
    growing: list[dict] = []
    ready: list[dict] = []
    for bucket in pending:
        if len(dream_dates(bucket.get("metadata") or {})) >= threshold:
            ready.append(bucket)
        else:
            growing.append(bucket)

    def _need_key(bucket: dict) -> tuple:
        meta = bucket.get("metadata") or {}
        dates = dream_dates(meta)
        # 缺得最多的排最前；同样缺的，最久没被见证的先来——保证轮转，
        # 不让固定几条把每场梦的名额包了。
        return (len(dates), dates[-1] if dates else "", str(meta.get("created") or ""))

    growing.sort(key=_need_key)
    starved = max(0, len(growing) - _MAX_SELF_CANDIDATES_PER_DREAM)
    growing = growing[:_MAX_SELF_CANDIDATES_PER_DREAM]
    ready.sort(key=lambda b: str((b.get("metadata") or {}).get("created") or ""))

    review = SelfReview(
        candidates=[
            SelfCandidate(bucket=b, passes=dream_dates(b.get("metadata") or {}))
            for b in growing
        ],
        ready=[
            SelfCandidate(bucket=b, passes=dream_dates(b.get("metadata") or {}))
            for b in ready
        ],
        starved=starved,
        pending_ids=all_pending_ids,
    )
    pending = growing

    if not (rt.embedding_engine and rt.embedding_engine.enabled):
        return review

    try:
        pending_ids = {b["id"] for b in pending}
        # 正式 I 条目和其它候选永远参与——候选最该撞的就是「我已经认下的东西」
        # 和「我另一个还没想清楚的念头」。
        pool = [
            b for b in all_buckets
            if b["id"] not in pending_ids
            and not is_letter_bucket(b)
            and (b.get("metadata") or {}).get("type") == "i"
            and not (b.get("metadata") or {}).get("pinned", False)
            and not parse_bool(
                (b.get("metadata") or {}).get("protected"), default=False
            )
        ]
        ordinary = [
            b for b in all_buckets
            if b["id"] not in pending_ids
            and not is_letter_bucket(b)
            and (b.get("metadata") or {}).get("type") not in ("i", "letter")
            and not (b.get("metadata") or {}).get("pinned", False)
            and not parse_bool(
                (b.get("metadata") or {}).get("protected"), default=False
            )
        ]
        ordinary.sort(key=_timestamp_key, reverse=True)
        pool.extend(ordinary[:_COLLISION_POOL_RECENT])
        # 候选之间也要能撞上：两个还没想清楚的念头互相矛盾，正是最该被看见的。
        pool.extend(pending)

        embeddings: dict = {}
        for b in pool:
            bid = b["id"]
            if bid in embeddings:
                continue
            emb = await rt.embedding_engine.get_embedding(bid)
            if emb is not None:
                embeddings[bid] = emb
        if not embeddings:
            return review
        review.vectors_available = True

        by_id = {b["id"]: b for b in pool}
        for candidate in review.candidates:
            cid = candidate.bucket["id"]
            cemb = embeddings.get(cid)
            if cemb is None:
                continue
            scored: list[tuple[dict, float]] = []
            for other_id, oemb in embeddings.items():
                if other_id == cid or other_id not in by_id:
                    continue
                sim = rt.embedding_engine._cosine_similarity(cemb, oemb)
                if sim >= _COLLISION_MIN_SIM:
                    scored.append((by_id[other_id], sim))
            scored.sort(key=lambda pair: pair[1], reverse=True)
            candidate.collisions = scored[:_COLLISION_TOP_K]
    except Exception as e:
        rt.logger.warning(f"Dream self candidate collisions failed: {e}")

    return review
