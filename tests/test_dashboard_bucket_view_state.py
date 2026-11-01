import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "frontend" / "dashboard.html"


def _dashboard_section(start_marker: str, end_marker: str) -> str:
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def test_bucket_reload_preserves_active_filter_and_page():
    source = _dashboard_section(
        "async function loadBuckets()", "function updateStats()"
    )

    assert "buildFilters();" in source
    assert "renderBuckets(filterBuckets(allBuckets), true);" in source
    assert "renderBuckets(allBuckets);" not in source


def test_filter_rebuild_restores_active_filter_without_listener_leaks():
    source = _dashboard_section("function buildFilters()", "function filterBuckets(")

    assert "if (!domains.has(currentFilter.slice(7))) currentFilter = 'all';" in source
    assert "!visibleDomains.includes(currentDomain)" in source
    assert "visibleDomains[visibleDomains.length - 1] = currentDomain;" in source
    assert "var active = t.key === currentFilter;" in source
    assert "var active = key === currentFilter;" in source
    assert "aria-pressed" in source
    assert "filters.onclick = function(e)" in source
    assert "filters.addEventListener('click'" not in source


def test_bucket_renderer_only_resets_page_for_an_explicit_view_change():
    source = _dashboard_section("function renderBuckets(", "function gotoBucketPage(")

    assert "function renderBuckets(buckets, preservePage)" in source
    assert "if (!preservePage) bucketPage = 1;" in source


def test_clearing_search_restores_the_active_filter_not_the_all_view():
    source = _dashboard_section(
        "document.getElementById('search-input').addEventListener",
        "async function loadBuckets()",
    )

    assert source.count("else renderBuckets(filterBuckets(allBuckets));") == 2
    assert "else renderBuckets(allBuckets);" not in source


def test_pin_and_edit_refreshes_are_awaited():
    pin_source = _dashboard_section("async function bucketPin(", "async function bucketAnchor(")
    edit_source = _dashboard_section(
        "async function bucketSaveEdit(", "async function maybeShowOnboarding("
    )

    assert "await loadBuckets();" in pin_source
    assert "await loadBuckets();" in edit_source


def test_bucket_pager_has_first_last_and_direct_page_navigation():
    source = _dashboard_section("function _bucketPagerHtml(", "function _paintBuckets(")

    assert '<nav class="bucket-pager" aria-label="记忆桶分页">' in source
    assert "gotoBucketPage(1)" in source
    assert "gotoBucketPage(' + totalPages + ')" in source
    assert 'id="bucket-page-input" type="number"' in source
    assert 'min="1" max="' in source
    assert 'step="1"' in source
    assert "jumpToBucketPage()" in source
    assert 'role="status" aria-live="polite"' in source


def test_bucket_sort_control_keeps_enough_vertical_space_for_text():
    html = DASHBOARD.read_text(encoding="utf-8")
    start = html.index(".bucket-sort-control select {")
    end = html.index("}", start)
    rule = html[start:end]

    # The later global form-control rule uses 10px vertical padding with
    # !important. This local override must win without combining that padding
    # with a fixed 32px box, which clipped the lower half of Chinese glyphs.
    assert "min-height:32px" in rule
    assert "height:auto" in rule
    assert "padding:5px 28px 5px 10px !important" in rule
    assert "line-height:1.4" in rule
    assert ";height:32px" not in rule


def test_empty_bucket_view_resets_page_and_selection_state():
    source = _dashboard_section("function _paintBuckets()", "function _localBucketMatches(")
    empty_branch = source[source.index("if (!visible.length)") : source.index("// 分页：")]

    assert "bucketPage = 1;" in empty_branch
    assert "syncBucketSelectionUi();" in empty_branch


