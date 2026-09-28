# 2단계 · DB 스키마, 마이그레이션, 종목 설정 시드

> 대상 리그: **KBO(야구) · KBL(농구) · V-리그 남/여(배구) · K리그1(축구)**
> 설계 근거는 [02-data-model.md](./02-data-model.md). 이 문서는 구현 결과와 1단계 설계에서 바뀐 점을 정리한다.

---

## 1. 산출물

| 경로 | 내용 |
|---|---|
| `backend/migrations/sql/000N_*.up.sql` / `.down.sql` | 순수 SQL 마이그레이션 7개 (업/다운) |
| `backend/migrations/versions/*.py` | 위 SQL을 실행하는 Alembic 래퍼 |
| `config/sports/{baseball,basketball,volleyball,football}.yaml` | 종목 설정 시드 (구간·포지션·이벤트·지표·리그 상수·스플릿·자격 조건) |
| `config/sports/_common.yaml` | 종목 공통 스플릿 |
| `config/leagues/*.yaml` | 리그 5개 + 수집 스케줄 |
| `config/sources/*.yaml` | 데이터 소스 4개 (**모두 `collection_allowed: false`**) |
| `backend/stats_engine/formula.py` | 안전한 수식 DSL (파싱·검증·평가·의존성 정렬) |
| `backend/config_sync/` | YAML 로드 → 의미 검증 → DB upsert |
| `backend/app/cli.py` | `validate-config`, `sync-config [--dry-run]` |
| `docker-compose.yml`, `docker/db/init/` | `db` + 1회성 `migrate` 서비스, 로그인 역할 생성 |
| `backend/tests/` | 테스트 46개 (수식, 설정 검증, DB 통합) |

### 마이그레이션 구성

| 리비전 | 내용 |
|---|---|
| 0001 foundation | 스키마 6개(`util`, `config`, `core`, `ingest`, `auth`, `analyst`), 역할 `app_api`/`app_ingest`/`app_readonly`, 공통 함수 |
| 0002 config | 종목, 구간, 포지션, 이벤트 타입, 지표 카테고리, **지표 정의**, 리그 상수 정의, 스플릿, 자격 조건 |
| 0003 league_hierarchy | 데이터 소스, 리그 → 시즌 → 대회 단계, 시즌별 리그 상수 값 |
| 0004 ingest | 수집 스케줄, 실행 로그, raw 원문, 외부 ID 매핑, 매핑 검토 대기열 |
| 0005 core_entities | 경기장·팀·선수·소속 이력·경기·구간·명단·기록(선수/팀 × 경기/시즌)·순위·**이벤트(연도 파티션)**, 정합성 점검 뷰 |
| 0006 auth_analyst | 사용자·워크스페이스·역할, 분석가 입력 테이블, **변경 이력 트리거, RLS** |
| 0007 views_grants | 롱포맷 리더보드 MV, 역할별 권한 |

---

## 2. 실행 방법

### Docker Compose
```bash
cp .env.example .env        # 비밀번호 변경
docker compose up -d db
docker compose run --rm migrate   # alembic upgrade head && sync-config
```
> 이 저장소 작업 환경에는 Docker 데몬이 없어 compose 자체는 실행해 보지 못했다. 같은 순서(초기화 스크립트 → 마이그레이션 → 설정 동기화)를 로컬 PostgreSQL 16 클러스터에서 검증했다.

### 로컬
```bash
cd backend
pip install -e ".[dev]"
export DATABASE_URL=postgresql://user:pw@localhost:5432/sports
alembic upgrade head
python -m app.cli validate-config     # DB 없이 설정만 검증
python -m app.cli sync-config --dry-run
python -m app.cli sync-config
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres pytest   # DB 테스트는 임시 DB 생성 후 삭제
```

---

## 3. 시드 요약

| 종목 | 지표 (파생) | 구간 | 포지션 | 이벤트 타입 | 종목 스플릿 | 자격 조건 |
|---|---|---|---|---|---|---|
| 야구 | 76 (24) | INN | 12 | PA, PITCH, BASERUNNING, SUB | 상대 투수/타자 유형, 주자 상황, 이닝별 | 규정 타석, 규정 이닝 |
| 농구 | 38 (16) | Q, OT | 3 | SHOT, REBOUND, TURNOVER, FOUL, SUB, TIMEOUT | 쿼터별 | 최소 출전 경기 (임시) |
| 배구 | 39 (10) | SET | 5 | RALLY, SUB, TIMEOUT, CHALLENGE | 세트별 | 공격·리시브 순위 기준 (임시) |
| 축구 | 42 (10) | H, ET, PSO | 4 | GOAL, SHOT, CARD, SUB, PSO_KICK | 전·후반 | 최소 출전, 골키퍼 (임시) |
| 공통 | — | — | — | — | 홈/원정, 월, 요일, 상대팀, 경기장, 대회 단계, 결과 | — |

요구사항의 대표 지표(야구 타율·OPS·ERA·WHIP·FIP·wOBA, 농구 득점·리바운드·어시스트·FG%·3P%·TS%·PER·+/-, 배구 공격 성공률·블로킹·서브 에이스·리시브 효율·디그, 축구 골·어시스트·슈팅·유효슈팅·패스 성공률·태클·출전 시간·xG)는 모두 정의되어 있고, 테스트가 누락 여부를 검사한다.

---

## 4. 1단계 설계에서 달라진 점

