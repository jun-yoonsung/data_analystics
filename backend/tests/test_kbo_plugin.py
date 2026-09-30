"""KBO 수집 플러그인 테스트.

- 정규화 단위 테스트: 네트워크·DB 없이 tests/fixtures/kbo/ 의 합성 픽스처로 파서를 검증한다.
- 통합 테스트(db): 가짜 HTTP 세션으로 픽스처를 돌려주고 실제 runner·writer·deriver 로 저장까지 확인한다.
픽스처는 실제 응답이 아니라 이전 kbo-dashboard 파서가 읽던 필드로 만든 합성 데이터다 (fixtures/kbo/README.md).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

from collectors.core.http import PoliteHttpClient, SourcePolicy
from collectors.core.interface import RawDocument, RunContext, SeasonRef
from collectors.core.registry import get_plugin
from collectors.core.runner import CollectionRunner, JobRequest
from collectors.plugins.baseball_kbo import kbo_html, naver
from collectors.plugins.baseball_kbo.common import kbo_innings_to_outs, naver_innings_to_outs, team_ref
from collectors.plugins.baseball_kbo.plugin import KboPlugin
from config_sync.loader import DEFAULT_CONFIG_DIR, load_config
from tests.fake_plugin import FakeSession

FIXTURES = Path(__file__).parent / "fixtures" / "kbo"
KST = ZoneInfo("Asia/Seoul")
GAME = "20260926LGOB02026"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> dict:
    return json.loads(fixture_text(name))


def team_pages() -> list[tuple[str, str]]:
    return [("batting", fixture_text("kbo_team_hitter_basic1.html")),
            ("batting", fixture_text("kbo_team_hitter_basic2.html")),
            ("pitching", fixture_text("kbo_team_pitcher_basic1.html")),
            ("pitching", fixture_text("kbo_team_pitcher_basic2.html"))]


@pytest.fixture(scope="module")
def raw_stat_codes() -> set[str]:
    """야구 설정의 원시(파생 아님) 지표 코드."""
    sport = load_config(DEFAULT_CONFIG_DIR).sports["baseball"]
    return {s.code for s in sport.stats if not s.is_derived}


def all_stat_keys(bundle) -> set[str]:
    keys: set[str] = set()
    for rows in (bundle.player_match_stats, bundle.team_match_stats, bundle.player_season_stats,
                 bundle.team_season_stats, bundle.standings):
        for r in rows:
            keys |= set(r.stats)
    return keys


# ---------------------------------------------------------------------------
# 표기 변환
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,outs", [
    ("6 1/3", 19), ("6 2/3", 20), ("6", 18), ("0", 0), ("2/3", 2), ("1250 1/3", 3751),
    ("-", None), ("", None), (None, None), ("6.1", None), ("6 1/2", None),
])
def test_kbo_innings_to_outs(value, outs):
    assert kbo_innings_to_outs(value) == outs


@pytest.mark.parametrize("value,outs", [
    ("6 ⅓", 19), ("6 ⅔", 20), ("0 ⅓", 1), ("⅔", 2), ("5", 15), (5, 15), ("-", None), ("x", None), (None, None),
])
def test_naver_innings_to_outs(value, outs):
    assert naver_innings_to_outs(value) == outs


def test_team_ref_unifies_names_and_warns_unknown():
    warnings: list[str] = []
    assert team_ref("SSG", warnings)[0] == "SK"
    assert team_ref("kia", warnings)[1].name_ko == "KIA 타이거즈"
    assert team_ref("두산 베어스", warnings)[0] == "OB"
    assert not warnings
    ext, team = team_ref("신생구단", warnings)
    assert ext == "name:신생구단" and team.name_ko == "신생구단" and len(warnings) == 1


# ---------------------------------------------------------------------------
# 네이버 일정
# ---------------------------------------------------------------------------
def test_parse_schedule():
    bundle = naver.parse_schedule(fixture_json("naver_schedule.json"))
    by_id = {m.external_id: m for m in bundle.matches}
    # 올스타전(kbo_as)은 조용히 제외, 모르는 roundCode 는 경고 후 제외
    assert set(by_id) == {GAME, "20260926HTSS02026", "20260927SKHH12026", "20260927SKHH22026",
                          "20260928WONC02026"}
    assert any("kbo_unknown" in w for w in bundle.warnings)
    assert not any("EASTWEST" in w for w in bundle.warnings)

    g = by_id[GAME]
    assert (g.home_team_external_id, g.away_team_external_id) == ("OB", "LG")
    assert (g.status, g.home_score, g.away_score, g.venue_external_id) == ("final", 3, 5, "잠실")
    assert g.scheduled_at == datetime(2026, 9, 26, 17, 0, tzinfo=KST)
    assert (g.season_label, g.stage_code, g.game_number) == ("2026", "REG", 1)
    assert g.attrs["win_pitcher"] == "가상투수1" and g.attrs["status_info"] == "경기종료"
    assert "win_pitcher" not in by_id["20260926HTSS02026"].attrs      # 빈 문자열은 넣지 않는다

    # 더블헤더: 같은 날 같은 대진은 시작 시각 순으로 1, 2차전
    assert by_id["20260927SKHH12026"].game_number == 1
    assert by_id["20260927SKHH22026"].game_number == 2
    # 취소·예정 경기는 점수를 넣지 않는다
    assert by_id["20260927SKHH22026"].status == "cancelled" and by_id["20260927SKHH22026"].home_score is None
    assert by_id["20260928WONC02026"].status == "scheduled" and by_id["20260928WONC02026"].away_score is None

    assert {t.external_id for t in bundle.teams} == {"OB", "LG", "SS", "HT", "HH", "SK", "NC", "WO"}
    assert {v.name_ko for v in bundle.venues} == {"잠실", "대구", "대전", "창원"}
    assert [(s.code, s.stage_type) for s in bundle.stages] == [("REG", "regular")]
    assert [s.label for s in bundle.seasons] == ["2026"]


def test_parse_schedule_empty_and_unknown_status():
    assert naver.parse_schedule({"result": {"games": []}}).is_empty()
    bundle = naver.parse_schedule({"result": {"games": [
        {"gameId": "X1", "roundCode": "kbo_r", "gameDateTime": "2026-09-26T17:00:00", "homeTeamName": "LG",
         "awayTeamName": "KT", "statusCode": "WEIRD"}]}})
    assert not bundle.matches and any("WEIRD" in w for w in bundle.warnings)


# ---------------------------------------------------------------------------
# 네이버 박스스코어
# ---------------------------------------------------------------------------
def test_parse_record(raw_stat_codes):
    bundle = naver.parse_record(fixture_json(f"naver_record_{GAME}.json"), GAME)
    stats = {s.player_external_id: s for s in bundle.player_match_stats}
    assert set(stats) == {"LG:가상타자1", "LG:가상타자2", "LG:가상투수1", "LG:가상투수2",
                          "OB:가상타자3", "OB:가상투수3", "OB:가상투수4"}
    assert all(s.match_external_id == GAME for s in stats.values())
    assert stats["OB:가상타자3"].team_external_id == "OB"

    b1 = stats["LG:가상타자1"].stats
    assert b1 == {"bat.G": 1, "bat.AB": 4, "bat.H": 2, "bat.HR": 1, "bat.RBI": 3, "bat.R": 2, "bat.SB": 0,
                  "bat.BB": 1, "bat.SO": 1, "bat.DBL": 1, "bat.TPL": 0, "bat.HBP": 0, "bat.SF": 0, "bat.SH": 0,
                  "bat.PA": 5}
    b2 = stats["LG:가상타자2"].stats
    assert (b2["bat.HBP"], b2["bat.TPL"], b2["bat.SF"], b2["bat.DBL"], b2["bat.PA"]) == (1, 1, 1, 0, 5)
    assert stats["OB:가상타자3"].stats["bat.SH"] == 1 and stats["OB:가상타자3"].stats["bat.PA"] == 5

    p1 = stats["LG:가상투수1"].stats
    # 타자 명단에도 있는 투수는 한 행에 타격·투구 지표가 함께 들어간다
    assert p1["bat.G"] == 1 and p1["bat.AB"] == 0
    assert {k: v for k, v in p1.items() if k.startswith("pit.")} == {
        "pit.G": 1, "pit.OUTS": 19, "pit.ER": 2, "pit.H": 5, "pit.R": 3, "pit.BB": 2, "pit.SO": 7, "pit.HR": 1,
        "pit.TBF": 26, "pit.NP": 98}
    # 시즌 누적 필드(gameCount, w, l, era)는 경기 기록에 섞지 않는다
    assert "pit.W" not in p1 and "pit.L" not in p1
    assert stats["LG:가상투수2"].stats["pit.OUTS"] == 8
    assert stats["OB:가상투수4"].stats["pit.OUTS"] == 1

    assert all_stat_keys(bundle) <= raw_stat_codes
    assert {p.name_ko for p in bundle.players} >= {"가상타자1", "가상투수4"}
    assert not bundle.warnings


def test_parse_record_requires_team_names():
    with pytest.raises(ValueError):
        naver.parse_record({"result": {"recordData": {}}}, GAME)


def test_inning_result_counts():
    counts = naver.inning_result_counts({"inn1": "좌2", "inn2": "중3", "inn3": "사구", "inn4": "우희비",
                                         "inn5": "투희번", "inn6": "좌홈", "inn7": "", "name": "좌2"})
    assert counts == {"bat.DBL": 1, "bat.TPL": 1, "bat.HBP": 1, "bat.SF": 1, "bat.SH": 1}


# ---------------------------------------------------------------------------
# KBO 공식 페이지
# ---------------------------------------------------------------------------
def test_parse_standings(raw_stat_codes):
    bundle = kbo_html.parse_standings(fixture_text("kbo_teamrank.html"), "2026", date(2026, 9, 29))
    rows = {s.team_external_id: s for s in bundle.standings}
    assert [(s.rank, s.team_external_id) for s in bundle.standings] == [(1, "LG"), (2, "HT"), (3, "OB")]
    assert rows["LG"].stats == {"std.G": 140, "std.W": 84, "std.L": 53, "std.D": 3, "std.GB": 0.0}
    assert rows["OB"].stats["std.GB"] == 9.5
    assert all(s.as_of_date == date(2026, 9, 29) and s.stage_code == "REG" for s in bundle.standings)
    assert all_stat_keys(bundle) <= raw_stat_codes
    assert {t.name_ko for t in bundle.teams} == {"LG 트윈스", "KIA 타이거즈", "두산 베어스"}
    assert not bundle.warnings


def test_parse_standings_season_from_page():
    bundle = kbo_html.parse_standings(fixture_text("kbo_teamrank.html"), "2027", date(2027, 3, 1))
    assert bundle.standings[0].season_label == "2026"
    assert any("페이지 시즌 2026" in w for w in bundle.warnings)


def test_parse_standings_rejects_changed_layout():
    with pytest.raises(ValueError):
        kbo_html.parse_standings("<html><body><table class='other'></table></body></html>", "2026", date(2026, 9, 29))


def test_parse_team_stats_merges_pages(raw_stat_codes):
    bundle = kbo_html.parse_team_stats(team_pages(), "2026")
    rows = {r.team_external_id: r.stats for r in bundle.team_season_stats}
    assert set(rows) == {"LG", "HT", "OB"}
    lg = rows["LG"]
    # 타격 두 페이지 + 투구 두 페이지가 한 행으로 합쳐진다
    assert (lg["bat.PA"], lg["bat.DBL"], lg["bat.SH"], lg["bat.GDP"], lg["bat.IBB"]) == (5412, 241, 41, 101, 21)
    assert (lg["pit.OUTS"], lg["pit.W"], lg["pit.HLD"], lg["pit.QS"], lg["pit.NP"], lg["pit.BK"]) == \
        (3751, 84, 88, 68, 20512, 3)
    assert rows["HT"]["pit.OUTS"] == 1241 * 3 + 2 and rows["OB"]["pit.OUTS"] == 1249 * 3
    # 투구 기록표의 피2루타(2B) 가 타격 2루타(bat.DBL) 로 섞이지 않는다
    assert lg["bat.DBL"] == 241
    # 파생 지표(AVG, ERA, WHIP, OPS ...)는 저장하지 않는다
    assert all_stat_keys(bundle) <= raw_stat_codes
    assert not bundle.warnings


def test_parse_team_stats_warns_unknown_header():
    html = fixture_text("kbo_team_hitter_basic1.html").replace("<th>SF</th>", "<th>NEWSTAT</th>")
    bundle = kbo_html.parse_team_stats([("batting", html)], "2026")
    assert any("NEWSTAT" in w for w in bundle.warnings)
    assert "bat.SF" not in bundle.team_season_stats[0].stats


# ---------------------------------------------------------------------------
# 플러그인 (가짜 HTTP, DB 없음)
# ---------------------------------------------------------------------------
def fixture_routes() -> dict:
    return {
        "/schedule/games": fixture_text("naver_schedule.json"),
        f"/schedule/games/{GAME}/record": fixture_text(f"naver_record_{GAME}.json"),
        "/record/teamrank/teamrank.aspx": fixture_text("kbo_teamrank.html"),
        "/Record/Team/Hitter/Basic1.aspx": fixture_text("kbo_team_hitter_basic1.html"),
        "/Record/Team/Hitter/Basic2.aspx": fixture_text("kbo_team_hitter_basic2.html"),
        "/Record/Team/Pitcher/Basic1.aspx": fixture_text("kbo_team_pitcher_basic1.html"),
        "/Record/Team/Pitcher/Basic2.aspx": fixture_text("kbo_team_pitcher_basic2.html"),
    }


def make_ctx(job: str, session: FakeSession) -> RunContext:
    http = PoliteHttpClient(SourcePolicy(code="x", base_url="https://x", collection_allowed=True),
                            session=session, sleep=lambda s: None)
    return RunContext(league_code="KBO", sport_code="baseball", timezone="Asia/Seoul", job_type=job,
                      today=date(2026, 9, 29), http=http)


def test_plugin_registered_with_job_sources():
    plugin = get_plugin("kbo_official")
    assert isinstance(plugin, KboPlugin)
    assert (plugin.sport_code, plugin.source_code) == ("baseball", "kbo_official")
    assert {job: plugin.source_code_for(job) for job in ("schedule", "results", "boxscore", "season_stats",
                                                        "standings")} == {
        "schedule": "naver_sports", "results": "naver_sports", "boxscore": "naver_sports",
        "season_stats": "kbo_official", "standings": "kbo_official"}


def test_fetch_schedule_splits_range_into_weeks():
    session = FakeSession(fixture_routes())
    docs = list(KboPlugin().fetch_schedule(make_ctx("schedule", session), date(2026, 9, 26), date(2026, 10, 12)))
    assert [doc.external_key for doc in docs] == ["2026-09-26~2026-10-02", "2026-10-03~2026-10-09",
                                                  "2026-10-10~2026-10-12"]
    assert docs[0].request_params == {"fields": "basic,schedule,baseball", "fromDate": "2026-09-26",
                                      "toDate": "2026-10-02", "categoryId": "kbo"}


def test_fetch_player_stats_bundles_team_pages():
    session = FakeSession(fixture_routes())
    plugin = KboPlugin()
    docs = list(plugin.fetch_player_stats(make_ctx("season_stats", session),
                                          SeasonRef(league_code="KBO", label="2026", start_year=2026)))
    assert len(docs) == 1 and docs[0].document_type == "team_season_stats"
    assert session.calls.count("/robots.txt") == 1 and len(session.calls) == 5
    bundle = plugin.normalize(docs[0], "KBO")
    assert len(bundle.team_season_stats) == 3


def test_normalize_dispatch_and_unknown_type():
    plugin = KboPlugin()
    doc = RawDocument(document_type="standings", external_key="2026", request_url="u",
                      body=fixture_text("kbo_teamrank.html").encode(),
                      fetched_at=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc))
    # 수집 시각 UTC 16:00 = 한국 9/29 01:00 → 순위 기준일 9/29
    assert plugin.normalize(doc, "KBO").standings[0].as_of_date == date(2026, 9, 29)
    with pytest.raises(ValueError):
        plugin.normalize(RawDocument(document_type="roster", external_key="k", request_url="u", body=b"{}",
                                     fetched_at=datetime.now(timezone.utc)), "KBO")


# ---------------------------------------------------------------------------
# 통합: runner → writer → deriver (임시 DB)
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 28, 23, 55, tzinfo=KST)


@pytest.fixture(scope="module")
def engine(db_url):
    eng = create_engine(db_url)
    with eng.begin() as c:
        c.execute(text("UPDATE ingest.data_source SET collection_allowed = true "
                       "WHERE code IN ('naver_sports', 'kbo_official')"))
    yield eng
    eng.dispose()


class Env:
    def __init__(self, engine):
        self.sessions: list[FakeSession] = []

        def http_factory(policy):
            session = FakeSession(fixture_routes())
            self.sessions.append(session)
            return PoliteHttpClient(policy, session=session, sleep=lambda s: None)

        self.runner = CollectionRunner(engine, http_factory=http_factory, now=lambda: NOW)

    def run(self, job, **params):
        return self.runner.run(JobRequest(league_code="KBO", job_type=job, params=params))


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


@pytest.mark.db
def test_pipeline_schedule_boxscore_standings_team_stats(engine):
    env = Env(engine)
    r = env.run("schedule", date_from="2026-09-26", date_to="2026-09-28")
    assert r.status == "success", r
    assert r.counts["by_table"]["core.match.inserted"] == 5
    assert r.counts["by_table"]["core.team.inserted"] == 8
    assert any("kbo_unknown" in w for w in r.warnings)

    # 박스스코어: 종료 경기 3개 중 픽스처가 있는 1경기만 성공, 나머지는 404 → 경기 단위 오류로 partial
    r = env.run("boxscore", recheck_days=2)
    assert r.status == "partial", r
    assert sum("HTTP 404" in w for w in r.warnings) == 2
    assert r.counts["by_table"]["core.player_match_stat.inserted"] == 7
    boxscore_calls = [c for c in env.sessions[-1].calls if c.endswith("/record")]
    assert len(boxscore_calls) == 3                     # 예정·취소 경기는 요청하지 않는다
    summary_calls = [c for c in env.sessions[-1].calls if c == "/schedule/games"]
    assert len(summary_calls) == 3                      # 날짜별 1회 (26, 27, 28일)

    rows = dict(q(engine, """
        SELECT p.name_ko, s.stats FROM core.player_match_stat s JOIN core.player p ON p.id = s.player_id
        WHERE p.name_ko IN ('가상타자1', '가상투수1')"""))
    assert rows["가상투수1"]["pit.OUTS"] == 19 and rows["가상타자1"]["bat.PA"] == 5
    # 파생 지표 (ERA = 27 * ER / OUTS)
    era = q(engine, """
        SELECT (s.derived->>'pit.ERA')::numeric FROM core.player_match_stat s JOIN core.player p ON p.id = s.player_id
        WHERE p.name_ko = '가상투수1'""")[0][0]
    assert round(float(era), 2) == round(27 * 2 / 19, 2)
    # 선수 시즌 집계(박스스코어 합산)
    agg = q(engine, """
        SELECT s.stats FROM core.player_season_stat s JOIN core.player p ON p.id = s.player_id
        WHERE p.name_ko = '가상타자1' AND s.origin = 'aggregated' AND s.team_id IS NOT NULL""")
    assert agg and agg[0][0]["bat.HR"] == 1

    # 순위·팀 시즌 기록은 KBO 공식 소스 — 네이버에서 만든 팀과 같은 행으로 연결되어야 한다
    r = env.run("standings")
    assert r.status == "success", r
    r = env.run("season_stats")
    assert r.status == "success", r
    assert r.counts["by_table"]["core.team_season_stat.inserted"] == 3
    assert q(engine, "SELECT count(*) FROM core.team")[0][0] == 8
    mapped = q(engine, """
        SELECT count(DISTINCT m.entity_id), count(DISTINCT ds.code)
        FROM ingest.external_id_map m JOIN ingest.data_source ds ON ds.id = m.source_id
        WHERE m.entity_type = 'team' AND m.external_id = 'LG'""")[0]
    assert tuple(mapped) == (1, 2)
    st = q(engine, """
        SELECT s.rank, s.stats FROM core.standing s JOIN core.team t ON t.id = s.team_id
        WHERE t.name_ko = 'LG 트윈스'""")  # 기준일은 실제 수집 시각(fetched_at) 기준이라 조건에서 뺀다
    assert st and st[0][0] == 1 and st[0][1]["std.W"] == 84
    runs = q(engine, """
        SELECT r.job_type, ds.code FROM ingest.ingest_run r JOIN ingest.data_source ds ON ds.id = r.source_id
        ORDER BY r.id""")
    assert [tuple(x) for x in runs] == [("schedule", "naver_sports"), ("boxscore", "naver_sports"),
                                       ("standings", "kbo_official"), ("season_stats", "kbo_official")]


@pytest.mark.db
def test_pipeline_events_not_supported_and_rerun_idempotent(engine):
    env = Env(engine)
    r = env.run("events")
    assert r.status == "success" and any("events" in w for w in r.warnings)
    r = env.run("schedule", date_from="2026-09-26", date_to="2026-09-28")
    assert r.status == "success"
    assert r.counts["by_table"].get("core.match.updated", 0) == 0
    assert r.counts["by_table"]["core.match.unchanged"] == 5
