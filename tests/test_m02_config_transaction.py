"""Core M-02 atomic config transaction contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config_transaction import (
    ConfigMutationError,
    ConfigPersistenceError,
    ConfigRollbackError,
    ConfigValidationError,
    RuntimeHook,
    get_config_health,
    read_config_yaml,
    run_config_transaction,
    validate_config_tree,
)


def _write(path: Path, value: dict) -> bytes:
    payload = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode()
    path.write_bytes(payload)
    return payload


def test_success_merges_from_fresh_disk_and_returns_fresh_detached_mapping(tmp_path):
    path = tmp_path / "config.yaml"
    _write(path, {"existing": {"keep": True}, "value": 1})
    result = run_config_transaction(
        path,
        lambda config: config.__setitem__("value", 2),
    )
    assert result.persisted == {"existing": {"keep": True}, "value": 2}
    assert read_config_yaml(path) == result.persisted
    result.persisted["existing"]["keep"] = False
    assert read_config_yaml(path)["existing"]["keep"] is True
    assert result.old_sha256 and result.new_sha256
    assert result.changed is True


def test_mutation_exception_keeps_exact_old_bytes(tmp_path):
    path = tmp_path / "config.yaml"
    old = _write(path, {"value": "old"})

    def fail(_config):
        raise ValueError("secret mutation detail")

    with pytest.raises(ConfigMutationError) as caught:
        run_config_transaction(path, fail)
    assert "secret mutation detail" not in str(caught.value)
    assert path.read_bytes() == old


@pytest.mark.parametrize(
    "point",
    [
        "persist.tmp_create",
        "persist.tmp_write",
        "persist.tmp_flush",
        "persist.tmp_fsync",
        "persist.replace",
        "persist.replace_after",
        "persist.reread",
    ],
)
def test_each_persistence_failure_restores_exact_old_bytes_and_cleans_tmp(
    tmp_path,
    point,
):
    path = tmp_path / "config.yaml"
    old = _write(path, {"value": "old", "untouched": 7})
    fired = False

    def injector(actual):
        nonlocal fired
        if not fired and actual == point:
            fired = True
            raise OSError(f"{point} injected")

    with pytest.raises((ConfigPersistenceError, OSError)):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            fault_injector=injector,
        )
    assert fired is True
    assert path.read_bytes() == old
    assert not list(tmp_path.glob(".config.yaml.*.tmp"))


def test_runtime_failure_restores_original_absence(tmp_path):
    path = tmp_path / "missing" / "config.yaml"
    hook = RuntimeHook(
        "component",
        snapshot=lambda: "old",
        apply=lambda _config: (_ for _ in ()).throw(RuntimeError("apply failed")),
        restore=lambda _snapshot: None,
    )
    with pytest.raises(Exception, match="component"):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("created", True),
            runtime_hooks=(hook,),
        )
    assert not path.exists()


def test_rollback_failure_reports_original_and_disk_failure(tmp_path):
    path = tmp_path / "config.yaml"
    _write(path, {"value": "old"})

    def injector(point):
        if point == "runtime.apply.component":
            raise RuntimeError("original apply")
        if point == "rollback.replace":
            raise OSError("rollback replace")

    hook = RuntimeHook(
        "component",
        snapshot=lambda: "old",
        apply=lambda _config: None,
        restore=lambda _snapshot: None,
    )
    with pytest.raises(ConfigRollbackError) as caught:
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            runtime_hooks=(hook,),
            fault_injector=injector,
        )
    assert caught.value.disk_errors
    assert isinstance(caught.value.original, BaseException)
    assert get_config_health()["ok"] is False


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "text",
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": float("-inf")},
        {"x": object()},
        {1: "non-string-key"},
        {"x": "nul\0character"},
    ],
)
def test_unsafe_root_types_values_and_controls_are_rejected(bad):
    with pytest.raises(ConfigValidationError):
        validate_config_tree(bad)


def test_bool_as_int_and_section_type_are_rejected_by_semantic_validator(tmp_path):
    path = tmp_path / "config.yaml"
    old = _write(path, {"limits": {"count": 2}})

    def validate(candidate):
        section = candidate.get("limits")
        if not isinstance(section, dict):
            raise ValueError("limits section")
        count = section.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10:
            raise ValueError("count range")

    for value in (True, 0, 11, "2"):
        with pytest.raises(ConfigValidationError):
            run_config_transaction(
                path,
                lambda config, value=value: config.__setitem__(
                    "limits", value if value == "2" else {"count": value}
                ),
                validate=validate,
            )
        assert path.read_bytes() == old
