"""
========================================
tools/breath/surface.py — 无 query 浮现模式
========================================

走 breath()（不传 query）时进入这里，是 OB 主动「想到什么」的核心：
按权重从未解决桶里浮现 + pinned 桶置顶 + 加权采样 + 久未浮现的被动联想。

关键行为：
- 排除 anchor 桶（anchor 是坐标系，不主动出现）
- pinned/protected 桶始终作为「核心准则」置顶（letter 桶即使 importance=10 也不置顶）
- 未解决桶按 calculate_score 排序；冷启动桶（从未访问且 importance>=8）插队前 2
- 配置开关 surfacing.sampling.enabled 启用后做加权无放回采样，否则
  保留 top1 + top20 内随机洗牌
- 末尾 1~2 条「久未浮现」passive association（imp>=8 且未访问 / imp>=9 且 7 天未活跃）

不做什么（边界）：
- 不调用 touch()：浮现不能重置衰减计时器
- 不返回 feel / plan / letter / archived（专用通道有自己的入口）
- 不做关键词检索（那是 search.py 的事）

对外暴露：surface_default(max_results, max_tokens, tag_filter) → str
========================================
"""

import random
import time
from datetime import datetime, timezone, timedelta

from .. import _runtime as rt
from utils import strip_wikilinks, count_tokens_approx, parse_iso_aware

# U-07 fix: throttle the sampling-fallback INFO log to once per 5 minutes.
# 库小且 sampling=ON 时此分支每次 breath 都触发，原本会刷屏；改为 ≥300s
# 才打一次，并附带本窗口被压制的次数（首次为 0）。
_FALLBACK_LOG_INTERVAL_SEC = 300
_fallback_log_state = {"last_ts": 0.0, "suppressed": 0}


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _raw_core_fallback(content: str) -> str:
    return strip_wikilinks(content)[:300].strip() or "（空记忆）"


