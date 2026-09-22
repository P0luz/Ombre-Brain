"""Runtime snapshot/apply/verify/restore compensation contract."""

from __future__ import annotations

import copy

import pytest
import yaml

from config_transaction import (
    ConfigRollbackError,
    ConfigRuntimeError,
    RuntimeHook,
    get_config_health,
    run_config_transaction,
)


def test_hooks_apply_and_verify_in_order_after_persistence(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")
    events = []

    def hook(name):
        return RuntimeHook(
            name,
            snapshot=lambda name=name: events.append(f"snapshot:{name}") or "old",
            apply=lambda config, name=name: events.append(
                f"apply:{name}:{config['value']}"
            ),
            restore=lambda _old, name=name: events.append(f"restore:{name}"),
            verify=lambda config, name=name: events.append(
                f"verify:{name}:{config['value']}"
            ),
        )

    run_config_transaction(
        path,
        lambda config: config.__setitem__("value", "new"),
        runtime_hooks=(hook("a"), hook("b")),
    )
    assert events == [
        "snapshot:a",
        "snapshot:b",
        "apply:a:new",
        "apply:b:new",
        "verify:a:new",
        "verify:b:new",
    ]


def test_apply_failure_restores_touched_hooks_in_reverse_and_disk_exact(tmp_path):
    path = tmp_path / "config.yaml"
    old = b"value: old\n"
    path.write_bytes(old)
    events = []

    first = RuntimeHook(
        "first",
        snapshot=lambda: "old-first",
        apply=lambda _config: events.append("apply:first"),
        restore=lambda _snapshot: events.append("restore:first"),
    )

    def fail_second(_config):
        events.append("apply:second")
        raise RuntimeError("second failed")

    second = RuntimeHook(
        "second",
        snapshot=lambda: "old-second",
        apply=fail_second,
        restore=lambda _snapshot: events.append("restore:second"),
    )
    with pytest.raises(ConfigRuntimeError):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            runtime_hooks=(first, second),
        )
    assert path.read_bytes() == old
    assert events == [
        "apply:first",
        "apply:second",
        "restore:second",
        "restore:first",
    ]


def test_verify_failure_restores_all_applied_hooks(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")
    state = {"a": "old", "b": "old"}

    def make(name, fail=False):
        return RuntimeHook(
            name,
            snapshot=lambda name=name: state[name],
            apply=lambda config, name=name: state.__setitem__(name, config["value"]),
            restore=lambda old, name=name: state.__setitem__(name, old),
            verify=(
                (lambda _config: (_ for _ in ()).throw(RuntimeError("verify")))
                if fail
                else lambda _config: None
            ),
        )

    with pytest.raises(ConfigRuntimeError):
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            runtime_hooks=(make("a"), make("b", fail=True)),
        )
    assert state == {"a": "old", "b": "old"}
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["value"] == "old"


def test_shared_config_hook_preserves_dict_identity_on_apply_and_restore(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")
    shared = {"value": "old", "nested": {"keep": True}}
    identity = id(shared)

    def snapshot():
        return copy.deepcopy(shared)

    def apply(config):
        shared.clear()
        shared.update(copy.deepcopy(config))

    def restore(old):
        shared.clear()
        shared.update(old)

    hook = RuntimeHook("shared-config", snapshot, apply, restore)
    result = run_config_transaction(
        path,
        lambda config: config.__setitem__("value", "new"),
        runtime_hooks=(hook,),
    )
    assert id(shared) == identity
    assert shared == result.persisted


def test_snapshot_failure_happens_before_disk_publish(tmp_path):
    path = tmp_path / "config.yaml"
    old = b"value: old\n"
    path.write_bytes(old)
    hook = RuntimeHook(
        "component",
        snapshot=lambda: (_ for _ in ()).throw(RuntimeError("snapshot")),
        apply=lambda _config: None,
        restore=lambda _old: None,
    )
    with pytest.raises(ConfigRuntimeError) as caught:
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            runtime_hooks=(hook,),
        )
    assert caught.value.phase == "snapshot"
    assert path.read_bytes() == old


def test_disk_and_runtime_rollback_failures_are_combined(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")

    def injector(point):
        if point == "runtime.apply.component":
            raise RuntimeError("original")
        if point == "rollback.replace":
            raise OSError("disk rollback")
        if point == "runtime.restore.component":
            raise OSError("component rollback")

    hook = RuntimeHook(
        "component",
        snapshot=lambda: "old",
        apply=lambda _config: None,
        restore=lambda _old: None,
    )
    with pytest.raises(ConfigRollbackError) as caught:
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            runtime_hooks=(hook,),
            fault_injector=injector,
        )
    assert caught.value.disk_errors
    assert caught.value.runtime_errors
    health = get_config_health()
    assert health["ok"] is False
    assert health["state"] == "rollback_failed"


@pytest.mark.parametrize(
    "rollback_point",
    [
        "rollback.begin",
        "rollback.tmp_create",
        "rollback.tmp_write",
        "rollback.tmp_flush",
        "rollback.tmp_fsync",
        "rollback.replace",
        "rollback.replace_after",
        "rollback.reread",
    ],
)
def test_each_old_file_rollback_failpoint_is_reported_loudly(
    tmp_path,
    rollback_point,
):
    path = tmp_path / "config.yaml"
    path.write_text("value: old\n", encoding="utf-8")

    def injector(point):
        if point == "runtime.apply.component":
            raise RuntimeError("original")
        if point == rollback_point:
            raise OSError(f"{rollback_point} injected")

    hook = RuntimeHook(
        "component",
        snapshot=lambda: "old",
        apply=lambda _config: None,
        restore=lambda _old: None,
    )
    with pytest.raises(ConfigRollbackError) as caught:
        run_config_transaction(
            path,
            lambda config: config.__setitem__("value", "new"),
            runtime_hooks=(hook,),
            fault_injector=injector,
        )
    assert caught.value.disk_errors
    assert get_config_health()["ok"] is False


def test_absent_file_rollback_remove_failure_is_reported_loudly(tmp_path):
    path = tmp_path / "missing" / "config.yaml"

    def injector(point):
        if point == "runtime.apply.component":
            raise RuntimeError("original")
        if point == "rollback.remove":
            raise OSError("remove injected")

    hook = RuntimeHook(
        "component",
        snapshot=lambda: "old",
        apply=lambda _config: None,
        restore=lambda _old: None,
    )
    with pytest.raises(ConfigRollbackError) as caught:
        run_config_transaction(
            path,
            lambda config: config.__setitem__("created", True),
            runtime_hooks=(hook,),
            fault_injector=injector,
        )
    assert caught.value.disk_errors
