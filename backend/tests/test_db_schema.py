"""DB 스키마 통합 테스트.

TEST_DATABASE_URL (관리자 권한 접속 URL) 이 있을 때만 실행된다. 테스트마다 임시 DB 를 만들고
마이그레이션 → 설정 동기화 → 제약·권한·RLS·이력 동작을 확인한 뒤 삭제한다.
  예) TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres pytest -m db
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from config_sync.loader import DEFAULT_CONFIG_DIR, load_config
from config_sync.sync import sync_bundle

pytestmark = pytest.mark.db

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def conn(db_url):
    """테스트마다 롤백되는 연결 (마이그레이션 소유자 = 슈퍼유저)."""
    engine = create_engine(db_url)
    with engine.connect() as c:
        trans = c.begin()
        yield c
        trans.rollback()
    engine.dispose()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def _seed_match(conn) -> dict:
    """KBO 시즌·스테이지·팀·선수·경기 하나를 만든다."""
    ids = {}
    ids["league"] = conn.execute(text("SELECT id FROM core.league WHERE code = 'KBO'")).scalar_one()
    ids["sport"] = conn.execute(text("SELECT sport_id FROM core.league WHERE code = 'KBO'")).scalar_one()
    ids["season"] = conn.execute(text(
        "INSERT INTO core.season (league_id, label, start_year) VALUES (:l, '2026', 2026) RETURNING id"
    ), {"l": ids["league"]}).scalar_one()
    ids["stage"] = conn.execute(text(
        "INSERT INTO core.competition_stage (season_id, code, name_ko, stage_type) "
        "VALUES (:s, 'REG', '정규시즌', 'regular') RETURNING id"), {"s": ids["season"]}).scalar_one()
    ids["home"], ids["away"] = [conn.execute(text(
        "INSERT INTO core.team (sport_id, name_ko) VALUES (:sp, :n) RETURNING id"),
        {"sp": ids["sport"], "n": n}).scalar_one() for n in ("홈팀", "원정팀")]
    ids["player"] = conn.execute(text(
        "INSERT INTO core.player (sport_id, name_ko) VALUES (:sp, '선수') RETURNING id"),
        {"sp": ids["sport"]}).scalar_one()
    ids["match"] = conn.execute(text("""
        INSERT INTO core.match (season_id, stage_id, home_team_id, away_team_id, scheduled_at, local_date,
                                status, home_score, away_score)
        VALUES (:season, :stage, :home, :away, '2026-04-01 09:30+00', '2026-04-01', 'final', 5, 3)
        RETURNING id"""), ids).scalar_one()
    return ids


def _make_user(conn, email: str, workspace_id: int, role: str) -> int:
    uid = conn.execute(text(
        "INSERT INTO auth.app_user (email, display_name) VALUES (:e, :e) RETURNING id"), {"e": email}).scalar_one()
    conn.execute(text("INSERT INTO auth.workspace_member (workspace_id, user_id, role) VALUES (:w, :u, :r)"),
                 {"w": workspace_id, "u": uid, "r": role})
    return uid


def _as_api_user(conn, user_id: int) -> None:
    conn.execute(text("SET LOCAL ROLE app_api"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})


def _reset_role(conn) -> None:
    conn.execute(text("RESET ROLE"))
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------
def test_seed_counts(conn):
    rows = dict(conn.execute(text("""
        SELECT s.code, count(sd.*) FROM config.sport s
        JOIN config.stat_definition sd ON sd.sport_id = s.id AND sd.is_active GROUP BY s.code
    """)).all())
    assert set(rows) == {"baseball", "basketball", "volleyball", "football"}
    assert all(n > 30 for n in rows.values())
    assert conn.execute(text("SELECT count(*) FROM core.league")).scalar_one() == 5
    assert conn.execute(text(
        "SELECT count(*) FROM ingest.data_source WHERE collection_allowed")).scalar_one() == 0


def test_player_match_stat_natural_key_treats_null_period_as_equal(conn):
    ids = _seed_match(conn)
    stmt = text("""INSERT INTO core.player_match_stat (match_id, player_id, team_id, stats)
                   VALUES (:match, :player, :home, '{"bat.H": 1}')""")
    conn.execute(stmt, ids)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(stmt, ids)


def test_match_rejects_final_without_score_and_same_team(conn):
    ids = _seed_match(conn)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text("""
                INSERT INTO core.match (season_id, stage_id, home_team_id, away_team_id, scheduled_at, local_date, status)
                VALUES (:season, :stage, :home, :away, now(), '2026-04-02', 'final')"""), ids)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text("""
                INSERT INTO core.match (season_id, stage_id, home_team_id, away_team_id, scheduled_at, local_date)
                VALUES (:season, :stage, :home, :home, now(), '2026-04-03')"""), ids)


def test_stage_season_consistency_enforced(conn):
    ids = _seed_match(conn)
    other_season = conn.execute(text(
        "INSERT INTO core.season (league_id, label, start_year) VALUES (:league, '2025', 2025) RETURNING id"),
        ids).scalar_one()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text("""
                INSERT INTO core.player_season_stat (season_id, stage_id, player_id, origin)
                VALUES (:s, :stage, :player, 'collected')"""), {**ids, "s": other_season})


def test_api_role_cannot_modify_collected_data(conn):
    ids = _seed_match(conn)
    conn.execute(text("SET LOCAL ROLE app_api"))
    assert conn.execute(text("SELECT count(*) FROM core.match")).scalar_one() >= 1
    with pytest.raises(DBAPIError, match="permission denied"):
        with conn.begin_nested():
            conn.execute(text("UPDATE core.match SET home_score = 99 WHERE id = :match"), ids)
    with pytest.raises(DBAPIError, match="permission denied"):
        with conn.begin_nested():
            conn.execute(text("UPDATE config.stat_definition SET name_ko = 'x'"))


def test_ingest_role_can_write_core_and_refresh_views(conn):
    ids = _seed_match(conn)
    conn.execute(text("SET LOCAL ROLE app_ingest"))
    conn.execute(text("""
        INSERT INTO core.player_season_stat (season_id, stage_id, player_id, team_id, origin, stats, derived)
        VALUES (:season, :stage, :player, :home, 'collected', '{"bat.HR": 12, "bat.AB": 300}', '{"bat.AVG": 0.3}')
    """), ids)
    conn.execute(text("SELECT util.refresh_stat_views()"))
    rows = dict(conn.execute(text(
        "SELECT stat_code, value FROM core.mv_player_season_stat_long WHERE player_id = :player"), ids).all())
    assert {k: float(v) for k, v in rows.items()} == {"bat.HR": 12, "bat.AB": 300, "bat.AVG": 0.3}
    with pytest.raises(DBAPIError, match="permission denied"):
        with conn.begin_nested():
            conn.execute(text("SELECT count(*) FROM analyst.injury_note"))


def test_stat_key_violation_view(conn):
    ids = _seed_match(conn)
    conn.execute(text("""
        INSERT INTO core.player_match_stat (match_id, player_id, team_id, stats, derived)
        VALUES (:match, :player, :home, '{"bat.H": 1, "bat.BOGUS": 2}', '{"bat.H": 1}')"""), ids)
    row = conn.execute(text("SELECT undefined_raw_keys, undefined_derived_keys FROM core.v_stat_key_violations")).one()
    # 원시 컬럼의 미정의 키, derived 컬럼에 들어간 원시 지표 모두 탐지
    assert row.undefined_raw_keys == ["bat.BOGUS"]
    assert row.undefined_derived_keys == ["bat.H"]


def test_event_partition_routing(conn):
    ids = _seed_match(conn)
    pa = conn.execute(text("""
        SELECT e.id FROM config.event_type_definition e JOIN config.sport s ON s.id = e.sport_id
        WHERE s.code = 'baseball' AND e.code = 'PA'""")).scalar_one()
    event_id = conn.execute(text("""
        INSERT INTO core.event (match_date, match_id, seq, event_type_id, attrs)
        VALUES ('2026-04-01', :match, 1, :pa, '{"result": "HR", "pitcher_hand": "L"}') RETURNING id
    """), {**ids, "pa": pa}).scalar_one()
    conn.execute(text("""INSERT INTO core.event_participant (event_id, match_date, player_id, role)
                         VALUES (:e, '2026-04-01', :player, 'batter')"""), {**ids, "e": event_id})
    part = conn.execute(text("SELECT tableoid::regclass::text FROM core.event WHERE id = :e"),
                        {"e": event_id}).scalar_one()
    assert part == "core.event_y2026"


def test_analyst_rls_visibility_and_audit(conn):
    ids = _seed_match(conn)
    ws = conn.execute(text("INSERT INTO auth.workspace (slug, name) VALUES ('club-a', 'A 구단') RETURNING id")).scalar_one()
    other_ws = conn.execute(text("INSERT INTO auth.workspace (slug, name) VALUES ('media-b', 'B 매체') RETURNING id")).scalar_one()
    alice = _make_user(conn, "alice@example.com", ws, "analyst")
    bob = _make_user(conn, "bob@example.com", ws, "analyst")
    viewer = _make_user(conn, "viewer@example.com", ws, "viewer")
    outsider = _make_user(conn, "out@example.com", other_ws, "admin")

    insert_note = text("""
        INSERT INTO analyst.injury_note (workspace_id, visibility, player_id, start_date, source_note)
        VALUES (:ws, :vis, :player, '2026-04-02', '구단 발표') RETURNING id""")

    _as_api_user(conn, alice)
    private_id = conn.execute(insert_note, {"ws": ws, "vis": "private", **ids}).scalar_one()
    shared_id = conn.execute(insert_note, {"ws": ws, "vis": "workspace", **ids}).scalar_one()
    # created_by 는 app.user_id 로 자동 설정
    assert conn.execute(text("SELECT created_by FROM analyst.injury_note WHERE id = :i"),
                        {"i": private_id}).scalar_one() == alice
    _reset_role(conn)

    def visible(uid):
        _as_api_user(conn, uid)
        rows = set(conn.execute(text("SELECT id FROM analyst.injury_note")).scalars())
        _reset_role(conn)
        return rows

    assert visible(alice) == {private_id, shared_id}
    assert visible(bob) == {shared_id}
    assert visible(viewer) == {shared_id}
    assert visible(outsider) == set()

    # 열람자는 작성 불가
    _as_api_user(conn, viewer)
    with pytest.raises(DBAPIError, match="row-level security"):
        with conn.begin_nested():
            conn.execute(insert_note, {"ws": ws, "vis": "workspace", **ids})
    _reset_role(conn)

    # 다른 분석가의 공유 메모는 수정 불가 (0건 갱신)
    _as_api_user(conn, bob)
    n = conn.execute(text("UPDATE analyst.injury_note SET note = '수정' WHERE id = :i"), {"i": shared_id}).rowcount
    assert n == 0
    _reset_role(conn)

    # 작성자 수정 → 버전·수정자 갱신, 변경 이력 기록
    _as_api_user(conn, alice)
    conn.execute(text("UPDATE analyst.injury_note SET note = '재활 중', created_by = :bob WHERE id = :i"),
                 {"i": shared_id, "bob": bob})
    row = conn.execute(text(
        "SELECT version, updated_by, created_by, note FROM analyst.injury_note WHERE id = :i"),
        {"i": shared_id}).one()
    assert (row.version, row.updated_by, row.created_by, row.note) == (2, alice, alice, "재활 중")
    ops = conn.execute(text("""SELECT op, actor_id FROM analyst.change_log
                               WHERE table_name = 'injury_note' AND row_id = :i ORDER BY id"""),
                       {"i": shared_id}).all()
    assert [tuple(r) for r in ops] == [("INSERT", alice), ("UPDATE", alice)]
    # 이력은 API 계정이 직접 쓸 수 없음
    with pytest.raises(DBAPIError, match="permission denied"):
        with conn.begin_nested():
            conn.execute(text("DELETE FROM analyst.change_log"))
    _reset_role(conn)


def test_tag_assignment_requires_exactly_one_target(conn):
    ids = _seed_match(conn)
    ws = conn.execute(text("INSERT INTO auth.workspace (slug, name) VALUES ('ws-tag', '태그') RETURNING id")).scalar_one()
    uid = _make_user(conn, "tagger@example.com", ws, "analyst")
    tag = conn.execute(text(
        "INSERT INTO analyst.tag (workspace_id, name, created_by) VALUES (:w, '관심 선수', :u) RETURNING id"),
        {"w": ws, "u": uid}).scalar_one()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text("""INSERT INTO analyst.tag_assignment (workspace_id, tag_id, player_id, team_id, created_by)
                                 VALUES (:w, :t, :player, :home, :u)"""), {**ids, "w": ws, "t": tag, "u": uid})


def test_sync_is_idempotent(db_url):
    engine = create_engine(db_url)
    with engine.connect() as c:
        trans = c.begin()
        report = sync_bundle(c, load_config(DEFAULT_CONFIG_DIR))
        assert report.hot_indexes_created == [] and report.hot_indexes_dropped == []
        assert sum(report.deactivated.values()) == 0
        trans.rollback()
    engine.dispose()


def test_downgrade_and_upgrade_roundtrip(db_url):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.downgrade(cfg, "base")
    engine = create_engine(db_url)
    with engine.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM pg_namespace WHERE nspname IN ('core','config','analyst')")).scalar_one() == 0
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'core'")).scalar_one() > 10
    engine.dispose()
