"""Fail-closed Windows handle-relative, no-replace file rename.

This module is intentionally narrow.  It admits only local NTFS paths on the
same drive, walks every directory component by handle without following
reparse points, opens the source relative to its pinned parent, and commits it
relative to the pinned destination parent with ``ReplaceIfExists=FALSE``.

No pathname-based fallback is provided.  Unsupported environments fail before
the namespace-changing system call.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes
from typing import Callable


class SafeRenameUnavailable(OSError):
    """The host or paths cannot satisfy the safe-commit contract."""


class UnsafePathError(ValueError):
    """A path is not a plain local absolute path beneath a plain chain."""


class SourceChangedError(RuntimeError):
    """The pinned source bytes do not match the caller's expected digest."""


class PostCommitRewriteError(RuntimeError):
    """Authority changed, but the replacement could not be safely verified."""


@dataclass(frozen=True)
class SafeRenameResult:
    filesystem: str
    volume_serial: int
    file_id: int
    source_sha256: str
    size: int


@dataclass(frozen=True)
class SafeSourceSnapshot:
    filesystem: str
    volume_serial: int
    file_id: int
    number_of_links: int
    last_write_time_100ns: int
    source_sha256: str
    size: int
    raw: bytes

    @property
    def revision(self) -> str:
        identity = (
            self.volume_serial,
            self.file_id,
            self.number_of_links,
            self.size,
            self.last_write_time_100ns,
        )
        material = json.dumps(identity, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(material + b"\0" + self.raw).hexdigest()


if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
else:  # pragma: no cover - definitions remain importable for fail-closed callers
    kernel32 = None
    ntdll = None


DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_READ_DATA = 0x0001
FILE_LIST_DIRECTORY = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x0001
FILE_SHARE_WRITE = 0x0002
FILE_SHARE_DELETE = 0x0004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3
FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x00000040
FILE_RENAME_INFORMATION_CLASS = 10
FILE_RENAME_INFORMATION_EX_CLASS = 65
FILE_DISPOSITION_INFORMATION_CLASS = 13
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_SOURCE_BYTES = 16 * 1024 * 1024


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("status_or_pointer", ctypes.c_void_p), ("information", ctypes.c_size_t)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", wintypes.LPVOID),
        ("SecurityQualityOfService", wintypes.LPVOID),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", wintypes.DWORD),
        ("CreationTime", wintypes.FILETIME),
        ("LastAccessTime", wintypes.FILETIME),
        ("LastWriteTime", wintypes.FILETIME),
        ("VolumeSerialNumber", wintypes.DWORD),
        ("FileSizeHigh", wintypes.DWORD),
        ("FileSizeLow", wintypes.DWORD),
        ("NumberOfLinks", wintypes.DWORD),
        ("FileIndexHigh", wintypes.DWORD),
        ("FileIndexLow", wintypes.DWORD),
    ]


class _FileRenamePrefix(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", wintypes.DWORD),
        ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD),
        ("FileName", wintypes.WCHAR * 1),
    ]


class _Handle:
    def __init__(self, value: int):
        if value in (None, INVALID_HANDLE_VALUE):
            raise ctypes.WinError(ctypes.get_last_error())
        self.value = value

    def close(self) -> None:
        if self.value is not None:
            kernel32.CloseHandle(self.value)
            self.value = None

    def __enter__(self) -> "_Handle":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


if os.name == "nt":
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation),
    )
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileSizeEx.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong))
    kernel32.GetFileSizeEx.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = (
        wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    )
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = (
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    )
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = (
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    )
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
    )
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    ntdll.NtCreateFile.argtypes = (
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes), ctypes.POINTER(_IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    )
    ntdll.NtCreateFile.restype = ctypes.c_long
    ntdll.NtSetInformationFile.argtypes = (
        wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), wintypes.LPVOID,
        wintypes.ULONG, wintypes.ULONG,
    )
    ntdll.NtSetInformationFile.restype = ctypes.c_long
    ntdll.RtlNtStatusToDosError.argtypes = (ctypes.c_long,)
    ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG


def _raise_status(status: int, context: str) -> None:
    code = int(ntdll.RtlNtStatusToDosError(status))
    if code in (80, 183):
        raise FileExistsError(code, context)
    raise OSError(code, context)


