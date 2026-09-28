"""검증된 설정을 DB 에 반영 (upsert).

- 자연키(종목 코드 + 항목 코드) 기준 upsert. 재실행해도 결과가 같다 (멱등).
- YAML 에서 사라진 항목은 삭제하지 않고 is_active = false 로 바꾼다
  (이미 수집된 JSONB 기록이 해당 코드를 참조할 수 있으므로).
- 수집 스케줄은 YAML 에 없으면 enabled = false.
- is_hot 지표에 대한 식 인덱스를 생성/정리한다.
- 전체를 하나의 트랜잭션으로 실행한다. dry_run 이면 마지막에 롤백한다.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from config_sync.loader import ConfigBundle, LoadedSport
from config_sync.models import SplitDef
from stats_engine.formula import parse

HOT_INDEX_PREFIX = "ix_hot_"
_SAFE_STAT_CODE = re.compile(r"^([a-z][a-z0-9]*\.)?[A-Z][A-Z0-9_]*$")


@dataclass
class SyncReport:
    """테이블별 반영 건수."""

    upserted: Counter = field(default_factory=Counter)
    deactivated: Counter = field(default_factory=Counter)
    hot_indexes_created: list[str] = field(default_factory=list)
    hot_indexes_dropped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"  {t}: upsert {n}건" + (f", 비활성화 {self.deactivated[t]}건" if self.deactivated[t] else "")
               for t, n in sorted(self.upserted.items())]
        for t, n in sorted(self.deactivated.items()):
            if t not in self.upserted:
                out.append(f"  {t}: 비활성화 {n}건")
        if self.hot_indexes_created:
            out.append(f"  식 인덱스 생성: {len(self.hot_indexes_created)}개")
        if self.hot_indexes_dropped:
            out.append(f"  식 인덱스 삭제: {len(self.hot_indexes_dropped)}개")
        return out


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def sync_bundle(conn: Connection, bundle: ConfigBundle) -> SyncReport:
    """호출자가 트랜잭션을 관리한다 (commit/rollback)."""
    report = SyncReport()
    sport_ids: dict[str, int] = {}
    for code, loaded in sorted(bundle.sports.items()):
        sport_ids[code] = _sync_sport(conn, loaded, report)
    _sync_splits(conn, None, bundle.common.splits, report)
    source_ids = _sync_sources(conn, bundle, report)
    _sync_leagues(conn, bundle, sport_ids, source_ids, report)
    _sync_hot_indexes(conn, report)
    return report


# ---------------------------------------------------------------------------
# 종목
# ---------------------------------------------------------------------------
def _deactivate_missing(conn: Connection, table: str, sport_id: int, codes: list[str],
                        report: SyncReport) -> None:
    # table 은 이 모듈 내부 상수만 전달된다 (사용자 입력 아님)
    result = conn.execute(
        text(f"UPDATE {table} SET is_active = false "
             "WHERE sport_id = :sid AND is_active AND NOT (code = ANY(:codes))"),
        {"sid": sport_id, "codes": codes},
    )
    report.deactivated[table] += result.rowcount


def _sync_sport(conn: Connection, loaded: LoadedSport, report: SyncReport) -> int:
    cfg = loaded.config
    s = cfg.sport
    sport_id = conn.execute(
        text("""
            INSERT INTO config.sport (code, name_ko, name_en, period_label_ko, score_unit_ko, clock_type, settings, is_active)
            VALUES (:code, :name_ko, :name_en, :period_label_ko, :score_unit_ko, :clock_type, CAST(:settings AS jsonb), true)
            ON CONFLICT (code) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, name_en = EXCLUDED.name_en,
                period_label_ko = EXCLUDED.period_label_ko, score_unit_ko = EXCLUDED.score_unit_ko,
                clock_type = EXCLUDED.clock_type, settings = EXCLUDED.settings, is_active = true
            RETURNING id
        """),
        {**s.model_dump(exclude={"settings"}), "settings": _j(s.settings)},
    ).scalar_one()
    report.upserted["config.sport"] += 1

    # 구간
    for i, p in enumerate(cfg.periods):
        conn.execute(text("""
            INSERT INTO config.period_definition
                (sport_id, code, name_ko, label_pattern, regulation_count, max_count, is_overtime,
                 nominal_duration_sec, sort_order, is_active)
            VALUES (:sid, :code, :name_ko, :label_pattern, :regulation_count, :max_count, :is_overtime,
                    :nominal_duration_sec, :sort_order, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, label_pattern = EXCLUDED.label_pattern,
                regulation_count = EXCLUDED.regulation_count, max_count = EXCLUDED.max_count,
                is_overtime = EXCLUDED.is_overtime, nominal_duration_sec = EXCLUDED.nominal_duration_sec,
                sort_order = EXCLUDED.sort_order, is_active = true
        """), {"sid": sport_id, "sort_order": i + 1, **p.model_dump()})
    report.upserted["config.period_definition"] += len(cfg.periods)
    _deactivate_missing(conn, "config.period_definition", sport_id, [p.code for p in cfg.periods], report)

    # 포지션
    for i, p in enumerate(cfg.positions):
        conn.execute(text("""
            INSERT INTO config.position_definition (sport_id, code, name_ko, name_en, position_group, sort_order, is_active)
            VALUES (:sid, :code, :name_ko, :name_en, :group, :sort_order, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, name_en = EXCLUDED.name_en,
                position_group = EXCLUDED.position_group, sort_order = EXCLUDED.sort_order, is_active = true
        """), {"sid": sport_id, "sort_order": i + 1, **p.model_dump()})
    report.upserted["config.position_definition"] += len(cfg.positions)
    _deactivate_missing(conn, "config.position_definition", sport_id, [p.code for p in cfg.positions], report)

    # 이벤트 타입
    for i, e in enumerate(cfg.event_types):
        conn.execute(text("""
            INSERT INTO config.event_type_definition
                (sport_id, code, name_ko, participant_roles, attr_schema, sort_order, is_active)
            VALUES (:sid, :code, :name_ko, :roles, CAST(:attr_schema AS jsonb), :sort_order, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, participant_roles = EXCLUDED.participant_roles,
                attr_schema = EXCLUDED.attr_schema, sort_order = EXCLUDED.sort_order, is_active = true
        """), {"sid": sport_id, "code": e.code, "name_ko": e.name_ko, "roles": e.participant_roles,
               "attr_schema": _j(e.attr_schema), "sort_order": i + 1})
    report.upserted["config.event_type_definition"] += len(cfg.event_types)
    _deactivate_missing(conn, "config.event_type_definition", sport_id, [e.code for e in cfg.event_types], report)

    # 지표 카테고리
    category_ids: dict[str, int] = {}
    for i, c in enumerate(cfg.stat_categories):
        category_ids[c.code] = conn.execute(text("""
            INSERT INTO config.stat_category (sport_id, code, name_ko, name_en, sort_order, is_active)
            VALUES (:sid, :code, :name_ko, :name_en, :sort_order, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, name_en = EXCLUDED.name_en,
                sort_order = EXCLUDED.sort_order, is_active = true
            RETURNING id
        """), {"sid": sport_id, "code": c.code, "name_ko": c.name_ko, "name_en": c.name_en,
               "sort_order": i + 1}).scalar_one()
    report.upserted["config.stat_category"] += len(cfg.stat_categories)
    _deactivate_missing(conn, "config.stat_category", sport_id, list(category_ids), report)

    # 지표 정의
    for st in loaded.stats:
        conn.execute(text("""
            INSERT INTO config.stat_definition
                (sport_id, category_id, code, name_ko, name_en, abbr, scope, levels, unit, data_type,
                 aggregation, decimals, higher_is_better, is_derived, formula, depends_on, is_hot,
                 display_order, description, is_active)
            VALUES (:sid, :category_id, :code, :name_ko, :name_en, :abbr, :scope, :levels, :unit, :data_type,
                    :aggregation, :decimals, :higher_is_better, :is_derived, :formula, :depends_on, :is_hot,
                    :display_order, :description, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                category_id = EXCLUDED.category_id, name_ko = EXCLUDED.name_ko, name_en = EXCLUDED.name_en,
                abbr = EXCLUDED.abbr, scope = EXCLUDED.scope, levels = EXCLUDED.levels, unit = EXCLUDED.unit,
                data_type = EXCLUDED.data_type, aggregation = EXCLUDED.aggregation,
                decimals = EXCLUDED.decimals, higher_is_better = EXCLUDED.higher_is_better,
                is_derived = EXCLUDED.is_derived, formula = EXCLUDED.formula,
                depends_on = EXCLUDED.depends_on, is_hot = EXCLUDED.is_hot,
                display_order = EXCLUDED.display_order, description = EXCLUDED.description, is_active = true
        """), {
            **st.model_dump(exclude={"category"}),
            "sid": sport_id,
            "category_id": category_ids[st.category],
            "levels": list(st.levels),
            "depends_on": list(st.depends_on),
        })
    report.upserted["config.stat_definition"] += len(loaded.stats)
    _deactivate_missing(conn, "config.stat_definition", sport_id, [s.code for s in loaded.stats], report)

    # 리그 상수 정의
    for c in cfg.league_constants:
        depends_on = sorted(parse(c.formula).variables) if c.formula else []
        conn.execute(text("""
            INSERT INTO config.league_constant_definition
                (sport_id, code, name_ko, formula, depends_on, decimals, description, is_active)
            VALUES (:sid, :code, :name_ko, :formula, :depends_on, :decimals, :description, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, formula = EXCLUDED.formula, depends_on = EXCLUDED.depends_on,
                decimals = EXCLUDED.decimals, description = EXCLUDED.description, is_active = true
        """), {"sid": sport_id, "depends_on": depends_on, **c.model_dump()})
    report.upserted["config.league_constant_definition"] += len(cfg.league_constants)
    _deactivate_missing(conn, "config.league_constant_definition", sport_id,
                        [c.code for c in cfg.league_constants], report)

    _sync_splits(conn, sport_id, cfg.splits, report)

    # 자격 조건
    for q in cfg.qualification_rules:
        conn.execute(text("""
            INSERT INTO config.qualification_rule
                (sport_id, category_id, code, name_ko, rule_expression, depends_on, is_provisional,
                 description, is_active)
            VALUES (:sid, :category_id, :code, :name_ko, :rule, :depends_on, :provisional, :description, true)
            ON CONFLICT (sport_id, code) DO UPDATE SET
                category_id = EXCLUDED.category_id, name_ko = EXCLUDED.name_ko,
                rule_expression = EXCLUDED.rule_expression, depends_on = EXCLUDED.depends_on,
                is_provisional = EXCLUDED.is_provisional, description = EXCLUDED.description, is_active = true
        """), {**q.model_dump(), "sid": sport_id, "category_id": category_ids[q.category],
               "depends_on": sorted(parse(q.rule).variables)})
    report.upserted["config.qualification_rule"] += len(cfg.qualification_rules)
    _deactivate_missing(conn, "config.qualification_rule", sport_id,
                        [q.code for q in cfg.qualification_rules], report)
    return sport_id


def _sync_splits(conn: Connection, sport_id: int | None, splits: list[SplitDef], report: SyncReport) -> None:
    for i, s in enumerate(splits):
        conn.execute(text("""
            INSERT INTO config.split_definition
                (sport_id, code, name_ko, source, key_expression, value_labels, sort_order, description, is_active)
            VALUES (:sid, :code, :name_ko, :source, :key, CAST(:value_labels AS jsonb), :sort_order, :description, true)
            ON CONFLICT ON CONSTRAINT uq_split_definition DO UPDATE SET
                name_ko = EXCLUDED.name_ko, source = EXCLUDED.source, key_expression = EXCLUDED.key_expression,
                value_labels = EXCLUDED.value_labels, sort_order = EXCLUDED.sort_order,
                description = EXCLUDED.description, is_active = true
        """), {**s.model_dump(), "sid": sport_id, "value_labels": _j(s.value_labels), "sort_order": i + 1})
    report.upserted["config.split_definition"] += len(splits)
    result = conn.execute(
        text("UPDATE config.split_definition SET is_active = false "
             "WHERE sport_id IS NOT DISTINCT FROM :sid AND is_active AND NOT (code = ANY(:codes))"),
        {"sid": sport_id, "codes": [s.code for s in splits]},
    )
    report.deactivated["config.split_definition"] += result.rowcount


# ---------------------------------------------------------------------------
# 소스·리그·스케줄
# ---------------------------------------------------------------------------
def _sync_sources(conn: Connection, bundle: ConfigBundle, report: SyncReport) -> dict[str, int]:
    ids: dict[str, int] = {}
    for code, src in sorted(bundle.sources.items()):
        # collection_allowed 는 YAML 값으로 덮어쓴다: 약관 검토 결과를 코드 리뷰로 남기기 위함
        ids[code] = conn.execute(text("""
            INSERT INTO ingest.data_source
                (code, name, base_url, terms_url, robots_url, terms_note, collection_allowed,
                 min_interval_ms, max_retries, user_agent)
            VALUES (:code, :name, :base_url, :terms_url, :robots_url, :terms_note, :collection_allowed,
                    :min_interval_ms, :max_retries, :user_agent)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name, base_url = EXCLUDED.base_url, terms_url = EXCLUDED.terms_url,
                robots_url = EXCLUDED.robots_url, terms_note = EXCLUDED.terms_note,
                collection_allowed = EXCLUDED.collection_allowed, min_interval_ms = EXCLUDED.min_interval_ms,
                max_retries = EXCLUDED.max_retries, user_agent = EXCLUDED.user_agent
            RETURNING id
        """), src.model_dump()).scalar_one()
    report.upserted["ingest.data_source"] += len(ids)
    return ids


def _sync_leagues(conn: Connection, bundle: ConfigBundle, sport_ids: dict[str, int],
                  source_ids: dict[str, int], report: SyncReport) -> None:
    for code, lg in sorted(bundle.leagues.items()):
        m = lg.league
        league_id = conn.execute(text("""
            INSERT INTO core.league
                (sport_id, code, name_ko, name_en, country, gender, tier, timezone, collector_key, settings, is_active)
            VALUES (:sid, :code, :name_ko, :name_en, :country, :gender, :tier, :timezone, :collector_key,
                    CAST(:settings AS jsonb), true)
            ON CONFLICT (code) DO UPDATE SET
                sport_id = EXCLUDED.sport_id, name_ko = EXCLUDED.name_ko, name_en = EXCLUDED.name_en,
                country = EXCLUDED.country, gender = EXCLUDED.gender, tier = EXCLUDED.tier,
                timezone = EXCLUDED.timezone, collector_key = EXCLUDED.collector_key,
                settings = EXCLUDED.settings, is_active = true
            RETURNING id
        """), {**m.model_dump(exclude={"sport", "settings"}), "sid": sport_ids[m.sport],
               "settings": _j(m.settings)}).scalar_one()
        report.upserted["core.league"] += 1

        keep: list[int] = []
        for sch in lg.schedules:
            keep.append(conn.execute(text("""
                INSERT INTO ingest.collection_schedule
                    (league_id, source_id, job_type, cron, timezone, offseason_policy, enabled, params)
                VALUES (:lid, :srcid, :job_type, :cron, :tz, :offseason_policy, :enabled, CAST(:params AS jsonb))
                ON CONFLICT (league_id, source_id, job_type) DO UPDATE SET
                    cron = EXCLUDED.cron, timezone = EXCLUDED.timezone,
                    offseason_policy = EXCLUDED.offseason_policy, enabled = EXCLUDED.enabled,
                    params = EXCLUDED.params
                RETURNING id
            """), {"lid": league_id, "srcid": source_ids[sch.source], "job_type": sch.job_type,
                   "cron": sch.cron, "tz": m.timezone, "offseason_policy": sch.offseason_policy,
                   "enabled": sch.enabled, "params": _j(sch.params)}).scalar_one())
        report.upserted["ingest.collection_schedule"] += len(keep)
        result = conn.execute(
            text("UPDATE ingest.collection_schedule SET enabled = false "
                 "WHERE league_id = :lid AND enabled AND NOT (id = ANY(:keep))"),
            {"lid": league_id, "keep": keep},
        )
        report.deactivated["ingest.collection_schedule"] += result.rowcount


# ---------------------------------------------------------------------------
# 핫 지표 식 인덱스
# ---------------------------------------------------------------------------
def hot_index_name(table: str, column: str, code: str) -> str:
    digest = hashlib.md5(f"{table}:{column}:{code}".encode()).hexdigest()[:12]
    return f"{HOT_INDEX_PREFIX}{table.split('.')[-1][:24]}_{digest}"


def _sync_hot_indexes(conn: Connection, report: SyncReport) -> None:
    """is_hot 지표마다 경기 기록 테이블에 ((stats->>'code')::numeric) 부분 인덱스를 둔다."""
    rows = conn.execute(text("""
        SELECT DISTINCT code, is_derived, scope FROM config.stat_definition
        WHERE is_hot AND is_active AND 'match' = ANY(levels)
    """)).all()
    wanted: dict[str, str] = {}
    for code, is_derived, scope in rows:
        if not _SAFE_STAT_CODE.match(code):  # DDL 에 직접 들어가므로 재확인
            raise ValueError(f"잘못된 지표 코드: {code}")
        column = "derived" if is_derived else "stats"
        tables = {"player": ["core.player_match_stat"], "team": ["core.team_match_stat"],
                  "both": ["core.player_match_stat", "core.team_match_stat"]}[scope]
        for table in tables:
            name = hot_index_name(table, column, code)
            wanted[name] = (
                f"CREATE INDEX {name} ON {table} ((({column} ->> '{code}')::numeric)) "
                f"WHERE {column} ? '{code}'"
            )

    existing = set(conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'core' AND indexname LIKE :p"
    ), {"p": HOT_INDEX_PREFIX + "%"}).scalars())

    for name in sorted(existing - set(wanted)):
        conn.execute(text(f"DROP INDEX core.{name}"))
        report.hot_indexes_dropped.append(name)
    for name in sorted(set(wanted) - existing):
        conn.exec_driver_sql(wanted[name])
        report.hot_indexes_created.append(name)
