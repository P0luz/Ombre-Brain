import pytest

from bucket_manager import BucketManager
from ombrebrain.storage.embedding_outbox import EmbeddingOutbox
from tests.test_embedding_outbox import (_config, PoisonEngine, FailingEngine,
                                  _POISON_CONTENT, RecordingEngine)


@pytest.mark.asyncio
@pytest.mark.parametrize('threshold', [None, 0])
async def test_default_poison_limit_and_explicit_zero(tmp_path, monkeypatch, threshold):
    import ombrebrain.storage.embedding_outbox as module
    now = [100000.0]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    options = {} if threshold is None else {'abandon_after_failures': threshold}
    config = _config(tmp_path, **options)
    engine = PoisonEngine()
    manager = BucketManager(config, embedding_engine=engine)
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)
    outbox._running = True
    poison = await manager.create(_POISON_CONTENT)
    for i in range(12):
        # Process a real successful item before each poison retry.
        await manager.create(f'fictional good item {i}')
        while await outbox.process_once():
            pass
        now[0] += 4000
    assert outbox.abandon_after_failures == (10 if threshold is None else 0)
    assert outbox.status()['abandoned'] == (1 if threshold is None else 0)
    assert outbox._items[poison]['content_strikes'] == (10 if threshold is None else 11)
    if threshold is None:
        engine.cured = True
        outbox.set_embedding_engine(RecordingEngine())
        assert outbox.status()['abandoned'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 3, 5])
async def test_global_failure_exponential_backoff_and_success_reset(tmp_path, monkeypatch, count):
    import ombrebrain.storage.embedding_outbox as module
    now = [100000.0]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    config = {'buckets_dir': str(tmp_path / 'vault'), 'embedding': {}}
    engine = FailingEngine()
    manager = BucketManager(config, embedding_engine=engine)
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)
    outbox._running = True
    for i in range(count):
        await manager.create(f'fictional failure {i}')
    for expected in [300, 600, 1200, 2400, 3600, 3600]:
        for _ in range(min(count, 3) if expected == 300 else count):
            assert await outbox.process_once()
        assert outbox.status()['next_retry_at'] - now[0] == expected
        assert not await outbox.process_once()
        now[0] += expected
    assert outbox.status()['abandoned'] == 0
    # Provider repairs itself without engine replacement or manual retry.
    engine.generate_and_store = RecordingEngine().generate_and_store
    assert await outbox.process_once()
    assert outbox._circuit_delay() == 0
    assert outbox._consecutive_failures == 0
    engine.generate_and_store = FailingEngine().generate_and_store
    await manager.create('fictional failure after recovery')
    while await outbox.process_once():
        pass
    assert outbox.status()['next_retry_at'] - now[0] == 300


@pytest.mark.parametrize('override,expected', [(None, 10), ('0', 0), ('4', 4)])
def test_loaded_config_effective_abandon_default_and_env(tmp_path, monkeypatch, override, expected):
    from utils import load_config

    monkeypatch.setenv('OMBRE_VAULT_DIR', str(tmp_path / 'vault'))
    monkeypatch.setenv('OMBRE_BUCKETS_DIR', str(tmp_path / 'vault'))
    monkeypatch.delenv('OMBRE_EMBED_ABANDON_AFTER_FAILURES', raising=False)
    if override is not None:
        monkeypatch.setenv('OMBRE_EMBED_ABANDON_AFTER_FAILURES', override)
    path = tmp_path / 'config.yaml'
    path.write_text('embedding:\n  enabled: false\n')
    config = load_config(str(path))
    outbox = EmbeddingOutbox(config, object(), RecordingEngine())
    assert outbox.abandon_after_failures == expected
    assert outbox.circuit_base_seconds == 300
    assert outbox.circuit_max_seconds == 3600
