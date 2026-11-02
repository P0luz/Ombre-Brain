import pytest

from tools.anchor import core as anchor_core


class BucketManager:
    async def get_stats(self):
        return {
            "permanent_count": 1,
            "dynamic_count": 2,
            "archive_count": 0,
            "feel_count": 1,
            "plan_count": 1,
            "letter_count": 1,
            "total_size_kb": 1.0,
        }

    async def list_all(self, include_archive=False):
        self.include_archive = include_archive
        return [
            _bucket("permanent-1", "permanent"),
            _bucket("dynamic-1", "dynamic"),
            _bucket("feel-1", "feel"),
            _bucket("plan-1", "plan"),
            _bucket("letter-1", "letter"),
            _bucket("self-1", "i"),
        ]


class DecayEngine:
    is_running = True

    async def ensure_started(self):
        return None

    @staticmethod
    def calculate_score(_metadata):
        return 1.0


def _bucket(bucket_id, bucket_type):
    return {
        "id": bucket_id,
        "content": f"{bucket_type} content",
        "metadata": {
            "type": bucket_type,
            "name": bucket_type,
            "domain": [bucket_type],
            "tags": [bucket_type],
            "valence": 0.5,
            "arousal": 0.3,
            "importance": 5,
        },
    }


@pytest.mark.asyncio
async def test_pulse_keeps_every_active_bucket_type_distinct(monkeypatch):
    monkeypatch.setattr(
        anchor_core.rt, "bucket_mgr", BucketManager(), raising=False
    )
    monkeypatch.setattr(
        anchor_core.rt, "decay_engine", DecayEngine(), raising=False
    )
    monkeypatch.setattr(
        anchor_core.rt, "embedding_engine", None, raising=False
    )

    result = await anchor_core.pulse()

    assert "📦 [permanent-1]" in result
    assert "💭 [dynamic-1]" in result
    assert "🫧 [feel-1]" in result
    assert "📋 [plan-1]" in result
    assert "💌 [letter-1]" in result
    assert "🪞 [self-1]" in result
    assert "=== I（1 条）===" in result
