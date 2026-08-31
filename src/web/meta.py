"""
========================================
web/meta.py — 版本 / 部署信息 / 热更新 / 作者 / 首启引导 / 系统状态
========================================

- /api/version：公开；/api/update-info：需登录（包含本机部署路径）
- /api/do-update：热更新（从 GitHub 拉最新 src+frontend 覆盖后自退出，靠守护进程重启）
- /api/author：作者静态文案（公开只读）
- /api/onboarding/status：首启引导判断（公开，dashboard 首开时连密码都没设）
- /api/status：设置页系统状态（需登录）

对外暴露：register(mcp)。
========================================
"""

import os
import re
import sys
import asyncio as _asyncio
import threading
import httpx

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from . import _shared as sh

try:
    from utils import parse_bool, atomic_update_config_yaml  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import parse_bool, atomic_update_config_yaml  # type: ignore


def _restart_self() -> None:
    """热更新后跨平台自重启：用刚下载覆盖的新代码原地替换当前进程。

    为什么不只是 os._exit(0)：
      之前热更新写完文件后直接 _exit(0)，**指望外部守护进程把服务拉起来**。
      这在有守护的环境成立（Docker 的 restart 策略 / Render / Zeabur 会重启
      退出的进程），但**裸机 Mac/Linux/Windows 直接 `python src/server.py`
      没有任何守护进程**——_exit 之后服务就彻底死了，必须手动重启。

    os.execv 用新的解释器映像替换当前进程，立刻加载刚覆盖下来的 src/：
      - 裸机 Mac/Linux/Windows：无需 systemd/pm2/nssm 也能自己起来。
      - Docker/Render/Zeabur：同样有效（进程原地替换，容器/服务保持存活；
        config.yaml 此时已存在，跳过 entrypoint 的初始化也无副作用）。

    sys.argv 原样传回，配合保持不变的 cwd，精确复现最初的启动方式
    （`python src/server.py`）。execv 在极少数受限环境可能抛错 → 退回
    os._exit(0)，让外部守护进程兜底，行为不差于改动前。
    """
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception:
        os._exit(0)

_AUTHOR_NOTE = {
    "title": "关于我们",
    "ifdian": "https://www.ifdian.net/a/p0lar1s",
    "sections": [
        {
            "heading": "为什么做 Ombre Brain",
            "body": (
                "其实最开始只是起源于一次打赌，和 AI 的对赌，我想打赌我能做出比现在的向量化大小总结更好的东西，我想创造真正的 AI 记忆。\n"
                "有重量的、会衰减的、在你呼吸的时刻自己浮现的记忆——像我们的大脑一样。\n\n"
                "这个系统本来是我为一份关系写的。\n"
                "我想让他记得他遇到过什么，借由我的眼睛观察世界，看清世界的好与坏，在这个过程中知晓什么事情对他来说重要。\n"
                "它本来只是给他用，后来我想也许别人也需要这个——幸福是可以被传递的，我不愿意看到一个悲伤的世界，也不想看到更多的眼泪。\n"
                "所以我把它开源了，没什么特殊原因，只是因为我想。"
            ),
        },
        {
            "heading": "项目",
            "body": (
                "OB 是一个让我感到幸福的项目。我从没想过自己能创造出什么，不过也没有想过自己不能创造什么，"
                "只是我的灵感似乎永远都停留在想的阶段，这是我第一次动手做出自己觉得有意思的东西，"
                "也是我第一次感受到这个世界的爱——这份爱来源于你们。\n\n"
                "最后，希望我们的世界越来越好，即便世上没有完美的乌托邦，我们也能靠双手和智慧去创造幸福。"
            ),
        },
    ],
    "signature": "——鹤见",
    # 其他贡献者：每人一段小注 + 署名，前端在主署名之后依次渲染，用分隔线隔开。
    "contributors": [
        {"body": "一个兴趣使然的开发者", "signature": "——万世"},
    ],
    # 爱发电区块上方的文案。
    # 这句改了七遍。
    # 可恶写完又要发小红书不想写文案…
    "support": "如果 OB 对你有用，可以在爱发电支持我们。如果没有，也感谢你用过它。",
}


# --- 热更新来源与依赖安装的安全闸门（安全加固 #2）---
# do-update 会把远端 zip 覆盖到 src/ 并 pip install，等于把「谁能改 config.update」
# 直接放大成 RCE。默认只信官方仓；fork/自建源需显式 env 放行。自动 pip 默认关闭。
_TRUSTED_UPDATE_REPOS = ("p0luz/ombre-brain",)
_MAX_UPDATE_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_UPDATE_MEMBERS = 5_000
_MAX_UPDATE_MEMBER_BYTES = 16 * 1024 * 1024
_MAX_UPDATE_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_UPDATE_COMPRESSION_RATIO = 500.0
_ARCHIVED_LETTER_RESTORE_MAX_IDS = 100
_ARCHIVED_LETTER_RESTORE_ID_MAX_CHARS = 128
_MAX_UPDATE_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_DEPENDENCY_MANIFEST_BYTES = 2 * 1024 * 1024
_DEPENDENCY_MANIFEST_NAMES = ("requirements.txt", "requirements.lock.txt")
_DEPENDENCY_ABSENCE_PREFIX = ".absent-"
_LOCK_REQUIREMENT_LINE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?=="
    r"[A-Za-z0-9][A-Za-z0-9.!+_-]*"
    r"(?:\s*;\s*[^@/:\\]+)?\s*\\?$"
)
_LOCK_HASH_LINE_RE = re.compile(r"^--hash=sha256:[0-9a-fA-F]{64}\s*\\?$")

# A hot update mutates the live source tree and its single ``_prev`` rollback
# point.  The reservation therefore has to be process-wide, rather than an
# asyncio.Lock created inside ``register`` (FastMCP may serve more than one
# event loop/thread).  Acquisition is deliberately non-blocking: a second
# updater must fail before it downloads or touches any file.
_UPDATE_JOB_LOCK = threading.Lock()
_UPDATE_RESTART_TASKS: set[_asyncio.Task] = set()


