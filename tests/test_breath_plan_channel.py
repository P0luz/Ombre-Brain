"""breath(domain="plan") 通道 —— plan 正文的唯一读取入口。

背景（3.0.0 修复的 bug）：plan 桶被排除在普通浮现之外，而 `domain` 参数只在
catalog 模式和「有 query 的检索模式」里生效。`breath_advanced(domain="plan")`
不带 query 时会直接落到浮现模式，返回权重最高的桶 + 置顶核心准则——
调用方拿到的是核心准则，不是 plan。加上 dream 末尾的 plan 段可能因总预算
降级成只报条数，plan 的正文一度没有任何读取入口。
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath import dispatch


class _NoopDecay:
    is_running = True

    async def ensure_started(self):
        return None


class _ExplodingDehydrator:
    async def dehydrate(self, content, meta=None):
        raise AssertionError("plan 通道不得调用 LLM")


class _DisabledEmbedding:
    enabled = False


def _install_runtime(bucket_mgr):
    rt.config = {"surfacing": {}}
    rt.bucket_mgr = bucket_mgr
    rt.decay_engine = _NoopDecay()
    rt.dehydrator = _ExplodingDehydrator()
    rt.embedding_engine = _DisabledEmbedding()
    rt.logger = MagicMock()
    rt.fire_webhook = None
    rt.mark_op = None
    rt.record_v3_tool_event = lambda *_args, **_kwargs: None


async def _make_plan(bucket_mgr, content: str, status: str = "active") -> str:
    bucket_id = await bucket_mgr.create(
        content=content,
        tags=["__plan__"],
        importance=7,
        domain=["plan"],
        valence=0.5,
        arousal=0.4,
        name=None,
        bucket_type="plan",
    )
    await bucket_mgr.update(bucket_id, status=status)
    return bucket_id


@pytest.mark.asyncio
async def test_domain_plan_returns_plan_bodies_not_core_principles(bucket_mgr):
    """原 bug 的回归：domain="plan" 必须返回 plan 正文，不是置顶核心准则。"""
    _install_runtime(bucket_mgr)
    await _make_plan(bucket_mgr, "把工具精简的第二步做完")
    # 一条 pinned 核心准则：修复前它会顶替 plan 出现在返回里
    await bucket_mgr.create(
        content="这是置顶核心准则，不该在 domain=plan 时返回",
        tags=[], importance=10, domain=["general"],
        valence=0.5, arousal=0.5, name=None, pinned=True,
    )

    out = await dispatch(domain="plan")

    assert "把工具精简的第二步做完" in out
    assert "这是置顶核心准则，不该在 domain=plan 时返回" not in out


@pytest.mark.asyncio
async def test_domain_plan_says_no_plan_instead_of_returning_nothing(bucket_mgr):
    """一条 plan 都没有时要明说，不能返回空或退化成浮现内容。"""
    _install_runtime(bucket_mgr)
    await bucket_mgr.create(
        content="一条普通记忆，不是 plan",
        tags=[], importance=5, domain=["general"],
        valence=0.5, arousal=0.5, name=None,
    )

    out = await dispatch(domain="plan")

    assert "没有计划。" in out
    assert "一条普通记忆，不是 plan" not in out


@pytest.mark.asyncio
async def test_domain_plan_excludes_resolved_and_abandoned(bucket_mgr):
    """只返回 active；已完成/已放弃的不再占用返回预算。"""
    _install_runtime(bucket_mgr)
    await _make_plan(bucket_mgr, "还没做完的事", status="active")
    await _make_plan(bucket_mgr, "已经做完的事", status="resolved")
    await _make_plan(bucket_mgr, "已经放弃的事", status="abandoned")

    out = await dispatch(domain="plan")

    assert "还没做完的事" in out
    assert "已经做完的事" not in out
    assert "已经放弃的事" not in out


@pytest.mark.asyncio
async def test_plan_channel_returns_verbatim_without_calling_llm(bucket_mgr):
    """plan 正文逐字返回：_ExplodingDehydrator 会在任何 LLM 调用时炸掉。"""
    _install_runtime(bucket_mgr)
    body = "周五之前把 relation 自动建立接线，阈值按施工单写死的那三个走"
    await _make_plan(bucket_mgr, body)

    out = await dispatch(domain="plan")

    assert body in out
