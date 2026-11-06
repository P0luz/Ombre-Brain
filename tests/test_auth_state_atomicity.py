import asyncio
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from web import _shared as sh
from web import auth as auth_web
from web import oauth as oauth_web


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
    def __init__(self, body=None, *, cookies=None):
        self._body = {} if body is None else body
        self.headers = {}
        self.cookies = cookies or {}
        self.client = type("Client", (), {"host": "127.0.0.1"})()

    async def json(self):
        return self._body


@pytest.fixture
def isolated_auth_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    _clear_auth_runtime_state()
    yield tmp_path
    _clear_auth_runtime_state()


def _clear_auth_runtime_state():
    sh._sessions.clear()
    oauth_web._oauth_codes.clear()
    oauth_web._mcp_tokens.clear()
    oauth_web._mcp_token_resources.clear()
    oauth_web._mcp_refresh_tokens.clear()


def test_security_question_update_cannot_restore_a_concurrent_old_password(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "password_hash": "hash:old-password",
                "security_question": "old question",
                "security_answer_hash": "hash:old-answer",
            }
        ),
        encoding="utf-8",
    )
    answer_hash_started = threading.Event()
    allow_answer_hash = threading.Event()

    def controlled_hash(secret):
        if secret == "new-answer":
            answer_hash_started.set()
            assert allow_answer_hash.wait(timeout=2)
        return f"hash:{secret}"

    monkeypatch.setattr(sh, "_hash_secret", controlled_hash)
    with ThreadPoolExecutor(max_workers=1) as executor:
        qa_update = executor.submit(
            sh._save_security_qa, "new question", "new-answer"
        )
        assert answer_hash_started.wait(timeout=2)
        sh._save_password_hash("new-password")
        allow_answer_hash.set()
        qa_update.result(timeout=2)

    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["password_hash"] == "hash:new-password"
    assert saved["security_question"] == "new question"
    assert saved["security_answer_hash"] == "hash:new-answer"


def test_legacy_rehash_is_compare_and_swap_not_a_password_rollback(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps({"password_hash": "legacy-hash"}), encoding="utf-8"
    )
    rehash_ready = threading.Event()
    allow_rehash = threading.Event()

    monkeypatch.setattr(
        sh,
        "_verify_secret",
        lambda secret, stored: secret == "old-password" and stored == "legacy-hash",
    )

    def controlled_needs_rehash(stored):
        assert stored == "legacy-hash"
        rehash_ready.set()
        assert allow_rehash.wait(timeout=2)
        return True

    monkeypatch.setattr(sh, "_needs_rehash", controlled_needs_rehash)
    monkeypatch.setattr(sh, "_hash_secret", lambda secret: f"hash:{secret}")

    with ThreadPoolExecutor(max_workers=1) as executor:
        old_login = executor.submit(sh._verify_any_password, "old-password")
        assert rehash_ready.wait(timeout=2)
        sh._save_password_hash("new-password")
        allow_rehash.set()
        assert old_login.result(timeout=2) is False

    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["password_hash"] == "hash:new-password"


def test_setup_lock_serializes_different_event_loops():
    lock = auth_web._CrossLoopSemaphore(1)
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0}

    async def critical_section():
        async with lock:
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            await asyncio.sleep(0.02)
            with state_lock:
                state["active"] -= 1

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _index: asyncio.run(critical_section()), range(8)))

    assert state["maximum"] == 1


def test_session_creation_rolls_back_when_private_state_cannot_persist(
    isolated_auth_dir, monkeypatch
):
    existing = "e" * 43
    sh._sessions[existing] = time.time() + 60
    monkeypatch.setattr(
        sh,
        "_atomic_write_private_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(sh.AuthPersistenceError):
        sh._create_session()

    assert list(sh._sessions) == [existing]


def test_authorization_code_exchange_is_single_use_across_threads(
    isolated_auth_dir, monkeypatch
):
    code_data = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "code_challenge": "",
        "resource": "https://ombre.example/mcp",
        "scope": "mcp",
        "expires": time.time() + 60,
    }
    oauth_web._oauth_codes["one-code"] = dict(code_data)
    monkeypatch.setattr(
        oauth_web, "_persist_mcp_token_state", lambda *_args, **_kwargs: None
    )

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(
                lambda _index: oauth_web._commit_authorization_code_exchange(
                    "one-code", code_data, "https://ombre.example/mcp"
                ),
                range(20),
            )
        )

    assert sum(result is not None for result in results) == 1
    assert "one-code" not in oauth_web._oauth_codes
    assert len(oauth_web._mcp_tokens) == 1
    assert len(oauth_web._mcp_refresh_tokens) == 1


