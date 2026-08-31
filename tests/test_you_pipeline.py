"""You 的写入与召回：模型自己写，两道结构性的闸，全程不碰 LLM。

这些用例锁的是 3.4.x 那次改动的设计意图（见 dev 侧「不走 LLM 的 You 设计」）：
认识由模型显式写下，验证靠三个不同自然日的重申 + 至少两个真实记忆桶的显式关系。
"""

import pytest

from ombrebrain.you import YouService, YouStore, YouStoreError
from ombrebrain.you.service import MIN_SUPPORTING_BUCKETS, REQUIRED_CONFIRMATIONS


class FakeBucketManager:
    def __init__(self):
        self.buckets = {}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeSourceStore:
    def __init__(self):
        self.sources = {}

    def read(self, source_id):
        return self.sources[source_id]


class ExplodingDehydrator:
    """任何一次 LLM 调用都会炸。

    You 不许再有自动抽取 / 自动复核 / 自动摘要——那正是这次拿掉的东西。
    把它做成地雷而不是空壳，是为了让"哪天有人又把 LLM 接回来"当场变成红灯，
    而不是悄悄多出一层替模型思考的中间人。
    """

    def __getattr__(self, name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f"You 不允许调用 LLM，却调了 dehydrator.{name}")

        return _boom


def _service(tmp_path):
    manager = FakeBucketManager()
    sources = FakeSourceStore()
    service = YouService(
        store=YouStore(tmp_path),
        bucket_mgr=manager,
        dehydrator=ExplodingDehydrator(),
        source_store=sources,
    )
    return service, manager


def _bucket(bucket_id, content, **metadata):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {"type": "dynamic", **metadata},
    }


def _enabled(tmp_path, *, buckets=2):
    service, manager = _service(tmp_path)
    service.set_enabled(True)
    for index in range(1, buckets + 1):
        bucket_id = f"memory-{index}"
        manager.buckets[bucket_id] = _bucket(bucket_id, f"第 {index} 次她提到希望被叫做 Lin。")
    return service, manager


async def _write(service, *, content="她希望日常被称呼为 Lin", buckets=("memory-1", "memory-2")):
    return await service.write(
        content=content,
        bucket_ids=list(buckets),
        aspect="preferred_address",
        concept_key="preferred_address",
        concept_value="lin",
        basis="explicit_statement",
        explicit=True,
        long_term=True,
    )


