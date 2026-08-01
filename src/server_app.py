"""Testable HTTP application and runtime lifecycle assembly.

This module deliberately has no Ombre engine construction at import time. The
CLI entry point creates the concrete services, then passes them into the small
factory and lifecycle objects below. A future desktop host can use the same
boundary without importing the side-effectful ``server`` module.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

import httpx
from starlette.middleware.cors import CORSMiddleware

from utils import parse_bool
from web.request_limits import MCPRequestBodyLimitMiddleware


DEFAULT_MAX_MCP_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_KEEPALIVE_INITIAL_DELAY_SECONDS = 10.0
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 60.0

TokenValidator = Callable[..., bool]
AsyncCallback = Callable[[], Awaitable[Any]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HTTPRuntimeSettings:
    """Normalized settings used while assembling an HTTP MCP application."""

    auth_required: bool
    max_request_bytes: int

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        default_max_request_bytes: int = DEFAULT_MAX_MCP_REQUEST_BYTES,
    ) -> "HTTPRuntimeSettings":
        limits = config.get("limits")
        if not isinstance(limits, Mapping):
            limits = {}
        try:
            max_request_bytes = int(
                limits.get("max_mcp_request_bytes", default_max_request_bytes)
            )
        except (TypeError, ValueError, OverflowError):
            max_request_bytes = default_max_request_bytes
        if max_request_bytes < 0:
            max_request_bytes = default_max_request_bytes
        return cls(
            auth_required=parse_bool(
                config.get("mcp_require_auth", True), default=True
            ),
            max_request_bytes=max_request_bytes,
        )


def merge_mcp_tool_registries(primary: Any, extra: Any) -> int:
    """Merge FastMCP's compatibility registry into the public registry.

    FastMCP does not currently expose a public registry merge API. Keeping this
    compatibility access in one function makes the private dependency easy to
    test and replace when the SDK adds one.
    """

    primary_tools = primary._tool_manager._tools
    extra_tools = extra._tool_manager._tools
    primary_tools.update(extra_tools)
    return len(extra_tools)


def _first_forwarded_value(value: str) -> str:
    return value.split(",", 1)[0].strip()


def _request_resource(scope: Mapping[str, Any], headers: Mapping[bytes, bytes]) -> tuple[str, str]:
    proto = _first_forwarded_value(
        headers.get(b"x-forwarded-proto", b"").decode("latin-1")
    ) or str(scope.get("scheme", "http"))
    host = _first_forwarded_value(
        (headers.get(b"x-forwarded-host") or headers.get(b"host", b"")).decode(
            "latin-1"
        )
    )
    path = str(scope.get("path", ""))
    base = f"{proto}://{host}"
    return f"{base}{path.rstrip('/')}", base


class MCPAuthMiddleware:
    """Require OAuth or a dedicated gateway credential for ``/mcp``.

    Human MCP clients keep using Ombre's OAuth flow. A separately configured
    gateway may use ``X-Ombre-Gateway-Key`` for server-to-server access, so it
    never needs the Dashboard password and OAuth does not need to be disabled.
    """

    def __init__(
        self,
        app: Any,
        *,
        auth_required: bool,
        token_validator: TokenValidator,
    ) -> None:
        self.app = app
        self.auth_required = bool(auth_required)
        self.token_validator = token_validator

    @staticmethod
    def _gateway_key_status(
        headers: Mapping[bytes, bytes],
    ) -> tuple[bool, bool, bool]:
        """Return safe presence and match flags for the machine credential."""
        configured = os.environ.get("OMBRE_GATEWAY_API_KEY", "").strip()
        supplied = headers.get(b"x-ombre-gateway-key", b"").decode("latin-1").strip()
        configured_present = bool(configured)
        header_received = bool(supplied)
        matched = bool(
            configured_present
            and header_received
            and hmac.compare_digest(supplied, configured)
        )
        return configured_present, header_received, matched

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if scope.get("type") == "http" and self.auth_required and path.startswith("/mcp"):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode("latin-1")
            resource, base = _request_resource(scope, headers)
            gateway_configured, gateway_header_received, gateway_matched = (
                self._gateway_key_status(headers)
            )
            valid = gateway_matched or (
                auth.startswith("Bearer ")
                and self.token_validator(auth[7:], resource=resource)
            )
            if not valid:
                logger.warning(
                    "MCP auth rejected: gateway_configured=%s, "
                    "gateway_header_received=%s, gateway_matched=%s",
                    gateway_configured,
                    gateway_header_received,
                    gateway_matched,
                )
                endpoint = path.strip("/")
                metadata_url = (
                    f"{base}/.well-known/oauth-protected-resource/{endpoint}"
                )
                challenge = (
                    'Bearer realm="Ombre Brain",'
                    f' resource_metadata="{metadata_url}", scope="mcp"'
                )
                body = json.dumps(
                    {
                        "error": "Unauthorized",
                        "resource_metadata": metadata_url,
                    }
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"www-authenticate", challenge.encode()],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    }
                )
                return
        await self.app(scope, receive, send)


class MCPAcceptShim:
    """Ensure MCP clients advertise both supported response media types."""

    _REQUIRED = (b"application/json", b"text/event-stream")

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith("/mcp"):
            headers = list(scope.get("headers", []))
            accept_index = next(
                (
                    index
                    for index, (key, _value) in enumerate(headers)
                    if key.lower() == b"accept"
                ),
                -1,
            )
            current = headers[accept_index][1].lower() if accept_index >= 0 else b""
            missing = [value for value in self._REQUIRED if value not in current]
            if missing:
                required = b", ".join(missing)
                if accept_index >= 0 and headers[accept_index][1].strip():
                    headers[accept_index] = (
                        headers[accept_index][0],
                        headers[accept_index][1] + b", " + required,
                    )
                elif accept_index >= 0:
                    headers[accept_index] = (headers[accept_index][0], required)
                else:
                    headers.append((b"accept", required))
                scope = dict(scope)
                scope["headers"] = headers
        await self.app(scope, receive, send)


@dataclass
class RuntimeLifecycle:
    """Own background service startup and shutdown for one HTTP app lifespan."""

    logger: Any
    decay_engine: Any = None
    embedding_outbox: Any = None
    ensure_ollama_child: AsyncCallback | None = None
    stop_ollama_child: AsyncCallback | None = None
    load_tunnel_config: Callable[[], Mapping[str, Any]] | None = None
    start_tunnel: Callable[[str], tuple[bool, str]] | None = None
    stop_tunnel: Callable[[], Any] | None = None
    restart_github_auto_task: Callable[[int], Any] | None = None
    github_auto_interval: int = 0
    boot_marker_path: str = ""
    keepalive_url: str = ""
    keepalive_initial_delay: float = DEFAULT_KEEPALIVE_INITIAL_DELAY_SECONDS
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL_SECONDS
    health_probe_timeout: float = DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS
    _keepalive_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    async def _run_async_step(self, label: str, callback: AsyncCallback | None) -> None:
        if callback is None:
            return
        try:
            await callback()
        except Exception as exc:
            self.logger.warning("%s failed: %s", label, exc)

    def _start_optional_services(self) -> None:
        if self.load_tunnel_config is not None and self.start_tunnel is not None:
            try:
                tunnel_config = self.load_tunnel_config()
                if tunnel_config.get("auto_start") and tunnel_config.get("token"):
                    _ok, message = self.start_tunnel(str(tunnel_config["token"]))
                    self.logger.info("Tunnel auto-start: %s", message)
            except Exception as exc:
                self.logger.warning("tunnel auto-start failed: %s", exc)

        if self.github_auto_interval > 0 and self.restart_github_auto_task is not None:
            try:
                self.restart_github_auto_task(self.github_auto_interval)
            except Exception as exc:
                self.logger.warning("github auto-sync start failed: %s", exc)

    def _reset_boot_marker(self) -> None:
        if not self.boot_marker_path or not os.path.exists(self.boot_marker_path):
            return
        try:
            with open(self.boot_marker_path, "w", encoding="utf-8") as marker:
                marker.write("0")
            self.logger.info("boot ok -> reset .boot_fails")
        except Exception as exc:
            self.logger.warning("reset .boot_fails failed: %s", exc)

    async def _keepalive_loop(self) -> None:
        await asyncio.sleep(max(0.0, self.keepalive_initial_delay))
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await client.get(
                        self.keepalive_url,
                        timeout=self.health_probe_timeout,
                    )
                    self.logger.debug("Keepalive ping OK")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.warning("Keepalive ping failed: %s", exc)
                await asyncio.sleep(max(0.01, self.keepalive_interval))

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._start_optional_services()
        await self._run_async_step(
            "decay engine start",
            getattr(self.decay_engine, "start", None),
        )
        await self._run_async_step("ollama child boot", self.ensure_ollama_child)
        await self._run_async_step(
            "embedding outbox start",
            getattr(self.embedding_outbox, "start", None),
        )
        if self.keepalive_url:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="ombre-health-keepalive",
            )
        self._reset_boot_marker()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self.restart_github_auto_task is not None:
            try:
                self.restart_github_auto_task(0)
            except Exception as exc:
                self.logger.warning("github auto-sync stop failed: %s", exc)

        await self._run_async_step(
            "embedding outbox stop",
            getattr(self.embedding_outbox, "stop", None),
        )
        await self._run_async_step(
            "decay engine stop",
            getattr(self.decay_engine, "stop", None),
        )
        await self._run_async_step("ollama child stop", self.stop_ollama_child)
        if self.stop_tunnel is not None:
            try:
                self.stop_tunnel()
            except Exception as exc:
                self.logger.warning("tunnel stop failed: %s", exc)


def install_runtime_lifespan(app: Any, lifecycle: RuntimeLifecycle) -> Any:
    """Compose Ombre runtime services with an app's existing lifespan."""

    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def managed_lifespan(lifespan_app: Any):
        async with parent_lifespan(lifespan_app):
            await lifecycle.start()
            try:
                yield
            finally:
                await lifecycle.stop()

    app.router.lifespan_context = managed_lifespan
    return app


def build_http_app(
    mcp: Any,
    transport: str,
    *,
    settings: HTTPRuntimeSettings,
    token_validator: TokenValidator,
    lifecycle: RuntimeLifecycle,
) -> Any:
    """Build the HTTP/SSE ASGI app with one consistent middleware stack."""

    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    elif transport == "sse":
        app = mcp.sse_app()
    else:
        raise ValueError(f"HTTP app cannot be built for transport: {transport}")

    install_runtime_lifespan(app, lifecycle)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    app.add_middleware(
        MCPRequestBodyLimitMiddleware,
        max_bytes=settings.max_request_bytes,
    )
    app.add_middleware(MCPAcceptShim)
    app.add_middleware(
        MCPAuthMiddleware,
        auth_required=settings.auth_required,
        token_validator=token_validator,
    )
    app.state.ombre_http_settings = settings
    app.state.ombre_runtime_lifecycle = lifecycle
    return app
