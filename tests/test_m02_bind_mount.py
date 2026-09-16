"""Strict bind-mount rejection and native Windows publication tests."""

from __future__ import annotations

import errno
import os

import pytest
import yaml

import config_transaction as ct
from config_transaction import (
    ConfigPersistenceError,
    UnsupportedAtomicBindMount,
    canonical_config_path,
    run_config_transaction,
)


def test_exact_linux_ebusy_mount_is_rejected_not_rewritten(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    old = b"value: old\n"
    path.write_bytes(old)
    monkeypatch.setattr(ct, "is_exact_linux_mount_point", lambda _path: True)
    monkeypatch.setattr(
        ct,
        "_write_through_replace",
        lambda _source, _target: (_ for _ in ()).throw(
            OSError(errno.EBUSY, "busy")
        ),
    )
    with pytest.raises(UnsupportedAtomicBindMount):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
        )
    assert path.read_bytes() == old


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EIO, "io"),
        OSError(errno.ENOENT, "missing"),
        OSError(errno.EXDEV, "cross-device"),
        PermissionError(errno.EACCES, "denied"),
    ],
)
def test_other_replace_errors_never_enter_bind_fallback(
    tmp_path,
    monkeypatch,
    error,
):
    path = tmp_path / "config.yaml"
    old = b"value: old\n"
    path.write_bytes(old)
    detector_calls = 0

    def detector(_path):
        nonlocal detector_calls
        detector_calls += 1
        return True

    monkeypatch.setattr(ct, "is_exact_linux_mount_point", detector)
    monkeypatch.setattr(
        ct,
        "_write_through_replace",
        lambda _source, _target: (_ for _ in ()).throw(error),
    )
    with pytest.raises(ConfigPersistenceError):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
        )
    assert path.read_bytes() == old
    assert detector_calls == 0


def test_ebusy_non_mount_is_not_rewritten(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    old = b"value: old\n"
    path.write_bytes(old)
    monkeypatch.setattr(ct, "is_exact_linux_mount_point", lambda _path: False)
    monkeypatch.setattr(
        ct,
        "_write_through_replace",
        lambda _source, _target: (_ for _ in ()).throw(
            OSError(errno.EBUSY, "busy")
        ),
    )
    with pytest.raises(ConfigPersistenceError):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
        )
    assert path.read_bytes() == old


def test_canonical_path_rejects_directory(tmp_path):
    with pytest.raises(ValueError, match="regular file"):
        canonical_config_path(tmp_path)


def test_symlink_config_target_is_rejected_without_mutating_referent(tmp_path):
    referent = tmp_path / "real.yaml"
    link = tmp_path / "config.yaml"
    referent.write_text("value: old\n", encoding="utf-8")
    try:
        link.symlink_to(referent)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="symlink"):
        run_config_transaction(
            link,
            lambda config: config.__setitem__("value", "new"),
        )
    assert referent.read_text(encoding="utf-8") == "value: old\n"


def test_native_windows_unicode_path_write_through_roundtrip(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows acceptance")
    path = tmp_path / "配置 空格" / "config.yaml"
    path.parent.mkdir()
    path.write_text("human: 旧值\n", encoding="utf-8")
    result = run_config_transaction(
        path,
        lambda config: config.__setitem__("human", "新值"),
    )
    assert result.persisted["human"] == "新值"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["human"] == "新值"
