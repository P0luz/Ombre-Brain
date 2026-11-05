import json

import pytest

from web import auth as auth_web
from web import _shared as shared_web


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
    def __init__(self, body):
        self._body = body
        self.headers = {"Host": "localhost"}
        self.cookies = {}
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def json(self):
        return self._body


def _payload(response):
    return json.loads(response.body)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("密码", "密码", True),
        ("密码", "密碼", False),
        ("密码", "ascii", False),
        ("ascii", "ascii", True),
    ],
)
def test_constant_time_text_compare_accepts_unicode(left, right, expected):
    assert shared_web._constant_time_text_equal(left, right) is expected


def test_environment_password_rejects_unicode_without_type_error(monkeypatch):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "ascii-secret")

    assert shared_web._verify_password_for_rotation("错误密码") is None


@pytest.mark.asyncio
async def test_environment_password_login_succeeds_from_trusted_docker_gateway(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "proxy-secret")
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "172.17.0.1/32")
    monkeypatch.setitem(shared_web.config, "buckets_dir", str(tmp_path))
    shared_web._sessions.clear()
    shared_web._login_failures.clear()
    shared_web._login_locked_until.clear()
    shared_web._login_source_lru.clear()
    shared_web._login_global_attempts.clear()
    mcp = FakeMCP()
    auth_web.register(mcp)
    request = JsonRequest({"password": "proxy-secret"})
    request.headers = {
        "host": "ombre.example:18080",
        "x-forwarded-for": "198.51.100.23",
        "x-forwarded-host": "ombre.example:18080",
        "x-forwarded-proto": "https",
    }
    request.client = type("Client", (), {"host": "172.17.0.1"})()

    try:
        response = await mcp.routes[("POST", "/auth/login")](request)
    finally:
        shared_web._sessions.clear()
        shared_web._login_failures.clear()
        shared_web._login_locked_until.clear()
        shared_web._login_source_lru.clear()
        shared_web._login_global_attempts.clear()

    assert response.status_code == 200
    assert _payload(response) == {"ok": True}
    cookie = response.headers["set-cookie"]
    assert "ombre_session=" in cookie
    assert "Secure" in cookie


def test_remote_setup_rejects_unicode_token_without_type_error(monkeypatch):
    monkeypatch.setenv("OMBRE_SETUP_TOKEN", "ascii-secret")
    request = JsonRequest({})
    request.headers["X-Ombre-Setup-Token"] = "错误令牌"
    request.client = type("Client", (), {"host": "203.0.113.10"})()

    assert auth_web._setup_request_allowed(request) is False


@pytest.fixture
def auth_routes(monkeypatch, tmp_path):
    monkeypatch.setitem(shared_web.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(auth_web.sh, "_is_setup_needed", lambda: True)
    monkeypatch.setattr(auth_web.sh, "_require_auth", lambda _request: None)
    monkeypatch.setattr(auth_web.sh, "_load_auth_data", lambda: {
        "security_answer_hash": "configured"
    })
    monkeypatch.setattr(auth_web.sh, "_login_retry_after", lambda _request: 0)
    mcp = FakeMCP()
    auth_web.register(mcp)
    return mcp.routes


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/auth/setup", "/auth/change-password", "/auth/security-question"])
async def test_auth_mutations_reject_non_object_json(auth_routes, path):
    response = await auth_routes[("POST", path)](JsonRequest(["not", "an", "object"]))

    assert response.status_code == 400
    assert _payload(response)["error"] == "JSON body must be an object"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/auth/login", "/auth/recover"])
async def test_public_auth_mutations_count_non_object_json_as_failure(
    auth_routes, monkeypatch, path
):
    failures = []
    monkeypatch.setattr(
        auth_web.sh, "_record_login_failure", lambda request: failures.append(request)
    )

    response = await auth_routes[("POST", path)](JsonRequest("not-an-object"))

    assert response.status_code == 400
    if path == "/auth/login":
        assert failures
    else:
        import sqlite3
        with sqlite3.connect(shared_web.config["buckets_dir"] + "/.recovery_limits.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM attempts").fetchone()[0] == 1
        assert failures == []


@pytest.mark.asyncio
async def test_setup_rejects_oversized_password_without_saving(auth_routes, monkeypatch):
    saved = []
    monkeypatch.setattr(auth_web.sh, "_save_password_hash", saved.append)

    response = await auth_routes[("POST", "/auth/setup")](
        JsonRequest({"password": "x" * 1025})
    )

    assert response.status_code == 400
    assert saved == []


@pytest.mark.asyncio
async def test_security_question_requires_current_password_and_keeps_old_answer(tmp_path, monkeypatch):
    sh = shared_web
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    sh._login_global_attempts.clear()
    sh._save_password_hash("original-password")
    sh._save_security_qa("old question", "old answer")
    token = sh._create_session()
    mcp = FakeMCP()
    auth_web.register(mcp)
    for current in (None, "wrong-password", "original-password"):
        body = {"question": "new question", "answer": "new answer"}
        if current is not None:
            body["current"] = current
        request = JsonRequest(body)
        request.cookies["ombre_session"] = token
        response = await mcp.routes[("POST", "/auth/security-question")](request)
        if current == "original-password":
            assert response.status_code == 200
            assert sh._verify_security_answer("new answer")
        else:
            assert response.status_code in (400, 401)
            assert sh._verify_security_answer("old answer")


@pytest.mark.asyncio
async def test_recovery_limit_does_not_block_correct_login(tmp_path, monkeypatch):
    sh = shared_web
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    sh._login_global_attempts.clear()
    sh._save_password_hash("original-password")
    sh._save_security_qa("question", "answer")
    mcp = FakeMCP()
    auth_web.register(mcp)
    for _ in range(5):
        response = await mcp.routes[("POST", "/auth/recover")](
            JsonRequest({"answer": "wrong", "new_password": "new-password"})
        )
        assert response.status_code == 401
    response = await mcp.routes[("POST", "/auth/recover")](
        JsonRequest({"answer": "answer", "new_password": "new-password"})
    )
    assert response.status_code == 429
    response = await mcp.routes[("POST", "/auth/login")](
        JsonRequest({"password": "original-password"})
    )
    assert response.status_code == 200


def test_recovery_budget_is_atomic_persistent_and_expires(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setitem(shared_web.config, "buckets_dir", str(tmp_path))
    monkeypatch.setattr(auth_web.time, "time", lambda: 10000)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: auth_web._reserve_recovery_attempt(JsonRequest({})), range(12)))
    assert results.count(0) == 5
    assert auth_web._reserve_recovery_attempt(JsonRequest({})) == 900
    monkeypatch.setattr(auth_web.time, "time", lambda: 10900)
    assert auth_web._reserve_recovery_attempt(JsonRequest({})) == 0


