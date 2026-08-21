"""
========================================
tools/breath/surface.py — 无 query 浮现模式
========================================

走 breath()（不传 query）时进入这里，是 OB 主动「想到什么」的核心：
按权重从未解决桶里浮现 + pinned 桶置顶 + 加权采样 + 久未浮现的被动联想。

关键行为：
- 排除 anchor 桶（anchor 是坐标系，不主动出现）
- 排除 digested 桶（已消化记忆只允许显式检索/审计找回）
- 通过主动浮现策略的 pinned/permanent 桶作为「核心准则」置顶
- protected 只防衰减，不进入核心准则、未解决池、被动联想或偶遇池
- 未解决桶按 calculate_score 排序；冷启动桶（从未访问且 importance>=8）的插队数由
  surfacing.cold_start_max_results 控制（默认 2；0 关闭该身份的特殊待遇）
- 配置开关 surfacing.sampling.enabled 启用后，从 top_k 候选池加权无放回抽出并只返回
  最多 sample_k 条普通主候选；否则保留 top1 + top20 内随机洗牌
- 末尾 1~2 条「久未浮现」passive association（imp>=8 且未访问 / imp>=9 且 7 天未活跃）；
  cold_start_max_results=0 时，单凭“未访问”不再进入该段

不做什么（边界）：
- 不调用 touch()：浮现不能重置衰减计时器
- 不返回 feel / plan / letter / archived（专用通道有自己的入口）
- 不做关键词检索（那是 search.py 的事）

对外暴露：surface_default(max_results, max_tokens, tag_filter) → str
========================================
"""

import random
import time
from datetime import datetime, timedelta

from ombrebrain.policy.surfacing import SurfacePolicyVM
from .. import _runtime as rt
from ..plan.core import is_letter_bucket
from utils import parse_bool, parse_iso_datetime
from ._verbatim import render_stored_bucket

# U-07 fix: throttle the sampling-fallback INFO log to once per 5 minutes.
# 库小且 sampling=ON 时此分支每次 breath 都触发，原本会刷屏；改为 ≥300s
# 才打一次，并附带本窗口被压制的次数（首次为 0）。
_FALLBACK_LOG_INTERVAL_SEC = 300
_fallback_log_state = {"last_ts": 0.0, "suppressed": 0}
_SURFACE_POLICY = SurfacePolicyVM.default()
_BUDGET_NOTICE = (
    "token 预算不足：有 {omitted} 条主要浮现记忆因放不下剩余预算而未返回；"
    "已返回正文均保持完整，未截断或摘要。"
    "当前约使用 {used}/{limit} token，如需被省略的整桶请提高 max_tokens 后重试。"
)
_BREATH_SAFETY_CAP = 40_000
_PIN_BUDGET_NOTICE = (
    "token 预算不足：核心准则 required≈{required} tokens（完整渲染核心准则总计），"
    "limit={limit} tokens，omitted={omitted} 条；普通浮现已跳过（ordinary surfacing skipped）。"
)


def _bucket_has_tags(meta: dict, tag_filter: list) -> bool:
    if not tag_filter:
        return True
    bucket_tags = set(meta.get("tags", []) or [])
    return all(t in bucket_tags for t in tag_filter)


def _can_surface(bucket: dict) -> bool:
    return _SURFACE_POLICY.evaluate_bucket(bucket, mode="spontaneous").allowed


def _budget_notice(*, omitted: int, used: int, limit: int) -> str:
    return _BUDGET_NOTICE.format(omitted=omitted, used=used, limit=limit)


def _pin_budget_notice(*, required: int, limit: int, omitted: int) -> str:
    notice = _PIN_BUDGET_NOTICE.format(
        required=required,
        limit=limit,
        omitted=omitted,
    )
    if limit < _BREATH_SAFETY_CAP:
        return (
            notice
            + "如需返回更多核心准则，可由用户明确提高 max_tokens / "
            "surfacing.breath_max_tokens；当前版本最高 40000。"
        )
    return notice + "已达到当前版本 40000 token 安全上限；请精简或取消部分核心准则后重试。"


