"""KBO 리그 수집 플러그인.

데이터 소스 (이전 kbo-dashboard 프로젝트와 같은 구성)
  - naver_sports  (api-gw.sports.naver.com): 경기 일정·결과, 경기별 선수 박스스코어 (공개 JSON)
  - kbo_official  (www.koreabaseball.com)  : 순위표, 팀 시즌 기록 (HTML)

KBO 공식 사이트의 일정·스코어보드는 ASP.NET 포스트백 기반이라 GET 만으로 날짜별·경기별 데이터를
받을 수 없어 경기 단위 데이터는 네이버 API 를 쓴다. 작업별 소스는 source_code_for 로 지정한다.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from collectors.core import dto as d
from collectors.core.interface import (CollectorPlugin, MatchTarget, NotSupported, RawDocument, RunContext,
                                       SeasonRef)
from collectors.core.registry import register_collector
from collectors.plugins.baseball_kbo import kbo_html, naver

KST = ZoneInfo("Asia/Seoul")

NAVER_API = "https://api-gw.sports.naver.com"
NAVER_SCHEDULE_URL = f"{NAVER_API}/schedule/games"
NAVER_RECORD_URL = f"{NAVER_API}/schedule/games/{{game_id}}/record"
# 한 번에 조회하는 최대 기간. 기간 조회 시 결과 개수 제한 여부가 확인되지 않아 짧게 나눈다.
SCHEDULE_CHUNK_DAYS = 7

KBO_BASE = "https://www.koreabaseball.com"
KBO_STANDINGS_URL = f"{KBO_BASE}/record/teamrank/teamrank.aspx"
KBO_TEAM_PAGES = [
    ("batting", f"{KBO_BASE}/Record/Team/Hitter/Basic1.aspx"),
    ("batting", f"{KBO_BASE}/Record/Team/Hitter/Basic2.aspx"),
    ("pitching", f"{KBO_BASE}/Record/Team/Pitcher/Basic1.aspx"),
    ("pitching", f"{KBO_BASE}/Record/Team/Pitcher/Basic2.aspx"),
]

NAVER_JOBS = frozenset({"schedule", "results", "boxscore", "events"})
# 박스스코어를 요청하는 경기 상태 (예정 경기는 기록이 없다)
BOXSCORE_STATUSES = frozenset({"live", "final", "suspended"})


@register_collector
class KboPlugin(CollectorPlugin):
    key = "kbo_official"
    sport_code = "baseball"
    source_code = "kbo_official"      # 순위·시즌 기록. 경기 단위 작업은 source_code_for 참고
    parser_version = 1

    def __init__(self) -> None:
        # 한 실행에서 같은 날짜 일정을 여러 번 요청하지 않도록 기억한다 (플러그인은 실행마다 새로 만든다)
        self._summary_dates: set[date] = set()

    def source_code_for(self, job_type: str) -> str:
        return "naver_sports" if job_type in NAVER_JOBS else self.source_code

    # ==================================================================
    # 가져오기
    # ==================================================================
    def fetch_schedule(self, ctx: RunContext, date_from: date, date_to: date) -> Iterator[RawDocument]:
        start = date_from
        while start <= date_to:
            end = min(start + timedelta(days=SCHEDULE_CHUNK_DAYS - 1), date_to)
            yield self._schedule_doc(ctx, start, end)
            start = end + timedelta(days=1)

    def _schedule_doc(self, ctx: RunContext, date_from: date, date_to: date) -> RawDocument:
        return ctx.http.get(NAVER_SCHEDULE_URL, params={
            "fields": "basic,schedule,baseball", "fromDate": date_from.isoformat(),
            "toDate": date_to.isoformat(), "categoryId": "kbo",
        }, document_type="schedule", external_key=f"{date_from}~{date_to}")

    def fetch_match(self, ctx: RunContext, match: MatchTarget, parts: frozenset[str]) -> Iterator[RawDocument]:
        if "events" in parts:
            raise NotSupported("events (문자중계 구조 미확인)")
        if "summary" in parts and match.local_date not in self._summary_dates:
            # 경기 요약은 그 날짜의 일정 응답에 들어 있다 (같은 날 경기는 한 번만 요청)
            self._summary_dates.add(match.local_date)
            yield self._schedule_doc(ctx, match.local_date, match.local_date)
        if "boxscore" in parts:
            if match.status not in BOXSCORE_STATUSES:
                ctx.log.info("경기 %s 상태 %s — 박스스코어 건너뜀", match.external_id, match.status)
                return
            yield ctx.http.get(NAVER_RECORD_URL.format(game_id=match.external_id),
                               document_type="boxscore", external_key=match.external_id)

    def fetch_player_stats(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        """팀 시즌 기록 4페이지를 한 원문으로 묶는다 (선수 시즌 기록은 박스스코어 집계로 만든다)."""
        docs = [(kind, ctx.http.get(url, document_type="team_season_page", external_key=f"{season.label}:{url}"))
                for kind, url in KBO_TEAM_PAGES]
        body = json.dumps({
            "season": season.label,
            "pages": [{"kind": kind, "url": doc.request_url, "html": doc.text()} for kind, doc in docs],
        }, ensure_ascii=False).encode()
        first = docs[0][1]
        yield replace(first, document_type="team_season_stats", external_key=season.label, body=body,
                      content_type="application/json; charset=utf-8", request_params=None)

    def fetch_standings(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        yield ctx.http.get(KBO_STANDINGS_URL, document_type="standings", external_key=season.label)

    # ==================================================================
    # 정규화 (순수 함수)
    # ==================================================================
    def normalize(self, doc: RawDocument, league_code: str) -> d.NormalizedBundle:
        if doc.document_type == "schedule":
            return naver.parse_schedule(json.loads(doc.text()))
        if doc.document_type == "boxscore":
            return naver.parse_record(json.loads(doc.text()), doc.external_key)
        if doc.document_type == "standings":
            as_of = doc.fetched_at.astimezone(KST).date()
            return kbo_html.parse_standings(doc.text(), doc.external_key, as_of)
        if doc.document_type == "team_season_stats":
            data = json.loads(doc.text())
            return kbo_html.parse_team_stats([(p["kind"], p["html"]) for p in data["pages"]], data["season"])
        raise ValueError(f"알 수 없는 문서 종류 {doc.document_type}")
