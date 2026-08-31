from pathlib import Path
from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from ombrebrain.policy.formal_invariants import FormalInvariantChecker
from tools.trace.core import trace_core


def _install_trace_runtime(bucket_mgr) -> None:
    rt.config = {"limits": {}}
    rt.bucket_mgr = bucket_mgr
    rt.logger = MagicMock()
    rt.mark_op = None
    rt.v3_runtime = None


@pytest.mark.asyncio
async def test_only_creation_marked_test_bucket_can_be_hard_deleted(bucket_mgr):
    real_id = await bucket_mgr.create(content="a real memory", domain=["life"])
    test_id = await bucket_mgr.create(
        content="synthetic memory for a test",
        domain=["test"],
        source_tool="hold",
        test_data=True,
    )

    test_bucket = await bucket_mgr.get(test_id)
    assert test_bucket["metadata"]["provenance"] == {
        "kind": "test",
        "created_by": "hold",
        "erasable": True,
    }

    refused = await bucket_mgr.hard_delete_test_bucket(real_id, reason="should fail")
    assert refused == {"ok": False, "error": "not_erasable_test_data"}
    assert await bucket_mgr.get(real_id) is not None

    erased_path = Path(test_bucket["path"])
    erased = await bucket_mgr.hard_delete_test_bucket(test_id, reason="test cleanup")
    assert erased == {"ok": True, "deleted": test_id}
    assert not erased_path.exists()
    assert await bucket_mgr.get(test_id) is None


@pytest.mark.asyncio
async def test_trace_hard_delete_refuses_normal_plan_without_archiving(bucket_mgr):
    plan_id = await bucket_mgr.create(
        content="a real plan that must remain recoverable",
        domain=["plan"],
        bucket_type="plan",
        source_tool="plan",
    )
    before = await bucket_mgr.get(plan_id)
    plan_path = Path(before["path"])
    _install_trace_runtime(bucket_mgr)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_core(
            plan_id,
            hard_delete=True,
            delete_reason="reported cleanup request",
        )
    result = str(excinfo.value)

    after = await bucket_mgr.get(plan_id)
    assert "普通记忆桶（包括 plan）不可被 trace 物理删除" in result
    assert "本次未删除、未归档" in result
    assert after is not None
    assert "deleted_at" not in after["metadata"]
    assert plan_path.exists()
    assert not any(
        event["event_type"] == "TraceHardDeleted"
        and event["trace_id"] == plan_id
        for event in bucket_mgr.ledger_mirror.iter_events()
    )


@pytest.mark.asyncio
async def test_test_data_cleanup_requires_reason_and_rejects_conflicting_modes(
    bucket_mgr,
):
    test_id = await bucket_mgr.create(
        content="synthetic gateway test payload",
        domain=["test"],
        source_tool="hold",
        test_data=True,
    )
    test_bucket = await bucket_mgr.get(test_id)
    test_path = Path(test_bucket["path"])
    _install_trace_runtime(bucket_mgr)

    missing = await bucket_mgr.hard_delete_test_bucket(test_id, reason="   ")
    assert missing == {"ok": False, "error": "missing_delete_reason"}
    assert test_path.exists()

    with pytest.raises(ToolInputError) as 缺理由:
        await trace_core(test_id, hard_delete=True)
    assert "必须提供非空 delete_reason" in str(缺理由.value)
    assert test_path.exists()

    with pytest.raises(ToolInputError) as 理由过长:
        await trace_core(
            test_id,
            hard_delete=True,
            delete_reason="x" * 501,
        )
    assert "不能超过 500 个字符" in str(理由过长.value)
    assert test_path.exists()

    with pytest.raises(ToolInputError) as 冲突:
        await trace_core(
            test_id,
            delete=True,
            hard_delete=True,
            delete_reason="gateway test cleanup",
        )
    assert "参数冲突" in str(冲突.value)
    assert "未删除、未归档" in str(冲突.value)
    assert test_path.exists()
    assert await bucket_mgr.get(test_id) is not None

    deleted = await trace_core(
        test_id,
        hard_delete=True,
        delete_reason="gateway test cleanup",
    )
    assert deleted == f"已永久删除测试桶: {test_id}"
    assert not test_path.exists()

    events = list(bucket_mgr.ledger_mirror.iter_events())
    delete_event = next(
        event for event in events if event["event_type"] == "TraceHardDeleted"
    )
    assert delete_event["payload"]["reason"] == "gateway test cleanup"
    assert FormalInvariantChecker.default().evaluate_ledger(events).ok is True


