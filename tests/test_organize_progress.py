"""整理进度统计（_organize_progress）：digested = 已整理，只统计活跃记忆桶。"""

from tools.anchor.core import _organize_progress


def _b(bid, btype="dynamic", digested=False):
    meta = {"type": btype}
    if digested:
        meta["digested"] = True
    return {"id": bid, "metadata": meta}


def test_counts_digested_and_excludes_layered_types():
    buckets = [
        _b("a", digested=True),                 # 动态，已整理
        _b("b"),                                # 动态，未整理
        _b("c", "permanent"),                   # 固化，未整理
        _b("d", "feel", digested=True),         # feel 分层桶，不计数
        _b("e", "plan"),                        # plan 分层桶，不计数
        _b("f", "letter", digested=True),       # letter 分层桶，不计数
        _b("g", "archived"),                    # 归档，不计数
    ]
    line = _organize_progress(buckets)
    assert "已整理 1" in line
    assert "未整理 2" in line


def test_empty_returns_empty_string():
    assert _organize_progress([]) == ""
    assert _organize_progress([_b("x", "feel")]) == ""  # 全是分层桶 → 无记忆桶


def test_full_progress_bar_when_all_digested():
    buckets = [_b("a", digested=True), _b("b", digested=True)]
    line = _organize_progress(buckets)
    assert "已整理 2" in line
    assert "未整理 0" in line
    assert "░" not in line.split("整理进度: ")[1].split(" 已整理")[0]
