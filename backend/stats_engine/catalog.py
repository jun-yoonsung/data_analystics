"""DB 의 종목 설정(config 스키마)을 메모리로 읽은 카탈로그.

수집 검증, 파생 지표 계산, 집계가 모두 이 카탈로그만 보고 동작한다 (종목별 분기 없음).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property

from sqlalchemy import Connection, text

from stats_engine.formula import Formula, parse, topological_order

PLAYER_SCOPES = frozenset({"player", "both"})
TEAM_SCOPES = frozenset({"team", "both"})


@dataclass(frozen=True)
class StatInfo:
    code: str
    category: str
    scope: str
    levels: frozenset[str]
    data_type: str
    aggregation: str
    is_derived: bool
    formula: str | None

    @cached_property
    def parsed(self) -> Formula | None:
        return parse(self.formula) if self.formula else None

    def applies_to(self, level: str, subject: str) -> bool:
        """level ∈ period/match/season, subject ∈ player/team."""
        scopes = PLAYER_SCOPES if subject == "player" else TEAM_SCOPES
        return level in self.levels and self.scope in scopes


@dataclass(frozen=True)
class PeriodInfo:
    id: int
    code: str
    sort_order: int
    regulation_count: int


@dataclass(frozen=True)
class ConstantInfo:
    id: int
    code: str
    formula: str | None
    decimals: int

    @cached_property
    def parsed(self) -> Formula | None:
        return parse(self.formula) if self.formula else None


@dataclass
class SportCatalog:
    sport_id: int
    sport_code: str
    stats: dict[str, StatInfo]
    periods: dict[str, PeriodInfo]
    positions: frozenset[str]
    event_types: dict[str, int]
    constants: dict[str, ConstantInfo]
    _order_cache: dict[tuple[str, str], list[StatInfo]] = field(default_factory=dict, repr=False)
    _sum_cache: dict[tuple[str, str], list[StatInfo]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    def validate_stats(self, stats: dict, level: str, subject: str) -> tuple[dict, list[str]]:
        """수집 값 검증: 해당 레벨·주체의 원시 지표 코드만 남기고 나머지는 경고로 보고."""
        clean: dict = {}
        problems: list[str] = []
        for code, value in stats.items():
            info = self.stats.get(code)
            if info is None:
                problems.append(f"정의되지 않은 지표 {code}")
            elif info.is_derived:
                problems.append(f"파생 지표 {code} 는 수집 값으로 받지 않습니다")
            elif not info.applies_to(level, subject):
                problems.append(f"지표 {code} 는 {subject}/{level} 레벨 지표가 아닙니다")
            elif info.data_type == "text":
                if isinstance(value, str):
                    clean[code] = value
                else:
                    problems.append(f"지표 {code} 값은 문자열이어야 합니다: {value!r}")
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"지표 {code} 값이 숫자가 아닙니다: {value!r}")
            else:
                clean[code] = value
        return clean, problems

    def zero_fill_codes(self, present: Iterable[str], level: str, subject: str) -> list[str]:
        """기록에 있는 카테고리의 누락된 합계형 원시 지표 (0 으로 간주할 코드).

        박스스코어는 0 인 항목을 생략하는 경우가 많다 (예: 3루타 0개). 한 카테고리의 지표가 하나라도
        있으면 같은 카테고리의 합계형 원시 지표는 0 으로 보고 수식을 계산한다.
        """
        present = set(present)
        categories = {self.stats[c].category for c in present if c in self.stats}
        key = (level, subject)
        if key not in self._sum_cache:
            self._sum_cache[key] = [s for s in self.stats.values()
                                    if not s.is_derived and s.aggregation == "sum" and s.applies_to(level, subject)]
        return [s.code for s in self._sum_cache[key] if s.category in categories and s.code not in present]

    def derived_order(self, level: str, subject: str) -> list[StatInfo]:
        """해당 레벨·주체에서 계산할 파생 지표를 의존성 순서로."""
        key = (level, subject)
        if key not in self._order_cache:
            targets = {s.code: s for s in self.stats.values() if s.is_derived and s.applies_to(level, subject)}
            deps = {c: {v for v in s.parsed.local_variables() if v in targets} for c, s in targets.items()}
            self._order_cache[key] = [targets[c] for c in topological_order(deps)]
        return self._order_cache[key]

    def constant_order(self) -> list[ConstantInfo]:
        computed = {c.code: c for c in self.constants.values() if c.formula}
        deps = {code: set(c.parsed.namespaced("lg")) & set(computed) for code, c in computed.items()}
        return [computed[c] for c in topological_order(deps)]

    def period_ordinal_key(self, code: str, seq: int) -> tuple[int, int]:
        return (self.periods[code].sort_order, seq)


def load_catalog(conn: Connection, sport_id: int) -> SportCatalog:
    sport_code = conn.execute(text("SELECT code FROM config.sport WHERE id = :s"), {"s": sport_id}).scalar_one()
    stats = {
        r.code: StatInfo(code=r.code, category=r.category, scope=r.scope, levels=frozenset(r.levels), data_type=r.data_type,
                         aggregation=r.aggregation, is_derived=r.is_derived, formula=r.formula)
        for r in conn.execute(text("""
            SELECT sd.code, c.code AS category, sd.scope, sd.levels, sd.data_type, sd.aggregation,
                   sd.is_derived, sd.formula
            FROM config.stat_definition sd JOIN config.stat_category c ON c.id = sd.category_id
            WHERE sd.sport_id = :s AND sd.is_active"""), {"s": sport_id})
    }
    periods = {
        r.code: PeriodInfo(id=r.id, code=r.code, sort_order=r.sort_order, regulation_count=r.regulation_count)
        for r in conn.execute(text("""
            SELECT id, code, sort_order, regulation_count FROM config.period_definition
            WHERE sport_id = :s AND is_active"""), {"s": sport_id})
    }
    positions = frozenset(conn.execute(text(
        "SELECT code FROM config.position_definition WHERE sport_id = :s AND is_active"), {"s": sport_id}).scalars())
    event_types = dict(conn.execute(text(
        "SELECT code, id FROM config.event_type_definition WHERE sport_id = :s AND is_active"), {"s": sport_id}).all())
    constants = {
        r.code: ConstantInfo(id=r.id, code=r.code, formula=r.formula, decimals=r.decimals)
        for r in conn.execute(text("""
            SELECT id, code, formula, decimals FROM config.league_constant_definition
            WHERE sport_id = :s AND is_active"""), {"s": sport_id})
    }
    return SportCatalog(sport_id=sport_id, sport_code=sport_code, stats=stats, periods=periods,
                        positions=positions, event_types=event_types, constants=constants)
