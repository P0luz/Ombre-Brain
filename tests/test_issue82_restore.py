import pytest

from tests.test_footprint import _install_runtime
from tools.trace.core import trace_core


@pytest.mark.asyncio
@pytest.mark.parametrize('changes,importance,warning', [
    ({'resolved': 0}, 1, False),
    ({'importance': 5}, 1, False),
    ({'resolved': 0, 'importance': 5}, 1, False),
    ({}, 1, True),
    ({}, 5, False),
])
async def test_restore_options_survive_real_decay(bucket_mgr, decay_eng, monkeypatch, changes, importance, warning):
    bid = await bucket_mgr.create('fictional resolved memory', importance=importance)
    await bucket_mgr.update(bid, resolved=True)
    await bucket_mgr.archive(bid)
    _install_runtime(bucket_mgr, decay_eng)
    commits = []
    commit = bucket_mgr._commit_bucket_update

    def record(*args, **kwargs):
        commits.append(args)
        return commit(*args, **kwargs)

    monkeypatch.setattr(bucket_mgr, '_commit_bucket_update', record)
    result = await trace_core(bid, restore=True, **changes)
    assert ('下次衰减可能再次归档' in result) is warning
    assert len(commits) == 1
    meta = (await bucket_mgr.get(bid))['metadata']
    assert meta['resolved'] == bool(changes.get('resolved', 1))
    assert meta['importance'] == changes.get('importance', importance)
    await decay_eng.run_decay_cycle()
    after = await bucket_mgr.get_including_archive(bid)
    assert (after['metadata']['type'] == 'archived') is warning


@pytest.mark.asyncio
async def test_restore_metadata_changes_roll_back_on_storage_failure(bucket_mgr, decay_eng, monkeypatch):
    from errors import ToolInputError

    bid = await bucket_mgr.create('fictional rollback memory', importance=1)
    await bucket_mgr.update(bid, resolved=True)
    await bucket_mgr.archive(bid)
    before = await bucket_mgr.get_including_archive(bid)
    _install_runtime(bucket_mgr, decay_eng)

    def fail_commit(*args, **kwargs):
        raise OSError('fictional disk failure')

    monkeypatch.setattr(bucket_mgr, '_commit_bucket_update', fail_commit)
    with pytest.raises(ToolInputError, match='恢复记忆桶失败'):
        await trace_core(bid, restore=True, importance=5, resolved=0)
    after = await bucket_mgr.get_including_archive(bid)
    assert after['path'] == before['path']
    assert after['metadata'] == before['metadata']
    assert after['content'] == before['content']
