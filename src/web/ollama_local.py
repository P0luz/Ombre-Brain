"""
========================================
web/ollama_local.py — 本地向量化「一键搭建」(非 Docker 用户)
========================================

背景：旧的「一键搭建本地向量化」只对 Docker 用户有效（ollama 跑在 ombre-ollama
容器里）。裸机/原生用户（pip 装、python src/server.py 跑在 Win/Linux/mac）机器上
根本没有 ollama，按钮只会提示去起容器。

本模块补上这块：检测宿主 → 免提权自动装 ollama 运行时 → 作为 OB 子进程常驻 →
（模型下载仍复用 embedding.py 的 /api/embedding/local/pull，已支持镜像）。

设计取舍（已与使用者确认）：
- 安装力度：免提权自动装 + 失败回退。Win 走 per-user 静默安装器（不需管理员）；
  Linux/mac 下静态二进制/包到用户目录（不需 sudo）。任何一步需要提权 / 被杀软拦 /
  下载失败 → 回退成「一键复制官方命令 + 指引」，绝不静默失败。
- 常驻方式：ollama serve 作为 OB 管理的子进程。OB 起它就起，OB 关它就关，
  挂了自动拉起。不写 systemd/launchd/注册表（跨平台最省、最可控）。

对外暴露：
- register(mcp)：注册 /api/embedding/local/env、/install(+status)、/start
- ensure_child_on_boot() / stop_child()：给 server.py lifespan 调
========================================
"""

import os
import sys
import hashlib
import hmac
import shutil
import platform
import asyncio
import subprocess
import zipfile
import stat
import threading
import urllib.parse
import urllib.request

import httpx
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from . import _shared as sh

logger = sh.logger

_OLLAMA_PORT = 11434
_LOCAL_BASE = f"http://127.0.0.1:{_OLLAMA_PORT}"

# ollama 运行时（二进制/安装器）下载镜像。模型 registry 的镜像在 embedding.py 里另算。
_BIN_MIRRORS = {
    "official": "https://github.com/ollama/ollama/releases/download/v0.32.0",
    "github": "https://github.com/ollama/ollama/releases/download/v0.32.0",
}

# 各系统官方安装命令（自动装失败时回退给用户手动跑）
_MANUAL_HINT = {
    "windows": "去 https://ollama.com/download/windows 下载 OllamaSetup.exe 双击安装；装完回来再点一次。",
    "linux": "在终端运行：curl -fsSL https://ollama.com/install.sh | sh （需要 sudo）；装完回来再点一次。",
    "macos": "用 Homebrew：brew install ollama；或去 https://ollama.com/download/mac 下载 App。装完回来再点一次。",
}

# 安装进度（线程里跑阻塞下载，主循环只读这个 dict）
_install_state: dict = {
    "running": False, "phase": "idle", "percent": 0.0,
    "msg": "", "error": "", "hint": "",
}
_install_thread: "threading.Thread | None" = None

# 子进程管理
_child_proc: "subprocess.Popen | None" = None
_child_managed = False
_child_monitor_task: "asyncio.Task | None" = None


# ============================================================
# 环境探测
# ============================================================

def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m or "amd64"


def _user_install_root() -> str:
    """免提权安装目标根目录（用户家目录下，不需 sudo/管理员）。"""
    return os.path.join(os.path.expanduser("~"), ".ollama", "local")


