"""v2.3.21: letter author 自定义署名 + ai_name 行为回归。

覆盖 tools/plan/core.py 的 letter_write / letter_read：
- author 接受任意字符串署名
- "ai" / 等于 ai_name / 旧值 "claude"(历史) → 统一存 ai_name 的值
- ai_name 默认取环境变量 AI_NAME，回退 "AI"，显式传参优先
- 任意其它字符串原样作为署名
- letter_read 原样返回存储署名、按方向过滤时把旧 "claude" 视为 AI 侧
"""

from unittest.mock import MagicMock

import pytest

from errors import ToolInputError

import tools._runtime as rt
from tools.plan.core import letter_write, letter_read


class DisabledEmbedding:
    enabled = False

    async def generate_and_store(self, *a, **k):
        return None


def _install(bucket_mgr):
    rt.bucket_mgr = bucket_mgr
    rt.embedding_engine = DisabledEmbedding()
    rt.logger = MagicMock()


async def _author_of(bucket_mgr, bucket_id):
    bucket = await bucket_mgr.get(bucket_id)
    return bucket["metadata"].get("author")


def _id(result: str) -> str:
    # letter_write 返回 "💌letter→<id> [<author>]"
    return result.split("→", 1)[1].split(" ", 1)[0]


@pytest.mark.asyncio
async def test_ai_author_defaults_to_env_ai_name(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    res = await letter_write(author="ai", content="hello from the ai side")
    assert await _author_of(bucket_mgr, _id(res)) == "Ombre"


@pytest.mark.asyncio
async def test_ai_author_falls_back_to_AI_when_env_unset(bucket_mgr, monkeypatch):
    monkeypatch.delenv("AI_NAME", raising=False)
    _install(bucket_mgr)
    res = await letter_write(author="ai", content="fallback case")
    assert await _author_of(bucket_mgr, _id(res)) == "AI"


@pytest.mark.asyncio
async def test_explicit_ai_name_param_overrides_env(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "FromEnv")
    _install(bucket_mgr)
    res = await letter_write(author="ai", content="x", ai_name="Explicit")
    assert await _author_of(bucket_mgr, _id(res)) == "Explicit"


@pytest.mark.asyncio
async def test_legacy_claude_author_maps_to_ai_name(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    res = await letter_write(author="claude", content="legacy author label")
    assert await _author_of(bucket_mgr, _id(res)) == "Ombre"


@pytest.mark.asyncio
async def test_arbitrary_author_string_stored_verbatim(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    res = await letter_write(author="Nova", content="custom signature")
    assert await _author_of(bucket_mgr, _id(res)) == "Nova"


@pytest.mark.asyncio
async def test_user_author_unchanged(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    res = await letter_write(author="user", content="from the human", user_name="Alex")
    bucket = await bucket_mgr.get(_id(res))
    assert bucket["metadata"].get("author") == "user"
    assert bucket["metadata"].get("user_name") == "Alex"


@pytest.mark.asyncio
async def test_empty_author_rejected(bucket_mgr):
    _install(bucket_mgr)
    with pytest.raises(ToolInputError) as excinfo:
        await letter_write(author="   ", content="x")
    assert 'author' in str(excinfo.value)


@pytest.mark.asyncio
async def test_letter_read_displays_stored_signature(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    await letter_write(author="ai", content="signed by ombre side")
    out = await letter_read(limit=10)
    assert "Ombre" in out
    assert "claude" not in out.lower()


@pytest.mark.asyncio
async def test_letter_read_filter_ai_matches_legacy_claude(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    # 直接造一封 author=claude 的「历史」信件
    legacy = await bucket_mgr.create(content="old claude letter", bucket_type="letter", domain=["letter"])
    await bucket_mgr.update(legacy, author="claude")
    await letter_write(author="user", content="user letter")

    ai_only = await letter_read(author="ai", limit=10)
    assert "old claude letter" in ai_only
    assert "user letter" not in ai_only


@pytest.mark.asyncio
async def test_letter_read_filter_custom_signature(bucket_mgr, monkeypatch):
    monkeypatch.setenv("AI_NAME", "Ombre")
    _install(bucket_mgr)
    await letter_write(author="Nova", content="nova speaking")
    await letter_write(author="ai", content="ombre speaking")

    nova_only = await letter_read(author="Nova", limit=10)
    assert "nova speaking" in nova_only
    assert "ombre speaking" not in nova_only
