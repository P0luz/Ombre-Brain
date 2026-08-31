from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from errors import ToolInputError
from ombrebrain.storage.source_store import (
    SourceStore,
    normalize_source_links,
    normalize_source_ranges,
    referenced_source_ids_from_markdown,
)
from tools.hold import dispatch as hold


def test_source_store_is_content_addressed_and_verifies_integrity(tmp_path):
    store = SourceStore(tmp_path)
    ref = store.put("第一行\n第二行\n第三行\n")
    assert store.put("第一行\n第二行\n第三行\n") == ref
    assert len(list((tmp_path / "_sources").glob("*.source"))) == 1
    assert store.read(ref) == "第一行\n第二行\n第三行\n"

    (tmp_path / "_sources" / f"{ref}.source").write_text("被篡改", encoding="utf-8")
    with pytest.raises(OSError, match="完整性"):
        store.read(ref)


def test_source_ranges_are_normalized_and_selected(tmp_path):
    store = SourceStore(tmp_path)
    ranges = normalize_source_ranges([[3, 3], [1, 2], [5, 5]])
    assert ranges == [[1, 3], [5, 5]]
    assert store.select_ranges("一\n二\n三\n四\n五\n", ranges) == "一\n二\n三\n五\n"
    with pytest.raises(ValueError, match="超出"):
        store.select_ranges("一\n二\n", [[2, 3]])


def test_source_links_reject_duplicate_bindings():
    ref = "src_" + "a" * 64
    duplicate = {"ref": ref, "ranges": [[1, 2]], "status": "active"}
    with pytest.raises(ValueError, match="重复绑定"):
        normalize_source_links([duplicate, dict(duplicate, status="detached")])


@pytest.mark.parametrize("invalid", [[[True, 1]], [[1.5, 2]], [["1", 2]]])
def test_source_ranges_reject_non_integer_line_numbers(invalid):
    with pytest.raises(ValueError, match="行号必须是整数"):
        normalize_source_ranges(invalid)


def test_source_store_falls_back_to_atomic_publish_without_hardlinks(
    tmp_path, monkeypatch
):
    store = SourceStore(tmp_path)

    def unsupported_link(*_args, **_kwargs):
        raise OSError(errno.EOPNOTSUPP, "hard links unsupported")

    monkeypatch.setattr(os, "link", unsupported_link)
    ref = store.put("NAS 上也必须原子发布")

    assert store.read(ref) == "NAS 上也必须原子发布"
    assert not list((tmp_path / "_sources").glob(".source-*"))


@pytest.mark.asyncio
async def test_hold_explicit_title_wins_over_model_suggestion(
    bucket_mgr, monkeypatch
):
    class Dehydrator:
        async def analyze(self, _content):
            return {
                "domain": ["恋爱"],
                "valence": 0.8,
                "arousal": 0.4,
                "tags": ["称呼"],
                "suggested_name": "直接确认关系",
            }

        def invalidate_cache(self, _content):
            return None

    class Decay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "dehydrator", Dehydrator())
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)

    result = await hold(
        content="她说 wife 喔，不是 girlfriend 喔。",
        title="wife",
    )
    bucket_id = result.split("→", 1)[1].split()[0]
    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["title"] == "wife"
    assert bucket["metadata"]["name"].endswith(" wife")


@pytest.mark.asyncio
async def test_hold_explicit_tags_replace_model_suggestions(
    bucket_mgr, monkeypatch
):
    class Dehydrator:
        async def analyze(self, _content):
            return {
                "domain": ["日常"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": ["模型标签"],
                "suggested_name": "模型标题",
                "importance": 7,
            }

        def invalidate_cache(self, _content):
            return None

    class Decay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "dehydrator", Dehydrator())
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)

    result = await hold(content="人工标签优先。", tags=["人工标签"])
    bucket_id = result.split("→", 1)[1].split()[0]
    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["tags"] == ["人工标签"]
    assert bucket["metadata"]["title"] == "模型标题"


@pytest.mark.asyncio
async def test_hold_explicit_domain_wins_over_model_suggestion(
    bucket_mgr, monkeypatch
):
    class Dehydrator:
        async def analyze(self, _content):
            return {
                "domain": ["模型域"],
                "valence": 0.5,
                "arousal": 0.3,
                "tags": [],
                "suggested_name": "模型标题",
            }

        def invalidate_cache(self, _content):
            return None

    class Decay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "dehydrator", Dehydrator())
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)

    result = await hold(content="人工 domain 优先。", domain="人工域")
    bucket_id = result.split("→", 1)[1].split()[0]
    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["domain"] == ["人工域"]


