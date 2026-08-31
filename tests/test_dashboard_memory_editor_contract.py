import json
from pathlib import Path
import shutil
import subprocess

import pytest

import web.buckets as buckets_web


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend" / "dashboard.html"


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(method, path)] = handler
            return handler

        return decorator


class JsonRequest:
    def __init__(self, *, path_params=None):
        self.path_params = path_params or {}
        self.headers = {}
        self.query_params = {}


class FakeDecayEngine:
    def calculate_score(self, _metadata):
        return 1.0


class FakeBucketManager:
    def __init__(self, bucket):
        self.bucket = bucket

    async def list_all(self, *, include_archive=False):
        assert include_archive is True
        return [self.bucket]

    async def get(self, bucket_id):
        return self.bucket if bucket_id == self.bucket["id"] else None

    async def get_triggered_feels(self, _bucket_id):
        return []


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def _dashboard_function(name, next_name):
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index(f"function {name}(")
    end = html.index(f"function {next_name}(", start)
    return html[start:end]


@pytest.mark.asyncio
async def test_bucket_detail_preserves_raw_content_and_separates_display_text(
    monkeypatch,
):
    raw_content = "before [[Target|Alias]] and [[Target#Section]] after"
    bucket = {
        "id": "memory-1",
        "metadata": {
            "name": "Linked memory",
            "type": "dynamic",
            "meaning": ["first meaning", "second meaning"],
            "imported": True,
        },
        "content": raw_content,
    }
    manager = FakeBucketManager(bucket)
    monkeypatch.setattr(buckets_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(buckets_web.sh, "bucket_mgr", manager, raising=False)
    monkeypatch.setattr(
        buckets_web.sh, "decay_engine", FakeDecayEngine(), raising=False
    )
    mcp = FakeMCP()
    buckets_web.register(mcp)

    list_response = await mcp.routes[("GET", "/api/buckets")](JsonRequest())
    detail_response = await mcp.routes[("GET", "/api/bucket/{bucket_id}")](
        JsonRequest(path_params={"bucket_id": "memory-1"})
    )
    listed = _payload(list_response)[0]
    detail = _payload(detail_response)

    assert listed["content_preview"] == (
        "before Target|Alias and Target#Section after"
    )
    assert detail["content"] == raw_content
    assert detail["display_content"] == listed["content_preview"]
    assert detail["metadata"]["meaning"] == [
        "first meaning",
        "second meaning",
    ]
    assert listed["imported"] is True


def test_dashboard_uses_display_text_for_preview_and_raw_content_for_editor():
    source = _dashboard_function("showDetail", "bucketPin")

    assert "typeof b.display_content === 'string'" in source
    assert "esc(displayContent)" in source
    assert "_content_for_edit: b.content" in source
    assert "'<div class=\"detail-content\">' + esc(b.content)" not in source


def test_dashboard_names_the_calculated_score_as_read_only_activity():
    source = _dashboard_function("showDetail", "bucketPin")

    assert "活跃度分 / Activity score" in source
    assert "权重分 / Weight" not in source
    assert "b.score.toFixed(4)" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_dashboard_detail_renders_meaning_as_escaped_quote_blocks():
    html = DASHBOARD.read_text(encoding="utf-8")
    normalize_source = _dashboard_function(
        "normalizeMeaningItems", "renderMeaningHtml"
    )
    render_start = html.index("function renderMeaningHtml(")
    render_end = html.index("async function searchBuckets(", render_start)
    render_source = html[render_start:render_end]
    detail_source = _dashboard_function("showDetail", "bucketPin")
    script = """
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>\"']/g, function(char) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[char];
  });
}
""" + normalize_source + render_source + """
const many = renderMeaningHtml([
  '  first meaning  ',
  '<img src=x onerror=alert(1)>',
  '',
  'second meaning',
]);
const legacy = renderMeaningHtml('  legacy string meaning  ');
process.stdout.write(JSON.stringify({many, legacy, empty: renderMeaningHtml([])}));
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    rendered = json.loads(completed.stdout)

    assert rendered["many"].count('class="meaning-quote"') == 3
    assert rendered["many"].index("first meaning") < rendered["many"].index(
        "second meaning"
    )
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered["many"]
    assert "<img" not in rendered["many"]
    assert "legacy string meaning" in rendered["legacy"]
    assert rendered["empty"] == ""
    assert 'class="meaning-block"' in rendered["many"]
    assert ".meaning-block" in html
    assert "border-left: 3px solid var(--accent);" in html
    assert "var meaningHtml = renderMeaningHtml(meta.meaning);" in detail_source
    assert detail_source.index("whyHtml +") < detail_source.index(
        "meaningHtml +"
    ) < detail_source.index("'<div class=\"detail-meta\">'")


def test_dashboard_detail_does_not_render_an_editor_for_failed_bucket_fetch():
    source = _dashboard_function("showDetail", "bucketPin")

    assert "var b = await readJsonSafe(res);" in source
    assert "if (!res.ok)" in source
    assert "Array.isArray(b)" in source
    assert "const generation = ++detailLoadGeneration;" in source
    assert "if (generation !== detailLoadGeneration) return false;" in source
    assert source.index("if (!res.ok)") < source.index("renderEditForm(")


def test_github_restore_surfaces_legacy_source_evidence_warning():
    source = _dashboard_function("runGithubImport", "_runBackfillSilent")

    assert "d.integrity_warning" in source
    assert "esc(d.integrity_warning)" in source
    assert "var(--negative)" in source
    assert "d.buckets_imported" in source
    assert "d.sources_imported" in source


def test_status_banner_tracks_responsive_sticky_header_height():
    html = DASHBOARD.read_text(encoding="utf-8")
    source = _dashboard_function(
        "syncStatusBannerOffset", "watchStatusBannerOffset"
    )
    watcher = _dashboard_function(
        "watchStatusBannerOffset", "renderStatusBannerCard"
    )

    assert "top: var(--ob-header-height, 96px)" in html
    assert "getBoundingClientRect().height" in source
    assert "--ob-header-height" in source
    assert "new ResizeObserver(syncStatusBannerOffset)" in watcher
    assert "window.addEventListener('resize', syncStatusBannerOffset)" in watcher


def test_dashboard_has_an_intermediate_pc_responsive_layout():
    html = DASHBOARD.read_text(encoding="utf-8")

    breakpoint = "@media (min-width: 769px) and (max-width: 1500px)"
    assert breakpoint in html
    responsive = html[html.index(breakpoint) :]

    assert 'grid-template-areas:' in responsive
    assert '"title meta meta"' in responsive
    assert '"search search actions"' in responsive
    assert 'grid-template-columns: repeat(6, minmax(0, 1fr))' in responsive
    assert 'white-space: nowrap' in responsive
    assert '.tab-spacer { display: none; }' in responsive
    assert 'max-width: min(220px, 100%)' in html
    assert '#owner-badge-text {' in html
    assert 'text-overflow: ellipsis' in html


def test_mobile_responsive_overrides_follow_the_retro_theme_layer():
    html = DASHBOARD.read_text(encoding="utf-8")

    retro_layer = html.index("RETRO HANDHELD CONSOLE LAYER")
    final_mobile_override = html.rindex("@media (max-width: 768px)")

    assert final_mobile_override > retro_layer
    responsive = html[final_mobile_override:]
    assert 'grid-template-columns: repeat(3, minmax(0, 1fr))' in responsive
    assert '.header-actions button {' in responsive
    assert 'font-size: 0 !important' in responsive


def test_editor_preserves_special_and_future_bucket_types():
    render_source = _dashboard_function("renderEditForm", "bucketSaveEdit")
    save_source = _dashboard_function("bucketSaveEdit", "maybeShowOnboarding")

    assert (
        "const editableTypes = ['dynamic','permanent','feel','plan','letter']"
        in render_source
    )
    assert "const currentType = String(meta.type || 'dynamic')" in render_source
    assert "[currentType].concat(editableTypes)" in render_source
    assert "meta.pinned && typeIsEditable" in render_source
    assert "? ['permanent', 'dynamic']" in render_source
    assert "currentType === t ? 'selected' : ''" in render_source
    assert "typeIsEditable ? '' : 'disabled" in render_source
    assert "if (typeEl && !typeEl.disabled) body.type = typeEl.value" in save_source
    assert "type: document.getElementById('edit-type').value" not in save_source


def test_editor_submits_metadata_using_storage_field_names():
    render_source = _dashboard_function("renderEditForm", "syncEditPinConstraints")
    source = _dashboard_function("bucketSaveEdit", "maybeShowOnboarding")

    assert 'id="edit-title"' in render_source
    assert 'maxlength="120"' in render_source
    assert "meta.title || fallbackTitle" in render_source
    assert "data-dirty=\"0\"" in render_source
    assert "oninput=\"this.dataset.dirty='1'\"" in render_source
    assert "title: document.getElementById('edit-title').value" not in source
    assert "if (titleEl && titleEl.dataset.dirty === '1') body.title" in source
    assert "dont_surface: document.getElementById('edit-dont-surface').checked" in source
    assert "why_remembered: document.getElementById('edit-why').value" in source
    assert "if (weightEl) body.weight = parseFloat(weightEl.value) / 100" in source


def test_editor_keeps_pin_type_and_importance_constraints_in_sync():
    render_source = _dashboard_function("renderEditForm", "syncEditPinConstraints")
    sync_source = _dashboard_function("syncEditPinConstraints", "bucketSaveEdit")
    save_source = _dashboard_function("bucketSaveEdit", "maybeShowOnboarding")

    assert "onchange=\"syncEditPinConstraints('type')\"" in render_source
    assert "syncEditPinConstraints('importance')" in render_source
    assert "onchange=\"syncEditPinConstraints('pinned')\"" in render_source
    assert "typeEl.value = 'permanent';" in sync_source
    assert "importanceEl.value = '10';" in sync_source
    assert "pinnedEl.checked = false;" in sync_source
    assert "syncEditPinConstraints('save');" in save_source
    assert save_source.index("syncEditPinConstraints('save');") < save_source.index(
        "const body = {"
    )


def test_imported_memory_cards_open_the_full_editor_and_refresh_after_save():
    list_source = _dashboard_function(
        "loadImportResults", "openImportedBucketEditor"
    )
    open_source = _dashboard_function(
        "openImportedBucketEditor", "detectPatterns"
    )
    render_source = _dashboard_function("renderEditForm", "syncEditPinConstraints")
    save_source = _dashboard_function("bucketSaveEdit", "maybeShowOnboarding")

    # The import result contains only a 300-character preview. The edit button
    # must pass only the ID and let showDetail fetch the lossless bucket body.
    assert "openImportedBucketEditor(this.dataset.bucketId)" in list_source
    assert 'data-bucket-id="${escAttr(b.id)}"' in list_source
    assert "renderEditForm(b.id, b)" not in list_source
    assert "if (!await showDetail(bid)) return;" in open_source

    # One global detail editor avoids duplicate edit-* element IDs, and opening
    # from the import review area should land directly in the expanded form.
    assert 'id="bucket-edit-form"' in render_source
    assert "document.getElementById('bucket-edit-form')" in open_source
    assert "editor.open = true;" in open_source
    assert 'for="edit-content"' in render_source
    assert "contentInput.focus();" in open_source
    assert "preventScroll" not in open_source

    # Saving while the import tab is visible refreshes the preview card without
    # kicking the reviewer back to the top of the list.
    assert "const importView = document.getElementById('import-view');" in save_source
    assert "const refreshImportResults =" in save_source
    assert "const detailGenerationAtSave = detailLoadGeneration;" in save_source
    assert "if (detailLoadGeneration === detailGenerationAtSave)" in save_source
    assert (
        "await loadImportResults({preserveScroll:true, "
        "scrollTop:importScrollTop});" in save_source
    )
    assert save_source.index("if (!r.ok)") < save_source.index(
        "await loadImportResults({preserveScroll:true, scrollTop:importScrollTop});"
    ) < save_source.index("} catch (e) {")


def test_import_ui_marks_provenance_refreshes_list_and_supports_pagination():
    html = DASHBOARD.read_text(encoding="utf-8")
    activate_source = _dashboard_function("activateDashboardTab", "doSearch")
    paint_source = _dashboard_function("_paintBuckets", "_localBucketMatches")
    detail_source = _dashboard_function("showDetail", "bucketPin")
    update_source = _dashboard_function("updateImportUI", "pauseImport")
    import_source = _dashboard_function(
        "loadImportResults", "openImportedBucketEditor"
    )

    assert "if (target === 'list') pending.push(loadBuckets());" in activate_source
    assert "b.imported ?" in paint_source
    assert "被导入" in paint_source
    assert "导入来源 / Imported" in detail_source
    assert "loadBuckets();" in update_source
    assert "&offset=" in import_source
    assert "data.has_more" in import_source
    assert "被导入" in import_source
    assert 'id="import-results-more"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_imported_memory_editor_opens_and_focuses_at_runtime():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("async function openImportedBucketEditor(")
    end = html.index("async function detectPatterns(", start)
    source = html[start:end]
    script = """
let passedId = null;
let scrolled = false;
let focused = false;
let loadSucceeds = true;
const editor = {
  open: false,
  scrollIntoView(options) { scrolled = options.block === 'start'; },
};
const contentInput = {
  focus() { focused = true; },
};
const document = {
  getElementById(id) {
    if (id === 'bucket-edit-form') return editor;
    if (id === 'edit-content') return contentInput;
    return null;
  },
};
async function showDetail(id) { passedId = id; return loadSucceeds; }
""" + source + """
(async function() {
  await openImportedBucketEditor('id / & "quoted"');
  const success = [passedId, editor.open, focused];
  editor.open = false;
  focused = false;
  loadSucceeds = false;
  await openImportedBucketEditor('missing');
  process.stdout.write(JSON.stringify([
    success, editor.open, focused,
  ]));
})().catch(function(error) {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        ['id / & "quoted"', True, True], False, False,
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_import_results_latest_request_wins_and_preserves_scroll():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("let importResultsLoadGeneration = 0;")
    end = html.index("async function openImportedBucketEditor(", start)
    source = html[start:end]
    script = """
const pending = [];
const container = {
  innerHTML: '<div>existing</div>',
  scrollTop: 73,
  setAttribute() {},
  removeAttribute() {},
};
const document = {
  getElementById(id) { return id === 'import-results-list' ? container : null; },
};
const BASE = '';
function fetch() { return new Promise(resolve => pending.push(resolve)); }
async function readJsonSafe(response) { return response.payload; }
function esc(value) { return String(value == null ? '' : value); }
function escAttr(value) { return esc(value); }
""" + source + """
(async function() {
  const oldRequest = loadImportResults({preserveScroll:true, scrollTop:73});
  const newRequest = loadImportResults({preserveScroll:true, scrollTop:73});
  pending[1]({ok:true, status:200, payload:{buckets:[{
    id:'new', name:'fresh response', content:'new body', type:'dynamic',
    domain:[], tags:[], importance:5,
  }]}});
  await newRequest;
  pending[0]({ok:true, status:200, payload:{buckets:[{
    id:'old', name:'stale response', content:'old body', type:'dynamic',
    domain:[], tags:[], importance:5,
  }]}});
  await oldRequest;
  process.stdout.write(JSON.stringify([
    container.innerHTML.includes('fresh response'),
    container.innerHTML.includes('stale response'),
    container.scrollTop,
  ]));
})().catch(function(error) {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [True, False, 73]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_bucket_detail_latest_request_wins_at_runtime():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index("let detailLoadGeneration = 0;")
    end = html.index("async function bucketPin(", start)
    source = html[start:end]
    script = """
const pending = [];
const panel = {classList:{add() {}, toggle() {}}};
const content = {innerHTML:''};
const document = {
  getElementById(id) {
    if (id === 'detail-panel') return panel;
    if (id === 'detail-content') return content;
    return null;
  },
};
const BASE = '';
function fetch(url) {
  return new Promise(resolve => pending.push({url, resolve}));
}
async function readJsonSafe(response) { return response.payload; }
function esc(value) { return String(value == null ? '' : value); }
""" + source + """
(async function() {
  const oldRequest = showDetail('old/id');
  const newRequest = showDetail('new/id');
  pending[1].resolve({ok:false, status:404, payload:{error:'new failure'}});
  const newResult = await newRequest;
  const afterNew = content.innerHTML;
  pending[0].resolve({ok:false, status:404, payload:{error:'stale failure'}});
  const oldResult = await oldRequest;
  process.stdout.write(JSON.stringify([
    pending.map(item => item.url), newResult, oldResult,
    afterNew.includes('new failure'),
    content.innerHTML.includes('new failure'),
    content.innerHTML.includes('stale failure'),
  ]));
})().catch(function(error) {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        ["/api/bucket/old%2Fid", "/api/bucket/new%2Fid"],
        False,
        False,
        True,
        True,
        False,
    ]
