"""Authenticated Dashboard switch for the otherwise invisible them module.

结构与 `web/you.py` 一一对应，包括那套「先摘工具、再落盘关闭」的顺序：
关闭时若先落盘、后摘工具，中间那一瞬工具还在清单里但库已经说关了，
调用会打到一个已关闭的模块上。开启则反过来。

多一个可写字段 `max_tokens_per_person`：每人的配额上限由人类在前端定
（poluz 2026-08-20），所以它是配置项不是常量。
"""

import threading

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ombrebrain.them import ThemStoreError
from utils import atomic_update_config_yaml

from . import _shared as sh

# 前端能把每人配额调到多大。封顶不是性能考虑：them 是沉淀不是日记，
# 一个人能写满几千 token 的档案，这个模块就变成了在给人建档。
MAX_TOKENS_CEILING = 4000
MIN_TOKENS_FLOOR = 200

# 「这个字段没出现在请求里」的哨兵，与「传了 null」区分开。
_UNSET = object()


def _persist_quota(quota: int) -> None:
    """配额写进 config.yaml，再同步到内存里那份 config。

    只改内存是不行的：进程一重启就被磁盘上的旧值盖回去，而人类在前端看到的是
    「保存成功」。`atomic_update_config_yaml` 的 docstring 把这个坑写得很清楚，
    我第一版恰恰就踩了它。

    落盘失败直接往上抛，由调用方转成如实的错误响应——绝不能吞掉异常之后
    还回「已保存」。
    """
    def _mutate(save_config: dict) -> None:
        section = save_config.setdefault("them", {})
        if not isinstance(section, dict):
            section = {}
            save_config["them"] = section
        section["max_tokens_per_person"] = quota

    atomic_update_config_yaml(_mutate)
    # 磁盘先落定再改内存：反过来的话，落盘失败会留下一个内存说 A、磁盘说 B 的
    # 状态，而这次请求还报了错——下次重启才暴露。
    runtime_section = sh.them_service.config.setdefault("them", {})
    if isinstance(runtime_section, dict):
        runtime_section["max_tokens_per_person"] = quota


