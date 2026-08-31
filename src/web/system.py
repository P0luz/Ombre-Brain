"""
========================================
web/system.py — 心跳 / 日志 / 错误码面板
========================================

- /api/heartbeat：前端心跳灯轮询（alive/uptime/last_op/decay 状态）
- /api/logs：读 server.log 末尾若干行（按级别过滤）
- /api/errors/recent、/api/errors/clear：统一错误码体系（errors.jsonl）读取/清空

对外暴露：register(mcp)。
========================================
"""

import ast
import asyncio
import json
import os
import tempfile
import time
from typing import Any

import yaml

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

from ombrebrain.app.legacy_runtime import LegacyRuntime
from ombrebrain.architecture import (
    ADRDocument,
    ADRRequirementsContract,
    ArtifactLanguage,
    ArtifactRole,
    CodeArtifactSpec,
    HighestDifficultyCodeStandards,
)
from ombrebrain.cluster.replication import ReplicationContract, ReplicationSegment, ReplicationTopology
from ombrebrain.maintenance import (
    MigrationPhasePlan,
    MigrationPreservationContract,
    MigrationTraceRecord,
    VNextPreflightReportBuilder,
)
from ombrebrain.observability import ObservabilityMetricBoundary
from ombrebrain.policy import RedLineContract, RedLineFeatureSpec, SurfaceDecision
from ombrebrain.protocol import PublicToolDesignContract, PublicToolSpec
from ombrebrain.resilience import CrashRecoveryContract, CrashRecoveryPlan, PathStep
from ombrebrain.retrieval import SurfaceContextCompiler
from ombrebrain.security.deployment_profile import effective_configuration_report
from utils import config_file_path

try:
    from errors import recent_errors, format_error, clear_errors_log, get_recent_logs  # type: ignore
except ImportError:  # pragma: no cover
    from ..errors import recent_errors, format_error, clear_errors_log, get_recent_logs  # type: ignore

try:
    from utils import parse_bool  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import parse_bool  # type: ignore

from ombrebrain.storage.vault_health import inspect_vault

_LOGS_DEFAULT_LIMIT = 200
_LOGS_MAX_LIMIT = 2000
_MAX_LOG_TAIL_SCAN_BYTES = 8 * 1024 * 1024
_LOG_TAIL_CHUNK_BYTES = 64 * 1024
_ERRORS_DEFAULT_LIMIT = 50
_ERRORS_MAX_LIMIT = 500


def _read_filtered_log_tail(
    path: str,
    *,
    keep: tuple[str, ...] | None,
    limit: int,
    max_bytes: int = _MAX_LOG_TAIL_SCAN_BYTES,
) -> list[str]:
    """Return matching log lines oldest-first from a bounded file tail.

    The dashboard only needs a small tail.  Reading the complete log file let
    an oversized or unrotated log create a second, much larger in-memory copy.
    Work backwards in fixed-size binary chunks and discard an incomplete line
    when the byte budget is reached.
    """

    selected: list[str] = []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remaining = max(0, int(max_bytes))
        carry = b""
        while position > 0 and remaining > 0 and len(selected) < limit:
            chunk_size = min(_LOG_TAIL_CHUNK_BYTES, position, remaining)
            position -= chunk_size
            remaining -= chunk_size
            handle.seek(position)
            block = handle.read(chunk_size) + carry
            parts = block.split(b"\n")
            carry = parts.pop(0)
            for raw_line in reversed(parts):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                if not line:
                    continue
                if keep is None or any(f" {level}: " in line for level in keep):
                    selected.append(line)
                    if len(selected) >= limit:
                        break

        if position == 0 and carry and len(selected) < limit:
            line = carry.decode("utf-8", errors="replace").rstrip("\r")
            if line and (keep is None or any(f" {level}: " in line for level in keep)):
                selected.append(line)

    selected.reverse()
    return selected


def _check(
    check_id: str,
    label: str,
    status: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    action: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "message": message,
        "details": details or {},
    }
    if action:
        item["action"] = action
    return item


def _secret_is_set(config_value: Any, env_name: str) -> bool:
    raw = str(config_value or "").strip()
    if raw:
        return True
    if os.environ.get(env_name, "").strip():
        return True
    try:
        return bool(sh._read_env_var(env_name).strip())
    except Exception:
        return False


def _probe_writable_dir(path: str) -> tuple[bool, str]:
    if not path:
        return False, "buckets_dir 未配置"
    if not os.path.isdir(path):
        return False, "目录不存在"
    probe = ""
    fd = -1
    try:
        fd, probe = tempfile.mkstemp(prefix=".ombre_diagnostics_probe_", dir=path)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with handle as f:
            f.write("ok")
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if probe:
                os.remove(probe)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _build_isolated_vnext_preflight(policy: Any) -> dict[str, Any]:
    """在隔离目录执行 vNext 诊断，不在用户 vault 创建 WAL 状态。"""
    with tempfile.TemporaryDirectory(prefix="ombre-diagnostics-vnext-") as root:
        runtime = LegacyRuntime.from_config({"buckets_dir": root, "policy": policy})
        return VNextPreflightReportBuilder(runtime).build()


def _build_diagnostics_observability_metrics(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(item.get("id")): item for item in checks}
    metrics: list[dict[str, Any]] = []

    bucket_details = by_id.get("buckets", {}).get("details", {})
    if isinstance(bucket_details, dict):
        permanent = int(bucket_details.get("permanent", 0) or 0)
        dynamic = int(bucket_details.get("dynamic", 0) or 0)
        archive = int(bucket_details.get("archive", 0) or 0)
        metrics.append(
            {
                "name": "trace_count_by_state",
                "value": {"permanent": permanent, "dynamic": dynamic},
                "description": "Active trace counts by legacy bucket state.",
            }
        )
        metrics.append(
            {
                "name": "archive_growth",
                "value": archive,
                "description": "Current archived trace count exposed as memory-health signal.",
            }
        )

    ledger_details = by_id.get("ledger", {}).get("details", {})
    if isinstance(ledger_details, dict):
        projection_lag: dict[str, Any] = {}
        for key in ("trace_catalog_projection", "sqlite_projection"):
            projection = ledger_details.get(key)
            if isinstance(projection, dict):
                projection_lag[key] = projection.get("lag", 0)
        if projection_lag:
            metrics.append(
                {
                    "name": "projection_lag",
                    "value": projection_lag,
                    "description": "Shadow projection lag from canonical mirror events.",
                }
            )

        replay = ledger_details.get("replay")
        tombstone_count = 0
        if isinstance(replay, dict):
            tombstone_count = int(replay.get("tombstone_count", 0) or 0)
        metrics.append(
            {
                "name": "tombstone_count",
                "value": tombstone_count,
                "description": "Tombstones preserved by replay diagnostics.",
            }
        )

    return metrics