def test_select_all_control_is_scoped_to_the_current_page():
    html = DASHBOARD.read_text(encoding="utf-8")
    source = _dashboard_section(
        "function _currentBucketPageItems(", "async function runBucketBatch("
    )

    assert "全选当前页" in html
    assert "selectAllCurrentPage(this.checked)" in html
    assert "selectAllFiltered" not in html
    assert "return visible.slice(startIdx, startIdx + BUCKETS_PER_PAGE);" in source
    assert "return _currentBucketPageItems().map(function(b)" in source
    assert "var pageIds = _currentBucketPageIds();" in source
    assert "function selectAllCurrentPage(checked)" in source
    assert "_currentBucketPageIds().forEach(function(id)" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_current_page_selection_runtime_boundaries():
    selection_source = _dashboard_section(
        "function _currentBucketPageItems(", "async function runBucketBatch("
    )
    normalizer_source = _dashboard_section(
        "function _normalizeBucketPage(", "function _visibleBucketTotalPages("
    )
    script = """
var _curBuckets = [{id:'hidden', dont_surface:true}].concat(
  Array.from({length:25}, (_, i) => ({id:'b' + (i + 1), dont_surface:false}))
);
var bucketPage = 2;
var BUCKETS_PER_PAGE = 10;
var selectedBucketIds = new Set(['b1']);
var selectAllBox = {checked:false, indeterminate:false, disabled:false};
var selectedCount = {textContent:''};
var document = {
  getElementById(id) {
    if (id === 'bucket-select-all') return selectAllBox;
    if (id === 'bucket-selected-count') return selectedCount;
    return null;
  },
  querySelectorAll(selector) {
    if (selector !== '.bucket-select') return [];
    return _currentBucketPageIds().map(id => ({dataset:{id}, checked:false}));
  },
};
""" + normalizer_source + selection_source + """
selectAllCurrentPage(true);
var secondPageSelected = Array.from(selectedBucketIds).sort();
var secondPageState = [selectAllBox.checked, selectAllBox.indeterminate, selectedCount.textContent];

selectAllCurrentPage(false);
var afterSecondPageClear = Array.from(selectedBucketIds).sort();
var otherPageOnlyState = [selectAllBox.checked, selectAllBox.indeterminate];

selectedBucketIds.add('b11');
syncBucketSelectionUi();
var partialState = [selectAllBox.checked, selectAllBox.indeterminate];

selectedBucketIds.delete('b11');
bucketPage = 3;
selectAllCurrentPage(true);
var lastPageSelected = Array.from(selectedBucketIds).sort();
var lastPageState = [selectAllBox.checked, selectAllBox.indeterminate];

process.stdout.write(JSON.stringify({
  secondPageSelected,
  secondPageState,
  afterSecondPageClear,
  otherPageOnlyState,
  partialState,
  lastPageSelected,
  lastPageState,
}));
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["secondPageSelected"] == [
        "b1",
        "b11",
        "b12",
        "b13",
        "b14",
        "b15",
        "b16",
        "b17",
        "b18",
        "b19",
        "b20",
    ]
    assert result["secondPageState"] == [True, False, "已选 11"]
    assert result["afterSecondPageClear"] == ["b1"]
    assert result["otherPageOnlyState"] == [False, False]
    assert result["partialState"] == [False, True]
    assert result["lastPageSelected"] == [
        "b1",
        "b21",
        "b22",
        "b23",
        "b24",
        "b25",
    ]
    assert result["lastPageState"] == [True, False]


def test_bucket_sort_is_persisted_sent_to_api_and_resets_page():
    html = DASHBOARD.read_text(encoding="utf-8")
    state_source = _dashboard_section("const BASE = location.origin", "function setDeveloperMode(")
    load_source = _dashboard_section("async function loadBuckets()", "function updateStats()")

    assert 'id="bucket-sort"' in html
    assert '<option value="score">综合分优先</option>' in html
    assert '<option value="created_desc">最新创建优先</option>' in html
    assert '<option value="created_asc">最早创建优先</option>' in html
    assert "['score', 'created_desc', 'created_asc']" in state_source
    assert "localStorage.getItem('ombreBucketSort')" in state_source
    assert "localStorage.setItem('ombreBucketSort', bucketSort)" in state_source
    assert "bucketPage = 1;" in state_source
    assert "await loadBuckets();" in state_source
    assert "'?sort=' + encodeURIComponent(requestedSort)" in load_source
    assert "generation !== bucketLoadGeneration" in load_source


def test_time_views_use_created_and_invalid_dates_never_render_nan():
    render_source = _dashboard_section("function _paintBuckets()", "function _localBucketMatches(")
    time_source = _dashboard_section("function parseBucketDate(", "function feelFace(")

    assert "firstValidBucketTime(b.created_epoch_ms, b.created)" in render_source
    assert "b.last_active_epoch_ms, b.last_active, b.created_epoch_ms, b.created" in render_source
    assert "'创建 ' + formatCompactBucketTime(shownTime)" in render_source
    assert "Number.isNaN(d.getTime()) ? null : d" in time_source
    assert "if (!d) return '—';" in time_source


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_filter_rebuild_keeps_selected_domain_outside_visible_top_ten():
    source = _dashboard_section("function buildFilters()", "function filterBuckets(")
    script = """
