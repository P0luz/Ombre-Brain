from __future__ import annotations

import json
from pathlib import Path

import pytest

import errors


@pytest.fixture
def error_log(tmp_path):
    errors.configure_errors_path(str(tmp_path))
    path = Path(errors._errors_path)
    yield path
    errors.configure_errors_path(str(tmp_path))


def _details(limit=50):
    return [entry.get("detail") for entry in errors.recent_errors(limit=limit)]


@pytest.mark.parametrize("keep", [0.3, 0.7, 0.95])
def test_a_record_written_after_a_truncated_tail_is_readable(error_log, keep):
    errors.record_error("OB-W001", "崩溃前的最后一条")
    raw = error_log.read_text(encoding="utf-8")
    error_log.write_text(raw[: int(len(raw) * keep)], encoding="utf-8", newline="")

    errors.record_error("OB-E004", "重启后的第一条")

    assert "重启后的第一条" in _details()


def test_the_truncated_record_is_the_only_casualty(error_log):
    for i in range(3):
        errors.record_error("OB-W001", f"完好第 {i} 条")
    raw = error_log.read_text(encoding="utf-8")
    error_log.write_text(raw + '{"code": "OB-W001", "detail": "被截断', encoding="utf-8", newline="")

    errors.record_error("OB-E004", "重启后的第一条")

    survived = _details()
    assert "重启后的第一条" in survived
    for i in range(3):
        assert f"完好第 {i} 条" in survived


def test_every_line_stays_valid_json(error_log):
    errors.record_error("OB-W001", "崩溃前")
    raw = error_log.read_text(encoding="utf-8")
    error_log.write_text(raw[: int(len(raw) * 0.6)], encoding="utf-8", newline="")
    errors.record_error("OB-E004", "重启后")

    lines = [line for line in error_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert json.loads(lines[-1])["detail"] == "重启后"


def test_a_healthy_log_gains_no_blank_lines(error_log):
    for i in range(4):
        errors.record_error("OB-W001", f"第 {i} 条")

    text = error_log.read_text(encoding="utf-8")
    assert "\n\n" not in text
    assert len([line for line in text.splitlines() if line.strip()]) == 4