def test_recovery_budget_survives_process_restart_and_concurrency(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    code = '''
import json, sys
from types import SimpleNamespace
from web import auth, _shared as sh
sh.config['buckets_dir'] = sys.argv[1]
request = SimpleNamespace(headers={}, client=SimpleNamespace(host='192.0.2.1'))
print(json.dumps([auth._reserve_recovery_attempt(request) for _ in range(4)]))
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        results.extend(json.loads(stdout))
    assert results.count(0) == 5
    restarted = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
                               capture_output=True, text=True, timeout=15, check=True)
    assert all(value > 0 for value in json.loads(restarted.stdout))


@pytest.mark.asyncio
async def test_corrupt_recovery_budget_fails_closed(auth_routes, tmp_path, monkeypatch):
    (tmp_path / ".recovery_limits.sqlite3").write_bytes(b"corrupt database")
    monkeypatch.setattr(shared_web, "_verify_security_answer_for_rotation",
                        lambda _: pytest.fail("损坏限流库时不能验证答案"))
    response = await auth_routes[("POST", "/auth/recover")](
        JsonRequest({"answer": "answer", "new_password": "new-password"}))
    assert response.status_code == 503


def test_recovery_total_budget_blocks_rotating_sources(tmp_path, monkeypatch):
    monkeypatch.setitem(shared_web.config, "buckets_dir", str(tmp_path))
    for number in range(21):
        request = JsonRequest({})
        request.client.host = f"192.0.2.{number + 1}"
        retry = auth_web._reserve_recovery_attempt(request)
        assert (retry == 0) == (number < 20)


@pytest.mark.asyncio
async def test_security_question_rejects_password_changed_during_save(tmp_path, monkeypatch):
    sh = shared_web
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    sh._login_global_attempts.clear()
    sh._save_password_hash("old-password")
    sh._save_security_qa("old question", "old answer")
    token = sh._create_session()
    original = sh._hash_secret
    def race(secret):
        if secret == "new answer":
            sh._save_password_hash("changed-password", keep_qa=True)
        return original(secret)
    monkeypatch.setattr(sh, "_hash_secret", race)
    mcp = FakeMCP()
    auth_web.register(mcp)
    request = JsonRequest({"question": "new question", "answer": "new answer", "current": "old-password"})
    request.cookies["ombre_session"] = token
    response = await mcp.routes[("POST", "/auth/security-question")](request)
    assert response.status_code == 409
    assert sh._verify_security_answer("old answer")
    assert sh._verify_password_for_rotation("changed-password")


@pytest.mark.asyncio
async def test_recover_does_not_clear_failures_for_invalid_new_password(
    auth_routes, monkeypatch
):
    successes = []
    saved = []
    monkeypatch.setattr(
        auth_web.sh,
        "_verify_security_answer_for_rotation",
        lambda _answer: object(),
    )
    monkeypatch.setattr(
        auth_web.sh, "_record_login_success", lambda request: successes.append(request)
    )
    monkeypatch.setattr(auth_web.sh, "_save_password_hash", lambda *args, **kwargs: saved.append(args))

    response = await auth_routes[("POST", "/auth/recover")](
        JsonRequest({"answer": "correct", "new_password": "short"})
    )

    assert response.status_code == 400
    assert successes == []
    assert saved == []
