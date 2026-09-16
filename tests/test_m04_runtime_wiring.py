from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import unittest

from runtime_control import AdmissionClosed
from runtime_owner import RuntimeAdmissionMiddleware, admission_gate, admitted


ROOT = Path(__file__).resolve().parents[1]


class RuntimeWiringInventoryTests(unittest.TestCase):
    def test_every_mcp_tool_wrapper_has_runtime_admitted_decorator(self) -> None:
        source = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        tools: list[str] = []
        missing: list[str] = []
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            decorators = []
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                    owner = getattr(decorator.func.value, "id", "")
                    decorators.append(f"{owner}.{decorator.func.attr}")
                elif isinstance(decorator, ast.Name):
                    decorators.append(decorator.id)
            if "mcp.tool" in decorators or "mcp_extra.tool" in decorators:
                tools.append(node.name)
                if "runtime_admitted" not in decorators:
                    missing.append(node.name)
        self.assertEqual(
            set(tools),
            {
                "breath",
                "breath_search",
                "breath_advanced",
                "hold",
                "grow",
                "trace",
                "anchor",
                "release",
                "pulse",
                "plan",
                "letter_write",
                "letter_read",
                "letter_lock_update",
                "feel",
                "I",
                "just_now",
                "dream",
            },
        )
        self.assertEqual(missing, [])

    def test_http_middleware_and_sync_cli_are_source_wired(self) -> None:
        server = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
        cli = (ROOT / "src" / "write_memory.py").read_text(encoding="utf-8")
        self.assertIn("_app.add_middleware(RuntimeAdmissionMiddleware)", server)
        self.assertIn("with admitted_sync():", cli)

    def test_startup_order_is_m04_then_emig_then_m03_then_components(self) -> None:
        source = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
        positions = [
            source.index("recover_restore_publications_before_startup("),
            source.index("recover_pending_publish(config_file_path()"),
            source.index("recover_import_transactions("),
            source.index("embedding_engine = EmbeddingEngine(config)"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_only_registered_lifecycle_tasks_use_raw_create_task(self) -> None:
        allowed = {
            ("server.py", "_github_auto_task"),
            ("decay_engine.py", "_task"),
            ("web/ollama_local.py", "_child_monitor_task"),
        }
        found: set[tuple[str, str]] = set()
        for path in (ROOT / "src").rglob("*.py"):
            relative = path.relative_to(ROOT / "src").as_posix()
            if relative.startswith("tests/") or relative in {
                "runtime_owner.py",
                "runtime_control.py",
                "service_quiescence.py",
                "snapshot_barrier.py",
                "config_transaction.py",
                "embedding_publish.py",
            }:
                continue
            text = path.read_text(encoding="utf-8")
            if "create_task(" not in text:
                continue
            if relative == "server.py" and "_github_auto_task = loop.create_task(" in text:
                found.add((relative, "_github_auto_task"))
                text = text.replace("_github_auto_task = loop.create_task(", "")
            if relative == "decay_engine.py" and "self._task = asyncio.create_task(" in text:
                found.add((relative, "_task"))
                text = text.replace("self._task = asyncio.create_task(", "")
            if relative == "web/ollama_local.py" and "_child_monitor_task = asyncio.create_task(" in text:
                found.add((relative, "_child_monitor_task"))
                text = text.replace("_child_monitor_task = asyncio.create_task(", "")
            self.assertNotIn("create_task(", text, relative)
        self.assertEqual(found, allowed)

    def test_real_runtime_components_and_blockers_are_registered(self) -> None:
        source = (ROOT / "src" / "server.py").read_text(encoding="utf-8")
        for name in (
            "decay-engine",
            "github-auto-sync",
            "ollama-managed-child",
            "conversation-import",
            "memory-package-migrate",
            "embedding-migration",
            "embedding-backfill",
            "ollama-model-pull",
        ):
            self.assertIn(f'"{name}"', source)
        self.assertIn("rebuild_and_publish_embeddings_sync", source)


class RuntimeAdmissionMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        status = admission_gate.status()
        if status.state == "CLOSED" and status.active == 0:
            await admission_gate.open()
        self.assertEqual(admission_gate.status().state, "OPEN")

    async def test_application_request_holds_lease_and_health_mcp_are_exempt(self) -> None:
        observed: list[int] = []

        async def app(scope, receive, send):
            observed.append((await admission_gate.active_count()))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RuntimeAdmissionMiddleware(app)
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await middleware({"type": "http", "path": "/api/buckets"}, receive, send)
        await middleware({"type": "http", "path": "/health"}, receive, send)
        await middleware({"type": "http", "path": "/mcp"}, receive, send)
        self.assertEqual(observed, [1, 0, 0])

    async def test_closed_admission_returns_503_but_health_still_runs(self) -> None:
        calls: list[str] = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        middleware = RuntimeAdmissionMiddleware(app)
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await admission_gate.close()
        try:
            await middleware({"type": "http", "path": "/api/buckets"}, receive, send)
            await middleware({"type": "http", "path": "/health"}, receive, send)
        finally:
            await admission_gate.open()
        statuses = [item["status"] for item in sent if item["type"] == "http.response.start"]
        self.assertEqual(statuses, [503, 200])
        self.assertEqual(calls, ["/health"])

    async def test_inherited_token_is_invalid_after_parent_context_exits(self) -> None:
        child_started = asyncio.Event()
        continue_child = asyncio.Event()

        async def child() -> None:
            child_started.set()
            await continue_child.wait()
            async with admitted():
                pass

        async with admitted():
            task = asyncio.create_task(child())
            await child_started.wait()
        await admission_gate.close()
        continue_child.set()
        try:
            with self.assertRaises(AdmissionClosed):
                await task
        finally:
            await admission_gate.open()


if __name__ == "__main__":
    unittest.main()
