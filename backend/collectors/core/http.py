"""정중한 HTTP 클라이언트.

모든 수집 플러그인은 이 클라이언트로만 요청한다. 플러그인이 우회할 수 없도록 다음을 강제한다.
- data_source.collection_allowed 가 false 면 요청 자체를 보내지 않는다 (약관 검토 전)
- robots.txt 를 도메인별로 받아 캐시하고, 금지된 경로는 요청하지 않는다
- 도메인별 최소 요청 간격 (data_source.min_interval_ms)
- 429/5xx/네트워크 오류는 지수 백오프로 재시도 (Retry-After 존중), 4xx 는 재시도하지 않음
- 식별 가능한 User-Agent
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from collectors.core.interface import RawDocument

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "SportsAnalyticsBot/0.1 (+internal analytics; contact: admin)"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
ROBOTS_TTL_SEC = 24 * 3600


class CollectionNotAllowed(RuntimeError):
    """소스가 수집 허용 상태가 아님 (약관·robots.txt 검토 전)."""


class RobotsDisallowed(RuntimeError):
    """robots.txt 가 해당 URL 수집을 금지."""


class FetchError(RuntimeError):
    """재시도 후에도 실패한 요청."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SourcePolicy:
    """ingest.data_source 행에서 읽은 요청 정책."""

    code: str
    base_url: str
    collection_allowed: bool
    min_interval_ms: int = 3000
    max_retries: int = 3
    user_agent: str | None = None
    robots_url: str | None = None


@dataclass
class _Robots:
    parser: RobotFileParser
    fetched_at: float


@dataclass
class PoliteHttpClient:
    policy: SourcePolicy
    session: requests.Session = field(default_factory=requests.Session)
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    backoff_base_sec: float = 2.0
    timeout_sec: float = 30.0
    request_count: int = 0
    _last_request_at: dict[str, float] = field(default_factory=dict)
    _robots: dict[str, _Robots] = field(default_factory=dict)
    robots_checked: bool = False

    @property
    def user_agent(self) -> str:
        return self.policy.user_agent or DEFAULT_USER_AGENT

    # ------------------------------------------------------------------
    def get(self, url: str, *, document_type: str, external_key: str,
            params: dict | None = None, headers: dict | None = None) -> RawDocument:
        """GET 요청 후 RawDocument 로 반환. 정책 위반·최종 실패 시 예외."""
        if not self.policy.collection_allowed:
            raise CollectionNotAllowed(
                f"소스 '{self.policy.code}' 는 수집 허용 상태가 아닙니다 (data_source.collection_allowed=false)")
        if not self._robots_allows(url):
            raise RobotsDisallowed(f"robots.txt 가 금지한 URL: {url}")

        resp = self._request_with_retry("GET", url, params=params, headers=headers)
        return RawDocument(
            document_type=document_type,
            external_key=external_key,
            request_url=resp.url or url,
            request_params=params,
            http_status=resp.status_code,
            content_type=resp.headers.get("Content-Type"),
            body=resp.content,
            fetched_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        interval = self.policy.min_interval_ms / 1000
        last = self._last_request_at.get(host)
        if last is not None:
            wait = last + interval - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last_request_at[host] = self.clock()

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        host = urlsplit(url).netloc
        self._throttle(host)
        self.request_count += 1
        headers = {"User-Agent": self.user_agent, **(kwargs.pop("headers", None) or {})}
        return self.session.request(method, url, headers=headers, timeout=self.timeout_sec, **kwargs)

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        attempts = self.policy.max_retries + 1
        last_error: str = ""
        last_status: int | None = None
        for attempt in range(attempts):
            try:
                resp = self._send(method, url, **dict(kwargs))
            except requests.RequestException as exc:
                last_error, last_status = f"{type(exc).__name__}: {exc}", None
                delay = self.backoff_base_sec * (2 ** attempt)
            else:
                if resp.status_code < 400:
                    return resp
                last_error, last_status = f"HTTP {resp.status_code}", resp.status_code
                if resp.status_code not in RETRYABLE_STATUS:
                    break
                delay = self._retry_after(resp) or self.backoff_base_sec * (2 ** attempt)
            if attempt < attempts - 1:
                log.warning("요청 실패 (%s), %.1f초 후 재시도 %d/%d: %s",
                            last_error, delay, attempt + 1, attempts - 1, url)
                self.sleep(delay)
        raise FetchError(f"요청 실패 ({last_error}): {url}", status=last_status)

    @staticmethod
    def _retry_after(resp: requests.Response) -> float | None:
        value = resp.headers.get("Retry-After")
        if value and value.isdigit():
            return min(float(value), 300.0)
        return None

    # ------------------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached is None or self.clock() - cached.fetched_at > ROBOTS_TTL_SEC:
            cached = _Robots(self._fetch_robots(origin), self.clock())
            self._robots[origin] = cached
        return cached.parser.can_fetch(self.user_agent, url)

    def _fetch_robots(self, origin: str) -> RobotFileParser:
        robots_url = (self.policy.robots_url
                      if self.policy.robots_url and self.policy.robots_url.startswith(origin)
                      else origin + "/robots.txt")
        parser = RobotFileParser(robots_url)
        try:
            resp = self._request_with_retry("GET", robots_url)
        except FetchError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                # robots.txt 없음(4xx) → 표준에 따라 전체 허용
                parser.parse([])
                self.robots_checked = True
                return parser
            # 서버 오류 등으로 확인 불가 → 안전하게 전체 금지
            log.error("robots.txt 확인 실패, 수집 중단: %s", exc)
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        parser.parse(resp.text.splitlines())
        self.robots_checked = True
        return parser
