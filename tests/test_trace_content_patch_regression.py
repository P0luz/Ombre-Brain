import asyncio
from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

from tools import _runtime as rt
from tools.trace.core import trace_core


@pytest.fixture
def trace_runtime(monkeypatch, bucket_mgr, test_config):
    monkeypatch.setattr(rt, "config", test_config, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "fire_webhook", None, raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    monkeypatch.setattr(rt, "v3_runtime", None, raising=False)
    return bucket_mgr


@pytest.mark.asyncio
async def test_trace_patch_replaces_unique_tail_of_long_pinned_bucket(
    trace_runtime,
    monkeypatch,
):
    manager = trace_runtime
    filler = "0123456789abcdef long bucket filler line\n" * 900
    old_str = "旧片段第一行：保留标点，emoji🙂。\n旧片段第二行\tMarkdown **原样**"
    new_str = "新片段第一行：保留标点，emoji🙂。\n新片段第二行\tMarkdown **原样**"
    suffix = "\n尾声也必须逐字保留。"
    original = filler + old_str + suffix
    assert 32 * 1024 < len(original.encode("utf-8")) < 50 * 1024

    bucket_id = await manager.create(
        content=original,
        importance=10,
        pinned=True,
        domain=["长期记忆"],
    )
    indexed = []

    async def capture_embedding(target_id, content):
        indexed.append((target_id, content))
        return True

    monkeypatch.setattr(
        manager.embedding_engine,
        "generate_and_store",
        capture_embedding,
    )

    result = await trace_core(bucket_id, old_str=old_str, new_str=new_str)
    bucket = await manager.get(bucket_id)

    assert "content=已局部替换" in result
    assert bucket is not None
    assert bucket["content"] == filler + new_str + suffix
    assert bucket["metadata"]["pinned"] is True
    assert bucket["metadata"]["importance"] == 10
    assert indexed == [(bucket_id, filler + new_str + suffix)]


@pytest.mark.asyncio
async def test_trace_patch_zero_or_multiple_matches_never_writes(trace_runtime):
    manager = trace_runtime
    original = "开头\n重复片段\n中间\n重复片段\n结尾"
    bucket_id = await manager.create(content=original)

    with pytest.raises(ToolInputError) as 找不到:
        await trace_core(
            bucket_id,
            old_str="不存在的连续原文",
            new_str="不应写入",
            importance=8,
        )
    with pytest.raises(ToolInputError) as 有歧义:
        await trace_core(
            bucket_id,
            old_str="重复片段",
            new_str="不应写入",
        )
    bucket = await manager.get(bucket_id)

    assert "未找到 old_str" in str(找不到.value)
    assert "至少出现 2 次" in str(有歧义.value)
    assert "没有任何字段需要修改" not in str(找不到.value)
    assert bucket is not None
    assert bucket["content"] == original
    assert bucket["metadata"]["importance"] == 5

    overlap_id = await manager.create(content="aaa")
    with pytest.raises(ToolInputError) as 重叠:
        await trace_core(
            overlap_id,
            old_str="aa",
            new_str="不应写入",
        )
    overlap_bucket = await manager.get(overlap_id)

    assert "至少出现 2 次" in str(重叠.value)
    assert overlap_bucket is not None
    assert overlap_bucket["content"] == "aaa"


@pytest.mark.asyncio
async def test_trace_patch_refreshes_appended_meaning_embedding(
    trace_runtime,
    monkeypatch,
):
    manager = trace_runtime
    bucket_id = await manager.create(content="old body")
    indexed_meaning = []

    async def capture_meaning(target_id, meaning):
        indexed_meaning.append((target_id, meaning))
        return True

    monkeypatch.setattr(
        manager.embedding_engine,
        "generate_and_store_meaning",
        capture_meaning,
        raising=False,
    )

    result = await trace_core(
        bucket_id,
        old_str="old body",
        new_str="new body",
        meaning_append="new perspective",
    )
    bucket = await manager.get(bucket_id)

    assert "content=已局部替换" in result
    assert bucket["metadata"]["meaning"] == ["new perspective"]
    assert indexed_meaning == [(bucket_id, "new perspective")]


@pytest.mark.asyncio
async def test_trace_patch_supports_deletion_and_rejects_invalid_argument_pairs(
    trace_runtime,
):
    manager = trace_runtime
    bucket_id = await manager.create(content="保留前文\n删除这一段\n保留后文")

    deleted = await trace_core(
        bucket_id,
        old_str="删除这一段\n",
        new_str="",
    )
    bucket = await manager.get(bucket_id)
    assert "content=已局部替换" in deleted
    assert bucket is not None
    assert bucket["content"] == "保留前文\n保留后文"

    whole_bucket_id = await manager.create(content="仅此正文")
    with pytest.raises(ToolInputError) as 清空:
        await trace_core(
            whole_bucket_id,
            old_str="仅此正文",
            new_str="",
        )
    whole_bucket = await manager.get(whole_bucket_id)

    assert "正文不能为空" in str(清空.value)
    assert whole_bucket is not None
    assert whole_bucket["content"] == "仅此正文"

    with pytest.raises(ToolInputError) as 净化后为空:
        await trace_core(
            whole_bucket_id,
            old_str="仅此正文",
            new_str="\x00",
        )
    whole_bucket = await manager.get(whole_bucket_id)

    assert "正文不能为空" in str(净化后为空.value)
    assert whole_bucket is not None
    assert whole_bucket["content"] == "仅此正文"

    with pytest.raises(ToolInputError, match="必须同时提供 old_str 和 new_str"):
        await trace_core(bucket_id, old_str="保留前文")
    with pytest.raises(ToolInputError, match="必须同时提供 old_str 和 new_str"):
        await trace_core(bucket_id, new_str="新前文")
    with pytest.raises(ToolInputError, match="不能同时使用 content"):
        await trace_core(
            bucket_id,
            content="完整替换",
            old_str="保留前文",
            new_str="新前文",
        )
    with pytest.raises(ToolInputError, match="完全相同"):
        await trace_core(
            bucket_id,
            old_str="保留前文",
            new_str="保留前文",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delete_kwargs", "test_data"),
    [
        ({"delete": True}, False),
        (
            {
                "hard_delete": True,
                "delete_reason": "verify patch/delete conflict is non-mutating",
            },
            True,
        ),
    ],
)
async def test_trace_patch_cannot_be_combined_with_delete_or_hard_delete(
    trace_runtime,
    delete_kwargs,
    test_data,
):
    manager = trace_runtime
    original = "必须保留的正文"
    bucket_id = await manager.create(content=original, test_data=test_data)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_core(
            bucket_id,
            old_str="必须保留",
            new_str="不应替换",
            **delete_kwargs,
        )
    bucket = await manager.get(bucket_id)

    assert "参数冲突" in str(excinfo.value)
    assert "未修改、未删除、未归档" in str(excinfo.value)
    assert bucket is not None
    assert bucket["content"] == original


@pytest.mark.asyncio
async def test_trace_patch_rejects_oversized_final_content_without_data_loss(
    trace_runtime,
):
    manager = trace_runtime
    old_str = "UNIQUE-TARGET"
    original = "a" * 50_000 + old_str
    bucket_id = await manager.create(content=original)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_core(
            bucket_id,
            old_str=old_str,
            new_str="b" * 2_000,
        )
    bucket = await manager.get(bucket_id)

    assert "内容过大" in str(excinfo.value)
    assert bucket is not None
    assert bucket["content"] == original


@pytest.mark.asyncio
async def test_concurrent_trace_patches_preserve_both_disjoint_edits(trace_runtime):
    manager = trace_runtime
    bucket_id = await manager.create(
        content="开头\n第一处旧文本\n中间\n第二处旧文本\n结尾"
    )

    results = await asyncio.gather(
        trace_core(bucket_id, old_str="第一处旧文本", new_str="第一处新文本"),
        trace_core(bucket_id, old_str="第二处旧文本", new_str="第二处新文本"),
    )
    bucket = await manager.get(bucket_id)

    assert all("content=已局部替换" in result for result in results)
    assert bucket is not None
    assert bucket["content"] == "开头\n第一处新文本\n中间\n第二处新文本\n结尾"


@pytest.mark.asyncio
async def test_concurrent_plan_patches_append_both_change_log_entries(
    trace_runtime,
    monkeypatch,
):
    manager = trace_runtime
    bucket_id = await manager.create(
        content="第一处旧文本\n第二处旧文本",
        bucket_type="plan",
        weight=0.7,
    )
    original_get = manager.get
    before = await original_get(bucket_id)
    assert before is not None
    initial_history = list(before["metadata"].get("change_log") or [])
    both_snapshots_read = asyncio.Event()
    readers = 0

    async def get_after_both_traces_have_the_same_snapshot(target_id):
        nonlocal readers
        snapshot = await original_get(target_id)
        readers += 1
        if readers == 2:
            both_snapshots_read.set()
        await both_snapshots_read.wait()
        return snapshot

    monkeypatch.setattr(manager, "get", get_after_both_traces_have_the_same_snapshot)

    results = await asyncio.gather(
        trace_core(bucket_id, old_str="第一处旧文本", new_str="第一处新文本"),
        trace_core(bucket_id, old_str="第二处旧文本", new_str="第二处新文本"),
    )
    bucket = await original_get(bucket_id)

    assert all("content=已局部替换" in result for result in results)
    assert bucket is not None
    assert bucket["content"] == "第一处新文本\n第二处新文本"
    appended = bucket["metadata"]["change_log"][len(initial_history):]
    assert [entry["action"] for entry in appended] == ["edit", "edit"]


@pytest.mark.asyncio
async def test_trace_patch_uses_latest_content_read_inside_bucket_lock(
    trace_runtime,
    monkeypatch,
):
    manager = trace_runtime
    bucket_id = await manager.create(content="目标旧文本\n原始结尾")
    original_get = manager.get
    first_read = True

    async def get_with_concurrent_write(target_id):
        nonlocal first_read
        snapshot = await original_get(target_id)
        if first_read:
            first_read = False
            assert snapshot is not None
            await manager.update(
                target_id,
                content=snapshot["content"] + "\n并发追加内容",
            )
        return snapshot

    monkeypatch.setattr(manager, "get", get_with_concurrent_write)

    result = await trace_core(
        bucket_id,
        old_str="目标旧文本",
        new_str="目标新文本",
    )
    bucket = await original_get(bucket_id)

    assert "content=已局部替换" in result
    assert bucket is not None
    assert bucket["content"] == "目标新文本\n原始结尾\n并发追加内容"


@pytest.mark.asyncio
async def test_trace_patch_records_plan_edit_change_log(trace_runtime):
    manager = trace_runtime
    bucket_id = await manager.create(
        content="计划旧正文",
        bucket_type="plan",
        weight=0.7,
    )
    await manager.update(
        bucket_id,
        resolution_suggested={
            "reason": "旧正文可能已完成",
            "ts": "2026-08-12T12:00:00",
        },
    )
    before = await manager.get(bucket_id)
    assert before is not None
    assert "resolution_suggested" in before["metadata"]

    result = await trace_core(
        bucket_id,
        old_str="旧正文",
        new_str="新正文",
    )
    bucket = await manager.get(bucket_id)

    assert "content=已局部替换" in result
    assert bucket is not None
    assert bucket["content"] == "计划新正文"
    assert "resolution_suggested" not in bucket["metadata"]
    assert bucket["metadata"]["change_log"][-1]["action"] == "edit"


@pytest.mark.asyncio
async def test_trace_plan_status_change_records_actor(trace_runtime):
    manager = trace_runtime
    bucket_id = await manager.create(
        content="待完成计划",
        bucket_type="plan",
        weight=0.7,
    )
    await manager.update(
        bucket_id,
        status="active",
        change_log=[],
        resolution_suggested={
            "reason": "计划可能已完成",
            "ts": "2026-08-12T12:00:00",
        },
    )
    before = await manager.get(bucket_id)
    assert before is not None
    assert "resolution_suggested" in before["metadata"]

    result = await trace_core(bucket_id, status="resolved")
    bucket = await manager.get(bucket_id)

    assert "status=resolved" in result
    assert bucket is not None
    assert "resolution_suggested" not in bucket["metadata"]
    entry = bucket["metadata"]["change_log"][-1]
    assert entry["action"] == "status"
    assert entry["from"] == "active"
    assert entry["to"] == "resolved"
    assert entry["by"] == "trace"
    assert entry["ts"]


@pytest.mark.asyncio
async def test_trace_plan_patch_and_status_change_records_actor(trace_runtime):
    manager = trace_runtime
    bucket_id = await manager.create(
        content="旧计划正文",
        bucket_type="plan",
        weight=0.7,
    )
    await manager.update(bucket_id, status="active", change_log=[])

    result = await trace_core(
        bucket_id,
        old_str="旧计划",
        new_str="新计划",
        status="resolved",
    )
    bucket = await manager.get(bucket_id)

    assert "content=已局部替换" in result
    assert bucket is not None
    assert bucket["content"] == "新计划正文"
    status_entry, edit_entry = bucket["metadata"]["change_log"][-2:]
    assert status_entry["action"] == "status"
    assert status_entry["from"] == "active"
    assert status_entry["to"] == "resolved"
    assert status_entry["by"] == "llm"
    assert status_entry["ts"]
    assert edit_entry["action"] == "edit"
    assert edit_entry["by"] == "llm"
