"""KBO 수집 플러그인 테스트.

픽스처(tests/fixtures/kbo/)는 GitHub 공개 저장소에서 받은 **실제 파일**이다 (README 참고).
- 정규화 단위 테스트: 네트워크·DB 없이 파서를 검증한다.
- 통합 테스트(db): 가짜 HTTP 세션이 픽스처를 돌려주고 실제 runner·writer·deriver 로 저장까지 확인한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

from collectors.core.http import FetchError, PoliteHttpClient, SourcePolicy
from collectors.core.interface import MatchTarget, NotSupported, RawDocument, RunContext, SeasonRef
from collectors.core.registry import get_plugin
from collectors.core.runner import CollectionRunner, JobRequest
from collectors.plugins.baseball_kbo import records, schedule
from collectors.plugins.baseball_kbo.common import kbo_innings_to_outs, team_ref
from collectors.plugins.baseball_kbo.plugin import KboPlugin
from config_sync.loader import DEFAULT_CONFIG_DIR, load_config
from tests.fake_plugin import FakeSession

FIXTURES = Path(__file__).parent / "fixtures" / "kbo"
KST = ZoneInfo("Asia/Seoul")


def fx(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_stat_codes() -> set[str]:
    """야구 설정의 원시(파생 아님) 지표 코드."""
    sport = load_config(DEFAULT_CONFIG_DIR).sports["baseball"]
    return {s.code for s in sport.stats if not s.is_derived}


def stat_keys(bundle) -> set[str]:
    keys: set[str] = set()
    for rows in (bundle.player_season_stats, bundle.team_season_stats, bundle.standings):
        for r in rows:
            keys |= set(r.stats)
    return keys


# ---------------------------------------------------------------------------
# 공용
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,outs", [
    ("6 1/3", 19), ("6 2/3", 20), ("6", 18), ("0", 0), ("2/3", 2), ("224 2/3", 674),
    ("-", None), ("", None), (None, None), ("6.1", None), ("6 1/2", None),
])
def test_kbo_innings_to_outs(value, outs):
    assert kbo_innings_to_outs(value) == outs


def test_team_ref_franchise_aliases():
    warnings: list[str] = []
    ids = [team_ref(n, warnings)[0] for n in
           ("SSG", "SK", "kia", "해태", "MBC", "두산 베어스", "OB", "빙그레", "넥센", "우리", "삼미", "태평양", "쌍방울")]
    assert ids == ["SK", "SK", "HT", "HT", "LG", "OB", "OB", "HH", "WO", "WO", "HD", "HD", "SB"]
    assert not warnings
    _, lg = team_ref("MBC", warnings)
    assert lg.name_ko == "LG 트윈스" and lg.attrs == {"former_names": ["MBC"]}
    assert team_ref("현대", warnings)[1].attrs["defunct"] is True
    ext, team = team_ref("신생구단", warnings)
    team_ref("신생구단", warnings)
    assert ext == "name:신생구단" and team.name_ko == "신생구단" and len(warnings) == 1   # 같은 경고는 한 번만


# ---------------------------------------------------------------------------
# 일정·결과 (comographer/kbo-crawler)
# ---------------------------------------------------------------------------
def test_parse_schedule_regular_month():
    bundle = schedule.parse_schedule(fx("gh_schedule_2026_09.json"), 2026, "regular")
    by_id = {m.external_id: m for m in bundle.matches}
    assert len(bundle.matches) == 108 and not bundle.warnings
    assert sum(m.status == "final" for m in bundle.matches) == 99
    assert sum(m.status == "postponed" for m in bundle.matches) == 4
    assert sum(m.status == "scheduled" for m in bundle.matches) == 5

    g = by_id["20260901LGOB0"]                  # LG 3 : 1 두산 (잠실, 원정 LG)
    assert (g.away_team_external_id, g.home_team_external_id) == ("LG", "OB")
    assert (g.status, g.away_score, g.home_score, g.venue_external_id) == ("final", 3, 1, "잠실")
    assert g.scheduled_at == datetime(2026, 9, 1, 18, 30, tzinfo=KST)
    assert (g.season_label, g.stage_code, g.game_number) == ("2026", "REG", 1)
    assert g.attrs == {"broadcast": ["SPO-T"]}

    rain = by_id["20260903HTNC0"]               # 링크 없는 취소 경기 → KBO 형식 ID 생성
    assert rain.status == "postponed" and rain.home_score is None
    assert rain.attrs["cancel_reason"] == "우천취소"
    assert by_id["20260925LTOB0"].attrs["broadcast"] == ["SPO-T", "KN-T"]

    tie = by_id["20260916KTHH0"]
    assert tie.status == "final" and tie.home_score == tie.away_score

    assert {t.external_id for t in bundle.teams} == {"LG", "KT", "SK", "NC", "OB", "HT", "LT", "SS", "HH", "WO"}
    assert [(s.code, s.stage_type) for s in bundle.stages] == [("REG", "regular")]


def test_parse_schedule_doubleheader_and_tie():
    dh = {m.external_id: m for m in schedule.parse_schedule(fx("gh_schedule_2023_10.json"), 2023, "regular").matches}
    assert dh["20231002SSLT1"].game_number == 1 and dh["20231002SSLT2"].game_number == 2
    assert dh["20231002SSLT2"].scheduled_at.time() == time(17, 0)
    march = {m.external_id: m for m in schedule.parse_schedule(fx("gh_schedule_2026_03.json"), 2026, "regular").matches}
    assert march["20260331OBSS0"].home_score == march["20260331OBSS0"].away_score == 5


def test_parse_schedule_postseason_and_empty():
    bundle = schedule.parse_schedule(fx("gh_postseason_2025_10.json"), 2025, "postseason")
    assert len(bundle.matches) == 16 and all(m.stage_code == "POST" for m in bundle.matches)
    assert [(s.code, s.stage_type) for s in bundle.stages] == [("POST", "postseason")]
    assert {m.external_id for m in bundle.matches} >= {"20251006NCSS0"}
    assert schedule.parse_schedule(fx("gh_postseason_2025_12_empty.json"), 2025, "postseason").is_empty()


def test_parse_schedule_rejects_changed_structure():
    with pytest.raises(ValueError):
        schedule.parse_schedule({"data": []}, 2026, "regular")
    bad = {"rows": [{"row": [{"Class": "day", "Text": "09.01(화)"}, {"Text": "<b>18:30</b>"},
                             {"Text": "<span>LG</span>"}, *[{"Text": ""}] * 6]}]}
    bundle = schedule.parse_schedule(bad, 2026, "regular")
    assert not bundle.matches and "대진 해석 불가" in bundle.warnings[0]


# ---------------------------------------------------------------------------
# 시즌 기록·순위 (PsyproLEE/KBO_statics)
# ---------------------------------------------------------------------------
def test_parse_season_stats_current(raw_stat_codes):
    bundle = records.parse_season_stats("2026", fx("gh_stats_hitters.json"), fx("gh_stats_pitchers.json"),
                                        fx("gh_stats_players.json"))
    assert not bundle.warnings
    players = {p.external_id: p for p in bundle.players}
    stats = {(s.player_external_id, s.team_external_id): s.stats for s in bundle.player_season_stats}
    assert len(players) == 587 and len(stats) == 587          # 타자 360 + 투수 293 - 겸업 66

    koo = players["62404"]
    assert (koo.name_ko, koo.birth_date, koo.height_cm, koo.weight_kg) == ("구자욱", date(1993, 2, 12), 189.0, 75.0)
    assert koo.attrs["position"] == "외야수" and koo.attrs["throws_bats"] == "우투좌타"
    assert stats[("62404", "SS")] == {
        "bat.G": 113, "bat.PA": 496, "bat.AB": 419, "bat.R": 88, "bat.H": 155, "bat.DBL": 30, "bat.TPL": 6,
        "bat.HR": 15, "bat.RBI": 102, "bat.SH": 1, "bat.SF": 5, "bat.BB": 66, "bat.IBB": 3, "bat.HBP": 5,
        "bat.SO": 71, "bat.GDP": 4}

    pitcher = next(r for r in fx("gh_stats_pitchers.json") if r["IP"] not in ("0", "-"))
    team = team_ref(pitcher["팀명"], [])[0]
    assert stats[(pitcher["playerId"], team)]["pit.OUTS"] == kbo_innings_to_outs(pitcher["IP"])
    # 타자·투수 명단 모두에 있는 선수는 한 행에 bat.*, pit.* 가 함께 있다
    both = {r["playerId"] for r in fx("gh_stats_hitters.json")} & {r["playerId"] for r in fx("gh_stats_pitchers.json")}
    pid = sorted(both)[0]
    row = next(v for (p, _), v in stats.items() if p == pid)
    assert any(k.startswith("bat.") for k in row) and any(k.startswith("pit.") for k in row)

    teams = {t.team_external_id: t.stats for t in bundle.team_season_stats}
    assert len(teams) == 10
    assert teams["SS"]["bat.H"] == sum(v.get("bat.H", 0) for (_, t), v in stats.items() if t == "SS")
    assert "bat.G" not in teams["SS"] and "pit.G" not in teams["SS"]      # 경기 수는 합산하지 않는다
    assert stat_keys(bundle) <= raw_stat_codes


def test_parse_season_stats_history_drops_unrecorded(raw_stat_codes):
    data = fx("gh_stats_season_1982.json")
    bundle = records.parse_season_stats("1982", data["hitters"], data["pitchers"])
    teams = {t.external_id: t for t in bundle.teams}
    # 1982년 MBC·해태·삼미는 계승 구단으로 연결
    assert set(teams) == {"OB", "SS", "LG", "HT", "LT", "HD"}
    assert not any("알 수 없는 팀" in w for w in bundle.warnings)
    # 당시 집계하지 않아 전부 0 인 투구 수·QS·홀드 등은 빼고 경고로 남긴다
    dropped = next(w for w in bundle.warnings if "미집계" in w)
    for code in ("pit.NP", "pit.QS", "pit.HLD"):
        assert code in dropped
    park = next(s.stats for s in bundle.player_season_stats if s.player_external_id == "82234")   # 박철순
    assert (park["pit.W"], park["pit.L"], park["pit.OUTS"]) == (24, 4, 674)
    assert "pit.NP" not in park and "pit.QS" not in park
    assert all(p.birth_date is None for p in bundle.players)          # 지난 시즌 파일에는 프로필이 없다
    assert stat_keys(bundle) <= raw_stat_codes


def test_parse_standings(raw_stat_codes):
    as_of = records.parse_updated_date(fx("gh_stats_meta.json"))
    assert as_of == date(2026, 9, 30)
    bundle = records.parse_standings("2026", fx("gh_stats_standings.json"), as_of)
    rows = [(s.rank, s.team_external_id) for s in bundle.standings]
    assert len(rows) == 10 and rows[0] == (1, "KT")
    kt = bundle.standings[0].stats
    assert kt == {"std.G": 135, "std.W": 82, "std.L": 49, "std.D": 4, "std.GB": 0.0}
    assert stat_keys(bundle) <= raw_stat_codes and not bundle.warnings
    with pytest.raises(ValueError):
        records.parse_standings("2026", [], as_of)


# ---------------------------------------------------------------------------
# 플러그인 (가짜 HTTP, DB 없음)
# ---------------------------------------------------------------------------
SCH = "/comographer/kbo-crawler/main/data/raw"
STA = "/PsyproLEE/KBO_statics/main/web/public/data"


def raw(name: str) -> str:
    """픽스처 원문 (FakeSession 은 리스트 값을 '차례로 돌려줄 응답 목록'으로 보므로 문자열로 넘긴다)."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def routes() -> dict:
    r = {f"{SCH}/2026/schedule_2026_09.json": raw("gh_schedule_2026_09.json"),
         f"{SCH}/2026/postseason/postseason_2026_09.json": raw("gh_postseason_2025_12_empty.json"),
         f"{SCH}/2025/postseason/postseason_2025_10.json": raw("gh_postseason_2025_10.json"),
         f"{SCH}/2025/schedule_2025_10.json": raw("gh_postseason_2025_12_empty.json"),
         f"{STA}/season/1982.json": raw("gh_stats_season_1982.json")}
    for n in ("meta", "hitters", "pitchers", "players", "standings"):
        r[f"{STA}/{n}.json"] = raw(f"gh_stats_{n}.json")
    return r


