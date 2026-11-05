import json

import pytest

from import_memory import ImportEngine, ImportState
from tests.test_import_memory_regressions import FakeBucketManager, FakeDehydrator


@pytest.mark.asyncio
@pytest.mark.parametrize('pause', [False, True])
async def test_failed_extraction_retried_once_per_resume(tmp_path, monkeypatch, pause):
    import import_memory as im
    chunks = [{'content': f'fictional chunk {i}'} for i in range(3)]
    monkeypatch.setattr(im, 'chunk_turns', lambda *a, **k: list(chunks))
    manager = FakeBucketManager()
    engine = ImportEngine({'buckets_dir': str(tmp_path)}, manager, FakeDehydrator())
    seen = []
    failures = 2

    async def extract(content):
        nonlocal failures
        seen.append(content)
        if content == chunks[0]['content'] and failures:
            failures -= 1
            if pause and failures == 1:
                engine._paused = True
            raise RuntimeError('fictional provider failure')
        return [{'content': content}]

    monkeypatch.setattr(engine, '_extract_memories', extract)
    raw = 'Human: fictional text\nAssistant: fictional reply'
    first = await engine.start(raw, filename='fiction.txt')
    assert first['status'] == ('paused' if pause else 'partial')
    assert first.get('failed_chunk_indices') == [0]
    assert engine.state.can_resume
    second = await engine.start(raw, filename='fiction.txt', resume=True)
    assert second['status'] == 'partial'
    assert second['chunks_failed'] == 1
    assert second['processed'] == 3
    assert seen.count(chunks[0]['content']) == 2
    third = await engine.start(raw, filename='fiction.txt', resume=True)
    assert third['status'] == 'completed'
    assert third['chunks_failed'] == 0
    assert third['failed_chunk_indices'] == []
    assert third['memories_created'] == 3
    assert seen == [chunks[i]['content'] for i in ([0, 0, 1, 2, 0] if pause else [0, 1, 2, 0, 0])]
    assert len(manager.created) == 3
    fresh = await engine.start(raw, filename='fiction.txt', resume=False)
    assert fresh['status'] == 'completed'
    assert fresh['processed'] == 3
    assert fresh['memories_created'] == 0
    assert fresh['memories_skipped'] == 3


@pytest.mark.asyncio
async def test_partial_write_resume_uses_exact_body_dedup(tmp_path, monkeypatch, bucket_mgr):
    manager = bucket_mgr
    engine = ImportEngine({'buckets_dir': str(tmp_path)}, manager,
                          FakeDehydrator([{'content': 'fiction A'}, {'content': 'fiction B'}]))
    create = manager.create
    fail = True

    async def flaky(content, **kwargs):
        nonlocal fail
        if content == 'fiction B' and fail:
            fail = False
            raise OSError('fictional storage failure')
        return await create(content, **kwargs)

    monkeypatch.setattr(manager, 'create', flaky)
    raw = 'Human: fictional history'
    first = await engine.start(raw, filename='fiction.txt')
    assert first['status'] == 'partial'
    second = await engine.start(raw, filename='fiction.txt', resume=True)
    assert second['status'] == 'completed'
    assert second['chunks_failed'] == 0
    assert second['memories_created'] == 2
    assert second['memories_skipped'] == 1
    assert sorted(b['content'] for b in await manager.list_all()) == ['fiction A', 'fiction B']


def test_old_state_missing_failure_list_and_reset(tmp_path):
    state = ImportState(str(tmp_path))
    state.reset('fiction.txt', 'hash', 2)
    old = state.to_dict()
    old.pop('failed_chunk_indices', None)
    old.update(status='paused', processed=1)
    (tmp_path / 'import_state.json').write_text(json.dumps(old))
    state.data['failed_chunk_indices'] = [9]
    assert state.load()
    assert state.data['failed_chunk_indices'] == []
    assert state.can_resume
    state.reset('fiction.txt', 'hash', 2)
    assert state.data['failed_chunk_indices'] == []