def test_dashboard_separates_normal_batch_actions_from_developer_erasure():
    text = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    assert "全选当前页" in text
    assert "/api/buckets/batch" in text
    assert "body.developer-mode .developer-only" in text
    assert "/api/developer/buckets/hard-delete" in text
    assert "DELETE TEST DATA" in text


def test_developer_only_hard_delete_button_has_no_inline_display_override():
    """一个 <button class="developer-only" style="display:...">.

    CSS 规则 `.developer-only { display:none; } body.developer-mode
    .developer-only { display:inline-flex; }` 靠 class 选择器控制显隐，但
    行内 style 的优先级永远高于 class 选择器——只要按钮自己的 style 属性里
    出现 display，就会不分开发者模式是否开启、永远覆盖那条 class 规则，
    等于把「永久删除测试桶」这种危险操作暴露给所有用户。这里直接检查按钮
    自身的 style 属性，防止未来的样式改动（比如给图标按钮统一加
    display:inline-flex）不小心又把它带回来。
    """
    import re

    text = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    match = re.search(
        r'<button class="developer-only"[^>]*onclick="hardDeleteSelectedTests\(\)"[^>]*>',
        text,
    )
    assert match, "hard-delete button markup not found"
    tag = match.group(0)
    style_match = re.search(r'style="([^"]*)"', tag)
    style = style_match.group(1) if style_match else ""
    assert "display" not in style, (
        f"inline display on the developer-only button defeats .developer-only "
        f"{{ display:none }} and always shows the destructive action: {style!r}"
    )


def test_dashboard_chick_is_draggable_and_remembers_a_safe_position():
    text = Path("frontend/dashboard.html").read_text(encoding="utf-8")
    assert "installPetDrag" in text
    assert "setPointerCapture" in text
    assert "ombreChickPosition" in text
    assert "clampPetPosition" in text
    assert "可恶的人类！" in text
    assert "touch-action: none" in text
    assert "dropRemark" in text
    assert "这位置以前是我的。" in text
    assert "你找什么？我帮你啄。" in text
    assert "你怎么还没睡？" in text
    assert "天旋地转……你礼貌吗？" in text
    assert "play-dead" in text
    assert "sleeping" in text
    assert "我会替你放远一点。" in text
    assert "护目镜戴好，开始实验。" in text
    assert "假的，清理完了。" in text
    assert "这条不行。" in text
    assert "……算了，原谅你。" in text


def test_dashboard_chick_can_be_tickled_and_reacts_to_real_system_states():
    text = Path("frontend/dashboard.html").read_text(encoding="utf-8")

    assert "TICKLE_LINES" in text
    assert "canvasX - spriteX" in text
    assert "lastPetAction==='tickle'" in text
    assert "别碰我。" in text
    assert "下次再碰我就把你的桶全归档。" in text
    assert "别挠了，痒。" in text
    assert "chickReactForApiProblem" in text
    assert "最近发现你有429的问题" in text
    assert "你的key坏了" in text
    assert "你喂了我一段话但我没消化成功" in text
    assert "正在重新理解所有的记忆" in text
    assert "这里什么都没有" in text
    assert "找不到我自己" in text
    assert "正在忘记一些不重要的事" in text
