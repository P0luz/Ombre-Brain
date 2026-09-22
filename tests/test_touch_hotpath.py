"""touch 批量 + ripple 开关回归测试（性能 P2）。

breath 浮现后应把 touch 移出响应路径、批量在后台跑；ripple 默认关（不为激活微调
多跑读全库的时间涟漪）。touch 本身的激活语义要保持不变。
"""
import pytest


@pytest.mark.asyncio
async def test_touch_default_still_ripples(bucket_mgr, monkeypatch):
    bid = await bucket_mgr.create_internal(content="内容一号一号一号", name="一", domain=["测试"])
    called = {"n": 0}
    orig = bucket_mgr._time_ripple

    async def spy(*a, **k):
        called["n"] += 1
        return await orig(*a, **k)
    monkeypatch.setattr(bucket_mgr, "_time_ripple", spy)

    await bucket_mgr.touch(bid)                 # 默认 ripple=True
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_touch_ripple_false_skips_time_ripple(bucket_mgr, monkeypatch):
    bid = await bucket_mgr.create_internal(content="内容一号一号一号", name="一", domain=["测试"])
    called = {"n": 0}

    async def spy(*a, **k):
        called["n"] += 1
    monkeypatch.setattr(bucket_mgr, "_time_ripple", spy)

    await bucket_mgr.touch(bid, ripple=False)   # 跳过涟漪
    assert called["n"] == 0
    # 但激活本身仍生效
    b = await bucket_mgr.get(bid)
    assert float(b["metadata"].get("activation_count") or 0) >= 1


@pytest.mark.asyncio
async def test_touch_many_bumps_all_and_ripples_at_most_once(bucket_mgr, monkeypatch):
    ids = []
    for i in range(3):
        ids.append(await bucket_mgr.create_internal(content=f"内容{i}号内容内容", name=f"桶{i}", domain=["测试"]))
    called = {"n": 0}

    async def spy(*a, **k):
        called["n"] += 1
    monkeypatch.setattr(bucket_mgr, "_time_ripple", spy)

    await bucket_mgr.touch_many(ids, ripple=False)
    assert called["n"] == 0
    for bid in ids:
        b = await bucket_mgr.get(bid)
        assert float(b["metadata"].get("activation_count") or 0) >= 1

    # ripple=True 时最多涟漪一次（只对第一个），避免 N×list_all
    called["n"] = 0
    await bucket_mgr.touch_many(ids, ripple=True)
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_touch_many_tolerates_bad_id(bucket_mgr):
    good = await bucket_mgr.create_internal(content="内容好好好好好", name="好", domain=["测试"])
    await bucket_mgr.touch_many(["nonexistent-id", good], ripple=False)  # 不抛
    b = await bucket_mgr.get(good)
    assert float(b["metadata"].get("activation_count") or 0) >= 1