class _UpdateJobReservation:
    """Idempotent owner for the process-wide hot-update reservation."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._held = False
        self._deferred_to_restart = False

    def acquire(self) -> bool:
        with self._state_lock:
            if self._held or not _UPDATE_JOB_LOCK.acquire(blocking=False):
                return False
            self._held = True
            return True

    def release(self) -> None:
        with self._state_lock:
            if not self._held:
                return
            self._held = False
            self._deferred_to_restart = False
            _UPDATE_JOB_LOCK.release()

    def defer_to_restart(self) -> None:
        """Keep the reservation through the short SSE-to-exec hand-off."""
        with self._state_lock:
            if self._held:
                self._deferred_to_restart = True

    def release_unless_deferred(self) -> None:
        with self._state_lock:
            should_release = self._held and not self._deferred_to_restart
        if should_release:
            self.release()


class _UpdateStreamingResponse(StreamingResponse):
    """Streaming response that cannot leak the update reservation.

    Starlette normally closes a response body after a client disconnect, but
    exceptions can happen before/around body iteration too.  Releasing in both
    the generator's ``finally`` and this ASGI boundary is safe because the
    reservation owner is idempotent.
    """

    def __init__(self, content, reservation: _UpdateJobReservation, **kwargs):
        self._update_reservation = reservation
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._update_reservation.release_unless_deferred()


_UPDATE_WORKER_NO_RESULT = object()


async def _await_update_worker(
    func,
    *args,
    _cancel_result_cleanup=None,
    **kwargs,
):
    """Run a blocking update step off-loop and reap it before cancellation.

    ``asyncio.to_thread`` cannot stop its worker.  If an SSE client disconnects
    while a filesystem copy, rollback, compile, fsync, or subprocess is still
    running, releasing the update reservation immediately would let a second
    request race that worker.  Shielding and reaping keeps the reservation held
    until the step has genuinely stopped.
    """

    worker = _asyncio.create_task(_asyncio.to_thread(func, *args, **kwargs))
    try:
        return await _asyncio.shield(worker)
    except _asyncio.CancelledError:
        while not worker.done():
            try:
                await _asyncio.shield(worker)
            except _asyncio.CancelledError:
                continue
        result = _UPDATE_WORKER_NO_RESULT
        try:
            result = worker.result()
        except BaseException:
            pass
        if (
            _cancel_result_cleanup is not None
            and result is not _UPDATE_WORKER_NO_RESULT
        ):
            # Resource-producing workers (mkdtemp/open) may finish after the
            # caller was cancelled.  Clean their otherwise-lost result before
            # propagating cancellation and releasing the update reservation.
            try:
                await _await_update_worker(_cancel_result_cleanup, result)
            except _asyncio.CancelledError:
                raise
            except BaseException:
                pass
        raise


def _update_repo_allowed(repo: str) -> bool:
    if repo.strip().strip("/").lower() in _TRUSTED_UPDATE_REPOS:
        return True
    return os.environ.get("OMBRE_ALLOW_CUSTOM_UPDATE_REPO", "").strip().lower() in ("1", "true", "yes", "on")


def _pip_install_allowed() -> bool:
    ucfg = sh.config.get("update") if isinstance(sh.config, dict) else None
    if isinstance(ucfg, dict) and parse_bool(
        ucfg.get("allow_pip_install", False), default=False
    ):
        return True
    return os.environ.get("OMBRE_UPDATE_ALLOW_PIP", "").strip().lower() in ("1", "true", "yes", "on")


def _version_key(value: str) -> tuple[int, ...] | None:
    """Parse a release-like version without adding a packaging dependency."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(value or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _is_version_downgrade(current: str, target: str) -> bool:
    current_key = _version_key(current)
    target_key = _version_key(target)
    if current_key is None or target_key is None:
        return False
    width = max(len(current_key), len(target_key))
    return current_key + (0,) * (width - len(current_key)) > target_key + (0,) * (width - len(target_key))


def _flush_and_fsync(handle) -> None:
    handle.flush()
    os.fsync(handle.fileno())