def _stamp(monkeypatch, day):
    """把 service 眼里的"今天"钉死在 2026-08-<day>，用来模拟隔天重申。"""
    monkeypatch.setattr(
        "ombrebrain.you.service.utc_now",
        lambda: f"2026-08-{day:02d}T10:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_writing_requires_at_least_two_supporting_buckets(tmp_path):
    service, _ = _enabled(tmp_path)

    with pytest.raises(ValueError, match="不能只有一个出处"):
        await _write(service, buckets=("memory-1",))


@pytest.mark.asyncio
async def test_the_same_bucket_twice_does_not_count_as_two(tmp_path):
    service, _ = _enabled(tmp_path)

    with pytest.raises(ValueError, match="不能只有一个出处"):
        await _write(service, buckets=("memory-1", "memory-1"))


@pytest.mark.asyncio
async def test_writing_rejects_missing_ignored_or_test_buckets(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["plan-1"] = _bucket("plan-1", "一条 plan", type="plan")
    manager.buckets["test-1"] = _bucket(
        "test-1", "测试数据", provenance={"kind": "test", "erasable": True}
    )
    manager.buckets["empty-1"] = _bucket("empty-1", "   ")

    with pytest.raises(ValueError, match="找不到记忆桶"):
        await _write(service, buckets=("memory-1", "nope"))
    with pytest.raises(ValueError, match="不能作为 you 的依据"):
        await _write(service, buckets=("memory-1", "plan-1"))
    with pytest.raises(ValueError, match="测试数据"):
        await _write(service, buckets=("memory-1", "test-1"))
    with pytest.raises(ValueError, match="没有正文"):
        await _write(service, buckets=("memory-1", "empty-1"))


@pytest.mark.asyncio
async def test_one_write_only_creates_a_candidate(tmp_path, monkeypatch):
    service, _ = _enabled(tmp_path)
    _stamp(monkeypatch, 17)

    claim, message = await _write(service)

    assert claim.lifecycle == "candidate"
    assert claim.review_date_count == 1
    assert "还要在另外 2 个不同的日子" in message
    assert await service.recall(query="Lin") == "", "候选不该被召回"


@pytest.mark.asyncio
async def test_repeating_on_the_same_day_does_not_advance(tmp_path, monkeypatch):
    service, _ = _enabled(tmp_path)
    _stamp(monkeypatch, 17)

    await _write(service)
    claim, _ = await _write(service)
    claim, _ = await _write(service)

    assert claim.review_date_count == 1, "同一天重申多少次都只算一天"
    assert claim.lifecycle == "candidate"


@pytest.mark.asyncio
async def test_three_distinct_days_make_it_real(tmp_path, monkeypatch):
    service, _ = _enabled(tmp_path)

    for day in (17, 18, 19):
        _stamp(monkeypatch, day)
        claim, message = await _write(service)

    assert claim.review_date_count == REQUIRED_CONFIRMATIONS
    assert claim.lifecycle == "formal"
    assert "已经生效" in message
    assert "Lin" in await service.recall(query="Lin")


@pytest.mark.asyncio
async def test_editing_a_live_entry_sends_it_back_to_candidate(tmp_path, monkeypatch):
    service, _ = _enabled(tmp_path)
    for day in (17, 18, 19):
        _stamp(monkeypatch, day)
        await _write(service)

    _stamp(monkeypatch, 20)
    edited, message = await _write(service, content="她希望被称呼为 Lin，工作场合除外")

    assert edited.lifecycle == "candidate", "改了正文就得重新攒三天"
    assert edited.review_date_count == 1
    assert "还要在另外 2 个不同的日子" in message
    assert await service.recall(query="Lin") == ""


@pytest.mark.asyncio
async def test_losing_a_supporting_bucket_expires_the_entry(tmp_path, monkeypatch):
    service, manager = _enabled(tmp_path)
    for day in (17, 18, 19):
        _stamp(monkeypatch, day)
        claim, _ = await _write(service)
    assert claim.lifecycle == "formal"

    manager.buckets.pop("memory-2")
    await service._remove_bucket_evidence(service.status().scope, "memory-2")

    assert await service.recall(query="Lin") == "", "依据塌到门槛以下就不该再被召回"
    remaining = service.store.get_claim(service.status().scope, claim.id)
    assert remaining.lifecycle == "expired"


@pytest.mark.asyncio
async def test_deleting_needs_no_confirmations(tmp_path, monkeypatch):
    service, _ = _enabled(tmp_path)
    for day in (17, 18, 19):
        _stamp(monkeypatch, day)
        claim, _ = await _write(service)
    assert claim.lifecycle == "formal"

    _stamp(monkeypatch, 20)
    message = await service.delete(claim.id)

    assert "撤回了" in message
    assert await service.recall(query="Lin") == ""


@pytest.mark.asyncio
async def test_disabled_module_refuses_both_directions(tmp_path):
    service, _ = _service(tmp_path)

    with pytest.raises(YouStoreError, match="unknown tool"):
        await _write(service)
    with pytest.raises(YouStoreError, match="unknown tool"):
        await service.recall(query="Lin")
    assert not (tmp_path / ".you").exists(), "关着的时候连库都不该建"


@pytest.mark.asyncio
async def test_forbidden_subjects_and_source_copies_are_refused(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _bucket(
        "memory-1", "她下班以后通常希望先安静一会儿，不要连续追问。"
    )

    with pytest.raises(ValueError):
        await service.write(
            content="她是典型的内向人格",
            bucket_ids=["memory-1", "memory-2"],
            aspect="interaction_habit",
            concept_key="personality",
            concept_value="introvert",
        )
    with pytest.raises(ValueError):
        await service.write(
            content="下班以后，通常希望先安静一会儿",
            bucket_ids=["memory-1", "memory-2"],
            aspect="interaction_habit",
            concept_key="after_work",
            concept_value="quiet",
        )


@pytest.mark.asyncio
async def test_thresholds_are_named_not_magic(tmp_path):
    assert REQUIRED_CONFIRMATIONS == 3
    assert MIN_SUPPORTING_BUCKETS == 2


async def _formalized(service, monkeypatch):
    """攒满三个不同自然日，让这条认识真正生效。"""
    for day in (16, 17, 18):
        _stamp(monkeypatch, day)
        claim, _ = await _write(service)
    return claim


@pytest.mark.asyncio
async def test_依据被删掉之后读回时当场失效(tmp_path, monkeypatch):
    """闸二的后半段，走读时校验而不是桶变动通知。

    这条以前是坏的：`_remove_bucket_evidence` 写了，但没有任何人调用它——
    `bucket_change_observers` 从来没被注册过。3.4.x 拿掉 LLM 那轮删掉了
    `observe_bucket_change` 和 outbox，没接替代路径，于是
    「依据被归档或删除，这条认识会自动失效」在工具描述、rule.md 和 SPEC 里
    留着，实际不成立。
    """
    service, manager = _enabled(tmp_path)
    await _formalized(service, monkeypatch)
    assert "Lin" in await service.recall(query="称呼")

    del manager.buckets["memory-1"]  # 依据塌到只剩一个

    assert await service.recall(query="称呼") == ""


@pytest.mark.asyncio
async def test_塌了一个但还够就继续生效(tmp_path, monkeypatch):
    service, manager = _enabled(tmp_path, buckets=3)
    for day in (16, 17, 18):
        _stamp(monkeypatch, day)
        await _write(service, buckets=("memory-1", "memory-2", "memory-3"))
    del manager.buckets["memory-1"]  # 还剩两个，仍然过门槛
    assert "Lin" in await service.recall(query="称呼")


@pytest.mark.asyncio
async def test_读桶抖动不会误杀(tmp_path, monkeypatch):
    """读不到桶不等于桶没了。一次磁盘抖动不该判死一条攒了三天的认识。"""
    service, manager = _enabled(tmp_path)
    await _formalized(service, monkeypatch)

    async def 炸(_bucket_id):
        raise OSError("disk hiccup")

    manager.get = 炸
    assert "Lin" in await service.recall(query="称呼")


@pytest.mark.asyncio
async def test_归档的依据仍然撑得住(tmp_path, monkeypatch):
    """归档只改变可见性，不使证据失效（rule.md 第 9 条、SPEC 9.3）。

    自动衰减归档是常态。让它触发失效，等于一条攒了三个自然日才立住的认识
    会因为某个依据自然淡出而被时间清空——和「立得那么难」的设计意图冲突。
    """
    service, manager = _enabled(tmp_path)
    await _formalized(service, monkeypatch)
    归档了 = dict(manager.buckets["memory-1"])
    归档了["metadata"] = {**归档了["metadata"], "type": "archived"}
    manager.buckets["memory-1"] = 归档了

    assert "Lin" in await service.recall(query="称呼")