def make_ctx(job: str, session: FakeSession) -> RunContext:
    http = PoliteHttpClient(SourcePolicy(code="x", base_url="https://x", collection_allowed=True),
                            session=session, sleep=lambda s: None)
    return RunContext(league_code="KBO", sport_code="baseball", timezone="Asia/Seoul", job_type=job,
                      today=date(2026, 9, 30), http=http)


def test_plugin_registered_with_job_sources():
    plugin = get_plugin("kbo_community")
    assert isinstance(plugin, KboPlugin) and (plugin.sport_code, plugin.source_code) == ("baseball", "kbo_gh_stats")
    assert {j: plugin.source_code_for(j) for j in ("schedule", "results", "season_stats", "standings")} == {
        "schedule": "kbo_gh_schedule", "results": "kbo_gh_schedule",
        "season_stats": "kbo_gh_stats", "standings": "kbo_gh_stats"}


def test_fetch_schedule_months_and_missing_postseason():
    session = FakeSession(routes())
    docs = list(KboPlugin().fetch_schedule(make_ctx("schedule", session), date(2025, 10, 1), date(2025, 10, 31)))
    assert [d.external_key for d in docs] == ["regular:2025-10", "postseason:2025-10"]
    # 포스트시즌 파일이 아직 없으면(404) 조용히 건너뛴다
    session = FakeSession({**routes(), f"{SCH}/2026/schedule_2026_10.json": raw("gh_postseason_2025_12_empty.json")})
    docs = list(KboPlugin().fetch_schedule(make_ctx("schedule", session), date(2026, 9, 27), date(2026, 10, 3)))
    assert [d.external_key for d in docs] == ["regular:2026-09", "postseason:2026-09", "regular:2026-10"]
    # 정규시즌 파일 404 는 오류로 올린다
    with pytest.raises(FetchError):
        list(KboPlugin().fetch_schedule(make_ctx("schedule", FakeSession(routes())), date(2026, 11, 1),
                                        date(2026, 11, 2)))


