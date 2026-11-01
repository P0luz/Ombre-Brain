"""迁移工作区的清扫线程不该在测试进程里真的跑起来。

它是进程级 daemon，起来之后跑满整个 pytest 进程，每 60 秒按当时读到的
`_PARSED_WORKSPACE_TTL_SECONDS` 去删所有已注册 engine 的未应用工作区。而
`test_migrate_job_state.py` 会把那个模块级常量 monkeypatch 成 10 秒——线程按
自己的节拍醒、和测试顺序无关，撞上就是一次没人复现得了的删除。
"""

from __future__ import annotations

import threading

import migrate_engine

_THREAD_NAME = "ombre-migrate-workspace-sweeper"


def test_sweeper_is_pre_disabled_by_conftest():
    # conftest 在任何 engine 注册之前就把这个开关置位，于是注册时直接短路。
    assert migrate_engine._MIGRATE_SWEEPER_STARTED is True


def test_no_sweeper_thread_is_running():
    alive = [t.name for t in threading.enumerate() if t.name == _THREAD_NAME]
    assert alive == []


def test_registering_an_engine_does_not_start_it():
    class _Engine:
        def _expire_parsed_workspace(self, _now):
            return False

    migrate_engine._register_migrate_engine(_Engine())

    alive = [t.name for t in threading.enumerate() if t.name == _THREAD_NAME]
    assert alive == []
