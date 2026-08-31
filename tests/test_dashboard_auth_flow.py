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


def test_all_auth_success_paths_share_the_dashboard_initializer():
    html = DASHBOARD.read_text(encoding="utf-8")
    initializer = _dashboard_section(
        "var _authenticatedDashboardInitPromise", "async function checkAuth()"
    )

    expected_tasks = (
        "loadBuckets()",
        "loadStatusBanner()",
        "syncRestartRequirement()",
        "refreshAnchorCounter()",
        "checkEmptyMemoryBanner()",
        "loadOwnerBadge()",
        "pollHeartbeat()",
        "pollCriticalErrors()",
        "maybeShowOnboarding()",
        "initSelfFab()",
    )
    for task in expected_tasks:
        assert task in initializer
    assert "Promise.allSettled(tasks)" in initializer
    assert "_authenticatedDashboardInitGeneration === generation" in initializer
    assert "previous.catch(function() { return null; })" in initializer
    assert "if (generation !== _dashboardAuthGeneration) return [];" in initializer
    assert "startAuthenticatedDashboardPolling();" in initializer
    assert "if (_authenticatedDashboardInitPromise === run)" in initializer

    for start, end in (
        ("async function doSetup()", "function showLogin()"),
        ("async function doRecover()", "async function doLogin()"),
        ("async function doLogin()", "async function doLogout()"),
    ):
        source = _dashboard_section(start, end)
        assert "await finishDashboardAuthentication();" in source
        assert "location.reload" not in source

    assert (
        "checkAuth().then(authed => { if (authed) return "
        "beginAuthenticatedDashboardSession(); });"
    ) in html
    logout = _dashboard_section("async function doLogout()", "async function changePassword()")
    assert "invalidateAuthenticatedDashboardSession();" in logout

    check_auth = _dashboard_section("async function checkAuth()", "function showAuthError")
    assert "var requestedGeneration = _dashboardAuthGeneration;" in check_auth
    assert check_auth.count(
        "if (requestedGeneration !== _dashboardAuthGeneration) return false;"
    ) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_auth_status_check_bypasses_http_cache():
    check_auth = _dashboard_section("async function checkAuth()", "function showAuthError")
    script = r"""
let _dashboardAuthGeneration = 0;
let fetchCall = null;
const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {textContent:'', style:{}});
    return elements.get(id);
  },
};
function invalidateAuthenticatedDashboardSession() {}
async function fetch(url, options) {
  fetchCall = {url, options: options ?? null};
  return {
    async json() { return {authenticated:true, setup_needed:false}; },
  };
}
""" + check_auth + r"""

(async function() {
  const authenticated = await checkAuth();
  process.stdout.write(JSON.stringify({authenticated, fetchCall}));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result == {
        "authenticated": True,
        "fetchCall": {
            "url": "/auth/status",
            "options": {"cache": "no-store"},
        },
    }


def test_auth_only_initial_loads_do_not_run_before_authentication():
    html = DASHBOARD.read_text(encoding="utf-8")
    dom_ready = _dashboard_section(
        "// Re-render Lucide icons whenever DOM is ready",
        "async function checkEmptyMemoryBanner()",
    )
    for eager_call in (
        "syncRestartRequirement();",
        "refreshAnchorCounter();",
        "checkEmptyMemoryBanner();",
        "loadOwnerBadge();",
    ):
        assert eager_call not in dom_ready
    assert "setInterval(refreshAnchorCounter, 30000);" not in dom_ready

    active_view = _dashboard_section(
        "function refreshAuthenticatedActiveView()", "async function openFeedback()"
    )
    assert "if (location.hash === '#letters')" in active_view
    assert "if (target) return activateDashboardTab(target);" in active_view
    assert html.count("refreshAuthenticatedActiveView") == 3

    tab_activation = _dashboard_section(
        "function activateDashboardTab(tab)",
        "document.querySelectorAll('.tab').forEach(tab =>",
    )
    assert "pending.push(loadLetters())" in tab_activation
    assert "return Promise.allSettled(pending);" in tab_activation

    polling = _dashboard_section(
        "function startAuthenticatedDashboardPolling()",
        "function invalidateAuthenticatedDashboardSession()",
    )
    assert html.count("setInterval(pollHeartbeat, 15000)") == 1
    assert html.count("setInterval(refreshAnchorCounter, 30000)") == 1
    assert html.count("setInterval(pollCriticalErrors, 60000)") == 1
    assert "stopAuthenticatedDashboardPolling();" in html
    assert "clearInterval(_heartbeatPollTimer)" in polling
    assert "clearInterval(_anchorPollTimer)" in polling
    assert "clearInterval(_errorAlertTimer)" in polling

    assert "(async function initSelfFab()" not in html
    assert "async function initSelfFab()" in html
    self_fab = _dashboard_section("async function initSelfFab()", "</script>")
    assert "classList.toggle('has-entries', hasEntries)" in self_fab
    assert "_selfEntries = hasEntries ? data : [];" in self_fab


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_setup_form_warns_only_when_not_on_a_loopback_host():
    section = _dashboard_section(
        "function _isLikelyLoopbackHost()", "function showAuthError"
    )

    def _remote_warning_display(hostname: str) -> str:
        script = f"""
