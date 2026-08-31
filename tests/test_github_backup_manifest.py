import base64
import hashlib
import json

import httpx
import pytest
import frontmatter

import github_sync as github_sync_mod
from github_sync import GitHubSync
from ombrebrain.storage.source_store import SourceStore
from ombrebrain.you import YouStore, validate_you_snapshot_bytes


def _json_response(method: str, url: str, status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request(method, url),
    )


def test_backup_manifest_records_hashes_counts_and_bytes():
    sync = GitHubSync(token="token", repo="owner/repo", branch="main", path_prefix="ombre")

    manifest = sync._build_backup_manifest({
        "dynamic/a.md": b"alpha",
        "permanent/b.md": "你好".encode("utf-8"),
    })

    assert manifest["schema_version"] == 1
    assert manifest["source"] == "ombre-brain"
    assert manifest["source_refs_complete"] is True
    assert manifest["repo"] == "owner/repo"
    assert manifest["branch"] == "main"
    assert manifest["path_prefix"] == "ombre"
    assert manifest["file_count"] == 2
    assert manifest["total_bytes"] == 5 + len("你好".encode("utf-8"))
    by_path = {item["path"]: item for item in manifest["files"]}
    assert by_path["dynamic/a.md"]["sha256"] == hashlib.sha256(b"alpha").hexdigest()
    assert by_path["dynamic/a.md"]["bytes"] == 5


def test_backup_manifest_refuses_dangling_source_reference():
    sync = GitHubSync(token="token", repo="owner/repo")
    missing_ref = "src_" + "b" * 64
    bucket = frontmatter.dumps(frontmatter.Post(
        "event",
        source_refs=[{"ref": missing_ref, "ranges": [[1, 1]]}],
    )).encode()

    with pytest.raises(RuntimeError, match="dangling source evidence"):
        sync._build_backup_manifest({"dynamic/a.md": bucket})


@pytest.mark.asyncio
async def test_sync_includes_transactional_you_snapshot(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    YouStore(vault).set_enabled(True, expected_revision=0)
    sync = GitHubSync(token="token", repo="owner/repo")
    captured = {}

    async def capture(files):
        captured.update({path: files[path] for path in files})
        return len(files)

    monkeypatch.setattr(sync, "_batch_commit", capture)
    result = await sync.sync(str(vault))

    assert result == {"ok": True, "uploaded": 1}
    assert set(captured) == {".you/you.sqlite3"}
    validate_you_snapshot_bytes(captured[".you/you.sqlite3"])


@pytest.mark.asyncio
async def test_import_restores_verified_you_snapshot(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_state = YouStore(source).set_enabled(True, expected_revision=0)
    snapshot_path = tmp_path / "you-snapshot.sqlite3"
    YouStore(source).snapshot_to(snapshot_path)
    snapshot_data = snapshot_path.read_bytes()
    relative = ".you/you.sqlite3"
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-08-18T00:00:00+00:00",
        "file_count": 1,
        "total_bytes": len(snapshot_data),
        "files": [{
            "path": relative,
            "bytes": len(snapshot_data),
            "sha256": hashlib.sha256(snapshot_data).hexdigest(),
        }],
    }
    manifest_data = json.dumps(manifest).encode()
    sync = GitHubSync(token="token", repo="owner/repo", path_prefix="ombre")

    async def fake_request(_client, method, url, **_kwargs):
        if url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(method, url, 200, {
                "truncated": False,
                "tree": [
                    {
                        "type": "blob",
                        "path": "ombre/_ombre_backup_manifest.json",
                        "sha": "manifest-sha",
                        "size": len(manifest_data),
                    },
                    {
                        "type": "blob",
                        "path": f"ombre/{relative}",
                        "sha": "you-sha",
                        "size": len(snapshot_data),
                    },
                ],
            })
        if url.endswith("/git/blobs/manifest-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(manifest_data).decode(),
            })
        if url.endswith("/git/blobs/you-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(snapshot_data).decode(),
            })
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    target = tmp_path / "target"
    result = await sync.import_from_github(str(target))

    assert result["ok"] is True
    assert result["you_restored"] is True
    restored = YouStore(target).get_state()
    assert restored.enabled is True
    assert restored.scope == source_state.scope


