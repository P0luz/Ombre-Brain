"""Regression coverage for bounded-memory GitHub backups."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

import github_sync as github_module
from github_sync import GitHubSync, _LazyMarkdownFiles
from ombrebrain.storage.source_store import SourceStore


def _response(method: str, url: str, status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request(method, url))


def test_collect_files_keeps_paths_not_all_file_bodies(tmp_path):
    dynamic = tmp_path / "dynamic"
    dynamic.mkdir()
    first = dynamic / "a.md"
    second = dynamic / "b.md"
    first.write_bytes(b"alpha")
    second.write_bytes(b"beta")

    sync = GitHubSync(token="t", repo="owner/repo")
    files = sync._collect_files(str(tmp_path))

    assert isinstance(files, Mapping)
    assert set(files) == {"dynamic/a.md", "dynamic/b.md"}
    # The production mapping indexes only filesystem paths.  Reading a value
    # reflects the current file, proving collection did not cache every body.
    first.write_bytes(b"changed")
    assert files["dynamic/a.md"] == b"changed"
    assert all(isinstance(path, str) for path in files._paths.values())


def test_collect_files_includes_content_addressed_source_evidence(tmp_path):
    ref = SourceStore(tmp_path).put("需要与 Markdown 一起备份的原文")
    sync = GitHubSync(token="t", repo="owner/repo")

    files = sync._collect_files(str(tmp_path))

    relative = f"_sources/{ref}.source"
    assert relative in files
    assert files[relative].decode() == "需要与 Markdown 一起备份的原文"
    manifest = sync._build_backup_manifest(files)
    assert [entry["path"] for entry in manifest["files"]] == [relative]


def test_collect_files_rejects_non_utf8_source_evidence(tmp_path):
    raw = b"\xff\xfeinvalid"
    import hashlib

    ref = f"src_{hashlib.sha256(raw).hexdigest()}"
    source_dir = tmp_path / "_sources"
    source_dir.mkdir()
    (source_dir / f"{ref}.source").write_bytes(raw)
    sync = GitHubSync(token="t", repo="owner/repo")
    files = sync._collect_files(str(tmp_path))

    with pytest.raises(RuntimeError, match="not UTF-8"):
        files[f"_sources/{ref}.source"]


def test_collect_files_streams_scandir_and_applies_count_cap(tmp_path, monkeypatch):
    for name in ("a.md", "b.md", "c.md"):
        (tmp_path / name).write_bytes(name.encode())
    sync = GitHubSync(token="t", repo="owner/repo")
    monkeypatch.setattr(github_module, "_MAX_BACKUP_FILES", 2)
    # A regression to os.walk would materialize the directory listing before
    # our cap and should also fail this explicit contract guard.
    monkeypatch.setattr(
        github_module.os,
        "walk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bounded collection must use scandir, not os.walk")
        ),
    )

    with pytest.raises(RuntimeError, match="more than 2 files"):
        sync._collect_files(str(tmp_path))


def test_lazy_read_stays_bounded_if_file_grows_after_stat(tmp_path, monkeypatch):
    source = tmp_path / "growing.md"
    source.write_bytes(b"123456")
    files = _LazyMarkdownFiles({"growing.md": str(source)})
    monkeypatch.setattr(github_module, "_MAX_FILE_BYTES", 5)
    # Simulate a race where the pre-open stat still reported the old size.
    monkeypatch.setattr(github_module.os.path, "getsize", lambda _path: 5)

    with pytest.raises(RuntimeError, match="grew beyond"):
        files["growing.md"]


@pytest.mark.asyncio
async def test_batch_commit_bounds_each_tree_request_by_decoded_bytes(monkeypatch):
    sync = GitHubSync(token="t", repo="owner/repo", path_prefix="ombre")
    monkeypatch.setattr(github_module, "_TREE_CHUNK_BYTES", 8)
    tree_payloads: list[dict] = []

    async def fake_request(_client, method, url, *, json=None, _max_retries=4):
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _response(method, url, 200, {"object": {"sha": "head"}})
        if method == "GET" and url.endswith("/git/commits/head"):
            return _response(method, url, 200, {"tree": {"sha": "base"}})
        if method == "POST" and url.endswith("/git/trees"):
            tree_payloads.append(json)
            return _response(method, url, 201, {"sha": f"tree-{len(tree_payloads)}"})
        if method == "POST" and url.endswith("/git/commits"):
            assert json["tree"] == "tree-3"
            return _response(method, url, 201, {"sha": "commit"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return _response(method, url, 200, {"ref": "refs/heads/main"})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    uploaded = await sync._batch_commit(
        {
            "dynamic/a.md": b"aaaa",
            "dynamic/b.md": b"bbbb",
            "dynamic/c.md": b"cccc",
            "dynamic/d.md": b"dddd",
        }
    )

    assert uploaded == 4
    assert len(tree_payloads) == 3
    uploaded_paths: list[str] = []
    for payload in tree_payloads[:-1]:
        file_entries = [
            entry
            for entry in payload["tree"]
            if not entry["path"].endswith("_ombre_backup_manifest.json")
        ]
        assert sum(len(entry.get("content", "").encode()) for entry in file_entries) <= 8
        uploaded_paths.extend(entry["path"] for entry in file_entries)
    assert uploaded_paths == [
        "ombre/dynamic/a.md",
        "ombre/dynamic/b.md",
        "ombre/dynamic/c.md",
        "ombre/dynamic/d.md",
    ]
    assert tree_payloads[0]["base_tree"] == "base"
    assert tree_payloads[1]["base_tree"] == "tree-1"
    assert tree_payloads[2]["base_tree"] == "tree-2"
    assert [
        entry["path"] for entry in tree_payloads[2]["tree"]
    ] == ["ombre/_ombre_backup_manifest.json"]
    assert sum(
        entry["path"].endswith("_ombre_backup_manifest.json")
        for payload in tree_payloads
        for entry in payload["tree"]
    ) == 1


def test_manifest_file_count_and_request_payload_are_hard_bounded(monkeypatch):
    sync = GitHubSync(token="t", repo="owner/repo")
    monkeypatch.setattr(github_module, "_MAX_BACKUP_FILES", 2)

    with pytest.raises(RuntimeError, match="more than 2 files"):
        sync._build_backup_manifest({
            "a.md": b"a",
            "b.md": b"b",
            "c.md": b"c",
        })


@pytest.mark.asyncio
async def test_manifest_payload_limit_fails_before_any_tree_write(monkeypatch):
    sync = GitHubSync(token="t", repo="owner/repo")
    monkeypatch.setattr(github_module, "_MAX_MANIFEST_PAYLOAD_BYTES", 1)
    tree_writes = 0

    async def fake_request(_client, method, url, *, json=None, _max_retries=4):
        nonlocal tree_writes
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _response(method, url, 200, {"object": {"sha": "head"}})
        if method == "GET" and url.endswith("/git/commits/head"):
            return _response(method, url, 200, {"tree": {"sha": "base"}})
        if method == "POST" and url.endswith("/git/trees"):
            tree_writes += 1
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)

    with pytest.raises(RuntimeError, match="manifest exceeds"):
        await sync._batch_commit({"a.md": b"a"})
    assert tree_writes == 0


@pytest.mark.asyncio
async def test_manual_and_scheduled_syncs_cannot_overlap(monkeypatch):
    sync = GitHubSync(token="t", repo="owner/repo")
    monkeypatch.setattr(sync, "_collect_files", lambda _root, _extra=None: {"a.md": b"x"})
    active = 0
    peak = 0

    async def slow_commit(_files):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return 1

    monkeypatch.setattr(sync, "_batch_commit", slow_commit)
    first, second = await asyncio.gather(sync.sync("unused"), sync.sync("unused"))

    assert first["ok"] is True
    assert second["ok"] is True
    assert peak == 1
