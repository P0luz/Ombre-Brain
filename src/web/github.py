"""
========================================
web/github.py — GitHub 同步配置与触发
========================================

把所有 bucket .md 备份到 GitHub 仓库。状态/保存配置/验证/立即同步四个路由。

状态共享：github 实例存在 sh.github_sync_instance（server.py 的后台定时同步循环
_github_sync_loop / _restart_github_auto_task 也读 sh.github_sync_instance，
保证这里改了实例后台循环立刻看到）。后台任务起停走 sh.restart_github_auto_task。

对外暴露：register(mcp)。
========================================
"""

import asyncio
import os
import time
import uuid
import zipfile

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

logger = sh.logger
_import_lock = asyncio.Lock()

try:
    from github_sync import GitHubSync  # type: ignore
    from utils import parse_bool, atomic_update_config_yaml  # type: ignore
except ImportError:  # pragma: no cover
    from ..github_sync import GitHubSync  # type: ignore
    from ..utils import parse_bool, atomic_update_config_yaml  # type: ignore


def _save_github_config_to_disk(gh_cfg: dict) -> None:
    """把 github_sync 这一个 key 原子写回 config.yaml，失败即抛异常。

    走 utils.atomic_update_config_yaml 共用锁 + 原子写 + 读回校验，
    不再是「open(w) 直接整份覆盖、失败只记 warning」——那样调用方会误以为保存成功，
    内存里的新配置在下次重启（崩溃/热更新/手动重启按钮）读盘时被这份没写成功的旧文件覆盖，
    表现为「填好过一两个小时自动清空」。"""
    atomic_update_config_yaml(lambda save_config: save_config.__setitem__("github_sync", gh_cfg))


def _should_back_up_before_import(relative_path: str) -> bool:
    """这个文件会被 GitHub 导入覆盖吗？会，就必须先备份。

    判据只有一条：**导入会写它。** 导入写四类东西——
    Markdown、`_sources/` 下的原文证据、以及 `.you` / `.them` 两个模块库
    （见 `github_sync` 的安装循环与 `_MODULE_SNAPSHOT_PATHS`）。

    原先这里只打包 `*.md`。于是导入中途失败时，调用方拿着一个自称
    「导入前备份」的 zip，里面却没有原文证据、也没有两个模块的库——
    而那三样已经被覆盖了。上层还会因为备份失败就中止导入、理由写着
    「为避免覆盖后无法找回记忆」，也就是说它**把这个 zip 当成了完整回滚点**。
    一个不完整的回滚点比没有回滚点更危险：人会依着它去做不可逆的操作。
    """
    normalized = relative_path.replace(os.sep, "/")
    if normalized.endswith(".md"):
        return True
    if normalized.startswith("_sources/"):
        return True
    # SQLite 的 -wal / -shm 必须跟主库一起备份：只还原主库而丢掉 WAL，
    # 恢复出来的是一个状态不自洽的库。
    for module_path in (".you/you.sqlite3", ".them/them.sqlite3"):
        if normalized == module_path or normalized.startswith(module_path + "-"):
            return True
    return False


def _rollback_from_backup(buckets_dir: str, backup_zip: str) -> dict:
    """导入失败后，把备份里的文件写回原位。

    ## 为什么需要它

    导入是一个文件一个文件装的：装到第 50 个失败，前 49 个已经落盘，
    原先只是把失败计进 `errors` 就返回 `ok: false`。于是本地变成
    「一半是远端的、一半是旧的」——而调用方看到的只是一句失败，
    很容易以为什么都没发生。

    ## 它不做什么

    **不删除本次导入新增的文件。** 那些文件导入前不存在，按理也该清掉才算
    回到原状，但「删掉本地某个文件」这个动作一旦判断错就不可逆，
    而判断依据（它到底是这次导入带来的，还是导入期间模型自己写下的）
    并不可靠。所以这里只还原被覆盖的，新增的原样留着并如实报告数量，
    由人来决定——**宁可留下多余的，不可删错一条记忆。**
    """
    还原 = 0
    失败: list[str] = []
    try:
        with zipfile.ZipFile(backup_zip) as z:
            成员 = [n for n in z.namelist() if not n.endswith("/")]
            for name in 成员:
                目标 = os.path.abspath(os.path.join(buckets_dir, name))
                # 防目录穿越：备份是本地生成的，但它也可能被人动过手脚
                if not 目标.startswith(os.path.abspath(buckets_dir) + os.sep):
                    失败.append(f"{name}: 路径越界")
                    continue
                try:
                    os.makedirs(os.path.dirname(目标), exist_ok=True)
                    with z.open(name) as src, open(目标, "wb") as dst:
                        dst.write(src.read())
                    还原 += 1
                except Exception as exc:
                    失败.append(f"{name}: {type(exc).__name__}")
    except Exception as exc:
        logger.error(f"[github] rollback failed to open backup: {exc}")
        return {"ok": False, "restored": 还原, "error": f"备份读不开：{type(exc).__name__}"}

    if 失败:
        logger.error(f"[github] rollback incomplete: {len(失败)} file(s) failed")
    return {
        "ok": not 失败,
        "restored": 还原,
        "failed": 失败[:10],
        "failed_count": len(失败),
    }