@pytest.mark.asyncio
async def test_batch_commit_includes_backup_manifest_without_counting_it(monkeypatch):
    sync = GitHubSync(token="token", repo="owner/repo", branch="main", path_prefix="ombre")
    tree_payloads = []

    async def fake_request(_client, method: str, url: str, *, json=None, _max_retries=4):
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if method == "GET" and url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "base-tree"}})
        if method == "POST" and url.endswith("/git/trees"):
            tree_payloads.append(json)
            paths = {entry["path"] for entry in json["tree"]}
            if len(tree_payloads) == 1:
                assert paths == {"ombre/dynamic/a.md"}
                assert json["base_tree"] == "base-tree"
                return _json_response(method, url, 201, {"sha": "file-tree"})
            assert paths == {"ombre/_ombre_backup_manifest.json"}
            assert json["base_tree"] == "file-tree"
            manifest_entry = json["tree"][0]
            manifest = json_module.loads(manifest_entry["content"])
            assert manifest["file_count"] == 1
            assert manifest["files"][0]["path"] == "dynamic/a.md"
            assert manifest["files"][0]["sha256"] == hashlib.sha256(b"alpha").hexdigest()
            return _json_response(method, url, 201, {"sha": "manifest-tree"})
        if method == "POST" and url.endswith("/git/commits"):
            assert json["tree"] == "manifest-tree"
            return _json_response(method, url, 201, {"sha": "commit-sha"})
        if method == "PATCH" and url.endswith("/git/refs/heads/main"):
            return _json_response(method, url, 200, {"ref": "refs/heads/main"})
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    json_module = json
    monkeypatch.setattr(sync, "_request", fake_request)

    uploaded = await sync._batch_commit({"dynamic/a.md": b"alpha"})

    assert uploaded == 1
    assert len(tree_payloads) == 2


@pytest.mark.asyncio
async def test_import_reads_backup_manifest_summary_when_present(monkeypatch, tmp_path):
    sync = GitHubSync(token="token", repo="owner/repo", branch="main", path_prefix="ombre")
    bucket_data = b"# hello"
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-07-02T00:00:00+00:00",
        "file_count": 1,
        "total_bytes": len(bucket_data),
        "files": [{
            "path": "dynamic/a.md",
            "bytes": len(bucket_data),
            "sha256": hashlib.sha256(bucket_data).hexdigest(),
        }],
    }
    manifest_data = json.dumps(manifest).encode("utf-8")

    async def fake_request(_client, method: str, url: str, *, json=None, _max_retries=4):
        if method == "GET" and url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if method == "GET" and url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if method == "GET" and url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(method, url, 200, {
                "truncated": False,
                "tree": [
                    {
                        "type": "blob",
                        "path": "ombre/_ombre_backup_manifest.json",
                        "sha": "manifest-sha",
                        "size": len(manifest_data),
                    },
                    {
                        "type": "blob",
                        "path": "ombre/dynamic/a.md",
                        "sha": "bucket-sha",
                        "size": len(bucket_data),
                    },
                ],
            })
        if method == "GET" and url.endswith("/git/blobs/manifest-sha"):
            data = base64.b64encode(manifest_data).decode()
            return _json_response(method, url, 200, {"encoding": "base64", "content": data})
        if method == "GET" and url.endswith("/git/blobs/bucket-sha"):
            data = base64.b64encode(bucket_data).decode()
            return _json_response(method, url, 200, {"encoding": "base64", "content": data})
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)

    result = await sync.import_from_github(str(tmp_path))

    assert result["ok"] is True
    assert result["imported"] == 1
    assert result["backup_manifest"] == {
        "present": True,
        "schema_version": 1,
        "generated_at": "2026-07-02T00:00:00+00:00",
        "file_count": 1,
        "total_bytes": 7,
    }
    assert (tmp_path / "dynamic" / "a.md").read_text(encoding="utf-8") == "# hello"


@pytest.mark.asyncio
async def test_import_restores_verified_source_evidence(monkeypatch, tmp_path):
    sync = GitHubSync(token="token", repo="owner/repo", branch="main", path_prefix="ombre")
    source_data = "GitHub 恢复的原文证据".encode()
    ref = f"src_{hashlib.sha256(source_data).hexdigest()}"
    relative = f"_sources/{ref}.source"
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00+00:00",
        "file_count": 1,
        "total_bytes": len(source_data),
        "files": [{
            "path": relative,
            "bytes": len(source_data),
            "sha256": hashlib.sha256(source_data).hexdigest(),
        }],
    }
    manifest_data = json.dumps(manifest).encode()

    async def fake_request(_client, method, url, **_kwargs):
        if url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(method, url, 200, {
                "truncated": False,
                "tree": [
                    {
                        "type": "blob",
                        "path": "ombre/_ombre_backup_manifest.json",
                        "sha": "manifest-sha",
                        "size": len(manifest_data),
                    },
                    {
                        "type": "blob",
                        "path": f"ombre/{relative}",
                        "sha": "source-sha",
                        "size": len(source_data),
                    },
                ],
            })
        if url.endswith("/git/blobs/manifest-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(manifest_data).decode(),
            })
        if url.endswith("/git/blobs/source-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(source_data).decode(),
            })
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync.import_from_github(str(tmp_path))

    assert result["ok"] is True
    assert result["imported"] == 1
    assert result["buckets_imported"] == 0
    assert result["sources_imported"] == 1
    assert SourceStore(tmp_path).read(ref) == source_data.decode()


