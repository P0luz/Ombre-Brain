"""登录失败限流 / 指数退避锁定回归测试（防在线密码爆破）。

对应安全加固 #4：/auth/login、/auth/recover 之前无任何失败计数，
在线爆破完全不受限。现在按客户端标识做滑窗计数 + 指数退避锁定。
"""
import time

import pytest

import web._shared as sh


class DummyRequest:
    def __init__(self, *, headers=None, client_host="1.2.3.4"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": client_host})()


@pytest.fixture(autouse=True)
def _clear_state():
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    sh._login_source_lru.clear()
    sh._login_global_attempts.clear()
    yield
    sh._login_failures.clear()
    sh._login_locked_until.clear()
    sh._login_source_lru.clear()
    sh._login_global_attempts.clear()


def test_not_limited_before_threshold():
    req = DummyRequest()
    for _ in range(sh._LOGIN_MAX_FAILURES - 1):
        sh._record_login_failure(req)
    assert sh._login_retry_after(req) == 0


def test_locks_after_threshold():
    req = DummyRequest()
    for _ in range(sh._LOGIN_MAX_FAILURES):
        sh._record_login_failure(req)
    retry = sh._login_retry_after(req)
    assert retry > 0
    assert retry <= sh._LOGIN_MAX_LOCK_SECONDS + 1


def test_success_clears_lock():
    req = DummyRequest()
    for _ in range(sh._LOGIN_MAX_FAILURES + 2):
        sh._record_login_failure(req)
    assert sh._login_retry_after(req) > 0
    sh._record_login_success(req)
    assert sh._login_retry_after(req) == 0
    assert req.client.host not in sh._login_failures


def test_lock_grows_with_more_failures():
    req = DummyRequest()
    for _ in range(sh._LOGIN_MAX_FAILURES):
        sh._record_login_failure(req)
    first = sh._login_locked_until[sh._client_key(req)]
    # 再连续失败若干次，锁定截止时间应被推后（指数退避）
    for _ in range(3):
        sh._record_login_failure(req)
    later = sh._login_locked_until[sh._client_key(req)]
    assert later > first


def test_different_clients_isolated():
    a = DummyRequest(client_host="10.0.0.1")
    b = DummyRequest(client_host="10.0.0.2")
    for _ in range(sh._LOGIN_MAX_FAILURES + 1):
        sh._record_login_failure(a)
    assert sh._login_retry_after(a) > 0
    assert sh._login_retry_after(b) == 0


def test_client_key_prefers_forwarded_for_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    req = DummyRequest(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"}, client_host="10.0.0.1")
    assert sh._client_key(req) == "203.0.113.9"


def test_client_key_ignores_spoofed_forwarded_for_from_direct_client(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,::1/128")
    req = DummyRequest(
        headers={"x-forwarded-for": "203.0.113.9"},
        client_host="198.51.100.24",
    )

    assert sh._client_key(req) == "198.51.100.24"


def test_client_key_ignores_invalid_forwarded_ip(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    req = DummyRequest(
        headers={"x-forwarded-for": "not-an-ip"}, client_host="10.0.0.1"
    )

    assert sh._client_key(req) == "10.0.0.1"


_DEFAULT_TRUSTED = "127.0.0.0/8,::1/128"


def _starlette_request(forwarded_values, client_host="127.0.0.1"):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "query_string": b"",
            "headers": [
                (b"x-forwarded-for", value.encode("latin-1"))
                for value in forwarded_values
            ],
            "client": (client_host, 50000),
        }
    )


def test_client_key_takes_rightmost_untrusted_hop_of_appended_chain(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = DummyRequest(
        headers={"x-forwarded-for": "198.51.100.66, 203.0.113.7"},
        client_host="127.0.0.1",
    )

    assert sh._client_key(req) == "203.0.113.7"


def test_rotating_forged_left_hop_cannot_evade_lockout(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    for index in range(sh._LOGIN_MAX_FAILURES):
        sh._record_login_failure(
            DummyRequest(
                headers={"x-forwarded-for": f"198.51.100.{index + 1}, 203.0.113.7"},
                client_host="127.0.0.1",
            )
        )

    fresh_forgery = DummyRequest(
        headers={"x-forwarded-for": "198.51.100.99, 203.0.113.7"},
        client_host="127.0.0.1",
    )
    assert sh._login_retry_after(fresh_forgery) > 0


def test_client_key_skips_every_trusted_hop(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,10.0.0.0/8")
    req = DummyRequest(
        headers={"x-forwarded-for": "198.51.100.66, 203.0.113.7, 10.0.0.9"},
        client_host="127.0.0.1",
    )

    assert sh._client_key(req) == "203.0.113.7"


def test_client_key_joins_repeated_forwarded_for_headers(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = _starlette_request(["198.51.100.66", "203.0.113.7"])

    assert req.headers.get("x-forwarded-for") == "198.51.100.66"
    assert sh._client_key(req) == "203.0.113.7"


def test_client_key_tolerates_spaces_and_tabs(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = DummyRequest(
        headers={"x-forwarded-for": " 198.51.100.66 ,\t203.0.113.7\t, 127.0.0.1 "},
        client_host="127.0.0.1",
    )

    assert sh._client_key(req) == "203.0.113.7"


@pytest.mark.parametrize(
    ("forwarded", "expected"),
    [
        ("203.0.113.7:51234", "203.0.113.7"),
        ("[2001:db8:1::5]:443", "2001:db8:1::/64"),
        ("[2001:db8:1::5]", "2001:db8:1::/64"),
        ("2001:db8:1::5", "2001:db8:1::/64"),
    ],
)
def test_client_key_accepts_ports_and_bracketed_ipv6(monkeypatch, forwarded, expected):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = DummyRequest(
        headers={"x-forwarded-for": forwarded}, client_host="127.0.0.1"
    )

    assert sh._client_key(req) == expected


@pytest.mark.parametrize(
    "forwarded",
    [
        "unknown",
        "_hidden",
        '"203.0.113.7"',
        "010.1.1.1",
        "203.0.113.7:99999",
        "203.0.113.7:abc",
        "203.0.113.7:",
        "[2001:db8::1",
        "[2001:db8::1]x",
        "[203.0.113.7]",
        "2001:db8::1:443:",
        "203.0.113.7" + chr(0xA0),
    ],
)
def test_client_key_falls_back_to_peer_for_unparseable_hop(monkeypatch, forwarded):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    req = DummyRequest(
        headers={"x-forwarded-for": forwarded}, client_host="10.0.0.1"
    )

    assert sh._client_key(req) == "10.0.0.1"


def test_client_key_ignores_garbage_left_of_the_client(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = DummyRequest(
        headers={"x-forwarded-for": "unknown, 203.0.113.7"},
        client_host="127.0.0.1",
    )

    assert sh._client_key(req) == "203.0.113.7"


def test_client_key_uses_peer_when_rightmost_hop_is_unparseable(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    for forwarded in ("203.0.113.7, unknown", "198.51.100.66, 203.0.113.7,"):
        req = DummyRequest(
            headers={"x-forwarded-for": forwarded}, client_host="10.0.0.1"
        )
        assert sh._client_key(req) == "10.0.0.1"


def test_client_key_stops_at_unparseable_hop_and_keeps_last_trusted(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    req = DummyRequest(
        headers={"x-forwarded-for": "198.51.100.66, junk, 10.0.0.9"},
        client_host="10.0.0.1",
    )

    assert sh._client_key(req) == "10.0.0.9"


def test_client_key_uses_leftmost_hop_when_whole_chain_is_trusted(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    local = DummyRequest(
        headers={"x-forwarded-for": "127.0.0.1"}, client_host="127.0.0.1"
    )
    assert sh._client_key(local) == "127.0.0.1"

    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    internal = DummyRequest(
        headers={"x-forwarded-for": "10.0.0.8, 10.0.0.9"}, client_host="10.0.0.1"
    )
    assert sh._client_key(internal) == "10.0.0.8"


def test_client_key_reads_a_bounded_number_of_hops(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    chain = ", ".join(["198.51.100.66"] + ["127.0.0.2"] * 40)
    req = DummyRequest(
        headers={"x-forwarded-for": chain}, client_host="127.0.0.1"
    )

    assert sh._client_key(req) == "127.0.0.2"


def test_client_key_treats_mapped_trusted_hop_as_trusted(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", "127.0.0.0/8,10.0.0.0/8")
    req = DummyRequest(
        headers={"x-forwarded-for": "203.0.113.7, ::ffff:10.0.0.9"},
        client_host="127.0.0.1",
    )

    assert sh._client_key(req) == "203.0.113.7"


def test_mapped_loopback_peer_is_a_trusted_proxy(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = DummyRequest(
        headers={"x-forwarded-for": "203.0.113.7"},
        client_host="::ffff:127.0.0.1",
    )

    assert sh._is_trusted_proxy("::ffff:127.0.0.1") is True
    assert sh._is_trusted_proxy("::ffff:203.0.113.7") is False
    assert sh._client_key(req) == "203.0.113.7"


def test_appending_upstream_outside_trusted_list_shares_its_bucket(monkeypatch):
    # 回归说明：会追加的上游（CDN、云负载均衡）没加进可信列表时，取到的是上游节点；
    # 把它的网段加进可信列表后取到真客户端。
    req = DummyRequest(
        headers={"x-forwarded-for": "203.0.113.7, 192.0.2.10"},
        client_host="127.0.0.1",
    )
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    assert sh._client_key(req) == "192.0.2.10"

    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED + ",192.0.2.0/24")
    assert sh._client_key(req) == "203.0.113.7"


def test_client_key_parses_trusted_networks_once(monkeypatch):
    calls = []
    original = sh._trusted_proxy_networks

    def counted():
        calls.append(1)
        return original()

    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    monkeypatch.setattr(sh, "_trusted_proxy_networks", counted)
    req = DummyRequest(
        headers={"x-forwarded-for": "198.51.100.66, 203.0.113.7"},
        client_host="127.0.0.1",
    )

    assert sh._client_key(req) == "203.0.113.7"
    assert len(calls) == 1


def test_forwarded_proto_and_host_still_use_the_first_element(monkeypatch):
    monkeypatch.setenv("OMBRE_TRUSTED_PROXY_CIDRS", _DEFAULT_TRUSTED)
    req = DummyRequest(
        headers={"x-forwarded-proto": "https, http"}, client_host="127.0.0.1"
    )

    assert sh._trusted_forwarded_value(req, "x-forwarded-proto") == "https"


def test_lock_expires(monkeypatch):
    req = DummyRequest()
    for _ in range(sh._LOGIN_MAX_FAILURES):
        sh._record_login_failure(req)
    key = sh._client_key(req)
    # 把锁定截止时间挪到过去 → 视为已解锁并清理
    sh._login_locked_until[key] = time.time() - 1
    assert sh._login_retry_after(req) == 0
    assert key not in sh._login_locked_until
