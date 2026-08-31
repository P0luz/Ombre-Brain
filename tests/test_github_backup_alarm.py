"""GitHub 自动备份连续失败计数回归测试（谨慎加固 A3）。

自动备份可能连挂几次而用户毫无察觉（以为有备份其实没有）。sync() 每次成功归零、
每次失败 +1；诊断面板据此在连续失败时升级为醒目告警。
"""
import pytest

from github_sync import GitHubSync


@pytest.fixture
def gh(monkeypatch):
    inst = GitHubSync(token="t", repo="owner/repo", branch="main", path_prefix="ombre")
    # 让 _collect_files 返回非空，避免走「无文件」的提前返回分支
    monkeypatch.setattr(inst, "_collect_files", lambda d, extra=None: {"a.md": b"x"})
    return inst


@pytest.mark.asyncio
async def test_failures_accumulate_and_reset(gh, monkeypatch):
    async def _boom(_files):
        raise RuntimeError("network down")

    monkeypatch.setattr(gh, "_batch_commit", _boom)
    for expected in (1, 2, 3):
        res = await gh.sync("/tmp/buckets")
        assert res["ok"] is False
        assert gh.consecutive_failures == expected
    assert gh.status()["consecutive_failures"] == 3

    # 一次成功后归零
    async def _ok(_files):
        return 1
    monkeypatch.setattr(gh, "_batch_commit", _ok)
    res = await gh.sync("/tmp/buckets")
    assert res["ok"] is True
    assert gh.consecutive_failures == 0
    assert gh.status()["consecutive_failures"] == 0


def test_status_exposes_counter(gh):
    assert "consecutive_failures" in gh.status()
    assert gh.status()["consecutive_failures"] == 0
