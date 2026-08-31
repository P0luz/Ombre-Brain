"""配额计数器同步回归测试 —— 按用户反馈的精确复现路径走。

反馈场景（v2.3.22，Render）：
1. pinned：取消钉选到 17 个后仍订不上新的，报「有 24 个 pin」
   → 旧根因：取消钉选后残留的 type=permanent 也被算进 pinned 配额。

当前实现的硬保证（本文件锁死，防止回退）：
- 配额计数每次实时从盘上数（无缓存计数器），trace 改完立即生效；
- pinned 只数 metadata.pinned，type=permanent 不占 pinned 配额。

注：importance≥9 硬配额/自动降级机制已按 rule.md §2 撤销（2026-08-11）——
稀缺性哲学改由 pinned(20)/anchor(24) 两个结构承担，importance 只是普通
评分字段，不再设配额。本文件原有围绕 `_HIGH_IMP_HARD_CAP` /
`count_high_importance` / `enforce_high_importance_quota` 的用例已整体移除。
"""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest

from errors import ToolInputError

import tools._runtime as rt
from tools._common import (
    check_protected_quota,
    count_pinned,
    count_protected,
    enforce_pinned_quota,
    merge_or_create,
)
from tools.trace.core import trace_core


class EchoDehydrator:
    async def dehydrate(self, content, meta=None):
        return content

    async def judge_same_event(self, *_args, **_kwargs):
        return {"same_event": True, "confidence": 0.99, "reason": "配额测试的合并前置"}


def install_runtime(bucket_mgr, limits=None):
    rt.config = {"surfacing": {}, "limits": limits or {}}
    rt.bucket_mgr = bucket_mgr
    rt.dehydrator = EchoDehydrator()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None


class StaticBucketManager:
    """Minimal counter fixture for legacy/imported physical row shapes."""

    def __init__(self, rows):
        self.rows = rows

    async def list_all(self, include_archive=False):
        assert include_archive is False
        return list(self.rows)


def _quota_row(
    bucket_id: str,
    *,
    importance=9,
    bucket_type="dynamic",
    pinned=False,
    protected=False,
    dont_surface=False,
):
    return {
        "id": bucket_id,
        "metadata": {
            "importance": importance,
            "type": bucket_type,
            "pinned": pinned,
            "protected": protected,
            "dont_surface": dont_surface,
        },
    }


# ------------------------------------------------------------
# ① pinned 配额：trace 解钉后必须立刻能钉新的
# ------------------------------------------------------------

@pytest.mark.asyncio
async def test_unpin_via_trace_frees_pinned_quota(bucket_mgr):
    # 上限设小（3）让测试轻量；语义与默认 20 一致
    install_runtime(bucket_mgr, limits={"max_pinned": 3})

    ids = []
    for i in range(3):
        ids.append(await bucket_mgr.create(content=f"核心准则 {i}", pinned=True))

    # 满额：钉新桶被拒（enforce 返回 False = 走普通桶）
    assert await count_pinned() == 3
    assert await enforce_pinned_quota(True) is False

    # 复现步骤：trace(bucket_id, pinned=0) 解钉一个
    await trace_core(ids[0], pinned=0, importance=7)

    # 计数必须实时下降，且立刻能钉新的——不允许残留旧计数
    assert await count_pinned() == 2
    assert await enforce_pinned_quota(True) is True


@pytest.mark.asyncio
async def test_trace_can_unpin_and_lower_importance_atomically(bucket_mgr):
    install_runtime(bucket_mgr)
    pinned_id = await bucket_mgr.create(content="lower while unpinning", pinned=True)

    result = await trace_core(pinned_id, pinned=0, importance=7)

    unpinned = await bucket_mgr.get(pinned_id)
    assert "pinned=False" in result
    assert "importance=7" in result
    assert unpinned["metadata"]["pinned"] is False
    assert unpinned["metadata"]["type"] == "dynamic"
    assert unpinned["metadata"]["importance"] == 7


@pytest.mark.asyncio
async def test_trace_rejects_unpin_without_same_call_importance(bucket_mgr):
    install_runtime(bucket_mgr)
    pinned_id = await bucket_mgr.create(content="must choose importance", pinned=True)

    with pytest.raises(ToolInputError) as excinfo:
        await trace_core(pinned_id, pinned=0)

    unchanged = await bucket_mgr.get(pinned_id)
    assert "importance" in str(excinfo.value)
    assert unchanged["metadata"]["pinned"] is True
    assert unchanged["metadata"]["type"] == "permanent"
    assert unchanged["metadata"]["importance"] == 10


@pytest.mark.asyncio
async def test_permanent_type_does_not_occupy_pinned_quota(bucket_mgr):
    """旧根因锁死：解钉后桶留在 permanent 类型/目录，不得再占 pinned 配额。

    （用户实际 17 个 pin 却被报 24：多出来的就是这类残留。）"""
    install_runtime(bucket_mgr, limits={"max_pinned": 3})

    # 2 个真 pinned + 2 个曾 pinned 后解钉的（type 仍是 permanent）
    await bucket_mgr.create(content="真钉 A", pinned=True)
    await bucket_mgr.create(content="真钉 B", pinned=True)
    for i in range(2):
        bid = await bucket_mgr.create(content=f"曾钉 {i}", pinned=True)
        await trace_core(bid, pinned=0, importance=7)

    # 只数 metadata.pinned=True 的：2，不是 4
    assert await count_pinned() == 2
    # 2 < 3 → 还能钉
    assert await enforce_pinned_quota(True) is True


