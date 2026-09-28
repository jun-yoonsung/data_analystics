"""수집 플러그인 공통 인터페이스.

플러그인 = 종목·리그(소스)별 "가져오기(fetch_*) + 정규화(normalize)" 만 구현한다.
저장, 외부 ID 매핑, 검증, 파생 지표 계산, 로그, 알림은 공통 코드(runner)가 맡는다.

규칙
  - fetch_* 는 원문(RawDocument)만 반환한다. 요청은 반드시 ctx.http 로 보낸다.
  - normalize 는 **순수 함수**다. 네트워크·DB 에 접근하지 않는다.
    → 파서를 고친 뒤 저장된 raw 로 재처리(reprocess)할 수 있다.
  - 파서가 바뀌면 parser_version 을 올린다.
"""
from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collectors.core.dto import NormalizedBundle
    from collectors.core.http import PoliteHttpClient


@dataclass(frozen=True)
class RawDocument:
    """소스에서 받은 원문 한 건."""

    document_type: str          # schedule / match_summary / boxscore / events / player_stats / standings ...
    external_key: str           # 소스 내 식별자 (경기 ID, 날짜 범위 등)
    request_url: str
    body: bytes
    fetched_at: datetime
    content_type: str | None = None
    http_status: int | None = None
    request_params: dict | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def text(self, encoding: str | None = None) -> str:
        """본문 문자열. encoding 미지정 시 Content-Type charset → utf-8 순."""
        if encoding is None and self.content_type and "charset=" in self.content_type:
            encoding = self.content_type.split("charset=", 1)[1].split(";")[0].strip()
        return self.body.decode(encoding or "utf-8", errors="replace")


@dataclass(frozen=True)
class SeasonRef:
    league_code: str
    label: str
    start_year: int


@dataclass(frozen=True)
class MatchTarget:
    """경기 단위 수집 대상 (runner 가 DB 에서 골라 전달)."""

    external_id: str
    local_date: date
    status: str


@dataclass
class RunContext:
    """수집 실행 문맥. 플러그인은 여기 있는 것만 사용한다."""

    league_code: str
    sport_code: str
    timezone: str
    job_type: str
    today: date                              # 리그 타임존 기준 오늘
    http: PoliteHttpClient
    league_settings: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("collector"))


class NotSupported(Exception):
    """플러그인이 해당 수집 작업을 지원하지 않음 (소스가 제공하지 않는 데이터)."""


class CollectorPlugin(ABC):
    """종목·리그별 수집 플러그인 기반 클래스."""

    key: ClassVar[str]            # league.collector_key 와 매칭
    sport_code: ClassVar[str]     # config.sport.code
    source_code: ClassVar[str]    # ingest.data_source.code
    parser_version: ClassVar[int] = 1
    requires_browser: ClassVar[bool] = False   # 동적 페이지(Playwright) 필요 여부 → 워커 큐 분리

    # ---- 시즌 -------------------------------------------------------------
    def season_for_date(self, league_code: str, d: date) -> SeasonRef:
        """날짜가 속한 시즌. 기본은 단일 연도 시즌 (KBO, K리그). 연도를 넘는 리그는 재정의."""
        return SeasonRef(league_code=league_code, label=str(d.year), start_year=d.year)

    # ---- 가져오기 ---------------------------------------------------------
    @abstractmethod
    def fetch_schedule(self, ctx: RunContext, date_from: date, date_to: date) -> Iterable[RawDocument]:
        """기간 내 경기 일정·결과 목록."""

    @abstractmethod
    def fetch_match(self, ctx: RunContext, match: MatchTarget, parts: frozenset[str]) -> Iterable[RawDocument]:
        """경기 상세. parts ⊆ {'summary', 'boxscore', 'events'}"""

    def fetch_player_stats(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        """선수·팀 시즌 누적 기록."""
        raise NotSupported("season_stats")

    def fetch_standings(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        """순위표."""
        raise NotSupported("standings")

    def fetch_roster(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        """선수 명단·등록 현황."""
        raise NotSupported("roster")

    # ---- 정규화 -----------------------------------------------------------
    @abstractmethod
    def normalize(self, doc: RawDocument, league_code: str) -> NormalizedBundle:
        """원문 → 공통 DTO. 순수 함수 (I/O 금지)."""
