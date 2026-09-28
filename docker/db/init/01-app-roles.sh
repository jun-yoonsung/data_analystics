#!/bin/bash
# DB 컨테이너 최초 초기화 시 1회 실행 (postgres 공식 이미지의 docker-entrypoint-initdb.d).
# 애플리케이션 로그인 역할을 만든다. 권한(GRANT)은 마이그레이션 0007 이 부여한다.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v api_pw="$APP_API_PASSWORD" -v ingest_pw="$APP_INGEST_PASSWORD" -v ro_pw="$APP_READONLY_PASSWORD" <<'SQL'
CREATE ROLE app_api      LOGIN PASSWORD :'api_pw';
CREATE ROLE app_ingest   LOGIN PASSWORD :'ingest_pw';
CREATE ROLE app_readonly LOGIN PASSWORD :'ro_pw';
SQL