def test_refresh_rotation_is_single_use_across_threads(
    isolated_auth_dir, monkeypatch
):
    refresh_data = {
        "client_id": "client-1",
        "resource": "https://ombre.example/mcp",
        "expires": time.time() + 60,
    }
    oauth_web._mcp_refresh_tokens["one-refresh"] = dict(refresh_data)
    monkeypatch.setattr(
        oauth_web, "_persist_mcp_token_state", lambda *_args, **_kwargs: None
    )

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(
                lambda _index: oauth_web._commit_refresh_token_rotation(
                    "one-refresh", refresh_data, "https://ombre.example/mcp"
                ),
                range(20),
            )
        )

    assert sum(result is not None for result in results) == 1
    assert "one-refresh" not in oauth_web._mcp_refresh_tokens
    assert len(oauth_web._mcp_tokens) == 1
    assert len(oauth_web._mcp_refresh_tokens) == 1


def test_oauth_revoke_does_not_claim_success_when_persistence_fails(
    isolated_auth_dir, monkeypatch
):
    oauth_web._mcp_tokens["access"] = time.time() + 60
    oauth_web._mcp_refresh_tokens["refresh"] = {
        "client_id": "client-1",
        "expires": time.time() + 60,
        "resource": "https://ombre.example/mcp",
    }
    monkeypatch.setattr(
        sh,
        "_atomic_write_private_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    with pytest.raises(oauth_web.OAuthPersistenceError):
        oauth_web.revoke_all_mcp_grants()

    assert "access" in oauth_web._mcp_tokens
    assert "refresh" in oauth_web._mcp_refresh_tokens


def test_oauth_revoke_invalidates_inflight_authorization_generation(
    isolated_auth_dir, monkeypatch
):
    generation = oauth_web._oauth_grant_generation_snapshot()
    monkeypatch.setattr(
        oauth_web, "_persist_mcp_token_state", lambda *_args, **_kwargs: None
    )

    oauth_web.revoke_all_mcp_grants()
    stored = oauth_web._store_authorization_code(
        "late-code",
        {"expires": time.time() + 60},
        generation,
    )

    assert stored is False
    assert "late-code" not in oauth_web._oauth_codes


@pytest.mark.asyncio
async def test_logout_returns_503_and_keeps_session_when_revoke_is_not_durable(
    isolated_auth_dir, monkeypatch
):
    token = "s" * 43
    sh._sessions[token] = time.time() + 60
    monkeypatch.setattr(
        sh,
        "_persist_sessions_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sh.AuthPersistenceError("read only")
        ),
    )
    mcp = FakeMCP()
    auth_web.register(mcp)

    response = await mcp.routes[("POST", "/auth/logout")](
        JsonRequest(cookies={"ombre_session": token})
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert token in sh._sessions


@pytest.mark.asyncio
async def test_password_change_stops_before_write_when_session_revoke_fails(
    isolated_auth_dir, monkeypatch
):
    saved_passwords = []
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps({"password_hash": "hash:old-password"}),
        encoding="utf-8",
    )
    proof = sh.CredentialProof(
        "password_hash",
        "hash:old-password",
        sh._credential_generation_snapshot(),
    )
    monkeypatch.setattr(
        sh, "_verify_password_for_rotation", lambda _password: proof
    )
    monkeypatch.setattr(sh, "_hash_secret", lambda value: f"hash:{value}")
    monkeypatch.setattr(
        sh,
        "_revoke_all_sessions",
        lambda: (_ for _ in ()).throw(sh.AuthPersistenceError("disk full")),
    )
    monkeypatch.setattr(
        sh,
        "_save_prehashed_password",
        lambda password, **_kwargs: saved_passwords.append(password),
    )
    mcp = FakeMCP()
    auth_web.register(mcp)

    response = await mcp.routes[("POST", "/auth/change-password")](
        JsonRequest({"current": "old-password", "new": "new-password"})
    )

    assert response.status_code == 503
    assert saved_passwords == []