def _validate_local_absolute(path: str | os.PathLike[str]) -> tuple[str, str, tuple[str, ...]]:
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw):
        raise UnsafePathError("path must be absolute")
    normalized = os.path.abspath(raw)
    drive, tail = os.path.splitdrive(normalized)
    if len(drive) != 2 or drive[1] != ":" or normalized.startswith("\\\\"):
        raise SafeRenameUnavailable("only local drive-letter paths are supported")
    parts = tuple(part for part in tail.replace("/", "\\").split("\\") if part)
    if any(part in (".", "..") for part in parts):
        raise UnsafePathError("dot path components are not allowed")
    return normalized, drive.upper(), parts


def _validate_name(name: str) -> None:
    if (
        not name or name in (".", "..") or "\\" in name or "/" in name
        or "\x00" in name or name.endswith(" ") or name.endswith(".")
        or any(ord(char) < 32 or char in '<>:"|?*' for char in name)
    ):
        raise UnsafePathError("target must be one normalized path component")
    stem = name.split(".", 1)[0].rstrip(" .").casefold()
    if stem in {"con", "prn", "aux", "nul"} or (
        len(stem) == 4 and stem[:3] in {"com", "lpt"} and stem[3] in "123456789"
    ):
        raise UnsafePathError("reserved DOS device names are not allowed")


def _file_info(handle: _Handle) -> _ByHandleFileInformation:
    info = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle.value, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info


def _reject_reparse(handle: _Handle, component: str) -> None:
    if _file_info(handle).FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafePathError(f"reparse component rejected: {component}")


def _unicode(value: str) -> tuple[ctypes.Array, _UnicodeString]:
    buffer = ctypes.create_unicode_buffer(value)
    length = len(value.encode("utf-16-le"))
    return buffer, _UnicodeString(length, length, ctypes.cast(buffer, wintypes.LPWSTR))


def _nt_open_relative(
    root: _Handle,
    name: str,
    *,
    desired_access: int,
    share_access: int,
    create_options: int,
    disposition: int = FILE_OPEN,
) -> _Handle:
    _validate_name(name)
    name_buffer, unicode_name = _unicode(name)
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), root.value, ctypes.pointer(unicode_name),
        OBJ_CASE_INSENSITIVE, None, None,
    )
    result = wintypes.HANDLE()
    iosb = _IoStatusBlock()
    status = int(
        ntdll.NtCreateFile(
            ctypes.byref(result), desired_access, ctypes.byref(attributes),
            ctypes.byref(iosb), None, 0, share_access, disposition,
            create_options, None, 0,
        )
    )
    _ = name_buffer  # keep the backing memory live through the syscall
    if status < 0:
        _raise_status(status, f"cannot open path component: {name}")
    return _Handle(result.value)