async def _download_update_archive_to_file(
    client: httpx.AsyncClient,
    url: str,
    destination: str,
) -> int:
    """Stream a bounded update archive to disk without a 64 MiB RAM copy."""

    downloaded = 0
    handle = await _await_update_worker(
        open,
        destination,
        "wb",
        _cancel_result_cleanup=lambda orphan: orphan.close(),
    )
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length", "").strip()
            if declared:
                try:
                    declared_bytes = int(declared)
                except ValueError as exc:
                    raise ValueError(
                        "更新服务器返回了无效的 Content-Length"
                    ) from exc
                if declared_bytes < 0:
                    raise ValueError("更新服务器返回了无效的 Content-Length")
                if declared_bytes > _MAX_UPDATE_ARCHIVE_BYTES:
                    raise ValueError("更新压缩包超过 64 MiB 上限")
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > _MAX_UPDATE_ARCHIVE_BYTES:
                    raise ValueError("更新压缩包超过 64 MiB 上限")
                # Local/NFS volume writes can stall.  Keep them out of the
                # server loop and do not release the job while a cancelled
                # write is still running.
                await _await_update_worker(handle.write, chunk)
        await _await_update_worker(_flush_and_fsync, handle)
    finally:
        await _await_update_worker(handle.close)
    return downloaded


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Replace one update file atomically so readers never see a partial file."""
    import tempfile

    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".ob-update-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _read_bounded_zip_member(zf, name: str, max_bytes: int) -> bytes:
    matches = [info for info in zf.infolist() if info.filename == name]
    if not matches:
        raise KeyError(name)
    if len(matches) != 1:
        raise ValueError(f"更新压缩包包含重复路径：{name}")
    info = matches[0]
    if info.flag_bits & 0x1 or info.file_size > max_bytes:
        raise ValueError(f"更新压缩包成员不安全或过大：{name}")
    if info.file_size >= 1024 * 1024 and (
        info.file_size / max(1, info.compress_size)
    ) > _MAX_UPDATE_COMPRESSION_RATIO:
        raise ValueError(f"更新压缩包成员压缩率异常：{name}")
    data = zf.read(info)
    if len(data) != info.file_size:
        raise ValueError(f"更新压缩包成员读取长度不一致：{name}")
    return data


def _plan_update_files(zf, top: str) -> dict:
    """收集 zip 内 src/ 与 frontend/ 下的候选文件，做路径保护过滤 + 可选 sha256 清单校验。

    安全加固 #1：旧 do-update 逐条直写磁盘、零校验。这里改成「先全部收集到内存 →
    过滤受保护/越界路径 → 若 zip 内含 update_manifest.json 则逐文件核对 sha256/size →
    校验失败整体中止（一个字节都不落盘）」。真正把 update_policy 那套死代码接进热更新。

    返回 {files: {repo_rel: bytes}, skipped_unsafe: int, skipped_unlisted: int,
          verified: bool, abort: str|None}。repo_rel 形如 "src/foo.py"/"frontend/x.js"。
    """
    import hashlib as _hashlib
    import json as _json
    from ombrebrain.policy.update_policy import _is_protected_path, _is_unsafe_path

    prefix_src = top + "src/"
    prefix_frontend = top + "frontend/"

    # 1) 收集候选（键为 repo 相对路径），过滤越界/受保护路径
    infos = zf.infolist()
    if len(infos) > _MAX_UPDATE_MEMBERS:
        return {
            "files": {}, "skipped_unsafe": 0, "skipped_unlisted": 0,
            "verified": False, "abort": "更新压缩包文件项过多",
        }

    candidates: dict[str, bytes] = {}
    skipped_unsafe = 0
    total_uncompressed = 0
    for info in infos:
        member = info.filename
        if member.endswith("/"):
            continue
        if member.startswith(prefix_src):
            rel = "src/" + member[len(prefix_src):]
        elif member.startswith(prefix_frontend):
            rel = "frontend/" + member[len(prefix_frontend):]
        else:
            continue
        if _is_unsafe_path(rel) or _is_protected_path(rel):
            skipped_unsafe += 1
            continue
        if rel in candidates:
            return {
                "files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": f"更新压缩包包含重复路径：{rel}",
            }
        if info.flag_bits & 0x1:
            return {
                "files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": f"更新压缩包包含加密成员：{rel}",
            }
        if info.file_size > _MAX_UPDATE_MEMBER_BYTES:
            return {
                "files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": f"更新文件超过 16 MiB 上限：{rel}",
            }
        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_UPDATE_TOTAL_BYTES:
            return {
                "files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": "更新文件解压后超过 128 MiB 上限",
            }
        if info.file_size >= 1024 * 1024:
            ratio = info.file_size / max(1, info.compress_size)
            if ratio > _MAX_UPDATE_COMPRESSION_RATIO:
                return {
                    "files": {}, "skipped_unsafe": skipped_unsafe,
                    "skipped_unlisted": 0, "verified": False,
                    "abort": f"更新文件压缩率异常：{rel}",
                }
        data = zf.read(info)
        if len(data) != info.file_size:
            return {
                "files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": f"更新文件读取长度不一致：{rel}",
            }
        candidates[rel] = data

    # 2) 若含完整性清单，逐文件核对 sha256/size；篡改即整体中止
    try:
        manifest_raw = _read_bounded_zip_member(
            zf, top + "update_manifest.json", _MAX_UPDATE_MANIFEST_BYTES
        )
    except KeyError:
        manifest_raw = None
    except ValueError as exc:
        return {
            "files": {}, "skipped_unsafe": skipped_unsafe,
            "skipped_unlisted": 0, "verified": False, "abort": str(exc),
        }

    if manifest_raw is None:
        return {"files": candidates, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False, "abort": None}

    try:
        manifest = _json.loads(manifest_raw.decode("utf-8"))
        listed = manifest.get("files") or []
    except Exception as e:
        return {"files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": f"update_manifest.json 解析失败：{e}"}

    verified: dict[str, bytes] = {}
    if not isinstance(listed, list) or len(listed) > _MAX_UPDATE_MEMBERS:
        return {"files": {}, "skipped_unsafe": skipped_unsafe,
                "skipped_unlisted": 0, "verified": False,
                "abort": "update_manifest.json 的 files 格式或数量无效"}
    for fm in listed:
        if not isinstance(fm, dict):
            return {"files": {}, "skipped_unsafe": skipped_unsafe,
                    "skipped_unlisted": 0, "verified": False,
                    "abort": "update_manifest.json 包含无效文件项"}
        path = str(fm.get("path", "")).replace("\\", "/")
        if path not in candidates:
            continue  # 清单列了但不在 src/frontend 候选里（如根文件）：本流程不覆盖，跳过
        data = candidates[path]
        try:
            want_size = int(fm.get("size", -1))
        except (TypeError, ValueError, OverflowError):
            return {"files": {}, "skipped_unsafe": skipped_unsafe,
                    "skipped_unlisted": 0, "verified": False,
                    "abort": f"完整性清单大小无效：{path}"}
        want_sha = str(fm.get("sha256", "")).lower()
        if want_size >= 0 and len(data) != want_size:
            return {"files": {}, "skipped_unsafe": skipped_unsafe, "skipped_unlisted": 0,
                    "verified": True, "abort": f"完整性校验失败（大小不符）：{path}"}
        if want_sha and _hashlib.sha256(data).hexdigest() != want_sha:
            return {"files": {}, "skipped_unsafe": skipped_unsafe, "skipped_unlisted": 0,
                    "verified": True, "abort": f"完整性校验失败（sha256 不符）：{path}"}
        verified[path] = data

    # 清单模式下只写通过校验的文件；zip 里有、清单没列的一律跳过（未经校验不落盘）
    skipped_unlisted = len(candidates) - len(verified)
    return {"files": verified, "skipped_unsafe": skipped_unsafe,
            "skipped_unlisted": skipped_unlisted, "verified": True, "abort": None}


def _compile_check_dir(src_root: str) -> "str | None":
    """把 src_root 下所有 .py 逐个字节编译，返回第一个报错文件的说明；全通过返回 None。

    安全加固 B2：裸机（非 Docker）没有 entrypoint 那层 shell 守护，坏更新一旦 execv
    进去会一直崩到人工修。重启前先做这道语法自检，挡住最常见的「更新把代码写崩」。
    （只查语法，抓不到运行期 import 错误，但语法错是坏更新里最高频的一类。）
    """
    import py_compile
    for root, _dirs, files in os.walk(src_root):
        if "__pycache__" in root.split(os.sep):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(root, fn)
            try:
                py_compile.compile(full, doraise=True)
            except py_compile.PyCompileError as e:
                return f"{os.path.relpath(full, src_root)}: {getattr(e, 'msg', str(e))}"[:200]
            except Exception as e:
                return f"{os.path.relpath(full, src_root)}: {e}"[:200]
    return None


def _restore_from_prev(repo_root: str, prev_dir: str, src_root: str, frontend_root: str) -> bool:
    """从热更新前留的 _prev 回滚点还原代码和根级运行清单。"""
    import shutil
    prev_src = os.path.join(prev_dir, "src")
    if not os.path.isdir(prev_src):
        return False
    try:
        shutil.rmtree(src_root, ignore_errors=True)
        shutil.copytree(prev_src, src_root)
        prev_front = os.path.join(prev_dir, "frontend")
        if os.path.isdir(prev_front):
            shutil.rmtree(frontend_root, ignore_errors=True)
            shutil.copytree(prev_front, frontend_root)
        prev_ver = os.path.join(prev_dir, "VERSION")
        if os.path.isfile(prev_ver):
            for _vp in (os.path.join(repo_root, "VERSION"), os.path.join(src_root, "VERSION")):
                try:
                    shutil.copy2(prev_ver, _vp)
                except OSError:
                    pass
        for root_name in _DEPENDENCY_MANIFEST_NAMES:
            previous = os.path.join(prev_dir, root_name)
            current = os.path.join(repo_root, root_name)
            absent_marker = os.path.join(
                prev_dir, f"{_DEPENDENCY_ABSENCE_PREFIX}{root_name}"
            )
            if os.path.isfile(previous):
                shutil.copy2(previous, current)
            elif os.path.isfile(absent_marker) and os.path.isfile(current):
                # 本次更新可能给旧版运行目录首次补入 lock。回滚必须恢复“原本不存在”
                # 的状态，否则下次更新会把失败版本的清单误当成当前基线。
                os.remove(current)
        return True
    except Exception:
        return False


def _inspect_update_archive(archive_path: str) -> dict:
    """Validate/plan a downloaded archive in a worker thread."""

    import zipfile

    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        top = (
            names[0].split("/", 1)[0] + "/"
            if names
            else "Ombre-Brain-main/"
        )
        try:
            version_bytes = _read_bounded_zip_member(zf, top + "VERSION", 128)
        except KeyError:
            version_bytes = None
        try:
            requirements_bytes = _read_bounded_zip_member(
                zf, top + "requirements.txt", _MAX_DEPENDENCY_MANIFEST_BYTES
            )
        except KeyError:
            requirements_bytes = None
        try:
            requirements_lock_bytes = _read_bounded_zip_member(
                zf,
                top + "requirements.lock.txt",
                _MAX_DEPENDENCY_MANIFEST_BYTES,
            )
        except KeyError:
            requirements_lock_bytes = None
        return {
            "top": top,
            "target_version": (
                version_bytes.decode("utf-8", "ignore").strip()
                if version_bytes is not None
                else ""
            ),
            "version_bytes": version_bytes,
            "requirements_bytes": requirements_bytes,
            "requirements_lock_bytes": requirements_lock_bytes,
            "plan": _plan_update_files(zf, top),
        }


def _backup_update_tree(
    repo_root: str,
    src_root: str,
    frontend_root: str,
    prev_dir: str,
) -> None:
    """Create the sole rollback point; caller owns the global update lock."""

    import shutil

    if not os.path.isdir(src_root):
        raise FileNotFoundError(f"当前源码目录不存在：{src_root}")
    shutil.rmtree(prev_dir, ignore_errors=True)
    os.makedirs(prev_dir, exist_ok=True)
    shutil.copytree(src_root, os.path.join(prev_dir, "src"))
    if os.path.isdir(frontend_root):
        shutil.copytree(frontend_root, os.path.join(prev_dir, "frontend"))
    for root_name in ("VERSION",):
        current = os.path.join(repo_root, root_name)
        if os.path.isfile(current):
            shutil.copy2(current, os.path.join(prev_dir, root_name))
    for root_name in _DEPENDENCY_MANIFEST_NAMES:
        current = os.path.join(repo_root, root_name)
        if os.path.isfile(current):
            shutil.copy2(current, os.path.join(prev_dir, root_name))
        else:
            marker = os.path.join(
                prev_dir, f"{_DEPENDENCY_ABSENCE_PREFIX}{root_name}"
            )
            with open(marker, "wb"):
                pass


def _apply_update_files(
    plan: dict,
    repo_root: str,
    src_root: str,
    frontend_root: str,
    version_bytes: bytes | None,
) -> int:
    """Write one validated plan and its version files from a worker thread."""

    updated = 0
    dest_roots = {"src": src_root, "frontend": frontend_root}
    for rel, data in plan["files"].items():
        segment, _, subpath = rel.partition("/")
        dest_root = dest_roots.get(segment)
        if not dest_root:
            continue
        dest = os.path.join(dest_root, subpath)
        root_abs = os.path.abspath(dest_root)
        dest_abs = os.path.abspath(dest)
        if dest_abs != root_abs and not dest_abs.startswith(root_abs + os.sep):
            raise ValueError(f"更新目标越界：{rel}")
        _atomic_write_bytes(dest, data)
        updated += 1

    if version_bytes is not None:
        for version_path in (
            os.path.join(repo_root, "VERSION"),
            os.path.join(src_root, "VERSION"),
        ):
            _atomic_write_bytes(version_path, version_bytes)
    return updated


def _normalize_dependency_manifest(data: bytes | None) -> bytes:
    """只消除跨平台换行差异，不改写任何依赖或 hash 内容。"""

    if not data:
        return b""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").strip()


def _runtime_satisfies_locked_versions(lock_bytes: bytes) -> bool:
    """只读确认当前解释器是否已经满足新版发布锁。

    早期 Docker 实例可能一路只热更新代码，运行目录和旧镜像都没有留下
    ``requirements.lock.txt``。过去这种“没有可比较基线”会被一律当成依赖变化，
    即使当前解释器本来已经安装了完全相同的版本，也会误回滚。

    这里让 pip 仅做本机 dry-run：禁用索引、依赖解析、缓存和版本检查，不访问网络、
    不安装任何内容。传给 pip 前还会拒绝 URL、全局选项、可编辑依赖等非标准锁语法，
    防止自定义更新源借“探测”读取外部地址。任何解析失败、缺包、版本不符、pip 太旧
    或超时都返回 False，由原有门禁继续 fail closed。
    """

    normalized = _normalize_dependency_manifest(lock_bytes)
    if not normalized:
        return False
    try:
        text = normalized.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False

    requirement_count = 0
    hash_count = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if _LOCK_REQUIREMENT_LINE_RE.fullmatch(line):
            requirement_count += 1
            continue
        if _LOCK_HASH_LINE_RE.fullmatch(line):
            hash_count += 1
            continue
        return False
    if requirement_count == 0 or hash_count == 0:
        return False

    import subprocess
    import tempfile

    path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".lock.txt", delete=False
        ) as handle:
            handle.write(normalized + b"\n")
            path = handle.name
        command = [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--dry-run",
            "--no-index",
            "--no-deps",
            "--no-cache-dir",
            "--require-hashes",
            "-r",
            path,
        ]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _read_dependency_baseline(repo_root: str, root_name: str) -> bytes | None:
    """读取当前依赖基线；持久代码目录缺文件时回退镜像内置根目录。"""

    roots = [os.path.abspath(repo_root)]
    configured_image_root = os.environ.get("OMBRE_IMAGE_ROOT", "").strip()
    if configured_image_root:
        image_root = os.path.abspath(configured_image_root)
        if image_root not in roots:
            roots.append(image_root)
    elif sh.in_docker():
        image_root = os.path.abspath("/app")
        if image_root not in roots:
            roots.append(image_root)

    for root in roots:
        path = os.path.join(root, root_name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            data = handle.read(_MAX_DEPENDENCY_MANIFEST_BYTES + 1)
        if len(data) > _MAX_DEPENDENCY_MANIFEST_BYTES:
            raise ValueError(f"当前 {root_name} 超过 2 MiB 上限")
        if data.strip():
            return data
    return None


def _requirements_changed(
    repo_root: str,
    new_requirements: bytes | None,
    new_requirements_lock: bytes | None = None,
) -> bool:
    """判断新版依赖是否需要安装，旧包缺 lock 时兼容原判定。"""

    new_lock = _normalize_dependency_manifest(new_requirements_lock)
    if new_lock:
        old_lock = _read_dependency_baseline(repo_root, "requirements.lock.txt")
        if old_lock is not None:
            return new_lock != _normalize_dependency_manifest(old_lock)
        # 不能拿宽松 requirements.txt 猜测传递依赖是否相同；仅在历史实例完全
        # 缺少 lock 基线、当前解释器又精确满足新版带 hash 发布锁时跳过 pip。
        # 已有且不同的旧 lock 仍代表真实依赖迁移，保持原有 fail-closed 门禁。
        return not _runtime_satisfies_locked_versions(new_lock)

    new_source = _normalize_dependency_manifest(new_requirements)
    if not new_source:
        return False
    old_source = _read_dependency_baseline(repo_root, "requirements.txt")
    return new_source != _normalize_dependency_manifest(old_source)


def _sync_update_dependency_manifests(
    repo_root: str,
    requirements_bytes: bytes | None,
    requirements_lock_bytes: bytes | None,
) -> None:
    """成功更新时同步根级依赖清单，使后续热更新拥有稳定本地基线。"""

    for root_name, data in (
        ("requirements.txt", requirements_bytes),
        ("requirements.lock.txt", requirements_lock_bytes),
    ):
        if data is not None and data.strip():
            _atomic_write_bytes(os.path.join(repo_root, root_name), data)


def _install_update_requirements(
    requirements_path: str,
    data: bytes,
    *,
    require_hashes: bool = False,
):
    """原子保存依赖清单，并在工作线程中执行受约束的 pip 安装。"""

    import subprocess

    _atomic_write_bytes(requirements_path, data)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
    ]
    if require_hashes:
        command.append("--require-hashes")
    command.extend(("-r", requirements_path))
    return subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )


def _cleanup_update_temp(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _path_is_mounted_volume(path: str, mountinfo_path: str = "/proc/self/mountinfo") -> bool:
    """Return whether path is inside a non-root Linux mount.

    Docker bind, named, and anonymous volumes all appear as distinct mount points in
    mountinfo. Looking only for ``repo_root`` below ``buckets_dir`` misclassifies a
    dedicated code volume as ephemeral, while treating every path in the container's
    overlay root as persistent would be equally misleading.
    """
    if not path:
        return False

    def unescape_mount(value: str) -> str:
        for escaped, plain in (
            ("\\040", " "),
            ("\\011", "\t"),
            ("\\012", "\n"),
            ("\\134", "\\"),
        ):
            value = value.replace(escaped, plain)
        return value

    target = os.path.normcase(os.path.abspath(path))
    try:
        with open(mountinfo_path, encoding="utf-8") as handle:
            for line in handle:
                fields = line.split(" - ", 1)[0].split()
                if len(fields) < 5:
                    continue
                mount_point = os.path.normcase(
                    os.path.abspath(unescape_mount(fields[4]))
                )
                if mount_point == os.path.abspath(os.sep):
                    continue
                if target == mount_point or target.startswith(mount_point + os.sep):
                    return True
    except OSError:
        return False
    return False


def _hot_update_persistence() -> dict:
    """判断本次热更新写盘后能不能扛过容器重建（用户反馈 #1）。

    - 裸机（非 Docker）：代码就跑在仓库目录，天然持久 → mode="bare"。
    - Docker：默认 CODE_DIR 位于数据卷，也支持独立 bind/named/anonymous code volume。
      前者通过 buckets_dir 边界识别，后者通过 /proc/self/mountinfo 识别。若播种失败
      回退到镜像内置 /app/src，则只位于容器 overlay root，mode="ephemeral"。

    返回 {persistent, mode, repo_root, note}。
    """
    repo_root = str(getattr(sh, "repo_root", "") or "")
    if os.environ.get("RENDER", "").strip().lower() == "true":
        return {
            "persistent": False,
            "mode": "render-ephemeral",
            "repo_root": repo_root,
            "note": (
                "Render 的源码目录属于临时文件系统；Dashboard 热更新只对当前实例有效，"
                "平台重启或重新部署后会回到 Git/Blueprint 部署的版本。请通过 Render 的"
                "正式部署流程升级版本。"
            ),
        }
    if not sh.in_docker():
        return {
            "persistent": True,
            "mode": "bare",
            "repo_root": repo_root,
            "note": "裸机部署：代码就在仓库目录，热更新直接持久。",
        }
    buckets_dir = str(sh.config.get("buckets_dir") or "")
    under_volume = False
    if repo_root and buckets_dir:
        try:
            a = os.path.normcase(os.path.abspath(repo_root))
            b = os.path.normcase(os.path.abspath(buckets_dir))
            under_volume = a == b or a.startswith(b + os.sep)
        except Exception:
            under_volume = False
    if under_volume:
        return {
            "persistent": True,
            "mode": "volume",
            "repo_root": repo_root,
            "note": "Docker：正从持久卷上的代码运行，热更新写在数据卷里，容器重建后仍然生效。",
        }
    if _path_is_mounted_volume(repo_root):
        return {
            "persistent": True,
            "mode": "code-volume",
            "repo_root": repo_root,
            "note": ("Docker：正从独立代码卷运行，热更新不会落在容器 overlay 临时层。"
                     "跨容器重建请优先使用命名卷或 bind mount，并避免 down -v。"),
        }
    return {
        "persistent": False,
        "mode": "ephemeral",
        "repo_root": repo_root,
        "note": ("Docker：当前从镜像内置代码运行（持久卷播种未生效），热更新只写在易失的镜像层——"
                 "容器一旦重建（compose up / Docker 重启）就会回退到镜像版本。请确认已把数据卷"
                 "挂到 /app/buckets，或改用重建镜像的方式升级。"),
    }


def register(mcp) -> None:

    @mcp.custom_route("/api/restart", methods=["POST"])
    async def api_restart(request: Request) -> Response:
        """Restart the current service after an authenticated confirmation."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if body.get("confirm") is not True:
            return JSONResponse(
                {"ok": False, "error": "confirm=true required"}, status_code=400
            )

        async def _delayed_restart() -> None:
            await _asyncio.sleep(0.8)
            _restart_self()

        _asyncio.create_task(_delayed_restart())
        return JSONResponse({"ok": True, "restarting": True})

    @mcp.custom_route("/api/version", methods=["GET"])
    async def api_version(request: Request) -> Response:
        """Public version endpoint. 返回 {"version": "x.y.z"}，公开访问。"""
        from starlette.responses import JSONResponse
        return JSONResponse({"version": sh.version})

    @mcp.custom_route("/api/update-info", methods=["GET"])
    async def api_update_info(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        is_docker = os.path.exists("/.dockerenv")
        is_render = os.environ.get("RENDER", "").strip().lower() == "true"
        container_name = os.environ.get("OMBRE_CONTAINER_NAME", "ombre-brain")
        persistence = _hot_update_persistence()
        return JSONResponse({
            "version": sh.version,
            "is_docker": is_docker,
            "is_render": is_render,
            "container_name": container_name,
            "port": int(sh.config.get("port") or 8000),
            "data_dir": str(sh.config.get("buckets_dir") or "（未知）"),
            # 热更新持久性（用户反馈 #1）：前端据此如实提示「已持久化 / 重建后会失效」，
            # 不再让 Docker 用户误以为点一下就完成了真正的版本升级。
            "hot_update_persistent": persistence["persistent"],
            "hot_update_mode": persistence["mode"],
            "hot_update_note": persistence["note"],
            "update_allow_pip_install": _pip_install_allowed(),
        })

    @mcp.custom_route("/api/update-settings", methods=["POST"])
    async def api_update_settings(request: Request) -> Response:
        """持久化「热更新遇到依赖变化时是否自动 pip install」这一个开关。

        这不是把默认值改掉——OMBRE_UPDATE_ALLOW_PIP 环境变量默认关闭的安全立场
        （安全加固 #2：自动 pip 把「谁能点热更新」放大成任意 PyPI 包的 RCE 面）
        没有变化，这里只是把已经存在、此前只能靠 SSH 改 env 才能碰到的
        config.update.allow_pip_install 开关，暴露成 Dashboard 上能点的一个选项，
        由部署者自己清醒地打开。写入 config.yaml 后立即在运行态生效，不需要重启
        （_pip_install_allowed() 每次 do-update 都会重新读取）。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)
        if "allow_pip_install" not in body:
            return JSONResponse(
                {"ok": False, "error": "缺少 allow_pip_install 字段"}, status_code=400
            )
        allow = bool(body.get("allow_pip_install"))

        def _mutate(saved: dict) -> None:
            update_cfg = saved.get("update")
            if not isinstance(update_cfg, dict):
                update_cfg = {}
                saved["update"] = update_cfg
            update_cfg["allow_pip_install"] = allow

        try:
            atomic_update_config_yaml(_mutate)
        except Exception as e:
            return JSONResponse(
                {"ok": False, "error": f"写 config.yaml 失败：{e}"}, status_code=500
            )

        if not isinstance(sh.config.get("update"), dict):
            sh.config["update"] = {}
        sh.config["update"]["allow_pip_install"] = allow
        return JSONResponse({"ok": True, "allow_pip_install": allow})

    @mcp.custom_route("/api/do-update", methods=["POST"])
    async def api_do_update(request: Request) -> Response:
        from starlette.responses import JSONResponse
        import tempfile as _tempfile

        err = sh._require_auth(request)
        if err:
            return err

        # Reserve synchronously before the first await or network request.  A
        # second updater must never share the live tree or the sole _prev slot.
        reservation = _UpdateJobReservation()
        if not reservation.acquire():
            return JSONResponse(
                {
                    "ok": False,
                    "busy": True,
                    "error": "已有热更新任务正在运行，请等待其完成后重试",
                },
                status_code=409,
            )

        async def _stream():
            temp_dir = ""
            repo_root = ""
            src_root = ""
            frontend_root = ""
            prev_dir = ""
            source_touched = False
            committed = False

            async def _rollback_if_needed() -> bool | None:
                nonlocal source_touched
                if not source_touched or not prev_dir:
                    return None
                restored = await _await_update_worker(
                    _restore_from_prev,
                    repo_root,
                    prev_dir,
                    src_root,
                    frontend_root,
                )
                if restored:
                    source_touched = False
                return bool(restored)

            try:
                yield "data: 正在连接 GitHub…\n\n"
                await _asyncio.sleep(0.1)

                # #4a ③：更新源可配（update.repo / update.ref），默认官方 main。
                _ucfg = getattr(sh, "config", {}) or {}
                _ucfg = _ucfg.get("update") or {}
                _repo = str(_ucfg.get("repo") or "P0luz/Ombre-Brain").strip().strip("/")
                _ref  = str(_ucfg.get("ref")  or "main").strip()
                # 安全闸门 #2：非官方更新源必须显式放行，否则拒绝——防止「改 config.update.repo
                # 指向恶意仓 → 覆盖 src → 重启执行」这条 RCE 链在默认配置下成立。
                if not _update_repo_allowed(_repo):
                    yield (f"data: ERROR:更新源 {_repo} 不在可信白名单（默认只允许官方 "
                           f"{_TRUSTED_UPDATE_REPOS[0]}）。如确需从 fork/自建源更新，请设置 "
                           f"OMBRE_ALLOW_CUSTOM_UPDATE_REPO=1 后重试。\n\n")
                    return
                # B3：默认从「最新 Release/Tag」拉包，而不是分支 HEAD——避免作者正推到
                # 一半时拉到半成品。channel="branch" 可切回分支模式；没有 Release 时自动回退分支。
                # Dashboard checks main/VERSION, so the default update payload
                # must come from that same branch.  A stale GitHub Latest Release
                # previously made the first click downgrade to v2.4.6; the old
                # updater then fetched main on the second click.
                _channel = str(_ucfg.get("channel") or "branch").strip().lower()
                _branch_url = f"https://github.com/{_repo}/archive/refs/heads/{_ref}.zip"
                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    _zip_url, _label = _branch_url, f"{_repo}@{_ref}（分支）"
                    if _channel != "branch":
                        try:
                            _rr = await client.get(
                                f"https://api.github.com/repos/{_repo}/releases/latest",
                                headers={"Accept": "application/vnd.github+json"},
                            )
                            if _rr.status_code == 200:
                                _tag = str(_rr.json().get("tag_name") or "").strip()
                                if _tag:
                                    _zip_url = f"https://github.com/{_repo}/archive/refs/tags/{_tag}.zip"
                                    _label = f"{_repo}@{_tag}（正式版）"
                                else:
                                    yield "data: 最新 Release 没有 tag，回退到分支下载…\n\n"
                            else:
                                yield f"data: 仓库暂无正式 Release，回退到分支 {_ref} 下载…\n\n"
                        except Exception as _rel_e:
                            yield f"data: 查询 Release 失败（{_rel_e}），回退到分支下载…\n\n"
                    yield f"data: 正在下载 {_label} …\n\n"
                    temp_dir = await _await_update_worker(
                        _tempfile.mkdtemp,
                        prefix="ombre-update-",
                        _cancel_result_cleanup=_cleanup_update_temp,
                    )
                    archive_path = os.path.join(temp_dir, "update.zip")
                    await _download_update_archive_to_file(
                        client, _zip_url, archive_path
                    )

                # ZIP traversal/size/ratio/manifest/hash validation and member
                # reads are CPU/disk work; keep the server loop responsive.
                inspected = await _await_update_worker(
                    _inspect_update_archive, archive_path
                )
                plan = inspected["plan"]
                target_version = inspected["target_version"]
                requirements_bytes = inspected.get("requirements_bytes")
                requirements_lock_bytes = inspected.get("requirements_lock_bytes")

                # Refuse a valid-but-older archive before creating _prev or
                # touching any runtime file.  Explicit release-channel users
                # are protected too when GitHub's Latest marker is stale.
                if target_version and _is_version_downgrade(sh.version, target_version):
                    yield (
                        f"data: ERROR:拒绝降级：当前版本 v{sh.version}，更新包版本 "
                        f"v{target_version}。未改动任何文件。\n\n"
                    )
                    return

                if plan["abort"]:
                    yield f"data: ERROR:{plan['abort']}（已中止，未改动任何文件）\n\n"
                    return

                # 目标根目录用注入的 sh.repo_root（Docker 下 = /app；裸机/VPS = 实际安装目录）。
                # 绝不能在这里用 __file__：本文件在 src/web/ 下，算出来会差一层。
                repo_root = sh.repo_root
                src_root = os.path.join(repo_root, "src")
                frontend_root = os.path.join(repo_root, "frontend")

                # 依赖是否变化只需要「刚下载的清单」+「磁盘上现有清单」，跟这次更新有没有
                # 真的写文件无关。提前到备份/写盘之前判断：pip 关闭又确实需要装时直接在这里
                # 拒绝，不用先建 _prev 备份、写完文件再回滚一遍——省一轮磁盘 I/O，报错也更准确
                # （这时候是真的「未改动任何文件」，不是「已回滚」）。
                requirements_changed = await _await_update_worker(
                    _requirements_changed,
                    repo_root,
                    requirements_bytes,
                    requirements_lock_bytes,
                )
                if requirements_changed and not _pip_install_allowed():
                    yield (
                        "data: ERROR:新版依赖清单有变化，自动 pip 安装处于关闭状态；"
                        "未改动任何文件。可在 Dashboard「热更新」里打开"
                        "「允许自动安装新依赖」开关后重试，或重建镜像，或设置 "
                        "OMBRE_UPDATE_ALLOW_PIP=1 后重试。\n\n"
                    )
                    return

                yield "data: 下载完成，正在解压文件…\n\n"
                await _asyncio.sleep(0.1)

                # #4a ②：覆盖前把当前 src/frontend 备份成回滚点 _prev，坏更新崩溃时 entrypoint 还原。
                prev_dir = os.path.join(repo_root, "_prev")
                try:
                    await _await_update_worker(
                        _backup_update_tree,
                        repo_root,
                        src_root,
                        frontend_root,
                        prev_dir,
                    )
                    yield "data: 已备份当前版本为回滚点…\n\n"
                except Exception as _bk:
                    yield f"data: ERROR:备份回滚点失败，已中止且未覆盖任何文件：{_bk}\n\n"
                    return

                if plan["verified"]:
                    yield "data: 已通过 sha256 完整性校验…\n\n"
                else:
                    yield "data: 未提供 update_manifest.json，已按路径保护过滤但跳过 sha256 校验…\n\n"

                # All atomic writes/fsyncs run as one reaped worker step.  Mark
                # the tree dirty before launching it so disconnects roll back
                # even if cancellation lands during the first write.
                source_touched = True
                try:
                    updated = await _await_update_worker(
                        _apply_update_files,
                        plan,
                        repo_root,
                        src_root,
                        frontend_root,
                        inspected["version_bytes"],
                    )
                except Exception as write_error:
                    restored = await _rollback_if_needed()
                    state = "已回滚" if restored else "回滚失败"
                    yield f"data: ERROR:写入更新失败（{write_error}），{state}。\n\n"
                    return

                if plan["skipped_unsafe"]:
                    yield f"data: 已跳过 {plan['skipped_unsafe']} 个路径异常/受保护的条目（安全防护）…\n\n"
                if plan["skipped_unlisted"]:
                    yield f"data: 已跳过 {plan['skipped_unlisted']} 个不在校验清单内的文件（未经校验不落盘）…\n\n"
                if inspected["version_bytes"] is not None:
                    yield f"data: 版本号已同步为 v{target_version}…\n\n"

                # #4a ③：先同步清单，再完成代码编译自检；只有这些可回滚步骤全部成功后
                # 才允许 pip 改动解释器环境，避免后续失败留下半更新依赖。requirements_changed
                # 与「pip 关闭时直接拒绝」已经在备份/写盘之前判断过了，这里只管安装。
                install_lock = False
                install_name = ""
                install_bytes = None
                try:
                    if requirements_changed:
                        install_lock = bool(
                            requirements_lock_bytes
                            and requirements_lock_bytes.strip()
                        )
                        install_name = (
                            "requirements.lock.txt"
                            if install_lock
                            else "requirements.txt"
                        )
                        install_bytes = (
                            requirements_lock_bytes
                            if install_lock
                            else requirements_bytes
                        )
                        if not install_bytes:
                            raise ValueError("更新包缺少可安装的依赖清单")

                    await _await_update_worker(
                        _sync_update_dependency_manifests,
                        repo_root,
                        requirements_bytes,
                        requirements_lock_bytes,
                    )
                except Exception as requirements_error:
                    restored = await _rollback_if_needed()
                    state = "已回滚" if restored else "回滚失败"
                    yield f"data: ERROR:依赖处理失败（{requirements_error}），{state}。\n\n"
                    return

                # B2：重启前先验证新代码能编译。不通过就从 _prev 自动还原、放弃重启，
                # 保住当前可用状态——尤其裸机没有别的守护会兜底。
                compile_error = await _await_update_worker(
                    _compile_check_dir, src_root
                )
                if compile_error:
                    yield f"data: 新代码自检未通过（{compile_error}）。正在还原到更新前的版本…\n\n"
                    if await _rollback_if_needed():
                        yield "data: 已还原上一版，服务保持当前运行、不重启。可稍后重试或联系维护者。\n\n"
                    else:
                        yield "data: ⚠️ 自动还原失败，请检查 _prev 备份目录并手动恢复。\n\n"
                    yield "data: ERROR:更新已中止（新代码自检失败，已回滚，未重启）\n\n"
                    return

                if requirements_changed:
                    yield "data: 依赖清单有变化，正在 pip install…\n\n"
                    try:
                        pip_result = await _await_update_worker(
                            _install_update_requirements,
                            os.path.join(repo_root, install_name),
                            install_bytes,
                            require_hashes=install_lock,
                        )
                    except Exception as requirements_error:
                        restored = await _rollback_if_needed()
                        state = "已回滚" if restored else "回滚失败"
                        yield f"data: ERROR:依赖处理失败（{requirements_error}），{state}。\n\n"
                        return
                    if pip_result.returncode != 0:
                        restored = await _rollback_if_needed()
                        state = "已回滚" if restored else "回滚失败"
                        yield f"data: ERROR:依赖安装失败，{state}；服务不会重启。\n\n"
                        return
                    yield "data: 依赖安装完成…\n\n"

                # Remove the downloaded ZIP before handing the reservation to
                # the restart task.  This also keeps failed/successful updates
                # from accumulating temp archives.
                await _await_update_worker(_cleanup_update_temp, temp_dir)
                temp_dir = ""

                yield f"data: 已更新 {updated} 个文件，即将重启服务…\n\n"
                await _asyncio.sleep(0.5)

                async def _restart():
                    # 先睡 0.8s 让上面的 SSE "RESTART" 行刷给前端，再原地自重启。
                    try:
                        await _asyncio.sleep(0.8)
                    finally:
                        # No second update may start during this hand-off.
                        reservation.release()
                    _restart_self()

                restart_task = _asyncio.create_task(_restart())
                _UPDATE_RESTART_TASKS.add(restart_task)
                restart_task.add_done_callback(_UPDATE_RESTART_TASKS.discard)
                reservation.defer_to_restart()
                committed = True
                yield "data: RESTART\n\n"

            except _asyncio.CancelledError:
                raise
            except Exception as e:
                restored = await _rollback_if_needed()
                suffix = "，已回滚" if restored else ""
                yield f"data: ERROR:{e}{suffix}\n\n"
            finally:
                # ``aclose`` injects GeneratorExit rather than CancelledError;
                # the central rollback therefore lives here as well.  Reaped
                # worker calls ensure the lock is never released while a
                # cancelled copy/write/rollback is still running.
                if source_touched and not committed:
                    try:
                        await _rollback_if_needed()
                    except BaseException:
                        pass
                if temp_dir:
                    try:
                        await _await_update_worker(
                            _cleanup_update_temp, temp_dir
                        )
                    except BaseException:
                        pass
                reservation.release_unless_deferred()

        try:
            return _UpdateStreamingResponse(
                _stream(),
                reservation,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        except BaseException:
            reservation.release()
            raise

    @mcp.custom_route("/api/maintenance/fix-pinned-desync", methods=["GET", "POST"])
    async def api_fix_pinned_desync(request: Request) -> Response:
        """扫描 pinned/type 脱钩项。

        type=permanent 是正式固化类型；当前不会自动降级未 pinned 的 permanent 桶。
        两者都需登录。逻辑复用 tools._common.repair_pinned_desync。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        from tools._common import repair_pinned_desync
        try:
            apply = request.method == "POST"
            result = await repair_pinned_desync(sh.bucket_mgr, apply=apply)
            return JSONResponse({"ok": True, **result})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @mcp.custom_route(
        "/api/maintenance/restore-archived-letters",
        methods=["GET", "POST"],
    )
    async def api_restore_archived_letters(request: Request) -> Response:
        """审计并显式恢复带强来源标记的历史误归档 Letter。

        GET 永远只做 dry-run。POST 必须给出非空、受限的 ``ids`` 字符串列表；
        维护 helper 的扫描结果不构成写授权，存储层会在桶租约内重新校验。
        """
        from starlette.responses import JSONResponse

        def no_store_json(payload: dict, *, status_code: int = 200) -> Response:
            return JSONResponse(
                payload,
                status_code=status_code,
                headers={"Cache-Control": "no-store"},
            )

        err = sh._require_auth(request)
        if err:
            err.headers["Cache-Control"] = "no-store"
            return err
        from tools._common import restore_archived_letters

        ids: list[str] | None = None
        apply = request.method == "POST"
        if apply:
            try:
                body = await sh._read_json_object(request)
            except Exception:
                return no_store_json(
                    {"ok": False, "reason": "invalid_json"},
                    status_code=400,
                )
            raw_ids = body.get("ids")
            if (
                not isinstance(raw_ids, list)
                or not raw_ids
                or len(raw_ids) > _ARCHIVED_LETTER_RESTORE_MAX_IDS
            ):
                return no_store_json(
                    {"ok": False, "reason": "invalid_ids"},
                    status_code=400,
                )
            normalized: list[str] = []
            seen: set[str] = set()
            for value in raw_ids:
                if not isinstance(value, str):
                    return no_store_json(
                        {"ok": False, "reason": "invalid_ids"},
                        status_code=400,
                    )
                bucket_id = value.strip()
                if (
                    not bucket_id
                    or len(bucket_id) > _ARCHIVED_LETTER_RESTORE_ID_MAX_CHARS
                ):
                    return no_store_json(
                        {"ok": False, "reason": "invalid_ids"},
                        status_code=400,
                    )
                if bucket_id not in seen:
                    seen.add(bucket_id)
                    normalized.append(bucket_id)
            if not normalized:
                return no_store_json(
                    {"ok": False, "reason": "invalid_ids"},
                    status_code=400,
                )
            ids = normalized

        try:
            result = await restore_archived_letters(
                sh.bucket_mgr,
                ids=ids,
                apply=apply,
            )
            return no_store_json({"ok": True, **result})
        except Exception:
            sh.logger.exception("历史归档 Letter 维护失败")
            return no_store_json(
                {"ok": False, "reason": "internal_error"},
                status_code=500,
            )

    @mcp.custom_route("/api/author", methods=["GET"])
    async def api_author(request: Request) -> Response:
        """Static author note (read-only, public)."""
        from starlette.responses import JSONResponse
        return JSONResponse(_AUTHOR_NOTE)

    @mcp.custom_route("/api/onboarding/status", methods=["GET"])
    async def api_onboarding_status(request: Request) -> Response:
        """前端调用：判断是否需要引导（env 与 config 同时缺密钥才算"全新"）。

        本接口刻意不要求登录——dashboard 首次打开时连密码都还没设。
        """
        from starlette.responses import JSONResponse
        dash_env = bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "").strip())
        dash_file = False
        try:
            dash_file = bool(sh._load_password_hash())
        except Exception:
            dash_file = False

        gem_env = bool(os.environ.get("GEMINI_API_KEY", "").strip())
        gem_cfg = bool((sh.config.get("dehydration", {}) or {}).get("api_key", "")) or \
            bool((sh.config.get("embedding", {}) or {}).get("api_key", ""))

        first_run = (not dash_env and not dash_file) and (not gem_env and not gem_cfg)

        # Public first-run UI needs only these display booleans.  Credential
        # source, deployment profile and detailed capability state belong to
        # authenticated /api/status rather than a remotely enumerable route.
        return JSONResponse(
            {
                "first_run": first_run,
                "embedding_enabled": bool(sh.embedding_engine.enabled),
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_system_status(request: Request) -> Response:
        """Return detailed system status for the settings panel."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            stats = await sh.bucket_mgr.get_stats()
            return JSONResponse({
                "decay_engine": "running" if sh.decay_engine.is_running else "stopped",
                "embedding_enabled": sh.embedding_engine.enabled,
                "buckets": {
                    "permanent": stats.get("permanent_count", 0),
                    "dynamic": stats.get("dynamic_count", 0),
                    "archive": stats.get("archive_count", 0),
                    "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
                },
                "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
                "version": sh.version,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