def test_concurrent_old_password_rotations_have_exactly_one_winner(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "password_hash": "hash:old-password",
                "security_question": "question",
                "security_answer_hash": "hash:answer",
            }
        ),
        encoding="utf-8",
    )
    generation = sh._credential_generation_snapshot()
    proof_one = sh.CredentialProof(
        "password_hash", "hash:old-password", generation
    )
    proof_two = sh.CredentialProof(
        "password_hash", "hash:old-password", generation
    )
    monkeypatch.setattr(
        oauth_web, "_persist_mcp_token_state", lambda *_args: None
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: auth_web._commit_password_rotation(*item),
                [
                    (proof_one, "hash:new-password-one"),
                    (proof_two, "hash:new-password-two"),
                ],
            )
        )

    assert sum(result is not None for result in results) == 1
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["password_hash"] in {
        "hash:new-password-one",
        "hash:new-password-two",
    }
    assert saved["security_answer_hash"] == "hash:answer"
    assert len(sh._sessions) == 1


def test_oauth_persistence_failure_cannot_publish_new_password_with_old_grants(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps({"password_hash": "hash:old-password"}),
        encoding="utf-8",
    )
    proof = sh.CredentialProof(
        "password_hash",
        "hash:old-password",
        sh._credential_generation_snapshot(),
    )
    oauth_web._mcp_tokens["old-access"] = time.time() + 60
    oauth_web._mcp_refresh_tokens["old-refresh"] = {
        "client_id": "client-1",
        "resource": "https://ombre.example/mcp",
        "expires": time.time() + 60,
    }
    monkeypatch.setattr(
        oauth_web,
        "_persist_mcp_token_state",
        lambda *_args: (_ for _ in ()).throw(
            oauth_web.OAuthPersistenceError("disk full")
        ),
    )

    with pytest.raises(oauth_web.OAuthPersistenceError):
        auth_web._commit_password_rotation(proof, "hash:new-password")

    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["password_hash"] == "hash:old-password"
    assert "old-access" in oauth_web._mcp_tokens
    assert "old-refresh" in oauth_web._mcp_refresh_tokens


def test_rotation_window_blocks_stale_code_and_grant_publication(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps({"password_hash": "hash:old-password"}),
        encoding="utf-8",
    )
    proof = sh.CredentialProof(
        "password_hash",
        "hash:old-password",
        sh._credential_generation_snapshot(),
    )
    code_data = {
        "client_id": "client-1",
        "redirect_uri": "https://client.example/callback",
        "code_challenge": "",
        "resource": "https://ombre.example/mcp",
        "scope": "mcp",
        "expires": time.time() + 60,
        "credential_generation": proof.generation,
    }
    oauth_web._oauth_codes["pre-rotation-code"] = dict(code_data)

    revoke_persist_started = threading.Event()
    release_revoke = threading.Event()
    store_attempted = threading.Event()
    exchange_attempted = threading.Event()

    def blocking_persist(*_args):
        revoke_persist_started.set()
        assert release_revoke.wait(timeout=2)

    def store_late_code():
        store_attempted.set()
        return oauth_web._store_authorization_code(
            "late-code", {"expires": time.time() + 60}, proof
        )

    def exchange_old_code():
        exchange_attempted.set()
        return oauth_web._commit_authorization_code_exchange(
            "pre-rotation-code",
            code_data,
            "https://ombre.example/mcp",
        )

    monkeypatch.setattr(oauth_web, "_persist_mcp_token_state", blocking_persist)
    with ThreadPoolExecutor(max_workers=3) as executor:
        rotation = executor.submit(
            auth_web._commit_password_rotation,
            proof,
            "hash:new-password",
        )
        assert revoke_persist_started.wait(timeout=2)
        late_code = executor.submit(store_late_code)
        old_exchange = executor.submit(exchange_old_code)
        assert store_attempted.wait(timeout=2)
        assert exchange_attempted.wait(timeout=2)
        assert not late_code.done()
        assert not old_exchange.done()
        release_revoke.set()

        assert rotation.result(timeout=2) is not None
        assert late_code.result(timeout=2) is False
        assert old_exchange.result(timeout=2) is None

    assert oauth_web._oauth_codes == {}
    assert oauth_web._mcp_tokens == {}
    assert oauth_web._mcp_refresh_tokens == {}


def test_stale_security_answer_proof_cannot_overwrite_new_recovery_state(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "password_hash": "hash:old-password",
                "security_question": "old question",
                "security_answer_hash": "hash:old-answer",
            }
        ),
        encoding="utf-8",
    )
    old_answer_proof = sh.CredentialProof(
        "security_answer_hash",
        "hash:old-answer",
        sh._credential_generation_snapshot(),
    )
    monkeypatch.setattr(sh, "_hash_secret", lambda value: f"hash:{value}")

    assert sh._save_security_qa("new question", "new-answer") is True
    result = auth_web._commit_password_rotation(
        old_answer_proof, "hash:attacker-password"
    )

    assert result is None
    saved = json.loads(auth_file.read_text(encoding="utf-8"))
    assert saved["password_hash"] == "hash:old-password"
    assert saved["security_question"] == "new question"
    assert saved["security_answer_hash"] == "hash:new-answer"


