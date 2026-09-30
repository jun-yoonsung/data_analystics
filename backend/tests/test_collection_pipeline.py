"""수집 파이프라인 통합 테스트 (가짜 플러그인 + 가짜 HTTP + 임시 DB).

한 모듈이 하나의 시나리오를 순서대로 검증한다:
일정 → 재실행(멱등) → 박스스코어(검증·파생·집계·리그 상수·MV) → 이벤트 → 재처리 → 비허용/락/디스패처/알림
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

from collectors.core.alerts import Notifier
from collectors.core.dispatch import find_due
from collectors.core.http import PoliteHttpClient
from collectors.core.registry import PluginNotFound
from collectors.core.runner import CollectionRunner, JobRequest
from tests.fake_plugin import FakeKboPlugin, FakeSession

pytestmark = pytest.mark.db

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 4, 2, 0, 30, tzinfo=KST)

SCHEDULE = {
    "season": "2026",
    "teams": [{"id": "T1", "name": "알파"}, {"id": "T2", "name": "베타"}],
    "games": [
        {"id": "G1", "date": "2026-04-01T18:30:00+09:00", "home": "T1", "away": "T2", "status": "final",
         "hs": 5, "as": 3, "attendance": 12000,
         "innings": [[0, 2], [1, 0], [0, 0], [2, 1], [0, 0], [0, 2], [0, 0], [0, 0], [0, 0]]},
        {"id": "G2", "date": "2026-04-03T18:30:00+09:00", "home": "T2", "away": "T1", "status": "scheduled"},
    ],
}
BOXSCORE = {
    "game": "G1",
    "players": [
        {"id": "P1", "name": "알파타자", "team": "T1", "pos": "SS",
         "bat": {"PA": 5, "AB": 4, "H": 2, "DBL": 1, "HR": 1, "BB": 1, "SO": 1, "R": 2, "RBI": 3, "BOGUS": 1}},
        {"id": "P2", "name": "베타투수", "team": "T2", "pos": "P",
         "pit": {"OUTS": 18, "H": 6, "ER": 3, "R": 4, "BB": 2, "HBP": 0, "SO": 7, "HR": 1, "TBF": 26}},
        {"id": "P3", "name": "알파투수", "team": "T1", "pos": "P",
         "pit": {"OUTS": 27, "H": 5, "ER": 3, "R": 3, "BB": 1, "HBP": 1, "SO": 9, "HR": 0, "TBF": 33}},
        {"id": "P4", "name": "유령", "team": "T9", "bat": {"PA": 1}},
    ],
    "team_totals": {
        "T1": {"pit_OUTS": 27, "pit_ER": 3, "pit_H": 5, "pit_BB": 1, "pit_HBP": 1, "pit_SO": 9, "pit_HR": 0,
               "bat_R": 5, "bat_H": 9},
        "T2": {"pit_OUTS": 24, "pit_ER": 5, "pit_H": 9, "pit_BB": 3, "pit_HBP": 0, "pit_SO": 6, "pit_HR": 2,
               "bat_R": 3, "bat_H": 5},
    },
}
EVENTS = {"game": "G1", "events": [
    {"seq": 1, "inning": 1, "team": "T1", "batter": "P1", "pitcher": "P2", "result": "HR", "pitcher_hand": "R"},
]}


class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, subject, body):
        self.sent.append((subject, body))


@pytest.fixture(scope="module")
def engine(db_url):
    eng = create_engine(db_url)
    with eng.begin() as c:
        c.execute(text("""
            INSERT INTO ingest.data_source (code, name, base_url, collection_allowed, min_interval_ms)
            VALUES ('test_source', '테스트 소스', 'https://fake-source.test', true, 500)"""))
        c.execute(text("""
            INSERT INTO core.league (sport_id, code, name_ko, name_en, country, gender, timezone, collector_key)
            SELECT id, 'TESTKBO', '테스트 리그', 'Test League', 'KR', 'men', 'Asia/Seoul', 'test_fake_kbo'
            FROM config.sport WHERE code = 'baseball'"""))
    yield eng
    eng.dispose()


class Env:
    def __init__(self, engine, routes=None, plugin_cls=FakeKboPlugin, notifier=None):
        self.routes = routes or {"/schedule": SCHEDULE, "/game/G1/boxscore": BOXSCORE, "/game/G1/events": EVENTS}
        self.sessions: list[FakeSession] = []

        def http_factory(policy):
            session = FakeSession(self.routes)
            self.sessions.append(session)
            return PoliteHttpClient(policy, session=session, sleep=lambda s: None)

        def plugin_factory(key):
            if key != plugin_cls.key:
                raise PluginNotFound(key)
            return plugin_cls()

        self.runner = CollectionRunner(engine, http_factory=http_factory, now=lambda: NOW,
                                       plugin_factory=plugin_factory, notifier=notifier)

    def run(self, job, **kw):
        return self.runner.run(JobRequest(league_code=kw.pop("league", "TESTKBO"), job_type=job, **kw))


def scalar(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()


# ---------------------------------------------------------------------------
def test_schedule_job_creates_season_teams_matches(engine):
    result = Env(engine).run("schedule")
    assert result.status == "success", result
    by = result.counts["by_table"]
    assert by["core.team.inserted"] == 2 and by["core.match.inserted"] == 2
    assert by["core.match_period.inserted"] == 9
    with engine.connect() as c:
        m = c.execute(text("""
            SELECT x.status, x.home_score, x.away_score, x.winner, x.local_date, x.attendance, x.source_id IS NOT NULL AS prov
            FROM core.match x JOIN ingest.external_id_map e ON e.entity_id = x.id AND e.entity_type = 'match'
            WHERE e.external_id = 'G1'""")).one()
        assert (m.status, m.home_score, m.away_score, m.winner, str(m.local_date), m.prov) == \
            ("final", 5, 3, "home", "2026-04-01", True)
        season = c.execute(text("""SELECT start_date, end_date FROM core.season s JOIN core.league l ON l.id = s.league_id
                                   WHERE l.code = 'TESTKBO'""")).one()
        assert (str(season.start_date), str(season.end_date)) == ("2026-04-01", "2026-04-03")
        run = c.execute(text("SELECT status, finished_at IS NOT NULL AS done FROM ingest.ingest_run WHERE id = :i"),
                        {"i": result.run_id}).one()
        assert run.status == "success" and run.done
        assert c.execute(text("SELECT count(*) FROM ingest.raw_payload WHERE ingest_run_id = :i"),
                         {"i": result.run_id}).scalar() == 1
        assert c.execute(text("SELECT count(*) FROM core.season_team st JOIN core.season s ON s.id = st.season_id "
                              "JOIN core.league l ON l.id = s.league_id WHERE l.code = 'TESTKBO'")).scalar() == 2


def test_schedule_rerun_is_idempotent(engine):
    result = Env(engine).run("schedule")
    assert result.status == "success"
    assert result.counts.get("inserted", 0) == 0 and result.counts.get("updated", 0) == 0
    assert result.counts["by_table"]["ingest.raw_payload.unchanged"] == 1


def test_boxscore_job_validates_derives_and_aggregates(engine):
    result = Env(engine).run("boxscore", params={"recheck_days": 1})
    assert result.status == "success", result
    # 모르는 지표 키는 버리고 경고, 없는 팀 참조 행은 거부
    assert any("bat.BOGUS" in w for w in result.warnings)
    assert result.counts["by_table"]["PlayerMatchStatDTO.rejected"] == 1
    with engine.connect() as c:
        p1 = c.execute(text("""
            SELECT s.stats, s.derived, s.position_code FROM core.player_match_stat s
            JOIN ingest.external_id_map e ON e.entity_id = s.player_id AND e.entity_type = 'player'
            WHERE e.external_id = 'P1'""")).one()
        assert "bat.BOGUS" not in p1.stats and p1.position_code == "SS"
        assert p1.derived["bat.AVG"] == 0.5 and p1.derived["bat.TB"] == 7 - 1  # 2루타+홈런 = 6
        # 리그 상수: 팀 시즌(aggregated) 합계 → LG_ERA, FIP_C
        consts = dict(c.execute(text("""
            SELECT d.code, c.value FROM config.league_constant c
            JOIN config.league_constant_definition d ON d.id = c.definition_id
            JOIN core.season s ON s.id = c.season_id JOIN core.league l ON l.id = s.league_id
            WHERE l.code = 'TESTKBO'""")).all())
        assert float(consts["LG_ERA"]) == pytest.approx(27 * 8 / 51)
        fip_c = 27 * 8 / 51 - 3 * (13 * 2 + 3 * (4 + 1) - 2 * 15) / 51
        assert float(consts["FIP_C"]) == pytest.approx(fip_c)
        # 선수 경기 FIP 는 리그 상수를 반영
        p2 = c.execute(text("""
            SELECT s.derived FROM core.player_match_stat s
            JOIN ingest.external_id_map e ON e.entity_id = s.player_id AND e.entity_type = 'player'
            WHERE e.external_id = 'P2'""")).scalar()
        assert p2["pit.FIP"] == pytest.approx(3 * (13 + 6 - 14) / 18 + fip_c)
        assert p2["pit.ERA"] == pytest.approx(4.5) and p2["pit.IP"] == 6
        # 시즌 집계 (팀별 + 합산 행) + 파생
        rows = c.execute(text("""
            SELECT s.team_id, s.stats, s.derived FROM core.player_season_stat s
            JOIN ingest.external_id_map e ON e.entity_id = s.player_id AND e.entity_type = 'player'
            WHERE e.external_id = 'P1' AND s.origin = 'aggregated' ORDER BY s.team_id NULLS LAST""")).all()
        assert len(rows) == 2 and rows[1].team_id is None
        assert rows[0].stats["bat.H"] == 2 and rows[0].derived["bat.AVG"] == 0.5
        # 리더보드 MV 갱신
        assert c.execute(text("""SELECT count(*) FROM core.mv_player_season_stat_long WHERE stat_code = 'bat.AVG'
                                 AND value = 0.5""")).scalar() >= 1


def test_events_job_writes_partitioned_events(engine):
    result = Env(engine).run("events")
    assert result.status == "success", result
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT e.tableoid::regclass::text AS part, e.attrs, count(p.*) AS participants
            FROM core.event e JOIN core.event_participant p ON p.event_id = e.id AND p.match_date = e.match_date
            JOIN ingest.external_id_map m ON m.entity_id = e.match_id AND m.entity_type = 'match'
            WHERE m.external_id = 'G1' GROUP BY 1, 2""")).one()
        assert row.part == "core.event_y2026" and row.attrs["pitcher_hand"] == "R" and row.participants == 2
    # 재실행 시 변화 없음
    again = Env(engine).run("events")
    assert again.counts.get("inserted", 0) == 0 and again.counts.get("updated", 0) == 0


