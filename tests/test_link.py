"""link 补线工具：手动声明 references / continuation_of / related_to 边。

- 双向同步写 + source=manual
- 幂等（重复补线不重复写）
- 方向正确（continuation_of → continues；related_to 双向同型）
- 拒绝非法输入（自连、坏类型、不存在、分层桶）
- 软边：不 bump last_active
"""

from unittest.mock import MagicMock

import pytest

import tools._runtime as rt
from tools.link import link, unlink
from ombrebrain.storage.relation_store import normalize_relation_links


@pytest.fixture
def link_runtime(monkeypatch, bucket_mgr, test_config):
    monkeypatch.setattr(rt, "bucket_mgr", bucket_mgr, raising=False)
    monkeypatch.setattr(rt, "logger", MagicMock(), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)
    return bucket_mgr


async def _mk(manager, content):
    return await manager.create(content=content, domain=["测试"])


@pytest.mark.asyncio
async def test_link_references_builds_both_sides(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "A 的正文")
    b = await _mk(manager, "B 的正文")
    out = await link(a, b, "references")
    assert "已补线" in out

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    blinks = (await manager.get(b))["metadata"]["relation_links"]
    fwd = [l for l in alinks if l["type"] == "references"]
    rev = [l for l in blinks if l["type"] == "referenced_by"]
    assert len(fwd) == 1 and fwd[0]["target_bucket_id"] == b
    assert fwd[0].get("source") == "manual"
    assert fwd[0].get("auto") is not True
    assert len(rev) == 1 and rev[0]["target_bucket_id"] == a
    assert rev[0].get("source") == "manual"


@pytest.mark.asyncio
async def test_link_continuation_of_direction(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "后段")
    b = await _mk(manager, "前段")
    out = await link(a, b, "continuation_of")
    assert "已补线" in out

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    blinks = (await manager.get(b))["metadata"]["relation_links"]
    assert any(l["type"] == "continuation_of" and l["target_bucket_id"] == b for l in alinks)
    assert any(l["type"] == "continues" and l["target_bucket_id"] == a for l in blinks)


@pytest.mark.asyncio
async def test_link_related_to_both_sides_same_type(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    await link(a, b, "related_to")

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    blinks = (await manager.get(b))["metadata"]["relation_links"]
    assert any(l["type"] == "related_to" and l["target_bucket_id"] == b for l in alinks)
    assert any(l["type"] == "related_to" and l["target_bucket_id"] == a for l in blinks)


@pytest.mark.asyncio
async def test_link_is_idempotent(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    first = await link(a, b, "references")
    second = await link(a, b, "references")
    assert "已补线" in first
    assert "已经在了" in second

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    assert sum(1 for l in alinks if l["type"] == "references" and l["target_bucket_id"] == b) == 1


@pytest.mark.asyncio
async def test_link_rejects_bad_input(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    assert "不能连到自己" in await link(a, a)
    assert "只支持" in await link(a, b, "caused_by")
    assert "未找到" in await link("nonexistent", b)
    assert "未找到" in await link(a, "nonexistent")

    feel_id = await manager.create(content="一条感受", domain=["测试"], bucket_type="feel")
    assert "分层桶" in await link(a, feel_id)


@pytest.mark.asyncio
async def test_link_does_not_bump_last_active(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    before = (await manager.get(a))["metadata"].get("last_active")
    await link(a, b, "references")
    after = (await manager.get(a))["metadata"].get("last_active")
    assert before == after


# ---------- unlink（3.8.0） ----------

@pytest.mark.asyncio
async def test_unlink_soft_detaches_both_sides(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    await link(a, b, "references")
    out = await unlink(a, b, "references")
    assert "已断线" in out

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    blinks = (await manager.get(b))["metadata"]["relation_links"]
    fwd = [l for l in alinks if l["type"] == "references" and l["target_bucket_id"] == b]
    rev = [l for l in blinks if l["type"] == "referenced_by" and l["target_bucket_id"] == a]
    # 软删：边还在，但 status 变 detached
    assert len(fwd) == 1 and fwd[0]["status"] == "detached"
    assert len(rev) == 1 and rev[0]["status"] == "detached"


@pytest.mark.asyncio
async def test_unlink_reports_missing(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    out = await unlink(a, b, "references")
    assert "不存在" in out


@pytest.mark.asyncio
async def test_unlink_rejects_auto_edge(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")

    def _force_auto(post):
        links = normalize_relation_links(post.metadata.get("relation_links"))
        links.append(
            {
                "target_bucket_id": b,
                "type": "references",
                "label": "",
                "status": "active",
                "auto": True,
                "score": 0.9,
            }
        )
        post["relation_links"] = normalize_relation_links(links)
        return True, None

    await manager.mutate_relation_links(a, _force_auto)
    out = await unlink(a, b, "references")
    assert "自动建立" in out


@pytest.mark.asyncio
async def test_unlink_then_relink_reactivates(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    await link(a, b, "references")
    await unlink(a, b, "references")
    out = await link(a, b, "references")
    assert "已补线" in out  # 重新激活，不报「已经在了」也不算新增

    alinks = (await manager.get(a))["metadata"]["relation_links"]
    fwd = [l for l in alinks if l["type"] == "references" and l["target_bucket_id"] == b]
    # 只有一条（重新激活，不是新增重复）
    assert len(fwd) == 1
    assert fwd[0]["status"] == "active"


@pytest.mark.asyncio
async def test_unlink_rejects_bad_input(link_runtime):
    manager = link_runtime
    a = await _mk(manager, "甲")
    b = await _mk(manager, "乙")
    assert "自己身上" in await unlink(a, a)
    assert "只支持" in await unlink(a, b, "caused_by")
    assert "未找到" in await unlink("nonexistent", b)