def test_stale_password_proof_cannot_issue_dashboard_session(
    isolated_auth_dir, monkeypatch
):
    auth_file = isolated_auth_dir / ".dashboard_auth.json"
    auth_file.write_text(
        json.dumps({"password_hash": "hash:old-password"}),
        encoding="utf-8",
    )
    proof = sh.CredentialProof(
        "password_hash",
        "hash:old-password",
        sh._credential_generation_snapshot(),
    )
    monkeypatch.setattr(sh, "_hash_secret", lambda value: f"hash:{value}")

    assert sh._save_password_hash("new-password") is True

    assert sh._create_session_for_credential(proof) is None
    assert sh._sessions == {}


# --- 环境变量口令换了：启动时撤销旧授权 ---

_RESOURCE = "https://ombre.example/mcp"


@pytest.fixture
def env_reconcile(isolated_auth_dir, monkeypatch):
    """隔离目录和 config.yaml + 端口探测固定为“没有旧进程” + 清空并在结束后还原客户端表。"""
    monkeypatch.setattr(auth_web, "_listen_port_unavailable", lambda *_args: False)
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(isolated_auth_dir / "config.yaml"))
    monkeypatch.delenv("OMBRE_MCP_TOKEN", raising=False)
    monkeypatch.setitem(sh.config, "mcp_token", "")
    saved_clients = dict(oauth_web._oauth_clients)
    oauth_web._oauth_clients.clear()
    yield isolated_auth_dir
    oauth_web._oauth_clients.clear()
    oauth_web._oauth_clients.update(saved_clients)
    sh._sessions.clear()


def _reconcile():
    return auth_web.reconcile_environment_password_at_startup("127.0.0.1", 9)


def _auth_data(directory):
    path = directory / ".dashboard_auth.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _record_fingerprint(record):
    with sh._credential_state_guard():
        sh._write_environment_password_record_locked(record)


def _issue_grants():
    session = sh._create_session()
    access = oauth_web._issue_mcp_access_token(_RESOURCE)
    refresh = oauth_web._issue_mcp_refresh_token("client-1", _RESOURCE)
    oauth_web._oauth_codes["pending-code"] = {"expires": time.time() + 60}
    oauth_web._save_mcp_tokens()
    return session, access, refresh


def _assert_all_grants_revoked(directory, session, access, refresh):
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session})) is False
    assert oauth_web._is_valid_mcp_token(access) is False
    assert refresh not in oauth_web._mcp_refresh_tokens
    assert oauth_web._oauth_codes == {}
    sessions_file = json.loads(
        (directory / ".dashboard_sessions.json").read_text(encoding="utf-8")
    )
    tokens_file = json.loads(
        (directory / ".dashboard_mcp_tokens.json").read_text(encoding="utf-8")
    )
    assert sessions_file == {}
    assert tokens_file == {"access_tokens": {}, "refresh_tokens": {}}


def test_env_password_first_reconcile_only_records(env_reconcile, monkeypatch):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    session, access, refresh = _issue_grants()

    assert _reconcile() == "recorded"

    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches("password-a", record)
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session}))
    assert oauth_web._is_valid_mcp_token(access) is True
    assert refresh in oauth_web._mcp_refresh_tokens


def test_env_password_first_reconcile_warns_with_counts_only(
    env_reconcile, monkeypatch, caplog
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _issue_grants()

    with caplog.at_level("WARNING", logger="ombre_brain"):
        assert _reconcile() == "recorded"

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("1 个" in message and "2 个" in message for message in warnings)
    assert all("password-a" not in r.getMessage() for r in caplog.records)
    assert all("pbkdf2_sha256" not in r.getMessage() for r in caplog.records)


def test_env_password_unchanged_does_not_revoke_or_rehash(env_reconcile, monkeypatch):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))
    session, access, refresh = _issue_grants()
    before = (env_reconcile / ".dashboard_auth.json").read_bytes()
    derived = []
    monkeypatch.setattr(
        sh,
        "_derive_environment_password_fingerprint",
        lambda value: derived.append(value) or "unused",
    )
    monkeypatch.setattr(
        sh, "_hash_secret", lambda _value: pytest.fail("口令没变时不该再派生")
    )

    assert _reconcile() == "unchanged"

    assert derived == []
    assert (env_reconcile / ".dashboard_auth.json").read_bytes() == before
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session}))
    assert oauth_web._is_valid_mcp_token(access) is True
    assert refresh in oauth_web._mcp_refresh_tokens