def _pre_import_backup(buckets_dir: str) -> str:
    """导入前把**所有会被导入覆盖的文件**打成 zip 存到 <buckets_dir>/.import_backups/。

    返回 zip 路径（失败返回 "" —— 备份失败不应阻断恢复，但会在结果里如实标注）。
    """
    try:
        bdir = os.path.join(buckets_dir, ".import_backups")
        os.makedirs(bdir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        unique = f"{time.time_ns()}_{uuid.uuid4().hex[:8]}"
        zpath = os.path.join(bdir, f"pre_import_{ts}_{unique}.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(buckets_dir):
                if os.path.basename(root) == ".import_backups":
                    continue
                for fn in files:
                    full = os.path.join(root, fn)
                    relative = os.path.relpath(full, buckets_dir)
                    if _should_back_up_before_import(relative):
                        z.write(full, relative)
        return zpath
    except Exception as e:
        logger.warning(f"[github] pre-import backup failed: {e}")
        return ""


def register(mcp) -> None:

    @mcp.custom_route("/api/github/status", methods=["GET"])
    async def api_github_status(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        _gh_cfg_now = sh.config.get("github_sync", {}) or {}
        _auto_min = int(_gh_cfg_now.get("auto_interval_minutes") or 0)
        _token_set = bool(
            os.environ.get("OMBRE_GITHUB_TOKEN") or _gh_cfg_now.get("token")
        )
        if sh.github_sync_instance is None:
            return JSONResponse({
                "ok": True,
                "configured": False,
                "repo": _gh_cfg_now.get("repo", ""),
                "branch": _gh_cfg_now.get("branch", "main"),
                "path_prefix": _gh_cfg_now.get("path_prefix", "ombre"),
                "token_set": _token_set,
                "auto_interval_minutes": _auto_min,
            })
        return JSONResponse({
            "ok": True,
            "configured": True,
            "token_set": _token_set,
            "auto_interval_minutes": _auto_min,
            **sh.github_sync_instance.status(),
        })

    @mcp.custom_route("/api/github/config", methods=["POST"])
    async def api_github_config(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)

        if "clear" in body and not isinstance(body["clear"], bool):
            return JSONResponse(
                {"ok": False, "error": "clear 必须是布尔值"},
                status_code=400,
            )
        string_fields = ("token", "repo", "branch", "path_prefix")
        if any(key in body and not isinstance(body[key], str) for key in string_fields):
            return JSONResponse({"ok": False, "error": "GitHub 配置字段必须是字符串"}, status_code=400)

        supplied = {
            key: str(body[key]).strip()
            for key in string_fields
            if key in body
        }
        if any(len(supplied.get(key, "")) > limit for key, limit in (
            ("token", 8192),
            ("repo", 255),
            ("branch", 255),
            ("path_prefix", 512),
        )):
            return JSONResponse({"ok": False, "error": "GitHub 配置字段过长"}, status_code=400)
        if any("\n" in value or "\r" in value for value in supplied.values()):
            return JSONResponse({"ok": False, "error": "GitHub 配置不能包含换行"}, status_code=400)

        current_cfg = dict(sh.config.get("github_sync", {}) or {})
        try:
            auto_raw = (
                body["auto_interval_minutes"]
                if "auto_interval_minutes" in body
                else current_cfg.get("auto_interval_minutes", 0)
            )
            if isinstance(auto_raw, bool):
                raise ValueError("boolean is not an interval")
            auto_interval = int(auto_raw or 0)
        except (TypeError, ValueError, OverflowError):
            return JSONResponse({"ok": False, "error": "auto_interval_minutes 必须是整数"}, status_code=400)
        if not 0 <= auto_interval <= 10_080:
            return JSONResponse({"ok": False, "error": "auto_interval_minutes 必须在 0-10080 之间"}, status_code=400)

        if body.get("clear") is True:
            # 清空是破坏性操作，只接受显式 clear=true。空表单或部分
            # 更新绝不能再被误解为「删掉 token/repo」。
            gh_cfg = {
                "repo": "",
                "branch": supplied.get("branch") or "main",
                "path_prefix": supplied.get("path_prefix", "ombre"),
                "auto_interval_minutes": 0,
            }
            try:
                _save_github_config_to_disk(gh_cfg)
            except Exception as e:
                logger.warning(f"[github] config.yaml 清空写入失败: {e}")
                return JSONResponse({"ok": False, "error": f"配置写入磁盘失败，未清空：{e}"}, status_code=500)
            sh.github_sync_instance = None
            sh.restart_github_auto_task(0)
            sh.config["github_sync"] = gh_cfg
            return JSONResponse({
                "ok": True,
                "message": "已清空 GitHub 同步配置",
                "configured": False,
                "token_set": bool(os.environ.get("OMBRE_GITHUB_TOKEN")),
            })

        # 持久化到 config.yaml（含 token，config.yaml 是 bind mount 重启不丢）。
        # 先落盘、落盘成功才更新内存里的 sh.config / github_sync_instance——
        # 避免「内存里已经是新配置、但磁盘还是旧的」这种半保存状态在下次重启时丢数据。
        gh_cfg = current_cfg
        # Secret inputs are write-only: an empty token means "keep the saved
        # token", never "erase it".  Empty repo follows the same safe partial
        # update rule; explicit clear=true is the only erasure path.
        if supplied.get("token"):
            gh_cfg["token"] = supplied["token"]
        if supplied.get("repo"):
            gh_cfg["repo"] = supplied["repo"]
        if "branch" in supplied:
            gh_cfg["branch"] = supplied["branch"] or "main"
        else:
            gh_cfg.setdefault("branch", "main")
        if "path_prefix" in supplied:
            # Empty is meaningful here: it selects the repository root.
            gh_cfg["path_prefix"] = supplied["path_prefix"]
        else:
            gh_cfg.setdefault("path_prefix", "ombre")
        gh_cfg["auto_interval_minutes"] = auto_interval
        try:
            _save_github_config_to_disk(gh_cfg)
        except Exception as e:
            logger.warning(f"[github] config.yaml 写入失败: {e}")
            return JSONResponse({"ok": False, "error": f"配置写入磁盘失败，未保存：{e}"}, status_code=500)

        sh.config["github_sync"] = gh_cfg
        # 重建实例。平台环境 token 与启动时语义一致，优先于磁盘值。
        _tok = str(
            os.environ.get("OMBRE_GITHUB_TOKEN") or gh_cfg.get("token") or ""
        ).strip()
        repo = str(gh_cfg.get("repo") or "").strip()
        branch = str(gh_cfg.get("branch") or "main").strip() or "main"
        path_prefix = str(gh_cfg.get("path_prefix", "ombre") or "").strip()
        if _tok and repo:
            sh.github_sync_instance = GitHubSync(
                token=_tok,
                repo=repo,
                branch=branch,
                path_prefix=path_prefix,
                max_source_bytes=int(
                    (sh.config.get("limits") or {}).get(
                        "max_grow_input_bytes", 2 * 1024 * 1024
                    )
                ),
            )
            sh.restart_github_auto_task(auto_interval)
        else:
            sh.github_sync_instance = None
            sh.restart_github_auto_task(0)
        return JSONResponse({
            "ok": True,
            "message": "配置已保存",
            "configured": sh.github_sync_instance is not None,
            "token_set": bool(_tok),
        })

    @mcp.custom_route("/api/github/validate", methods=["POST"])
    async def api_github_validate(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if sh.github_sync_instance is None:
            return JSONResponse({"ok": False, "error": "尚未配置 GitHub 同步"}, status_code=400)
        result = await sh.github_sync_instance.validate()
        return JSONResponse(result)

    @mcp.custom_route("/api/github/sync", methods=["POST"])
    async def api_github_sync(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if sh.github_sync_instance is None:
            return JSONResponse({"ok": False, "error": "尚未配置 GitHub 同步，请先填写配置并保存"}, status_code=400)
        buckets_dir = sh.config.get("buckets_dir", "")
        if not buckets_dir:
            return JSONResponse({"ok": False, "error": "buckets_dir 未配置"}, status_code=500)
        result = await sh.github_sync_instance.sync(buckets_dir)
        return JSONResponse(result)

    @mcp.custom_route("/api/github/import", methods=["POST"])
    async def api_github_import(request: Request) -> Response:
        """从 GitHub 拉回记忆（恢复 / 回滚）。⚠️ 会覆盖本地同名记忆。

        合并覆盖语义 + 导入前自动 zip 备份本地（可退回）。导入后建议跑 backfill 重建
        向量（前端会自动接着调 /api/embedding/backfill）。embeddings.db 不在仓库里。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        if sh.github_sync_instance is None:
            return JSONResponse({"ok": False, "error": "尚未配置 GitHub 同步，请先填写配置并保存"}, status_code=400)
        buckets_dir = sh.config.get("buckets_dir", "")
        if not buckets_dir:
            return JSONResponse({"ok": False, "error": "buckets_dir 未配置"}, status_code=500)
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "无效 JSON"}, status_code=400)
        try:
            force = parse_bool(body.get("force", False))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        async with _import_lock:
            # 1) 导入前自动备份本地（合并覆盖会改动本地，留个后悔药）
            backup = _pre_import_backup(buckets_dir)
            # 记忆安全闸门：备份没成功就默认不动本地记忆——覆盖不可逆，宁可拦下。
            # 用户确认愿意冒险（force=true）才放行，并如实标注这次没有后悔药。
            if not backup and not force:
                return JSONResponse({
                    "ok": False,
                    "error": "导入前的本地备份没有成功，为避免覆盖后无法找回记忆，已取消本次导入。"
                             "请检查数据目录是否可写、磁盘是否有空间后重试；确要强制导入可带 force=true。",
                    "backup_failed": True,
                }, status_code=409)
            # 2) 从 GitHub 拉回。GitHubSync 内部再与定时 sync 共用同一把锁。
            result = await sh.github_sync_instance.import_from_github(buckets_dir)
            # 导入不是事务：失败时前面已经装进去的文件仍留在磁盘上。
            # 有备份就按备份还原，让「失败」真的等于「什么都没变」。
            if not result.get("ok") and backup:
                回滚 = _rollback_from_backup(buckets_dir, backup)
                result["rolled_back"] = 回滚
                if 回滚.get("ok"):
                    result["error"] = (
                        f"{result.get('error') or '导入失败'}；"
                        f"已按导入前的备份还原本地（{回滚['restored']} 个文件）。"
                        "本次导入若新增过文件，不会被删除。"
                    )
                else:
                    # 回滚也失败——这是最坏的一档，必须说得明明白白，
                    # 绝不能因为「我们尝试过回滚」就把它说成已经恢复。
                    result["error"] = (
                        f"{result.get('error') or '导入失败'}；"
                        f"**自动还原没有完成**（成功 {回滚['restored']} 个，"
                        f"失败 {回滚.get('failed_count', 0)} 个）。"
                        f"本地目前可能是新旧混合状态，请用备份手动恢复：{backup}"
                    )
            if result.pop("you_restored", False):
                try:
                    state = sh.you_service.status()
                    sh.you_tool_gate.sync(state.enabled)
                except Exception:
                    state = sh.you_service.status()
                    if state.enabled:
                        try:
                            sh.you_service.set_enabled(
                                False,
                                expected_revision=state.state_revision,
                            )
                        except Exception:
                            pass
                    try:
                        sh.you_tool_gate.sync(False)
                    except Exception:
                        pass
                    result["ok"] = False
                    result["error"] = "恢复后的 You 开关未能生效，已按关闭处理"
            # them 与 you 同构，同样要在恢复后把工具门对回磁盘上的状态。
            # 少了这一段，`github_sync` 明明返回了 `them_restored`，却没有任何人消费：
            # 磁盘上的开关已经变了，当前进程的工具清单还停在旧状态，要重启才对得上。
            if result.pop("them_restored", False):
                try:
                    state = sh.them_service.status()
                    sh.them_tool_gate.sync(state.enabled)
                except Exception:
                    state = sh.them_service.status()
                    if state.enabled:
                        try:
                            sh.them_service.set_enabled(
                                False,
                                expected_revision=state.state_revision,
                            )
                        except Exception:
                            pass
                    try:
                        sh.them_tool_gate.sync(False)
                    except Exception:
                        pass
                    result["ok"] = False
                    result["error"] = "恢复后的 them 开关未能生效，已按关闭处理"
            result["pre_import_backup"] = backup
            # 3) 让 bucket_mgr 的 BM25 索引失效（导入直写磁盘，绕过了 bucket_mgr 的脏标记）
            try:
                if sh.bucket_mgr is not None:
                    sh.bucket_mgr.invalidate_bm25()
            except Exception:
                pass
        return JSONResponse(result)