@pytest.mark.asyncio
async def test_hold_optional_source_content_uses_shared_source_layer(
    bucket_mgr, monkeypatch
):
    class Dehydrator:
        async def analyze(self, _content):
            return {
                "domain": ["旅行"],
                "valence": 0.8,
                "arousal": 0.5,
                "tags": ["京都"],
                "suggested_name": "京都计划",
            }

        def invalidate_cache(self, _content):
            return None

    class Decay:
        async def ensure_started(self):
            return None

    store = SourceStore(bucket_mgr.base_dir)
    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "dehydrator", Dehydrator())
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "source_store", store)
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)

    source = "第一行：讨论目的地\n第二行：决定去京都\n第三行：约定时间\n"
    existing_ref = store.put(source)
    result = await hold(
        content="我们决定下个月一起去京都。",
        title="京都计划",
        source_content=source,
    )
    bucket_id = result.split("→", 1)[1].split()[0]
    bucket = await bucket_mgr.get(bucket_id)
    refs = bucket["metadata"]["source_refs"]

    assert refs[0]["ref"] == existing_ref
    assert len(list((Path(bucket_mgr.base_dir) / "_sources").glob("*.source"))) == 1
    assert refs[0]["ranges"] == [[1, 3]]
    assert store.read(refs[0]["ref"]) == source

    # 工具层已删除，直接从存储层验证 event 范围仍选中正确的原文行
    event = store.select_ranges(store.read(refs[0]["ref"]), refs[0]["ranges"])
    assert "第一行：讨论目的地" in event
    assert "第三行：约定时间" in event


@pytest.mark.asyncio
async def test_hold_source_ranges_select_event_and_merge_appends_evidence(
    bucket_mgr, monkeypatch
):
    class Dehydrator:
        async def analyze(self, _content):
            return {
                "domain": ["旅行"],
                "valence": 0.8,
                "arousal": 0.5,
                "tags": [],
                "suggested_name": "去了京都",
            }

        def invalidate_cache(self, _content):
            return None

    class Decay:
        async def ensure_started(self):
            return None

    store = SourceStore(bucket_mgr.base_dir)
    monkeypatch.setattr(rt, "config", {"limits": {}, "merge_threshold": 75})
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "dehydrator", Dehydrator())
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "source_store", store)
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "fire_webhook", None)
    monkeypatch.setattr(rt, "mark_op", None)

    memory = "我们今天真的去了京都。"
    first = await hold(
        content=memory,
        title="去了京都",
        source_content="前情\n今天到了京都\n去了清水寺\n收尾\n",
        source_ranges=[[2, 3]],
    )
    bucket_id = first.split("→", 1)[1].split()[0]
    # 工具层已删除，直接从存储层验证 ranges 只选中声明的行
    first_bucket = await bucket_mgr.get(bucket_id)
    first_ref = first_bucket["metadata"]["source_refs"][0]
    event = store.select_ranges(store.read(first_ref["ref"]), first_ref["ranges"])
    assert "今天到了京都" in event
    assert "去了清水寺" in event
    assert "前情" not in event
    assert "收尾" not in event

    second = await hold(
        content=memory,
        title="去了京都",
        source_content="另一段独立原话",
    )
    assert second.startswith("合并→")
    bucket = await bucket_mgr.get(bucket_id)
    assert len(bucket["metadata"]["source_refs"]) == 2


@pytest.mark.asyncio
async def test_hold_source_ranges_require_source_content(bucket_mgr, monkeypatch):
    class Decay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "mark_op", None)

    with pytest.raises(ToolInputError, match="source_ranges 需要同时提供 source_content"):
        await hold(
            content="这条不应该写进去。",
            title="无原文",
            source_ranges=[[1, 1]],
        )
    assert await bucket_mgr.list_all() == []


def test_source_evidence_closure_unions_and_validates_both_metadata_fields():
    first = "src_" + "1" * 64
    second = "src_" + "2" * 64
    markdown = (
        "---\n"
        f"source_refs:\n  - ref: {first}\n    ranges: []\n"
        "source_links:\n"
        f"  - ref: {second}\n    ranges: []\n    status: detached\n"
        "---\nbody\n"
    )
    assert referenced_source_ids_from_markdown(markdown) == {first, second}

    malformed = markdown.replace(first, "not-a-source-ref")
    with pytest.raises(ValueError):
        referenced_source_ids_from_markdown(malformed)


@pytest.mark.asyncio
async def test_title_over_limit_is_rejected_before_hold_writes(bucket_mgr, monkeypatch):
    class Decay:
        async def ensure_started(self):
            return None

    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr)
    monkeypatch.setattr(rt, "decay_engine", Decay())
    monkeypatch.setattr(rt, "mark_op", None)

    with pytest.raises(ToolInputError, match="120"):
        await hold(content="不能半截标题", title="长" * 121)
    assert await bucket_mgr.list_all() == []


@pytest.mark.asyncio
async def test_bucket_manager_never_silently_truncates_explicit_title(bucket_mgr):
    valid_title = "标" * 120
    bucket_id = await bucket_mgr.create(content="正文", title=valid_title)
    assert (await bucket_mgr.get(bucket_id))["metadata"]["title"] == valid_title

    with pytest.raises(ValueError, match="120"):
        await bucket_mgr.update(bucket_id, title="越" * 121)
    assert (await bucket_mgr.get(bucket_id))["metadata"]["title"] == valid_title
