# 멀티 스포츠 데이터 분석 플랫폼

야구·농구·배구·축구 등 여러 종목의 공개 기록을 매일 자동 수집하고, 분석가가 직접 입력한 데이터(스카우팅, 부상 메모, 커스텀 지표 등)와 결합해 분석하는 플랫폼입니다.
새 종목·리그는 **수집 플러그인 + 종목 설정**만 추가하면 동작하도록 설계합니다.

초기 대상 리그: **KBO**(야구), **KBL**(농구), **V-리그 남·여**(배구), **K리그1**(축구)

## 진행 현황

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | 아키텍처 · 종목 독립적 데이터 모델 · ERD | 완료 |
| 2 | DB 스키마 SQL · 마이그레이션 · 종목 설정 시드 | 완료 |
| 3 | 수집 플러그인 인터페이스 + 첫 수집기 (KBO) | 완료 — GitHub 공개 데이터로 일정·결과·시즌 기록·순위 실수집 ([§6](docs/design/05-stage3-collectors.md#6-kbo-수집기)) |
| 4 | 나머지 종목 수집기 | 대기 (KBL·KOVO·K리그 사이트 접속 차단) |
| 5 | 백엔드 API (FastAPI, OpenAPI) | - |
| 6 | 프론트엔드 주요 화면 (Next.js) | - |
| 7 | 커스텀 지표 빌더 · 스카우팅 템플릿 · 내보내기 | - |
| 8 | 테스트 · README(새 종목 추가 가이드) | - |

## 설계 문서

- [01. 전체 아키텍처](docs/design/01-architecture.md)
- [02. 데이터 모델 · 저장 방식 비교 · ERD](docs/design/02-data-model.md)
- [03. 가정 및 확인 필요 사항](docs/design/03-assumptions-and-questions.md)
- [04. 2단계: DB 스키마 · 마이그레이션 · 설정 시드](docs/design/04-stage2-schema.md)
- [05. 3단계: 수집 플러그인 · 파이프라인 (플러그인 작성 가이드 포함)](docs/design/05-stage3-collectors.md)

## 저장소 구조

```
backend/
  migrations/      Alembic (본문은 migrations/sql/*.sql 순수 SQL)
  collectors/      수집 플러그인 인터페이스·공통 파이프라인 (core/), 종목·리그별 플러그인 (plugins/)
  worker/          Celery (dispatch / collect)
  stats_engine/    지표 수식 DSL, 파생 지표·시즌 집계·리그 상수 계산
  config_sync/     설정 YAML 로드·검증·DB 동기화
  app/             CLI (이후 FastAPI)
  tests/
config/
  sports/          종목 설정 (지표 정의, 구간, 포지션, 이벤트, 스플릿, 자격 조건)
  leagues/         리그 + 수집 스케줄
  sources/         데이터 소스 (약관 확인 전 수집 비활성). KBO 는 GitHub 공개 데이터(kbo_gh_schedule, kbo_gh_stats)
docker/            DB 초기화 스크립트
docker-compose.yml
```

## 빠른 시작

```bash
cp .env.example .env              # 비밀번호 변경
docker compose up -d db
docker compose run --rm migrate   # 스키마 마이그레이션 + 종목/리그 설정 동기화
docker compose up -d redis worker scheduler   # 수집 워커 + 스케줄러
```

로컬 개발과 테스트 실행 방법은 [04 문서](docs/design/04-stage2-schema.md#2-실행-방법)를 참고하세요.