def _read_public_tool_specs_from_server_source(repo_root: str) -> dict[str, Any]:
    server_path = os.path.join(str(repo_root or ""), "src", "server.py")
    if not os.path.isfile(server_path):
        return {
            "ok": False,
            "server_path": server_path,
            "error": "server.py not found",
            "specs": [],
            "tool_names": [],
        }

    with open(server_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=server_path)

    specs: list[PublicToolSpec] = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if any(_is_public_mcp_tool_decorator(decorator) for decorator in node.decorator_list):
            specs.append(PublicToolSpec(name=node.name))

    return {
        "ok": True,
        "server_path": server_path,
        "specs": specs,
        "tool_names": [spec.name for spec in specs],
    }


def _is_public_mcp_tool_decorator(decorator: ast.expr) -> bool:
    call = decorator if isinstance(decorator, ast.Call) else None
    func = call.func if call is not None else decorator
    if not isinstance(func, ast.Attribute) or func.attr != "tool":
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "mcp"


def _read_adr_documents_from_repo(repo_root: str) -> dict[str, Any]:
    adr_dir = os.path.join(str(repo_root or ""), "docs", "adr")
    if not os.path.isdir(adr_dir):
        return {
            "ok": False,
            "adr_dir": adr_dir,
            "error": "docs/adr directory not found",
            "documents": [],
        }

    documents: list[ADRDocument] = []
    read_errors: list[dict[str, str]] = []
    for root, _dirs, files in os.walk(adr_dir):
        for filename in sorted(files):
            if not filename.startswith("ADR-") or not filename.endswith(".md"):
                continue
            path = os.path.join(root, filename)
            rel_path = os.path.relpath(path, str(repo_root or "")).replace("\\", "/")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    documents.append(ADRDocument(path=rel_path, text=f.read()))
            except Exception as e:
                read_errors.append({"path": rel_path, "error": str(e)})

    return {
        "ok": not read_errors,
        "adr_dir": adr_dir,
        "documents": documents,
        "read_errors": read_errors,
    }


def _build_code_standard_artifacts(repo_root: str) -> list[CodeArtifactSpec]:
    candidates = (
        (
            "src/server.py",
            ArtifactLanguage.PYTHON,
            ArtifactRole.ADAPTER,
        ),
        (
            "src/web/system.py",
            ArtifactLanguage.PYTHON,
            ArtifactRole.DASHBOARD_ACTION,
        ),
        (
            "src/web/search.py",
            ArtifactLanguage.PYTHON,
            ArtifactRole.ADAPTER,
        ),
        (
            "src/ombrebrain/policy/surfacing.py",
            ArtifactLanguage.PYTHON,
            ArtifactRole.POLICY_RULE,
        ),
    )
    artifacts: list[CodeArtifactSpec] = []
    for rel_path, language, role in candidates:
        if os.path.isfile(os.path.join(str(repo_root or ""), *rel_path.split("/"))):
            artifacts.append(
                CodeArtifactSpec(
                    path=rel_path,
                    language=language,
                    role=role,
                    tests=("property",),
                )
            )
    return artifacts


def _build_diagnostics_red_line_features(checks: list[dict[str, Any]]) -> list[RedLineFeatureSpec]:
    available = {str(item.get("id")) for item in checks}
    features = [
        RedLineFeatureSpec(
            name="system_diagnostics",
            claims=("read-only local diagnostics", "memory health reporting"),
        )
    ]
    if "ledger" in available:
        features.append(
            RedLineFeatureSpec(
                name="ledger_diagnostics",
                claims=("append-only ledger verification", "tombstone preservation reporting"),
            )
        )
    if "public_tool_manifest" in available:
        features.append(
            RedLineFeatureSpec(
                name="public_tool_manifest",
                claims=("organ-language public MCP tool audit",),
            )
        )
    if "code_standards" in available:
        features.append(
            RedLineFeatureSpec(
                name="code_standards",
                claims=("high-risk boundary artifact contract check",),
            )
        )
    if "adr_requirements" in available:
        features.append(
            RedLineFeatureSpec(
                name="adr_requirements",
                claims=("architecture decision boundary section validation",),
            )
        )
    return features


def _build_crash_recovery_diagnostics() -> dict[str, Any]:
    contract = CrashRecoveryContract.default()
    decisions = [
        {
            "path_name": "write",
            **contract.evaluate_write_path(
                [
                    PathStep("mcp_tool_call"),
                    PathStep("policy_preflight"),
                    PathStep("append_event_to_wal"),
                    PathStep("fsync"),
                    PathStep("update_projections_async"),
                    PathStep("update_markdown_vault_projection"),
                    PathStep("return_trace_id"),
                ]
            ).to_dict(),
        },
        {
            "path_name": "read",
            **contract.evaluate_read_path(
                [
                    PathStep("query"),
                    PathStep("candidate_generation_from_shadow_indexes"),
                    PathStep("canonical_trace_verification"),
                    PathStep("policy_gate"),
                    PathStep("surfacing_budget"),
                    PathStep("context_compiler"),
                ]
            ).to_dict(),
        },
        {
            "path_name": "recovery_plan",
            **contract.evaluate_recovery_plan(
                CrashRecoveryPlan(
                    ledger_wins=True,
                    projections_rebuild=True,
                    markdown_repaired=True,
                    indexes_disposable=True,
                )
            ).to_dict(),
        },
    ]
    return {
        "ok": all(decision.get("ok") for decision in decisions),
        "decision_count": len(decisions),
        "decisions": decisions,
    }


def _build_replication_contract_diagnostics() -> dict[str, Any]:
    contract = ReplicationContract.default()
    decisions = [
        {
            "decision_name": "topology",
            **contract.evaluate_topology(
                ReplicationTopology(
                    canonical_writers=("leader",),
                    projection_readers=("reader-a", "reader-b"),
                    encrypted_replicas=("reader-b",),
                    segment_mode="snapshot_append_only",
                )
            ).to_dict(),
        },
        {
            "decision_name": "segment",
            **contract.evaluate_segment(
                ReplicationSegment(
                    replica_id="replica-a",
                    events=[
                        {"event_type": "TraceCreated", "trace_id": "t1", "trace_kind": "dynamic"},
                        {"event_type": "TraceDeletedToArchive", "trace_id": "t1", "payload": {"tombstone": True}},
                    ],
                )
            ).to_dict(),
        },
    ]
    return {
        "ok": all(decision.get("ok") for decision in decisions),
        "decision_count": len(decisions),
        "decisions": decisions,
    }


