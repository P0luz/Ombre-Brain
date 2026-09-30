"""Phase 3 Step 2 — surface_default 里 plan resurfacing 的 budget 行为测试。

装配沿用 test_breath_recency 的最小 stub（PlainBucketManager / WeightedDecay /
DisabledEmbedding / _install）。random 被固定：shuffle no-op、random()=1.0（关掉
3% 偶遇），保证 deterministic。
"""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.breath.surface import surface_default


class DisabledEmbedding:
    enabled = False


class WeightedDecay:
    is_running = True

    async def ensure_started(self):
        return None

    def calculate_score(self, metadata):
        return float(metadata.get("_score", metadata.get("importance") or 5))


class PlainBucketManager:
    def __init__(self, buckets):
        self.buckets = list(buckets)

    async def list_all(self, include_archive=False):
        return list(self.buckets)

    async def get_stats(self):
        return {"permanent_count": 0, "dynamic_count": len(self.buckets)}

    def footprint_snapshot(self):
        raise RuntimeError("no footprint in tests")


def _install(monkeypatch, manager, surfacing=None):
    monkeypatch.setattr(rt, "config", {"surfacing": surfacing or {}})
    monkeypatch.setattr(rt, "bucket_mgr", manager)
    monkeypatch.setattr(rt, "decay_engine", WeightedDecay())
    monkeypatch.setattr(rt, "embedding_engine", DisabledEmbedding())
    monkeypatch.setattr(rt, "logger", MagicMock())
    monkeypatch.setattr(rt, "mark_op", None)
    monkeypatch.setattr("tools.breath.surface.random.shuffle", lambda _seq: None)
    monkeypatch.setattr("tools.breath.surface.random.random", lambda: 1.0)


def _dyn(bid, score=5.0):
    return {
        "id": bid,
        "content": f"{bid} 的正文内容。",
        "metadata": {
            "name": bid, "type": "dynamic", "importance": 5, "domain": ["t"],
            "created": "2026-01-01T00:00:00", "last_active": "2026-01-01T00:00:00",
            "activation_count": 3, "_score": score,
        },
    }


def _plan(pid, related, *, status="active", open_window=True):
    now = datetime.now(timezone.utc)
    if open_window:
        ws = (now - timedelta(days=1)).isoformat()
        we = (now + timedelta(days=1)).isoformat()
    else:  # 窗口在未来，尚未到点
        ws = (now + timedelta(days=5)).isoformat()
        we = (now + timedelta(days=10)).isoformat()
    return {
        "id": pid,
        "content": f"{pid} 的计划正文。",
        "metadata": {
            "name": pid, "type": "plan", "status": status,
            "related_bucket": related, "window_start": ws, "window_end": we,
            "created": "2026-01-01T00:00:00",
        },
    }


SECTION = "=== 到点的计划 ==="


async def _surface(max_tokens=20000):
    return await surface_default(max_results=10, max_tokens=max_tokens, tag_filter=[])


# --- eligible plan 出现 ---
@pytest.mark.asyncio
async def test_eligible_plan_appears_in_section(monkeypatch):
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _plan("plan1", "bkt1")]))
    out = await _surface()
    assert SECTION in out
    assert "plan1 的计划正文" in out
    assert "bkt1" in out  # 普通浮现仍在


# --- 无 eligible plan：section 不出现 ---
@pytest.mark.asyncio
async def test_no_plan_no_section(monkeypatch):
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _dyn("bkt2")]))
    out = await _surface()
    assert SECTION not in out


@pytest.mark.asyncio
async def test_window_not_open_no_section(monkeypatch):
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _plan("plan1", "bkt1", open_window=False)]))
    out = await _surface()
    assert SECTION not in out


@pytest.mark.asyncio
async def test_resolved_plan_no_section(monkeypatch):
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _plan("plan1", "bkt1", status="resolved")]))
    out = await _surface()
    assert SECTION not in out


@pytest.mark.asyncio
async def test_related_bucket_not_surfaced_no_section(monkeypatch):
    # plan 指向的桶不在本轮浮现结果里 → 不触发
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _plan("plan1", "bkt_absent")]))
    out = await _surface()
    assert SECTION not in out


# --- 关键：无 eligible plan 时 budget 行为完全不变（逐字一致）---
@pytest.mark.asyncio
async def test_ineligible_plan_output_identical_to_no_plan(monkeypatch):
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _dyn("bkt2")]))
    out_no_plan = await _surface()

    # 加入一个不 eligible 的 plan（related 指向没浮现的桶）——不应改变普通输出
    _install(monkeypatch, PlainBucketManager([_dyn("bkt1"), _dyn("bkt2"), _plan("p", "bkt_absent")]))
    out_with_ineligible = await _surface()

    assert out_with_ineligible == out_no_plan
    assert SECTION not in out_with_ineligible


def _fake_render(bucket, header, footprint):
    """固定 token 的渲染替身：用 metadata['_tok'] 精确控制预算消耗。"""
    tok = int((bucket.get("metadata") or {}).get("_tok", 50))
    return (f"{header}\n{bucket.get('content', '')}", tok)


@pytest.mark.asyncio
async def test_pinned_heavy_reserve_clamped_no_negative_budget(monkeypatch):
    """edge case：pinned 已把预算吃到 remaining < nominal reserve 时，reserve 被
    clamp 到 remaining、token_budget 不进入负数（否则 budget notice 的 used 会 > limit）。"""
    pinned = {
        "id": "core1", "content": "核心准则正文",
        "metadata": {"name": "core1", "type": "permanent", "importance": 10,
                     "domain": ["t"], "created": "2026-01-01T00:00:00",
                     "last_active": "2026-01-01T00:00:00", "activation_count": 3,
                     "_tok": 350},
    }
    dyn = _dyn("bkt1")
    dyn["metadata"]["_tok"] = 50
    plan = _plan("plan1", "bkt1")
    _install(monkeypatch, PlainBucketManager([pinned, dyn, plan]))
    monkeypatch.setattr("tools.breath.surface.render_stored_bucket", _fake_render)

    # max_tokens=400：pinned 吃 350 → 剩 50；nominal reserve=min(600,100)=100 > 50，
    # 必须 clamp 到 50，token_budget 落到 0 而不是 -50。
    out = await surface_default(max_results=10, max_tokens=400, tag_filter=[])

    assert "core1" in out                        # 核心准则仍渲染、未崩
    m = re.search(r"当前约使用\s*(\d+)/(\d+)", out)
    assert m is not None                          # dynamic 因预算被挤 → 有 budget notice
    used, limit = int(m.group(1)), int(m.group(2))
    assert limit == 400
    assert used <= limit                          # token_budget 未变负（否则 used=450 > 400）