let currentFilter = 'domain:d11';
let allBuckets = Array.from({length: 12}, (_, i) => ({domain: ['d' + i]}));
function escAttr(value) { return String(value); }
function esc(value) { return String(value); }
const filterElement = {innerHTML: '', onclick: null};
const document = {getElementById() { return filterElement; }};
""" + source + """
buildFilters();
const retained = currentFilter;
const retainedButton = filterElement.innerHTML.includes('data-filter="domain:d11"');
allBuckets = allBuckets.slice(0, 11);
buildFilters();
process.stdout.write(JSON.stringify([retained, retainedButton, currentFilter]));
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == ["domain:d11", True, "all"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_server_epoch_wins_over_browser_local_naive_time():
    source = _dashboard_section("function parseBucketDate(", "function feelFace(")
    script = source + """
const normalized = firstValidBucketTime(0, '1970-01-01T00:00:00');
const fallback = firstValidBucketTime(NaN, '2000-01-01T00:00:00Z');
process.stdout.write(JSON.stringify([normalized.getTime(), fallback.getTime()]));
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [0, 946684800000]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_page_normalizer_runtime_boundaries():
    source = _dashboard_section(
        "function _normalizeBucketPage(", "function _visibleBucketTotalPages("
    )
    script = source + """
const values = [
  _normalizeBucketPage(3, 5, 1),
  _normalizeBucketPage(0, 5, 3),
  _normalizeBucketPage(99, 5, 3),
  _normalizeBucketPage(2.9, 5, 1),
  _normalizeBucketPage('', 5, 3),
  _normalizeBucketPage('not-a-page', 5, 3),
  _normalizeBucketPage(Infinity, 5, 3),
  _normalizeBucketPage(1, 0, 5),
  _normalizeBucketPage(null, 5, 0),
];
process.stdout.write(JSON.stringify(values));
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [3, 1, 5, 2, 3, 3, 3, 1, 1]


def test_reload_keeps_the_search_result_view_when_the_box_still_has_a_query():
    source = _dashboard_section(
        "async function loadBuckets()", "function updateStats()"
    )

    assert "getElementById('search-input')" in source
    assert "await searchBuckets(" in source
    assert "renderBuckets(filterBuckets(allBuckets), true);" in source


def test_reload_passes_the_trimmed_query_and_keeps_the_page():
    source = _dashboard_section(
        "async function loadBuckets()", "function updateStats()"
    )

    assert "await searchBuckets(activeQuery.trim(), true);" in source


def test_search_can_preserve_the_page_for_a_refresh_but_not_for_typing():
    source = _dashboard_section(
        "async function searchBuckets(", "let detailLoadGeneration"
    )

    assert "async function searchBuckets(query, preservePage)" in source
    assert "renderBuckets(merged, preservePage);" in source

    typing = _dashboard_section("let searchTimer;", "async function loadBuckets()")
    assert "searchBuckets(q);" in typing
