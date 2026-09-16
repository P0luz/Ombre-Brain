import hashlib
import os
import threading
from pathlib import Path

import pytest

import windows_safe_rename as wsr


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows native contract")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_basic_preserves_object_and_bytes(tmp_path):
    source = tmp_path / "source.tmp"
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    source.write_bytes(b"historical-letter")
    expected = digest(source)
    before = source.stat().st_ino

    result = wsr.safe_rename_no_replace(source, target_dir, "letter.md", expected_sha256=expected)

    target = target_dir / "letter.md"
    assert not source.exists()
    assert target.read_bytes() == b"historical-letter"
    assert target.stat().st_ino == before == result.file_id
    assert result.source_sha256 == expected
    assert result.filesystem == "NTFS"


def test_existing_target_is_never_replaced(tmp_path):
    source = tmp_path / "source.tmp"
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = target_dir / "letter.md"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        wsr.safe_rename_no_replace(source, target_dir, target.name, expected_sha256=digest(source))

    assert source.read_bytes() == b"new"
    assert target.read_bytes() == b"old"


def test_digest_mismatch_fails_before_namespace_change(tmp_path):
    source = tmp_path / "source.tmp"
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    source.write_bytes(b"changed")

    with pytest.raises(wsr.SourceChangedError):
        wsr.safe_rename_no_replace(source, target_dir, "letter.md", expected_sha256="0" * 64)

    assert source.exists()
    assert not (target_dir / "letter.md").exists()


def test_rename_then_atomic_rewrite_uses_held_destination(tmp_path):
    source = tmp_path / "source.tmp"
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    source.write_bytes(b"type: archived")

    result = wsr.safe_rename_then_replace_bytes(
        source, target_dir, "letter.md", b"type: letter",
        expected_sha256=digest(source),
    )

    assert not source.exists()
    assert (target_dir / "letter.md").read_bytes() == b"type: letter"
    assert result.source_sha256 == hashlib.sha256(b"type: archived").hexdigest()


@pytest.mark.parametrize(
    "name",
    [
        "", ".", "..", "a/b", "a\\b", "trail.", "trail ",
        "stream:name", "CON", "nul.md", "COM1.txt", "bad?.md",
    ],
)
def test_rejects_non_component_target_names(tmp_path, name):
    source = tmp_path / "source.tmp"
    source.write_bytes(b"x")
    with pytest.raises(wsr.UnsafePathError):
        wsr.safe_rename_no_replace(source, tmp_path, name)
    assert source.exists()


def test_unsupported_rename_ex_version_fails_before_namespace_change(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.tmp"
    target = tmp_path / "target"
    source.write_bytes(b"x")
    target.mkdir()
    monkeypatch.setattr(
        wsr.sys, "getwindowsversion", lambda: type("Version", (), {"build": 15063})()
    )

    with pytest.raises(wsr.SafeRenameUnavailable):
        wsr.safe_rename_no_replace(source, target, "letter.md")

    assert source.exists()
    assert not (target / "letter.md").exists()


def test_two_writer_race_has_exactly_one_winner(tmp_path):
    for index in range(100):
        case = tmp_path / str(index)
        target_dir = case / "target"
        target_dir.mkdir(parents=True)
        barrier = threading.Barrier(3)
        results = {}

        def contender(name):
            source = case / f"source-{name}.tmp"
            source.write_bytes(name.encode())
            barrier.wait()
            try:
                wsr.safe_rename_no_replace(source, target_dir, "letter.md", expected_sha256=digest(source))
                results[name] = "won"
            except FileExistsError:
                results[name] = "lost"

        threads = [threading.Thread(target=contender, args=(name,)) for name in ("A", "B")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        assert sorted(results.values()) == ["lost", "won"]
        winner = next(name for name, result in results.items() if result == "won")
        loser = "B" if winner == "A" else "A"
        assert (target_dir / "letter.md").read_text() == winner
        assert (case / f"source-{loser}.tmp").read_text() == loser


def test_held_handles_defeat_source_and_destination_path_rebind(tmp_path, monkeypatch):
    destination = tmp_path / "destination"
    destination.mkdir()
    held_destination = tmp_path / "held-destination"
    source = tmp_path / "source.tmp"
    held_source_path = tmp_path / "held-source.tmp"
    source.write_bytes(b"held-authority")
    expected = digest(source)
    actual = wsr._nt_rename_no_replace

    def rebind_then_commit(source_handle, target_handle, target_name):
        os.replace(destination, held_destination)
        destination.mkdir()
        (destination / target_name).write_bytes(b"attacker-target-decoy")
        os.replace(source, held_source_path)
        source.write_bytes(b"attacker-source-decoy")
        actual(source_handle, target_handle, target_name)

    monkeypatch.setattr(wsr, "_nt_rename_no_replace", rebind_then_commit)
    result = wsr.safe_rename_no_replace(source, destination, "letter.md", expected_sha256=expected)

    assert result.source_sha256 == expected
    assert (held_destination / "letter.md").read_bytes() == b"held-authority"
    assert (destination / "letter.md").read_bytes() == b"attacker-target-decoy"
    assert source.read_bytes() == b"attacker-source-decoy"
    assert not held_source_path.exists()


def test_reparse_component_is_rejected_if_symlinks_are_available(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")
    source = tmp_path / "source.tmp"
    source.write_bytes(b"x")

    with pytest.raises(wsr.UnsafePathError):
        wsr.safe_rename_no_replace(source, link, "letter.md", expected_sha256=digest(source))

    assert source.exists()
    assert not (real / "letter.md").exists()


def test_source_reparse_is_rejected_if_symlinks_are_available(tmp_path):
    real_source = tmp_path / "real-source.tmp"
    real_source.write_bytes(b"x")
    link_source = tmp_path / "source-link.tmp"
    try:
        os.symlink(real_source, link_source)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")
    target_dir = tmp_path / "target"
    target_dir.mkdir()

    with pytest.raises(wsr.UnsafePathError):
        wsr.safe_rename_no_replace(link_source, target_dir, "letter.md", expected_sha256=digest(real_source))

    assert link_source.is_symlink()
    assert real_source.read_bytes() == b"x"
    assert not (target_dir / "letter.md").exists()


def test_ensure_directory_creates_plain_child_relative_to_pinned_parent(tmp_path):
    parent = tmp_path / "letters"
    parent.mkdir()

    wsr.safe_ensure_directory(parent, "history")
    wsr.safe_ensure_directory(parent, "history")

    assert (parent / "history").is_dir()


def test_ensure_directory_rejects_existing_reparse_child(tmp_path):
    parent = tmp_path / "letters"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    link = parent / "history"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(wsr.UnsafePathError):
        wsr.safe_ensure_directory(parent, "history")

    assert list(outside.iterdir()) == []
