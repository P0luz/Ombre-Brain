import json

import pytest

from ombrebrain.them import ThemService, ThemStore
from ombrebrain.them.service import REQUIRED_CONFIRMATIONS


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
    for index in (1, 2):
        bucket_id = f"memory-{index}"
        manager.buckets[bucket_id] = {
            "id": bucket_id,
            "content": f"第 {index} 次，Zoey 讲话都是直奔结论。",
            "metadata": {"type": "dynamic"},
        }
    return service


async def _write(service, **overrides):
    payload = {
        "content": "她讲话直奔结论，不铺垫",
        "bucket_ids": ["memory-1", "memory-2"],
        "aspect": "communication_preference",
        "concept_key": "talk_style",
        "concept_value": "blunt",
        "names": ["Zoey"],
    }
    payload.update(overrides)
    return await service.write(**payload)


def _age_receipts(service, claim):
    from dataclasses import replace

    receipts = tuple(
        replace(receipt, reviewed_at=f"2026-08-{10 + index:02d}T10:00:00+00:00")
        for index, receipt in enumerate(claim.review_receipts)
    )
    return service.store.put_claim(
        replace(claim, review_receipts=receipts), expected_revision=claim.revision
    )


def _payload(output):
    body = output.split("```json", 1)[1].split("```", 1)[0]
    return json.loads(body)


@pytest.mark.asyncio
async def test_pending_candidate_exposes_its_id(tmp_path):
    service = _enabled(tmp_path)
    claim, _ = await _write(service)

    output = await service.recall()

    assert claim.id in output


@pytest.mark.asyncio
async def test_pending_digest_points_at_delete_id(tmp_path):
    service = _enabled(tmp_path)
    await _write(service)

    output = await service.recall()

    assert "delete_id" in output


@pytest.mark.asyncio
async def test_a_candidate_id_can_actually_be_withdrawn(tmp_path):
    service = _enabled(tmp_path)
    claim, _ = await _write(service)

    assert claim.id in await service.recall()
    await service.delete(claim.id)

    assert claim.id not in await service.recall()


@pytest.mark.asyncio
async def test_live_claim_exposes_its_id(tmp_path):
    service = _enabled(tmp_path)
    claim = None
    for _ in range(REQUIRED_CONFIRMATIONS):
        claim, _ = await _write(service)
        claim = _age_receipts(service, service.store.get_claim(service.status().scope, claim.id))
    await _write(service)

    payload = _payload(await service.recall())

    notes = payload["them"][0]["notes"]
    assert notes
    for note in notes:
        assert note["claim_id"]


@pytest.mark.asyncio
async def test_a_live_claim_id_can_actually_be_withdrawn(tmp_path):
    service = _enabled(tmp_path)
    claim = None
    for _ in range(REQUIRED_CONFIRMATIONS):
        claim, _ = await _write(service)
        claim = _age_receipts(service, service.store.get_claim(service.status().scope, claim.id))
    await _write(service)

    payload = _payload(await service.recall())
    claim_id = payload["them"][0]["notes"][0]["claim_id"]

    await service.delete(claim_id)

    assert "她讲话直奔结论" not in await service.recall()