def register(mcp) -> None:
    commit_lock = threading.Lock()

    def response_payload() -> dict[str, object]:
        state = sh.them_service.status()
        return {
            "enabled": bool(state.enabled),
            "state_revision": int(state.state_revision),
            "max_tokens_per_person": int(sh.them_service.max_tokens_per_person),
        }

    @mcp.custom_route("/api/settings/them", methods=["GET"])
    async def get_them_setting(request: Request) -> Response:
        error = sh._require_auth(request)
        if error:
            return error
        return JSONResponse(response_payload(), headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/api/settings/them", methods=["POST"])
    async def set_them_setting(request: Request) -> Response:
        error = sh._require_auth(request)
        if error:
            return error
        try:
            body = await sh._read_json_object(request)
        except (ValueError, TypeError):
            return JSONResponse({"error": "无效 JSON"}, status_code=400)
        if not set(body) <= {"enabled", "state_revision", "max_tokens_per_person"}:
            return JSONResponse(
                {"error": "只接受 enabled、state_revision 和 max_tokens_per_person"},
                status_code=400,
            )
        if not {"enabled", "state_revision"} <= set(body):
            return JSONResponse(
                {"error": "enabled 和 state_revision 必填"}, status_code=400
            )
        enabled = body.get("enabled")
        revision = body.get("state_revision")
        if (
            not isinstance(enabled, bool)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            return JSONResponse({"error": "开关参数格式无效"}, status_code=400)

        # 区分「没传这个字段」和「传了 null」：前者是不改配额，后者是个无效值。
        # 用 body.get(...) 的话两者都是 None，显式传 null 会被静默当成不改，
        # 而调用方以为自己设了个值。
        quota = body.get("max_tokens_per_person", _UNSET)
        if quota is not _UNSET:
            if isinstance(quota, bool) or not isinstance(quota, int):
                return JSONResponse({"error": "配额必须是整数"}, status_code=400)
            if not MIN_TOKENS_FLOOR <= quota <= MAX_TOKENS_CEILING:
                return JSONResponse(
                    {
                        "error": f"每人配额只能在 {MIN_TOKENS_FLOOR}–{MAX_TOKENS_CEILING} "
                        "之间；再大就不是沉淀，是在给人建档了。"
                    },
                    status_code=400,
                )

        with commit_lock, sh.them_tool_gate.lock:
            before = sh.them_service.status()
            if revision != before.state_revision:
                return JSONResponse(
                    {"error": "开关状态已变化，请刷新后重试"}, status_code=409
                )
            try:
                if enabled:
                    state = sh.them_service.set_enabled(True, expected_revision=revision)
                    visible = sh.them_tool_gate.sync(True)
                else:
                    visible = sh.them_tool_gate.sync(False)
                    if visible:
                        raise RuntimeError("MCP tool removal failed")
                    state = sh.them_service.set_enabled(False, expected_revision=revision)
                if visible != state.enabled:
                    raise RuntimeError("MCP tool state mismatch")
            except ThemStoreError as exc:
                try:
                    sh.them_tool_gate.sync(sh.them_service.status().enabled)
                except Exception:
                    pass
                status = 409 if "revision conflict" in str(exc) else 503
                message = (
                    "开关状态已变化，请刷新后重试" if status == 409 else "them 暂时不可用"
                )
                return JSONResponse({"error": message}, status_code=status)
            except Exception:
                if enabled and before.enabled is False:
                    current = sh.them_service.status()
                    if current.enabled:
                        try:
                            sh.them_service.set_enabled(
                                False, expected_revision=current.state_revision
                            )
                        except Exception:
                            pass
                try:
                    sh.them_tool_gate.sync(sh.them_service.status().enabled)
                except Exception:
                    pass
                return JSONResponse({"error": "them 开关未能生效"}, status_code=503)

            # 配额落盘单独一段，不裹在上面那个 try 里：它失败的时候开关**已经**
            # 生效了，混进去会让回滚把开关一起撤掉，还回一句「开关未能生效」——
            # 报错报在了没坏的那一半上。
            if quota is not _UNSET:
                try:
                    _persist_quota(quota)
                except Exception:
                    return JSONResponse(
                        {
                            "error": "开关已生效，但每人配额没能写进 config.yaml，"
                            "仍是原来的值。请检查配置文件是否可写后重试。"
                        },
                        status_code=503,
                    )

        return JSONResponse(response_payload(), headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/api/them/people", methods=["GET"])
    async def list_them_people(request: Request) -> Response:
        """名册：**只有称呼，没有任何一条认识。**

        rule.md 13.3 的口子只开到这里。认识本身、依据、历史一概不出这个接口——
        那是模型的，不是给人读的。
        """
        error = sh._require_auth(request)
        if error:
            return error
        return JSONResponse(
            {"people": sh.them_service.list_people()},
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/them/people/new", methods=["POST"])
    async def add_them_person(request: Request) -> Response:
        """登记一个自己认识的人。

        这一份的认识对人类可见，也只有这一份能留言纠错——
        模型自己认识的人，人类连它记了什么都看不见，那种情况下的「纠错」
        是在对着看不见的东西提意见。
        """
        error = sh._require_auth(request)
        if error:
            return error
        try:
            body = await sh._read_json_object(request)
        except (ValueError, TypeError):
            return JSONResponse({"error": "无效 JSON"}, status_code=400)
        if set(body) != {"names"}:
            return JSONResponse({"error": "只接受 names"}, status_code=400)
        names = body.get("names")
        if not isinstance(names, list) or not all(
            isinstance(name, str) for name in names
        ):
            return JSONResponse({"error": "names 必须是字符串数组"}, status_code=400)
        try:
            person = sh.them_service.add_person(names)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ThemStoreError:
            return JSONResponse({"error": "them 暂时不可用"}, status_code=503)
        return JSONResponse(
            {
                "person_id": person.id,
                "names": list(person.names),
                "revision": person.revision,
                "origin": person.origin,
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/them/note", methods=["POST"])
    async def leave_them_note(request: Request) -> Response:
        """给模型留一条纠错。

        下次浮现时在尾部交给它一次，读完就清。**不占每人的 token 配额**——
        配额管的是模型自己沉淀了多少，人类说的话不该挤掉模型的记忆。
        改不改、信不信由模型自己定：这是纠错，不是命令。
        """
        error = sh._require_auth(request)
        if error:
            return error
        try:
            body = await sh._read_json_object(request)
        except (ValueError, TypeError):
            return JSONResponse({"error": "无效 JSON"}, status_code=400)
        if set(body) != {"person_id", "text"}:
            return JSONResponse({"error": "只接受 person_id 和 text"}, status_code=400)
        person_id = body.get("person_id")
        text = body.get("text")
        if not isinstance(person_id, str) or not isinstance(text, str):
            return JSONResponse({"error": "参数格式无效"}, status_code=400)
        try:
            person = sh.them_service.leave_note(person_id, text)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ThemStoreError:
            return JSONResponse({"error": "them 暂时不可用"}, status_code=503)
        return JSONResponse(
            {
                "person_id": person.id,
                "pending_notes": [dict(note) for note in person.pending_notes],
                "revision": person.revision,
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/them/people", methods=["POST"])
    async def rename_them_person(request: Request) -> Response:
        """改一个人的正名与昵称。人类唯一改得动的东西。

        改完模型会在下次浮现时被提醒一次，认识正文一个字都不动——
        那些句子是模型写的，人类改的是名册上的称呼，不是模型的判断。
        """
        error = sh._require_auth(request)
        if error:
            return error
        try:
            body = await sh._read_json_object(request)
        except (ValueError, TypeError):
            return JSONResponse({"error": "无效 JSON"}, status_code=400)
        if set(body) != {"person_id", "names", "revision"}:
            return JSONResponse(
                {"error": "只接受 person_id、names 和 revision"}, status_code=400
            )
        person_id = body.get("person_id")
        names = body.get("names")
        revision = body.get("revision")
        if (
            not isinstance(person_id, str)
            or not isinstance(names, list)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or not all(isinstance(name, str) for name in names)
        ):
            return JSONResponse({"error": "参数格式无效"}, status_code=400)

        try:
            person = sh.them_service.rename_person(
                person_id, names, expected_revision=revision
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except ThemStoreError as exc:
            status = 409 if "revision conflict" in str(exc) else 503
            return JSONResponse(
                {
                    "error": "这个人的资料已经变过了，请刷新后重试"
                    if status == 409
                    else "them 暂时不可用"
                },
                status_code=status,
            )
        return JSONResponse(
            {
                "person_id": person.id,
                "names": list(person.names),
                "revision": person.revision,
            },
            headers={"Cache-Control": "no-store"},
        )
