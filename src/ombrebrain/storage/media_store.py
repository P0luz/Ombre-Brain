"""OB 媒体持久化存储。

本模块把 MCP 调用携带的服务器可读临时文件或 Base64 数据复制到持久媒体目录，
并返回可写入 Markdown frontmatter 的稳定元数据。它不理解记忆内容、不操作桶文件，
也不会因为记忆归档而删除媒体。对外暴露 ``MediaStore`` 和
``MediaPersistenceError``。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

_SAFE_SUFFIX = re.compile(r"^\.[a-zA-Z0-9]{1,10}$")
_DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
# 与 bucket_manager._MEDIA_MAX_ITEMS 同一个契约。那边的截断发生在持久化**之后**，
# 于是 10 万项会被逐个写完再截成 20 条（实测 35.9 秒）。上限得在动手之前就生效。
_DEFAULT_MAX_MEDIA_ITEMS = 20


class MediaPersistenceError(ValueError):
    """媒体无法在 OB 服务器上永久保存。"""


class MediaStore:
    """把媒体复制到持久目录，并生成稳定引用。"""

    def __init__(
        self,
        vault_dir: str,
        media_dir: str,
        *,
        max_bytes: int = _DEFAULT_MAX_MEDIA_BYTES,
        max_items: int = _DEFAULT_MAX_MEDIA_ITEMS,
    ) -> None:
        self.vault_dir = Path(vault_dir).resolve()
        self.media_dir = Path(media_dir).resolve()
        self.max_bytes = max(1, int(max_bytes))
        self.max_items = max(1, int(max_items))
        self.allowed_roots = self._default_allowed_roots()
        self.media_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_allowed_roots() -> tuple[Path, ...]:
        """``path`` 这条路只接受这些根目录下的文件。

        为什么是临时目录：path 本来就是给「客户端刚上传、还躺在服务器临时
        目录里」的文件用的（见模块 docstring，以及下面那句「不能把客户端临时
        路径直接写进记忆」的错误文案）。在此之外的任何服务器文件都不该因为
        一次 MCP 调用就被复制进 vault——那等于把记忆当成任意文件读取器。

        原来这里什么都不查：只验了文件类型、符号链接和大小，``..`` 与绝对
        路径畅通无阻。真机复现过一次读走 vault 外文件并落成记忆附件。
        """
        roots: list[Path] = []
        # B108 在这里是反的：它防的是「往写死的 /tmp 里建临时文件」，而这行是
        # 把 /tmp 列进**允许读取的白名单**——这个函数本身就是那道限制。
        # 显式列它是因为 gettempdir() 在 macOS 上返回 /var/folders/...，
        # 而容器里的客户端通常把上传文件放在 /tmp，只认 gettempdir() 会漏掉。
        for candidate in (tempfile.gettempdir(), os.environ.get("TMPDIR") or "", "/tmp"):  # nosec B108
            if not candidate:
                continue
            try:
                resolved = Path(candidate).resolve()
            except OSError:
                continue
            if resolved not in roots:
                roots.append(resolved)
        return tuple(roots)

    def _reject_outside_allowed_roots(self, source: Path, raw_path: str) -> None:
        try:
            resolved = source.resolve()
        except OSError as exc:
            raise MediaPersistenceError(
                f"媒体临时路径在 OB 服务器上不可读：{raw_path}。"
                "请改传 data_base64，不能把客户端临时路径直接写进记忆。"
            ) from exc
        for root in self.allowed_roots:
            if resolved == root or root in resolved.parents:
                return
        raise MediaPersistenceError(
            f"媒体路径不在允许的临时目录内：{raw_path}。"
            "请改传 data_base64——服务器上的文件不会因为一次调用就变成记忆附件。"
        )

    @staticmethod
    def _suffix(name: str, mime_type: str) -> str:
        suffix = Path(name).suffix.lower()
        if _SAFE_SUFFIX.fullmatch(suffix):
            return suffix
        guessed = mimetypes.guess_extension(mime_type or "") or ".bin"
        return guessed if _SAFE_SUFFIX.fullmatch(guessed) else ".bin"

    def _stable_path(self, bucket_id: str, digest: str, suffix: str) -> Path:
        safe_bucket = re.sub(r"[^a-zA-Z0-9_.-]", "_", bucket_id)[:128]
        target_dir = (self.media_dir / safe_bucket).resolve()
        if self.media_dir not in target_dir.parents:
            raise MediaPersistenceError("媒体目录越界，已拒绝保存。")
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{digest}{suffix}"

    def _frontmatter_path(self, target: Path) -> str:
        try:
            return target.relative_to(self.vault_dir).as_posix()
        except ValueError:
            return str(target)

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        """在目标目录内写临时文件后原子替换，避免崩溃留下半张媒体。"""
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _read_path(self, raw_path: str) -> tuple[bytes, str]:
        source = Path(raw_path).expanduser()
        try:
            before_open = os.lstat(source)
        except OSError as exc:
            raise MediaPersistenceError(
                f"媒体临时路径在 OB 服务器上不可读：{raw_path}。"
                "请改传 data_base64，不能把客户端临时路径直接写进记忆。"
            ) from exc
        if stat.S_ISLNK(before_open.st_mode):
            raise MediaPersistenceError(
                f"媒体路径必须是普通文件，不能是符号链接：{raw_path}"
            )
        if not stat.S_ISREG(before_open.st_mode):
            raise MediaPersistenceError(
                f"媒体路径必须是普通文件：{raw_path}"
            )
        # 放在符号链接检查之后：那一步已经保证最后一段不是链接，这里再按
        # 解析后的真实路径判定它落在哪个根下。
        self._reject_outside_allowed_roots(source, raw_path)

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(source, flags)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise MediaPersistenceError(
                    f"媒体路径必须是普通文件：{raw_path}"
                )

            # 以已打开的文件描述符为读取真源。打开后再比较路径身份，
            # 可在不二次按路径读取的前提下检出并发替换。
            after_open = os.lstat(source)
            if stat.S_ISLNK(after_open.st_mode) or (
                after_open.st_dev,
                after_open.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise MediaPersistenceError(
                    f"媒体路径在打开期间发生变化：{raw_path}"
                )
            if opened.st_size > self.max_bytes:
                raise MediaPersistenceError(
                    f"媒体文件超过单项上限 {self.max_bytes} 字节：{raw_path}"
                )

            with os.fdopen(fd, "rb") as handle:
                fd = None
                data = handle.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise MediaPersistenceError(
                    f"媒体文件超过单项上限 {self.max_bytes} 字节：{raw_path}"
                )
            return data, source.name
        except MediaPersistenceError:
            raise
        except OSError as exc:
            raise MediaPersistenceError(
                f"媒体临时路径在 OB 服务器上不可读：{raw_path}。"
                "请改传 data_base64，不能把客户端临时路径直接写进记忆。"
            ) from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _decode_base64(self, value: str) -> bytes:
        payload = value.strip()
        if payload.startswith("data:"):
            _, separator, payload = payload.partition(",")
            if not separator:
                raise MediaPersistenceError("媒体 data URI 缺少数据部分。")
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MediaPersistenceError("媒体 data_base64 不是有效 Base64。") from exc
        if len(data) > self.max_bytes:
            raise MediaPersistenceError(
                f"媒体数据超过单项上限 {self.max_bytes} 字节。"
            )
        return data

    def _load_one(self, item: Any) -> tuple[bytes, str, str]:
        """把一项媒体读进内存。所有可失败的步骤都在这里，不写任何文件。

        单独抽出来是为了让调用方能在真正落盘之前先问一句「这批读得进来吗」，
        见 precheck()。
        """
        entry = {"path": item} if isinstance(item, str) else dict(item or {})
        mime_type = str(entry.get("type") or entry.get("mime_type") or "")[:128]
        if entry.get("data_base64"):
            data = self._decode_base64(str(entry["data_base64"]))
            source_name = str(entry.get("filename") or entry.get("title") or "media")
        else:
            raw_path = str(entry.get("path") or "").strip()
            if not raw_path:
                raise MediaPersistenceError("media 每项必须提供 path 或 data_base64。")
            data, source_name = self._read_path(raw_path)
        return data, source_name, mime_type

    def _persist_one(self, bucket_id: str, item: Any) -> dict[str, Any]:
        entry = {"path": item} if isinstance(item, str) else dict(item or {})
        data, source_name, mime_type = self._load_one(item)
        digest = hashlib.sha256(data).hexdigest()
        suffix = self._suffix(source_name, mime_type)
        target = self._stable_path(bucket_id, digest, suffix)
        if not target.exists():
            self._atomic_write(target, data)
        result: dict[str, Any] = {
            "path": self._frontmatter_path(target),
            "sha256": digest,
            "size": len(data),
            "stored": True,
        }
        for key, limit in (("title", 200), ("type", 128), ("note", 500)):
            value = entry.get(key)
            if value:
                result[key] = str(value)[:limit]
        return result

    async def precheck(self, media: Any) -> None:
        """只读不写：确认这批媒体每一项都能取到字节。

        为什么需要它：媒体的持久化发生在建桶那一步，比 hold 写原文证据晚。
        等到那时才失败，原文已经落进 _sources，而给调用方的错误说的是
        「未创建任何桶」——它据此重试，上一半副作用已经在那了。有它，
        所有可失败的准备就都排在任何写入之前。

        代价是 path 类媒体会被读两遍，所以调用方只在「确实要先写别的东西」
        时才调它，普通 hold 不付这个成本。
        """
        if not media:
            return
        items = self._bounded_items(media)
        await asyncio.to_thread(lambda: [self._load_one(item) for item in items])

    def _bounded_items(self, media: Any) -> list[Any]:
        """归一成列表并当场卡上限——超了就在读第一个字节之前拒绝。"""
        items = media if isinstance(media, list) else [media]
        if len(items) > self.max_items:
            raise MediaPersistenceError(
                f"media 一次最多 {self.max_items} 项，收到 {len(items)} 项；本次未保存任何媒体。"
            )
        return items

    async def persist(self, bucket_id: str, media: Any) -> list[dict[str, Any]]:
        """永久保存一项或多项媒体；任何一项失败则明确报错。"""
        if not media:
            return []
        items = self._bounded_items(media)
        return await asyncio.to_thread(
            lambda: [self._persist_one(bucket_id, item) for item in items]
        )
