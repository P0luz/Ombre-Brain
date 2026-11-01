from __future__ import annotations

import httpx
import pytest
from openai._models import FinalRequestOptions

from embedding_engine import _API_TIMEOUT_SECONDS, APIEmbeddingEngine

# httpx.AsyncClient() 自己的默认超时。openai SDK 只在 http_client.timeout
# **不等于**这个值时才采纳它，否则换成自己的 Timeout(connect=5, read=600...)。
# 于是 timeout_seconds 恰好配成 5 的用户，读超时会被悄悄放大到 600 秒。
HTTPX_DEFAULT = 5.0


def _engine(base_url="https://example.invalid/v1", timeout_seconds=HTTPX_DEFAULT):
    return APIEmbeddingEngine(
        api_key="k", base_url=base_url, model="m", timeout_seconds=timeout_seconds
    )


def _request_timeout(engine):
    options = FinalRequestOptions.construct(
        method="post", url="/embeddings", json_data={}
    )
    return engine._client._build_request(options).extensions["timeout"]


@pytest.mark.parametrize("phase", ["connect", "read", "write", "pool"])
def test_timeout_equal_to_the_httpx_default_still_reaches_the_request(phase):
    assert _request_timeout(_engine(timeout_seconds=HTTPX_DEFAULT))[phase] == (
        pytest.approx(HTTPX_DEFAULT)
    )


def test_sdk_600s_read_timeout_never_takes_over():
    for configured in (HTTPX_DEFAULT, 7.5, 30.0, 120.0):
        assert _request_timeout(_engine(timeout_seconds=configured))["read"] == (
            pytest.approx(configured)
        )


def test_invalid_timeout_falls_back_to_the_default():
    assert _request_timeout(_engine(timeout_seconds=0))["read"] == pytest.approx(
        _API_TIMEOUT_SECONDS
    )


def test_the_httpx_default_this_guards_against_has_not_moved():
    assert httpx.Timeout(HTTPX_DEFAULT) == httpx.AsyncClient().timeout


@pytest.mark.parametrize(
    "base_url, trust_env",
    [
        ("http://127.0.0.1:11434/v1", False),
        ("http://localhost:11434/v1", False),
        ("http://ombre-ollama:11434/v1", False),
        ("https://api.example.com/v1", True),
    ],
)
def test_local_hosts_still_bypass_system_proxies(base_url, trust_env):
    assert _engine(base_url=base_url)._client._client.trust_env is trust_env
