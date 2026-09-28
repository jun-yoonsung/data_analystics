"""정중한 HTTP 클라이언트 테스트 (네트워크 없음)."""
import pytest
import requests

from collectors.core.http import (CollectionNotAllowed, FetchError, PoliteHttpClient, RobotsDisallowed,
                                  SourcePolicy)
from tests.fake_plugin import BASE, FakeSession


def make_client(routes, robots="User-agent: *\nAllow: /\n", allowed=True, **policy):
    sleeps: list[float] = []
    clock = {"t": 0.0}

    def sleep(sec):
        sleeps.append(sec)
        clock["t"] += sec

    session = FakeSession(routes, robots=robots)
    client = PoliteHttpClient(
        SourcePolicy(code="test", base_url=BASE, collection_allowed=allowed,
                     **{"min_interval_ms": 1000, "max_retries": 3, **policy}),
        session=session, sleep=sleep, clock=lambda: clock["t"])
    return client, session, sleeps


def test_blocks_when_collection_not_allowed():
    client, session, _ = make_client({"/a": "ok"}, allowed=False)
    with pytest.raises(CollectionNotAllowed):
        client.get(f"{BASE}/a", document_type="x", external_key="a")
    assert session.calls == []  # robots.txt 조차 요청하지 않음


def test_respects_robots_txt():
    client, session, _ = make_client({"/public": "ok", "/private/x": "secret"},
                                     robots="User-agent: *\nDisallow: /private\n")
    assert client.get(f"{BASE}/public", document_type="x", external_key="p").body == b"ok"
    with pytest.raises(RobotsDisallowed):
        client.get(f"{BASE}/private/x", document_type="x", external_key="s")
    assert "/private/x" not in session.calls
    assert session.calls.count("/robots.txt") == 1  # 캐시
    assert client.robots_checked


def test_missing_robots_allows_all_but_server_error_blocks():
    client, _, _ = make_client({"/a": "ok"}, robots=None)       # robots.txt 404
    assert client.get(f"{BASE}/a", document_type="x", external_key="a").http_status == 200
    client, _, _ = make_client({"/a": "ok", "/robots.txt": (503, "")}, robots=None, max_retries=0)
    with pytest.raises(RobotsDisallowed):
        client.get(f"{BASE}/a", document_type="x", external_key="a")


def test_retries_with_backoff_then_succeeds():
    client, session, sleeps = make_client({"/flaky": [(503, ""), (503, ""), (200, "done")]})
    doc = client.get(f"{BASE}/flaky", document_type="x", external_key="f")
    assert doc.body == b"done"
    assert session.calls.count("/flaky") == 3
    assert 2.0 in sleeps and 4.0 in sleeps       # 지수 백오프


def test_network_error_is_retried():
    client, session, _ = make_client({"/net": [requests.ConnectionError("boom"), (200, "ok")]})
    assert client.get(f"{BASE}/net", document_type="x", external_key="n").body == b"ok"


def test_client_error_is_not_retried():
    client, session, _ = make_client({"/missing": (404, "")})
    with pytest.raises(FetchError) as exc:
        client.get(f"{BASE}/missing", document_type="x", external_key="m")
    assert exc.value.status == 404
    assert session.calls.count("/missing") == 1


def test_gives_up_after_max_retries():
    client, session, _ = make_client({"/down": (500, "")}, max_retries=2)
    with pytest.raises(FetchError):
        client.get(f"{BASE}/down", document_type="x", external_key="d")
    assert session.calls.count("/down") == 3


def test_min_interval_between_requests():
    client, _, sleeps = make_client({"/a": "1", "/b": "2"}, min_interval_ms=1500)
    client.get(f"{BASE}/a", document_type="x", external_key="a")
    client.get(f"{BASE}/b", document_type="x", external_key="b")
    # robots.txt → /a → /b 사이마다 1.5초 대기
    assert sleeps == [1.5, 1.5]