def test_env_password_change_revokes_everything_and_records_new_value(
    env_reconcile, monkeypatch
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))
    session, access, refresh = _issue_grants()
    clients_file = env_reconcile / ".oauth_clients.json"
    clients_file.write_text(json.dumps({"kept": {"note": "unchanged"}}), encoding="utf-8")
    generation = sh._credential_generation_snapshot()

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    assert _reconcile() == "revoked"

    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    assert sh._credential_generation_snapshot() > generation
    assert json.loads(clients_file.read_text(encoding="utf-8")) == {
        "kept": {"note": "unchanged"}
    }
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches("password-b", record)
    assert not sh._environment_password_fingerprint_matches("password-a", record)
    assert not (env_reconcile / "config.yaml").exists()


def test_switch_from_env_password_to_file_password_revokes(env_reconcile, monkeypatch):
    sh._save_password_hash("file-password")
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))
    session, access, refresh = _issue_grants()

    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD")
    assert _reconcile() == "revoked"

    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    saved = _auth_data(env_reconcile)
    assert saved[sh._ENV_PASSWORD_FINGERPRINT_KEY] is None
    assert sh._verify_secret("file-password", saved["password_hash"])


def test_removing_env_password_without_file_password_revokes_and_needs_setup(
    env_reconcile, monkeypatch
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))
    session, access, refresh = _issue_grants()

    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD")
    assert _reconcile() == "revoked"

    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] is None
    assert sh._is_setup_needed() is True


def test_switch_from_file_password_to_env_password_revokes(env_reconcile, monkeypatch):
    sh._save_password_hash("file-password")
    _record_fingerprint(None)
    session, access, refresh = _issue_grants()

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    assert _reconcile() == "revoked"

    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches("password-a", record)


def test_file_password_mode_reconcile_does_no_work(env_reconcile, monkeypatch):
    sh._save_password_hash("file-password")
    _record_fingerprint(None)
    session, _access, _refresh = _issue_grants()
    before = (env_reconcile / ".dashboard_auth.json").read_bytes()
    monkeypatch.setattr(
        sh,
        "_derive_environment_password_fingerprint",
        lambda _value: pytest.fail("文件口令模式不该派生环境口令指纹"),
    )

    assert _reconcile() == "unchanged"

    assert (env_reconcile / ".dashboard_auth.json").read_bytes() == before
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session}))


@pytest.mark.parametrize(
    "corrupt_record",
    [
        "deadbeefdeadbeefdeadbeefdeadbeef:"
        + hashlib.sha256(
            b"deadbeefdeadbeefdeadbeefdeadbeef:password-a"
        ).hexdigest(),
        "salt:" + hashlib.sha256(b"password-a").hexdigest(),
        12345,
        ["pbkdf2_sha256"],
        "pbkdf2_sha256$999999999$00$00",
        "pbkdf2_sha256$0$00$00",
        "pbkdf2_sha256$240000$zz$00",
    ],
)
def test_corrupt_fingerprint_counts_as_changed(env_reconcile, monkeypatch, corrupt_record):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(corrupt_record)
    session, access, refresh = _issue_grants()

    assert _reconcile() == "revoked"

    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert record.startswith(f"pbkdf2_sha256${sh._PBKDF2_ITERATIONS}$")
    assert sh._environment_password_fingerprint_matches("password-a", record)


def test_revoke_persistence_failure_clears_memory_and_retries_next_start(
    env_reconcile, monkeypatch, caplog
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    old_record = sh._derive_environment_password_fingerprint("password-a")
    _record_fingerprint(old_record)
    session, access, refresh = _issue_grants()
    real_write = sh._atomic_write_private_json

    def failing_grant_writes(path, data):
        if path.endswith((".dashboard_sessions.json", ".dashboard_mcp_tokens.json")):
            raise OSError("read only")
        return real_write(path, data)

    monkeypatch.setattr(sh, "_atomic_write_private_json", failing_grant_writes)
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    with caplog.at_level("ERROR", logger="ombre_brain"):
        assert _reconcile() == "revoke_not_durable"

    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert sh._sessions == {}
    assert oauth_web._mcp_tokens == {}
    assert oauth_web._mcp_refresh_tokens == {}
    assert oauth_web._oauth_codes == {}
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] == old_record

    monkeypatch.setattr(sh, "_atomic_write_private_json", real_write)
    assert _reconcile() == "revoked"
    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches("password-b", record)


