from pathlib import Path

from ombrebrain.maintenance.code_fingerprint import fingerprint_code_tree


def _tree(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "src" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "frontend" / "dashboard.html").write_text("<h1>one</h1>\n", encoding="utf-8")


def test_fingerprint_is_stable_and_content_sensitive(tmp_path):
    _tree(tmp_path)

    first = fingerprint_code_tree(tmp_path)
    second = fingerprint_code_tree(tmp_path)
    (tmp_path / "src" / "server.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = fingerprint_code_tree(tmp_path)

    assert first == second
    assert changed != first
    assert len(first) == 64


def test_fingerprint_includes_relative_paths(tmp_path):
    _tree(tmp_path)
    original = fingerprint_code_tree(tmp_path)
    source = tmp_path / "src" / "server.py"
    renamed = tmp_path / "src" / "renamed.py"
    source.rename(renamed)

    assert fingerprint_code_tree(tmp_path) != original


def test_fingerprint_ignores_runtime_bytecode(tmp_path):
    _tree(tmp_path)
    original = fingerprint_code_tree(tmp_path)
    cache = tmp_path / "src" / "__pycache__"
    cache.mkdir()
    (cache / "server.cpython-312.pyc").write_bytes(b"generated")

    assert fingerprint_code_tree(tmp_path) == original


# ============================================================
# 可选目录（docs / tools / kernel）必须计入指纹
#
# 它们会被 entrypoint.sh 的 SEED_DIRS_OPTIONAL 播种到运行时树，而 adr_requirements /
# preflight_cli_diagnostics / vnext_preflight 三项诊断读的正是运行时树。不计入指纹
# 的话，改了这些文件指纹不变 → entrypoint 判定 image-match 跳过播种 → 运行时目录
# 永远停在第一次播种的版本，看起来像「改了没生效」。
# ============================================================

def test_optional_dirs_change_the_fingerprint(tmp_path):
    _tree(tmp_path)
    baseline = fingerprint_code_tree(tmp_path)

    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "ADR-0001.md").write_text("one\n", encoding="utf-8")
    with_docs = fingerprint_code_tree(tmp_path)
    assert with_docs != baseline, "新增 docs/ 必须改变指纹"

    (tmp_path / "docs" / "adr" / "ADR-0001.md").write_text("two\n", encoding="utf-8")
    assert fingerprint_code_tree(tmp_path) != with_docs, "改 docs/ 内容必须改变指纹"


def test_each_optional_dir_is_covered(tmp_path):
    _tree(tmp_path)
    seen = set()
    previous = fingerprint_code_tree(tmp_path)
    for name in ("docs", "tools", "kernel"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / name / "probe.txt").write_text(f"{name}\n", encoding="utf-8")
        current = fingerprint_code_tree(tmp_path)
        assert current != previous, f"{name}/ 未计入指纹"
        seen.add(name)
        previous = current
    assert seen == {"docs", "tools", "kernel"}


def test_missing_optional_dirs_do_not_raise(tmp_path):
    """老镜像/老运行时没有这些目录，缺失只能跳过，不能让指纹计算失败。"""
    _tree(tmp_path)
    assert len(fingerprint_code_tree(tmp_path)) == 64


def test_missing_required_dir_still_raises(tmp_path):
    """必需目录缺失仍必须报错——那是真的镜像损坏。"""
    import pytest

    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        fingerprint_code_tree(tmp_path)
