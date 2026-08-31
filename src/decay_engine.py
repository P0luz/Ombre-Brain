"""
========================================
decay_engine.py — 记忆衰减引擎，模拟人类遗忘曲线
========================================

这个文件负责给每个桶算「现在还有多重」的权重分，然后把分数掉到阈值以下
的桶搬到 archive。后台一个 asyncio 任务每隔 N 小时跑一次。

关键行为：
- 打分公式（改进版艾宾浩斯 + 情感坐标）：
    Score = Importance × (activation_count^0.3) × e^(-λ×days) × emotion_weight
- 情感权重 = base + arousal × arousal_boost；唤醒度高的记忆衰减得慢
- anchor / pinned / protected 桶不参与衰减、不被归档
- ensure_started() 幂等启动后台循环；可被测试 monkeypatch 成 noop

不做什么（边界）：
- 不删除桶（只把分数低的搬到 archive）
- 不做内容修改、不打标、不调用 LLM
- 不决定「该不该 hold/grow」，只对已有桶打分

对外暴露：DecayEngine 类（calculate_score / run_once / ensure_started）
========================================
"""

import math
import asyncio
import logging
from datetime import datetime

from utils import parse_bool, parse_iso_datetime

logger = logging.getLogger("ombre_brain.decay")


# ============================================================
# 调参面板 / Tunable constants
# ------------------------------------------------------------
# rule.md §⑩：禁止裸魔法数字。下面这些常量原本散落在 calculate_score()
# 和 run_decay_cycle() 各处，集中后：① 公式可读性大幅提升；
# ② 任何调参改一处即可；③ 单元测试可直接 import 这些常量做断言。
#
# ⚠️ 改这些数字前先读 rule.md §1.0 哲学："记忆只会淡去，不会消失"。
# decay 不是删除，是分数下沉。改 threshold/lambda 会直接影响"多少天后被遗忘"。
# ============================================================

# --- DecayEngine 默认值（被 config.yaml 的 decay.* 覆盖）---
_DEFAULT_LAMBDA = 0.05            # 指数衰减率：每过一天分数 × e^(-λ)
_DEFAULT_THRESHOLD = 0.3          # 低于此分数 → 归档
_DEFAULT_CHECK_INTERVAL_HRS = 24  # 后台循环间隔（小时）
_DEFAULT_EMOTION_BASE = 1.0       # 情感权重基准
_DEFAULT_AROUSAL_BOOST = 0.8      # arousal 每 +1 → 情感权重 +0.8

# --- 锁分：某些桶不参与衰减 ---
_SCORE_PINNED = 999.0    # pinned / protected / permanent 桶恒高分（永不归档）
_SCORE_FEEL = 50.0       # feel / plan / letter 桶固定中分（生命周期由 status 控制）

# --- 周期自愈：每轮衰减最多补多少条缺失向量（防一次性打爆 embedding API）---
# 活跃桶落盘了但 embeddings.db 没它的向量 → breath 向量通道会漏掉它（permanent
# 尤其常见，见 #6）。剩余的下一轮继续补。
_BACKFILL_MAX_PER_CYCLE = 50

# --- Freshness bonus：bonus = 1 + e^(-hours/HALF_LIFE) ---
_FRESHNESS_HALF_LIFE_HRS = 36.0  # 36h 半衰：刚存 ×2.0，36h 后 ×1.5，72h 后 ≈×1.14
_FRESHNESS_AMPLITUDE = 1.0       # bonus 上限增量（0 → 无加成；1 → 最多 ×2）

# --- 短期 vs 长期权重分配（核心心理模型）---
# 短期：刚发生的事 time 占主导（"印象很新"）
# 长期：超过这个分界后 emotion 占主导（"刻骨铭心 vs 已经无所谓"）
_SHORT_TERM_DAYS = 3.0
_SHORT_TERM_TIME_RATIO = 0.7
_LONG_TERM_EMOTION_RATIO = 0.7

# --- Activation count 的次线性放大：访问越多越鲜活，但不线性 ---
_ACTIVATION_EXPONENT = 0.3

# --- Resolved/digested 衰减加速因子 ---
_FACTOR_RESOLVED_DIGESTED = 0.02  # 已处理 + 已写 feel → 加速淡化到背景
_FACTOR_RESOLVED_ONLY = 0.05      # 仅已处理（未写 feel）→ 中度淡化