def test_fingerprint_write_failure_after_revoke_retries_next_start(
    env_reconcile, monkeypatch, caplog
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    old_record = sh._derive_environment_password_fingerprint("password-a")
    _record_fingerprint(old_record)
    session, access, refresh = _issue_grants()
    real_write = sh._atomic_write_private_json

    def failing_auth_write(path, data):
        if path.endswith(".dashboard_auth.json"):
            raise OSError("read only")
        return real_write(path, data)

    monkeypatch.setattr(sh, "_atomic_write_private_json", failing_auth_write)
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    with caplog.at_level("ERROR", logger="ombre_brain"):
        assert _reconcile() == "revoked_unrecorded"

    assert any(r.levelname == "ERROR" for r in caplog.records)
    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] == old_record

    monkeypatch.setattr(sh, "_atomic_write_private_json", real_write)
    assert _reconcile() == "revoked"
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches("password-b", record)


def test_unreadable_auth_file_with_env_password_revokes_until_fixed(
    env_reconcile, monkeypatch, caplog
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    session, access, refresh = _issue_grants()
    auth_file = env_reconcile / ".dashboard_auth.json"
    auth_file.write_text("{not json", encoding="utf-8")

    with caplog.at_level("ERROR", logger="ombre_brain"):
        assert _reconcile() == "revoked_unreadable"

    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert all("password-b" not in r.getMessage() for r in caplog.records)
    assert auth_file.read_text(encoding="utf-8") == "{not json"
    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    assert _reconcile() == "revoked_unreadable"
    assert auth_file.read_text(encoding="utf-8") == "{not json"


def test_unreadable_auth_file_without_env_password_skips(env_reconcile, caplog):
    session, access, refresh = _issue_grants()
    auth_file = env_reconcile / ".dashboard_auth.json"
    auth_file.write_text("{not json", encoding="utf-8")

    with caplog.at_level("ERROR", logger="ombre_brain"):
        assert _reconcile() == "skipped_unreadable"

    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert auth_file.read_text(encoding="utf-8") == "{not json"
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session}))
    assert oauth_web._is_valid_mcp_token(access) is True
    assert refresh in oauth_web._mcp_refresh_tokens


def _write_config_yaml(directory, data):
    import yaml

    (directory / "config.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )


def _config_yaml(directory):
    import yaml

    return yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))


def test_env_password_change_invalidates_config_static_mcp_token(
    env_reconcile, monkeypatch, caplog
):
    # 拿着旧口令登录过的人可以事先生成静态 Token、把模式存成共存，换口令重启时一起生效。
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))
    _write_config_yaml(
        env_reconcile,
        {"mcp_auth_mode": "hybrid", "mcp_token": "minted-before-rotation", "timezone": "UTC"},
    )
    monkeypatch.setitem(sh.config, "mcp_auth_mode", "hybrid")
    sh.config["mcp_token"] = "minted-before-rotation"
    assert oauth_web._is_valid_static_mcp_token("minted-before-rotation") is True
    session, access, refresh = _issue_grants()

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    with caplog.at_level("WARNING", logger="ombre_brain"):
        assert _reconcile() == "revoked"

    _assert_all_grants_revoked(env_reconcile, session, access, refresh)
    assert oauth_web._is_valid_static_mcp_token("minted-before-rotation") is False
    assert _config_yaml(env_reconcile) == {
        "mcp_auth_mode": "hybrid",
        "mcp_token": "",
        "timezone": "UTC",
    }
    messages = [r.getMessage() for r in caplog.records]
    assert any("mcp_token" in m and "重新生成" in m for m in messages)
    assert all("minted-before-rotation" not in m for m in messages)


def test_env_static_mcp_token_is_kept_and_config_copy_is_invalidated(
    env_reconcile, monkeypatch, caplog
):
    monkeypatch.setenv("OMBRE_MCP_TOKEN", "deployer-managed-token")
    sh.config["mcp_token"] = "deployer-managed-token"  # load_config 的环境变量覆盖结果
    _write_config_yaml(env_reconcile, {"mcp_token": "dormant-config-token"})
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    with caplog.at_level("WARNING", logger="ombre_brain"):
        assert _reconcile() == "revoked"

    assert oauth_web._is_valid_static_mcp_token("deployer-managed-token") is True
    assert _config_yaml(env_reconcile)["mcp_token"] == ""
    messages = [r.getMessage() for r in caplog.records]
    assert any("OMBRE_MCP_TOKEN" in m for m in messages)
    assert all("deployer-managed-token" not in m for m in messages)
    assert all("dormant-config-token" not in m for m in messages)


