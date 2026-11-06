"""密码 / 安全问题答案哈希：PBKDF2 + 旧格式兼容 + 登录静默升级。

对应安全加固 #5：历史用单轮 salt:sha256hex，auth 文件泄露即离线爆破。
改用 PBKDF2-HMAC-SHA256；旧格式仍能校验，并在校验成功时升级到新格式。
"""
import hashlib
import json
import os
import stat
import time

import pytest

import web._shared as sh


@pytest.fixture
def auth_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    return tmp_path


def _legacy_hash(secret: str) -> str:
    salt = "deadbeefdeadbeefdeadbeefdeadbeef"
    h = hashlib.sha256(f"{salt}:{secret}".encode()).hexdigest()
    return f"{salt}:{h}"


def test_new_hash_is_pbkdf2_format():
    stored = sh._hash_secret("hunter2")
    assert stored.startswith("pbkdf2_sha256$")
    parts = stored.split("$")
    assert len(parts) == 4 and int(parts[1]) >= 200_000


def test_pbkdf2_roundtrip():
    stored = sh._hash_secret("correct horse")
    assert sh._verify_secret("correct horse", stored)
    assert not sh._verify_secret("wrong horse", stored)


def test_legacy_hash_still_verifies():
    stored = _legacy_hash("oldpass")
    assert sh._verify_secret("oldpass", stored)
    assert not sh._verify_secret("nope", stored)


def test_needs_rehash_detects_legacy_and_weak():
    assert sh._needs_rehash(_legacy_hash("x")) is True
    assert sh._needs_rehash("") is True
    assert sh._needs_rehash("pbkdf2_sha256$1000$aa$bb") is True  # 迭代数过低
    assert sh._needs_rehash(sh._hash_secret("x")) is False


def test_password_save_and_verify_uses_pbkdf2(auth_dir):
    sh._save_password_hash("s3cret!")
    stored = sh._load_password_hash()
    assert stored.startswith("pbkdf2_sha256$")
    assert sh._verify_any_password("s3cret!")
    assert not sh._verify_any_password("bad")


def test_login_upgrades_legacy_hash(auth_dir):
    # 手写一个旧格式 auth 文件
    legacy = _legacy_hash("legacypw")
    (auth_dir / ".dashboard_auth.json").write_text(
        json.dumps({"password_hash": legacy}), encoding="utf-8"
    )
    assert sh._load_password_hash() == legacy
    # 用旧密码登录成功
    assert sh._verify_any_password("legacypw")
    # 成功后应已静默升级为 PBKDF2
    upgraded = sh._load_password_hash()
    assert upgraded.startswith("pbkdf2_sha256$")
    assert sh._verify_any_password("legacypw")


def test_security_answer_pbkdf2_and_legacy(auth_dir):
    sh._save_security_qa("你的城市？", "  ShangHai  ")
    # 答案归一化（strip+lower）后校验
    assert sh._verify_security_answer("shanghai")
    assert not sh._verify_security_answer("beijing")
    stored = sh._load_auth_data().get("security_answer_hash", "")
    assert stored.startswith("pbkdf2_sha256$")


def test_auth_material_is_written_with_private_permissions(auth_dir):
    sh._save_password_hash("private-secret")
    path = auth_dir / ".dashboard_auth.json"

    assert path.exists()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_environment_password_fingerprint_is_salted_slow_and_private(auth_dir):
    first = sh._derive_environment_password_fingerprint("env-secret")
    second = sh._derive_environment_password_fingerprint("env-secret")

    assert first.startswith(f"pbkdf2_sha256${sh._PBKDF2_ITERATIONS}$")
    assert sh._PBKDF2_ITERATIONS >= 200_000
    assert first != second
    assert sh._environment_password_fingerprint_matches("env-secret", first)
    assert sh._environment_password_fingerprint_matches("env-secret", second)
    assert not sh._environment_password_fingerprint_matches("env-secre", first)

    with sh._credential_state_guard():
        sh._write_environment_password_record_locked(first)
    path = auth_dir / ".dashboard_auth.json"
    stored = path.read_text(encoding="utf-8")
    assert "env-secret" not in stored
    assert hashlib.sha256(b"env-secret").hexdigest() not in stored
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_persisted_century_session_is_capped_to_current_ttl(
    auth_dir, monkeypatch
):
    monkeypatch.setenv("OMBRE_DASHBOARD_SESSION_DAYS", "30")
    now = time.time()
    token = "t" * 43
    (auth_dir / ".dashboard_sessions.json").write_text(
        json.dumps({token: now + 100 * 365 * 86400}), encoding="utf-8"
    )
    sh._sessions.clear()

    sh._load_sessions()

    assert now < sh._sessions[token] <= now + 30 * 86400 + 2
    sh._sessions.clear()


@pytest.mark.parametrize(
    ("raw", "days"),
    [("", 30), ("0", 1), ("9999", 365), ("invalid", 30)],
)
def test_dashboard_session_ttl_is_bounded(monkeypatch, raw, days):
    if raw:
        monkeypatch.setenv("OMBRE_DASHBOARD_SESSION_DAYS", raw)
    else:
        monkeypatch.delenv("OMBRE_DASHBOARD_SESSION_DAYS", raising=False)

    assert sh._session_ttl_seconds() == days * 86400


def test_session_registry_evicts_oldest_entry(auth_dir, monkeypatch):
    monkeypatch.setattr(sh, "_MAX_ACTIVE_SESSIONS", 2)
    monkeypatch.setenv("OMBRE_DASHBOARD_SESSION_DAYS", "30")
    now = time.time()
    sh._sessions.clear()
    sh._sessions.update({"a" * 43: now + 100, "b" * 43: now + 200})

    new_token = sh._create_session()

    assert len(sh._sessions) == 2
    assert "a" * 43 not in sh._sessions
    assert new_token in sh._sessions
    sh._sessions.clear()