const window = {{ location: {{ hostname: {json.dumps(hostname)} }} }};
let _dashboardAuthGeneration = 0;
const elements = new Map();
const document = {{
  getElementById(id) {{
    if (!elements.has(id)) elements.set(id, {{textContent:'', style:{{}}}});
    return elements.get(id);
  }},
}};
function invalidateAuthenticatedDashboardSession() {{}}
async function fetch(url, options) {{
  return {{ async json() {{ return {{authenticated:false, setup_needed:true}}; }} }};
}}
""" + section + r"""

(async function() {
  await checkAuth();
  const el = document.getElementById('auth-setup-remote-warning');
  process.stdout.write(JSON.stringify({display: el.style.display}));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
        completed = subprocess.run(
            [shutil.which("node"), "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(completed.stdout)["display"]

    assert _remote_warning_display("ombre.example.com") == "block"
    assert _remote_warning_display("localhost") == "none"
    assert _remote_warning_display("127.0.0.1") == "none"
    assert _remote_warning_display("127.5.6.7") == "none"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_setup_failure_explains_the_local_only_restriction():
    setup_source = _dashboard_section(
        "function showAuthError(msg)", "function showLogin()"
    )
    backend_error = (
        "Initial password setup is local-only. Set OMBRE_DASHBOARD_PASSWORD "
        "before public deployment, or supply X-Ombre-Setup-Token matching "
        "OMBRE_SETUP_TOKEN."
    )
    script = f"""
const elements = new Map([
  ['auth-setup-pwd', {{value:'a-real-password', textContent:'', style:{{}}}}],
  ['auth-setup-pwd2', {{value:'a-real-password', textContent:'', style:{{}}}}],
  ['auth-error', {{value:'', textContent:'', style:{{}}}}],
]);
const document = {{
  getElementById(id) {{
    return elements.has(id) ? elements.get(id) : null;
  }},
}};
async function fetch(url) {{
  if (url !== '/auth/setup') throw new Error('unexpected fetch: ' + url);
  return {{
    ok: false,
    status: 403,
    async json() {{ return {{error: {json.dumps(backend_error)}}}; }},
  }};
}}
""" + setup_source + r"""

(async function() {
  await doSetup();
  const error = elements.get('auth-error');
  process.stdout.write(JSON.stringify({text: error.textContent}));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert "OMBRE_DASHBOARD_PASSWORD" in result["text"]
    assert "OMBRE_SETUP_TOKEN" in result["text"]
    assert "X-Ombre-Setup-Token" in result["text"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_login_failure_displays_backend_error_message():
    auth_source = (
        _dashboard_section("function showAuthError(msg)", "async function doSetup()")
        + _dashboard_section("async function doLogin()", "async function doLogout()")
    )
    expected_error = "登录服务繁忙，请 17 秒后重试"
    script = r"""
const elements = new Map([
  ['auth-login-pwd', {value:'wrong-password', textContent:'', style:{}}],
  ['auth-error', {value:'', textContent:'', style:{}}],
]);
const document = {
  getElementById(id) {
    if (!elements.has(id)) throw new Error('unexpected element: ' + id);
    return elements.get(id);
  },
};
async function fetch(url) {
  if (url !== '/auth/login') throw new Error('unexpected fetch: ' + url);
  return {
    ok: false,
    async json() { return {error:'登录服务繁忙，请 17 秒后重试'}; },
  };
}
""" + auth_source + r"""

(async function() {
  await doLogin();
  const error = elements.get('auth-error');
  process.stdout.write(JSON.stringify({
    textContent: error.textContent,
    display: error.style.display,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result["textContent"] == expected_error
    assert result["display"] == "block"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_login_failure_reports_non_json_proxy_response_instead_of_password_error():
    auth_source = (
        _dashboard_section("function showAuthError(msg)", "async function doSetup()")
        + _dashboard_section("async function doLogin()", "async function doLogout()")
    )
    script = r"""
const elements = new Map([
  ['auth-login-pwd', {value:'correct-password', textContent:'', style:{}}],
  ['auth-error', {value:'', textContent:'', style:{}}],
]);
const document = {
  getElementById(id) {
    if (!elements.has(id)) throw new Error('unexpected element: ' + id);
    return elements.get(id);
  },
};
async function fetch(url) {
  if (url !== '/auth/login') throw new Error('unexpected fetch: ' + url);
  return {
    ok: false,
    status: 502,
    async json() { throw new Error('nginx returned HTML'); },
  };
}
""" + auth_source + r"""

(async function() {
  await doLogin();
  const error = elements.get('auth-error');
  process.stdout.write(JSON.stringify({
    textContent: error.textContent,
    display: error.style.display,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result == {
        "textContent": (
            "登录请求失败（HTTP 502）：反向代理未返回 OB 的 JSON 响应，"
            "请检查 nginx 是否完整转发 /auth/*。"
        ),
        "display": "block",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_login_success_requires_cookie_backed_session_before_dashboard_init():
    auth_source = _dashboard_section(
        "var _authenticatedDashboardInitPromise", "async function checkAuth()"
    ) + _dashboard_section("function showAuthError(msg)", "async function doSetup()")
    script = r"""
let _dashboardAuthGeneration = 0;
let initialized = false;
function setInterval() { return 1; }
function clearInterval() {}
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value:'', textContent:'', style:{}});
  return elements.get(id);
}
const document = {getElementById: element};
function invalidateAuthenticatedDashboardSession() {}
function startAuthenticatedDashboardPolling() {}
async function loadBuckets() { initialized = true; }
async function loadStatusBanner() {}
async function syncRestartRequirement() {}
async function refreshAnchorCounter() {}
async function checkEmptyMemoryBanner() {}
async function loadOwnerBadge() {}
async function pollHeartbeat() {}
async function pollCriticalErrors() {}
async function maybeShowOnboarding() {}
async function initSelfFab() {}
async function refreshAuthenticatedActiveView() {}
async function fetch(url) {
  if (url !== '/auth/status') throw new Error('unexpected fetch: ' + url);
  return {
    ok: true,
    async json() { return {authenticated:false, setup_needed:false}; },
  };
}
""" + auth_source + r"""

(async function() {
  const authenticated = await finishDashboardAuthentication();
  process.stdout.write(JSON.stringify({
    authenticated,
    initialized,
    error: element('auth-error').textContent,
    errorDisplay: element('auth-error').style.display,
    overlayDisplay: element('auth-overlay').style.display,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result == {
        "authenticated": False,
        "initialized": False,
        "error": (
            "验证已通过，但浏览器未建立登录会话。请检查 nginx 的 Host、"
            "X-Forwarded-Proto 与 Set-Cookie 转发。"
        ),
        "errorDisplay": "block",
        "overlayDisplay": "flex",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_auth_success_runtime_initializes_without_reload_and_is_single_flight():
    auth_source = _dashboard_section(
        "var _dashboardAuthGeneration", "async function changePassword()"
    )
    script = r"""
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value:'', textContent:'', style:{}});
  return elements.get(id);
}
const document = {
  readyState: 'complete',
  getElementById: element,
  addEventListener() { throw new Error('DOMContentLoaded wait was unexpected'); },
};
element('auth-setup-pwd').value = 'secure-password';
element('auth-setup-pwd2').value = 'secure-password';
element('auth-setup-q').value = '';
element('auth-setup-a').value = '';
element('auth-recovery-answer').value = 'answer';
element('auth-recovery-newpwd').value = 'new-secure-password';
element('auth-login-pwd').value = 'secure-password';

let responseOK = true;
let failSecurityQuestion = false;
let pendingAuthStatus = null;
const fetchCalls = [];
let nextTimerId = 1;
let timerEvents = [];
const activeTimers = new Map();
function setInterval(callback, delay) {
  const id = nextTimerId++;
  activeTimers.set(id, {callback, delay});
  timerEvents.push(['start', delay]);
  return id;
}
function clearInterval(id) {
  const timer = activeTimers.get(id);
  if (timer) timerEvents.push(['stop', timer.delay]);
  activeTimers.delete(id);
}
async function fetch(url) {
  fetchCalls.push(url);
  if (url === '/auth/status' && pendingAuthStatus) {
    const response = pendingAuthStatus;
    pendingAuthStatus = null;
    return response;
  }
  if (url === '/auth/status') {
    return {
      ok: true,
      async json() { return {authenticated:true, setup_needed:false}; },
    };
  }
  if (url === '/auth/security-question' && failSecurityQuestion) {
    throw new Error('optional security question is unavailable');
  }
  return {
    ok: responseOK,
    async json() { return {error:'failed', detail:'failed'}; },
  };
}

let initCalls = [];
let bucketGate = null;
async function loadBuckets() {
  initCalls.push('loadBuckets');
  if (bucketGate) await bucketGate;
}
async function loadStatusBanner() { initCalls.push('loadStatusBanner'); }
async function syncRestartRequirement() { initCalls.push('syncRestartRequirement'); }
async function refreshAnchorCounter() { initCalls.push('refreshAnchorCounter'); }
async function checkEmptyMemoryBanner() { initCalls.push('checkEmptyMemoryBanner'); }
async function loadOwnerBadge() { initCalls.push('loadOwnerBadge'); }
async function pollHeartbeat() { initCalls.push('pollHeartbeat'); }
async function pollCriticalErrors() { initCalls.push('pollCriticalErrors'); }
async function maybeShowOnboarding() { initCalls.push('maybeShowOnboarding'); }
async function initSelfFab() { initCalls.push('initSelfFab'); }
let activeViewGate = null;
let notifyActiveViewStarted = null;
async function refreshAuthenticatedActiveView() {
  initCalls.push('refreshAuthenticatedActiveView');
  if (notifyActiveViewStarted) {
    notifyActiveViewStarted();
    notifyActiveViewStarted = null;
  }
  if (activeViewGate) await activeViewGate;
}
""" + auth_source + r"""

(async function() {
  const timersBeforeAuth = timerEvents.slice();
  let releaseGate;
  bucketGate = new Promise(resolve => { releaseGate = resolve; });
  const first = initializeAuthenticatedDashboard();
  const second = initializeAuthenticatedDashboard();
  await Promise.resolve();
  releaseGate();
  await first;
  const singleFlight = {
    samePromise: first === second,
    calls: initCalls.slice(),
  };
  bucketGate = null;

  async function successful(action) {
    responseOK = true;
    initCalls = [];
    await action();
    return initCalls.slice();
  }
  const login = await successful(doLogin);
  const setup = await successful(doSetup);
  const recover = await successful(doRecover);

  element('auth-setup-q').value = 'question';
  element('auth-setup-a').value = 'answer';
  failSecurityQuestion = true;
  const setupWithOptionalFailure = await successful(doSetup);
  failSecurityQuestion = false;

  responseOK = false;
  initCalls = [];
  await doLogin();
  await doRecover();
  await doSetup();
  const failedAuthCalls = initCalls.slice();

  responseOK = true;
  stopAuthenticatedDashboardPolling();
  timerEvents = [];
  initCalls = [];
  let releaseOldRun;
  let resolveOldStarted;
  const oldStarted = new Promise(resolve => { resolveOldStarted = resolve; });
  notifyActiveViewStarted = resolveOldStarted;
  activeViewGate = new Promise(resolve => { releaseOldRun = resolve; });
  const oldRun = beginAuthenticatedDashboardSession();
  await oldStarted;
  await doLogout();
  let reloginSettled = false;
  const relogin = doLogin().then(function() { reloginSettled = true; });
  await Promise.resolve();
  const raceBeforeRelease = {
    reloginSettled,
    calls: initCalls.slice(),
    timerEvents: timerEvents.slice(),
  };
  releaseOldRun();
  activeViewGate = null;
  await oldRun;
  await relogin;
  const raceAfterRelease = {
    reloginSettled,
    calls: initCalls.slice(),
    timerEvents: timerEvents.slice(),
  };

  stopAuthenticatedDashboardPolling();
  timerEvents = [];
  initCalls = [];
  let resolveStaleStatus;
  pendingAuthStatus = new Promise(resolve => { resolveStaleStatus = resolve; });
  const staleCheck = checkAuth();
  await Promise.resolve();
  const generationBeforeStaleRelogin = _dashboardAuthGeneration;
  await doLogin();
  resolveStaleStatus({
    ok: true,
    async json() { return {authenticated: false}; },
  });
  pendingAuthStatus = null;
  const staleCheckResult = await staleCheck;
  const staleStatusRace = {
    staleCheckResult,
    generationDelta: _dashboardAuthGeneration - generationBeforeStaleRelogin,
    calls: initCalls.slice(),
    timerEvents: timerEvents.slice(),
    overlayDisplay: element('auth-overlay').style.display,
  };

  process.stdout.write(JSON.stringify({
    timersBeforeAuth,
    singleFlight,
    login,
    setup,
    recover,
    setupWithOptionalFailure,
    failedAuthCalls,
    raceBeforeRelease,
    raceAfterRelease,
    staleStatusRace,
    overlayDisplay: element('auth-overlay').style.display,
  }));
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout)
    expected = [
        "loadBuckets",
        "loadStatusBanner",
        "syncRestartRequirement",
        "refreshAnchorCounter",
        "checkEmptyMemoryBanner",
        "loadOwnerBadge",
        "pollHeartbeat",
        "pollCriticalErrors",
        "maybeShowOnboarding",
        "initSelfFab",
        "refreshAuthenticatedActiveView",
    ]

    assert result["singleFlight"] == {"samePromise": True, "calls": expected}
    assert result["timersBeforeAuth"] == []
    assert result["login"] == expected
    assert result["setup"] == expected
    assert result["recover"] == expected
    assert result["setupWithOptionalFailure"] == expected
    assert result["failedAuthCalls"] == []
    assert result["raceBeforeRelease"] == {
        "reloginSettled": False,
        "calls": expected,
        "timerEvents": [
            ["start", 15000],
            ["start", 30000],
            ["start", 60000],
            ["stop", 15000],
            ["stop", 30000],
            ["stop", 60000],
        ],
    }
    assert result["raceAfterRelease"] == {
        "reloginSettled": True,
        "calls": expected + expected,
        "timerEvents": [
            ["start", 15000],
            ["start", 30000],
            ["start", 60000],
            ["stop", 15000],
            ["stop", 30000],
            ["stop", 60000],
            ["start", 15000],
            ["start", 30000],
            ["start", 60000],
        ],
    }
    assert result["staleStatusRace"] == {
        "staleCheckResult": False,
        "generationDelta": 1,
        "calls": expected,
        "timerEvents": [
            ["start", 15000],
            ["start", 30000],
            ["start", 60000],
        ],
        "overlayDisplay": "none",
    }
    assert result["overlayDisplay"] == "none"
