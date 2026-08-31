import pytest

from ombrebrain.you import YouService, YouStore
from ombrebrain.you.models import VALID_ASPECTS, VALID_BASES
from ombrebrain.you.tool_gate import YouToolGate


class FakeBucketManager:
    def __init__(self):
        self.buckets = {}

    async def get(self, bucket_id):
        return self.buckets.get(bucket_id)


class FakeSourceStore:
    def __init__(self):
        self.sources = {}

    def read(self, source_id):
        return self.sources[source_id]


class ExplodingDehydrator:
    def __getattr__(self, name):
        async def _boom(*_args, **_kwargs):
            raise AssertionError(f"You 不允许调用 LLM，却调了 dehydrator.{name}")

        return _boom


def _bucket(bucket_id, content):
    return {"id": bucket_id, "content": content, "metadata": {"type": "dynamic"}}


def _enabled(tmp_path):
    manager = FakeBucketManager()
    service = YouService(
        store=YouStore(tmp_path),
        bucket_mgr=manager,
        dehydrator=ExplodingDehydrator(),
        source_store=FakeSourceStore(),
    )
    service.set_enabled(True)
    for index in (1, 2):
        bucket_id = f"memory-{index}"
        manager.buckets[bucket_id] = _bucket(
            bucket_id, f"第 {index} 次她提到希望被叫做 Lin。"
        )
    return service


async def _write(service, **overrides):
    payload = {
        "content": "她希望日常被称呼为 Lin",
        "bucket_ids": ["memory-1", "memory-2"],
        "aspect": "preferred_address",
        "concept_key": "preferred_address",
        "concept_value": "lin",
        "basis": "explicit_statement",
        "explicit": True,
        "long_term": True,
    }
    payload.update(overrides)
    return await service.write(**payload)


@pytest.mark.asyncio
async def test_bad_aspect_names_every_allowed_value(tmp_path):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(service, aspect="nonsense")

    message = str(excinfo.value)
    assert "nonsense" in message
    for allowed in VALID_ASPECTS:
        assert allowed in message, allowed


@pytest.mark.asyncio
async def test_bad_basis_names_every_allowed_value(tmp_path):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(service, basis="nonsense")

    message = str(excinfo.value)
    assert "nonsense" in message
    for allowed in VALID_BASES:
        assert allowed in message, allowed


@pytest.mark.asyncio
async def test_empty_aspect_still_lists_the_allowed_values(tmp_path):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(service, aspect="")

    for allowed in VALID_ASPECTS:
        assert allowed in str(excinfo.value), allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"concept_key": "Not Snake Case"}, "concept_key"),
        ({"concept_value": "有中文"}, "concept_value"),
        ({"content": "   "}, "content"),
        ({"content": "x" * 501}, "500"),
    ],
)
async def test_each_field_rejection_names_that_field(tmp_path, overrides, expected):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(service, **overrides)

    assert expected in str(excinfo.value)


@pytest.mark.asyncio
async def test_core_aspect_without_explicit_says_so(tmp_path):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(service, aspect="explicit_boundary", explicit=False)

    message = str(excinfo.value)
    assert "explicit" in message
    assert "explicit_boundary" in message


@pytest.mark.asyncio
async def test_stable_fact_without_long_term_says_so(tmp_path):
    service = _enabled(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        await _write(
            service, aspect="stable_fact", explicit=True, long_term=False
        )

    message = str(excinfo.value)
    assert "stable_fact" in message
    assert "long_term" in message


@pytest.mark.asyncio
async def test_valid_write_is_unaffected(tmp_path):
    service = _enabled(tmp_path)

    _, message = await _write(service)

    assert message


def test_tool_description_lists_both_enums():
    class _FakeToolManager:
        def __init__(self):
            self.tools = {}

        def get_tool(self, name):
            return self.tools.get(name)

        def add_tool(self, handler, *, name, description):
            class _Tool:
                def __init__(self):
                    self.description = description
                    self.parameters = {}

                    class _Meta:
                        class arg_model:
                            model_config = {}

                            @staticmethod
                            def model_rebuild(force=False):
                                return None

                            @staticmethod
                            def model_json_schema():
                                return {}

                    self.fn_metadata = _Meta()

            tool = _Tool()
            self.tools[name] = tool
            return tool

        def remove_tool(self, name):
            self.tools.pop(name, None)

    class _FakeMCP:
        def __init__(self):
            self._tool_manager = _FakeToolManager()

    mcp = _FakeMCP()
    gate = YouToolGate(mcp, lambda **_kwargs: None)
    gate.sync(True)

    description = mcp._tool_manager.get_tool("You").description
    for allowed in VALID_ASPECTS:
        assert allowed in description, allowed
    for allowed in VALID_BASES:
        assert allowed in description, allowed
