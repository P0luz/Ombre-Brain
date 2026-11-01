from __future__ import annotations

import pytest

# server 一律在函数内导入：模块顶层导入会在收集阶段装配 tools/_runtime 的全局。

失败用例 = [
    ("请求 id 不存在", {"deletion_request_id": "no-such-id", "deletion_decision": "approve"}),
    ("决定值非法", {"deletion_request_id": "any", "deletion_decision": "赞成"}),
    ("只给决定不给 id", {"deletion_decision": "approve"}),
]


@pytest.mark.parametrize("说明, 参数", 失败用例, ids=[c[0] for c in 失败用例])
@pytest.mark.asyncio
async def test_删除审批失败必须抛错(说明, 参数, monkeypatch):
    import server

    async def decide(*_args, **_kwargs):
        return {"ok": False, "error": "pending request not found"}

    monkeypatch.setattr(server.deletion_requests, "decide", decide)

    with pytest.raises(Exception) as excinfo:
        await server.trace(bucket_id="whatever", **参数)

    assert not isinstance(excinfo.value, AssertionError)


@pytest.mark.asyncio
async def test_删除审批成功照旧返回正文(monkeypatch):
    import server

    async def decide(*_args, **_kwargs):
        return {"ok": True, "decision": "approved", "bucket_id": "bucket123"}

    monkeypatch.setattr(server.deletion_requests, "decide", decide)

    out = await server.trace(
        bucket_id="bucket123",
        deletion_request_id="req-1",
        deletion_decision="approve",
    )

    assert "approved" in out
    assert "bucket123" in out
    assert "req-1" in out