async def surface_plans(max_tokens: int) -> str:
    """走 breath(domain="plan") 时进入这里，逐字返回所有 active plan。

    plan 不参与普通浮现（surface_default 会把它排除），dream 末尾的 plan 段又
    可能因总预算降级成只报条数。没有这个通道时，plan 的正文根本没有读取入口。

    与 feel 通道同构：按 created 倒序，在 token 预算内逐条放全文，
    放不下的整条省略，不截断、不摘要。
    """
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
        plans = [
            b for b in all_buckets
            if b.get("metadata", {}).get("type") == "plan"
            and not is_letter_bucket(b)
            and b.get("metadata", {}).get("status", "active") == "active"
        ]
        plans.sort(key=lambda b: b.get("metadata", {}).get("created", ""), reverse=True)
        if not plans:
            return "没有计划。"

        try:
            footprint_snapshot = rt.bucket_mgr.footprint_snapshot()
        except Exception as exc:
            rt.logger.warning(f"Footprint snapshot unavailable / 足迹读取失败: {exc}")
            footprint_snapshot = None

        def _footprint(bucket: dict) -> str:
            if footprint_snapshot is None:
                return "👣 Footprint：暂时无法读取"
            return footprint_snapshot.summary(
                str(bucket.get("id") or ""), bucket.get("metadata", {})
            )

        lines: list[str] = []
        used = 0
        omitted = 0
        for index, p in enumerate(plans):
            created = p["metadata"].get("created", "")
            entry, cost = render_stored_bucket(
                p,
                f"[{created}] [bucket_id:{p['id']}]",
                _footprint(p),
            )
            if used + cost <= max_tokens:
                lines.append(entry)
                used += cost
            else:
                omitted = len(plans) - index
                break
        out = (
            "=== 你的 active plans（新→旧）===\n"
            "完成了用 trace(bucket_id, status=\"resolved\")，"
            "放弃了用 trace(bucket_id, status=\"abandoned\")。\n\n"
            + "\n---\n".join(lines)
        )
        if omitted:
            out += f"\n\n另有 {omitted} 条 plan 因 token 预算不足未返回；正文未截断或摘要。"
        return out
    except Exception as e:
        rt.logger.error(f"Plan retrieval failed: {e}")
        return "读取 plan 失败。"


