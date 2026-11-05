import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import pytest

from web import _shared as sh
from web import auth as auth_web


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
    def __init__(self, body, *, client_host="198.51.100.10"):
        self._body = body
        self.headers = {}
        self.cookies = {}
        self.client = SimpleNamespace(host=client_host)

    async def json(self):
        return self._body


@pytest.fixture(autouse=True)
def clear_login_pressure_state(tmp_path, monkeypatch):
    monkeypatch.setitem(sh.config, "buckets_dir", str(tmp_path))
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    if hasattr(sh, "_login_source_lru"):
        sh._login_source_lru.clear()
    if hasattr(sh, "_login_global_attempts"):
        sh._login_global_attempts.clear()
    yield
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    if hasattr(sh, "_login_source_lru"):
        sh._login_source_lru.clear()
    if hasattr(sh, "_login_global_attempts"):
        sh._login_global_attempts.clear()


def request_for(host, *, forwarded=""):
    headers = {"x-forwarded-for": forwarded} if forwarded else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_ipv6_login_sources_are_normalized_to_a_64_prefix(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128")

    first = sh._client_key(request_for("2001:db8:1234:5678::1"))
    same_subnet = sh._client_key(request_for("2001:db8:1234:5678:ffff::2"))
    other_subnet = sh._client_key(request_for("2001:db8:1234:5679::1"))

    assert first == "2001:db8:1234:5678::/64"
    assert same_subnet == first
    assert other_subnet == "2001:db8:1234:5679::/64"


def test_ipv4_mapped_ipv6_keeps_the_underlying_ipv4_identity():
    request = request_for("::ffff:192.0.2.44")

    assert sh._client_key(request) == "192.0.2.44"


def test_login_source_tracking_is_lru_bounded(monkeypatch):
    monkeypatch.setattr(sh, "_LOGIN_MAX_TRACKED_SOURCES", 4, raising=False)

    for offset in range(20):
        sh._record_login_failure(request_for(f"198.51.100.{offset + 1}"))

    assert len(sh._login_failures) <= 4
    assert len(sh._login_locked_until) <= 4
    assert len(sh._login_source_lru) <= 4


def test_login_source_tracking_prunes_inactive_entries(monkeypatch):
    now = {"value": 1_000.0}
    monkeypatch.setattr(sh.time, "time", lambda: now["value"])
    stale = request_for("198.51.100.8")
    sh._record_login_failure(stale)
    stale_key = sh._client_key(stale)

    now["value"] += max(
        sh._LOGIN_WINDOW_SECONDS,
        sh._LOGIN_MAX_LOCK_SECONDS,
        getattr(sh, "_LOGIN_SOURCE_TTL_SECONDS", 0),
    ) + 1
    sh._login_retry_after(request_for("198.51.100.9"))

    assert stale_key not in sh._login_failures
    assert stale_key not in sh._login_locked_until
    assert stale_key not in sh._login_source_lru


def test_global_login_attempt_rate_has_a_bounded_window(monkeypatch):
    now = {"value": 10_000.0}
    monkeypatch.setattr(sh.time, "time", lambda: now["value"])
    monkeypatch.setattr(sh, "_LOGIN_GLOBAL_MAX_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(sh, "_LOGIN_GLOBAL_WINDOW_SECONDS", 30, raising=False)

    assert sh._reserve_global_login_attempt() == 0
    assert sh._reserve_global_login_attempt() == 0
    assert sh._reserve_global_login_attempt() > 0
    assert len(sh._login_global_attempts) == 2

    now["value"] += 31
    assert sh._reserve_global_login_attempt() == 0
    assert len(sh._login_global_attempts) == 1


def test_global_login_admission_is_atomic_across_threads(monkeypatch):
    monkeypatch.setattr(sh, "_LOGIN_GLOBAL_MAX_ATTEMPTS", 7, raising=False)
    monkeypatch.setattr(sh, "_LOGIN_GLOBAL_WINDOW_SECONDS", 60, raising=False)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(
            pool.map(
                lambda _index: sh._reserve_global_login_attempt(),
                range(100),
            )
        )

    assert sum(result == 0 for result in results) == 7
    assert len(sh._login_global_attempts) == 7


@pytest.mark.parametrize(
    ("path", "body", "verifier_name"),
    [
        (
            "/auth/login",
            {"password": "wrong-password"},
            "_verify_password_for_rotation",
        ),
        (
            "/auth/recover",
            {"answer": "wrong-answer", "new_password": "new-password"},
            "_verify_security_answer_for_rotation",
        ),
    ],
)
@pytest.mark.asyncio
async def test_public_pbkdf2_verification_does_not_block_event_loop(
    monkeypatch, path, body, verifier_name
):
    started = threading.Event()
    release = threading.Event()

    def slow_verifier(_secret):
        started.set()
        release.wait(timeout=1)
        return False

    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(auth_web.sh, "_login_retry_after", lambda _request: 0)
    monkeypatch.setattr(
        auth_web.sh,
        "_reserve_global_login_attempt",
        lambda: 0,
        raising=False,
    )
    monkeypatch.setattr(auth_web.sh, verifier_name, slow_verifier)
    monkeypatch.setattr(
        auth_web.sh,
        "_load_auth_data",
        lambda: {"security_answer_hash": "configured"},
    )
    monkeypatch.setattr(auth_web.sh, "_record_login_failure", lambda _request: None)
    mcp = FakeMCP()
    auth_web.register(mcp)
    handler = mcp.routes[("POST", path)]

    watchdog = threading.Timer(0.25, release.set)
    watchdog.start()
    started_at = time.perf_counter()
    task = asyncio.create_task(handler(JsonRequest(body)))
    await asyncio.sleep(0.01)
    event_loop_delay = time.perf_counter() - started_at

    assert started.is_set()
    assert event_loop_delay < 0.1
    release.set()
    response = await task
    watchdog.cancel()
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "body", "verifier_name"),
    [
        (
            "/auth/login",
            {"password": "wrong-password"},
            "_verify_password_for_rotation",
        ),
        (
            "/auth/recover",
            {"answer": "wrong-answer", "new_password": "new-password"},
            "_verify_security_answer_for_rotation",
        ),
    ],
)
@pytest.mark.asyncio
async def test_global_rate_limit_sheds_work_before_password_verification(
    monkeypatch, path, body, verifier_name
):
    monkeypatch.delenv("OMBRE_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(auth_web.sh, "_login_retry_after", lambda _request: 0)
    monkeypatch.setattr(auth_web.sh, "_reserve_global_login_attempt", lambda: 17)
    monkeypatch.setattr(auth_web, "_reserve_recovery_attempt", lambda _request: 17)
    monkeypatch.setattr(
        auth_web.sh,
        verifier_name,
        lambda _secret: pytest.fail("rate-limited requests must not run PBKDF2"),
    )
    monkeypatch.setattr(
        auth_web.sh,
        "_load_auth_data",
        lambda: {"security_answer_hash": "configured"},
    )
    mcp = FakeMCP()
    auth_web.register(mcp)

    response = await mcp.routes[("POST", path)](JsonRequest(body))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"


@pytest.mark.asyncio
async def test_same_source_concurrency_cannot_queue_past_its_lockout(monkeypatch):
    monkeypatch.setattr(
        auth_web,
        "_password_work_semaphore",
        asyncio.Semaphore(2),
    )
    monkeypatch.setattr(auth_web.sh, "_reserve_global_login_attempt", lambda: 0)
    verifier_calls = {"count": 0}

    def wrong_password(_secret):
        verifier_calls["count"] += 1
        time.sleep(0.01)
        return False

    monkeypatch.setattr(
        auth_web.sh, "_verify_password_for_rotation", wrong_password
    )
    mcp = FakeMCP()
    auth_web.register(mcp)
    login = mcp.routes[("POST", "/auth/login")]

    responses = await asyncio.gather(
        *(login(JsonRequest({"password": "wrong"})) for _ in range(20))
    )

    statuses = [response.status_code for response in responses]
    assert 429 in statuses
    assert verifier_calls["count"] <= sh._LOGIN_MAX_FAILURES + 1
    assert sh._login_retry_after(JsonRequest({})) > 0


@pytest.mark.asyncio
async def test_password_worker_has_one_process_wide_concurrency_ceiling(monkeypatch):
    monkeypatch.setattr(
        auth_web,
        "_password_work_semaphore",
        asyncio.Semaphore(2),
        raising=False,
    )
    release = threading.Event()
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0, "started": 0}

    def blocking_work(value):
        with state_lock:
            state["active"] += 1
            state["started"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        try:
            release.wait(timeout=1)
            return value
        finally:
            with state_lock:
                state["active"] -= 1

    tasks = [
        asyncio.create_task(auth_web._run_password_work(blocking_work, value))
        for value in range(5)
    ]
    for _ in range(100):
        with state_lock:
            started = state["started"]
        if started >= 2:
            break
        await asyncio.sleep(0.005)

    assert started == 2
    assert state["maximum"] == 2
    release.set()
    assert await asyncio.gather(*tasks) == list(range(5))
    assert state["maximum"] == 2


def test_password_worker_ceiling_is_shared_across_event_loops(monkeypatch):
    monkeypatch.setattr(
        auth_web,
        "_password_work_semaphore",
        auth_web._CrossLoopSemaphore(2),
    )
    state_lock = threading.Lock()
    state = {"active": 0, "maximum": 0}

    def blocking_work(value):
        with state_lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        try:
            time.sleep(0.03)
            return value
        finally:
            with state_lock:
                state["active"] -= 1

    def run_one(value):
        return asyncio.run(auth_web._run_password_work(blocking_work, value))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run_one, range(12)))

    assert results == list(range(12))
    assert state["maximum"] == 2


@pytest.mark.asyncio
async def test_cancelled_password_request_keeps_slot_until_worker_finishes(monkeypatch):
    monkeypatch.setattr(
        auth_web,
        "_password_work_semaphore",
        asyncio.Semaphore(1),
        raising=False,
    )
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def first_work():
        first_started.set()
        release_first.wait(timeout=1)
        return "first"

    def second_work():
        second_started.set()
        return "second"

    first = asyncio.create_task(auth_web._run_password_work(first_work))
    assert await asyncio.to_thread(first_started.wait, 0.5)
    first.cancel()
    await asyncio.sleep(0)
    # Repeated cancellation must not release the slot while the executor thread
    # is still burning CPU.
    first.cancel()
    second = asyncio.create_task(auth_web._run_password_work(second_work))
    await asyncio.sleep(0.05)

    assert second_started.is_set() is False
    release_first.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == "second"