@pytest.mark.asyncio
async def test_pinned_counter_normalizes_booleans_and_logical_ids():
    pinned = _quota_row("pinned", pinned=True, importance=10)
    quoted_false = _quota_row(
        "quoted-false", pinned="false", importance=10
    )
    archived = _quota_row("archived", pinned=True, importance=10)
    archived["metadata"]["type"] = "archived"
    install_runtime(
        StaticBucketManager(
            [pinned, pinned, quoted_false, archived]
        )
    )

    assert await count_pinned() == 1


@pytest.mark.asyncio
async def test_active_protected_uses_an_independent_configured_quota(bucket_mgr):
    install_runtime(bucket_mgr, limits={"max_pinned": 1, "max_protected": 1})
    await bucket_mgr.create(content="ordinary pinned slot", pinned=True)
    await bucket_mgr.create(
        content="the sole protected slot",
        protected=True,
        bucket_type="dynamic",
    )
    await bucket_mgr.create(
        content="unprotected permanent does not use protected quota",
        bucket_type="permanent",
    )

    assert await count_pinned() == 1
    assert await count_protected() == 1
    assert await check_protected_quota() is not None

    candidate_id = await bucket_mgr.create(
        content="protected quota overflow candidate",
        importance=5,
    )
    with pytest.raises(ToolInputError) as excinfo:
        await trace_core(candidate_id, protected=1)
    candidate = await bucket_mgr.get(candidate_id)

    assert "protected 桶已达上限" in str(excinfo.value)
    assert candidate["metadata"].get("protected", False) is False
    assert candidate["metadata"]["importance"] == 5
    assert await count_protected() == 1


@pytest.mark.asyncio
async def test_restore_archived_protected_rejects_when_quota_is_full(bucket_mgr):
    install_runtime(bucket_mgr, limits={"max_protected": 1})
    archived_id = await bucket_mgr.create(
        content="archived protected memory",
        protected=True,
    )
    assert await bucket_mgr.archive(archived_id) is True
    assert await count_protected() == 0

    await bucket_mgr.create(content="active protected slot", protected=True)
    assert await count_protected() == 1

    result = await trace_core(archived_id, restore=True)
    archived = await bucket_mgr.get_including_archive(archived_id)

    assert "protected 桶已达上限" in result
    assert archived["metadata"]["type"] == "archived"
    assert archived["metadata"]["protected"] is True
    assert await count_protected() == 1


@pytest.mark.asyncio
async def test_trace_restore_dirty_protected_anchor_requires_atomic_unprotect(
    bucket_mgr,
):
    install_runtime(bucket_mgr)

    archived_id = await bucket_mgr.create(
        content="historical protected anchor conflict",
        protected=True,
    )
    active = await bucket_mgr.get(archived_id)
    active_path = Path(active["path"])
    dirty_post = frontmatter.load(active_path)
    dirty_post["anchor"] = True
    active_path.write_text(frontmatter.dumps(dirty_post), encoding="utf-8")
    assert await bucket_mgr.archive(archived_id) is True

    with pytest.raises(ToolInputError) as 冲突:
        await trace_core(archived_id, restore=True)
    with pytest.raises(ToolInputError) as 缺importance:
        await trace_core(
            archived_id,
            restore=True,
            protected=0,
        )
    unchanged = await bucket_mgr.get_including_archive(archived_id)

    assert "restore=True, protected=0" in str(冲突.value)
    assert "importance=1..10" in str(冲突.value)
    assert "importance=1..10" in str(缺importance.value)
    assert unchanged["metadata"]["type"] == "archived"
    assert unchanged["metadata"]["protected"] is True
    assert unchanged["metadata"]["anchor"] is True

    result = await trace_core(
        archived_id,
        restore=True,
        protected=0,
        importance=9,
    )
    restored = await bucket_mgr.get(archived_id)

    assert result == f"已重新回忆并恢复记忆桶: {archived_id}"
    assert restored["metadata"]["type"] == "dynamic"
    assert restored["metadata"].get("protected", False) is False
    assert restored["metadata"]["pinned"] is False
    assert restored["metadata"]["anchor"] is True
    assert restored["metadata"]["importance"] == 9


@pytest.mark.asyncio
async def test_concurrent_merge_promotions_preserve_both_events_and_one_slot(
    bucket_mgr,
    monkeypatch,
):
    install_runtime(bucket_mgr)
    target_id = await bucket_mgr.create(content="merge base", importance=5)

    async def find_target(*_args, **_kwargs):
        row = await bucket_mgr.get(target_id)
        row["score"] = 100.0
        return [row]

    monkeypatch.setattr(bucket_mgr, "search", find_target)

    async def merge_event(text):
        return await merge_or_create(
            content=text,
            tags=[],
            importance=9,
            domain=[],
            valence=0.5,
            arousal=0.3,
            raw_merge=True,
            source_tool="hold",
        )

    results = await asyncio.gather(
        merge_event("concurrent event A"),
        merge_event("concurrent event B"),
    )

    persisted = await bucket_mgr.get(target_id)
    assert all(result[:2] == (target_id, True) for result in results)
    assert "concurrent event A" in persisted["content"]
    assert "concurrent event B" in persisted["content"]
    assert persisted["metadata"]["importance"] == 9
