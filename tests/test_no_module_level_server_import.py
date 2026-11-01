from __future__ import annotations

import re
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
PATTERN = re.compile(r"^(?:import\s+server\b|from\s+server\s+import\b)", re.M)


def _offenders() -> list[str]:
    bad = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if PATTERN.search(text):
            bad.append(path.name)
    return bad


def test_no_test_module_imports_server_at_top_level():
    offenders = _offenders()
    assert not offenders, (
        "这些测试文件在模块顶层 import server，会在收集阶段污染 tools/_runtime："
        f"{'、'.join(offenders)}。改成在测试函数内部导入。"
    )


@pytest.mark.parametrize("snippet", ["import server", "from server import trace"])
def test_the_guard_actually_catches_it(tmp_path, monkeypatch, snippet):
    fake = tmp_path / "test_fake_offender.py"
    fake.write_text(f"{snippet}\n", encoding="utf-8")
    monkeypatch.setattr(
        "tests.test_no_module_level_server_import.TESTS", tmp_path, raising=False
    )
    import tests.test_no_module_level_server_import as guard

    monkeypatch.setattr(guard, "TESTS", tmp_path)
    assert guard._offenders() == ["test_fake_offender.py"]
