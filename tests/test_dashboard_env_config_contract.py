import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend" / "dashboard.html"


def _quick_save_source() -> str:
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("async function _saveEnvKeys(")
    end = html.index("async function saveCompressKey()", start)
    return html[start:end]


def test_quick_env_save_requires_http_and_payload_success():
    source = _quick_save_source()

    assert "var responseFailed = !r.ok || !d || !d.ok" in source
    assert "if (responseFailed && savedKeys.length === 0)" in source
    assert "HTTP ' + r.status" in source
    assert "保存失败 / Save failed" in source


def test_quick_env_save_confirms_every_requested_field_before_green_success():
    source = _quick_save_source()

    requested = "Object.keys(updates || {})"
    missing = "updatedKeys.indexOf(key) === -1"
    safe_success = "if (!responseFailed && !responsePartial"
    positive_feedback = "color:var(--positive,#7EAD68)"

    assert requested in source
    assert "Array.isArray(d.updated)" in source
    assert missing in source
    assert safe_success in source
    assert source.index(safe_success) < source.index(positive_feedback)


def test_quick_env_save_surfaces_warnings_as_partial_or_failed():
    source = _quick_save_source()

    assert "Array.isArray(d.warnings)" in source
    assert "部分保存 / Partially saved" in source
    assert "color:var(--warning,#B89762)" in source
    assert "警告 / Warning:" in source
    assert "savedKeys.length > 0" in source
    assert "服务器未确认任何请求字段" in source


def test_quick_env_save_honors_partial_and_persistence_contract():
    source = _quick_save_source()

    assert "var responsePartial = !!(d && d.partial)" in source
    assert "Array.isArray(d.persisted)" in source
    assert "unpersistedKeys.length === 0" in source
    assert "未持久化 / Not persisted:" in source
    assert "if (savedKeys.length > 0)" in source
    assert "refreshEnvConfig();" in source


def test_main_env_save_flow_is_left_intact():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("async function saveEnvConfig()")
    end = html.index("async function _saveEnvKeys(", start)
    source = html[start:end]

    assert "var r = await authFetch('/api/env-config'" in source
    assert "refreshEnvConfig();" in source
    assert "已保存：" in source


def test_embedding_quick_save_submits_one_complete_provider_tuple():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("async function saveEmbedKey()")
    end = html.index("async function saveConfig(", start)
    source = html[start:end]

    assert "'OMBRE_EMBED_BASE_URL': base" in source
    assert "'OMBRE_EMBED_MODEL': model" in source
    assert "'OMBRE_EMBED_FORMAT': format" in source
    assert "if (key) { updates['OMBRE_EMBED_API_KEY'] = key" in source
    assert source.count("await _saveEnvKeys(") == 1


def test_embedding_migration_submits_current_provider_tuple():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("var migrationPayload = {")
    end = html.index("fetch(BASE + '/api/embedding/migrate'", start)
    source = html[start:end]

    assert "target_backend: targetBackend" in source
    assert "cfg-emb-format" in source
    assert "cfg-emb-base-url" in source
    assert "cfg-emb-model" in source


def test_main_config_save_also_keeps_embedding_base_url_with_model():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("async function saveConfig(")
    end = html.index("checkAuth().then", start)
    source = html[start:end]

    assert "base_url: document.getElementById('cfg-emb-base-url').value" in source


def test_main_config_load_and_save_preserve_valid_zero_values():
    html = DASHBOARD.read_text(encoding="utf-8")
    load_start = html.index("async function loadConfig()")
    save_start = html.index("async function saveConfig(", load_start)
    load_source = html[load_start:save_start]
    save_end = html.index("checkAuth().then", save_start)
    save_source = html[save_start:save_end]

    assert "cfg.dehydration.temperature != null" in load_source
    assert "cfg.merge_threshold != null" in load_source
    assert "Number.isFinite(dehyTemperature) ? dehyTemperature : 0.1" in save_source
    assert "Number.isFinite(mergeThreshold) ? mergeThreshold : 75" in save_source
    assert "parseFloat(document.getElementById('cfg-dehy-temp').value) || 0.1" not in save_source
    assert "parseInt(document.getElementById('cfg-merge').value) || 75" not in save_source


def test_import_budget_dashboard_declares_safe_integer_contract():
    html = DASHBOARD.read_text(encoding="utf-8")
    input_markup = (
        '<input type="number" id="cfg-dehy-import-maxtokens" min="1" '
        'max="9007199254740991" />'
    )
    load_start = html.index("async function loadConfig()")
    save_start = html.index("async function saveConfig(", load_start)
    load_source = html[load_start:save_start]
    save_end = html.index("checkAuth().then", save_start)
    save_source = html[save_start:save_end]

    assert input_markup in html
    assert "normalizeImportMaxTokens(cfg.dehydration.import_max_tokens)" in load_source
    assert "import_max_tokens: dehyImportMaxTokensRaw" in save_source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_import_budget_dashboard_executes_lossless_load_save_round_trip():
    html = DASHBOARD.read_text(encoding="utf-8")
    helper_start = html.index("var IMPORT_MAX_TOKENS_DEFAULT")
    helper_end = html.index("async function loadConfig()", helper_start)
    helpers = html[helper_start:helper_end]
    script = helpers + r"""
const valid = [32768, Number.MAX_SAFE_INTEGER].map(function(value) {
  const loaded = normalizeImportMaxTokens(value);
  return {loaded: loaded, saved: normalizeImportMaxTokens(loaded)};
});
const invalid = normalizeImportMaxTokens('9007199254740992');
process.stdout.write(JSON.stringify({valid: valid, invalid: invalid}));
"""

    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "valid": [
            {"loaded": "32768", "saved": "32768"},
            {
                "loaded": "9007199254740991",
                "saved": "9007199254740991",
            },
        ],
        "invalid": None,
    }
