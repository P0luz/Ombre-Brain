"""Cross-process, crash-safe-enough configuration transactions.

This module is the single persistence primitive for runtime ``config.yaml``
mutations.  It deliberately excludes multi-file ``.env`` transactions and
does not perform network I/O.

Normal files are published with a same-directory, exclusive temporary file,
file flush/fsync, and a write-through namespace replacement.  A Linux
single-file bind mount is detected precisely after ``EBUSY`` and rejected:
in-place truncate/overwrite cannot provide the atomicity promised by M-02.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ctypes
import errno
import functools
import hashlib
import inspect
import math
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Mapping, Sequence

import yaml


Mutation = Callable[[dict[str, Any]], None]
Validator = Callable[[Mapping[str, Any]], None]
FaultInjector = Callable[[str], None]

_LOCKS_DIR = ".locks"
_MAX_DEPTH = 64
_MAX_NODES = 100_000
_MOUNTINFO_ESCAPES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}


class ConfigTransactionError(RuntimeError):
    """Base class for M-02 configuration transaction failures."""


class ConfigValidationError(ConfigTransactionError):
    """Candidate YAML failed structural or semantic validation."""


class ConfigMutationError(ConfigTransactionError):
    """The pure mutation callback failed."""


class ConfigPersistenceError(ConfigTransactionError):
    """Atomic persistence or fresh reread verification failed."""

    def __init__(self, phase: str, cause: BaseException):
        super().__init__(
            f"config transaction persistence failed at {phase} "
            f"({type(cause).__name__})"
        )
        self.phase = phase
        self.cause = cause


class ConfigRuntimeError(ConfigTransactionError):
    """A runtime apply or verify hook failed without exposing its value."""

    def __init__(self, phase: str, hook: str, cause: BaseException):
        super().__init__(
            f"config runtime {phase} failed for {hook} "
            f"({type(cause).__name__})"
        )
        self.phase = phase
        self.hook = hook
        self.cause = cause


class UnsupportedAtomicBindMount(ConfigPersistenceError):
    """A single-file bind mount cannot be atomically replaced."""

    def __init__(self, cause: BaseException):
        super().__init__("replace-bind-mount-unsupported", cause)


class ConfigRollbackError(ConfigTransactionError):
    """The original transaction and one or more compensations failed."""

    def __init__(
        self,
        original: BaseException,
        *,
        disk_errors: Sequence[BaseException] = (),
        runtime_errors: Sequence[BaseException] = (),
    ):
        labels = [type(error).__name__ for error in (*disk_errors, *runtime_errors)]
        super().__init__(
            "config transaction failed and rollback failed: "
            f"original={type(original).__name__}; "
            f"rollback={'+'.join(labels) or 'unknown'}"
        )
        self.original = original
        self.disk_errors = tuple(disk_errors)
        self.runtime_errors = tuple(runtime_errors)


_health_lock = threading.Lock()
_config_health: dict[str, Any] = {
    "ok": True,
    "state": "idle",
    "error": "",
}


def _set_health(ok: bool, state: str, error: str = "") -> None:
    with _health_lock:
        _config_health.update(
            {"ok": bool(ok), "state": str(state), "error": str(error)}
        )


def get_config_health() -> dict[str, Any]:
    with _health_lock:
        return dict(_config_health)


def canonical_config_path(path: str | os.PathLike[str]) -> Path:
    requested = Path(path).expanduser()
    # Reject a symlink at the config leaf before resolve() erases that fact.
    # Parent-directory aliases are canonicalized to one resolved target and
    # therefore one lock namespace; replacing a symlink leaf itself is not
    # allowed because it makes mount/rollback identity ambiguous.
    if requested.is_symlink():
        raise ValueError("config path must not be a symlink")
    candidate = requested.resolve(strict=False)
    if not candidate.name:
        raise ValueError("config path must name a file")
    if candidate.exists() and not candidate.is_file():
        raise ValueError("config path is not a regular file")
    return candidate


def _lock_path(path: Path) -> Path:
    # Keep the E-MIG-01 lock namespace byte-for-byte compatible while M-02
    # moves ownership of the primitive into this neutral module.
    digest = hashlib.sha256(
        (
            "ombre-brain:E-MIG-01:config-resource:"
            + os.path.normcase(str(path))
        ).encode("utf-8")
    ).hexdigest()
    lock_dir = path.parent / _LOCKS_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir / f"{digest}.lock"


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED),
    ]
    _UnlockFileEx.restype = wintypes.BOOL


class _ConfigLease:
    def __init__(self, path: Path, *, exclusive: bool, blocking: bool = True):
        self.path = _lock_path(path)
        self.exclusive = exclusive
        self.blocking = blocking
        self.fd: int | None = None
        self._overlapped: Any = None

    def acquire(self) -> bool:
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.fsync(fd)
            if os.name == "nt":
                handle = msvcrt.get_osfhandle(fd)
                overlapped = _OVERLAPPED()
                flags = _LOCKFILE_EXCLUSIVE_LOCK if self.exclusive else 0
                if not self.blocking:
                    flags |= _LOCKFILE_FAIL_IMMEDIATELY
                if not _LockFileEx(
                    handle, flags, 0, 1, 0, ctypes.byref(overlapped)
                ):
                    error = ctypes.get_last_error()
                    if not self.blocking and error == _ERROR_LOCK_VIOLATION:
                        os.close(fd)
                        return False
                    raise ctypes.WinError(error)
                self._overlapped = overlapped
            else:  # pragma: no cover - production host is Windows
                import fcntl

                operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
                if not self.blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(fd, operation)
                except BlockingIOError:
                    os.close(fd)
                    return False
            self.fd = fd
            return True
        except BaseException:
            if self.fd is None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise

    def close(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                handle = msvcrt.get_osfhandle(fd)
                overlapped = self._overlapped
                if overlapped is not None and not _UnlockFileEx(
                    handle, 0, 1, 0, ctypes.byref(overlapped)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:  # pragma: no cover - production host is Windows
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "_ConfigLease":
        if not self.acquire():
            raise BlockingIOError("config lease is already held")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@contextlib.contextmanager
def config_yaml_turn(
    config_path: str | os.PathLike[str],
    *,
    exclusive: bool = False,
) -> Iterator[None]:
    path = canonical_config_path(config_path)
    with _ConfigLease(path, exclusive=exclusive):
        yield


@contextlib.asynccontextmanager
async def async_config_yaml_turn(
    config_path: str | os.PathLike[str],
    *,
    exclusive: bool = False,
) -> AsyncIterator[None]:
    path = canonical_config_path(config_path)
    lease = _ConfigLease(path, exclusive=exclusive)
    enter_task = asyncio.create_task(asyncio.to_thread(lease.__enter__))
    try:
        await asyncio.shield(enter_task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await enter_task
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(lease.__exit__, None, None, None)
        raise
    try:
        yield
    finally:
        await asyncio.shield(
            asyncio.to_thread(lease.__exit__, None, None, None)
        )


def _infer_config_path(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
    for key in ("config_path", "path"):
        value = kwargs.get(key)
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
    for obj in args:
        value = getattr(obj, "config_path", None)
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
        runtime = getattr(obj, "runtime", None)
        value = getattr(runtime, "config_path", None)
        if isinstance(value, (str, os.PathLike)):
            return os.fspath(value)
    raise RuntimeError(
        "config_mutation_guard could not infer config_path; "
        "pass config_path= or use path_getter="
    )


def config_mutation_guard(
    func: Callable[..., Any] | None = None,
    *,
    path_getter: Callable[..., str | os.PathLike[str]] | None = None,
) -> Any:
    """Compatibility shared gate for readers/legacy routes.

    M-02 writers must use :func:`run_config_transaction`, whose lease is
    exclusive.  This decorator remains shared so existing E-MIG lock-scope
    tests and transitional readers preserve their behavior.
    """

    def decorate(inner: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(inner)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            config_path = (
                os.fspath(path_getter(*args, **kwargs))
                if path_getter is not None
                else _infer_config_path(args, kwargs)
            )
            async with async_config_yaml_turn(config_path):
                return await inner(*args, **kwargs)

        return wrapped

    return decorate(func) if func is not None else decorate


def _inject(injector: FaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _validate_string(value: str, path: str) -> None:
    for character in value:
        code = ord(character)
        if (code < 32 and character not in "\t\n\r") or code == 127:
            raise ConfigValidationError(
                f"config contains a control character at {path}"
            )


def validate_config_tree(value: Any) -> None:
    """Validate that ``value`` is finite, acyclic, safe YAML data."""

    if not isinstance(value, dict):
        raise ConfigValidationError("config root must be a mapping")
    active: set[int] = set()
    seen_containers: set[int] = set()
    nodes = 0

    def walk(item: Any, path: str, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES:
            raise ConfigValidationError("config contains too many values")
        if depth > _MAX_DEPTH:
            raise ConfigValidationError("config nesting is too deep")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ConfigValidationError(
                    f"config contains a non-finite number at {path}"
                )
            return
        if isinstance(item, str):
            _validate_string(item, path)
            return
        if isinstance(item, dict):
            identity = id(item)
            if identity in active:
                raise ConfigValidationError("config contains a recursive mapping")
            if identity in seen_containers:
                raise ConfigValidationError("config contains a YAML mapping alias")
            seen_containers.add(identity)
            active.add(identity)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ConfigValidationError(
                            f"config mapping key is not a string at {path}"
                        )
                    _validate_string(key, path)
                    walk(child, f"{path}.{key}", depth + 1)
            finally:
                active.remove(identity)
            return
        if isinstance(item, list):
            identity = id(item)
            if identity in active:
                raise ConfigValidationError("config contains a recursive list")
            if identity in seen_containers:
                raise ConfigValidationError("config contains a YAML list alias")
            seen_containers.add(identity)
            active.add(identity)
            try:
                for index, child in enumerate(item):
                    walk(child, f"{path}[{index}]", depth + 1)
            finally:
                active.remove(identity)
            return
        raise ConfigValidationError(
            f"config contains an unsafe value type at {path}"
        )

    walk(value, "$", 0)


def _serialize(candidate: Mapping[str, Any]) -> bytes:
    try:
        text = yaml.safe_dump(
            dict(candidate),
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        payload = text.encode("utf-8")
        reread = yaml.safe_load(payload.decode("utf-8"))
    except Exception as error:
        raise ConfigValidationError(
            f"config safe YAML serialization failed ({type(error).__name__})"
        ) from error
    if reread is None:
        reread = {}
    validate_config_tree(reread)
    if reread != dict(candidate):
        raise ConfigValidationError("config safe YAML roundtrip changed semantics")
    return payload


def _parse_payload(payload: bytes) -> dict[str, Any]:
    try:
        decoded = payload.decode("utf-8")
        value = yaml.safe_load(decoded)
    except Exception as error:
        raise ConfigValidationError(
            f"config YAML parsing failed ({type(error).__name__})"
        ) from error
    if value is None:
        value = {}
    validate_config_tree(value)
    return value


def _read_snapshot(path: Path) -> "ConfigSnapshot":
    if not path.exists():
        return ConfigSnapshot(False, b"", {})
    if not path.is_file():
        raise ConfigValidationError("config path is not a regular file")
    payload = path.read_bytes()
    return ConfigSnapshot(True, payload, _parse_payload(payload))


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(
        r"\\(040|011|012|134)",
        lambda match: _MOUNTINFO_ESCAPES[match.group(1)],
        value,
    )


def is_exact_linux_mount_point(path: str | os.PathLike[str]) -> bool:
    candidate = canonical_config_path(path)
    if not sys.platform.startswith("linux") or not candidate.is_file():
        return False
    target = os.path.realpath(os.path.abspath(candidate))
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) <= 4:
                    continue
                mounted_at = _decode_mountinfo_path(fields[4])
                if os.path.realpath(mounted_at) == target:
                    return True
    except OSError:
        return False
    return False


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_through_replace(source: Path, target: Path) -> None:
    if source.resolve(strict=False).anchor.lower() != target.resolve(
        strict=False
    ).anchor.lower():
        raise ValueError("config replace requires one volume")
    if os.name == "nt":
        from ctypes import wintypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        move.restype = wintypes.BOOL
        flags = 0x1 | 0x8  # REPLACE_EXISTING | WRITE_THROUGH
        if not move(str(source), str(target), flags):
            raise ctypes.WinError(ctypes.get_last_error())
    else:  # pragma: no cover - production host is Windows
        os.replace(source, target)
        _fsync_parent(target)


def _atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    injector: FaultInjector | None,
    prefix: str,
    on_published: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        _inject(injector, f"{prefix}.tmp_create")
        descriptor = os.open(
            str(tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        if path.exists():
            with contextlib.suppress(OSError):
                os.chmod(tmp, path.stat().st_mode & 0o777)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            _inject(injector, f"{prefix}.tmp_write")
            handle.write(payload)
            _inject(injector, f"{prefix}.tmp_flush")
            handle.flush()
            _inject(injector, f"{prefix}.tmp_fsync")
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = None
        _inject(injector, f"{prefix}.replace")
        try:
            _write_through_replace(tmp, path)
        except OSError as error:
            if error.errno == errno.EBUSY and is_exact_linux_mount_point(path):
                raise UnsupportedAtomicBindMount(error) from error
            raise
        if on_published is not None:
            on_published()
        _inject(injector, f"{prefix}.replace_after")
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _remove_live_path(path: Path, injector: FaultInjector | None) -> None:
    if not path.exists():
        return
    _inject(injector, "rollback.remove")
    if os.name == "nt":
        quarantine = path.parent / f".{path.name}.{uuid.uuid4().hex}.rollback"
        _write_through_replace(path, quarantine)
        quarantine.unlink()
    else:  # pragma: no cover - production host is Windows
        path.unlink()
        _fsync_parent(path)


def _ensure_sync_result(value: Any, phase: str) -> None:
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise TypeError(f"{phase} callback must be synchronous")


@dataclass(frozen=True)
class ConfigSnapshot:
    exists: bool
    payload: bytes
    mapping: dict[str, Any]

    @property
    def sha256(self) -> str | None:
        if not self.exists:
            return None
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class PreparedConfig:
    session_id: str
    candidate: dict[str, Any]
    payload: bytes
    old: ConfigSnapshot
    changed: bool

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class RuntimeHook:
    name: str
    snapshot: Callable[[], Any]
    apply: Callable[[Mapping[str, Any]], None]
    restore: Callable[[Any], None]
    verify: Callable[[Mapping[str, Any]], None] | None = None


@dataclass(frozen=True)
class ConfigTransactionResult:
    persisted: dict[str, Any]
    old_sha256: str | None
    new_sha256: str
    changed: bool


class ConfigTransactionSession:
    """One exclusive, cross-process config transaction session."""

    def __init__(
        self,
        config_path: str | os.PathLike[str],
        *,
        fault_injector: FaultInjector | None = None,
    ):
        self.path = canonical_config_path(config_path)
        self.fault_injector = fault_injector
        self.session_id = uuid.uuid4().hex
        self._lease = _ConfigLease(self.path, exclusive=True)
        self._entered = False
        self._old: ConfigSnapshot | None = None
        self._disk_changed = False

    def __enter__(self) -> "ConfigTransactionSession":
        self._lease.__enter__()
        self._entered = True
        return self

    def __exit__(self, *_exc: object) -> None:
        self._entered = False
        self._lease.close()

    def _require_entered(self) -> None:
        if not self._entered:
            raise RuntimeError("config transaction session is not entered")

    def read_fresh(self) -> dict[str, Any]:
        self._require_entered()
        _inject(self.fault_injector, "read")
        return copy.deepcopy(_read_snapshot(self.path).mapping)

    def prepare(
        self,
        mutate: Mutation,
        validate: Validator | None = None,
    ) -> PreparedConfig:
        self._require_entered()
        _inject(self.fault_injector, "prepare.read")
        old = _read_snapshot(self.path)
        candidate = copy.deepcopy(old.mapping)
        try:
            result = mutate(candidate)
            _ensure_sync_result(result, "mutation")
        except ConfigTransactionError:
            raise
        except BaseException as error:
            raise ConfigMutationError(
                f"config mutation failed ({type(error).__name__})"
            ) from error
        _inject(self.fault_injector, "prepare.mutate")
        validate_config_tree(candidate)
        if validate is not None:
            try:
                result = validate(candidate)
                _ensure_sync_result(result, "validator")
            except ConfigTransactionError:
                raise
            except BaseException as error:
                raise ConfigValidationError(
                    "additional config validation failed "
                    f"({type(error).__name__})"
                ) from error
        _inject(self.fault_injector, "prepare.validate")
        payload = _serialize(candidate)
        _inject(self.fault_injector, "prepare.serialize")
        self._old = old
        return PreparedConfig(
            self.session_id,
            candidate,
            payload,
            old,
            candidate != old.mapping,
        )

    def persist(self, prepared: PreparedConfig) -> dict[str, Any]:
        self._require_entered()
        if prepared.session_id != self.session_id:
            raise RuntimeError("prepared config belongs to another session")
        if prepared.old is not self._old:
            raise RuntimeError("prepared config is not current for this session")
        if not prepared.changed:
            fresh = _read_snapshot(self.path).mapping
            if fresh != prepared.candidate:
                raise ConfigPersistenceError(
                    "unchanged-reread",
                    RuntimeError("semantic mismatch"),
                )
            return copy.deepcopy(fresh)
        phase = "write"
        try:
            def mark_published() -> None:
                self._disk_changed = True

            _atomic_write_bytes(
                self.path,
                prepared.payload,
                injector=self.fault_injector,
                prefix="persist",
                on_published=mark_published,
            )
            phase = "reread"
            _inject(self.fault_injector, "persist.reread")
            fresh = _read_snapshot(self.path)
            if fresh.mapping != prepared.candidate:
                raise RuntimeError("fresh semantic mismatch")
            return copy.deepcopy(fresh.mapping)
        except UnsupportedAtomicBindMount:
            raise
        except BaseException as error:
            original = (
                error
                if isinstance(error, ConfigTransactionError)
                else ConfigPersistenceError(phase, error)
            )
            if self._disk_changed:
                try:
                    self.restore_old()
                except BaseException as rollback:
                    combined = ConfigRollbackError(
                        original,
                        disk_errors=(rollback,),
                    )
                    _set_health(
                        False,
                        "rollback_failed",
                        type(rollback).__name__,
                    )
                    raise combined from rollback
            raise original

    def restore_old(self) -> None:
        self._require_entered()
        if self._old is None:
            raise RuntimeError("config transaction has no old snapshot")
        _inject(self.fault_injector, "rollback.begin")
        if self._old.exists:
            _atomic_write_bytes(
                self.path,
                self._old.payload,
                injector=self.fault_injector,
                prefix="rollback",
            )
            _inject(self.fault_injector, "rollback.reread")
            if not self.path.exists() or self.path.read_bytes() != self._old.payload:
                raise ConfigPersistenceError(
                    "rollback-reread",
                    RuntimeError("exact byte mismatch"),
                )
        else:
            _remove_live_path(self.path, self.fault_injector)
            if self.path.exists():
                raise ConfigPersistenceError(
                    "rollback-absence",
                    RuntimeError("path still exists"),
                )
        self._disk_changed = False

    @property
    def disk_changed(self) -> bool:
        return self._disk_changed


def open_config_transaction(
    config_path: str | os.PathLike[str],
    *,
    fault_injector: FaultInjector | None = None,
) -> ConfigTransactionSession:
    return ConfigTransactionSession(
        config_path,
        fault_injector=fault_injector,
    )


@contextlib.asynccontextmanager
async def async_open_config_transaction(
    config_path: str | os.PathLike[str],
    *,
    fault_injector: FaultInjector | None = None,
) -> AsyncIterator[ConfigTransactionSession]:
    session = ConfigTransactionSession(
        config_path,
        fault_injector=fault_injector,
    )
    enter_task = asyncio.create_task(asyncio.to_thread(session.__enter__))
    try:
        await asyncio.shield(enter_task)
    except asyncio.CancelledError:
        with contextlib.suppress(BaseException):
            await enter_task
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(session.__exit__, None, None, None)
        raise
    try:
        yield session
    finally:
        await asyncio.shield(
            asyncio.to_thread(session.__exit__, None, None, None)
        )


def run_config_transaction(
    config_path: str | os.PathLike[str],
    mutate: Mutation,
    *,
    validate: Validator | None = None,
    runtime_hooks: Sequence[RuntimeHook] = (),
    fault_injector: FaultInjector | None = None,
) -> ConfigTransactionResult:
    """Persist one candidate and publish compensating runtime hooks."""

    with open_config_transaction(
        config_path,
        fault_injector=fault_injector,
    ) as transaction:
        prepared = transaction.prepare(mutate, validate=validate)
        snapshots: list[tuple[RuntimeHook, Any]] = []
        for hook in runtime_hooks:
            try:
                _inject(fault_injector, f"runtime.snapshot.{hook.name}")
                snapshot = hook.snapshot()
                _ensure_sync_result(snapshot, f"snapshot {hook.name}")
                snapshots.append((hook, snapshot))
            except BaseException as error:
                raise ConfigRuntimeError("snapshot", hook.name, error) from error

        touched: list[tuple[RuntimeHook, Any]] = []
        try:
            persisted = transaction.persist(prepared)
            for hook, snapshot in snapshots:
                touched.append((hook, snapshot))
                try:
                    _inject(fault_injector, f"runtime.apply.{hook.name}")
                    result = hook.apply(persisted)
                    _ensure_sync_result(result, f"apply {hook.name}")
                except BaseException as error:
                    raise ConfigRuntimeError("apply", hook.name, error) from error
            for hook, _snapshot in snapshots:
                if hook.verify is None:
                    continue
                try:
                    _inject(fault_injector, f"runtime.verify.{hook.name}")
                    result = hook.verify(persisted)
                    _ensure_sync_result(result, f"verify {hook.name}")
                except BaseException as error:
                    raise ConfigRuntimeError("verify", hook.name, error) from error
        except BaseException as original:
            disk_errors: list[BaseException] = []
            runtime_errors: list[BaseException] = []
            if transaction.disk_changed:
                try:
                    transaction.restore_old()
                except BaseException as error:
                    disk_errors.append(error)
            for hook, snapshot in reversed(touched):
                try:
                    _inject(fault_injector, f"runtime.restore.{hook.name}")
                    result = hook.restore(snapshot)
                    _ensure_sync_result(result, f"restore {hook.name}")
                except BaseException as error:
                    runtime_errors.append(error)
            if disk_errors or runtime_errors:
                combined = ConfigRollbackError(
                    original,
                    disk_errors=disk_errors,
                    runtime_errors=runtime_errors,
                )
                _set_health(
                    False,
                    "rollback_failed",
                    "+".join(
                        type(error).__name__
                        for error in (*disk_errors, *runtime_errors)
                    ),
                )
                raise combined from original
            _set_health(True, "aborted_restored", type(original).__name__)
            raise

        _set_health(True, "committed")
        return ConfigTransactionResult(
            copy.deepcopy(persisted),
            prepared.old.sha256,
            hashlib.sha256(prepared.payload).hexdigest(),
            prepared.changed,
        )


def read_config_yaml(config_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = canonical_config_path(config_path)
    with _ConfigLease(path, exclusive=False):
        return copy.deepcopy(_read_snapshot(path).mapping)


__all__ = [
    "ConfigTransactionError",
    "ConfigValidationError",
    "ConfigMutationError",
    "ConfigPersistenceError",
    "ConfigRuntimeError",
    "ConfigRollbackError",
    "UnsupportedAtomicBindMount",
    "ConfigSnapshot",
    "PreparedConfig",
    "RuntimeHook",
    "ConfigTransactionResult",
    "ConfigTransactionSession",
    "canonical_config_path",
    "config_yaml_turn",
    "async_config_yaml_turn",
    "config_mutation_guard",
    "validate_config_tree",
    "is_exact_linux_mount_point",
    "open_config_transaction",
    "async_open_config_transaction",
    "run_config_transaction",
    "read_config_yaml",
    "get_config_health",
]
