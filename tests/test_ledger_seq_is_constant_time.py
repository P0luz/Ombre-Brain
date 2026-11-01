from __future__ import annotations

import json

import pytest

from ombrebrain.eventsourcing.ledger_mirror import LedgerMirror


def _mirror(tmp_path):
    return LedgerMirror(tmp_path / "ledger.jsonl")


def _append(mirror, n, body="正文"):
    for i in range(n):
        mirror.append_event(
            event_type="hold", trace_id=f"t{i}", trace_kind="k", body=body
        )


def test_empty_ledger_has_no_seq(tmp_path):
    assert _mirror(tmp_path).latest_seq() == 0


def test_seq_increments_by_one(tmp_path):
    mirror = _mirror(tmp_path)
    assert mirror.append_event(event_type="a", trace_id="1", trace_kind="k")["seq"] == 1
    assert mirror.append_event(event_type="a", trace_id="2", trace_kind="k")["seq"] == 2
    assert mirror.latest_seq() == 2


def test_seq_survives_a_reopen(tmp_path):
    _append(_mirror(tmp_path), 5)
    assert _mirror(tmp_path).latest_seq() == 5


def test_seq_reads_the_tail_not_the_maximum(tmp_path):
    # ledger 是追加的；最后一行就是最新的 seq
    mirror = _mirror(tmp_path)
    _append(mirror, 3)
    assert mirror.latest_seq() == 3


@pytest.mark.parametrize("truncated", ['{"seq": 99, "brok', "\x00\x00", "不是 JSON"])
def test_a_broken_tail_falls_back_to_the_previous_event(tmp_path, truncated):
    mirror = _mirror(tmp_path)
    _append(mirror, 4)
    with mirror.path.open("a", encoding="utf-8") as handle:
        handle.write(truncated)

    assert mirror.latest_seq() == 4
    assert mirror.append_event(event_type="a", trace_id="x", trace_kind="k")["seq"] == 5


def test_a_ledger_of_only_garbage_starts_from_zero(tmp_path):
    mirror = _mirror(tmp_path)
    mirror.path.parent.mkdir(parents=True, exist_ok=True)
    mirror.path.write_text("垃圾\n更多垃圾\n", encoding="utf-8")

    assert mirror.latest_seq() == 0


def test_an_event_larger_than_the_read_window_is_still_found(tmp_path):
    mirror = _mirror(tmp_path)
    _append(mirror, 1, body="x" * 20000)

    assert mirror.latest_seq() == 1


def test_every_appended_event_stays_readable(tmp_path):
    mirror = _mirror(tmp_path)
    _append(mirror, 30)

    seqs = [event["seq"] for event in mirror.iter_events()]
    assert seqs == list(range(1, 31))


def test_latest_seq_does_not_read_the_whole_ledger(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path)
    _append(mirror, 50)

    def refuse(self):
        raise AssertionError("latest_seq 又在全扫 ledger 了")

    monkeypatch.setattr(LedgerMirror, "iter_events", refuse)
    assert mirror.latest_seq() == 50


def test_appending_does_not_read_the_whole_ledger(tmp_path, monkeypatch):
    mirror = _mirror(tmp_path)
    _append(mirror, 50)

    def refuse(self):
        raise AssertionError("append_event 又在全扫 ledger 了")

    monkeypatch.setattr(LedgerMirror, "iter_events", refuse)
    assert mirror.append_event(event_type="a", trace_id="x", trace_kind="k")["seq"] == 51


def test_bytes_read_stay_bounded_as_the_ledger_grows(tmp_path):
    mirror = _mirror(tmp_path)

    def read_volume():
        seen = []
        real_open = type(mirror.path).open

        def counting_open(self, *args, **kwargs):
            handle = real_open(self, *args, **kwargs)
            real_read = handle.read

            def read(*a, **k):
                data = real_read(*a, **k)
                seen.append(len(data))
                return data

            handle.read = read
            return handle

        import pathlib as _p
        original = _p.Path.open
        _p.Path.open = counting_open
        try:
            mirror.latest_seq()
        finally:
            _p.Path.open = original
        return sum(seen)

    _append(mirror, 50)
    small = read_volume()
    _append(mirror, 800)
    big = read_volume()

    assert big <= small * 2, f"读了 {small} -> {big} 字节，说明还在按库大小读"


def test_lines_are_still_valid_json(tmp_path):
    mirror = _mirror(tmp_path)
    _append(mirror, 5)

    for line in mirror.path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)
