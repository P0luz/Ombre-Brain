import pytest

from ombrebrain.them import ThemService, ThemStore


class FakeBucketManager:
    def __init__(self):
        self.buckets = {}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeSourceStore:
    def read(self, source_id):
        raise KeyError(source_id)


class RealisticDecay:
    @staticmethod
    def calculate_score(metadata):
        return float(metadata.get("activation_count") or 1)


class ExplodingLLM:
    def __getattr__(self, name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f"them 不允许调用 LLM，却调了 {name}")

        return _boom


def _bucket(bucket_id, content, **meta):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {"type": "dynamic", **meta},
    }


def _letter(bucket_id, content, **meta):
    return _bucket(bucket_id, content, type="letter", source_tool="letter", **meta)


def _enabled(tmp_path):
    manager = FakeBucketManager()
    service = ThemService(
        store=ThemStore(tmp_path),
        bucket_mgr=manager,
        decay_engine=RealisticDecay(),
        source_store=FakeSourceStore(),
        config={},
    )
    service.dehydrator = ExplodingLLM()
    service.set_enabled(True)
    return service, manager


async def _write(service, buckets=("memory-1", "memory-2")):
    return await service.write(
        content="她讲话直奔结论，不铺垫",
        bucket_ids=list(buckets),
        aspect="communication_preference",
        concept_key="talk_style",
        concept_value="blunt",
        names=["Zoey"],
    )


@pytest.mark.asyncio
async def test_an_open_letter_can_back_a_claim(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _letter("memory-1", "今天和 Zoey 吃饭，她说话很直。")
    manager.buckets["memory-2"] = _letter("memory-2", "Zoey 又一次直接给了结论。")

    claim, _ = await _write(service)

    assert claim is not None


@pytest.mark.asyncio
async def test_a_locked_letter_is_refused(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _letter("memory-1", "今天和 Zoey 吃饭。")
    manager.buckets["memory-2"] = _letter(
        "memory-2",
        "Zoey 说了一些话。",
        lock_type="permanent",
        locked_by="human",
    )

    with pytest.raises(ValueError, match="还没对你开放"):
        await _write(service)


@pytest.mark.asyncio
async def test_a_letter_the_ai_locked_itself_is_allowed(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _letter("memory-1", "今天和 Zoey 吃饭。")
    manager.buckets["memory-2"] = _letter(
        "memory-2",
        "Zoey 说了一些话。",
        lock_type="permanent",
        locked_by="ai",
    )

    claim, _ = await _write(service)

    assert claim is not None


@pytest.mark.asyncio
async def test_an_expired_time_lock_is_open_again(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _letter("memory-1", "今天和 Zoey 吃饭。")
    manager.buckets["memory-2"] = _letter(
        "memory-2",
        "Zoey 说了一些话。",
        lock_type="timed",
        locked_by="human",
        unlock_date="2020-01-01T00:00:00+00:00",
    )

    claim, _ = await _write(service)

    assert claim is not None


@pytest.mark.asyncio
async def test_other_private_types_are_still_refused(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _bucket("memory-1", "Zoey 说话很直。")
    manager.buckets["memory-2"] = _bucket("memory-2", "Zoey 又直说了。", type="feel")

    with pytest.raises(ValueError, match="feel 类型"):
        await _write(service)


# --------------------------------------------------------------
# 名字也可以由标题指明
# --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_title_naming_the_person_counts(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _bucket("memory-1", "Zoey 说话很直。")
    manager.buckets["memory-2"] = _bucket(
        "memory-2", "她又一次直接给了结论。", title="和 Zoey 的晚饭"
    )

    claim, _ = await _write(service)

    assert claim is not None


@pytest.mark.asyncio
async def test_a_bucket_name_naming_the_person_counts(tmp_path):
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _bucket("memory-1", "Zoey 说话很直。")
    manager.buckets["memory-2"] = _bucket(
        "memory-2", "她又一次直接给了结论。", name="Zoey 的沟通方式"
    )

    claim, _ = await _write(service)

    assert claim is not None


@pytest.mark.asyncio
async def test_naming_nowhere_is_still_refused(tmp_path):
    """门槛没降：正文、标题、桶名都不提这个人，照旧拒。"""
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _bucket("memory-1", "Zoey 说话很直。")
    manager.buckets["memory-2"] = _bucket(
        "memory-2", "她又一次直接给了结论。", title="一次晚饭"
    )

    with pytest.raises(ValueError, match="都没有出现"):
        await _write(service)


@pytest.mark.asyncio
async def test_every_bucket_must_still_name_the_person(tmp_path):
    """仍然是「每个桶都要有」，不是「至少一个」。"""
    service, manager = _enabled(tmp_path)
    manager.buckets["memory-1"] = _bucket(
        "memory-1", "Zoey 说话很直。", title="和 Zoey 聊天"
    )
    manager.buckets["memory-2"] = _bucket("memory-2", "她又一次直接给了结论。")

    with pytest.raises(ValueError, match="都没有出现"):
        await _write(service)
