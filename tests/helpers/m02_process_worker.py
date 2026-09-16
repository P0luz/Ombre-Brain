"""Spawn-safe synthetic workers for M-02 config transaction tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def mutate_worker(
    config_path: str,
    section: str,
    key: str,
    value,
    ready,
    start,
    results,
) -> None:
    from config_transaction import run_config_transaction

    ready.set()
    start.wait(30)
    try:
        result = run_config_transaction(
            config_path,
            lambda config: config.setdefault(section, {}).__setitem__(key, value),
        )
        results.put({"ok": True, "value": result.persisted[section][key]})
    except BaseException:
        results.put({"ok": False, "traceback": traceback.format_exc()})


def increment_from_fresh_worker(
    config_path: str,
    ready,
    start,
    results,
) -> None:
    """Pre-read stale state, then increment only the lock-fresh candidate."""

    import yaml
    from config_transaction import run_config_transaction

    try:
        stale = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        stale_value = int((stale or {}).get("counter", 0))
        ready.set()
        start.wait(30)

        def mutate(config):
            config["counter"] = int(config.get("counter", 0)) + 1

        result = run_config_transaction(config_path, mutate)
        results.put(
            {
                "ok": True,
                "stale": stale_value,
                "fresh": result.persisted["counter"],
            }
        )
    except BaseException:
        results.put({"ok": False, "traceback": traceback.format_exc()})


def hold_transaction_worker(config_path: str, entered, release) -> None:
    from config_transaction import open_config_transaction

    with open_config_transaction(config_path):
        entered.set()
        release.wait(30)


def hard_exit_holding_worker(config_path: str, entered) -> None:
    from config_transaction import open_config_transaction

    with open_config_transaction(config_path):
        entered.set()
        os._exit(0)


def hard_exit_at_persist_point(
    config_path: str,
    point: str,
    exit_code: int,
) -> None:
    from config_transaction import run_config_transaction

    def injector(actual: str) -> None:
        if actual == point:
            os._exit(exit_code)

    run_config_transaction(
        config_path,
        lambda config: config.__setitem__("generation", "candidate"),
        fault_injector=injector,
    )