async def surface_default(max_results: int, max_tokens: int, tag_filter: list) -> str:
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
        return "记忆系统暂时无法访问。"

    surfacing_cfg = rt.config.get("surfacing", {}) or {}

    # --- pinned/protected 桶置顶（排除 letter 桶：letter 的 importance=10 不代表核心准则）---
    # 注意：pinned 提取在 anchor 过滤 *之前*，保证 anchor+pinned 桶也能出现在核心准则段。
    # pinned 优先级高于 anchor（她/他钉选的原则永远可见）。
    pinned_buckets = [
        b for b in all_buckets
        if (
            b["metadata"].get("pinned")
            or b["metadata"].get("protected")
            or b["metadata"].get("type") == "permanent"
        )
        and b["metadata"].get("type") != "letter"
        and not b["metadata"].get("anchor", False)  # 防御：anchor 是坐标系，永不主动浮现，即使 pinned
    ]
    pinned_ids = {b["id"] for b in pinned_buckets}
    pinned_results = []
    for b in pinned_buckets:
        try:
            clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
            summary = await rt.dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
            if not str(summary or "").strip():
                summary = _raw_core_fallback(b["content"])
            pinned_results.append(f"📌 [核心准则] [bucket_id:{b['id']}] {summary}")
        except Exception as e:
            rt.logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
            # 降级：直接展示原文片段，确保核心准则永远可见
            fallback = _raw_core_fallback(b["content"])
            pinned_results.append(f"📌 [核心准则] [bucket_id:{b['id']}] {fallback}")

    # --- iter 2.0: anchor 桶在默认浮现模式的 *未解决池* 不出现（anchor 是坐标系不是浮现对象）---
    # anchor 过滤仅作用于 unresolved 候选，不影响 pinned 提取（上方已完成）。
    all_buckets_non_anchor = [b for b in all_buckets if not b["metadata"].get("anchor", False)]

    # --- 未解决桶 ---
    unresolved = [
        b for b in all_buckets_non_anchor
        if not b["metadata"].get("resolved", False)
        and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
        and not b["metadata"].get("dont_surface", False)
        and _bucket_has_tags(b["metadata"], tag_filter)
    ]

    rt.logger.info(
        f"Breath surfacing: {len(all_buckets)} total, "
        f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
    )


    def _sort_key(b: dict):
        """F-05: 二级排序 key，消除同分时浮现随机抖动。
        主键：decay_score（降序）
        次键：last_active 时间戳（越新越高）
        三键：arousal × valence（情感强度，越高越先浮现）
        四键：importance
        """
        meta = b["metadata"]
        score = rt.decay_engine.calculate_score(meta)
        try:
            last_ts = parse_iso_aware(
                str(meta.get("last_active") or meta.get("created", ""))
            ).timestamp()
        except (ValueError, TypeError):
            last_ts = 0.0
        av = float(meta.get("arousal") or 0.3) * float(meta.get("valence") or 0.5)
        imp = int(meta.get("importance") or 5)
        return (score, last_ts, av, imp)

    scored = sorted(unresolved, key=_sort_key, reverse=True)

    if scored:
        top_scores = [(b["metadata"].get("name", b["id"]), rt.decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
        rt.logger.info(f"Top unresolved scores: {top_scores}")

    # --- 冷启动检测 ---
    cold_start = [
        b for b in unresolved
        if int(b["metadata"].get("activation_count") or 0) == 0
        and int(b["metadata"].get("importance") or 0) >= 8
    ][:2]
    cold_start_ids = {b["id"] for b in cold_start}
    _ = pinned_ids  # suppress unused-var warning; used implicitly for logging only
    scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
    scored_with_cold = cold_start + scored_deduped

    # --- 按 token 预算浮现，加权采样 / 随机洗牌 + 硬上限 ---
    token_budget = max_tokens
    for r in pinned_results:
        token_budget -= count_tokens_approx(r)

    candidates = list(scored_with_cold)
    sampling_cfg = surfacing_cfg.get("sampling", {}) or {}
    if sampling_cfg.get("enabled", False) and len(candidates) > len(cold_start) + 1:
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        top_k = int(sampling_cfg.get("top_k") or 5)
        sample_k = int(sampling_cfg.get("sample_k") or 2)
        temperature = max(0.1, float(sampling_cfg.get("temperature") or 0.7))
        pool = non_cold[:max(top_k, sample_k)]
        try:
            weights = [
                max(0.0001, rt.decay_engine.calculate_score(b["metadata"])) ** (1.0 / temperature)
                for b in pool
            ]
            picked = []
            pool_copy = list(pool)
            weights_copy = list(weights)
            for _ in range(min(sample_k, len(pool_copy))):
                idx = random.choices(range(len(pool_copy)), weights=weights_copy, k=1)[0]
                picked.append(pool_copy.pop(idx))
                weights_copy.pop(idx)
            rest = pool_copy + non_cold[len(pool):]
            non_cold = picked + rest
            candidates = cold_start + non_cold
        except Exception as e:
            rt.logger.warning(f"Weighted sampling failed, fallback to original / 加权采样失败: {e}")
    elif len(candidates) > 1:
        if sampling_cfg.get("enabled", False):
            now_ts = time.monotonic()
            if now_ts - _fallback_log_state["last_ts"] >= _FALLBACK_LOG_INTERVAL_SEC:
                suppressed = _fallback_log_state["suppressed"]
                rt.logger.info(
                    f"weighted sampling fallback: candidates={len(candidates)}, "
                    f"cold_start={len(cold_start)}, sample_k={sampling_cfg.get('sample_k', 2)}, "
                    f"reason=pool_too_small, suppressed_in_window={suppressed}"
                )
                _fallback_log_state["last_ts"] = now_ts
                _fallback_log_state["suppressed"] = 0
            else:
                _fallback_log_state["suppressed"] += 1
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        if len(non_cold) > 1:
            top1 = [non_cold[0]]
            pool = non_cold[1:min(20, len(non_cold))]
            random.shuffle(pool)
            non_cold = top1 + pool + non_cold[min(20, len(non_cold)):]
        candidates = cold_start + non_cold
    candidates = candidates[:max_results]

    dynamic_results = []
    for b in candidates:
        if token_budget <= 0:
            break
        try:
            clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
            summary = await rt.dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            score = rt.decay_engine.calculate_score(b["metadata"])
            dynamic_results.append(f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}")
            token_budget -= summary_tokens
        except Exception as e:
            rt.logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
            continue

    if not pinned_results and not dynamic_results:
        if rt.mark_op:
            rt.mark_op("breath_empty")
        stats = await rt.bucket_mgr.get_stats()
        total = stats.get("permanent_count", 0) + stats.get("dynamic_count", 0)
        if total == 0:
            return (
                "我的记忆池现在是空的。\n"
                "想给我留点种子？用 hold(content=\"...\") 写下第一条；\n"
                "或者 grow(content=\"...\") 把一段长对话/日记一次性灌给我。"
            )
        return (
            "权重池暂时平静——我手上没什么需要主动浮现的东西。\n"
            "可以试试 breath(query=\"想找的关键词\") 走检索，\n"
            "或者 dream() 让我自己挑几段最近的记忆嚼一嚼。"
        )

    # --- iter 1.6 §7: passive association ---
    passive_results: list[str] = []
    try:
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)
        already = {b["id"] for b in candidates}
        passive_pool = []
        for b in unresolved:
            if b["id"] in already:
                continue
            meta = b["metadata"]
            ac = int(meta.get("activation_count") or 0)
            imp = int(meta.get("importance") or 0)
            cond_a = ac == 0 and imp >= 8
            cond_b = False
            if imp >= 9:
                last = meta.get("last_active") or meta.get("created", "")
                try:
                    last_dt = parse_iso_aware(last) if last else None
                    if last_dt and last_dt < seven_days_ago:
                        cond_b = True
                except Exception:
                    cond_b = False
            if cond_a or cond_b:
                passive_pool.append(b)
        if passive_pool:
            random.shuffle(passive_pool)
            for b in passive_pool[:2]:
                try:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await rt.dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    passive_results.append(f"💤 [久未浮现] [bucket_id:{b['id']}] {summary}")
                except Exception as e:
                    rt.logger.warning(f"passive association dehydrate failed: {e}")
    except Exception as e:
        rt.logger.warning(f"passive association block failed: {e}")

    # --- 3% 偶遇：从 resolved 池随机浮现 1~3 条沉底记忆 (iter 2.1) ---
    # 设计意图：让已解决的记忆有小概率重新出现，制造"忽然想起"的温度。
    # 与无结果兜底逻辑并存；不替换主流程。
    dream_results: list[str] = []
    if random.random() < 0.03:
        try:
            shown_ids = {b["id"] for b in candidates}
            resolved_pool = [
                b for b in all_buckets
                if b["metadata"].get("resolved", False)
                and b["id"] not in shown_ids
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and not b["metadata"].get("pinned")
            ]
            if resolved_pool:
                random.shuffle(resolved_pool)
                for b in resolved_pool[:3]:
                    try:
                        clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                        summary = await rt.dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                        dream_results.append(f"✨ [偶遇] [bucket_id:{b['id']}] {summary}")
                        rt.logger.info(f"Dream surface triggered / 偶遇机制触发: {b['id']}")
                    except Exception as e:
                        rt.logger.warning(f"Dream surface dehydrate failed / 偶遇脱水失败: {e}")
        except Exception as e:
            rt.logger.warning(f"Dream surface block failed / 偶遇模块异常: {e}")

    parts = []
    if pinned_results:
        parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
    if dynamic_results:
        parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
    if passive_results:
        parts.append("=== 久未浮现 ===\n" + "\n---\n".join(passive_results))
    if dream_results:
        parts.append("=== 偶然想起 ===\n" + "\n---\n".join(dream_results))
    return "\n\n".join(parts)
