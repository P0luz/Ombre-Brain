"""Authenticated Dashboard switch for the otherwise invisible You module."""

import threading

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ombrebrain.you import YouStoreError

from . import _shared as sh


def register(mcp) -> None:
    commit_lock = threading.Lock()

    def response_payload() -> dict[str, object]:
        state = sh.you_service.status()
        return {
            "enabled": bool(state.enabled),
            "state_revision": int(state.state_revision),
        }

    @mcp.custom_route("/api/settings/you", methods=["GET"])
    async def get_you_setting(request: Request) -> Response:
        error = sh._require_auth(request)
        if error:
            return error
        return JSONResponse(
            response_payload(),
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/api/settings/you", methods=["POST"])
    async def set_you_setting(request: Request) -> Response:
        error = sh._require_auth(request)
        if error:
            return error
        try:
            body = await sh._read_json_object(request)
        except (ValueError, TypeError):
            return JSONResponse({"error": "无效 JSON"}, status_code=400)
        if set(body) != {"enabled", "state_revision"}:
            return JSONResponse(
                {"error": "只接受 enabled 和 state_revision"},
                status_code=400,
            )
        enabled = body.get("enabled")
        revision = body.get("state_revision")
        if (
            not isinstance(enabled, bool)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            return JSONResponse({"error": "开关参数格式无效"}, status_code=400)

        with commit_lock, sh.you_tool_gate.lock:
            before = sh.you_service.status()
            if revision != before.state_revision:
                return JSONResponse(
                    {"error": "开关状态已变化，请刷新后重试"},
                    status_code=409,
                )
            try:
                if enabled:
                    state = sh.you_service.set_enabled(
                        True,
                        expected_revision=revision,
                    )
                    visible = sh.you_tool_gate.sync(True)
                else:
                    # Hide the discoverable surface before persisting off. A
                    # failed disk write is rolled back to the latest authority.
                    visible = sh.you_tool_gate.sync(False)
                    if visible:
                        raise RuntimeError("MCP tool removal failed")
                    state = sh.you_service.set_enabled(
                        False,
                        expected_revision=revision,
                    )
                if visible != state.enabled:
                    raise RuntimeError("MCP tool state mismatch")
            except YouStoreError as exc:
                try:
                    sh.you_tool_gate.sync(sh.you_service.status().enabled)
                except Exception:
                    pass
                status = 409 if "revision conflict" in str(exc) else 503
                message = (
                    "开关状态已变化，请刷新后重试"
                    if status == 409
                    else "You 暂时不可用"
                )
                return JSONResponse({"error": message}, status_code=status)
            except Exception:
                # The handler itself also checks the state, so this rollback
                # makes background processing match the fail-closed tool result.
                if enabled and before.enabled is False:
                    current = sh.you_service.status()
                    if current.enabled:
                        try:
                            sh.you_service.set_enabled(
                                False,
                                expected_revision=current.state_revision,
                            )
                        except Exception:
                            pass
                try:
                    sh.you_tool_gate.sync(sh.you_service.status().enabled)
                except Exception:
                    pass
                return JSONResponse({"error": "You 开关未能生效"}, status_code=503)

        return JSONResponse(
            response_payload(),
            headers={"Cache-Control": "no-store"},
        )