def find_ollama_bin() -> "str | None":
    """找 ollama 可执行文件：PATH 优先，再查各系统免提权安装位置。"""
    p = shutil.which("ollama")
    if p:
        return p
    osk = _os_key()
    home = os.path.expanduser("~")
    cands = []
    if osk == "windows":
        cands += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
            os.path.join(_user_install_root(), "ollama.exe"),
        ]
    elif osk == "macos":
        cands += [
            os.path.join(_user_install_root(), "Ollama.app", "Contents", "Resources", "ollama"),
            "/Applications/Ollama.app/Contents/Resources/ollama",
        ]
    cands += [
        os.path.join(_user_install_root(), "bin", "ollama"),
        os.path.join(home, ".ollama", "bin", "ollama"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


async def _is_running(base: str = _LOCAL_BASE) -> bool:
    # trust_env=False：本地 ollama 必须绕过系统代理（Clash/V2Ray 等会把 127.0.0.1
    # 也丢给代理 → 502，明明 serve 在跑却判定挂了）。
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as c:
            r = await c.get(f"{base}/api/version")
            return r.status_code == 200
    except Exception:
        return False


async def _detect() -> dict:
    osk = _os_key()
    bin_path = find_ollama_bin()
    running = await _is_running()
    in_docker = sh.in_docker()
    return {
        "ok": True,
        "os": osk,
        "arch": _arch(),
        "in_docker": in_docker,
        "installed": bool(bin_path),
        "bin_path": bin_path or "",
        "running": running,
        "managed": _child_managed,
        "mirrors": list(_BIN_MIRRORS.keys()),
        # 给前端决定下一步：docker → 走容器；裸机 → 装/起/拉
        "recommended": _recommend(in_docker, bool(bin_path), running),
        "manual_hint": _MANUAL_HINT.get(osk, ""),
    }


def _recommend(in_docker: bool, installed: bool, running: bool) -> str:
    if in_docker:
        return "docker"          # 容器部署：用 compose 的 ollama 服务，前端给容器指引
    if running:
        return "pull"            # 已在跑：直接拉模型
    if installed:
        return "start"           # 装了没跑：起子进程
    return "install"             # 啥都没有：先装


# ============================================================
# 安装（线程里跑，免提权）
# ============================================================

_OLLAMA_VERSION = "v0.32.0"
_GH_RELEASES = (
    f"https://github.com/ollama/ollama/releases/download/{_OLLAMA_VERSION}"
)
_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 2_000.0
_ARTIFACT_SHA256 = {
    ("windows", "amd64"): "07846c9074875e4d47518d41636880a9d9a40a7e1483659ac00be7aec082de06",
    ("macos", "amd64"): "0762f0a5c086f77a5fceda3ec1e6a0d96500a114a8adc82856224bbc8f3a9d7f",
    ("macos", "arm64"): "0762f0a5c086f77a5fceda3ec1e6a0d96500a114a8adc82856224bbc8f3a9d7f",
    ("linux", "amd64"): "56362d7609dfa9e35aaebb7c9cab25605d8f0528ec3d5d585dc83d6642002bab",
    ("linux", "arm64"): "2c1fbc47a5351c74f5400d7e4b1104bb470291af5d2f425c37c151487a477ad6",
}


def _bin_url(mirror: str, osk: str, arch: str) -> str:
    """按系统给出运行时下载地址。

    实测（v0.30.x）：
    - Win/mac 的安装包 OllamaSetup.exe / Ollama-darwin.zip 在 ollama.com/download
      与 GitHub Releases 都有。
    - Linux 只有 GitHub Releases 提供 `ollama-linux-{arch}.tar.zst`（zstd 压缩，
      不是 tgz；ollama.com/download 不托管它，会 404）。
    custom 镜像（既不是 official 也不是 github）直接拿来当前缀拼资源名。
    """
    if osk == "windows":
        name = "OllamaSetup.exe"
    elif osk == "macos":
        name = "Ollama-darwin.zip"
    else:
        name = f"ollama-linux-{arch}.tar.zst"
    if mirror in ("official", "github"):
        base = _GH_RELEASES
    else:
        base = mirror.rstrip("/")     # 自定义镜像前缀（如 github 代理）
    return f"{base}/{name}"


def _safe_archive_target(dest: str, member_name: str) -> str:
    if not member_name or os.path.isabs(member_name):
        raise ValueError(f"unsafe archive member path: {member_name!r}")
    base = os.path.realpath(dest)
    target = os.path.realpath(os.path.join(base, member_name))
    if os.path.commonpath([base, target]) != base:
        raise ValueError(f"archive member escapes target dir: {member_name!r}")
    return target


def _safe_extract_zip(zf: zipfile.ZipFile, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    infos = zf.infolist()
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise ValueError("archive contains too many members")
    total = 0
    for info in infos:
        if info.file_size < 0 or info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"archive member is too large: {info.filename!r}")
        total += info.file_size
        if total > _MAX_EXTRACTED_BYTES:
            raise ValueError("archive expands beyond the byte limit")
        if (
            info.file_size > 0
            and info.compress_size > 0
            and info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO
        ):
            raise ValueError(f"suspicious compression ratio: {info.filename!r}")
        target = _safe_archive_target(dest, info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode in (stat.S_IFLNK, stat.S_IFSOCK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK):
            raise ValueError(f"unsafe archive member type: {info.filename!r}")
        if info.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _safe_extract_tar(tf, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    members = 0
    total = 0
    for member in tf:
        members += 1
        if members > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("archive contains too many members")
        if member.size < 0 or member.size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"archive member is too large: {member.name!r}")
        total += member.size
        if total > _MAX_EXTRACTED_BYTES:
            raise ValueError("archive expands beyond the byte limit")
        target = _safe_archive_target(dest, member.name)
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"unsafe archive member type: {member.name!r}")
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        if not member.isfile():
            raise ValueError(f"unsupported archive member type: {member.name!r}")
        src = tf.extractfile(member)
        if src is None:
            raise ValueError(f"could not read archive member: {member.name!r}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _extract_zst(path: str, dest: str) -> bool:
    """解 .tar.zst 到 dest。逐成员校验路径和类型后再写入。成功 True。"""
    try:
        import io as _io
        import tarfile as _tf
        import zstandard  # type: ignore
        with open(path, "rb") as f:
            with zstandard.ZstdDecompressor().stream_reader(f) as reader:
                with _tf.open(fileobj=_io.BufferedReader(reader), mode="r|") as t:
                    _safe_extract_tar(t, dest)
        return True
    except Exception:
        return False


# 运行时下载的是**可执行安装器**（Win 直接静默运行、Linux/mac 解出二进制），
# 所以下载源必须严格约束。默认只信官方 ollama 与 GitHub Releases 的分发域。
# 自定义 mirror（如 GFW 下的 GitHub 代理）确有正当需求 —— 但必须由部署者显式
# 开 OMBRE_ALLOW_UNTRUSTED_MIRROR=1 主动承担风险，绝不默认放行任意主机。
_TRUSTED_DOWNLOAD_HOSTS = (
    "ollama.com",
    "github.com",
    "githubusercontent.com",   # objects.githubusercontent.com：GitHub Release 资产实际落点
)


def _host_is_trusted(host: str) -> bool:
    host = (host or "").lower().split(":", 1)[0].strip(".")
    return any(host == h or host.endswith("." + h) for h in _TRUSTED_DOWNLOAD_HOSTS)


def _allow_untrusted_mirror() -> bool:
    return os.environ.get("OMBRE_ALLOW_UNTRUSTED_MIRROR", "").strip().lower() in ("1", "true", "yes", "on")


def _validate_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("download URL must be HTTPS without credentials")
    if not _host_is_trusted(parsed.hostname or ""):
        if not _allow_untrusted_mirror():
            raise ValueError(
                f"下载主机不在可信白名单：{parsed.hostname!r}。"
                f"只信任 {', '.join(_TRUSTED_DOWNLOAD_HOSTS)}；"
                f"如确需自定义镜像，请在部署环境设置 OMBRE_ALLOW_UNTRUSTED_MIRROR=1 后重试。"
            )
        logger.warning(f"[ollama-install] 使用非白名单下载主机（已由 OMBRE_ALLOW_UNTRUSTED_MIRROR 放行）：{parsed.hostname}")
    return url


class _ValidatedDownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 必须先校验再交给 urllib，不能等目标收到请求后才检查响应 URL。
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, dest: str) -> None:
    """流式下载，进度写 _install_state['percent']。"""
    safe_url = _validate_download_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": "OmbreBrain-Setup"})
    opener = urllib.request.build_opener(_ValidatedDownloadRedirect())
    with opener.open(req, timeout=60) as resp:
        _validate_download_url(resp.geturl())
        total = int(resp.headers.get("Content-Length") or 0)
        if total < 0 or total > _MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"download exceeds {_MAX_DOWNLOAD_BYTES} byte limit"
            )
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"download exceeds {_MAX_DOWNLOAD_BYTES} byte limit"
                    )
                if total > 0:
                    _install_state["percent"] = round(done / total * 100, 1)
    _install_state["percent"] = 100.0


