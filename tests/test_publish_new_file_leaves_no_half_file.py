from __future__ import annotations

import errno
import os

import pytest

from utils import publish_new_file

TEXT = "---\nid: probe\n---\n" + "一整条记忆的正文。" * 200


@pytest.fixture
def no_hardlinks(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "link", refuse)


@pytest.fixture
def staged(tmp_path):
    source = tmp_path / "staged.tmp"
    source.write_text(TEXT, encoding="utf-8")
    return str(source), str(tmp_path / "bucket.md")


def _break_writes(monkeypatch, code):
    real_fdopen = os.fdopen

    def broken(fd, *args, **kwargs):
        handle = real_fdopen(fd, *args, **kwargs)
        real_write = handle.write

        def write(chunk):
            real_write(chunk[: len(chunk) // 3])
            raise OSError(code, os.strerror(code))

        handle.write = write
        return handle

    monkeypatch.setattr(os, "fdopen", broken)


def test_hardlink_path_publishes_the_whole_file(staged, tmp_path):
    source, target = staged
    publish_new_file(source, target, TEXT)
    assert open(target, encoding="utf-8").read() == TEXT


def test_fallback_path_publishes_the_whole_file(staged, no_hardlinks):
    source, target = staged
    publish_new_file(source, target, TEXT)
    assert open(target, encoding="utf-8").read() == TEXT


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT, errno.EIO])
def test_failed_fallback_write_leaves_no_file_behind(
    staged, no_hardlinks, monkeypatch, code
):
    source, target = staged
    _break_writes(monkeypatch, code)

    with pytest.raises(OSError):
        publish_new_file(source, target, TEXT)

    assert not os.path.exists(target)


def test_existing_target_is_never_clobbered(staged, no_hardlinks):
    source, target = staged
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("别人的文件")

    with pytest.raises(FileExistsError):
        publish_new_file(source, target, TEXT)

    assert open(target, encoding="utf-8").read() == "别人的文件"


def test_existing_target_survives_a_failed_write(staged, no_hardlinks, monkeypatch):
    source, target = staged
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("别人的文件")
    _break_writes(monkeypatch, errno.ENOSPC)

    with pytest.raises(FileExistsError):
        publish_new_file(source, target, TEXT)

    assert open(target, encoding="utf-8").read() == "别人的文件"
