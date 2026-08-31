"""热更新完整性清单必须按「仓库存储字节」生成，而不是按工作区字节。

真实事故：清单首次入库后，热更新在 frontend/onboarding.html 上整包中止——
清单在 core.autocrlf=true 的 Windows 工作区生成，记的是 CRLF 大小（10819），
而 GitHub 源码归档携带的是仓库里的 LF 内容（10621），206 个文件里 170 个对不上。

这里用临时 git 仓库精确复现两种会被算错的形态：
- index 存 LF、工作区是 CRLF（Windows 检出的常态）→ 必须记 LF 的大小
- index 存的就是 CRLF（本仓库有 9 个这样的文件）→ 不许被「文本一律归一为 LF」
  这种启发式反向算错
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "deploy" / "gen_update_manifest.py"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="需要 git 才能读仓库存储字节"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_update_manifest", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "VERSION").write_bytes(b"9.9.9\n")
    _git(tmp_path, "add", "VERSION")
    _git(tmp_path, "commit", "-q", "-m", "version")
    return tmp_path


def _entry(manifest: dict, path: str) -> dict:
    return next(item for item in manifest["files"] if item["path"] == path)


def test_manifest_records_lf_bytes_when_worktree_is_crlf(repo: Path):
    """仅 clean/autocrlf 转换不算未暂存改动，清单仍必须记 index 的 LF。"""
    module = _load_module()
    target = repo / "src" / "app.py"

    # 显式配置转换规则，避免测试结果依赖执行机的全局 Git 配置。
    _git(repo, "config", "core.autocrlf", "true")
    target.write_bytes(b"line one\nline two\nline three\n")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "lf")
    # 模拟 autocrlf 检出：磁盘变 CRLF，index 仍是 LF。某些平台的
    # diff-files 会把它列为候选，门禁必须用 hash-object --path 再按 clean
    # 过滤器核对，不能把这种纯检出转换误判为真实未暂存内容。
    target.write_bytes(b"line one\r\nline two\r\nline three\r\n")

    manifest = module.build_manifest(str(repo))
    entry = _entry(manifest, "src/app.py")

    lf = b"line one\nline two\nline three\n"
    assert entry["size"] == len(lf) == 29
    assert entry["sha256"] == hashlib.sha256(lf).hexdigest()
    # 工作区那份多 3 个 \r，绝不能是它
    assert entry["size"] != target.stat().st_size


def test_manifest_keeps_crlf_when_repository_stores_crlf(repo: Path):
    """仓库里存的就是 CRLF 时，不许被归一化成 LF 反向算错。"""
    module = _load_module()
    target = repo / "frontend" / "widget.html"
    crlf = b"<div>\r\n</div>\r\n"

    target.write_bytes(crlf)
    _git(repo, "-c", "core.autocrlf=false", "add", "frontend/widget.html")
    _git(repo, "commit", "-q", "-m", "crlf")

    manifest = module.build_manifest(str(repo))
    entry = _entry(manifest, "frontend/widget.html")

    stored = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:frontend/widget.html"],
        capture_output=True,
        check=True,
    ).stdout
    assert stored == crlf, "前置条件：这个文件在仓库里应存为 CRLF"
    assert entry["size"] == len(crlf)
    assert entry["sha256"] == hashlib.sha256(crlf).hexdigest()


def test_manifest_refuses_files_missing_from_the_repository(repo: Path):
    """没入库的文件宁可报错，也不能拿工作区字节顶上——那正是事故的来源。"""
    module = _load_module()
    (repo / "src" / "stray.py").write_bytes(b"print('hi')\n")

    with pytest.raises(module.ManifestSourceError) as excinfo:
        module.build_manifest(str(repo))
    assert "src/stray.py" in str(excinfo.value)


def test_manifest_refuses_unstaged_version_bump(repo: Path):
    """版本已改但未 add 时必须中止，不能把工作区版本与旧 index 哈希混在一起。"""
    module = _load_module()
    src_version = repo / "src" / "VERSION"

    src_version.write_bytes(b"9.9.9\n")
    _git(repo, "add", "VERSION", "src/VERSION")
    (repo / "VERSION").write_bytes(b"10.0.0\n")
    src_version.write_bytes(b"10.0.0\n")

    with pytest.raises(module.ManifestSourceError) as excinfo:
        module.build_manifest(str(repo))

    message = str(excinfo.value)
    assert "VERSION" in message
    assert "src/VERSION" in message
    assert "git add" in message


def test_shipped_manifest_matches_repository_bytes():
    """仓库里现有的清单必须与 HEAD 的字节一致，否则热更新会整包中止。"""
    import json

    manifest_path = _REPO_ROOT / "update_manifest.json"
    if not manifest_path.exists():
        pytest.skip("仓库未携带完整性清单")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    mismatched = []
    for item in manifest["files"]:
        blob = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "show", f"HEAD:{item['path']}"],
            capture_output=True,
        )
        if blob.returncode != 0:
            continue  # 该文件尚未提交，交给发布前的 --check 把关
        data = blob.stdout
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            mismatched.append(item["path"])

    assert not mismatched, (
        f"{len(mismatched)} 个文件的清单记录与仓库字节不符，热更新会中止："
        f"{mismatched[:5]}。改完 src/frontend 后请重跑 deploy/gen_update_manifest.py。"
    )