@pytest.mark.parametrize("declares_complete", [False, True])
@pytest.mark.asyncio
async def test_restore_detects_dangling_source_references_before_writing(
    monkeypatch, tmp_path, declares_complete
):
    sync = GitHubSync(
        token="token", repo="owner/repo", branch="main", path_prefix="ombre"
    )
    missing_ref = "src_" + "d" * 64
    bucket_data = frontmatter.dumps(frontmatter.Post(
        "remote event",
        source_refs=[{"ref": missing_ref, "ranges": [[1, 1]]}],
    )).encode("utf-8")
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00+00:00",
        "file_count": 1,
        "total_bytes": len(bucket_data),
        "files": [{
            "path": "dynamic/a.md",
            "bytes": len(bucket_data),
            "sha256": hashlib.sha256(bucket_data).hexdigest(),
        }],
    }
    if declares_complete:
        manifest["source_refs_complete"] = True
    manifest_data = json.dumps(manifest).encode("utf-8")
    local = tmp_path / "dynamic" / "a.md"
    local.parent.mkdir()
    local.write_text("local original", encoding="utf-8")

    async def fake_request(_client, method, url, **_kwargs):
        if url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(method, url, 200, {
                "truncated": False,
                "tree": [
                    {
                        "type": "blob",
                        "path": "ombre/_ombre_backup_manifest.json",
                        "sha": "manifest-sha",
                        "size": len(manifest_data),
                    },
                    {
                        "type": "blob",
                        "path": "ombre/dynamic/a.md",
                        "sha": "bucket-sha",
                        "size": len(bucket_data),
                    },
                ],
            })
        if url.endswith("/git/blobs/manifest-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(manifest_data).decode(),
            })
        if url.endswith("/git/blobs/bucket-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(bucket_data).decode(),
            })
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync.import_from_github(str(tmp_path))

    if declares_complete:
        assert result["ok"] is False
        assert result["imported"] == 0
        assert "dangling references" in result["error"]
        assert local.read_text(encoding="utf-8") == "local original"
    else:
        assert result["ok"] is True
        assert "原文证据引用没有对应文件" in result["integrity_warning"]
        assert local.read_bytes() == bucket_data


@pytest.mark.asyncio
async def test_invalid_source_after_bucket_never_overwrites_markdown(
    monkeypatch, tmp_path
):
    sync = GitHubSync(
        token="token", repo="owner/repo", branch="main", path_prefix="ombre"
    )
    bucket_data = b"remote bucket"
    expected_source = b"valid source"
    source_data = b"tampered source"
    ref = f"src_{hashlib.sha256(expected_source).hexdigest()}"
    source_rel = f"_sources/{ref}.source"
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-07-28T00:00:00+00:00",
        "file_count": 2,
        "total_bytes": len(bucket_data) + len(source_data),
        "files": [
            {
                "path": "dynamic/a.md",
                "bytes": len(bucket_data),
                "sha256": hashlib.sha256(bucket_data).hexdigest(),
            },
            {
                "path": source_rel,
                "bytes": len(source_data),
                "sha256": hashlib.sha256(source_data).hexdigest(),
            },
        ],
    }
    manifest_data = json.dumps(manifest).encode()
    local = tmp_path / "dynamic" / "a.md"
    local.parent.mkdir()
    local.write_text("local original", encoding="utf-8")

    async def fake_request(_client, method, url, **_kwargs):
        if url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(method, url, 200, {
                "truncated": False,
                # Deliberately put the bucket before the failing source.
                "tree": [
                    {
                        "type": "blob",
                        "path": "ombre/_ombre_backup_manifest.json",
                        "sha": "manifest-sha",
                        "size": len(manifest_data),
                    },
                    {
                        "type": "blob",
                        "path": "ombre/dynamic/a.md",
                        "sha": "bucket-sha",
                        "size": len(bucket_data),
                    },
                    {
                        "type": "blob",
                        "path": f"ombre/{source_rel}",
                        "sha": "source-sha",
                        "size": len(source_data),
                    },
                ],
            })
        payloads = {
            "manifest-sha": manifest_data,
            "bucket-sha": bucket_data,
            "source-sha": source_data,
        }
        for sha, payload in payloads.items():
            if url.endswith(f"/git/blobs/{sha}"):
                return _json_response(method, url, 200, {
                    "encoding": "base64",
                    "content": base64.b64encode(payload).decode(),
                })
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync.import_from_github(str(tmp_path))

    assert result["ok"] is False
    assert "source evidence integrity mismatch" in str(result)
    assert local.read_text(encoding="utf-8") == "local original"


