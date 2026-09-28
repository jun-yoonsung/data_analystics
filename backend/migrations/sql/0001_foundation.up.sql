-- =====================================================================
-- 0001 기반: 스키마, 애플리케이션 역할, 공통 유틸 함수
-- =====================================================================

-- 책임 단위별 스키마 (docs/design/02-data-model.md §1)
CREATE SCHEMA util;      -- 공통 함수
CREATE SCHEMA config;    -- 종목 설정 (지표 정의, 구간, 포지션 ...)
CREATE SCHEMA core;      -- 자동 수집 데이터 (사용자 수정 불가)
CREATE SCHEMA ingest;    -- 수집 관리 (소스, 스케줄, 로그, raw, ID 매핑)
CREATE SCHEMA auth;      -- 사용자, 워크스페이스
CREATE SCHEMA analyst;   -- 분석가 입력 데이터

-- 애플리케이션 역할 (클러스터 전역 객체라 이미 있으면 건너뜀)
--  app_api      : FastAPI. core/config/ingest 읽기 전용, auth/analyst 읽기·쓰기(RLS 적용)
--  app_ingest   : 수집 워커. core/ingest 쓰기
--  app_readonly : (선택) SQL 콘솔. core/config 읽기 전용
-- 로그인 가능 여부와 비밀번호는 배포 환경(docker/db/init)에서 설정한다.
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['app_api', 'app_ingest', 'app_readonly'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE 'CREATE ROLE ' || quote_ident(r) || ' NOLOGIN';
        END IF;
    END LOOP;
END $$;

-- updated_at 자동 갱신 트리거 함수
CREATE FUNCTION util.set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- 테이블에 updated_at 트리거를 붙이는 헬퍼
CREATE FUNCTION util.attach_updated_at_trigger(tbl regclass) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE 'CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON ' || tbl::text
         || ' FOR EACH ROW EXECUTE FUNCTION util.set_updated_at()';
END $$;

-- 현재 요청의 사용자 ID. API가 트랜잭션마다 SET LOCAL app.user_id = '<id>' 로 설정한다.
CREATE FUNCTION util.current_app_user() RETURNS integer
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::integer
$$;
