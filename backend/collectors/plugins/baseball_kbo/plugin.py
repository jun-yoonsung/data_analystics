"""KBO 리그 수집 플러그인 — GitHub 에 공개된 커뮤니티 수집 데이터 사용.

KBO 공식 사이트·네이버·스탯티즈 등은 자동 수집을 막거나 이 환경에서 접속할 수 없어,
다른 사람들이 매일 자동으로 수집해 GitHub 공개 저장소에 올리는 데이터를 raw.githubusercontent.com 으로 받는다.

  kbo_gh_schedule  comographer/kbo-crawler  경기 일정·결과·취소, 포스트시즌 (매일 23:55 KST 갱신)
  kbo_gh_stats     PsyproLEE/KBO_statics    선수·팀 시즌 기록, 순위, 선수 프로필, 1982년~ 지난 시즌 (매일 00:30 KST 예약)

선수 경기별 기록(박스스코어)·문자중계는 온전한 공개 데이터를 찾지 못해 지원하지 않는다.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import date

from collectors.core import dto as d
from collectors.core.http import FetchError
from collectors.core.interface import (CollectorPlugin, MatchTarget, NotSupported, RawDocument, RunContext,
                                       SeasonRef)
from collectors.core.registry import register_collector
from collectors.plugins.baseball_kbo import records, schedule

RAW = "https://raw.githubusercontent.com"
SCHEDULE_BASE = f"{RAW}/comographer/kbo-crawler/main/data/raw"
STATS_BASE = f"{RAW}/PsyproLEE/KBO_statics/main/web/public/data"
# 포스트시즌 파일을 찾아보는 달 (그 외 달은 요청하지 않는다)
POSTSEASON_MONTHS = frozenset({9, 10, 11})

SCHEDULE_SOURCE = "kbo_gh_schedule"
STATS_SOURCE = "kbo_gh_stats"


@register_collector
class KboPlugin(CollectorPlugin):
    key = "kbo_community"
    sport_code = "baseball"
    source_code = STATS_SOURCE        # 시즌 기록·순위. 일정·결과는 source_code_for 참고
    parser_version = 1

    def __init__(self) -> None:
        # 한 실행에서 같은 달 일정을 여러 번 요청하지 않도록 기억한다 (플러그인은 실행마다 새로 만든다)
        self._months_done: set[tuple[int, int]] = set()

    def source_code_for(self, job_type: str) -> str:
        return SCHEDULE_SOURCE if job_type in ("schedule", "results", "boxscore", "events") else STATS_SOURCE

    # ==================================================================
    # 가져오기
    # ==================================================================
    def fetch_schedule(self, ctx: RunContext, date_from: date, date_to: date) -> Iterator[RawDocument]:
        y, m = date_from.year, date_from.month
        while (y, m) <= (date_to.year, date_to.month):
            yield from self._month_docs(ctx, y, m)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    def _month_docs(self, ctx: RunContext, year: int, month: int) -> Iterator[RawDocument]:
        if (year, month) in self._months_done:
            return
        self._months_done.add((year, month))
        yield ctx.http.get(f"{SCHEDULE_BASE}/{year}/schedule_{year}_{month:02d}.json",
                           document_type="schedule", external_key=f"regular:{year}-{month:02d}")
        if month in POSTSEASON_MONTHS:
            try:
                yield ctx.http.get(f"{SCHEDULE_BASE}/{year}/postseason/postseason_{year}_{month:02d}.json",
                                   document_type="schedule", external_key=f"postseason:{year}-{month:02d}")
            except FetchError as exc:
                if exc.status != 404:
                    raise
                ctx.log.info("%d년 %d월 포스트시즌 파일 없음", year, month)

    def fetch_match(self, ctx: RunContext, match: MatchTarget, parts: frozenset[str]) -> Iterator[RawDocument]:
        if parts - {"summary"}:
            raise NotSupported(f"{', '.join(sorted(parts - {'summary'}))} (공개된 경기별 선수 기록 소스 없음)")
        # 경기 결과는 그 달 일정 파일에 들어 있다 (같은 달은 한 번만 요청)
        yield from self._month_docs(ctx, match.local_date.year, match.local_date.month)

    def fetch_player_stats(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        yield self._stats_doc(ctx, season, "season_stats")

    def fetch_standings(self, ctx: RunContext, season: SeasonRef) -> Iterable[RawDocument]:
        yield self._stats_doc(ctx, season, "standings")

    def _stats_doc(self, ctx: RunContext, season: SeasonRef, kind: str) -> RawDocument:
        """현재 시즌이면 최신 파일들을, 지난 시즌이면 season/{연도}.json 을 받아 원문 1건으로 묶는다."""
        meta_doc = ctx.http.get(f"{STATS_BASE}/meta.json", document_type="stats_meta", external_key="meta")
        meta = json.loads(meta_doc.text())
        if str(meta.get("season")) == season.label:
            names = (["hitters", "pitchers", "players"] if kind == "season_stats" else ["standings"])
            files = {n: json.loads(ctx.http.get(f"{STATS_BASE}/{n}.json", document_type=f"stats_{n}",
                                                external_key=n).text()) for n in names}
            body = {"season": season.label, "current": True, "meta": meta, **files}
            url = f"{STATS_BASE}/{names[0]}.json"
        else:
            past = ctx.http.get(f"{STATS_BASE}/season/{season.label}.json", document_type="stats_season",
                                external_key=season.label)
            data = json.loads(past.text())
            keys = ["hitters", "pitchers"] if kind == "season_stats" else ["standings"]
            body = {"season": season.label, "current": False, "meta": meta, **{k: data.get(k) or [] for k in keys}}
            url = past.request_url
        return replace(meta_doc, document_type=kind, external_key=season.label, request_url=url,
                       body=json.dumps(body, ensure_ascii=False).encode(),
                       content_type="application/json; charset=utf-8", request_params=None)

    # ==================================================================
    # 정규화 (순수 함수)
    # ==================================================================
    def normalize(self, doc: RawDocument, league_code: str) -> d.NormalizedBundle:
        data = json.loads(doc.text())
        if doc.document_type == "schedule":
            kind, _, ym = doc.external_key.partition(":")
            return schedule.parse_schedule(data, int(ym[:4]), kind)
        if doc.document_type == "season_stats":
            return records.parse_season_stats(data["season"], data.get("hitters") or [],
                                              data.get("pitchers") or [], data.get("players"))
        if doc.document_type == "standings":
            label = data["season"]
            if data.get("current"):
                as_of = records.parse_updated_date(data.get("meta") or {})
                if as_of is None:
                    raise ValueError("meta.updatedAt 이 없어 순위 기준일을 알 수 없습니다")
            else:
                as_of = date(int(label), 12, 31)       # 지난 시즌은 최종 순위
            return records.parse_standings(label, data.get("standings") or [], as_of)
        raise ValueError(f"알 수 없는 문서 종류 {doc.document_type}")
