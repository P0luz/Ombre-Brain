"""Small ASGI request-size guard for the public MCP endpoint."""

from __future__ import annotations

import json
from typing import Awaitable, Callable


_Receive = Callable[[], Awaitable[dict]]
_Send = Callable[[dict], Awaitable[None]]
_REJECTION_DRAIN_MULTIPLIER = 2


# 唯一的公开 MCP 端点。
#
# `/mcp-extra` 在 2.8.5 退役、3.2.0 随信件恢复、3.4.0 随信件并回主链路再次退役。
# 它从这里移除后不再被当作 MCP 路径：路由已经不存在，请求会 404，而在 404 之前
# 先落到管理面的体积限制上——这比继续认它更严，是想要的方向。
_MCP_ENDPOINT_PATHS = frozenset({"/mcp"})


def is_mcp_endpoint_path(path: object) -> bool:
    """Match the public MCP endpoints without accepting prefix lookalikes."""
    return str(path or "").rstrip("/") in _MCP_ENDPOINT_PATHS


def is_sse_endpoint_path(path: object) -> bool:
    """Match FastMCP's legacy SSE handshake and message endpoints."""
    normalized = str(path or "").rstrip("/") or "/"
    return normalized == "/sse" or normalized == "/messages" or normalized.startswith(
        "/messages/"
    )


class MCPRequestBodyLimitMiddleware:
    """Reject oversized MCP requests before JSON-RPC parsing or tool dispatch."""

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        self.app = app
        self.max_bytes = max(0, int(max_bytes))
        self.path_matcher = path_matcher

    async def __call__(self, scope: dict, receive: _Receive, send: _Send) -> None:
        if (
            self.max_bytes <= 0
            or scope.get("type") != "http"
            or not self.path_matcher(scope.get("path"))
            or str(scope.get("method", "GET")).upper() not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"").decode("latin-1").strip()
        if raw_length:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared_length < 0:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared_length > self.max_bytes:
                # Docker Desktop on Windows may reset the TCP connection when
                # an ASGI app returns before a modest in-flight request body is
                # consumed. Drain only a bounded amount, without parsing or
                # retaining it, so normal oversized clients reliably see 413
                # while very large/slow attacks are still rejected promptly.
                if declared_length <= self.max_bytes * _REJECTION_DRAIN_MULTIPLIER:
                    await self._drain_request(receive, max_bytes=declared_length)
                await self._send_too_large(send)
                return

        received = 0
        buffered: list[dict] = []
        while True:
            message = await receive()
            if not isinstance(message, dict):
                return
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    if message.get("more_body", False):
                        await self._drain_request(receive, max_bytes=self.max_bytes)
                    await self._send_too_large(send)
                    return
                buffered.append(message)
                if not message.get("more_body", False):
                    break

        buffered_iter = iter(buffered)

        async def replay_receive() -> dict:
            try:
                return next(buffered_iter)
            except StopIteration:
                # Long-lived MCP transports keep reading after the request body
                # to observe the real disconnect. Replaying synthetic empty
                # requests forever creates a tight CPU loop.
                return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _drain_request(receive: _Receive, *, max_bytes: int) -> bool:
        """Discard at most ``max_bytes`` and report whether the request ended."""
        drained = 0
        while drained <= max(0, max_bytes):
            message = await receive()
            if not isinstance(message, dict):
                return False
            if message.get("type") == "http.disconnect":
                return True
            if message.get("type") != "http.request":
                continue
            drained += len(message.get("body", b""))
            if not message.get("more_body", False):
                return True
        return False

    async def _send_too_large(self, send: _Send) -> None:
        await self._send_json(
            send,
            413,
            f"MCP request body exceeds {self.max_bytes} bytes",
        )

    @staticmethod
    async def _send_json(send: _Send, status: int, error: str) -> None:
        body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


_LARGE_UPLOAD_PATHS = {
    "/api/import/preflight",
    "/api/import/upload",
    "/api/migrate/upload",
}


class ManagementRequestBodyLimitMiddleware(MCPRequestBodyLimitMiddleware):
    """Bound normal Dashboard/OAuth mutations while preserving large upload APIs."""

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        mcp_path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        def should_limit(path: object) -> bool:
            normalized = str(path or "").rstrip("/") or "/"
            return (
                not mcp_path_matcher(normalized)
                and normalized not in _LARGE_UPLOAD_PATHS
            )

        super().__init__(app, max_bytes=max_bytes, path_matcher=should_limit)

    async def _send_too_large(self, send: _Send) -> None:
        await self._send_json(
            send,
            413,
            f"management request body exceeds {self.max_bytes} bytes",
        )
