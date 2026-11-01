from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, RateLimitError

import dehydrator as dh_mod
from dehydrator import _RETRY_MAX_ATTEMPTS, Dehydrator

_REQUEST = httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def _status_error(code):
    response = httpx.Response(code, request=_REQUEST)
    return httpx.HTTPStatusError("boom", request=_REQUEST, response=response)


@pytest.fixture
def dehydrator(tmp_path):
    return Dehydrator(
        {
            "buckets_dir": str(tmp_path),
            "dehydration": {
                "enabled": True,
                "api_format": "openai_compat",
                "base_url": "https://example.invalid/v1",
                "api_key": "k",
                "model": "m",
                "timeout_seconds": 5,
            },
        }
    )


def test_sdk_does_not_retry_underneath_the_chat_loop(dehydrator):
    assert dehydrator.client.max_retries == 0


async def _count_attempts(dehydrator, monkeypatch, exc):
    calls = []

    async def boom(*args, **kwargs):
        calls.append(1)
        raise exc

    monkeypatch.setattr(dehydrator, "_chat_once", boom)
    monkeypatch.setattr(dh_mod, "_RETRY_BASE_DELAY", 0.0)
    with pytest.raises(type(exc)):
        await dehydrator._chat("s", "u")
    return len(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        APIConnectionError(request=_REQUEST),
        APITimeoutError(request=_REQUEST),
        RateLimitError("slow", response=httpx.Response(429, request=_REQUEST), body=None),
        _status_error(500),
        _status_error(502),
        _status_error(503),
        _status_error(504),
    ],
    ids=lambda e: type(e).__name__ + str(getattr(getattr(e, "response", None), "status_code", "")),
)
async def test_transient_errors_use_exactly_the_configured_attempt_count(
    dehydrator, monkeypatch, exc
):
    assert await _count_attempts(dehydrator, monkeypatch, exc) == _RETRY_MAX_ATTEMPTS


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ValueError("bad request"), _status_error(400), _status_error(401)])
async def test_permanent_errors_are_not_retried(dehydrator, monkeypatch, exc):
    assert await _count_attempts(dehydrator, monkeypatch, exc) == 1