@pytest.mark.asyncio
async def test_manifest_reader_rejects_oversized_declared_blob_before_download(
    monkeypatch,
):
    sync = GitHubSync(token="token", repo="owner/repo")
    monkeypatch.setattr(github_sync_mod, "_MAX_MANIFEST_PAYLOAD_BYTES", 4)

    async def unexpected_request(*_args, **_kwargs):
        raise AssertionError("oversized manifest must be rejected before blob download")

    monkeypatch.setattr(sync, "_request", unexpected_request)
    result = await sync._read_backup_manifest_summary(
        object(), {"sha": "manifest-sha", "size": 5}
    )

    assert result["present"] is False
    assert "decoded-byte limit" in result["error"]


@pytest.mark.asyncio
async def test_manifest_reader_rejects_oversized_base64_text(monkeypatch):
    sync = GitHubSync(token="token", repo="owner/repo")
    monkeypatch.setattr(github_sync_mod, "_MAX_MANIFEST_BASE64_BYTES", 4)

    async def fake_request(_client, method, url, **_kwargs):
        return _json_response(
            method,
            url,
            200,
            {"encoding": "base64", "content": "A" * 5},
        )

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync._read_backup_manifest_summary(
        object(), {"sha": "manifest-sha", "size": 1}
    )

    assert result["present"] is False
    assert "base64 payload is too large" in result["error"]


@pytest.mark.asyncio
async def test_manifest_reader_rejects_invalid_base64(monkeypatch):
    sync = GitHubSync(token="token", repo="owner/repo")

    async def fake_request(_client, method, url, **_kwargs):
        return _json_response(
            method,
            url,
            200,
            {"encoding": "base64", "content": "%%%%"},
        )

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync._read_backup_manifest_summary(
        object(), {"sha": "manifest-sha", "size": 3}
    )

    assert result["present"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_manifest_reader_rejects_decoded_payload_over_limit(monkeypatch):
    sync = GitHubSync(token="token", repo="owner/repo")
    monkeypatch.setattr(github_sync_mod, "_MAX_MANIFEST_PAYLOAD_BYTES", 3)
    monkeypatch.setattr(github_sync_mod, "_MAX_MANIFEST_BASE64_BYTES", 100)

    async def fake_request(_client, method, url, **_kwargs):
        return _json_response(
            method,
            url,
            200,
            {
                "encoding": "base64",
                "content": base64.b64encode(b"1234").decode(),
            },
        )

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync._read_backup_manifest_summary(
        object(), {"sha": "manifest-sha", "size": 3}
    )

    assert result["present"] is False
    assert "decoded-byte limit" in result["error"]


@pytest.mark.asyncio
async def test_restore_rejects_truncated_tree_before_fetching_blobs(
    monkeypatch, tmp_path
):
    sync = GitHubSync(
        token="token", repo="owner/repo", branch="main", path_prefix="ombre"
    )
    calls = []

    async def fake_request(_client, method, url, **_kwargs):
        calls.append((method, url))
        if url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(
                method,
                url,
                200,
                {
                    "truncated": True,
                    "tree": [{
                        "type": "blob",
                        "path": "ombre/dynamic/a.md",
                        "sha": "bucket-sha",
                        "size": 7,
                    }],
                },
            )
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync.import_from_github(str(tmp_path))

    assert result["ok"] is False
    assert "truncated tree" in result["error"]
    assert all("/git/blobs/" not in url for _method, url in calls)
    assert not (tmp_path / "dynamic" / "a.md").exists()


@pytest.mark.asyncio
async def test_restore_manifest_hash_mismatch_does_not_overwrite_local_file(
    monkeypatch, tmp_path
):
    sync = GitHubSync(
        token="token", repo="owner/repo", branch="main", path_prefix="ombre"
    )
    remote_data = b"remote replacement"
    manifest = {
        "schema_version": 1,
        "generated_at": "2026-07-15T00:00:00+00:00",
        "file_count": 1,
        "total_bytes": len(remote_data),
        "files": [{
            "path": "dynamic/a.md",
            "bytes": len(remote_data),
            "sha256": "0" * 64,
        }],
    }
    manifest_data = json.dumps(manifest).encode("utf-8")
    local = tmp_path / "dynamic" / "a.md"
    local.parent.mkdir()
    local.write_text("local original", encoding="utf-8")

    async def fake_request(_client, method, url, **_kwargs):
        if url.endswith("/git/ref/heads/main"):
            return _json_response(method, url, 200, {"object": {"sha": "head-sha"}})
        if url.endswith("/git/commits/head-sha"):
            return _json_response(method, url, 200, {"tree": {"sha": "tree-sha"}})
        if url.endswith("/git/trees/tree-sha?recursive=1"):
            return _json_response(method, url, 200, {
                "truncated": False,
                "tree": [
                    {
                        "type": "blob",
                        "path": "ombre/_ombre_backup_manifest.json",
                        "sha": "manifest-sha",
                        "size": len(manifest_data),
                    },
                    {
                        "type": "blob",
                        "path": "ombre/dynamic/a.md",
                        "sha": "bucket-sha",
                        "size": len(remote_data),
                    },
                ],
            })
        if url.endswith("/git/blobs/manifest-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(manifest_data).decode(),
            })
        if url.endswith("/git/blobs/bucket-sha"):
            return _json_response(method, url, 200, {
                "encoding": "base64",
                "content": base64.b64encode(remote_data).decode(),
            })
        raise AssertionError(f"Unexpected GitHub API call: {method} {url}")

    monkeypatch.setattr(sync, "_request", fake_request)
    result = await sync.import_from_github(str(tmp_path))

    assert result["ok"] is False
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert "integrity mismatch" in result["errors"][0]
    assert local.read_text(encoding="utf-8") == "local original"


