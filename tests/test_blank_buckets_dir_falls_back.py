from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from utils import load_config


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for name in ("OMBRE_BUCKETS_DIR", "OMBRE_VAULT_DIR", "OMBRE_MEDIA_DIR"):
        monkeypatch.delenv(name, raising=False)


def _write(tmp_path, mapping):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(mapping, allow_unicode=True), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_buckets_dir_does_not_become_the_working_directory(
    tmp_path, monkeypatch, blank
):
    monkeypatch.chdir(tmp_path)
    config = load_config(_write(tmp_path, {"buckets_dir": blank}))

    assert str(config["buckets_dir"]).strip()
    assert not (tmp_path / "dynamic").exists()
    assert not (tmp_path / "permanent").exists()


def test_a_real_buckets_dir_is_still_honoured(tmp_path):
    wanted = tmp_path / "我的记忆库"
    config = load_config(_write(tmp_path, {"buckets_dir": str(wanted)}))

    assert Path(config["buckets_dir"]) == wanted
    assert (wanted / "dynamic").is_dir()


def test_media_dir_follows_the_repaired_buckets_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(_write(tmp_path, {"buckets_dir": ""}))

    assert os.path.commonpath(
        [str(config["media_dir"]), str(config["buckets_dir"])]
    ) == str(config["buckets_dir"])
