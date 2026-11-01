"""grow 失败时，服务端日志必须说得清是哪儿错了。

真机反馈：长内容 grow 连续报错，日志只有
`err_type=RuntimeError detail=hidden`，查了两天无从下手。两个原因叠在一起：
- `detail=hidden` 是**写死在格式串里的字面量**，从来没打算记录原因
- 空返回和「给了东西但解析不出来」被塌缩成同一句「返回空结果」
"""

from __future__ import annotations

import logging

import pytest

from dehydrator import Dehydrator


def _dehydrator(tmp_path):
    return Dehydrator(
        {
            "dehydration": {
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "model": "test-model",
            },
            "buckets_dir": str(tmp_path),
        }
    )


def _stub_chat(dehydrator, monkeypatch, reply):
    async def fake_chat(_system, _user, **_kwargs):
        return reply

    monkeypatch.setattr(dehydrator, "_chat", fake_chat)


@pytest.mark.asyncio
async def test_empty_reply_and_unparseable_reply_report_differently(
    tmp_path, monkeypatch
):
    """两种毛病要分得开——原来都报「返回空结果」，等于什么都没说。"""
    empty = _dehydrator(tmp_path)
    _stub_chat(empty, monkeypatch, "   ")
    with pytest.raises(RuntimeError) as 空的:
        await empty.digest("一段够长的日记内容。\n第二行。\n第三行。")
    empty._cache_conn.close()

    garbage = _dehydrator(tmp_path)
    _stub_chat(garbage, monkeypatch, '[{"name": "断在这里')
    with pytest.raises(RuntimeError) as 半截的:
        await garbage.digest("一段够长的日记内容。\n第二行。\n第三行。")
    garbage._cache_conn.close()

    assert "没有返回任何内容" in str(空的.value)
    assert "解析不出条目" in str(半截的.value)
    # 两条都要带上预算，否则看到报错也不知道该调哪个旋钮。
    assert "max_tokens" in str(空的.value)
    assert "max_tokens" in str(半截的.value)


@pytest.mark.asyncio
async def test_digest_budget_is_configurable(tmp_path):
    """预算得能调：thinking 模型下多少算够跟具体模型强相关，写死必然有人撞上。"""
    default = _dehydrator(tmp_path)
    assert default.digest_max_tokens == 8192
    default._cache_conn.close()

    tuned = Dehydrator(
        {
            "dehydration": {
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "model": "test-model",
                "digest_max_tokens": 32768,
            },
            "buckets_dir": str(tmp_path),
        }
    )
    assert tuned.digest_max_tokens == 32768
    tuned._cache_conn.close()


@pytest.mark.asyncio
async def test_api_digest_still_returns_empty_list(tmp_path, monkeypatch):
    """`_api_digest` 的老契约不变：失败返回空列表，不抛。"""
    dehydrator = _dehydrator(tmp_path)
    _stub_chat(dehydrator, monkeypatch, "[]")

    assert await dehydrator._api_digest("随便写点什么") == []
    dehydrator._cache_conn.close()


@pytest.mark.asyncio
async def test_grow_logs_the_real_reason(tmp_path, monkeypatch, caplog):
    """日志里不能再出现写死的 detail=hidden。"""
    from tools import _runtime as rt
    from tools.grow import core as grow_core

    class _Boom:
        api_available = True

        async def digest(self, _content):
            raise RuntimeError("可辨认的具体原因")

    monkeypatch.setattr(rt, "dehydrator", _Boom(), raising=False)
    monkeypatch.setattr(rt, "logger", logging.getLogger("test-grow"), raising=False)

    with caplog.at_level(logging.ERROR, logger="test-grow"):
        with pytest.raises(Exception):
            await grow_core.grow_core("一段内容")

    日志 = "\n".join(record.getMessage() for record in caplog.records)
    assert "可辨认的具体原因" in 日志
    assert "detail=hidden" not in 日志
