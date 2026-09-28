-- =====================================================================
-- 0007 읽기 최적화 뷰 + 권한
-- =====================================================================

-- ---------------------------------------------------------------------
-- 롱포맷 materialized view (docs/design/02-data-model.md §2.3)
--  JSONB(stats + derived)를 (지표 코드, 값) 행으로 펼쳐 임의 지표 리더보드를 인덱스로 처리한다.
--  숫자 값만 포함한다 (text 지표 제외).
-- ---------------------------------------------------------------------
CREATE MATERIALIZED VIEW core.mv_player_season_stat_long AS
SELECT p.id        AS player_season_stat_id,
       p.season_id,
       p.stage_id,
       p.player_id,
       p.team_id,
       p.origin,
       kv.key      AS stat_code,
       kv.value::numeric AS value
FROM core.player_season_stat p
CROSS JOIN LATERAL jsonb_each(p.stats || p.derived) AS kv (key, value)
WHERE jsonb_typeof(kv.value) = 'number'
WITH DATA;

CREATE UNIQUE INDEX uq_mv_player_season_stat_long ON core.mv_player_season_stat_long (player_season_stat_id, stat_code);
CREATE INDEX ix_mv_player_season_stat_long_rank
    ON core.mv_player_season_stat_long (season_id, stage_id, stat_code, value DESC);
CREATE INDEX ix_mv_player_season_stat_long_player ON core.mv_player_season_stat_long (player_id, stat_code);

CREATE MATERIALIZED VIEW core.mv_team_season_stat_long AS
SELECT t.id        AS team_season_stat_id,
       t.season_id,
       t.stage_id,
       t.team_id,
       t.origin,
       kv.key      AS stat_code,
       kv.value::numeric AS value
FROM core.team_season_stat t
CROSS JOIN LATERAL jsonb_each(t.stats || t.derived) AS kv (key, value)
WHERE jsonb_typeof(kv.value) = 'number'
WITH DATA;

CREATE UNIQUE INDEX uq_mv_team_season_stat_long ON core.mv_team_season_stat_long (team_season_stat_id, stat_code);
CREATE INDEX ix_mv_team_season_stat_long_rank
    ON core.mv_team_season_stat_long (season_id, stage_id, stat_code, value DESC);

-- MV 갱신은 소유자만 가능하므로 SECURITY DEFINER 함수로 수집 워커에 위임한다.
CREATE FUNCTION util.refresh_stat_views() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY core.mv_player_season_stat_long;
    REFRESH MATERIALIZED VIEW CONCURRENTLY core.mv_team_season_stat_long;
END $$;
REVOKE ALL ON FUNCTION util.refresh_stat_views() FROM PUBLIC;

-- ---------------------------------------------------------------------
-- 권한
--  "자동 수집 데이터는 사용자가 수정할 수 없다" → app_api 는 core/config/ingest 에 SELECT 만.
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA util, config, core, ingest TO app_api, app_ingest, app_readonly;
GRANT USAGE ON SCHEMA auth, analyst TO app_api;

-- app_api
GRANT SELECT ON ALL TABLES IN SCHEMA config, core, ingest TO app_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA auth, analyst TO app_api;
REVOKE INSERT, UPDATE, DELETE ON analyst.change_log FROM app_api;   -- 이력은 트리거만 기록
GRANT USAGE ON ALL SEQUENCES IN SCHEMA auth, analyst TO app_api;

-- app_ingest (수집 워커)
GRANT SELECT ON ALL TABLES IN SCHEMA config TO app_ingest;
GRANT INSERT, UPDATE ON config.league_constant TO app_ingest;       -- 계산된 리그 상수
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core, ingest TO app_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA config, core, ingest TO app_ingest;
GRANT EXECUTE ON FUNCTION util.refresh_stat_views() TO app_ingest;

-- app_readonly (SQL 콘솔): 수집·설정 데이터만. analyst 는 노출하지 않는다
--  (콘솔에서 SET app.user_id 로 RLS 를 우회할 수 있으므로)
GRANT SELECT ON ALL TABLES IN SCHEMA config, core TO app_readonly;
GRANT SELECT ON ingest.data_source, ingest.ingest_run, ingest.collection_schedule TO app_readonly;
ALTER ROLE app_readonly SET statement_timeout = '10s';
ALTER ROLE app_readonly SET default_transaction_read_only = on;

-- 이후 마이그레이션에서 만들 테이블에도 같은 권한 적용
ALTER DEFAULT PRIVILEGES IN SCHEMA config GRANT SELECT ON TABLES TO app_api, app_ingest, app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT ON TABLES TO app_api, app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA core GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA ingest GRANT SELECT ON TABLES TO app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA ingest GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA analyst GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, ingest GRANT USAGE ON SEQUENCES TO app_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth, analyst GRANT USAGE ON SEQUENCES TO app_api;
