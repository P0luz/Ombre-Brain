"""热更新完整性校验 + 路径保护回归测试（安全加固 #1）。

do-update 旧行为：逐条直写磁盘、零校验，整仓覆盖 src/。现在 `_plan_update_files`
先收集到内存、过滤受保护/越界路径、若含 update_manifest.json 则逐文件核对
sha256/size，校验失败整体中止（不落盘）。
"""
import hashlib
import io
import json
import zipfile

import httpx
import pytest

import web.meta as meta

_TOP = "Ombre-Brain-main/"


def _zip(members: dict) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_collects_src_and_frontend_no_manifest():
    zf = _zip({
        _TOP + "src/server.py": b"print('hi')",
        _TOP + "frontend/app.js": b"//js",
        _TOP + "README.md": b"ignored",   # 不在 src/frontend → 不收集
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert plan["abort"] is None
    assert plan["verified"] is False
    assert set(plan["files"]) == {"src/server.py", "frontend/app.js"}


def test_protected_paths_are_filtered():
    zf = _zip({
        _TOP + "src/server.py": b"ok",
        _TOP + "src/.env": b"SECRET=1",              # 受保护
        _TOP + "src/config.yaml": b"k: v",           # 受保护
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert "src/server.py" in plan["files"]
    assert "src/.env" not in plan["files"]
    assert "src/config.yaml" not in plan["files"]
    assert plan["skipped_unsafe"] >= 2


def test_manifest_valid_passes_and_marks_verified():
    body = b"print('v2')"
    zf = _zip({
        _TOP + "src/server.py": body,
        _TOP + "update_manifest.json": json.dumps({
            "version": "9.9.9",
            "files": [{
                "path": "src/server.py",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
            }],
        }).encode(),
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert plan["abort"] is None
    assert plan["verified"] is True
    assert plan["files"] == {"src/server.py": body}


def test_manifest_sha_mismatch_aborts_whole_update():
    zf = _zip({
        _TOP + "src/server.py": b"TAMPERED",
        _TOP + "update_manifest.json": json.dumps({
            "version": "9.9.9",
            "files": [{
                "path": "src/server.py",
                "sha256": hashlib.sha256(b"original").hexdigest(),
                "size": len(b"original"),
            }],
        }).encode(),
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert plan["abort"] is not None
    assert "sha256" in plan["abort"]
    assert plan["files"] == {}   # 一个字节都不落盘


def test_manifest_size_mismatch_aborts():
    body = b"12345"
    zf = _zip({
        _TOP + "src/server.py": body,
        _TOP + "update_manifest.json": json.dumps({
            "version": "1",
            "files": [{"path": "src/server.py", "sha256": hashlib.sha256(body).hexdigest(), "size": 999}],
        }).encode(),
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert plan["abort"] is not None and "大小" in plan["abort"]
    assert plan["files"] == {}


def test_manifest_mode_skips_unlisted_files():
    listed = b"listed"
    zf = _zip({
        _TOP + "src/server.py": listed,
        _TOP + "src/extra.py": b"not-in-manifest",
        _TOP + "update_manifest.json": json.dumps({
            "version": "1",
            "files": [{"path": "src/server.py", "sha256": hashlib.sha256(listed).hexdigest(), "size": len(listed)}],
        }).encode(),
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert plan["abort"] is None
    assert set(plan["files"]) == {"src/server.py"}   # extra.py 未列 → 不写
    assert plan["skipped_unlisted"] == 1


def test_broken_manifest_json_aborts():
    zf = _zip({
        _TOP + "src/server.py": b"ok",
        _TOP + "update_manifest.json": b"{not valid json",
    })
    plan = meta._plan_update_files(zf, _TOP)
    assert plan["abort"] is not None
    assert plan["files"] == {}


def test_duplicate_candidate_path_aborts():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(_TOP + "src/server.py", b"first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr(_TOP + "src/server.py", b"second")
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf:
        plan = meta._plan_update_files(zf, _TOP)

    assert "重复路径" in plan["abort"]
    assert plan["files"] == {}


def test_oversized_update_member_aborts(monkeypatch):
    monkeypatch.setattr(meta, "_MAX_UPDATE_MEMBER_BYTES", 4)
    zf = _zip({_TOP + "src/server.py": b"12345"})

    plan = meta._plan_update_files(zf, _TOP)

    assert "超过" in plan["abort"]
    assert plan["files"] == {}


def test_manifest_rejects_non_object_items():
    zf = _zip({
        _TOP + "src/server.py": b"ok",
        _TOP + "update_manifest.json": json.dumps({"files": ["src/server.py"]}).encode(),
    })

    plan = meta._plan_update_files(zf, _TOP)

    assert "无效文件项" in plan["abort"]
    assert plan["files"] == {}


def test_bounded_zip_member_rejects_duplicate_root_file():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(_TOP + "VERSION", b"1")
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr(_TOP + "VERSION", b"2")
    buf.seek(0)

    with zipfile.ZipFile(buf) as zf, pytest.raises(ValueError, match="重复路径"):
        meta._read_bounded_zip_member(zf, _TOP + "VERSION", 128)


@pytest.mark.asyncio
async def test_update_download_stream_enforces_actual_byte_limit(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(meta, "_MAX_UPDATE_ARCHIVE_BYTES", 10)

    def handler(_request):
        return httpx.Response(200, content=b"12345678901")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="64 MiB"):
            await meta._download_update_archive_to_file(
                client,
                "https://updates.example/archive.zip",
                str(tmp_path / "update.zip"),
            )


@pytest.mark.asyncio
async def test_update_download_streams_complete_archive_to_disk(tmp_path):
    payload = b"bounded-archive"

    def handler(_request):
        return httpx.Response(200, content=payload)

    destination = tmp_path / "update.zip"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        downloaded = await meta._download_update_archive_to_file(
            client,
            "https://updates.example/archive.zip",
            str(destination),
        )

    assert downloaded == len(payload)
    assert destination.read_bytes() == payload


def test_atomic_update_write_replaces_complete_file(tmp_path):
    target = tmp_path / "src" / "module.py"
    target.parent.mkdir()
    target.write_bytes(b"old")

    meta._atomic_write_bytes(str(target), b"new-complete")

    assert target.read_bytes() == b"new-complete"
    assert not list(target.parent.glob(".ob-update-*"))
