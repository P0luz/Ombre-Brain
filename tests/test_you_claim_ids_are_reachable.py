from dataclasses import replace

import pytest

from ombrebrain.you import YouService, YouStore
from ombrebrain.you.service import REQUIRED_CONFIRMATIONS


class FakeBucketManager:
    def __init__(self):
        self.buckets = {}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeSourceStore:
    def read(self, source_id):
        raise KeyError(source_id)


class ExplodingDehydrator:
    def __getattr__(self, name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f"You 不允许调用 LLM，却调了 {name}")

        return _boom


def _enabled(tmp_path):
    manager = FakeBucketManager()
    service = YouService(
        store=YouStore(tmp_path),
        bucket_mgr=manager,
        dehydrator=ExplodingDehydrator(),
        source_store=FakeSourceStore(),
    )
    service.set_enabled(True)
    for index in (1, 2):
        bucket_id = f"memory-{index}"
        manager.buckets[bucket_id] = {
            "id": bucket_id,
            "content": f"第 {index} 次她提到希望被叫做 Lin。",
            "metadata": {"type": "dynamic"},
        }
    return service


async def _write(service):
    return await service.write(
        content="她希望日常被称呼为 Lin",
        bucket_ids=["memory-1", "memory-2"],
        aspect="preferred_address",
        concept_key="preferred_address",
        concept_value="lin",
        basis="explicit_statement",
        explicit=True,
        long_term=True,
    )


def _age_receipts(service, claim):
    receipts = tuple(
        replace(receipt, reviewed_at=f"2026-08-{10 + index:02d}T10:00:00+00:00")
        for index, receipt in enumerate(claim.review_receipts)
    )
    return service.store.put_claim(
        replace(claim, review_receipts=receipts), expected_revision=claim.revision
    )


async def _make_live(service):
    claim = None
    for _ in range(REQUIRED_CONFIRMATIONS):
        claim, _ = await _write(service)
        claim = _age_receipts(
            service, service.store.get_claim(service.status().scope, claim.id)
        )
    await _write(service)
    return service.store.get_claim(service.status().scope, claim.id)


@pytest.mark.asyncio
async def test_default_recall_carries_no_ids(tmp_path):
    service = _enabled(tmp_path)
    claim = await _make_live(service)

    output = await service.recall()

    assert "她希望日常被称呼为 Lin" in output
    assert claim.id not in output


@pytest.mark.asyncio
async def test_with_ids_exposes_the_claim_id(tmp_path):
    service = _enabled(tmp_path)
    claim = await _make_live(service)

    output = await service.recall(with_ids=True)

    assert claim.id in output


@pytest.mark.asyncio
async def test_an_exposed_id_can_actually_be_withdrawn(tmp_path):
    service = _enabled(tmp_path)
    await _make_live(service)

    output = await service.recall(with_ids=True)
    claim_id = output.split("[id=")[1].split("]")[0]
    await service.delete(claim_id)

    assert "她希望日常被称呼为 Lin" not in await service.recall()


@pytest.mark.asyncio
async def test_with_ids_still_returns_the_content(tmp_path):
    service = _enabled(tmp_path)
    await _make_live(service)

    output = await service.recall(with_ids=True)

    assert "她希望日常被称呼为 Lin" in output