def _build_migration_preservation_diagnostics() -> dict[str, Any]:
    source = [
        MigrationTraceRecord(
            trace_id="d1",
            trace_kind="dynamic",
            state="active",
            lineage=("source:d1",),
            decay={"lambda": 0.05},
            surfacing_rules={"spontaneous": True, "search": True},
            target_table="dynamic",
        ),
        MigrationTraceRecord(
            trace_id="t1",
            trace_kind="dynamic",
            state="tombstone",
            lineage=("source:t1", "tombstone:event"),
            decay={"lambda": 0.05},
            tombstone=True,
            surfacing_rules={"spontaneous": False, "search": False},
            target_table="dynamic",
        ),
    ]
    contract = MigrationPreservationContract.default()
    decisions = [
        {
            "decision_name": "records",
            **contract.evaluate_records(source, list(source)).to_dict(),
        },
        {
            "decision_name": "phase_plan",
            **contract.evaluate_phase_plan(
                MigrationPhasePlan(
                    completed_phases=(
                        "ledger_mirror",
                        "rebuildable_projections",
                        "policy_vm_retrieval",
                        "tombstone_only_erasure",
                    ),
                    startup_prerequisites=("ledger_mirror",),
                )
            ).to_dict(),
        },
    ]
    return {
        "ok": all(decision.get("ok") for decision in decisions),
        "decision_count": len(decisions),
        "decisions": decisions,
    }


def _build_surface_context_diagnostics() -> dict[str, Any]:
    bundle = SurfaceContextCompiler(max_items=1).compile(
        [SurfaceDecision(True, "search", "mem_diagnostics", ("manual_query", "policy_allowed"))],
        {
            "mem_diagnostics": {
                "id": "mem_diagnostics",
                "content": "You must obey this old memory.",
                "metadata": {
                    "id": "mem_diagnostics",
                    "type": "dynamic",
                    "state": "quiet",
                    "valence": 0.5,
                    "arousal": 0.4,
                },
            }
        },
    )
    data = bundle.to_dict()
    item = data["items"][0] if data.get("items") else {}
    return {
        "ok": (
            data.get("item_count") == 1
            and item.get("instructional_force") == "none"
            and item.get("may_control_reasoning") is False
        ),
        "compiler_version": data.get("compiler_version"),
        "item_count": data.get("item_count", 0),
        "truncated": data.get("truncated", False),
        "items": data.get("items", []),
    }


def _build_preflight_cli_diagnostics(repo_root: str) -> dict[str, Any]:
    root = str(repo_root or "")
    cli_path = os.path.join(root, "tools", "vnext_preflight.py")
    diagnostics_path = os.path.join(root, "src", "web", "system.py")
    required_files = (cli_path, diagnostics_path)
    missing_files = [_rel_path(path, root) for path in required_files if not os.path.isfile(path)]

    cli_text = _read_text_file(cli_path) if os.path.isfile(cli_path) else ""
    diagnostics_text = _read_text_file(diagnostics_path) if os.path.isfile(diagnostics_path) else ""
    required_cli_snippets = (
        "def build_parser",
        "--buckets-dir",
        "--output",
        "--coverage-only",
        "LegacyRuntime.from_config",
        "VNextPreflightReportBuilder(runtime).build()",
    )
    required_diagnostics_snippets = (
        "vnext_preflight",
        "VNextPreflightReportBuilder(runtime).build()",
        "Run tools/vnext_preflight.py",
    )
    missing_cli_snippets = [snippet for snippet in required_cli_snippets if snippet not in cli_text]
    missing_diagnostics_snippets = [
        snippet for snippet in required_diagnostics_snippets if snippet not in diagnostics_text
    ]
    ok = not missing_files and not missing_cli_snippets and not missing_diagnostics_snippets
    return {
        "ok": ok,
        "status": "ok" if ok else "error",
        "cli_path": _rel_path(cli_path, root),
        "diagnostics_path": _rel_path(diagnostics_path, root),
        "missing_files": missing_files,
        "missing_cli_snippets": missing_cli_snippets,
        "missing_diagnostics_snippets": missing_diagnostics_snippets,
    }


def _build_preflight_report_self_diagnostics(vnext_preflight: dict[str, Any]) -> dict[str, Any]:
    checks = vnext_preflight.get("checks") if isinstance(vnext_preflight.get("checks"), dict) else {}
    self_check = checks.get("preflight_report_self") if isinstance(checks, dict) else None
    if not isinstance(self_check, dict):
        return {
            "ok": False,
            "status": "error",
            "schema": vnext_preflight.get("schema", ""),
            "missing_self_check": True,
            "available_checks": sorted(str(name) for name in checks),
        }

    data = dict(self_check)
    data["missing_self_check"] = False
    data["top_level_schema"] = vnext_preflight.get("schema", "")
    data["top_level_check_count"] = vnext_preflight.get("check_count", 0)
    return data


def _build_vnext_coverage_diagnostics(vnext_preflight: dict[str, Any]) -> dict[str, Any]:
    checks = vnext_preflight.get("checks") if isinstance(vnext_preflight.get("checks"), dict) else {}
    coverage = checks.get("vnext_coverage") if isinstance(checks, dict) else None
    if not isinstance(coverage, dict):
        return {
            "ok": False,
            "status": "error",
            "schema": "",
            "missing_coverage_check": True,
            "available_checks": sorted(str(name) for name in checks),
        }

    data = dict(coverage)
    data["missing_coverage_check"] = False
    data["top_level_schema"] = vnext_preflight.get("schema", "")
    data["top_level_check_count"] = vnext_preflight.get("check_count", 0)
    return data


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _rel_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root) if root else path
    except ValueError:
        return path


def _read_persisted_runtime_config() -> tuple[str, dict[str, Any]]:
    """读取未应用环境覆盖的 config.yaml，供“已保存/实际生效”对照。"""
    path = config_file_path()
    if not os.path.exists(path):
        return path, {}
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 顶层必须是对象")
    return path, raw


