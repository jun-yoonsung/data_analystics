"""파생 지표 배치 계산, 시즌 집계, 리그 상수 계산.

모두 stat_definition / league_constant_definition 설정만 보고 동작한다 (종목별 코드 없음).

순서 (runner 가 호출)
  1. aggregate_stage      : 경기 기록(구간 전체 행) → origin='aggregated' 시즌 기록
  2. compute_constants    : 팀 시즌 기록 합계(리그 합계) → 수식이 있는 리그 상수
  3. derive_matches       : 경기 기록의 derived (팀 → 선수 순서, 선수 수식의 team.* 참조 때문)
  4. derive_stage_seasons : 시즌 기록의 derived (팀 → 선수)

비율 지표는 평균하지 않는다: 집계는 원시 지표만 aggregation 방식으로 합치고,
파생 지표는 합쳐진 원시 값으로 다시 계산한다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from stats_engine.catalog import SportCatalog

Values = dict[str, float | int | str]


@dataclass
class DeriveReport:
    derived_rows: int = 0
    aggregated_rows: int = 0
    constants: dict[str, float] = field(default_factory=dict)


def _numeric(values: dict) -> dict:
    return {k: v for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def evaluate_derived(catalog: SportCatalog, level: str, subject: str, stats: Values,
                     constants: dict[str, float], team_values: Values | None = None) -> dict[str, float]:
    """원시 값 + 문맥으로 파생 지표를 계산해 반환 (계산 불가 지표는 제외)."""
    env: dict = dict(_numeric(stats))
    for code in catalog.zero_fill_codes(env, level, subject):
        env[code] = 0
    env.update({f"lg.{k}": v for k, v in constants.items()})
    if team_values:
        team_env = dict(_numeric(team_values))
        for code in catalog.zero_fill_codes(team_env, level, "team"):
            team_env[code] = 0
        env.update({f"team.{k}": v for k, v in team_env.items()})
    out: dict[str, float] = {}
    for info in catalog.derived_order(level, subject):
        value = info.parsed.evaluate(env)
        if isinstance(value, bool) or value is None:
            continue
        value = round(float(value), 10)
        if info.data_type == "int" and value.is_integer():
            value = int(value)
        env[info.code] = value
        out[info.code] = value
    return out


def aggregate_values(catalog: SportCatalog, rows: list[Values], subject: str) -> Values:
    """여러 경기 기록 → 시즌 원시 값. rows 는 시간 순서 (aggregation=last 용)."""
    result: Values = {}
    buckets: dict[str, list] = defaultdict(list)
    for row in rows:
        for code, value in row.items():
            buckets[code].append(value)
    for code, vals in buckets.items():
        info = catalog.stats.get(code)
        if info is None or info.is_derived or not info.applies_to("season", subject):
            continue
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        agg = info.aggregation
        if agg == "last":
            result[code] = vals[-1]
        elif not nums or agg == "none":
            continue
        elif agg == "sum":
            result[code] = sum(nums)
        elif agg == "avg":
            result[code] = sum(nums) / len(nums)
        elif agg == "max":
            result[code] = max(nums)
        elif agg == "min":
            result[code] = min(nums)
    return result


class Deriver:
    def __init__(self, conn: Connection, catalog: SportCatalog) -> None:
        self.conn = conn
        self.catalog = catalog
        self.report = DeriveReport()
        self._constants: dict[int, dict[str, float]] = {}

    # ------------------------------------------------------------------
    # 리그 상수
    # ------------------------------------------------------------------
    def constants_for_season(self, season_id: int) -> dict[str, float]:
        if season_id not in self._constants:
            self._constants[season_id] = {
                code: float(v) for code, v in self.conn.execute(text("""
                    SELECT d.code, c.value FROM config.league_constant c
                    JOIN config.league_constant_definition d ON d.id = c.definition_id
                    WHERE c.season_id = :s AND d.is_active"""), {"s": season_id}).all()
            }
        return self._constants[season_id]

    def compute_constants(self, season_id: int) -> dict[str, float]:
        """정규시즌 팀 기록 합계로 수식 리그 상수 계산. 수동 입력(manual) 값은 덮어쓰지 않는다."""
        if not any(c.formula for c in self.catalog.constants.values()):
            return {}
        # 팀별로 collected 가 있으면 우선, 없으면 aggregated
        rows = self.conn.execute(text("""
            SELECT DISTINCT ON (t.stage_id, t.team_id) t.stats
            FROM core.team_season_stat t
            JOIN core.competition_stage cs ON cs.id = t.stage_id
            WHERE t.season_id = :s AND cs.stage_type = 'regular'
            ORDER BY t.stage_id, t.team_id, (t.origin = 'collected') DESC
        """), {"s": season_id}).scalars().all()
        if not rows:
            return {}
        totals: Values = {}
        for stats in rows:
            for code, v in _numeric(stats).items():
                info = self.catalog.stats.get(code)
                if info is not None and info.aggregation == "sum":
                    totals[code] = totals.get(code, 0) + v

        self._constants.pop(season_id, None)
        known = dict(self.constants_for_season(season_id))
        manual = set(self.conn.execute(text("""
            SELECT d.code FROM config.league_constant c JOIN config.league_constant_definition d ON d.id = c.definition_id
            WHERE c.season_id = :s AND c.origin <> 'computed'"""), {"s": season_id}).scalars())
        env_stats = {**totals, **evaluate_derived(self.catalog, "season", "team", totals, known)}
        computed: dict[str, float] = {}
        for const in self.catalog.constant_order():
            if const.code in manual:
                continue
            env = {**env_stats, **{f"lg.{k}": v for k, v in known.items()}}
            value = const.parsed.evaluate(env)
            if value is None or isinstance(value, bool):
                continue
            known[const.code] = computed[const.code] = float(value)
            self.conn.execute(text("""
                INSERT INTO config.league_constant (season_id, definition_id, value, origin, computed_at)
                VALUES (:s, :d, :v, 'computed', now())
                ON CONFLICT (season_id, definition_id) DO UPDATE
                    SET value = EXCLUDED.value, computed_at = now()
                    WHERE config.league_constant.origin = 'computed'
            """), {"s": season_id, "d": const.id, "v": value})
        self._constants[season_id] = known
        self.report.constants.update(computed)
        return computed

    # ------------------------------------------------------------------
    # 시즌 집계
    # ------------------------------------------------------------------
    def aggregate_stage(self, stage_id: int) -> None:
        season_id = self.conn.execute(text("SELECT season_id FROM core.competition_stage WHERE id = :s"),
                                      {"s": stage_id}).scalar_one()
        base = """
            FROM {table} x JOIN core.match m ON m.id = x.match_id
            WHERE m.stage_id = :st AND m.status = 'final' AND x.period_id IS NULL
            ORDER BY m.local_date, m.scheduled_at, m.id
        """
        # 선수: 팀별 + 시즌 합산(team_id NULL)
        per_key: dict[tuple, list] = defaultdict(list)
        for player_id, team_id, stats in self.conn.execute(text(
                "SELECT x.player_id, x.team_id, x.stats " + base.format(table="core.player_match_stat")),
                {"st": stage_id}):
            per_key[(player_id, team_id)].append(stats)
            per_key[(player_id, None)].append(stats)
        for (player_id, team_id), rows in per_key.items():
            self._upsert_aggregated("core.player_season_stat", "ON CONSTRAINT uq_player_season_stat",
                                    {"stage_id": stage_id, "player_id": player_id, "team_id": team_id},
                                    season_id, aggregate_values(self.catalog, rows, "player"))
        per_team: dict[int, list] = defaultdict(list)
        for team_id, stats in self.conn.execute(text(
                "SELECT x.team_id, x.stats " + base.format(table="core.team_match_stat")), {"st": stage_id}):
            per_team[team_id].append(stats)
        for team_id, rows in per_team.items():
            self._upsert_aggregated("core.team_season_stat", "(stage_id, team_id, origin)",
                                    {"stage_id": stage_id, "team_id": team_id}, season_id,
                                    aggregate_values(self.catalog, rows, "team"))

    def _upsert_aggregated(self, table: str, conflict: str, key: dict, season_id: int, stats: Values) -> None:
        if not stats:
            return
        cols = [*key, "season_id", "origin", "stats"]
        vals = [f":{k}" for k in key] + [":season_id", "'aggregated'", "CAST(:stats AS jsonb)"]
        n = self.conn.execute(text(f"""
            INSERT INTO {table} AS t ({', '.join(cols)}) VALUES ({', '.join(vals)})
            ON CONFLICT {conflict} DO UPDATE SET stats = EXCLUDED.stats
            WHERE t.stats IS DISTINCT FROM EXCLUDED.stats
        """), {**key, "season_id": season_id, "stats": json.dumps(stats)}).rowcount
        self.report.aggregated_rows += n

    # ------------------------------------------------------------------
    # 파생 지표
    # ------------------------------------------------------------------
    def _write_derived(self, table: str, row_id: int, derived: dict) -> None:
        n = self.conn.execute(text(f"""
            UPDATE {table} SET derived = CAST(:d AS jsonb), derived_at = now()
            WHERE id = :id AND derived IS DISTINCT FROM CAST(:d AS jsonb)
        """), {"id": row_id, "d": json.dumps(derived)}).rowcount
        self.report.derived_rows += n

    def derive_matches(self, match_ids: set[int]) -> None:
        if not match_ids:
            return
        ids = sorted(match_ids)
        team_values: dict[tuple, Values] = {}
        for r in self.conn.execute(text("""
            SELECT x.id, x.match_id, x.team_id, x.period_id, x.stats, m.season_id
            FROM core.team_match_stat x JOIN core.match m ON m.id = x.match_id
            WHERE x.match_id = ANY(:ids)"""), {"ids": ids}):
            level = "period" if r.period_id else "match"
            derived = evaluate_derived(self.catalog, level, "team", r.stats, self.constants_for_season(r.season_id))
            self._write_derived("core.team_match_stat", r.id, derived)
            team_values[(r.match_id, r.team_id, r.period_id)] = {**r.stats, **derived}
        for r in self.conn.execute(text("""
            SELECT x.id, x.match_id, x.team_id, x.period_id, x.stats, m.season_id
            FROM core.player_match_stat x JOIN core.match m ON m.id = x.match_id
            WHERE x.match_id = ANY(:ids)"""), {"ids": ids}):
            level = "period" if r.period_id else "match"
            derived = evaluate_derived(self.catalog, level, "player", r.stats,
                                       self.constants_for_season(r.season_id),
                                       team_values.get((r.match_id, r.team_id, r.period_id)))
            self._write_derived("core.player_match_stat", r.id, derived)

    def derive_stage_seasons(self, stage_id: int) -> None:
        team_values: dict[tuple, Values] = {}
        for r in self.conn.execute(text("""
            SELECT id, team_id, origin, season_id, stats FROM core.team_season_stat WHERE stage_id = :s
        """), {"s": stage_id}):
            derived = evaluate_derived(self.catalog, "season", "team", r.stats, self.constants_for_season(r.season_id))
            self._write_derived("core.team_season_stat", r.id, derived)
            team_values[(r.team_id, r.origin)] = {**r.stats, **derived}
        for r in self.conn.execute(text("""
            SELECT id, team_id, origin, season_id, stats FROM core.player_season_stat WHERE stage_id = :s
        """), {"s": stage_id}):
            # 선수 수식의 team.* 는 같은 출처(origin)의 소속 팀 시즌 기록. 합산 행(team_id NULL)은 문맥 없음
            team_ctx = team_values.get((r.team_id, r.origin)) if r.team_id else None
            derived = evaluate_derived(self.catalog, "season", "player", r.stats,
                                       self.constants_for_season(r.season_id), team_ctx)
            self._write_derived("core.player_season_stat", r.id, derived)

    # ------------------------------------------------------------------
    def run(self, match_ids: set[int], stage_ids: set[int]) -> DeriveReport:
        """변경된 경기·스테이지에 대해 1~4 단계를 실행."""
        for stage_id in sorted(stage_ids):
            self.aggregate_stage(stage_id)
        seasons = set(self.conn.execute(text(
            "SELECT DISTINCT season_id FROM core.competition_stage WHERE id = ANY(:ids)"),
            {"ids": sorted(stage_ids)}).scalars()) if stage_ids else set()
        match_ids = set(match_ids)
        for season_id in sorted(seasons):
            before = dict(self.constants_for_season(season_id))
            self.compute_constants(season_id)
            if self.constants_for_season(season_id) != before:
                # 리그 상수가 바뀌면 시즌 전체 기록의 파생 지표를 다시 계산
                match_ids |= set(self.conn.execute(text(
                    "SELECT id FROM core.match WHERE season_id = :s"), {"s": season_id}).scalars())
                stage_ids = set(stage_ids) | set(self.conn.execute(text(
                    "SELECT id FROM core.competition_stage WHERE season_id = :s"), {"s": season_id}).scalars())
        self.derive_matches(match_ids)
        for stage_id in sorted(stage_ids):
            self.derive_stage_seasons(stage_id)
        return self.report
