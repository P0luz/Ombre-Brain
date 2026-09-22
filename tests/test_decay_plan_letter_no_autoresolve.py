"""decay_engine 不得碰 plan/letter 回归测试。

找茬会话发现的 bug：run_decay_cycle() 的跳过名单只写了
("permanent", "feel", "i")，漏了 plan/letter。calculate_score() 确实对
plan/letter 恒定返回高于阈值的分数（不会被归档），但同一循环里跑在打分
之前的「自动结案」分支（重要度≤4 + 超过30天未激活 → resolved=True）不看
type，会直接把地位是"生命周期只能由 status 字段驱动"的 plan、和"承诺永久
原样保留"的 letter 一起标成 resolved——绕开了它们各自的真实生命周期规则。

修复：跳过名单加上 "plan"、"letter"，跟 tools/_common.py 的
cascade_plan_resolved_to_buckets() 用的排除逻辑保持一致。
"""
import frontmatter as fm
import pytest


def _backdate(bucket_mgr, bucket_id: str, days_ago: int) -> None:
    """把桶的 created/last_active 都改成 N 天前，制造「超期未激活」的条件。"""
    from datetime import datetime, timedelta

    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = fm.load(fpath)
    old_ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    post["created"] = old_ts
    post["last_active"] = old_ts
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))


@pytest.mark.asyncio
async def test_decay_cycle_never_auto_resolves_plan(bucket_mgr, decay_eng):
    bid = await bucket_mgr.create_internal(
        content="记得帮她把这件事办完", bucket_type="plan", importance=3,
    )
    _backdate(bucket_mgr, bid, days_ago=60)

    await decay_eng.run_decay_cycle()

    plan = await bucket_mgr.get(bid)
    assert plan["metadata"].get("resolved") is not True, (
        "plan 的生命周期只能由 status 字段驱动，decay 的自动结案不该碰它"
    )
    assert plan["metadata"]["type"] == "plan"


@pytest.mark.asyncio
async def test_decay_cycle_never_auto_resolves_letter(bucket_mgr, decay_eng):
    bid = await bucket_mgr.create_internal(
        content="给未来自己的一封信", bucket_type="letter", importance=3,
    )
    _backdate(bucket_mgr, bid, days_ago=60)

    await decay_eng.run_decay_cycle()

    letter = await bucket_mgr.get(bid)
    assert letter["metadata"].get("resolved") is not True, (
        "letter 承诺永久原样保留，decay 的自动结案不该碰它"
    )
    assert letter["metadata"]["type"] == "letter"


@pytest.mark.asyncio
async def test_decay_cycle_still_auto_resolves_ordinary_dynamic_bucket(bucket_mgr, decay_eng):
    """对照组：证明上面两条不是「自动结案分支根本没触发」这种伪通过。"""
    bid = await bucket_mgr.create_internal(content="一件早就不重要的小事", importance=3)
    _backdate(bucket_mgr, bid, days_ago=60)

    stats = await decay_eng.run_decay_cycle()

    ordinary = await bucket_mgr.get(bid)
    assert ordinary["metadata"].get("resolved") is True
    assert stats["auto_resolved"] >= 1
