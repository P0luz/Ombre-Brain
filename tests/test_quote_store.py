"""引语归一化的边界行为。

这个模块的每条上限都是**防退化约束**——防止「记住几句重要的话」退化成「存原文」。
所以测试重点不是"能不能存进去"，是**超限时会不会被静默放过**。
"""

import pytest

from ombrebrain.storage.quote_store import (
    MAX_QUOTES,
    MAX_QUOTE_CHARS,
    normalize_quotes,
    quotes_from_metadata,
    render_quotes,
)


def test_empty_inputs_return_empty_list():
    for value in (None, "", []):
        assert normalize_quotes(value) == []


def test_plain_strings_are_accepted():
    assert normalize_quotes(["我不会走的"]) == [{"text": "我不会走的"}]


def test_optional_fields_kept_only_when_present():
    result = normalize_quotes([{"text": "我不会走的", "speaker": "她"}])
    assert result == [{"text": "我不会走的", "speaker": "她"}]
    # speaker/at 为空时不落进结果，避免 frontmatter 堆空值
    assert "at" not in result[0]


def test_order_is_preserved():
    """说话有先后，重排会改变意思。"""
    result = normalize_quotes(["先说的", "后说的"])
    assert [q["text"] for q in result] == ["先说的", "后说的"]


def test_duplicates_dropped_by_text_and_speaker():
    result = normalize_quotes(["一样的话", "一样的话"])
    assert len(result) == 1
    # 同一句话不同人说，是两条
    result = normalize_quotes([
        {"text": "我懂了", "speaker": "她"},
        {"text": "我懂了", "speaker": "我"},
    ])
    assert len(result) == 2


def test_too_many_quotes_is_rejected_not_truncated():
    """超量必须报错。静默取前 N 条 = 悄悄丢掉我说要记住的话。"""
    with pytest.raises(ValueError, match="最多"):
        normalize_quotes([f"第{i}句" for i in range(MAX_QUOTES + 1)])


def test_overlong_quote_is_rejected_not_truncated():
    """截断过的引语不是引语。这条是整个功能的底线。"""
    with pytest.raises(ValueError, match="最多"):
        normalize_quotes(["字" * (MAX_QUOTE_CHARS + 1)])


def test_boundary_lengths_are_accepted():
    assert len(normalize_quotes(["字" * MAX_QUOTE_CHARS])) == 1
    assert len(normalize_quotes([f"第{i}句" for i in range(MAX_QUOTES)])) == MAX_QUOTES


def test_empty_text_is_rejected():
    with pytest.raises(ValueError, match="非空"):
        normalize_quotes([{"text": "   "}])
    with pytest.raises(ValueError, match="非空"):
        normalize_quotes([""])


def test_non_list_and_bad_item_types_are_rejected():
    with pytest.raises(ValueError, match="必须是列表"):
        normalize_quotes({"text": "不是列表"})
    with pytest.raises(ValueError, match="字符串或对象"):
        normalize_quotes([123])


def test_speaker_and_at_are_bounded():
    result = normalize_quotes([{"text": "话", "speaker": "人" * 200, "at": "日" * 200}])
    assert len(result[0]["speaker"]) <= 40
    assert len(result[0]["at"]) <= 32


def test_quotes_from_metadata_never_raises():
    """读取路径不该因为一条写坏的引语而整体失败——记忆本身比引语重要。"""
    assert quotes_from_metadata(None) == []
    assert quotes_from_metadata({}) == []
    assert quotes_from_metadata({"quotes": "坏数据"}) == []
    assert quotes_from_metadata({"quotes": ["字" * 999]}) == []
    assert quotes_from_metadata({"quotes": ["好数据"]}) == [{"text": "好数据"}]


def test_bad_entry_does_not_wipe_the_good_ones():
    """逐条抢救，不是整体放弃。一条坏数据让整桶引语消失，那是把问题放大了。"""
    result = quotes_from_metadata({"quotes": ["好的一句", "字" * 999, 123, {"text": "另一句"}]})
    assert [q["text"] for q in result] == ["好的一句", "另一句"]


def test_oversized_stored_list_is_salvaged_not_dropped():
    """磁盘上的 frontmatter 可以被人手工编辑坏。写入拒绝、读取兜底，两者不冲突。"""
    stored = {"quotes": [f"第{i}句" for i in range(MAX_QUOTES + 3)]}
    result = quotes_from_metadata(stored)
    assert len(result) == MAX_QUOTES
    assert result[0]["text"] == "第0句"


def test_render_is_verbatim():
    """逐字返回，不摘要不改写——这是这个功能存在的全部理由。"""
    text = "我不会走的"
    rendered = render_quotes([{"text": text, "speaker": "她", "at": "2026-08-18"}])
    assert text in rendered
    assert "她" in rendered
    assert "2026-08-18" in rendered
    assert render_quotes([]) == ""