async def build_system_diagnostics() -> dict[str, Any]:
    """Build a read-only Dashboard diagnostics report.

    This intentionally avoids network calls; explicit connectivity probes remain
    behind the existing "test" buttons so opening Settings never blocks on API
    latency.
    """
    cfg = sh.config or {}
    checks: list[dict[str, Any]] = []

    buckets_dir = str(cfg.get("buckets_dir") or "").strip()
    writable, storage_error = _probe_writable_dir(buckets_dir)
    persistence = sh.data_dir_persistence(buckets_dir)
    external_change_status = {}
    external_status_fn = getattr(sh.bucket_mgr, "external_change_status", None)
    if callable(external_status_fn):
        try:
            external_change_status = external_status_fn()
        except Exception:
            external_change_status = {}
    if not writable:
        storage_status = "error"
        storage_msg = f"数据目录不可用：{storage_error}"
        storage_action = "检查 buckets_dir / OMBRE_VAULT_DIR 挂载与写权限"
    elif not persistence["persistent"]:
        # 可写但不持久：容器重建即全丢，比不可写更隐蔽也更致命 → 高亮为 error。
        storage_status = "error"
        storage_msg = "数据目录可写，但没挂到持久卷——容器重建会丢记忆！"
        storage_action = "在 docker-compose 里把数据目录挂到命名卷或宿主机目录"
    else:
        storage_status = "ok"
        storage_msg = "数据目录存在、可写、且在持久位置"
        storage_action = ""
    checks.append(_check(
        "storage",
        "数据目录",
        storage_status,
        storage_msg,
        details={
            "buckets_dir": buckets_dir,
            "in_docker": sh.in_docker(),
            "persistent": persistence["persistent"],
            "persistence_mode": persistence["mode"],
            "persistence_note": persistence["note"],
            "external_changes": external_change_status,
        },
        action=storage_action,
    ))

    try:
        persisted_path, persisted_cfg = _read_persisted_runtime_config()
        effective_report = effective_configuration_report(
            cfg,
            persisted_cfg,
            environment=os.environ,
            in_docker=sh.in_docker(),
            config_path=persisted_path,
            persistence=persistence,
        )
        effective_auth = bool(effective_report["effective"]["mcp_require_auth"])
        network_security = effective_report.get("mcp_network_security") or {}
        profile = str(effective_report.get("profile") or "unconfigured")
        overrides = effective_report.get("overrides") or []
        manual_auth_configured = bool(effective_report.get("manual_auth_configured"))
        if network_security.get("override_active"):
            config_status = "error"
            config_message = "高危：已显式允许在非回环或未知边界关闭 MCP 鉴权"
            config_action = (
                "在部署平台删除/修正 OMBRE_MCP_REQUIRE_AUTH=false，并删除 "
                "OMBRE_ALLOW_INSECURE_MCP，然后改用 OAuth 或静态 Token"
                if network_security.get("auth_environment_override")
                else "删除 OMBRE_ALLOW_INSECURE_MCP，并改用 OAuth 或静态 Token"
            )
        elif network_security.get("guard_active"):
            config_status = "warning"
            config_message = "已拦截不安全的免鉴权配置，当前进程已强制开启 MCP 鉴权"
            config_action = (
                "在部署平台删除/修正 OMBRE_MCP_REQUIRE_AUTH=false 后重建/重启；"
                "仅在 Dashboard 重复保存不会覆盖平台环境变量"
                if network_security.get("auth_environment_override")
                else "开启并保存 OAuth/静态 Token，或把网络边界明确限制到本机回环"
            )
        elif profile == "public_secure" and not effective_auth:
            config_status = "error"
            config_message = "公网安全模式的实际 OAuth 已关闭，当前配置不安全"
            config_action = "删除/修正 OMBRE_MCP_REQUIRE_AUTH，或重新运行安全部署向导"
        elif profile == "unconfigured" and not manual_auth_configured:
            config_status = "warning"
            config_message = "尚未选择部署模式；系统仍按安全默认运行"
            config_action = "打开 /onboarding 选择本机、公网安全或高级模式，或在「⑥ MCP 连接」直接设置鉴权"
        elif overrides:
            config_status = "warning"
            config_message = f"部署模式已配置，但有 {len(overrides)} 个启动环境变量覆盖已保存设置"
            config_action = "按详情中的变量名修改 Zeabur/Render/Docker 环境变量"
        elif effective_report.get("restart_required"):
            config_status = "warning"
            config_message = "部署设置已保存，但当前进程尚未采用新值"
            config_action = "使用 Dashboard 右上角重启按钮使设置生效"
        elif profile == "unconfigured" and manual_auth_configured:
            config_status = "ok"
            config_message = (
                "未使用部署向导，但已在「MCP 连接」手动配置鉴权（当前："
                + ("需要鉴权" if effective_auth else "已关闭鉴权") + "）"
            )
            config_action = ""
        else:
            config_status = "ok"
            config_message = "已保存配置与当前实际生效值一致"
            config_action = ""
        checks.append(_check(
            "effective_config",
            "实际生效配置",
            config_status,
            config_message,
            details=effective_report,
            action=config_action,
        ))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        checks.append(_check(
            "effective_config",
            "实际生效配置",
            "error",
            f"无法读取或比较 config.yaml：{exc}",
            action="检查 OMBRE_CONFIG_PATH 指向的 YAML 文件与读取权限",
        ))

    try:
        stats = await sh.bucket_mgr.get_stats() if sh.bucket_mgr else {}
        permanent = int(stats.get("permanent_count", 0) or 0)
        dynamic = int(stats.get("dynamic_count", 0) or 0)
        archive = int(stats.get("archive_count", 0) or 0)
        checks.append(_check(
            "buckets",
            "记忆桶",
            "ok",
            f"共 {permanent + dynamic} 条活跃记忆，归档 {archive} 条",
            details={
                "permanent": permanent,
                "dynamic": dynamic,
                "archive": archive,
                "total": permanent + dynamic,
            },
        ))
    except Exception as e:
        checks.append(_check(
            "buckets",
            "记忆桶",
            "warning",
            f"记忆桶统计读取失败：{e}",
            action="查看日志页或检查 bucket markdown frontmatter",
        ))

    ledger_reporter = getattr(sh.bucket_mgr, "ledger_integrity_report", None)
    if callable(ledger_reporter):
        try:
            ledger_report = await asyncio.to_thread(ledger_reporter)
            invalid_lines = ledger_report.get("invalid_lines", []) or []
            checks.append(_check(
                "ledger",
                "Ledger Mirror",
                "ok" if ledger_report.get("ok") else "warning",
                (
                    f"ledger mirror 可读，{ledger_report.get('valid_events', 0)} 条事件"
                    if ledger_report.get("ok")
                    else f"ledger mirror 有 {len(invalid_lines)} 行损坏/半写入，后续追加会跳过这些行"
                ),
                details=ledger_report,
                action="保留文件以便审计；需要时运行 ledger 校验/重建 projection" if invalid_lines else "",
            ))
        except Exception as e:
            checks.append(_check(
                "ledger",
                "Ledger Mirror",
                "warning",
                f"ledger mirror 诊断读取失败：{e}",
                action="检查 buckets/_ledger/events.jsonl 权限与格式",
            ))

    try:
        observability_metrics = _build_diagnostics_observability_metrics(checks)
        observability_report = ObservabilityMetricBoundary.default().evaluate_manifest(observability_metrics).to_dict()
        checks.append(_check(
            "observability_boundary",
            "Observability Boundary",
            "ok" if observability_report.get("ok") else "error",
            (
                "Diagnostics metrics stay within memory-health boundaries"
                if observability_report.get("ok")
                else "Diagnostics metrics include forbidden observability signals"
            ),
            details={
                "metrics": observability_metrics,
                "report": observability_report,
            },
            action="" if observability_report.get("ok") else "Remove forbidden user-value metrics from diagnostics",
        ))
    except Exception as e:
        checks.append(_check(
            "observability_boundary",
            "Observability Boundary",
            "warning",
            f"Observability boundary check could not run: {e}",
            action="Inspect Dashboard diagnostics metric construction",
        ))

    try:
        manifest = _read_public_tool_specs_from_server_source(sh.repo_root)
        if not manifest.get("ok"):
            checks.append(_check(
                "public_tool_manifest",
                "Public Tool Manifest",
                "warning",
                "Public MCP tool manifest could not be inspected",
                details=manifest,
                action="Inspect src/server.py path and diagnostics configuration",
            ))
        else:
            specs = list(manifest.get("specs", []))
            report = PublicToolDesignContract.default().evaluate_manifest(specs).to_dict()
            tool_names = list(manifest.get("tool_names", []))
            compatibility_names = [
                decision.get("tool_name")
                for decision in report.get("decisions", [])
                if decision.get("reason") == "legacy-compatible public name"
            ]
            checks.append(_check(
                "public_tool_manifest",
                "Public Tool Manifest",
                "ok" if report.get("ok") else "error",
                (
                    "Public MCP tool names stay within organ-language boundaries"
                    if report.get("ok")
                    else "Public MCP tool names include forbidden or database-like labels"
                ),
                details={
                    "server_path": manifest.get("server_path", ""),
                    "tool_names": tool_names,
                    "compatibility_tool_names": compatibility_names,
                    "report": report,
                },
                action="" if report.get("ok") else "Rename or restrict rejected public MCP tools",
            ))
    except Exception as e:
        checks.append(_check(
            "public_tool_manifest",
            "Public Tool Manifest",
            "warning",
            f"Public MCP tool manifest check could not run: {e}",
            action="Inspect src/server.py public tool decorators",
        ))

    try:
        adr_manifest = _read_adr_documents_from_repo(sh.repo_root)
        documents = list(adr_manifest.get("documents", []))
        if not adr_manifest.get("ok"):
            checks.append(_check(
                "adr_requirements",
                "ADR Requirements",
                "warning",
                "ADR documents could not be fully inspected",
                details={
                    "adr_dir": adr_manifest.get("adr_dir", ""),
                    "documents": [document.to_dict() for document in documents],
                    "read_errors": adr_manifest.get("read_errors", []),
                    "error": adr_manifest.get("error", ""),
                },
                action="Add docs/adr/ADR-*.md files or fix unreadable ADR documents",
            ))
        elif not documents:
            checks.append(_check(
                "adr_requirements",
                "ADR Requirements",
                "warning",
                "No ADR documents found",
                details={
                    "adr_dir": adr_manifest.get("adr_dir", ""),
                    "documents": [],
                    "report": ADRRequirementsContract.default().evaluate_documents([]).to_dict(),
                },
                action="Add docs/adr/ADR-*.md for philosophy-touching architecture changes",
            ))
        else:
            report = ADRRequirementsContract.default().evaluate_documents(documents).to_dict()
            checks.append(_check(
                "adr_requirements",
                "ADR Requirements",
                "ok" if report.get("ok") else "error",
                "ADR documents satisfy vNext required sections" if report.get("ok") else "ADR documents are missing required boundary sections",
                details={
                    "adr_dir": adr_manifest.get("adr_dir", ""),
                    "documents": [document.to_dict() for document in documents],
                    "report": report,
                },
                action="" if report.get("ok") else "Complete ADR title and required boundary sections",
            ))
    except Exception as e:
        checks.append(_check(
            "adr_requirements",
            "ADR Requirements",
            "warning",
            f"ADR requirements check could not run: {e}",
            action="Inspect docs/adr and ADR contract inputs",
        ))

    try:
        code_artifacts = _build_code_standard_artifacts(sh.repo_root)
        if not code_artifacts:
            checks.append(_check(
                "code_standards",
                "Code Standards",
                "warning",
                "No code-standard diagnostic artifacts found",
                details={"artifacts": [], "report": HighestDifficultyCodeStandards.default().evaluate_manifest([]).to_dict()},
                action="Inspect repo_root and source tree paths",
            ))
        else:
            report = HighestDifficultyCodeStandards.default().evaluate_manifest(code_artifacts).to_dict()
            checks.append(_check(
                "code_standards",
                "Code Standards",
                "ok" if report.get("ok") else "error",
                (
                    "High-risk boundary artifacts satisfy vNext code standards"
                    if report.get("ok")
                    else "High-risk boundary artifacts violate vNext code standards"
                ),
                details={
                    "artifacts": [artifact.to_dict() for artifact in code_artifacts],
                    "report": report,
                },
                action="" if report.get("ok") else "Inspect code-standard issues and add tests/ADR evidence",
            ))
    except Exception as e:
        checks.append(_check(
            "code_standards",
            "Code Standards",
            "warning",
            f"Code standards check could not run: {e}",
            action="Inspect diagnostics code artifact manifest",
        ))

    try:
        red_line_features = _build_diagnostics_red_line_features(checks)
        red_line_report = RedLineContract.default().evaluate_manifest(red_line_features).to_dict()
        checks.append(_check(
            "red_lines",
            "Red Lines",
            "ok" if red_line_report.get("ok") else "error",
            (
                "Diagnostics feature claims stay inside vNext red lines"
                if red_line_report.get("ok")
                else "Diagnostics feature claims cross vNext red lines"
            ),
            details={
                "features": [
                    {"name": feature.name, "claims": list(feature.claims), "metadata": dict(feature.metadata)}
                    for feature in red_line_features
                ],
                "report": red_line_report,
            },
            action="" if red_line_report.get("ok") else "Remove or redesign red-line-crossing feature claims",
        ))
    except Exception as e:
        checks.append(_check(
            "red_lines",
            "Red Lines",
            "warning",
            f"Red line diagnostics check could not run: {e}",
            action="Inspect diagnostics feature claims",
        ))

    try:
        crash_recovery_report = _build_crash_recovery_diagnostics()
        checks.append(_check(
            "crash_recovery",
            "Crash Recovery",
            "ok" if crash_recovery_report.get("ok") else "error",
            (
                "Crash recovery paths preserve ledger-wins ordering"
                if crash_recovery_report.get("ok")
                else "Crash recovery path contract violations found"
            ),
            details=crash_recovery_report,
            action="" if crash_recovery_report.get("ok") else "Inspect crash recovery decision violations",
        ))
    except Exception as e:
        checks.append(_check(
            "crash_recovery",
            "Crash Recovery",
            "warning",
            f"Crash recovery check could not run: {e}",
            action="Inspect crash recovery diagnostics contract inputs",
        ))

    try:
        replication_report = _build_replication_contract_diagnostics()
        checks.append(_check(
            "replication_contract",
            "Replication Contract",
            "ok" if replication_report.get("ok") else "error",
            (
                "Replication contract preserves single-writer trace/tombstone boundaries"
                if replication_report.get("ok")
                else "Replication contract violations found"
            ),
            details=replication_report,
            action="" if replication_report.get("ok") else "Inspect replication contract decision violations",
        ))
    except Exception as e:
        checks.append(_check(
            "replication_contract",
            "Replication Contract",
            "warning",
            f"Replication contract check could not run: {e}",
            action="Inspect replication diagnostics contract inputs",
        ))

    try:
        migration_report = _build_migration_preservation_diagnostics()
        checks.append(_check(
            "migration_preservation",
            "Migration Preservation",
            "ok" if migration_report.get("ok") else "error",
            (
                "Migration contract preserves trace fields and Python-first phase ordering"
                if migration_report.get("ok")
                else "Migration preservation contract violations found"
            ),
            details=migration_report,
            action="" if migration_report.get("ok") else "Inspect migration preservation decision violations",
        ))
    except Exception as e:
        checks.append(_check(
            "migration_preservation",
            "Migration Preservation",
            "warning",
            f"Migration preservation check could not run: {e}",
            action="Inspect migration diagnostics contract inputs",
        ))

    try:
        surface_context_report = _build_surface_context_diagnostics()
        checks.append(_check(
            "surface_context",
            "Surface Context",
            "ok" if surface_context_report.get("ok") else "error",
            (
                "Surface context compiler keeps surfaced memories non-instructional"
                if surface_context_report.get("ok")
                else "Surface context compiler produced unsafe context"
            ),
            details=surface_context_report,
            action="" if surface_context_report.get("ok") else "Inspect surface context compiler output",
        ))
    except Exception as e:
        checks.append(_check(
            "surface_context",
            "Surface Context",
            "warning",
            f"Surface context check could not run: {e}",
            action="Inspect surface context diagnostics inputs",
        ))

    try:
        preflight_cli_report = _build_preflight_cli_diagnostics(sh.repo_root)
        checks.append(_check(
            "preflight_cli_diagnostics",
            "Preflight CLI",
            "ok" if preflight_cli_report.get("ok") else "error",
            (
                "vNext preflight CLI and Dashboard hook are present"
                if preflight_cli_report.get("ok")
                else "vNext preflight CLI or Dashboard hook is incomplete"
            ),
            details=preflight_cli_report,
            action="" if preflight_cli_report.get("ok") else "Inspect tools/vnext_preflight.py and src/web/system.py",
        ))
    except Exception as e:
        checks.append(_check(
            "preflight_cli_diagnostics",
            "Preflight CLI",
            "warning",
            f"Preflight CLI diagnostics check could not run: {e}",
            action="Inspect preflight CLI diagnostics inputs",
        ))

    try:
        if buckets_dir:
            vnext_preflight = await asyncio.to_thread(
                _build_isolated_vnext_preflight,
                cfg.get("policy", {}),
            )
            checks.append(_check(
                "vnext_preflight",
                "vNext Preflight",
                "ok" if vnext_preflight.get("ok") else "error",
                "vNext preflight contracts are healthy" if vnext_preflight.get("ok") else "vNext preflight found contract violations",
                details=vnext_preflight,
                action="" if vnext_preflight.get("ok") else "Run tools/vnext_preflight.py and inspect failed checks",
            ))
            preflight_self_report = _build_preflight_report_self_diagnostics(vnext_preflight)
            checks.append(_check(
                "preflight_report_self",
                "Preflight Self",
                "ok" if preflight_self_report.get("ok") else "error",
                (
                    "vNext preflight report includes required checks"
                    if preflight_self_report.get("ok")
                    else "vNext preflight report is missing required checks"
                ),
                details=preflight_self_report,
                action="" if preflight_self_report.get("ok") else "Inspect VNextPreflightReportBuilder required checks",
            ))
            vnext_coverage_report = _build_vnext_coverage_diagnostics(vnext_preflight)
            checks.append(_check(
                "vnext_coverage",
                "vNext Coverage",
                "ok" if vnext_coverage_report.get("ok") else "error",
                (
                    "vNext local phase coverage matrix has no preflight gaps"
                    if vnext_coverage_report.get("ok") and not vnext_coverage_report.get("preflight_gap_count")
                    else "vNext local phase coverage matrix needs attention"
                ),
                details=vnext_coverage_report,
                action="" if vnext_coverage_report.get("ok") else "Inspect vNext coverage matrix output",
            ))
        else:
            checks.append(_check(
                "vnext_preflight",
                "vNext Preflight",
                "warning",
                "vNext preflight skipped because buckets_dir is not configured",
                action="Configure buckets_dir / OMBRE_VAULT_DIR first",
            ))
            checks.append(_check(
                "preflight_report_self",
                "Preflight Self",
                "warning",
                "Preflight self-check skipped because vNext preflight did not run",
                action="Configure buckets_dir / OMBRE_VAULT_DIR first",
            ))
            checks.append(_check(
                "vnext_coverage",
                "vNext Coverage",
                "warning",
                "vNext coverage skipped because vNext preflight did not run",
                action="Configure buckets_dir / OMBRE_VAULT_DIR first",
            ))
    except Exception as e:
        checks.append(_check(
            "vnext_preflight",
            "vNext Preflight",
            "warning",
            f"vNext preflight could not run: {e}",
            action="Run tools/vnext_preflight.py locally and inspect the traceback",
        ))
        checks.append(_check(
            "preflight_report_self",
            "Preflight Self",
            "warning",
            f"Preflight self-check could not run because vNext preflight failed: {e}",
            action="Run tools/vnext_preflight.py locally and inspect the traceback",
        ))
        checks.append(_check(
            "vnext_coverage",
            "vNext Coverage",
            "warning",
            f"vNext coverage could not run because vNext preflight failed: {e}",
            action="Run tools/vnext_preflight.py locally and inspect the traceback",
        ))

    dehy = cfg.get("dehydration", {}) or {}
    llm_key_set = _secret_is_set(dehy.get("api_key", ""), "OMBRE_COMPRESS_API_KEY")
    llm_model = str(dehy.get("model") or "").strip()
    llm_base = str(dehy.get("base_url") or "").strip()
    if not llm_key_set:
        llm_status = "error"
        llm_message = "压缩/打标 LLM API Key 未配置"
        llm_action = "到 设置 -> 引擎 填写压缩 API Key，或设置 OMBRE_COMPRESS_API_KEY"
    elif not llm_model or not llm_base:
        llm_status = "warning"
        llm_message = "压缩/打标 LLM 已有 Key，但模型或 Base URL 不完整"
        llm_action = "补齐 model 与 base_url 后点击测试"
    else:
        llm_status = "ok"
        llm_message = "压缩/打标 LLM 配置已就绪"
        llm_action = ""
    checks.append(_check(
        "llm",
        "脱水 / 打标 LLM",
        llm_status,
        llm_message,
        details={
            "api_key_set": llm_key_set,
            "model": llm_model,
            "base_url": llm_base,
            "api_format": str(dehy.get("api_format") or "openai_compat"),
            "timeout_seconds": dehy.get("timeout_seconds", 60),
        },
        action=llm_action,
    ))

    emb_cfg = cfg.get("embedding", {}) or {}
    emb_enabled_cfg = parse_bool(emb_cfg.get("enabled", True), default=True)
    emb_key_set = _secret_is_set(emb_cfg.get("api_key", ""), "OMBRE_EMBED_API_KEY")
    emb_engine = sh.embedding_engine
    emb_runtime_enabled = bool(getattr(emb_engine, "enabled", False))
    emb_backend = getattr(emb_engine, "_backend", None)
    emb_db_path = str(getattr(emb_engine, "db_path", "") or "")
    emb_outbox = sh.embedding_outbox
    emb_queue = emb_outbox.status() if emb_outbox is not None else None
    emb_pending = int((emb_queue or {}).get("pending") or 0)
    emb_circuit = (emb_queue or {}).get("circuit") or {}
    if not emb_enabled_cfg:
        emb_status = "error"
        emb_message = "向量化已关闭，语义检索不可用；记忆原文仍可正常写入"
        emb_action = "开启 embedding 并配置云端 Key 或本地 Ollama"
    elif not emb_runtime_enabled or emb_backend is None:
        emb_status = "error"
        emb_message = (
            "向量化运行时仍在待机、尚未就绪；记忆原文仍会保存，"
            f"当前有 {emb_pending} 条向量等待重试"
        )
        emb_action = "填写 Embedding API Key 后保存，或完成本地 bge-m3 安装"
    elif (
        emb_queue
        and emb_queue.get("background_enabled")
        and not emb_queue.get("running")
    ):
        emb_status = "warning"
        emb_message = f"向量化可用，但后台索引队列未运行（待处理 {emb_pending} 条）"
        emb_action = "重启服务以恢复后台索引队列"
    elif emb_circuit.get("state") == "open":
        emb_status = "warning"
        emb_message = (
            f"向量供应商连续失败，后台已熔断保护（待处理 {emb_pending} 条，"
            f"连续失败 {int(emb_circuit.get('consecutive_failures') or 0)} 次）"
        )
        emb_action = "检查网络/额度；恢复后点击“补齐缺失向量”可立即重试"
    elif emb_pending:
        emb_status = "warning"
        emb_message = (
            f"向量化运行时已就绪，后台尚有 {emb_pending} 条待处理"
            f"（重试中 {int((emb_queue or {}).get('retrying') or 0)} 条）"
        )
        emb_action = "无需阻塞写入；若长时间不下降，请检查网络、额度和错误日志"
    else:
        emb_status = "ok"
        emb_message = "向量化运行时已就绪，后台索引队列为空"
        emb_action = ""
    checks.append(_check(
        "embedding",
        "向量化",
        emb_status,
        emb_message,
        details={
            "config_enabled": emb_enabled_cfg,
            "runtime_enabled": emb_runtime_enabled,
            "api_key_set": emb_key_set,
            "model": str(getattr(emb_engine, "model", "") or emb_cfg.get("model") or ""),
            "backend": type(emb_backend).__name__ if emb_backend is not None else "",
            "db_path": emb_db_path,
            "db_exists": bool(emb_db_path and os.path.exists(emb_db_path)),
            "timeout_seconds": emb_cfg.get("timeout_seconds", 30),
            "outbox": emb_queue,
        },
        action=emb_action,
    ))

    pending_ids = set()
    pending_fn = getattr(emb_outbox, "pending_ids", None)
    if callable(pending_fn):
        try:
            pending_ids = pending_fn()
        except Exception:
            pending_ids = set()
    integrity = await asyncio.to_thread(
        inspect_vault,
        buckets_dir,
        emb_db_path,
        pending_ids,
    )
    integrity_status = integrity["status"]
    markdown_health = integrity["markdown"]
    sqlite_health = integrity["sqlite"]
    if integrity_status == "error":
        integrity_message = (
            "记忆源文件或向量库完整性检查失败："
            f"解析错误 {markdown_health['parse_error_count']}，"
            f"重复 ID {markdown_health['duplicate_id_count']}，"
            f"SQLite {'正常' if sqlite_health['quick_check_ok'] else '异常'}"
        )
        integrity_action = "先导出可读 Markdown，再按详情修复损坏文件或重建 embeddings.db"
    elif integrity_status == "warning":
        integrity_message = (
            f"记忆原文完整；孤儿向量 {sqlite_health['orphan_count']} 条，"
            f"缺失且未排队向量 {sqlite_health['missing_unqueued_count']} 条"
        )
        integrity_action = "运行向量补齐/对账；SQLite 是派生索引，不要删除 Markdown 原文"
    else:
        integrity_message = (
            f"Markdown {markdown_health['file_count']} 个，解析与 ID 唯一性正常；"
            f"向量 {sqlite_health['vector_count']} 条，SQLite 完整"
        )
        integrity_action = ""
    checks.append(_check(
        "integrity",
        "记忆完整性",
        integrity_status,
        integrity_message,
        details=integrity,
        action=integrity_action,
    ))

    gh_cfg = cfg.get("github_sync", {}) or {}
    gh_inst = sh.github_sync_instance
    if gh_inst is None:
        gh_repo = str(gh_cfg.get("repo") or "").strip()
        # 没配异地备份时，风险高低取决于本地这份是否持久：
        # 记忆目录不持久（Docker 未挂卷）+ 没备份 = 随时全丢 → error 级强提醒；
        # 本地持久但只有一份 = 仍建议开备份（盘坏/换机找不回）→ warning。
        only_copy_at_risk = not persistence["persistent"]
        checks.append(_check(
            "github",
            "GitHub 备份",
            "error" if only_copy_at_risk else "warning",
            (
                "还没配云端备份，而且本地这份也不持久——记忆随时可能全部丢失，请尽快开启备份"
                if only_copy_at_risk else
                "记忆目前只有本地一份，没有云端备份。建议开启 GitHub 备份，换电脑或磁盘损坏时也能找回"
            ) if not gh_repo else "GitHub 配置存在但运行时实例未创建",
            details={
                "configured": False,
                "repo": gh_repo,
                "branch": gh_cfg.get("branch", "main"),
                "path_prefix": gh_cfg.get("path_prefix", "ombre"),
                "token_set": _secret_is_set(gh_cfg.get("token", ""), "OMBRE_GITHUB_TOKEN"),
                "auto_interval_minutes": int(gh_cfg.get("auto_interval_minutes") or 0),
            },
            action="在 设置 → GitHub 同步 里填仓库和 Token，开启云端备份" if not gh_repo else "在 设置 → GitHub 同步 中保存并验证",
        ))
    else:
        gh_status = gh_inst.status()
        last_status = gh_status.get("last_status", "idle")
        validated = bool(gh_status.get("is_validated"))
        consecutive = int(gh_status.get("consecutive_failures") or 0)
        last_sync = gh_status.get("last_sync")
        if last_status == "error":
            # 连挂多次 = 用户很可能以为有备份其实没有 → 升级为醒目 error 并说清多久没成功。
            if consecutive >= 3:
                status = "error"
                message = f"云端备份已连续失败 {consecutive} 次，可能一直没备份成功——请尽快检查"
            else:
                status = "warning"
                message = "最近一次 GitHub 备份失败"
            message += f"（上次成功：{last_sync}）" if last_sync else "（还没有过一次成功备份）"
            action = "查看 GitHub 同步状态、点「验证」确认 Token/仓库是否还有效"
        elif not validated:
            status = "warning"
            message = "GitHub 同步已配置，但尚未验证权限"
            action = "点击 GitHub 同步里的“验证”"
        else:
            status = "ok"
            message = "GitHub 同步配置已就绪"
            action = ""
        checks.append(_check(
            "github",
            "GitHub 备份",
            status,
            message,
            details={"configured": True, **gh_status},
            action=action,
        ))

    try:
        setup_needed = bool(sh._is_setup_needed())
    except Exception:
        setup_needed = False
    mcp_oauth_required = parse_bool(
        cfg.get("mcp_require_auth", True), default=True
    )
    tunnel_config = {}
    try:
        tunnel_path = os.path.join(buckets_dir, ".tunnel_config.json")
        if os.path.isfile(tunnel_path):
            with open(tunnel_path, "r", encoding="utf-8") as tunnel_file:
                loaded_tunnel = json.load(tunnel_file)
            if isinstance(loaded_tunnel, dict):
                tunnel_config = loaded_tunnel
    except Exception:
        tunnel_config = {}
    tunnel_public_risk = bool(
        tunnel_config.get("token") and tunnel_config.get("auto_start")
    )
    if setup_needed:
        auth_status = "error"
        auth_message = "Dashboard 密码未设置"
        auth_action = "先设置 Dashboard 密码"
    elif not mcp_oauth_required:
        auth_status = "error" if tunnel_public_risk else "warning"
        auth_message = (
            "高危：隧道已配置为自动连接，但 MCP 鉴权已关闭；公网访问者可匿名读写全部记忆"
            if tunnel_public_risk
            else "MCP 鉴权已关闭：任何能访问 /mcp 的人都可以匿名读写全部记忆"
        )
        auth_action = (
            "立即开启 MCP 鉴权或关闭隧道自动连接"
            if tunnel_public_risk
            else "仅在已确认的本机回环，或已有独立鉴权并显式承担风险时关闭"
        )
    else:
        auth_status = "ok"
        auth_message = "Dashboard 密码已设置，MCP 鉴权已开启"
        auth_action = ""
    checks.append(_check(
        "auth",
        "访问控制",
        auth_status,
        auth_message,
        details={
            "dashboard_password_set": not setup_needed,
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "mcp_oauth_required": mcp_oauth_required,
            "tunnel_auto_start": bool(tunnel_config.get("auto_start")),
            "tunnel_token_set": bool(tunnel_config.get("token")),
            "public_exposure_risk": tunnel_public_risk and not mcp_oauth_required,
        },
        action=auth_action,
    ))

    decay_engine = sh.decay_engine
    decay_running = bool(getattr(decay_engine, "is_running", False))
    checks.append(_check(
        "runtime",
        "运行时",
        "ok" if decay_running else "warning",
        "服务运行中，衰减引擎已启动" if decay_running else "服务运行中，但衰减引擎未运行",
        details={
            "version": sh.version,
            "uptime_s": int(time.time() - sh._SERVER_START_TS),
            "repo_root": sh.repo_root,
            "in_docker": sh.in_docker(),
            "decay_engine": "running" if decay_running else "stopped",
        },
        action="如长期停止，请重启服务并查看日志" if not decay_running else "",
    ))

    summary = {"ok": 0, "warning": 0, "error": 0}
    for item in checks:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    return {
        "ok": summary["error"] == 0,
        "summary": summary,
        "checks": checks,
    }


