"""OOM 报告要带真实读数，不能只有「内存持续增长」。

上游报了 Render 512MB 上 OOM。仓库里几个最可疑的地方查下来都是有界的
（语义检索走 fetchmany(32) + O(top_k) 堆、模块级无常驻缓存、metrics 无状态），
再往下就是猜。所以先把读数装上，下一次报告能带上 diagnostics 的 memory 段。

这里守两件事：容器上限必须从 cgroup 取（**不是** /proc/meminfo，容器里后者
报的是宿主机内存，照它算会得出「用了 2%」而进程正在被 OOM killer 杀掉），
以及读不到时要安静降级、不能让整个诊断页 500。
"""

import pytest

from ombrebrain.observability import process_memory as pm


def test_container_limit_comes_from_cgroup_not_host_memory(monkeypatch, tmp_path):
    v2 = tmp_path / "memory.max"
    v2.write_text("536870912", encoding="utf-8")
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(v2))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(tmp_path / "nope"))

    assert pm.memory_limit_bytes() == 536870912


def test_cgroup_v1_is_read_when_v2_is_absent(monkeypatch, tmp_path):
    v1 = tmp_path / "memory.limit_in_bytes"
    v1.write_text("268435456", encoding="utf-8")
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(tmp_path / "nope"))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(v1))

    assert pm.memory_limit_bytes() == 268435456


@pytest.mark.parametrize("sentinel", ["max", str(1 << 62), str(9223372036854771712)])
def test_no_limit_sentinels_are_not_reported_as_a_limit(monkeypatch, tmp_path, sentinel):
    """没有限制时 v2 写 "max"、v1 写一个接近 2^63 的数。

    把这些当成真上限的话，used_percent 会算出一个荒唐的小数字，
    而那正是这个字段要防的误导。
    """
    path = tmp_path / "limit"
    path.write_text(sentinel, encoding="utf-8")
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(path))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(tmp_path / "nope"))

    assert pm.memory_limit_bytes() is None


def test_snapshot_degrades_quietly_where_proc_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_STATUS", str(tmp_path / "nope"))

    report = pm.snapshot()

    assert report["available"] is False
    assert "reason" in report


def test_snapshot_reports_percent_against_the_container_limit(monkeypatch, tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:	python" + chr(10) + "VmRSS:	   262144 kB" + chr(10), encoding="utf-8")
    limit = tmp_path / "memory.max"
    limit.write_text("536870912", encoding="utf-8")
    monkeypatch.setattr(pm, "_STATUS", str(status))
    monkeypatch.setattr(pm, "_CGROUP_V2_MAX", str(limit))
    monkeypatch.setattr(pm, "_CGROUP_V1_MAX", str(tmp_path / "nope"))

    report = pm.snapshot()

    assert report["available"] is True
    assert report["rss_mb"] == 256.0
    assert report["limit_mb"] == 512.0
    assert report["used_percent"] == 50.0


def test_object_types_are_not_computed_unless_asked(monkeypatch, tmp_path):
    """gc.get_objects() 的开销不该压在每次打开诊断页上。"""
    status = tmp_path / "status"
    status.write_text("VmRSS:	   1024 kB" + chr(10), encoding="utf-8")
    monkeypatch.setattr(pm, "_STATUS", str(status))

    assert "top_object_types" not in pm.snapshot()
    assert "top_object_types" in pm.snapshot(include_objects=True)