def test_reprocess_uses_stored_raw_without_requests(engine):
    class FakeKboPluginV2(FakeKboPlugin):
        parser_version = 2

        def _team(self, t):  # 파서 개선: 약칭 추가
            from collectors.core.dto import TeamDTO
            return TeamDTO(external_id=t["id"], name_ko=t["name"], short_name_ko=t["name"][:1])

    env = Env(engine, plugin_cls=FakeKboPluginV2)
    result = env.run("schedule", trigger="reprocess")
    assert result.status == "success", result
    assert env.sessions == []                          # HTTP 클라이언트를 만들지도 않음
    assert result.counts["by_table"]["core.team.updated"] == 2
    assert scalar(engine, "SELECT short_name_ko FROM core.team WHERE name_ko = '알파'") == "알"
    assert scalar(engine, "SELECT max(parser_version) FROM ingest.raw_payload WHERE document_type = 'schedule'") == 2


def test_source_not_allowed_is_skipped_without_requests(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE ingest.data_source SET collection_allowed = false WHERE code = 'test_source'"))
    try:
        env = Env(engine)
        result = env.run("schedule")
        assert result.status == "skipped" and "비허용" in result.error
        assert env.sessions == []
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE ingest.data_source SET collection_allowed = true WHERE code = 'test_source'"))


def test_missing_plugin_is_skipped(engine):
    result = Env(engine).run("schedule", league="KBL")   # KBL 수집기는 아직 등록 전
    assert result.status == "skipped" and "kbl_official" in result.error


def test_concurrent_run_is_skipped(engine):
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(util.collect_lock_key('TESTKBO'))"))
        try:
            result = Env(engine).run("schedule")
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(util.collect_lock_key('TESTKBO'))"))
    assert result.status == "skipped" and "실행 중" in result.error


def test_failure_alerts_once(engine):
    notifier = RecordingNotifier()
    env = Env(engine, routes={"/schedule": (500, "")}, notifier=notifier)
    first = env.run("schedule")
    second = env.run("schedule")
    assert first.status == second.status == "failed"
    assert len(notifier.sent) == 1 and "TESTKBO" in notifier.sent[0][0]


def test_dispatcher_due_and_offseason(engine):
    with engine.begin() as c:
        league_id = c.execute(text("SELECT id FROM core.league WHERE code = 'TESTKBO'")).scalar()
        src = c.execute(text("SELECT id FROM ingest.data_source WHERE code = 'test_source'")).scalar()
        c.execute(text("UPDATE ingest.collection_schedule SET enabled = false"))   # 시드 리그 스케줄 제외
        for job, cron, policy in (("results", "50 23 * * *", "skip"), ("roster", "10 9 * * *", "weekly")):
            c.execute(text("""
                INSERT INTO ingest.collection_schedule (league_id, source_id, job_type, cron, timezone, offseason_policy)
                VALUES (:l, :s, :j, :c, 'Asia/Seoul', :p)"""), {"l": league_id, "s": src, "j": job, "c": cron, "p": policy})

    def due(now):
        with engine.begin() as c:
            return {j.job_type: j for j in find_due(c, now)}

    # 시즌 중(2026-04-01 ~ 04-03), 23:50 직후 → results due, 다시 호출하면 due 아님
    jobs = due(datetime(2026, 4, 2, 23, 55, tzinfo=KST))
    assert jobs["results"].skip_reason is None
    assert "results" not in due(datetime(2026, 4, 2, 23, 58, tzinfo=KST))
    # 비시즌 (12월): skip 정책은 건너뜀, weekly 는 월요일에만
    jobs = due(datetime(2026, 12, 15, 23, 55, tzinfo=KST))            # 화요일 밤
    assert jobs["results"].skip_reason == "비시즌"
    jobs = due(datetime(2026, 12, 16, 9, 15, tzinfo=KST))             # 수요일 09:15
    assert "월요일" in jobs["roster"].skip_reason
    jobs = due(datetime(2026, 12, 21, 9, 15, tzinfo=KST))             # 월요일 09:15
    assert jobs["roster"].skip_reason is None
    # 발화 후 6시간 넘게 지난 작업은 놓친 것으로 보고 건너뜀
    assert "results" not in due(datetime(2026, 12, 23, 8, 0, tzinfo=KST))


def test_pipeline_runs_with_ingest_role_privileges(db_url):
    """운영과 같은 app_ingest 권한으로 전체 파이프라인이 동작하는지 (권한 누락 탐지)."""
    from sqlalchemy import event

    eng = create_engine(db_url)

    @event.listens_for(eng, "connect")
    def _set_role(dbapi_conn, _):
        with dbapi_conn.cursor() as cur:
            cur.execute("SET ROLE app_ingest")
        dbapi_conn.commit()

    routes = {"/schedule": {**SCHEDULE, "games": [{**SCHEDULE["games"][0], "id": "G9",
                                                    "date": "2027-04-01T18:30:00+09:00"}]},
              "/game/G9/boxscore": {**BOXSCORE, "game": "G9"},
              "/game/G9/events": {**EVENTS, "game": "G9"}}
    env = Env(eng, routes=routes)
    env.runner.now = lambda: datetime(2027, 4, 2, 0, 30, tzinfo=KST)
    for job in ("schedule", "boxscore", "events"):
        result = env.run(job)
        assert result.status == "success", (job, result)
    with eng.connect() as c:
        assert c.execute(text("SELECT current_user")).scalar() == "app_ingest"
        assert c.execute(text("SELECT count(*) FROM core.event WHERE match_date = '2027-04-01'")).scalar() == 1
    eng.dispose()
