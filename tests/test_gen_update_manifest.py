"""update_manifest.json 生成器 + 与 do-update 校验器的往返测试（谨慎加固 B1）。

生成器产出的清单，必须能被 web/meta._plan_update_files 逐文件 sha256 校验通过。
这证明「发布侧生成清单」真正接上了「用户侧完整性校验」。
"""
import importlib.util
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import web.meta as meta

_GEN_PATH = Path(__file__).resolve().parents[1] / "deploy" / "gen_update_manifest.py"
_spec = importlib.util.spec_from_file_location("gen_update_manifest", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

_TOP = "Ombre-Brain-main/"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="清单按 Git 仓库存储字节生成，需要 git"
)


def _fake_repo(tmp_path):
    # 必须是真的 Git 仓库：清单描述的是仓库里的字节，不是磁盘上的字节
    # （见 deploy/gen_update_manifest.repo_bytes 的事故说明）。
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "src" / "server.py").write_bytes(b"print('hello')\n")
    (tmp_path / "src" / "util.py").write_bytes(b"x = 1\n")
    (tmp_path / "frontend" / "app.js").write_bytes(b"// js\n")
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    # 缓存产物必须被生成器忽略
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "server.cpython-312.pyc").write_bytes(b"junk")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    return tmp_path


def _archive_bytes(repo, rel):
    """取该文件在仓库里的字节——GitHub 源码归档携带的就是这一份。"""
    return gen.repo_bytes(str(repo), rel)


def test_manifest_excludes_pycache(tmp_path):
    repo = _fake_repo(tmp_path)
    m = gen.build_manifest(str(repo))
    paths = {e["path"] for e in m["files"]}
    assert paths == {"src/server.py", "src/util.py", "frontend/app.js"}
    assert m["version"] == "9.9.9"


def test_check_rejects_stale_manifest_version(tmp_path, monkeypatch):
    fresh = {
        "version": "9.9.9",
        "rollout_strategy": "single-node",
        "files": [],
    }
    manifest_path = tmp_path / "update_manifest.json"
    manifest_path.write_text(
        json.dumps({**fresh, "version": "9.9.8"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(gen, "_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setattr(gen, "build_manifest", lambda: fresh)

    assert gen.main(["gen_update_manifest.py", "--check"]) == 1


def test_generated_manifest_passes_do_update_verifier(tmp_path):
    repo = _fake_repo(tmp_path)
    manifest = gen.build_manifest(str(repo))

    # 用真实文件内容 + 生成的清单打一个 release zip
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for e in manifest["files"]:
            zf.writestr(_TOP + e["path"], _archive_bytes(repo, e["path"]))
        zf.writestr(_TOP + "update_manifest.json", json.dumps(manifest))
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        plan = meta._plan_update_files(zf, _TOP)

    assert plan["abort"] is None
    assert plan["verified"] is True
    assert set(plan["files"]) == {"src/server.py", "src/util.py", "frontend/app.js"}


def test_tampered_file_against_generated_manifest_aborts(tmp_path):
    repo = _fake_repo(tmp_path)
    manifest = gen.build_manifest(str(repo))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for e in manifest["files"]:
            content = _archive_bytes(repo, e["path"])
            if e["path"] == "src/server.py":
                content = b"print('TAMPERED')\n"   # 篡改一个文件
            zf.writestr(_TOP + e["path"], content)
        zf.writestr(_TOP + "update_manifest.json", json.dumps(manifest))
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        plan = meta._plan_update_files(zf, _TOP)

    assert plan["abort"] is not None
    assert plan["files"] == {}