# 各系统安装产物的「魔数」（文件头），执行/解压前用来确认下到的是真东西，
# 而不是镜像返回的 200 HTML 错误页或半截文件（那些被直接运行/解压很危险）。
_ARTIFACT_MAGICS = {
    "windows": (b"MZ",),                    # PE 可执行（安装器）
    "macos": (b"PK\x03\x04", b"PK\x05\x06"),  # zip
    "linux": (b"\x28\xB5\x2F\xFD",),         # zstd（.tar.zst）
}
_ARTIFACT_MIN_BYTES = 100 * 1024            # <100KB 基本可判定是错误页/截断


def _verify_downloaded_artifact(path: str, osk: str, arch: str) -> None:
    """执行/解压前校验固定发行版的大小、文件头和 SHA-256。"""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise RuntimeError(f"下载文件不可读：{e}")
    if size < _ARTIFACT_MIN_BYTES:
        raise RuntimeError(f"下载文件过小（{size} 字节），疑似错误页或未下全，已中止")
    magics = _ARTIFACT_MAGICS.get(osk, ())
    if magics:
        with open(path, "rb") as f:
            head = f.read(8)
        if not any(head.startswith(m) for m in magics):
            raise RuntimeError("下载文件的格式不对（文件头不匹配预期），疑似被劫持/损坏，已中止")
    digest_key = (osk, arch)
    if osk == "windows" and digest_key not in _ARTIFACT_SHA256:
        # The published setup executable is currently x64 and runs under
        # Windows-on-ARM emulation; it is still the exact same signed artifact.
        digest_key = ("windows", "amd64")
    expected = _ARTIFACT_SHA256.get(digest_key)
    if not expected:
        raise RuntimeError(
            f"版本 {_OLLAMA_VERSION} 没有 {osk}/{arch} 的已信任摘要"
        )
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), expected):
        raise RuntimeError("Ollama 发行产物 SHA-256 校验失败，已中止")


