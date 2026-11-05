import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_dashboard_bucket_edit_persistence import FakeMCP, JsonRequest
from web import buckets


@pytest.mark.asyncio
@pytest.mark.parametrize('meta,can_pin', [
    ({'type': 'dynamic'}, True),
    ({'type': 'letter'}, False),
    ({'type': 'dynamic', 'source_tool': 'letter'}, False),
    ({'type': 'permanent', 'tags': ['__letter__'], 'pinned': True}, False),
    ({'type': 'dynamic', 'lock_type': 'permanent', 'locked_by': 'ai'}, False),
])
async def test_detail_pin_capability_matches_backend(monkeypatch, meta, can_pin):
    row = {'id': 'fiction', 'content': 'secret fictional body',
           'metadata': {'name': 'secret title', 'importance': 5, **meta}}
    async def update(_id, **updates):
        row['metadata'].update(updates)
        return True

    manager = SimpleNamespace(get=AsyncMock(return_value=row),
                              get_triggered_feels=AsyncMock(return_value=[]),
                              update=update)
    monkeypatch.setattr(buckets.sh, '_require_auth', lambda r: None)
    monkeypatch.setattr(buckets.sh, 'bucket_mgr', manager)
    monkeypatch.setattr(buckets.sh, 'decay_engine', SimpleNamespace(calculate_score=lambda m: 1))
    monkeypatch.setattr(buckets.sh, 'deletion_requests', None)
    # Normal pin quota is checked separately by the existing quota suite.
    monkeypatch.setattr(buckets, '_check_pinned_quota', AsyncMock(return_value=None))
    app = FakeMCP()
    buckets.register(app)
    request = JsonRequest('fiction', {})
    response = await app.routes[('GET', '/api/bucket/{bucket_id}')](request)
    data = json.loads(response.body)
    assert data.get('can_pin') is can_pin
    if meta.get('locked_by'):
        assert data['content'] == ''
        assert 'secret' not in response.body.decode()
    pin = await app.routes[('POST', '/api/bucket/{bucket_id}/pin')](request)
    assert pin.status_code == (200 if can_pin else 403)


def test_dashboard_renders_pin_only_when_detail_api_allows(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    html = (Path(__file__).resolve().parents[1] / 'frontend/dashboard.html').read_text()
    source = html[html.index('let detailLoadGeneration = 0;'):html.index('async function bucketPin(')]
    harness = '''
const assert = require('node:assert/strict');
const content = {innerHTML: ''};
const panel = {classList: {add(){}, remove(){}, toggle(){}}};
const document = {getElementById: id => id === 'detail-content' ? content : panel};
const BASE = '', window = {}, _SV = {};
const esc = String, escAttr = String;
const safeNumber = (n, fallback) => Number.isFinite(Number(n)) ? Number(n) : fallback;
const renderMeaningHtml = () => '', renderEditForm = () => '';
let response;
const fetch = async () => ({ok: true});
const readJsonSafe = async () => response;
'''
    cases = '''
(async () => {
  for (const [canPin, type, pinned, locked] of [
    [true, 'dynamic', false, false],
    [false, 'letter', false, false],
    [false, 'dynamic', false, false],
    [false, 'permanent', true, false],
    [false, 'dynamic', false, true]
  ]) {
    response = {can_pin: canPin, metadata: {type, pinned}, letter_locked: locked};
    assert.equal(await showDetail('fiction'), true);
    assert.equal(content.innerHTML.includes('onclick="bucketPin('), canPin);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    script = tmp_path / 'detail.js'
    script.write_text(harness + source + cases)
    result = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