def test_static_token_persistence_failure_retries_next_start(
    env_reconcile, monkeypatch, caplog
):
    import utils

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    old_record = sh._derive_environment_password_fingerprint("password-a")
    _record_fingerprint(old_record)
    _write_config_yaml(env_reconcile, {"mcp_token": "minted-before-rotation"})
    sh.config["mcp_token"] = "minted-before-rotation"
    real_update = utils.atomic_update_config_yaml

    def read_only_config(_mutate):
        raise OSError("read only")

    monkeypatch.setattr(utils, "atomic_update_config_yaml", read_only_config)
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    with caplog.at_level("ERROR", logger="ombre_brain"):
        assert _reconcile() == "revoke_not_durable"

    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert oauth_web._is_valid_static_mcp_token("minted-before-rotation") is False
    assert _config_yaml(env_reconcile)["mcp_token"] == "minted-before-rotation"
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] == old_record

    # 下次启动：配置照旧装载出这把 Token；写盘恢复后撤销完成、记下新指纹。
    monkeypatch.setattr(utils, "atomic_update_config_yaml", real_update)
    sh.config["mcp_token"] = "minted-before-rotation"
    assert _reconcile() == "revoked"
    assert _config_yaml(env_reconcile)["mcp_token"] == ""
    assert oauth_web._is_valid_static_mcp_token("minted-before-rotation") is False
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches("password-b", record)


def test_revoke_without_static_token_leaves_config_yaml_untouched(
    env_reconcile, monkeypatch
):
    config_file = env_reconcile / "config.yaml"
    config_file.write_text("# 手写的注释\nmcp_auth_mode: oauth\n", encoding="utf-8")
    before = config_file.read_bytes()
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    assert _reconcile() == "revoked"

    assert config_file.read_bytes() == before


def test_matching_low_iteration_fingerprint_is_upgraded_without_revoking(
    env_reconcile, monkeypatch
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    salt = "00" * 16
    digest = hashlib.pbkdf2_hmac("sha256", b"password-a", bytes.fromhex(salt), 1000)
    weak = f"pbkdf2_sha256$1000${salt}${digest.hex()}"
    _record_fingerprint(weak)
    session, access, _refresh = _issue_grants()

    assert _reconcile() == "unchanged"

    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert record.startswith(f"pbkdf2_sha256${sh._PBKDF2_ITERATIONS}$")
    assert sh._environment_password_fingerprint_matches("password-a", record)
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session}))
    assert oauth_web._is_valid_mcp_token(access) is True


def test_whitespace_and_non_utf8_env_passwords_are_fingerprinted(
    env_reconcile, monkeypatch
):
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "   ")
    assert _reconcile() == "recorded"
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert isinstance(record, str)
    assert sh._environment_password_fingerprint_matches("   ", record)
    assert _reconcile() == "unchanged"

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "pass" + chr(0xDCFF) + "word")
    assert _reconcile() == "revoked"
    record = _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY]
    assert sh._environment_password_fingerprint_matches(
        "pass" + chr(0xDCFF) + "word", record
    )
    assert _reconcile() == "unchanged"


def test_password_and_security_qa_writes_keep_the_fingerprint(env_reconcile):
    _record_fingerprint("pbkdf2_sha256$240000$00$00")

    sh._save_password_hash("file-password")
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] == (
        "pbkdf2_sha256$240000$00$00"
    )
    sh._save_security_qa("question", "answer")
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] == (
        "pbkdf2_sha256$240000$00$00"
    )
    sh._save_password_hash("file-password-2", keep_qa=False)
    assert _auth_data(env_reconcile)[sh._ENV_PASSWORD_FINGERPRINT_KEY] == (
        "pbkdf2_sha256$240000$00$00"
    )


@pytest.mark.asyncio
async def test_fingerprint_only_auth_file_keeps_setup_and_recovery_state(
    env_reconcile,
):
    _record_fingerprint(None)
    mcp = FakeMCP()
    auth_web.register(mcp)

    response = await mcp.routes[("GET", "/auth/recovery-question")](JsonRequest())

    assert sh._is_setup_needed() is True
    assert json.loads(response.body) == {"question": None}