# --- Urgency boost：高 arousal 且未处理 → 临时加重，避免被错误归档 ---
_AROUSAL_URGENCY_THRESHOLD = 0.7
_URGENCY_BOOST = 1.5

# --- Auto-resolve 触发条件 ---
_AUTO_RESOLVE_IMPORTANCE_MAX = 4   # 重要度 ≤ 4 才允许自动结案
_AUTO_RESOLVE_DAYS_MIN = 30        # 且 30 天未被激活
_AUTO_RESOLVE_FALLBACK_DAYS = 999  # 时间字段坏掉时，按"很久以前"对待，触发自动结案

# --- Arousal/importance 兜底 ---
_DEFAULT_AROUSAL = 0.3
_DEFAULT_IMPORTANCE = 5
_DEFAULT_DAYS_FALLBACK = 30  # calculate_score 时间字段坏 → 按 30 天处理（保守）

# --- 时间换算 ---
_SECONDS_PER_DAY = 86400
_SECONDS_PER_HOUR = 3600


def _days_since_active(meta: dict, fallback_days: float = _DEFAULT_DAYS_FALLBACK) -> float:
    """从 metadata 解析"距上次激活的天数"。

    抽出来的原因：原文件里 calculate_score / run_decay_cycle 各写了一遍
    同样的 "fromisoformat → 求差 → 兜底" 三段式，且兜底值还不一样
    （前者 30、后者 999）。统一成一个函数，由调用方传 fallback_days
    决定坏数据怎么处理：
      * calculate_score 用默认 30：保守地按"一个月没动"算分
      * run_decay_cycle 的 auto-resolve 路径传 999：让坏数据顺利触发结案

    边界（rule.md §⑨）：
      * meta 不是 dict / 字段缺失 / 字符串无法解析 → 返回 fallback_days
      * 永远返回 ≥ 0 的浮点数（防止时钟漂移产生负数）
    """
    if not isinstance(meta, dict):
        return fallback_days
    raw = meta.get("last_active") or meta.get("created") or ""
    try:
        last_active = parse_iso_datetime(raw)
        return max(0.0, (datetime.now() - last_active).total_seconds() / _SECONDS_PER_DAY)
    except (ValueError, TypeError):
        return float(fallback_days)


