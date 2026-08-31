"""自动建立桶间关系 —— 门槛与上限。

阈值不是拍脑袋定的：2026-08-18 对 917 桶真实记忆做过全量相似度扫描，
原定的 related_to=0.65 会建出 7,620 条关系、47.8% 的桶撞上每桶 8 条上限。
**一旦大面积撞上限，阈值就形同虚设**——决定挂哪几条的不再是"相关不相关"，
而是"截断时谁排前八"。上调到 0.72 后每桶中位 2 条，只有 3.5% 需要截断。

所以这里测的重点是**门槛真的在挡**，而不是"能不能建出关系"。
"""

import pytest

from ombrebrain.storage.relation_store import (
    AUTO_CONTINUATION_MAX_HOURS,
    AUTO_MAX_LINKS_PER_BUCKET,
    AUTO_RELATED_MIN_SCORE,
    AUTO_SAME_EVENT_MAX_HOURS,
    infer_auto_relation_type,
    merge_auto_links,
    normalize_relation_links,
)


def _auto(target: str, score: float, rel_type: str = "related_to") -> dict:
    return {
        "target_bucket_id": target,
        "type": rel_type,
        "label": "",
        "status": "active",
        "auto": True,
        "score": score,
    }


# --------------------------------------------------------------
# 门槛
# --------------------------------------------------------------


def test_below_threshold_builds_nothing():
    """低于 0.72 的一律不建，哪怕时间挨得很近。

    时间近不能补偿语义不相关——否则同一天写的所有记忆都会连成一片。
    """
    assert infer_auto_relation_type(0.71, 0.5) is None
    assert infer_auto_relation_type(0.5, 0.1) is None
    assert infer_auto_relation_type(0.0, 0.0) is None


def test_threshold_boundary_is_inclusive():
    assert infer_auto_relation_type(AUTO_RELATED_MIN_SCORE, None) == "related_to"
    assert infer_auto_relation_type(AUTO_RELATED_MIN_SCORE - 0.001, None) is None


def test_same_event_needs_both_high_score_and_time_proximity():
    """same_event = 高相似 + 时间邻近，缺一不可。"""
    assert infer_auto_relation_type(0.9, 1.0) == "same_event"
    # 相似度够但隔了太久：降级成 continuation，不是 same_event
    assert infer_auto_relation_type(0.9, AUTO_SAME_EVENT_MAX_HOURS + 1) == "continuation_of"
    # 时间近但相似度不够
    assert infer_auto_relation_type(0.78, 1.0) == "continuation_of"


def test_continuation_falls_back_to_related_beyond_window():
    assert infer_auto_relation_type(0.78, AUTO_CONTINUATION_MAX_HOURS - 1) == "continuation_of"
    assert infer_auto_relation_type(0.78, AUTO_CONTINUATION_MAX_HOURS + 1) == "related_to"


def test_unknown_time_never_builds_time_based_types():
    """缺 created 时不猜时间关系，降级到 related_to。

    猜错的 same_event 比没有 same_event 更糟——它会让两件不同的事在
    hint 里显示成同一件。
    """
    assert infer_auto_relation_type(0.95, None) == "related_to"
    assert infer_auto_relation_type(0.8, None) == "related_to"


def test_causal_types_are_never_inferred():
    """因果需要语义理解，规则判不了，宁可不建也不能瞎建。"""
    for score in (0.72, 0.8, 0.9, 0.99):
        for hours in (0.0, 1.0, 100.0, None):
            assert infer_auto_relation_type(score, hours) not in {
                "caused_by",
                "causes",
                "custom",
            }


# --------------------------------------------------------------
# 每桶上限
# --------------------------------------------------------------


def test_cap_keeps_highest_scoring_links():
    existing = []
    incoming = [_auto(f"b{i}", 0.72 + i * 0.01) for i in range(12)]
    merged = merge_auto_links(existing, incoming)

    assert len(merged) == AUTO_MAX_LINKS_PER_BUCKET
    scores = [link["score"] for link in merged]
    assert scores == sorted(scores, reverse=True)
    # 被丢掉的必须是分数最低的那几条
    assert min(scores) > 0.72


def test_manual_links_are_never_evicted_by_auto_ones():
    """手动关系一条都不动。

    3.0.0 之后已经没有手动入口，但存量数据还在——它们是人当初明确
    建立的，不该被后来的自动推断挤掉。
    """
    manual = [
        {
            "target_bucket_id": "manual-1",
            "type": "caused_by",
            "label": "",
            "status": "active",
        }
    ]
    incoming = [_auto(f"auto{i}", 0.9) for i in range(12)]
    merged = merge_auto_links(manual, incoming)

    assert merged[0]["target_bucket_id"] == "manual-1"
    assert len(merged) == AUTO_MAX_LINKS_PER_BUCKET
    # 手动那条占掉一个名额，自动的只能有 7 条
    assert sum(1 for link in merged if link.get("auto")) == AUTO_MAX_LINKS_PER_BUCKET - 1


def test_existing_target_is_not_duplicated_or_rewritten():
    existing = [_auto("b1", 0.99, "same_event")]
    merged = merge_auto_links(existing, [_auto("b1", 0.75, "related_to")])

    assert len(merged) == 1
    # 已有关系的类型不被后来的推断改写
    assert merged[0]["type"] == "same_event"


# --------------------------------------------------------------
# 落库形态
# --------------------------------------------------------------


def test_auto_marker_and_score_survive_normalization():
    """auto 标记要能落进 frontmatter，否则无法区分和整体回滚。"""
    normalized = normalize_relation_links([_auto("b1", 0.8123456)])

    assert normalized[0]["auto"] is True
    assert normalized[0]["score"] == pytest.approx(0.8123, abs=1e-4)


def test_manual_links_do_not_gain_auto_marker():
    normalized = normalize_relation_links(
        [{"target_bucket_id": "b1", "type": "related_to", "label": "", "status": "active"}]
    )

    assert "auto" not in normalized[0]
    assert "score" not in normalized[0]