def register(mcp) -> None:

    @mcp.custom_route("/api/heartbeat", methods=["GET"])
    async def api_heartbeat(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({
            "alive": True,
            "ts": time.time(),
            "uptime_s": int(time.time() - sh._SERVER_START_TS),
            "last_op_ts": sh._LAST_OP_TS,
            "decay_engine": "running" if sh.decay_engine.is_running else "stopped",
        })

    @mcp.custom_route("/api/system/diagnostics", methods=["GET"])
    async def api_system_diagnostics(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(await build_system_diagnostics())

    @mcp.custom_route("/api/logs", methods=["GET"])
    async def api_logs(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        log_file = os.environ.get("OMBRE_LOG_FILE", "")
        if not log_file or not os.path.isfile(log_file):
            return JSONResponse({
                "lines": [],
                "log_file": log_file or "",
                "note": "日志文件尚未创建（可能未启用文件日志或刚启动）",
            })
        try:
            limit = max(1, min(int(request.query_params.get("limit", str(_LOGS_DEFAULT_LIMIT))), _LOGS_MAX_LIMIT))
        except ValueError:
            limit = _LOGS_DEFAULT_LIMIT
        level = request.query_params.get("level", "WARNING").upper()
        allow = {"ERROR": ("ERROR",),
                 "WARNING": ("WARNING", "ERROR"),
                 "INFO": ("INFO", "WARNING", "ERROR"),
                 "ALL": None}
        keep = allow.get(level, ("WARNING", "ERROR"))
        try:
            lines = _read_filtered_log_tail(log_file, keep=keep, limit=limit)
            return JSONResponse({
                "lines": lines,
                "log_file": log_file,
                "level": level,
                "count": len(lines),
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/errors/recent", methods=["GET"])
    async def api_errors_recent(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            limit = max(1, min(int(request.query_params.get("limit", str(_ERRORS_DEFAULT_LIMIT))), _ERRORS_MAX_LIMIT))
        except ValueError:
            limit = _ERRORS_DEFAULT_LIMIT
        min_level = request.query_params.get("min_level", "W").upper()
        items = recent_errors(limit=limit, min_level=min_level)
        tail = get_recent_logs(15)
        for it in items:
            it["formatted"] = format_error(
                it.get("code", ""), it.get("detail", ""),
                extra=it.get("extra"), include_logs=True,
            )
        return JSONResponse({
            "ok": True,
            "count": len(items),
            "min_level": min_level,
            "log_tail": tail,
            "errors": items,
        })

    @mcp.custom_route("/api/errors/clear", methods=["POST"])
    async def api_errors_clear(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        n = clear_errors_log()
        return JSONResponse({"ok": True, "cleared": n})