class DecayEngine:
    """
    Memory decay engine — periodically scans all dynamic buckets,
    calculates decay scores, auto-archives low-activity buckets
    to simulate natural forgetting.
    记忆衰减引擎 —— 定期扫描所有动态桶，
    计算衰减得分，将低活跃桶自动归档，模拟自然遗忘。
    """

    def __init__(self, config: dict, bucket_mgr):
        # --- Load decay parameters / 加载衰减参数 ---
        decay_cfg = config.get("decay", {})
        self.decay_lambda = decay_cfg.get("lambda", _DEFAULT_LAMBDA)
        self.threshold = decay_cfg.get("threshold", _DEFAULT_THRESHOLD)
        self.check_interval = decay_cfg.get("check_interval_hours", _DEFAULT_CHECK_INTERVAL_HRS)

        # --- Emotion weight params (continuous arousal coordinate) ---
        # --- 情感权重参数（基于连续 arousal 坐标）---
        emotion_cfg = decay_cfg.get("emotion_weights", {})
        self.emotion_base = emotion_cfg.get("base", _DEFAULT_EMOTION_BASE)
        self.arousal_boost = emotion_cfg.get("arousal_boost", _DEFAULT_AROUSAL_BOOST)

        self.bucket_mgr = bucket_mgr

        # --- Background task control / 后台任务控制 ---
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def is_running(self) -> bool:
        """Whether the decay engine is running in the background.
        衰减引擎是否正在后台运行。"""
        return self._running

    # ---------------------------------------------------------
    # Core: calculate decay score for a single bucket
    # 核心：计算单个桶的衰减得分
    #
    # Higher score = more vivid memory; below threshold → archive
    # 得分越高 = 记忆越鲜活，低于阈值则归档
    # Permanent buckets never decay / 固化桶永远不衰减
    # ---------------------------------------------------------
    # ---------------------------------------------------------
    # Freshness bonus: continuous exponential decay
    # 新鲜度加成：连续指数衰减
    # bonus = 1.0 + 1.0 × e^(-t/36), t in hours
    # t=0 → 2.0×, t≈25h(半衰) → 1.5×, t≈72h → ≈1.14×, t→∞ → 1.0×
    # ---------------------------------------------------------
    @staticmethod
    def _calc_time_weight(days_since: float) -> float:
        """
        Freshness bonus multiplier: 1.0 + e^(-t/36), t in hours.
        新鲜度加成乘数：刚存入×2.0，~36小时半衰，72小时后趋近×1.0。
        """
        hours = days_since * 24.0
        return 1.0 + _FRESHNESS_AMPLITUDE * math.exp(-hours / _FRESHNESS_HALF_LIFE_HRS)

    def calculate_score(self, metadata: dict) -> float:
        """
        Calculate current activity score for a memory bucket.
        计算一个记忆桶的当前活跃度得分。

        New model: short-term vs long-term weight separation.
        新模型：短期/长期权重分离。
        - Short-term (≤3 days): time_weight dominates, emotion amplifies
        - Long-term (>3 days): emotion_weight dominates, time decays to floor
        短期（≤3天）：时间权重主导，情感放大
        长期（>3天）：情感权重主导，时间衰减到底线
        """
        if not isinstance(metadata, dict):
            return 0.0

        # --- Pinned/protected buckets: never decay, importance locked to 10 ---
        if metadata.get("pinned") or metadata.get("protected"):
            return _SCORE_PINNED

        # --- Permanent buckets never decay ---
        if metadata.get("type") == "permanent":
            return _SCORE_PINNED

        # --- Feel buckets: never decay, fixed moderate score ---
        if metadata.get("type") == "feel":
            return _SCORE_FEEL

        # --- Plan / letter buckets: never decay (status-driven, not time-driven) ---
        # --- plan / letter 桶不衰减；plan 由 status 字段控制生命周期，letter 永久保存 ---
        if metadata.get("type") in ("plan", "letter"):
            return _SCORE_FEEL

        try:
            importance = max(1, min(10, int(metadata.get("importance", _DEFAULT_IMPORTANCE))))
        except (TypeError, ValueError):
            importance = _DEFAULT_IMPORTANCE
        activation_count = max(1.0, float(metadata.get("activation_count") or 1))

        # --- Days since last activation ---
        days_since = _days_since_active(metadata, fallback_days=_DEFAULT_DAYS_FALLBACK)

        # --- Emotion weight ---
        try:
            arousal = max(0.0, min(1.0, float(metadata.get("arousal", _DEFAULT_AROUSAL))))
        except (ValueError, TypeError):
            arousal = _DEFAULT_AROUSAL
        emotion_weight = self.emotion_base + arousal * self.arousal_boost

        # --- Time weight ---
        time_weight = self._calc_time_weight(days_since)

        # --- Short-term vs Long-term weight separation ---
        # 短期（≤3天）：time_weight 占 70%，emotion 占 30%
        # 长期（>3天）：emotion 占 70%，time_weight 占 30%
        if days_since <= _SHORT_TERM_DAYS:
            # Short-term: time dominates, emotion amplifies
            combined_weight = (
                time_weight * _SHORT_TERM_TIME_RATIO
                + emotion_weight * (1.0 - _SHORT_TERM_TIME_RATIO)
            )
        else:
            # Long-term: emotion dominates, time provides baseline
            combined_weight = (
                emotion_weight * _LONG_TERM_EMOTION_RATIO
                + time_weight * (1.0 - _LONG_TERM_EMOTION_RATIO)
            )

        # --- Base score ---
        base_score = (
            importance
            * (activation_count ** _ACTIVATION_EXPONENT)
            * math.exp(-self.decay_lambda * days_since)
            * combined_weight
        )

        # --- Weight pool modifiers ---
        # resolved + digested (has feel) → 加速淡化
        # resolved only → 中度淡化
        resolved = metadata.get("resolved", False)
        digested = metadata.get("digested", False)  # set when feel is written for this memory
        if resolved and digested:
            resolved_factor = _FACTOR_RESOLVED_DIGESTED
        elif resolved:
            resolved_factor = _FACTOR_RESOLVED_ONLY
        else:
            resolved_factor = 1.0
        urgency_boost = (
            _URGENCY_BOOST
            if (arousal > _AROUSAL_URGENCY_THRESHOLD and not resolved)
            else 1.0
        )

        return round(base_score * resolved_factor * urgency_boost, 4)

    # ---------------------------------------------------------
    # Execute one decay cycle
    # 执行一轮衰减周期
    # Scan all dynamic buckets → score → archive those below threshold
    # 扫描所有动态桶 → 算分 → 低于阈值的归档
    # ---------------------------------------------------------
    async def run_decay_cycle(self) -> dict:
        """
        Execute one decay cycle: iterate dynamic buckets, archive those
        scoring below threshold.
        执行一轮衰减：遍历动态桶，归档得分低于阈值的桶。

        Returns stats: {"checked": N, "archived": N, "lowest_score": X}
        """
        try:
            buckets = await self.bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for decay / 衰减周期列桶失败: {e}")
            return {"checked": 0, "archived": 0, "lowest_score": 0, "error": str(e)}

        checked = 0
        archived = 0
        auto_resolved = 0
        lowest_score = float("inf")

        demoted_orphans = 0
        for bucket in buckets:
            meta = bucket.get("metadata", {})

            # Skip anchor / permanent / pinned / protected / feel / i / plan / letter buckets
            # 跳过 anchor、固化桶、钉选/保护桶、feel 桶和 i（自我认知）桶
            # i 桶承诺永不衰减（tools/i/core.py 注释）——必须在此显式排除
            # anchor 是 dynamic 桶上的 bool 标记，不锁高 importance，必须显式排除
            # plan / letter 同样必须在此排除：calculate_score() 对它们恒定返回
            # _SCORE_FEEL，本来就不会被下面的归档阈值判定动到，但下面的自动结案
            # 分支跑在 calculate_score() 之前、不看 type，会直接把 plan/letter
            # 也 resolved=True——plan 的生命周期只能由 status 字段驱动，letter
            # 承诺永久保留原样，两者都不该被这条「重要度低+超期未解决」的通用
            # 自动结案逻辑碰。
            if (
                meta.get("type") in ("permanent", "feel", "i", "plan", "letter")
                or meta.get("pinned")
                or meta.get("protected")
                or parse_bool(meta.get("anchor"), default=False)
            ):
                continue

            checked += 1

            # --- Auto-resolve: imp≤4 + >30 days old + not resolved → auto resolve ---
            # --- 自动结案：重要度≤4 + 超过30天 + 未解决 → 自动 resolve ---
            if not meta.get("resolved", False):
                imp = int(meta.get("importance") or _DEFAULT_IMPORTANCE)
                # auto-resolve 路径上时间字段坏 → 按 999 天处理（加速会被结案）
                days_since = _days_since_active(
                    meta, fallback_days=_AUTO_RESOLVE_FALLBACK_DAYS
                )
                if imp <= _AUTO_RESOLVE_IMPORTANCE_MAX and days_since > _AUTO_RESOLVE_DAYS_MIN:
                    try:
                        await self.bucket_mgr.update(bucket["id"], resolved=True)
                        meta["resolved"] = True  # refresh local meta so resolved_factor applies this cycle
                        auto_resolved += 1
                        logger.info(
                            f"Auto-resolved / 自动结案: "
                            f"{meta.get('name', bucket['id'])} "
                            f"(imp={imp}, days={days_since:.0f})"
                        )
                    except Exception as e:
                        logger.warning(f"Auto-resolve failed / 自动结案失败: {e}")

            try:
                score = self.calculate_score(meta)
            except Exception as e:
                logger.warning(
                    f"Score calculation failed for {bucket.get('id', '?')} / "
                    f"计算得分失败: {e}"
                )
                continue

            lowest_score = min(lowest_score, score)

            # --- Below threshold → archive (simulate forgetting) ---
            # --- 低于阈值 → 归档（模拟遗忘）---
            if score < self.threshold:
                try:
                    success = await self.bucket_mgr.archive(bucket["id"])
                    if success:
                        archived += 1
                        logger.info(
                            f"Decay archived / 衰减归档: "
                            f"{meta.get('name', bucket['id'])} "
                            f"(score={score:.4f}, threshold={self.threshold})"
                        )
                except Exception as e:
                    logger.warning(
                        f"Archive failed for {bucket.get('id', '?')} / "
                        f"归档失败: {e}"
                    )

        # --- Self-heal: 补齐缺失向量（周期性，详见 _self_heal_embeddings）---
        backfilled_embeddings = await self._self_heal_embeddings(buckets)

        result = {
            "checked": checked,
            "archived": archived,
            "auto_resolved": auto_resolved,
            "demoted_orphans": demoted_orphans,
            "backfilled_embeddings": backfilled_embeddings,
            "lowest_score": lowest_score if checked > 0 else 0,
        }
        logger.info(f"Decay cycle complete / 衰减周期完成: {result}")
        return result

    async def _self_heal_embeddings(self, buckets: list) -> int:
        """周期自愈：给「落盘了但 embeddings.db 里没向量」的活跃桶补向量。

        背景（#6）：permanent 桶常因批量导入 / dashboard 钉选而漏建向量，
        breath 的向量通道就检索不到它们，表现为「只读得到 dynamic」。衰减循环
        每轮顺手补齐，无需人工跑 backfill_embeddings.py。

        边界：embedding 未启用 → 跳过；每轮最多补 _BACKFILL_MAX_PER_CYCLE 条
        （防打爆 API），剩余下一轮继续；单条失败仅 warning（rule.md §1.5 允许降级）。
        只处理活跃桶（buckets 不含 archive），不在此删孤儿向量（删除走专用脚本，
        避免把 archive 桶的有效向量误判为孤儿）。"""
        outbox = getattr(self.bucket_mgr, "embedding_outbox", None)
        if outbox is not None and getattr(outbox, "running", False):
            try:
                queued = await outbox.reconcile(
                    buckets=buckets,
                    include_archive=False,
                )
                if queued:
                    logger.info(
                        "Decay self-heal queued / 衰减自愈已加入向量队列: %s 条",
                        queued,
                    )
                return queued
            except Exception as e:
                logger.warning(f"self-heal embeddings: 投递后台队列失败: {e}")
                return 0

        ee = getattr(self.bucket_mgr, "embedding_engine", None)
        if not ee or not getattr(ee, "enabled", False):
            return 0
        try:
            index_ids = set(ee.list_all_ids())
        except Exception as e:
            logger.warning(f"self-heal embeddings: 读取向量索引失败: {e}")
            return 0
        missing = [b for b in buckets if b["id"] not in index_ids and (b.get("content") or "").strip()]
        if not missing:
            return 0
        healed = 0
        for b in missing[:_BACKFILL_MAX_PER_CYCLE]:
            try:
                if await ee.generate_and_store(b["id"], b["content"]):
                    healed += 1
            except Exception as e:
                logger.warning(f"self-heal embeddings: 补 {b['id']} 失败: {e}")
        if healed:
            remaining = len(missing) - healed
            logger.info(
                f"Decay self-heal / 自愈补向量: {healed} 条"
                + (f"（本轮上限 {_BACKFILL_MAX_PER_CYCLE}，剩 {remaining} 下轮继续）"
                   if remaining > 0 else "")
            )
        return healed

    # ---------------------------------------------------------
    # Background decay task management
    # 后台衰减任务管理
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        """
        Ensure the decay engine is started (lazy init on first call).
        确保衰减引擎已启动（懒加载，首次调用时启动）。
        """
        if not self._running:
            await self.start()

    async def start(self) -> None:
        """Start the background decay loop.
        启动后台衰减循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Decay engine started, interval: {self.check_interval}h / "
            f"衰减引擎已启动，检查间隔: {self.check_interval} 小时"
        )

    async def stop(self) -> None:
        """Stop the background decay loop.
        停止后台衰减循环。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Decay engine stopped / 衰减引擎已停止")

    async def _background_loop(self) -> None:
        """Background loop: run decay → sleep → repeat.
        后台循环体：执行衰减 → 睡眠 → 重复。"""
        while self._running:
            try:
                await self.run_decay_cycle()
            except Exception as e:
                logger.error(f"Decay cycle error / 衰减周期出错: {e}")
            # --- Wait for next cycle / 等待下一个周期 ---
            try:
                await asyncio.sleep(self.check_interval * _SECONDS_PER_HOUR)
            except asyncio.CancelledError:
                break
