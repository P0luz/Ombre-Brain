"""#131 同类：vault 内 _app/ 等内部目录的 .md 不是记忆。

GitHub 同步上传 / 恢复、GitHub 导入前备份、I 的同内容查重都曾把它们当记忆：
恢复时用备份那一版覆盖运行时文档、导入计数虚高；I 每写一条候选都把 _app 解析一遍。
"""

import hashlib
import os
from types import SimpleNamespace

import frontmatter

import tools._runtime as rt
from github_sync import _is_backup_relative_path, _iter_backup_paths
from tools.i import core as i_core
from web.github import _should_back_up_before_import

_REF = "src_" + hashlib.sha256(b"x").hexdigest()


def _vault(root):
    files = {
        "dynamic/general/mem_m1.md": "---\nid: m1\n---\n记忆\n",
        "dynamic/_私密/mem_m2.md": "---\nid: m2\n---\n下划线 domain 里的记忆\n",
        "_app/docs/INTERNALS.md": "# 运行时文档\n",
        "_app/_prev/docs/INTERNALS.md": "# 上一版运行时文档\n",
        f"_sources/{_REF}.source": "x",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_github_restore_filter_skips_internal_markdown():
    assert _is_backup_relative_path("dynamic/general/mem_m1.md")
    assert _is_backup_relative_path("dynamic/_私密/mem_m2.md")
    assert _is_backup_relative_path(f"_sources/{_REF}.source")
    assert not _is_backup_relative_path("_app/docs/INTERNALS.md")
    assert not _is_backup_relative_path("_app/_prev/docs/INTERNALS.md")


def test_github_upload_walk_skips_internal_markdown(tmp_path):
    _vault(tmp_path)
    found = {
        os.path.relpath(p, tmp_path).replace(os.sep, "/")
        for p in _iter_backup_paths(str(tmp_path))
    }
    assert found == {
        "dynamic/general/mem_m1.md",
        "dynamic/_私密/mem_m2.md",
        f"_sources/{_REF}.source",
    }


def test_pre_import_backup_skips_internal_markdown():
    assert _should_back_up_before_import("dynamic/general/mem_m1.md")
    assert _should_back_up_before_import("dynamic/_私密/mem_m2.md")
    assert _should_back_up_before_import(f"_sources/{_REF}.source")
    assert _should_back_up_before_import(".you/you.sqlite3")
    assert not _should_back_up_before_import("_app/docs/INTERNALS.md")


def test_i_same_content_lookup_never_parses_app_tree(tmp_path, monkeypatch):
    _vault(tmp_path)
    # _app 里放一份正文完全相同、还带 I 标签的文件：不该被当成已有候选。
    (tmp_path / "_app" / "docs" / "dup.md").write_text(
        frontmatter.dumps(frontmatter.Post("我觉得慢一点没关系", tags=[i_core.I_CANDIDATE_TAG])),
        encoding="utf-8",
    )
    monkeypatch.setattr(rt, "bucket_mgr", SimpleNamespace(base_dir=str(tmp_path)), raising=False)
    loaded = []
    real_load = frontmatter.load
    monkeypatch.setattr(frontmatter, "load", lambda p, *a, **k: loaded.append(str(p)) or real_load(p, *a, **k))

    assert i_core._find_same_content("我觉得慢一点没关系") is None
    assert not [p for p in loaded if f"{os.sep}_app{os.sep}" in p]
    assert any(p.endswith("mem_m1.md") for p in loaded)
