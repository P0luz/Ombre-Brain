"""
========================================
bucket_scoring.py — 检索多维评分子函数
========================================

从 bucket_manager.py 拆出。BucketManager.search() 用加权多维评分给候选桶
排序：文本相关性(topic) + 情感共鸣(emotion) + 时间亲近(time) + 触碰频率
(touch)，这四维是纯函数（只读传入参数 + 本模块常量），不碰文件系统/网络，
所以独立成模块，方便单测和复用。

importance / semantic(embedding) / bm25 三个维度的计算逻辑较短，仍留在
bucket_manager.search() 内联（importance 是一行归一化，semantic/bm25 依赖
self.embedding_engine / self._bm25 等实例状态，硬抽出去反而增加耦合）。

不做什么：
- 不做加权求和/归一化（那是 search() 的事，这里只给出单维度 0~1 分）
- 不读 bucket 文件、不碰 self.config（topic 分需要的 content_weight 由
  调用方显式传入，不在这里读配置）

对外暴露：calc_topic_score / calc_emotion_score / calc_time_score /
         calc_touch_score
========================================
"""

import math
from datetime import datetime, timezone
from typing import Optional

from rapidfuzz import fuzz

from utils import parse_iso_aware

# --- topic 文本维度权重 ---
TOPIC_NAME_W = 3.0
TOPIC_DOMAIN_W = 2.5
TOPIC_TAG_W = 2.0
TOPIC_BODY_SLICE = 1000   # body 文本参与 fuzzy 的首部截断长度

# --- emotion 维度 ---
_DEFAULT_VALENCE = 0.5
_DEFAULT_AROUSAL = 0.3
EMOTION_MAX_DIST = math.sqrt(2)  # Russell 理论最大欧氏距离

# --- time 维度 ---
TIME_DECAY_LAMBDA = 0.02  # e^(-λ*days)，越小 → 起冷起慢
TIME_FALLBACK_DAYS = 30   # 无可解析 last_active 时的默认天数

# --- touch 维度 ---
TOUCH_NORMALIZE_CAP = 10.0   # activation_count / 该值，裁到 1.0


# ---------------------------------------------------------
# Topic relevance sub-score:
# name(×3) + domain(×2.5) + tags(×2) + body(×1)
# 文本相关性子分：桶名(×3) + 主题域(×2.5) + 标签(×2) + 正文(×1)
# ---------------------------------------------------------
def calc_topic_score(query: str, bucket: dict, content_weight: float = 1.0) -> float:
    """
    Calculate text dimension relevance score (0~1).
    计算文本维度的相关性得分。
    """
    meta = bucket.get("metadata", {})

    name_score = fuzz.partial_ratio(query, meta.get("name", "")) * TOPIC_NAME_W
    domain_score = (
        max(
            (fuzz.partial_ratio(query, d) for d in meta.get("domain", [])),
            default=0,
        )
        * TOPIC_DOMAIN_W
    )
    tag_score = (
        max(
            (fuzz.partial_ratio(query, tag) for tag in meta.get("tags", [])),
            default=0,
        )
        * TOPIC_TAG_W
    )
    content_score = fuzz.partial_ratio(query, bucket.get("content", "")[:TOPIC_BODY_SLICE]) * content_weight

    return (name_score + domain_score + tag_score + content_score) / (
        100 * (TOPIC_NAME_W + TOPIC_DOMAIN_W + TOPIC_TAG_W + content_weight)
    )


# ---------------------------------------------------------
# Emotion resonance sub-score:
# Based on Russell circumplex Euclidean distance
# 情感共鸣子分：基于环形情感模型的欧氏距离
# No emotion in query → neutral 0.5 (doesn't affect ranking)
# ---------------------------------------------------------
def calc_emotion_score(
    q_valence: Optional[float], q_arousal: Optional[float], meta: dict
) -> float:
    """
    Calculate emotion resonance score (0~1, closer = higher).
    计算情感共鸣度（0~1，越近越高）。
    """
    if q_valence is None or q_arousal is None:
        return 0.5  # No emotion coordinates → neutral / 无情感坐标时给中性分

    try:
        b_valence = float(meta.get("valence", _DEFAULT_VALENCE))
        b_arousal = float(meta.get("arousal", _DEFAULT_AROUSAL))
    except (ValueError, TypeError):
        return 0.5

    # Euclidean distance, max sqrt(2) ≈ 1.414
    dist = math.sqrt((q_valence - b_valence) ** 2 + (q_arousal - b_arousal) ** 2)
    return max(0.0, 1.0 - dist / EMOTION_MAX_DIST)


# ---------------------------------------------------------
# Time proximity sub-score:
# More recent activation → higher score
# 时间亲近子分：距上次激活越近分越高
# ---------------------------------------------------------
def calc_time_score(meta: dict) -> float:
    """
    Calculate time proximity score (0~1, more recent = higher).
    计算时间亲近度。
    """
    last_active_str = meta.get("last_active", meta.get("created", ""))
    try:
        # Legacy naive values were written as UTC; parse uniformly so the
        # subtraction below never mixes naive and aware datetimes.
        last_active = parse_iso_aware(str(last_active_str))
        days = max(0.0, (datetime.now(timezone.utc) - last_active).total_seconds() / 86400)
    except (ValueError, TypeError):
        days = TIME_FALLBACK_DAYS
    return math.exp(-TIME_DECAY_LAMBDA * days)


# ---------------------------------------------------------
# Touch frequency sub-score (iter 2.1)
# 触碰频率子分：被主动召回次数越多分越高
# ---------------------------------------------------------
def calc_touch_score(meta: dict) -> float:
    """
    Calculate touch frequency score (0~1).
    Normalizes activation_count over 10; capped at 1.0.
    计算触碰频率得分（0~1），以 10 次为上限归一化。
    """
    count = float(meta.get("activation_count") or 0)
    return min(count / TOUCH_NORMALIZE_CAP, 1.0)