async def surface_default(max_results: int, max_tokens: int, tag_filter: list) -> str:
    try:
        all_buckets = await rt.bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        rt.logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
        return "记忆系统暂时无法访问。"

    surfacing_cfg = rt.config.get("surfacing", {}) or {}
    try:
        footprint_snapshot = rt.bucket_mgr.footprint_snapshot()
    except Exception as exc:
        rt.logger.warning(f"Footprint snapshot unavailable / 足迹读取失败: {exc}")
        footprint_snapshot = None

    def _footprint(bucket: dict) -> str:
        if footprint_snapshot is None:
            return "👣 Footprint：暂时无法读取"
        return footprint_snapshot.summary(
            str(bucket.get("id") or ""), bucket.get("metadata", {})
        )

    # --- pinned/permanent 桶置顶（protected 仅防衰减，不主动浮现）---
    # 排除 letter 桶：letter 的 importance=10 不代表核心准则。
    # pinned 与 anchor 在正常写入路径互斥：钉选会清除 anchor，设 anchor 会拒绝 pinned 桶。
    # 末尾的 anchor 排除是脏数据防御；若异常并存，仍按 anchor 语义不主动浮现。
    pinned_buckets = [
        b for b in all_buckets
        if (
            b["metadata"].get("pinned")
            or b["metadata"].get("type") == "permanent"
        )
        and not parse_bool(b["metadata"].get("protected"), default=False)
        and _can_surface(b)
        and not is_letter_bucket(b)
        and not b["metadata"].get("anchor", False)  # 防御：anchor 是坐标系，永不主动浮现，即使 pinned
    ]
    core_filter_notice = ""
    if tag_filter and pinned_buckets:
        core_filter_notice = "[说明：tags 仅过滤普通浮现记忆；核心准则按设计始终注入。]"
    pinned_results = []
    token_budget = max_tokens
    pinned_omitted = 0
    pinned_required_tokens = 0
    for b in pinned_buckets:
        try:
            rendered, entry_tokens = render_stored_bucket(
                b,
                f"📌 [核心准则] [bucket_id:{b['id']}]",
                _footprint(b),
            )
            pinned_required_tokens += entry_tokens
            if entry_tokens > token_budget:
                pinned_omitted += 1
                continue
            pinned_results.append(rendered)
            token_budget -= entry_tokens
        except Exception as e:
            rt.logger.warning(f"Failed to render pinned bucket / 钉选桶渲染失败: {e}")

    # --- iter 2.0: anchor 桶在默认浮现模式的 *未解决池* 不出现（anchor 是坐标系不是浮现对象）---
    # anchor 过滤仅作用于 unresolved 候选，不影响 pinned 提取（上方已完成）。
    all_buckets_non_anchor = [b for b in all_buckets if not b["metadata"].get("anchor", False)]

    # --- 未解决桶 ---
    unresolved = [
        b for b in all_buckets_non_anchor
        if _can_surface(b)
        and not b["metadata"].get("resolved", False)
        and not is_letter_bucket(b)
        and b["metadata"].get("type") not in ("permanent", "feel", "plan", "letter", "self", "i")
        and not b["metadata"].get("pinned", False)
        and not parse_bool(b["metadata"].get("protected"), default=False)
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
            last_ts = parse_iso_datetime(
                meta.get("last_active") or meta.get("created", "")
            ).timestamp()
        except (ValueError, TypeError):
            last_ts = 0.0
        # `or` 会把合法的 0.0（比如效价/唤醒度恰好为极端值的记忆）当成缺失值
        # 吞掉，静默换成默认值——用 .get(key, default) 才能保留 0.0 本身。
        try:
            av = float(meta.get("arousal", 0.3)) * float(meta.get("valence", 0.5))
        except (TypeError, ValueError):
            av = 0.3 * 0.5
        imp = int(meta.get("importance") or 5)
        return (score, last_ts, av, imp)

    scored = sorted(unresolved, key=_sort_key, reverse=True)

    if scored:
        top_scores = [(b["metadata"].get("name", b["id"]), rt.decay_engine.calculate_score(b["metadata"])) for b in scored[:5]]
        rt.logger.info(f"Top unresolved scores: {top_scores}")

    # --- 冷启动检测 ---
    raw_cold_start_max = surfacing_cfg.get("cold_start_max_results", 2)
    try:
        if isinstance(raw_cold_start_max, bool):
            raise ValueError("boolean is not an integer count")
        cold_start_max_results = int(raw_cold_start_max)
        if isinstance(raw_cold_start_max, float) and not raw_cold_start_max.is_integer():
            raise ValueError("fractional count")
        if not 0 <= cold_start_max_results <= 2:
            raise ValueError("outside 0..2")
    except (TypeError, ValueError):
        rt.logger.warning(
            "Invalid surfacing.cold_start_max_results=%r; fallback to 2",
            raw_cold_start_max,
        )
        cold_start_max_results = 2
    cold_start = [
        b for b in unresolved
        if int(b["metadata"].get("activation_count") or 0) == 0
        and int(b["metadata"].get("importance") or 0) >= 8
    ][:cold_start_max_results]
    cold_start_ids = {b["id"] for b in cold_start}
    scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
    scored_with_cold = cold_start + scored_deduped

    # --- 按 token 预算浮现，加权采样 / 随机洗牌 + 硬上限 ---
    candidates = list(scored_with_cold)
    sampling_cfg = surfacing_cfg.get("sampling", {}) or {}
    sampling_enabled = parse_bool(sampling_cfg.get("enabled", False), default=False)
    if sampling_enabled and len(candidates) > len(cold_start) + 1:
        n_cold = len(cold_start)
        non_cold = candidates[n_cold:]
        top_k = int(sampling_cfg.get("top_k") or 5)
        sample_k = int(sampling_cfg.get("sample_k") or 2)
        temperature = max(0.1, float(sampling_cfg.get("temperature") or 0.7))
        pool = non_cold[:top_k]
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
            # Sampling is a returned subset, not merely a permutation of the
            # same top-N set.  This is what lets consecutive breaths vary.
            candidates = cold_start + picked
        except Exception as e:
            rt.logger.warning(f"Weighted sampling failed, fallback to original / 加权采样失败: {e}")
    elif len(candidates) > 1:
        if sampling_enabled:
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
    dynamic_omitted = 0
    if not pinned_omitted:
        for b in candidates:
            try:
                score = rt.decay_engine.calculate_score(b["metadata"])
                rendered, entry_tokens = render_stored_bucket(
                    b,
                    f"[权重:{score:.2f}] [bucket_id:{b['id']}]",
                    _footprint(b),
                )
                if entry_tokens > token_budget:
                    dynamic_omitted += 1
                    continue
                dynamic_results.append(rendered)
                token_budget -= entry_tokens
            except Exception as e:
                rt.logger.warning(f"Failed to render surfaced bucket / 浮现渲染失败: {e}")
                continue

    if not pinned_results and not dynamic_results:
        if pinned_omitted:
            return _pin_budget_notice(
                required=pinned_required_tokens,
                limit=max_tokens,
                omitted=pinned_omitted,
            )
        if dynamic_omitted:
            return _budget_notice(
                omitted=dynamic_omitted,
                used=max_tokens - token_budget,
                limit=max_tokens,
            )
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
            "可以试试 breath_search(query=\"想找的关键词\") 走检索，\n"
            "或者 dream() 让我自己挑几段最近的记忆嚼一嚼。"
        )

    # --- iter 1.6 §7: passive association ---
    passive_results: list[str] = []
    try:
        now = datetime.now()
        seven_days_ago = now - timedelta(days=7)
        already = {b["id"] for b in candidates}
        passive_pool = []
        for b in unresolved:
            if b["id"] in already:
                continue
            meta = b["metadata"]
            ac = int(meta.get("activation_count") or 0)
            imp = int(meta.get("importance") or 0)
            # A zero cap disables the special "unvisited" identity everywhere.
            # The same bucket still remains eligible in the ordinary main pool.
            cond_a = cold_start_max_results > 0 and ac == 0 and imp >= 8
            cond_b = False
            if imp >= 9:
                last = meta.get("last_active") or meta.get("created", "")
                try:
                    last_dt = parse_iso_datetime(last) if last else None
                    if last_dt and last_dt < seven_days_ago:
                        cond_b = True
                except Exception:
                    cond_b = False
            if cond_a or cond_b:
                passive_pool.append(b)
        if passive_pool and not pinned_omitted and not dynamic_omitted:
            random.shuffle(passive_pool)
            for b in passive_pool[:2]:
                try:
                    rendered, entry_tokens = render_stored_bucket(
                        b,
                        f"💤 [久未浮现] [bucket_id:{b['id']}]",
                        _footprint(b),
                    )
                    if entry_tokens > token_budget:
                        continue
                    passive_results.append(rendered)
                    token_budget -= entry_tokens
                except Exception as e:
                    rt.logger.warning(f"passive association render failed: {e}")
    except Exception as e:
        rt.logger.warning(f"passive association block failed: {e}")

    # --- 3% 偶遇：从 resolved 池随机浮现 1~3 条沉底记忆 (iter 2.1) ---
    # 设计意图：让已解决的记忆有小概率重新出现，制造"忽然想起"的温度。
    # 与无结果兜底逻辑并存；不替换主流程。
    dream_results: list[str] = []
    if not pinned_omitted and not dynamic_omitted and random.random() < 0.03:
        try:
            shown_ids = {b["id"] for b in candidates}
            resolved_pool = [
                b for b in all_buckets
                if _can_surface(b)
                and b["metadata"].get("resolved", False)
                and b["id"] not in shown_ids
                and not is_letter_bucket(b)
                and b["metadata"].get("type") not in ("feel", "plan", "letter")
                and not b["metadata"].get("pinned")
                and not parse_bool(
                    b["metadata"].get("protected"), default=False
                )
            ]
            if resolved_pool:
                random.shuffle(resolved_pool)
                for b in resolved_pool[:3]:
                    try:
                        rendered, entry_tokens = render_stored_bucket(
                            b,
                            f"✨ [偶遇] [bucket_id:{b['id']}]",
                            _footprint(b),
                        )
                        if entry_tokens > token_budget:
                            continue
                        dream_results.append(rendered)
                        token_budget -= entry_tokens
                        rt.logger.info(f"Dream surface triggered / 偶遇机制触发: {b['id']}")
                    except Exception as e:
                        rt.logger.warning(f"Dream surface render failed / 偶遇渲染失败: {e}")
        except Exception as e:
            rt.logger.warning(f"Dream surface block failed / 偶遇模块异常: {e}")

    parts = []
    if core_filter_notice:
        parts.append(core_filter_notice)
    if pinned_results:
        parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
    if dynamic_results:
        parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))
    if passive_results:
        parts.append("=== 久未浮现 ===\n" + "\n---\n".join(passive_results))
    if dream_results:
        parts.append("=== 偶然想起 ===\n" + "\n---\n".join(dream_results))
    if pinned_omitted:
        parts.append(
            _pin_budget_notice(
                required=pinned_required_tokens,
                limit=max_tokens,
                omitted=pinned_omitted,
            )
        )
    if dynamic_omitted:
        parts.append(
            _budget_notice(
                omitted=dynamic_omitted,
                used=max_tokens - token_budget,
                limit=max_tokens,
            )
        )
    return "\n\n".join(parts)
