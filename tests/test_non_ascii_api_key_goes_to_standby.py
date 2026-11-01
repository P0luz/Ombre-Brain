from __future__ import annotations

import tempfile

import pytest

import errors
from embedding_engine import EmbeddingEngine, _first_non_ascii, _header_safe

GOOD_KEY = "AIzaSyC" + "x" * 32
CONTAMINATED = "AI密钥Sy" + "x" * 30


def _engine(tmp_path, api_key, **overrides):
    embed = {
        "enabled": True,
        "provider": "openai",
        "api_format": "openai_compat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": api_key,
        "model": "gemini-embedding-001",
        "timeout_seconds": 5,
    }
    embed.update(overrides)
    return EmbeddingEngine({"buckets_dir": str(tmp_path), "embedding": embed})


@pytest.fixture(autouse=True)
def _error_log():
    errors.configure_errors_path(tempfile.mkdtemp(prefix="e104-"))


@pytest.mark.parametrize("value", [GOOD_KEY, "sk-abc123", "ollama", ""])
def test_ascii_keys_are_header_safe(value):
    assert _header_safe(value) is True


@pytest.mark.parametrize(
    "value", [CONTAMINATED, "密钥", "sk-abc\uff0d123", "key\u3000with\u3000ideographic\u3000space"]
)
def test_non_ascii_keys_are_refused(value):
    assert _header_safe(value) is False


def test_position_is_one_based_and_points_at_the_first_bad_character():
    assert _first_non_ascii(CONTAMINATED) == 3
    assert _first_non_ascii("密钥") == 1
    assert _first_non_ascii(GOOD_KEY) == 0


def test_a_contaminated_key_disables_embedding_instead_of_retrying(tmp_path):
    engine = _engine(tmp_path, CONTAMINATED)

    assert engine.enabled is False


def test_the_recorded_error_names_the_real_cause(tmp_path):
    _engine(tmp_path, CONTAMINATED)

    detail = " ".join(str(e.get("detail")) for e in errors.recent_errors(limit=5))
    assert "api_key" in detail
    assert "非 ASCII" in detail
    assert "第 3 位" in detail
    assert "UnicodeEncodeError" not in detail


def test_the_vector_store_is_still_created(tmp_path):
    engine = _engine(tmp_path, CONTAMINATED)

    assert engine.db_path
    import os

    assert os.path.exists(engine.db_path)


@pytest.mark.asyncio
async def test_writes_degrade_instead_of_raising(tmp_path):
    engine = _engine(tmp_path, CONTAMINATED)

    assert await engine.generate_and_store("b1", "今天下午和万世聊了很久") is False


def test_a_clean_key_still_enables_embedding(tmp_path):
    assert _engine(tmp_path, GOOD_KEY).enabled is True


def test_local_backend_is_unaffected(tmp_path):
    engine = _engine(
        tmp_path,
        CONTAMINATED,
        api_format="ollama",
        base_url="http://127.0.0.1:11434/v1",
    )

    assert engine.enabled is True