def test_restore_manifest_rejects_entry_missing_from_tree():
    targets = {
        "dynamic/a.md": {
            "type": "blob",
            "path": "ombre/dynamic/a.md",
            "sha": "bucket-sha",
        }
    }

    with pytest.raises(RuntimeError, match="invalid entry"):
        GitHubSync._validate_restore_manifest(
            [{
                "path": "dynamic/missing.md",
                "bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
            }],
            targets,
        )


def test_restore_destination_rejects_traversal(tmp_path):
    base = tmp_path / "vault"
    base.mkdir()

    with pytest.raises(RuntimeError, match="unsafe restore path components"):
        GitHubSync._assert_safe_restore_destination(str(base), "../outside.md")


def test_restore_destination_rejects_symlink_parent(tmp_path):
    base = tmp_path / "vault"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    link = base / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    with pytest.raises(RuntimeError, match="symbolic link"):
        GitHubSync._assert_safe_restore_destination(str(base), "linked/escape.md")


def test_每个恢复标记都必须有人消费():
    """`github_sync` 每返回一个 `<模块>_restored`，web 层就必须把对应的工具门同步回去。

    这条守的是一个真实漏过的形状：3.5.0 给 GitHub 同步接 them 时，
    `github_sync` 里 `result["them_restored"] = True` 写了，
    `web/github.py` 那一侧却只处理 `you_restored`——磁盘上的开关已经恢复，
    当前进程的工具清单还停在旧状态，要重启才对得上。

    生产端本来就有测试（见上面 `you_restored is True` 那条），
    漏的是消费端，所以这里断言的是两侧的对称性：
    以后再加第四个模块，忘了接消费端同样会红。
    """
    import re
    from pathlib import Path

    repo = Path(__file__).parents[1]
    produced = set(
        re.findall(
            r'result\["(\w+)_restored"\]\s*=\s*True',
            (repo / "src" / "github_sync.py").read_text(encoding="utf-8"),
        )
    )
    assert produced, "没在 github_sync.py 里找到任何恢复标记，正则该更新了"

    web = (repo / "src" / "web" / "github.py").read_text(encoding="utf-8")
    漏掉的 = [
        模块
        for 模块 in sorted(produced)
        if f'result.pop("{模块}_restored"' not in web
        or f"{模块}_tool_gate.sync" not in web
    ]
    assert not 漏掉的, (
        f"{漏掉的} 的恢复标记没人消费：github_sync 返回了它，"
        "但 web/github.py 没有据此同步工具门，恢复后要重启才生效。"
    )