def test_fetch_match_summary_once_per_month_and_boxscore_unsupported():
    session = FakeSession(routes())
    plugin = KboPlugin()
    ctx = make_ctx("results", session)
    t1 = MatchTarget(external_id="20260901LGOB0", local_date=date(2026, 9, 1), status="final")
    t2 = MatchTarget(external_id="20260902LGOB0", local_date=date(2026, 9, 2), status="final")
    docs = list(plugin.fetch_match(ctx, t1, frozenset({"summary"}))) + \
        list(plugin.fetch_match(ctx, t2, frozenset({"summary"})))
    assert [d.external_key for d in docs] == ["regular:2026-09", "postseason:2026-09"]
    with pytest.raises(NotSupported):
        list(plugin.fetch_match(ctx, t1, frozenset({"summary", "boxscore"})))


def test_stats_doc_current_and_past_season():
    session = FakeSession(routes())
    plugin = KboPlugin()
    ctx = make_ctx("season_stats", session)
    cur = list(plugin.fetch_player_stats(ctx, SeasonRef("KBO", "2026", 2026)))[0]
    assert cur.document_type == "season_stats" and cur.external_key == "2026"
    assert len(plugin.normalize(cur, "KBO").player_season_stats) == 587
    past = list(plugin.fetch_player_stats(ctx, SeasonRef("KBO", "1982", 1982)))[0]
    assert len(plugin.normalize(past, "KBO").player_season_stats) == 141
    st = list(plugin.fetch_standings(ctx, SeasonRef("KBO", "1982", 1982)))[0]
    b = plugin.normalize(st, "KBO")
    assert b.standings[0].as_of_date == date(1982, 12, 31) and len(b.standings) == 6