def test_reconcile_skips_while_another_process_holds_the_port(
    isolated_auth_dir, monkeypatch
):
    import socket
    import sys

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))
    session, access, refresh = _issue_grants()
    before = (isolated_auth_dir / ".dashboard_auth.json").read_bytes()
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    try:
        assert auth_web._listen_port_unavailable("127.0.0.1", port) is True
        if sys.platform.startswith("linux"):
            # Linux 上通配地址也和已被占用的具体地址冲突；其他系统的 SO_REUSEADDR
            # 规则不同（uvicorn 那边同样遵循），所以这一条只在 Linux 上断言。
            assert auth_web._listen_port_unavailable("0.0.0.0", port) is True
        assert (
            auth_web.reconcile_environment_password_at_startup("127.0.0.1", port)
            == "skipped_port_busy"
        )
    finally:
        listener.close()

    assert (isolated_auth_dir / ".dashboard_auth.json").read_bytes() == before
    assert sh._is_authenticated(JsonRequest(cookies={"ombre_session": session}))
    assert oauth_web._is_valid_mcp_token(access) is True
    assert refresh in oauth_web._mcp_refresh_tokens
    assert auth_web._listen_port_unavailable("127.0.0.1", port) is False
    sh._sessions.clear()


def test_listen_probe_skips_address_families_that_asyncio_skips(monkeypatch):
    import errno
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    unavailable = ("192.0.2.1", port)  # 文档专用地址，本机不会配
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
        try:
            check.bind(unavailable)
        except OSError as e:
            if e.errno != errno.EADDRNOTAVAIL:
                pytest.skip(f"绑定不存在的本机地址报的不是 EADDRNOTAVAIL：{e}")
        else:
            pytest.skip("本机配了 192.0.2.1")
    stream = socket.SOCK_STREAM
    resolved = [
        (12345, stream, 0, "", ("unsupported-family", port)),
        (socket.AF_INET, stream, 0, "", unavailable),
        (socket.AF_INET, stream, 0, "", ("127.0.0.1", port)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: list(resolved))

    assert auth_web._listen_port_unavailable("localhost", port) is False

    del resolved[2]
    assert auth_web._listen_port_unavailable("localhost", port) is True


def test_revoke_keeps_legacy_authorized_client_activated(env_reconcile, monkeypatch):
    monkeypatch.setitem(sh.config, "mcp_require_auth", True)
    monkeypatch.setitem(sh.config, "mcp_auth_mode", "oauth")
    expires = time.time() + 86400
    (env_reconcile / ".oauth_clients.json").write_text(
        json.dumps(
            {
                "legacy-client": {
                    "redirect_uris": ["https://client.example/callback"],
                    "client_name": "Legacy Authorized",
                    "expires": expires,
                }
            }
        ),
        encoding="utf-8",
    )
    oauth_web._issue_mcp_refresh_token("legacy-client", _RESOURCE)
    oauth_web._save_mcp_tokens()
    oauth_web._mcp_refresh_tokens.clear()
    oauth_web._load_mcp_tokens()
    oauth_web._load_oauth_clients()
    assert oauth_web._oauth_clients["legacy-client"]["activated"] is True
    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-a")
    _record_fingerprint(sh._derive_environment_password_fingerprint("password-a"))

    monkeypatch.setenv("OMBRE_DASHBOARD_PASSWORD", "password-b")
    assert _reconcile() == "revoked"

    oauth_web._oauth_clients.clear()
    oauth_web._load_mcp_tokens()
    oauth_web._load_oauth_clients()
    restored = oauth_web._oauth_clients["legacy-client"]
    assert restored["activated"] is True
    assert restored["expires"] == expires


def test_server_reconciles_before_serving_http_only():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "server.py"
    ).read_text(encoding="utf-8")
    main = source.index('if __name__ == "__main__":')
    http_branch = source.index('if transport == "streamable-http":', main)
    call = source.index("reconcile_environment_password_at_startup(", http_branch)
    stdio_branch = source.index('elif transport == "stdio":', http_branch)

    assert call < source.index("build_http_app(\n", http_branch)
    assert call < source.index("uvicorn.run(", http_branch) < stdio_branch
    assert source.count("reconcile_environment_password_at_startup(") == 1
    assert source[source.rfind("\n", 0, call) + 1 : call].strip() == ""
    assert source[source.rfind("\n", 0, call) + 1 : call] == " " * 8