def _open_volume_root(drive: str) -> _Handle:
    value = kernel32.CreateFileW(
        drive + "\\", FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None,
        OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    handle = _Handle(value)
    _reject_reparse(handle, drive + "\\")
    return handle


def _open_plain_directory(path: str | os.PathLike[str]) -> _Handle:
    _normalized, drive, parts = _validate_local_absolute(path)
    current = _open_volume_root(drive)
    try:
        for component in parts:
            child = _nt_open_relative(
                current, component,
                desired_access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_access=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                create_options=(
                    FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT
                    | FILE_OPEN_REPARSE_POINT | FILE_OPEN_FOR_BACKUP_INTENT
                ),
            )
            try:
                _reject_reparse(child, component)
            except BaseException:
                child.close()
                raise
            current.close()
            current = child
        return current
    except BaseException:
        current.close()
        raise


def _open_plain_source(parent: _Handle, name: str) -> _Handle:
    handle = _nt_open_relative(
        parent, name,
        desired_access=DELETE | GENERIC_READ | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        # Denying FILE_SHARE_WRITE prevents a new non-cooperating writer from
        # changing bytes between digest validation and the rename syscall.
        share_access=FILE_SHARE_READ | FILE_SHARE_DELETE,
        create_options=(
            FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT
            | FILE_OPEN_REPARSE_POINT | FILE_OPEN_FOR_BACKUP_INTENT
        ),
    )
    try:
        _reject_reparse(handle, name)
        return handle
    except BaseException:
        handle.close()
        raise


def _read_all(handle: _Handle, max_bytes: int) -> bytes:
    size = ctypes.c_longlong()
    if not kernel32.GetFileSizeEx(handle.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    if size.value < 0 or size.value > max_bytes:
        raise SafeRenameUnavailable(f"source size outside admitted range: {size.value}")
    if not kernel32.SetFilePointerEx(handle.value, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    remaining = size.value
    chunks: list[bytes] = []
    while remaining:
        amount = min(remaining, 1024 * 1024)
        buffer = ctypes.create_string_buffer(amount)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(handle.value, buffer, amount, ctypes.byref(read), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if read.value == 0:
            raise OSError("unexpected EOF while reading pinned source")
        chunks.append(buffer.raw[: read.value])
        remaining -= read.value
    return b"".join(chunks)


def _source_snapshot(
    handle: _Handle, filesystem: str, max_source_bytes: int
) -> SafeSourceSnapshot:
    info = _file_info(handle)
    if int(info.NumberOfLinks) != 1:
        raise UnsafePathError(
            f"source must have exactly one hardlink, found {int(info.NumberOfLinks)}"
        )
    raw = _read_all(handle, max_source_bytes)
    last_write = (
        int(info.LastWriteTime.dwHighDateTime) << 32
    ) | int(info.LastWriteTime.dwLowDateTime)
    return SafeSourceSnapshot(
        filesystem=filesystem,
        volume_serial=int(info.VolumeSerialNumber),
        file_id=(int(info.FileIndexHigh) << 32) | int(info.FileIndexLow),
        number_of_links=int(info.NumberOfLinks),
        last_write_time_100ns=last_write,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        raw=raw,
    )


def _assert_pinned_source_unchanged(
    handle: _Handle,
    snapshot: SafeSourceSnapshot,
    *,
    expected_links: int,
) -> None:
    info = _file_info(handle)
    last_write = (
        int(info.LastWriteTime.dwHighDateTime) << 32
    ) | int(info.LastWriteTime.dwLowDateTime)
    current = (
        int(info.VolumeSerialNumber),
        (int(info.FileIndexHigh) << 32) | int(info.FileIndexLow),
        int(info.NumberOfLinks),
        (int(info.FileSizeHigh) << 32) | int(info.FileSizeLow),
        last_write,
    )
    expected = (
        snapshot.volume_serial,
        snapshot.file_id,
        expected_links,
        snapshot.size,
        snapshot.last_write_time_100ns,
    )
    if current != expected:
        raise SourceChangedError("pinned source identity or hardlink count changed")


def _filesystem_for_drive(drive: str) -> str:
    fs_name = ctypes.create_unicode_buffer(261)
    volume_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationW(
        drive + "\\", volume_name, len(volume_name), ctypes.byref(serial),
        ctypes.byref(max_component), ctypes.byref(flags), fs_name, len(fs_name),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return fs_name.value


def _nt_rename(
    source: _Handle, target_dir: _Handle, target_name: str, *, replace: bool
) -> None:
    encoded = target_name.encode("utf-16-le")
    size = ctypes.sizeof(_FileRenamePrefix) + len(encoded)
    buffer = ctypes.create_string_buffer(size)
    prefix = _FileRenamePrefix.from_buffer(buffer)
    # Ex + POSIX semantics is required to replace the still-open renamed source
    # while its DELETE-sharing handle remains pinned for the transaction.
    prefix.ReplaceIfExists = 0x00000003 if replace else 0
    prefix.RootDirectory = target_dir.value
    prefix.FileNameLength = len(encoded)
    ctypes.memmove(
        ctypes.addressof(buffer) + _FileRenamePrefix.FileName.offset,
        encoded,
        len(encoded),
    )
    iosb = _IoStatusBlock()
    status = int(
        ntdll.NtSetInformationFile(
            source.value, ctypes.byref(iosb), buffer, size,
            FILE_RENAME_INFORMATION_EX_CLASS if replace else FILE_RENAME_INFORMATION_CLASS,
        )
    )
    if status < 0:
        _raise_status(status, "handle-relative rename failed")


def _nt_rename_no_replace(source: _Handle, target_dir: _Handle, target_name: str) -> None:
    _nt_rename(source, target_dir, target_name, replace=False)


def _mark_delete_on_close(handle: _Handle) -> None:
    delete = wintypes.BOOLEAN(1)
    iosb = _IoStatusBlock()
    status = int(
        ntdll.NtSetInformationFile(
            handle.value, ctypes.byref(iosb), ctypes.byref(delete),
            ctypes.sizeof(delete), FILE_DISPOSITION_INFORMATION_CLASS,
        )
    )
    if status < 0:
        _raise_status(status, "cannot clean temporary file")


def _write_all(handle: _Handle, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        chunk = data[offset : offset + 1024 * 1024]
        written = wintypes.DWORD()
        if not kernel32.WriteFile(
            handle.value, chunk, len(chunk), ctypes.byref(written), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value == 0:
            raise OSError("zero-byte write to pinned temporary file")
        offset += written.value
    if not kernel32.FlushFileBuffers(handle.value):
        raise ctypes.WinError(ctypes.get_last_error())


def _file_identity(info: _ByHandleFileInformation) -> tuple[int, int]:
    return (
        int(info.VolumeSerialNumber),
        (int(info.FileIndexHigh) << 32) | int(info.FileIndexLow),
    )


def _assert_single_link(
    handle: _Handle, *, expected_identity: tuple[int, int] | None = None
) -> tuple[int, int]:
    info = _file_info(handle)
    identity = _file_identity(info)
    if expected_identity is not None and identity != expected_identity:
        raise UnsafePathError("replacement target identity changed")
    if int(info.NumberOfLinks) != 1:
        raise UnsafePathError(
            f"replacement must have exactly one hardlink, found {int(info.NumberOfLinks)}"
        )
    return identity


def _atomic_replace_relative(directory: _Handle, target_name: str, data: bytes) -> None:
    _validate_name(target_name)
    temp_name = f".ob-restore-{uuid.uuid4().hex}.tmp"
    temp = _nt_open_relative(
        directory, temp_name,
        desired_access=GENERIC_WRITE | FILE_READ_ATTRIBUTES | DELETE | SYNCHRONIZE,
        share_access=FILE_SHARE_READ | FILE_SHARE_DELETE,
        create_options=(
            FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT
            | FILE_OPEN_REPARSE_POINT
        ),
        disposition=FILE_CREATE,
    )
    renamed = False
    try:
        _write_all(temp, data)
        replacement_identity = _assert_single_link(temp)
        _nt_rename(temp, directory, target_name, replace=True)
        renamed = True
        try:
            _assert_single_link(temp, expected_identity=replacement_identity)
            with _nt_open_relative(
                directory, target_name,
                desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_access=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                create_options=(
                    FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT
                    | FILE_OPEN_REPARSE_POINT | FILE_OPEN_FOR_BACKUP_INTENT
                ),
            ) as target:
                _reject_reparse(target, target_name)
                _assert_single_link(target, expected_identity=replacement_identity)
        except BaseException as exc:
            if isinstance(exc, PostCommitRewriteError):
                raise
            raise PostCommitRewriteError(
                "target replaced but replacement authority could not be verified"
            ) from exc
    finally:
        if not renamed:
            try:
                _mark_delete_on_close(temp)
            except OSError:
                pass
        temp.close()


def require_safe_rename_capability(
    source: str | os.PathLike[str],
    target_directory: str | os.PathLike[str],
) -> None:
    """Fail before caller side effects when this host/path pair is unsupported."""
    if os.name != "nt" or kernel32 is None or ntdll is None:
        raise SafeRenameUnavailable("Windows native safe rename is unavailable")
    if sys.getwindowsversion().build < 16299:
        raise SafeRenameUnavailable(
            "FileRenameInformationEx requires Windows 10 version 1709 or newer"
        )
    _source_path, source_drive, source_parts = _validate_local_absolute(source)
    _target_path, target_drive, _target_parts = _validate_local_absolute(target_directory)
    if not source_parts:
        raise UnsafePathError("volume roots cannot be renamed")
    if source_drive != target_drive:
        raise SafeRenameUnavailable("source and target must be on the same drive")
    filesystem = _filesystem_for_drive(source_drive)
    if filesystem.upper() != "NTFS":
        raise SafeRenameUnavailable(f"filesystem is not admitted: {filesystem or 'unknown'}")


def safe_read_source_snapshot(
    source: str | os.PathLike[str], *, max_source_bytes: int = MAX_SOURCE_BYTES
) -> SafeSourceSnapshot:
    """Read one source through a securely walked and pinned native handle."""
    if os.name != "nt" or kernel32 is None or ntdll is None:
        raise SafeRenameUnavailable("Windows native safe rename is unavailable")
    source_path, drive, parts = _validate_local_absolute(source)
    if not parts:
        raise UnsafePathError("volume roots cannot be read as sources")
    filesystem = _filesystem_for_drive(drive)
    if filesystem.upper() != "NTFS":
        raise SafeRenameUnavailable(f"filesystem is not admitted: {filesystem or 'unknown'}")
    with _open_plain_directory(str(Path(source_path).parent)) as parent:
        with _open_plain_source(parent, parts[-1]) as source_handle:
            return _source_snapshot(source_handle, filesystem, max_source_bytes)


def safe_transform_rename_no_replace(
    source: str | os.PathLike[str],
    target_directory: str | os.PathLike[str],
    target_name: str,
    transform: Callable[[SafeSourceSnapshot], bytes],
    *,
    expected_revision: str | None = None,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> tuple[SafeRenameResult, bytes]:
    """Validate and transform the pinned source, then move and rewrite it.

    ``transform`` runs while the exact source and destination handles remain
    open.  It must perform every content-dependent eligibility check and return
    the complete replacement bytes.  No pathname read occurs between those
    checks and the authoritative rename.
    """
    require_safe_rename_capability(source, target_directory)
    _validate_name(target_name)
    source_path, source_drive, source_parts = _validate_local_absolute(source)
    target_parent_path, target_drive, _target_parts = _validate_local_absolute(
        target_directory
    )
    if source_drive != target_drive or not source_parts:
        raise SafeRenameUnavailable("source and target must be on the same volume")
    filesystem = _filesystem_for_drive(source_drive)
    with _open_plain_directory(str(Path(source_path).parent)) as source_parent:
        with _open_plain_directory(target_parent_path) as target_parent:
            with _open_plain_source(source_parent, source_parts[-1]) as source_handle:
                snapshot = _source_snapshot(
                    source_handle, filesystem, max_source_bytes
                )
                target_info = _file_info(target_parent)
                if snapshot.volume_serial != int(target_info.VolumeSerialNumber):
                    raise SafeRenameUnavailable(
                        "source and target directory are not on the same volume"
                    )
                if (
                    expected_revision is not None
                    and snapshot.revision != expected_revision.casefold()
                ):
                    raise SourceChangedError(
                        "pinned source revision changed before commit"
                    )
                replacement = transform(snapshot)
                if not isinstance(replacement, bytes):
                    raise TypeError("safe transform must return bytes")
                if len(replacement) > max_source_bytes:
                    raise SafeRenameUnavailable("replacement exceeds admitted size")
                _assert_pinned_source_unchanged(
                    source_handle, snapshot, expected_links=1
                )
                _nt_rename_no_replace(source_handle, target_parent, target_name)
                try:
                    _assert_pinned_source_unchanged(
                        source_handle, snapshot, expected_links=1
                    )
                    _atomic_replace_relative(target_parent, target_name, replacement)
                    try:
                        _assert_pinned_source_unchanged(
                            source_handle, snapshot, expected_links=0
                        )
                    except SourceChangedError as exc:
                        raise PostCommitRewriteError(
                            "authority committed but an old-inode hardlink remains"
                        ) from exc
                except BaseException as exc:
                    if isinstance(exc, PostCommitRewriteError):
                        raise
                    raise PostCommitRewriteError(
                        "authority moved but target rewrite did not complete"
                    ) from exc
                return (
                    SafeRenameResult(
                        filesystem=filesystem,
                        volume_serial=snapshot.volume_serial,
                        file_id=snapshot.file_id,
                        source_sha256=snapshot.source_sha256,
                        size=snapshot.size,
                    ),
                    replacement,
                )


def safe_transform_existing_file(
    target_directory: str | os.PathLike[str],
    target_name: str,
    transform: Callable[[SafeSourceSnapshot], bytes],
    *,
    expected_revision: str | None = None,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> tuple[SafeRenameResult, bytes]:
    """Transform an existing named file through one held directory/file pair."""
    if os.name != "nt" or kernel32 is None or ntdll is None:
        raise SafeRenameUnavailable("Windows native safe rename is unavailable")
    _validate_name(target_name)
    _target_path, drive, _parts = _validate_local_absolute(target_directory)
    filesystem = _filesystem_for_drive(drive)
    if filesystem.upper() != "NTFS":
        raise SafeRenameUnavailable(f"filesystem is not admitted: {filesystem or 'unknown'}")
    with _open_plain_directory(target_directory) as target_parent:
        with _open_plain_source(target_parent, target_name) as source_handle:
            snapshot = _source_snapshot(source_handle, filesystem, max_source_bytes)
            if (
                expected_revision is not None
                and snapshot.revision != expected_revision.casefold()
            ):
                raise SourceChangedError("pinned source revision changed before rewrite")
            replacement = transform(snapshot)
            if not isinstance(replacement, bytes):
                raise TypeError("safe transform must return bytes")
            if len(replacement) > max_source_bytes:
                raise SafeRenameUnavailable("replacement exceeds admitted size")
            _assert_pinned_source_unchanged(
                source_handle, snapshot, expected_links=1
            )
            _atomic_replace_relative(target_parent, target_name, replacement)
            try:
                _assert_pinned_source_unchanged(
                    source_handle, snapshot, expected_links=0
                )
            except BaseException as exc:
                raise PostCommitRewriteError(
                    "target rewritten but the retired source could not be verified"
                ) from exc
            return (
                SafeRenameResult(
                    filesystem=filesystem,
                    volume_serial=snapshot.volume_serial,
                    file_id=snapshot.file_id,
                    source_sha256=snapshot.source_sha256,
                    size=snapshot.size,
                ),
                replacement,
            )


def safe_rename_no_replace(
    source: str | os.PathLike[str],
    target_directory: str | os.PathLike[str],
    target_name: str,
    *,
    expected_sha256: str | None = None,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> SafeRenameResult:
    """Rename one pinned local NTFS file without replacing an existing target.

    The source and destination parents are acquired by one-component native
    opens rooted at the drive.  Reparse points are opened, detected, and
    rejected rather than traversed.  All authoritative handles stay open until
    ``NtSetInformationFile`` returns.
    """
    require_safe_rename_capability(source, target_directory)
    _validate_name(target_name)
    source_path, source_drive, source_parts = _validate_local_absolute(source)
    target_parent_path, target_drive, _target_parts = _validate_local_absolute(target_directory)
    if source_drive != target_drive:
        raise SafeRenameUnavailable("source and target must be on the same drive")
    if not source_parts:
        raise UnsafePathError("volume roots cannot be renamed")
    _validate_name(source_parts[-1])
    filesystem = _filesystem_for_drive(source_drive)
    if filesystem.upper() != "NTFS":
        raise SafeRenameUnavailable(f"filesystem is not admitted: {filesystem or 'unknown'}")
    source_parent_path = str(Path(source_path).parent)
    with _open_plain_directory(source_parent_path) as source_parent:
        with _open_plain_directory(target_parent_path) as target_parent:
            with _open_plain_source(source_parent, source_parts[-1]) as source_handle:
                source_info = _file_info(source_handle)
                target_info = _file_info(target_parent)
                if source_info.VolumeSerialNumber != target_info.VolumeSerialNumber:
                    raise SafeRenameUnavailable("source and target directory are not on the same volume")
                raw = _read_all(source_handle, max_source_bytes)
                digest = hashlib.sha256(raw).hexdigest()
                if expected_sha256 is not None and digest != expected_sha256.casefold():
                    raise SourceChangedError("pinned source digest changed before commit")
                _nt_rename_no_replace(source_handle, target_parent, target_name)
                return SafeRenameResult(
                    filesystem=filesystem,
                    volume_serial=source_info.VolumeSerialNumber,
                    file_id=(source_info.FileIndexHigh << 32) | source_info.FileIndexLow,
                    source_sha256=digest,
                    size=len(raw),
                )


def safe_rename_then_replace_bytes(
    source: str | os.PathLike[str],
    target_directory: str | os.PathLike[str],
    target_name: str,
    replacement: bytes,
    *,
    expected_sha256: str,
    max_source_bytes: int = MAX_SOURCE_BYTES,
) -> SafeRenameResult:
    """Commit authority, then atomically replace its bytes via the held directory.

    A crash between the two namespace operations leaves the original source
    bytes at the destination.  Historical Letter recovery deliberately admits
    that archived-typed intermediate and can retry the scalar rewrite.
    """
    require_safe_rename_capability(source, target_directory)
    _validate_name(target_name)
    if len(replacement) > max_source_bytes:
        raise SafeRenameUnavailable("replacement exceeds admitted size")
    source_path, source_drive, source_parts = _validate_local_absolute(source)
    target_parent_path, target_drive, _target_parts = _validate_local_absolute(target_directory)
    if source_drive != target_drive or not source_parts:
        raise SafeRenameUnavailable("source and target must be on the same volume")
    filesystem = _filesystem_for_drive(source_drive)
    with _open_plain_directory(str(Path(source_path).parent)) as source_parent:
        with _open_plain_directory(target_parent_path) as target_parent:
            with _open_plain_source(source_parent, source_parts[-1]) as source_handle:
                source_info = _file_info(source_handle)
                target_info = _file_info(target_parent)
                if source_info.VolumeSerialNumber != target_info.VolumeSerialNumber:
                    raise SafeRenameUnavailable("source and target directory are not on the same volume")
                raw = _read_all(source_handle, max_source_bytes)
                digest = hashlib.sha256(raw).hexdigest()
                if digest != expected_sha256.casefold():
                    raise SourceChangedError("pinned source digest changed before commit")
                _nt_rename_no_replace(source_handle, target_parent, target_name)
                try:
                    _atomic_replace_relative(target_parent, target_name, replacement)
                except BaseException as exc:
                    raise PostCommitRewriteError(
                        "authority moved but target rewrite did not complete"
                    ) from exc
                return SafeRenameResult(
                    filesystem=filesystem,
                    volume_serial=source_info.VolumeSerialNumber,
                    file_id=(source_info.FileIndexHigh << 32) | source_info.FileIndexLow,
                    source_sha256=digest,
                    size=len(raw),
                )


def safe_atomic_replace_bytes(
    target_directory: str | os.PathLike[str],
    target_name: str,
    data: bytes,
) -> None:
    """Atomically replace one named file relative to a securely walked directory."""
    if os.name != "nt" or kernel32 is None or ntdll is None:
        raise SafeRenameUnavailable("Windows native safe rename is unavailable")
    _validate_name(target_name)
    _target_path, drive, _parts = _validate_local_absolute(target_directory)
    filesystem = _filesystem_for_drive(drive)
    if filesystem.upper() != "NTFS":
        raise SafeRenameUnavailable(f"filesystem is not admitted: {filesystem or 'unknown'}")
    with _open_plain_directory(target_directory) as target_parent:
        _atomic_replace_relative(target_parent, target_name, data)


def safe_ensure_directory(
    parent_directory: str | os.PathLike[str], directory_name: str
) -> None:
    """Open or create one plain child directory beneath a pinned plain parent.

    The parent chain is acquired without following reparse points.  Creation is
    relative to that held handle, so a pathname rebind cannot redirect the side
    effect outside the admitted parent.
    """
    if os.name != "nt" or kernel32 is None or ntdll is None:
        raise SafeRenameUnavailable("Windows native safe rename is unavailable")
    _validate_name(directory_name)
    _parent_path, drive, _parts = _validate_local_absolute(parent_directory)
    filesystem = _filesystem_for_drive(drive)
    if filesystem.upper() != "NTFS":
        raise SafeRenameUnavailable(f"filesystem is not admitted: {filesystem or 'unknown'}")
    with _open_plain_directory(parent_directory) as parent:
        child = _nt_open_relative(
            parent,
            directory_name,
            desired_access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_access=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            create_options=(
                FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT
                | FILE_OPEN_REPARSE_POINT | FILE_OPEN_FOR_BACKUP_INTENT
            ),
            disposition=FILE_OPEN_IF,
        )
        try:
            _reject_reparse(child, directory_name)
        finally:
            child.close()