def _install_run(osk: str, arch: str, mirror: str) -> None:
    """阻塞安装流程（在线程里）。任何失败都落到 _install_state.error + hint，不抛。"""
    global _install_state
    tmpdir = os.path.join(_user_install_root(), "_dl")
    try:
        os.makedirs(tmpdir, exist_ok=True)
        url = _bin_url(mirror, osk, arch)
        _install_state.update(phase="downloading", percent=0.0, msg=f"下载 ollama 运行时…（{url}）")
        logger.info(f"[ollama-install] downloading {url}")

        if osk == "windows":
            exe = os.path.join(tmpdir, "OllamaSetup.exe")
            _download(url, exe)
            _verify_downloaded_artifact(exe, osk, arch)
            _install_state.update(phase="installing", msg="静默安装中（per-user，无需管理员）…")
            # Ollama 的 Windows 安装器（Inno Setup）默认就装到 %LOCALAPPDATA%\Programs\Ollama，
            # per-user、不需管理员。静默装即可；不传 /CURRENTUSER 以免个别版本不允许覆盖而报错。
            r = subprocess.run(
                [exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                capture_output=True, timeout=600,
            )
            if r.returncode not in (0, 3010):  # 3010 = 需重启，但二进制已就位
                raise RuntimeError(f"安装器返回 {r.returncode}: {(r.stderr or b'')[:200].decode('utf-8','replace')}")

        elif osk == "linux":
            tzst = os.path.join(tmpdir, "ollama.tar.zst")
            _download(url, tzst)
            _verify_downloaded_artifact(tzst, osk, arch)
            _install_state.update(phase="extracting", msg="解压到用户目录（无需 sudo）…")
            root = _user_install_root()
            os.makedirs(root, exist_ok=True)
            if not _extract_zst(tzst, root):  # 解出 bin/ollama + lib/
                raise RuntimeError(
                    "解压 .tar.zst 失败（缺少 Python zstandard 支持或安装包异常）。"
                    "可改用官方脚本：curl -fsSL https://ollama.com/install.sh | sh"
                )
            binp = os.path.join(root, "bin", "ollama")
            if os.path.isfile(binp):
                os.chmod(binp, 0o755)  # nosec B103

        elif osk == "macos":
            zp = os.path.join(tmpdir, "Ollama-darwin.zip")
            _download(url, zp)
            _verify_downloaded_artifact(zp, osk, arch)
            _install_state.update(phase="extracting", msg="解压 App 到用户目录…")
            root = _user_install_root()
            os.makedirs(root, exist_ok=True)
            with zipfile.ZipFile(zp) as z:
                _safe_extract_zip(z, root)
            binp = os.path.join(root, "Ollama.app", "Contents", "Resources", "ollama")
            if os.path.isfile(binp):
                os.chmod(binp, 0o755)  # nosec B103
        else:
            raise RuntimeError(f"不支持的系统：{osk}")

        # 校验真的装上了
        if not find_ollama_bin():
            raise RuntimeError("安装流程跑完但没找到 ollama 可执行文件")
        _install_state.update(phase="done", percent=100.0, msg="安装完成 ✓", error="", hint="")
        logger.info("[ollama-install] done")
    except Exception as e:
        _install_state.update(
            phase="error",
            error=f"{type(e).__name__}: {e}"[:300],
            hint="可换下载镜像重试（official ↔ github），或按提示手动安装：" + _MANUAL_HINT.get(osk, ""),
        )
        logger.warning(f"[ollama-install] failed: {e}")
    finally:
        _install_state["running"] = False
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ============================================================
# 子进程常驻
# ============================================================

def _spawn() -> "subprocess.Popen | None":
    binp = find_ollama_bin()
    if not binp:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _os_key() == "windows" else 0
    env = os.environ.copy()
    env.setdefault("OLLAMA_HOST", f"127.0.0.1:{_OLLAMA_PORT}")
    return subprocess.Popen(
        [binp, "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, creationflags=flags,
    )


async def ensure_child() -> dict:
    """确保 ollama 在跑：已可达→直接用；装了没跑→拉起子进程并等就绪；没装→报缺失。"""
    global _child_proc, _child_managed
    if await _is_running():
        return {"running": True, "managed": _child_managed, "reason": "already_running"}
    if sh.in_docker():
        return {"running": False, "managed": False, "reason": "in_docker"}
    if not find_ollama_bin():
        return {"running": False, "managed": False, "reason": "not_installed"}
    try:
        _child_proc = _spawn()
    except Exception as e:
        return {"running": False, "managed": False, "reason": f"spawn_failed: {e}"}
    if _child_proc is None:
        return {"running": False, "managed": False, "reason": "not_installed"}
    # 等就绪：首次冷启动很慢——实测 Windows 全新安装后第一次 `ollama serve`
    # 要做运行时/GPU 探测，可能 >150s 才开始监听 11434。给到 ~180s，
    # 每秒探一次（_is_running 自带 3s 超时，端口已开但慢响应时不会误判失败）。
    for _ in range(180):
        if await _is_running():
            _child_managed = True
            _start_monitor()
            logger.info("[ollama] child serve started & ready")
            return {"running": True, "managed": True, "reason": "spawned"}
        await asyncio.sleep(1)
    return {"running": False, "managed": False, "reason": "spawn_timeout"}


def _start_monitor() -> None:
    global _child_monitor_task
    if _child_monitor_task is None or _child_monitor_task.done():
        _child_monitor_task = asyncio.create_task(_monitor())


async def _monitor() -> None:
    """子进程挂了自动拉起（仅限我们托管的那只）。"""
    global _child_proc
    while _child_managed:
        await asyncio.sleep(5)
        try:
            if _child_proc is not None and _child_proc.poll() is not None:
                logger.warning("[ollama] managed child exited, respawning")
                _child_proc = _spawn()
        except Exception as e:
            logger.warning(f"[ollama] monitor respawn failed: {e}")


async def stop_child() -> None:
    """OB 关停时一并停掉我们托管的 ollama 子进程。"""
    global _child_proc, _child_managed
    _child_managed = False
    if _child_monitor_task:
        _child_monitor_task.cancel()
    if _child_proc is not None and _child_proc.poll() is None:
        try:
            _child_proc.terminate()
            try:
                _child_proc.wait(timeout=5)
            except Exception:
                _child_proc.kill()
        except Exception:
            pass
    _child_proc = None


async def ensure_child_on_boot() -> None:
    """server.py lifespan 调用：仅当裸机 + 配置成本地向量化时，开机就把子进程拉起来。
    Docker / 云端向量化 → 不动（裸机才有「OB 托管 ollama」一说）。"""
    try:
        if sh.in_docker():
            return
        emb = (sh.config.get("embedding") or {})
        fmt = (emb.get("api_format") or "").strip().lower()
        if not emb.get("enabled", True) or fmt not in ("ollama", "local"):
            return
        if not find_ollama_bin():
            return
        res = await ensure_child()
        logger.info(f"[ollama] boot ensure_child: {res}")
    except Exception as e:
        logger.warning(f"[ollama] ensure_child_on_boot failed: {e}")


# ============================================================
# 路由
# ============================================================

def register(mcp) -> None:

    @mcp.custom_route("/api/embedding/local/env", methods=["GET"])
    async def api_local_env(request: Request) -> Response:
        """检测宿主：os/arch/docker/已装/在跑 + 推荐下一步。前端据此渲染面板。"""
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(await _detect())

    @mcp.custom_route("/api/embedding/local/install", methods=["POST"])
    async def api_local_install(request: Request) -> Response:
        """免提权自动安装 ollama 运行时（后台线程）。body: {mirror?: official|github|<自定义URL前缀>}。"""
        err = sh._require_auth(request)
        if err:
            return err
        global _install_thread, _install_state
        if sh.in_docker():
            return JSONResponse({
                "ok": False,
                "error": "检测到运行在 Docker 容器里：容器内不能给宿主装 ollama。请用 deploy/docker-compose.yml 的 ollama 服务（docker compose up -d ollama）。",
            }, status_code=400)
        if _install_state.get("running"):
            return JSONResponse({"ok": False, "error": "已有安装任务在进行中。"}, status_code=409)
        if find_ollama_bin():
            return JSONResponse({"ok": True, "already": True, "msg": "ollama 已安装，可直接启动。"})
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "JSON body must be an object"}, status_code=400)
        if "mirror" in body and not isinstance(body["mirror"], str):
            return JSONResponse({"ok": False, "error": "mirror must be a string"}, status_code=400)
        if len(str(body.get("mirror") or "")) > 2048:
            return JSONResponse({"ok": False, "error": "mirror is too large"}, status_code=400)
        mirror = (str(body.get("mirror") or "official")).strip()
        osk, arch = _os_key(), _arch()
        _install_state = {"running": True, "phase": "starting", "percent": 0.0,
                          "msg": "准备安装…", "error": "", "hint": ""}
        _install_thread = threading.Thread(target=_install_run, args=(osk, arch, mirror), daemon=True)
        _install_thread.start()
        return JSONResponse({"ok": True, "status_path": "/api/embedding/local/install/status"}, status_code=202)

    @mcp.custom_route("/api/embedding/local/install/status", methods=["GET"])
    async def api_local_install_status(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({"ok": True, "install": _install_state})

    @mcp.custom_route("/api/embedding/local/start", methods=["POST"])
    async def api_local_start(request: Request) -> Response:
        """把 ollama 作为 OB 子进程拉起来并等就绪。返回是否在跑。"""
        err = sh._require_auth(request)
        if err:
            return err
        res = await ensure_child()
        ok = bool(res.get("running"))
        out = {"ok": ok, **res}
        if not ok and res.get("reason") == "not_installed":
            out["error"] = "ollama 尚未安装，请先点「安装」。"
        elif not ok and res.get("reason") == "in_docker":
            out["error"] = "Docker 环境请用 compose 的 ollama 服务。"
        elif not ok:
            out["error"] = f"启动失败：{res.get('reason')}"
        return JSONResponse(out, status_code=200 if ok else 400)