def test_normalize_unknown_type():
    with pytest.raises(ValueError):
        KboPlugin().normalize(RawDocument(document_type="boxscore", external_key="k", request_url="u", body=b"{}",
                                          fetched_at=datetime.now(KST)), "KBO")


# ---------------------------------------------------------------------------
# 통합: runner → writer → deriver (임시 DB)
# ---------------------------------------------------------------------------
NOW = datetime(2026, 9, 30, 6, 30, tzinfo=KST)


@pytest.fixture(scope="module")
def engine(db_url):
    eng = create_engine(db_url)
    yield eng
    eng.dispose()


class Env:
    def __init__(self, engine):
        self.sessions: list[FakeSession] = []

        def http_factory(policy):
            session = FakeSession(routes())
            self.sessions.append(session)
            return PoliteHttpClient(policy, session=session, sleep=lambda s: None)

        self.runner = CollectionRunner(engine, http_factory=http_factory, now=lambda: NOW)

    def run(self, job, **params):
        return self.runner.run(JobRequest(league_code="KBO", job_type=job, params=params))


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).all()


@pytest.mark.db
def test_pipeline_schedule_results_stats_standings(engine):
    env = Env(engine)
    r = env.run("schedule", date_from="2026-09-27", date_to="2026-09-30")
    assert r.status == "success", r
    by = r.counts["by_table"]
    assert by["core.match.inserted"] == 108 and by["core.team.inserted"] == 10
    assert r.counts["http_requests"] == 3                # robots + 9월 정규 + 9월 포스트시즌(빈 파일)

    r = env.run("results", recheck_days=3)
    assert r.status == "success", r
    assert r.counts["by_table"]["core.match.unchanged"] == 108
    assert env.sessions[-1].calls.count(f"{SCH}/2026/schedule_2026_09.json") == 1

    r = env.run("boxscore")
    assert r.status == "success" and any("boxscore" in w for w in r.warnings)

    r = env.run("season_stats")
    assert r.status == "success", r
    by = r.counts["by_table"]
    assert by["core.player.inserted"] == 587 and by["core.player_season_stat.inserted"] == 587
    assert by["core.team_season_stat.inserted"] == 10
    assert by.get("core.team.inserted", 0) == 0            # 일정에서 만든 팀과 같은 행으로 연결

    r = env.run("standings")
    assert r.status == "success", r
    assert q(engine, "SELECT count(*) FROM core.team")[0][0] == 10

    koo = q(engine, """
        SELECT p.birth_date, s.stats, s.derived FROM core.player p
        JOIN core.player_season_stat s ON s.player_id = p.id AND s.origin = 'collected'
        WHERE p.name_ko = '구자욱'""")
    assert str(koo[0][0]) == "1993-02-12" and koo[0][1]["bat.H"] == 155
    assert koo[0][2]["bat.AVG"] == pytest.approx(155 / 419, abs=1e-3)    # 파생 지표 재계산
    kt = q(engine, """
        SELECT s.rank, s.stats, s.as_of_date FROM core.standing s JOIN core.team t ON t.id = s.team_id
        WHERE t.code = 'KT'""")
    assert kt[0][0] == 1 and kt[0][1]["std.W"] == 82 and str(kt[0][2]) == "2026-09-30"
    consts = dict(q(engine, """
        SELECT d.code, c.value FROM config.league_constant c
        JOIN config.league_constant_definition d ON d.id = c.definition_id
        JOIN core.season s ON s.id = c.season_id WHERE s.label = '2026'"""))
    assert "LG_ERA" in consts and "FIP_C" in consts                      # 팀 기록 합계로 리그 상수 계산
    runs = [tuple(x) for x in q(engine, """
        SELECT r.job_type, ds.code FROM ingest.ingest_run r JOIN ingest.data_source ds ON ds.id = r.source_id
        ORDER BY r.id""")]
    assert runs == [("schedule", "kbo_gh_schedule"), ("results", "kbo_gh_schedule"), ("boxscore", "kbo_gh_schedule"),
                    ("season_stats", "kbo_gh_stats"), ("standings", "kbo_gh_stats")]


@pytest.mark.db
def test_pipeline_history_backfill_and_idempotent(engine):
    env = Env(engine)
    r = env.run("season_stats", date="1982-06-01")
    assert r.status == "success", r
    assert any("미집계" in w for w in r.warnings)
    lg_1982 = q(engine, """
        SELECT count(*) FROM core.season_team st JOIN core.season s ON s.id = st.season_id
        JOIN core.team t ON t.id = st.team_id WHERE s.label = '1982'""")[0][0]
    assert lg_1982 == 6
    r = env.run("season_stats")
    assert r.status == "success"
    assert r.counts["by_table"].get("core.player_season_stat.updated", 0) == 0
    assert r.counts["by_table"]["core.player_season_stat.unchanged"] == 587
