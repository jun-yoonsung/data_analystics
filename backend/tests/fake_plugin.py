"""테스트용 가짜 야구 소스 플러그인 + 가짜 HTTP 세션.

실제 소스 구조를 흉내 내지 않는다. 공통 파이프라인(저장·검증·파생·로그)이
플러그인 인터페이스만으로 동작하는지 검증하기 위한 최소 구현이다.

가짜 소스 JSON 형식
  GET /schedule?from=..&to=..  → {"season": "2026", "teams": [...], "games": [...]}
  GET /game/<id>/boxscore      → {"game": id, "players": [...], "team_totals": {...}}
  GET /game/<id>/events        → {"game": id, "events": [...]}
"""
from __future__ import annotations

import json
from datetime import datetime

import requests
from requests.structures import CaseInsensitiveDict

from collectors.core import dto as d
from collectors.core.interface import CollectorPlugin, MatchTarget, RawDocument, RunContext

BASE = "https://fake-source.test"


class FakeKboPlugin(CollectorPlugin):
    key = "test_fake_kbo"
    sport_code = "baseball"
    source_code = "test_source"
    parser_version = 1

    def fetch_schedule(self, ctx: RunContext, date_from, date_to):
        yield ctx.http.get(f"{BASE}/schedule", params={"from": str(date_from), "to": str(date_to)},
                           document_type="schedule", external_key=f"{date_from}~{date_to}")

    def fetch_match(self, ctx: RunContext, match: MatchTarget, parts):
        if "boxscore" in parts:
            yield ctx.http.get(f"{BASE}/game/{match.external_id}/boxscore", document_type="boxscore",
                               external_key=match.external_id)
        if "events" in parts:
            yield ctx.http.get(f"{BASE}/game/{match.external_id}/events", document_type="events",
                               external_key=match.external_id)

    # ------------------------------------------------------------------
    def normalize(self, doc: RawDocument, league_code: str) -> d.NormalizedBundle:
        data = json.loads(doc.text())
        return getattr(self, f"_normalize_{doc.document_type}")(data)

    def _team(self, t: dict) -> d.TeamDTO:
        return d.TeamDTO(external_id=t["id"], name_ko=t["name"])

    def _normalize_schedule(self, data: dict) -> d.NormalizedBundle:
        season = data["season"]
        matches = []
        for g in data["games"]:
            periods = None
            if g.get("innings"):
                periods = [d.PeriodScoreDTO(period=d.PeriodRef(code="INN", seq=i + 1), away_score=a, home_score=h)
                           for i, (a, h) in enumerate(g["innings"])]
            matches.append(d.MatchDTO(
                external_id=g["id"], season_label=season, stage_code="REG",
                home_team_external_id=g["home"], away_team_external_id=g["away"],
                scheduled_at=datetime.fromisoformat(g["date"]), status=g["status"],
                home_score=g.get("hs"), away_score=g.get("as"), periods=periods,
                attendance=g.get("attendance")))
        return d.NormalizedBundle(
            seasons=[d.SeasonDTO(label=season, start_year=int(season))],
            stages=[d.StageDTO(season_label=season, code="REG", name_ko="정규시즌", stage_type="regular")],
            teams=[self._team(t) for t in data["teams"]],
            matches=matches,
        )

    def _normalize_boxscore(self, data: dict) -> d.NormalizedBundle:
        players, stats = [], []
        for p in data["players"]:
            players.append(d.PlayerDTO(external_id=p["id"], name_ko=p["name"]))
            values = {f"bat.{k}": v for k, v in p.get("bat", {}).items()}
            values.update({f"pit.{k}": v for k, v in p.get("pit", {}).items()})
            stats.append(d.PlayerMatchStatDTO(match_external_id=data["game"], player_external_id=p["id"],
                                              team_external_id=p["team"], position_code=p.get("pos"),
                                              stats=values))
        team_stats = [
            d.TeamMatchStatDTO(match_external_id=data["game"], team_external_id=team,
                               stats={f"{k.split('_', 1)[0]}.{k.split('_', 1)[1]}": v for k, v in vals.items()})
            for team, vals in data.get("team_totals", {}).items()
        ]
        return d.NormalizedBundle(players=players, player_match_stats=stats, team_match_stats=team_stats)

    def _normalize_events(self, data: dict) -> d.NormalizedBundle:
        return d.NormalizedBundle(events=[
            d.EventDTO(match_external_id=data["game"], seq=e["seq"], event_type="PA",
                       period=d.PeriodRef(code="INN", seq=e["inning"]), team_external_id=e["team"],
                       attrs={"result": e["result"], "pitcher_hand": e["pitcher_hand"]},
                       participants=[d.EventParticipantDTO(player_external_id=e["batter"], role="batter"),
                                     d.EventParticipantDTO(player_external_id=e["pitcher"], role="pitcher")])
            for e in data["events"]
        ])


class FakeSession:
    """requests.Session 대체. routes: {경로(쿼리 제외): 응답 또는 응답 리스트(호출 순서대로)}."""

    def __init__(self, routes: dict, robots: str | None = "User-agent: *\nAllow: /\n") -> None:
        self.routes = dict(routes)
        if robots is not None:
            self.routes.setdefault("/robots.txt", robots)
        self.calls: list[str] = []

    def request(self, method, url, headers=None, timeout=None, params=None, **kwargs):
        path = url.split("://", 1)[1].split("/", 1)
        path = "/" + (path[1] if len(path) > 1 else "")
        self.calls.append(path)
        route = self.routes.get(path, (404, ""))
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        status, body = route if isinstance(route, tuple) else (200, route)
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body, ensure_ascii=False)
        resp = requests.Response()
        resp.status_code = status
        resp._content = body.encode() if isinstance(body, str) else body
        resp.headers = CaseInsensitiveDict({"Content-Type": "application/json; charset=utf-8"})
        resp.url = url
        return resp