| 항목 | 1단계 설계 | 2단계 구현 | 이유 |
|---|---|---|---|
| 출전 시간 | `player_match_stat.played_sec` 공통 컬럼 | 종목 지표 `SEC`(stats JSONB) | 시즌 누적에도 같은 코드가 필요해 중복 저장을 피함 |
| 야구 이닝 | IP | `pit.OUTS`(아웃카운트) 저장, `pit.IP = OUTS/3` 파생 | 6⅓ 같은 표기를 합산하면 틀림. 수집기가 변환 |
| 리그 상수 | 값만 저장 | `league_constant_definition`에 **수식** 정의 가능 (리그 합계 기반). 수식이 없으면 수동 입력 | FIP 상수, PER의 VOP 등을 종목별 코드 없이 계산 |
| 수식 참조 범위 | 자기 지표 + `lg.` | + `team.`(소속 팀 같은 레벨), `ctx.`(팀 경기 수 등 공통 문맥) | PER(팀 어시스트 비율·페이스), 규정 타석 계산 |
| 지표 코드 | 모든 종목 카테고리 접두어 | 야구만 `bat./pit./fld.` 접두어, 다른 종목은 접두어 없이 `category`로 분류, 순위 지표는 전 종목 `std.` | 야구만 타자/투수 약어 충돌. 커스텀 수식 작성 편의 |
| 데이터 소스 | — | `collection_allowed` 게이트 (기본 false) | 약관·robots.txt 검토 전 요청 차단 |
| CSV 템플릿 | `upload_template` 테이블 | 제거. `stat_definition` + `custom_field`로 런타임 생성 | 지표 정의와 템플릿 이중 관리 방지 |
| 분석가 정의 필드 | — | `analyst.custom_field` (`cf.` 접두어) 추가 | 공개 데이터에 없는 수치 입력용 |
| `custom_metric_value` | 선택적 캐시 | 7단계로 연기 | 캐시 전략은 커스텀 지표 빌더와 함께 결정 |
| 시즌 참가 팀 / 팀명 이력 | — | `core.season_team`, `core.team_name_history` 추가 | K리그 승강제, 구단명 변경 |
| SQL 콘솔 역할 | analyst는 RLS 뷰로 노출 | `app_readonly`에 analyst 미노출 | 콘솔에서 `SET app.user_id`로 RLS 우회 가능 |
| MV 갱신 | 해당 시즌만 | 전체 `CONCURRENTLY` | PostgreSQL MV는 부분 갱신 불가. 현재 규모에서 충분 |

---

## 5. 핵심 동작 (테스트로 검증됨)

| 동작 | 구현 |
|---|---|
| 자동 수집 데이터 수정 불가 | `app_api`는 `core`/`config`/`ingest`에 SELECT만 → UPDATE 시 `permission denied` |
| 구간 NULL 포함 자연키 | `UNIQUE NULLS NOT DISTINCT (match_id, player_id, period_id)` |
| 스테이지–시즌 일관성 | `(stage_id, season_id)` 복합 FK |
| 경기 상태 규칙 | 종료 경기는 스코어 필수, 홈≠원정 |
| 이벤트 파티션 | `core.event_y2023`~`y2027` + default, `util.ensure_event_partition(연도)` |
| 미정의 지표 키 탐지 | `core.v_stat_key_violations` (원시 컬럼에 파생 지표를 넣은 경우도 탐지) |
| 리더보드 MV | `util.refresh_stat_views()` (SECURITY DEFINER, `app_ingest`만 실행 가능) |
| 공개 범위 | RLS: 비공개는 작성자만, 공유는 워크스페이스 멤버. 열람자는 작성 불가, 다른 분석가의 공유 행 수정 불가 |
| 작성자·이력 | `created_by` 기본값 = `app.user_id`, 수정 시 작성자 보존·버전 증가·수정자 기록, `change_log`는 트리거만 기록 |
| 설정 동기화 멱등성 | 재실행 시 변경 없음, YAML에서 빠진 항목은 삭제 대신 `is_active=false` |
| 설정 검증 | 미정의 지표/상수/문맥 변수 참조, 순환 참조, scope·레벨 불일치, 접두어–카테고리 불일치, 오타 키, cron·소스 참조 |

---

## 6. 알려진 한계 · 후속 단계로 넘긴 것

- **임시 자격 조건**: KBL·V-리그·K리그1의 리더보드 최소 조건은 공식 기준을 확인하지 못해 `is_provisional = true`로 표시한 임시값이다. KBO 규정 타석(팀 경기 × 3.1)·규정 이닝(팀 경기 × 1)만 확정값이다.
- **wOBA 가중치**: 득점 기대값 표가 필요해 리그 합계로는 계산할 수 없다. 시즌별 수동 입력(출처 기록) 대상이며, 입력 전에는 wOBA가 계산되지 않는다(None).
- **PER**: Hollinger 방식이다. 리그 평균 uPER(`LG_UPER`)은 리그 합계를 한 명의 선수로 보고 근사한다. 시즌 합산 행(이적 선수 `team_id = NULL`)은 팀 문맥이 없어 PER이 계산되지 않는다.
- **수집 스케줄 시각**: 종목별 경기 종료 시각을 기준으로 잡은 가정값이다. 3·4단계에서 소스 갱신 시점을 확인한 뒤 조정한다.
- **소스 URL**: 공식 사이트 도메인만 등록했고, 약관·robots.txt는 아직 확인하지 않았다. 야구 소스는 3단계 시작 시 확인한다.
- **`position_code` FK 없음**: 기록 테이블의 포지션 코드는 앱 검증에 맡긴다(종목 컬럼이 없어 복합 FK를 걸 수 없음).
