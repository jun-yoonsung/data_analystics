-- =====================================================================
-- 0008 수집 워커용 함수 권한
--  수집 워커(app_ingest)는 core 스키마에 CREATE 권한이 없으므로, 새 연도 이벤트 파티션 생성을
--  소유자 권한 함수로 위임한다.
-- =====================================================================
ALTER FUNCTION util.ensure_event_partition(integer) SECURITY DEFINER;
ALTER FUNCTION util.ensure_event_partition(integer) SET search_path = pg_catalog, pg_temp;
REVOKE ALL ON FUNCTION util.ensure_event_partition(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION util.ensure_event_partition(integer) TO app_ingest;

-- 수집 워커가 같은 리그를 동시에 수집하지 않도록 쓰는 advisory lock 키
CREATE FUNCTION util.collect_lock_key(p_league_code text) RETURNS bigint
LANGUAGE sql IMMUTABLE AS $$
    SELECT hashtextextended('collect:' || p_league_code, 0)
$$;

-- 시즌·스테이지도 다른 수집 테이블처럼 수집 실행 ID 를 남긴다 (출처 추적 일관성)
ALTER TABLE core.season ADD COLUMN ingest_run_id bigint REFERENCES ingest.ingest_run (id) ON DELETE SET NULL;
ALTER TABLE core.competition_stage ADD COLUMN ingest_run_id bigint REFERENCES ingest.ingest_run (id) ON DELETE SET NULL;
