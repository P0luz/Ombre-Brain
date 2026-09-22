from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from letter_display import format_letter_written_age
import tools._runtime as rt
from tools.plan.core import letter_read
import web.hooks as hooks


TOKYO = timezone(timedelta(hours=9), name="Asia/Tokyo")
NOW = datetime(2026, 1, 2, 12, 0, tzinfo=TOKYO)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"letter_date": "2026-01-02"}, "写于 2026-01-02 · 今天"),
        ({"letter_date": "2026-01-01"}, "写于 2026-01-01 · 1 天前"),
        ({"letter_date": "2025-12-31"}, "写于 2025-12-31 · 2 天前"),
        ({"letter_date": "2025-12-30"}, "写于 2025-12-30 · 3 天前"),
        ({"letter_date": "2026-01-01T15:30:00+00:00"}, "写于 2026-01-02 · 今天"),
        ({"letter_date": "2026-01-03"}, "写于 2026-01-03 · 日期未知"),
        ({"letter_date": "not-a-date"}, "写于 not-a-date · 日期未知"),
        ({}, "写于 日期未知"),
        ({"created": "2026-01-01T00:00:00+09:00"}, "写于 2026-01-01 · 1 天前"),
    ],
)
def test_format_letter_written_age_uses_tokyo_calendar(metadata, expected):
    assert format_letter_written_age(metadata, now=NOW) == expected


class _LetterManager:
    async def list_all(self, include_archive=False):
        return [
            {
                "id": "letter-1",
                "content": "dated body",
                "metadata": {
                    "type": "letter",
                    "author": "user",
                    "letter_date": "2026-01-01",
                },
            }
        ]


class _DisabledEmbedding:
    enabled = False


@pytest.mark.asyncio
async def test_letter_read_uses_shared_formatter(monkeypatch):
    rt.bucket_mgr = _LetterManager()
    rt.embedding_engine = _DisabledEmbedding()
    expected = "写于 fixed-date · 7 天前"
    monkeypatch.setattr("tools.plan.core.format_letter_written_age", lambda metadata: expected)

    output = await letter_read(query="dated", author="user", limit=1)

    assert expected in output


def test_session_start_hook_uses_same_formatter_symbol():
    assert hooks.format_letter_written_age is format_letter_written_age


class _Routes:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def _register(handler):
            self.routes[path] = handler
            return handler
        return _register


class _HookBuckets:
    async def list_all(self, include_archive=False):
        return [
            {
                "id": "core",
                "content": "core context",
                "metadata": {"type": "dynamic", "pinned": True},
            },
            {
                "id": "user-letter",
                "content": "user body",
                "metadata": {"type": "letter", "author": "user"},
            },
            {
                "id": "ai-letter",
                "content": "ai body",
                "metadata": {"type": "letter", "author": "AI"},
            },
        ]


@pytest.mark.asyncio
async def test_session_start_hook_renders_shared_letter_formatter(monkeypatch):
    routes = _Routes()
    hooks.register(routes)
    monkeypatch.setenv("OMBRE_HOOK_ALLOW_PUBLIC", "1")
    monkeypatch.setenv("AI_NAME", "AI")
    monkeypatch.setattr(hooks.sh, "bucket_mgr", _HookBuckets())
    monkeypatch.setattr(
        hooks.sh,
        "dehydrator",
        SimpleNamespace(dehydrate=AsyncMock(return_value="core summary")),
    )
    monkeypatch.setattr(hooks.sh, "fire_webhook", AsyncMock())
    marker = "写于 fixed-date · 7 天前"
    monkeypatch.setattr(hooks, "format_letter_written_age", lambda metadata: marker)

    response = await routes.routes["/breath-hook"](
        SimpleNamespace(query_params={}, headers={})
    )

    body = response.body.decode("utf-8")
    assert body.count(marker) == 2
