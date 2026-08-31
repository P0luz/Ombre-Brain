import pytest


@pytest.mark.parametrize(
    "tool_name",
    ["hold", "grow", "trace", "breath", "pulse", "dream", "anchor", "I", "You", "letter", "plan", "feel"],
)
def test_public_tool_contract_accepts_normal_organ_tools(tool_name):
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec

    decision = PublicToolDesignContract.default().evaluate_tool(PublicToolSpec(name=tool_name))

    assert decision.allowed is True
    assert decision.reason == "allowed normal organ tool"
    assert decision.tool_class == "normal"


@pytest.mark.parametrize(
    ("tool_name", "replacement"),
    [
        ("release", "anchor"),
        ("letter_write", "letter"),
        ("letter_read", "letter"),
        ("letter_lock_update", "letter"),
    ],
)
def test_public_tool_contract_allows_current_compatibility_public_names(tool_name, replacement):
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec

    decision = PublicToolDesignContract.default().evaluate_tool(PublicToolSpec(name=tool_name))

    assert decision.allowed is True
    assert decision.reason == "legacy-compatible public name"
    assert decision.replacement == replacement


@pytest.mark.parametrize(
    ("tool_name", "replacement"),
    [
        ("remember", "hold/grow"),
        ("touch", "trace"),
        ("resolve", "trace"),
        ("suppress", "trace"),
        ("surface", "breath"),
        ("hippocampal_recall", ""),
        ("offline_consolidate", ""),
        ("update_memory_row", ""),
    ],
)
def test_public_tool_contract_rejects_engineering_aliases_as_public_tools(tool_name, replacement):
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec

    decision = PublicToolDesignContract.default().evaluate_tool(PublicToolSpec(name=tool_name))

    assert decision.allowed is False
    assert decision.reason == "engineering name cannot be public MCP tool"
    assert decision.replacement == replacement


def test_public_tool_contract_allows_engineering_names_only_as_internal_labels():
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec, ToolExposure

    decision = PublicToolDesignContract.default().evaluate_tool(
        PublicToolSpec(name="remember", exposure=ToolExposure.INTERNAL)
    )

    assert decision.allowed is True
    assert decision.reason == "allowed internal engineering label"
    assert decision.tool_class == "internal"


@pytest.mark.parametrize(
    "tool_name",
    ["admin_erasure_request", "admin_write_tombstone", "rebuild_projection", "verify_ledger", "replay_ledger"],
)
def test_public_tool_contract_restricted_tools_require_admin_exposure(tool_name):
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec, ToolExposure

    contract = PublicToolDesignContract.default()
    public_decision = contract.evaluate_tool(PublicToolSpec(name=tool_name))
    restricted_decision = contract.evaluate_tool(
        PublicToolSpec(name=tool_name, exposure=ToolExposure.RESTRICTED, requires_admin=True)
    )

    assert public_decision.allowed is False
    assert public_decision.reason == "restricted tool requires admin exposure"
    assert restricted_decision.allowed is True
    assert restricted_decision.tool_class == "restricted"


@pytest.mark.parametrize(
    "tool_name",
    ["delete", "dump_all", "set_emotion", "decide", "update_user_profile", "force_personality"],
)
def test_public_tool_contract_rejects_forbidden_normal_tools(tool_name):
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec

    decision = PublicToolDesignContract.default().evaluate_tool(PublicToolSpec(name=tool_name))

    assert decision.allowed is False
    assert decision.reason == "forbidden normal tool"


def test_public_tool_contract_rejects_unknown_public_tool_names():
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec

    decision = PublicToolDesignContract.default().evaluate_tool(PublicToolSpec(name="memory_dump"))

    assert decision.allowed is False
    assert decision.reason == "unknown public tool"


def test_public_tool_manifest_report_is_json_safe():
    from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec, ToolExposure

    report = PublicToolDesignContract.default().evaluate_manifest(
        [
            PublicToolSpec(name="breath"),
            PublicToolSpec(name="remember"),
            PublicToolSpec(name="verify_ledger", exposure=ToolExposure.RESTRICTED, requires_admin=True),
        ]
    )
    data = report.to_dict()

    assert report.ok is False
    assert data["tool_count"] == 3
    assert data["allowed_count"] == 2
    assert data["rejected_count"] == 1
    assert data["decisions"][1]["replacement"] == "hold/grow"


def test_protocol_package_exports_public_tool_design_contract():
    from ombrebrain.protocol import (
        PublicToolDecision,
        PublicToolDesignContract,
        PublicToolReport,
        PublicToolSpec,
        ToolExposure,
    )

    assert PublicToolDesignContract.default() is not None
    assert PublicToolSpec(name="breath") is not None
    assert PublicToolDecision is not None
    assert PublicToolReport is not None
    assert ToolExposure.NORMAL.value == "normal"
