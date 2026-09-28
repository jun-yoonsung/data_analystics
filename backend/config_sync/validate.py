"""설정 의미 검증.

구조(타입·필수 키)는 pydantic 이 검사하고, 여기서는 설정 간 참조 관계를 검사한다.
- 코드 형식·중복
- 파생 지표 수식이 참조하는 지표/상수/문맥 변수의 존재, 범위(scope)·레벨 호환
- 파생 지표·리그 상수의 순환 참조
- 스플릿, 자격 조건, 리그 스케줄 참조
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config_sync.loader import ConfigBundle, ConfigError, LoadedSport
from config_sync.models import ResolvedStat
from stats_engine.context import CONTEXT_VARIABLES
from stats_engine.formula import RESERVED_NAMESPACES, FormulaError, parse, topological_order

STAT_CODE_RE = re.compile(r"^([a-z][a-z0-9]*\.)?[A-Z][A-Z0-9_]*$")
UPPER_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
POSITION_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_]*$")
LOWER_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CATEGORY_RE = re.compile(r"^[a-z][a-z0-9]*$")
SPLIT_KEY_RE = re.compile(r"^(match|period|event|player)\.[a-z_][a-z0-9_.]*$")
CRON_FIELD_RE = re.compile(r"^[\d*/,\-]+$")

SCOPE_COVERS = {
    # 지표 scope → 의존 지표가 가져야 하는 scope
    "player": {"player", "both"},
    "team": {"team", "both"},
    "both": {"both"},
}


def _duplicates(codes: Iterable[str]) -> list[str]:
    return sorted(c for c, n in Counter(codes).items() if n > 1)


def validate_bundle(bundle: ConfigBundle) -> None:
    """모든 오류를 모아 ConfigError 로 던진다. 문제가 없으면 None."""
    errors: list[str] = []
    for sport in bundle.sports.values():
        errors += _validate_sport(sport, bundle)
    errors += _validate_common(bundle)
    errors += _validate_leagues(bundle)
    for src in bundle.sources.values():
        if not LOWER_CODE_RE.match(src.code):
            errors.append(f"source {src.code}: 코드 형식 오류")
    if errors:
        raise ConfigError(errors)


def _validate_common(bundle: ConfigBundle) -> list[str]:
    errors = []
    common_codes = [s.code for s in bundle.common.splits]
    for dup in _duplicates(common_codes):
        errors.append(f"_common: 스플릿 코드 중복 {dup}")
    for s in bundle.common.splits:
        errors += _check_split(s, "_common")
    for sport in bundle.sports.values():
        for s in sport.config.splits:
            if s.code in common_codes:
                errors.append(f"{sport.config.sport.code}: 스플릿 {s.code} 가 공통 스플릿과 중복됩니다")
    return errors


def _check_split(split, where: str) -> list[str]:
    errors = []
    if not LOWER_CODE_RE.match(split.code):
        errors.append(f"{where}: 스플릿 코드 형식 오류 {split.code}")
    if not SPLIT_KEY_RE.match(split.key):
        errors.append(f"{where}: 스플릿 {split.code} key 형식 오류 {split.key}")
    elif not split.key.startswith(split.source + "."):
        errors.append(f"{where}: 스플릿 {split.code} key({split.key}) 가 source({split.source}) 와 맞지 않습니다")
    return errors


def _validate_sport(loaded: LoadedSport, bundle: ConfigBundle) -> list[str]:
    cfg = loaded.config
    sc = cfg.sport.code
    errors: list[str] = []

    if not LOWER_CODE_RE.match(sc):
        errors.append(f"{sc}: 종목 코드 형식 오류")

    # --- 코드 형식·중복 -------------------------------------------------------
    for label, codes, regex in (
        ("구간", [p.code for p in cfg.periods], UPPER_CODE_RE),
        ("포지션", [p.code for p in cfg.positions], POSITION_CODE_RE),
        ("이벤트 타입", [e.code for e in cfg.event_types], UPPER_CODE_RE),
        ("지표 카테고리", [c.code for c in cfg.stat_categories], CATEGORY_RE),
        ("지표", [s.code for s in cfg.stats], STAT_CODE_RE),
        ("리그 상수", [c.code for c in cfg.league_constants], UPPER_CODE_RE),
        ("스플릿", [s.code for s in cfg.splits], LOWER_CODE_RE),
        ("자격 조건", [q.code for q in cfg.qualification_rules], LOWER_CODE_RE),
    ):
        for dup in _duplicates(codes):
            errors.append(f"{sc}: {label} 코드 중복 {dup}")
        for code in codes:
            if not regex.match(code):
                errors.append(f"{sc}: {label} 코드 형식 오류 {code}")

    if not any(p.regulation_count > 0 for p in cfg.periods):
        errors.append(f"{sc}: 정규 구간(regulation_count > 0)이 최소 하나 필요합니다")

    for c in cfg.stat_categories:
        if c.code in RESERVED_NAMESPACES:
            errors.append(f"{sc}: 카테고리 코드 {c.code} 는 예약어입니다")

    for e in cfg.event_types:
        for role in e.participant_roles:
            if not LOWER_CODE_RE.match(role):
                errors.append(f"{sc}: 이벤트 {e.code} 참여자 역할 형식 오류 {role}")

    # --- 지표 ---------------------------------------------------------------
    stats = {s.code: s for s in loaded.stats}
    constants = {c.code: c for c in cfg.league_constants}

    for s in loaded.stats:
        prefix = s.code.rpartition(".")[0]
        if prefix and prefix != s.category:
            errors.append(f"{sc}: 지표 {s.code} 접두어와 category({s.category})가 다릅니다")
        if len(set(s.levels)) != len(s.levels):
            errors.append(f"{sc}: 지표 {s.code} levels 중복")
        if s.is_derived and s.aggregation != "formula":
            errors.append(f"{sc}: 파생 지표 {s.code} 의 aggregation 은 formula 여야 합니다")
        if not s.is_derived and s.aggregation == "formula":
            errors.append(f"{sc}: 원시 지표 {s.code} 에 aggregation=formula 를 쓸 수 없습니다")
        if s.is_derived and s.data_type == "text":
            errors.append(f"{sc}: 파생 지표 {s.code} 는 text 타입일 수 없습니다")
        if s.is_derived:
            errors += _check_stat_formula(sc, s, stats, constants)

    # 파생 지표 순환 (team.X 참조도 계산 순서에 영향을 주므로 포함)
    derived_deps = {
        s.code: {d.removeprefix("team.") for d in s.depends_on if not d.startswith(("lg.", "ctx."))}
        for s in loaded.stats if s.is_derived
    }
    try:
        topological_order(derived_deps)
    except FormulaError as exc:
        errors.append(f"{sc}: 파생 지표 {exc}")

    # --- 리그 상수 -----------------------------------------------------------
    const_deps: dict[str, set[str]] = {}
    for c in cfg.league_constants:
        if c.formula is None:
            continue
        try:
            f = parse(c.formula)
        except FormulaError as exc:
            errors.append(f"{sc}: 리그 상수 {c.code} 수식 오류: {exc}")
            continue
        # 리그 상수 수식의 지역 변수 = 리그 합계 (팀 시즌 기록 합산)
        for v in sorted(f.local_variables()):
            st = stats.get(v)
            if st is None:
                errors.append(f"{sc}: 리그 상수 {c.code} 가 정의되지 않은 지표 {v} 를 참조합니다")
            elif st.scope == "player" or "season" not in st.levels:
                errors.append(f"{sc}: 리그 상수 {c.code} 는 팀 시즌 지표만 참조할 수 있습니다 ({v})")
        for v in sorted(f.namespaced("lg")):
            if v not in constants:
                errors.append(f"{sc}: 리그 상수 {c.code} 가 정의되지 않은 상수 lg.{v} 를 참조합니다")
        other = f.variables - f.local_variables() - {f"lg.{x}" for x in f.namespaced("lg")}
        for v in sorted(other):
            errors.append(f"{sc}: 리그 상수 {c.code} 에서 {v} 는 참조할 수 없습니다")
        const_deps[c.code] = set(f.namespaced("lg"))
    try:
        topological_order(const_deps)
    except FormulaError as exc:
        errors.append(f"{sc}: 리그 상수 {exc}")

    # --- 스플릿 --------------------------------------------------------------
    for s in cfg.splits:
        errors += _check_split(s, sc)

    # --- 자격 조건 ------------------------------------------------------------
    categories = {c.code for c in cfg.stat_categories}
    for q in cfg.qualification_rules:
        if q.category not in categories:
            errors.append(f"{sc}: 자격 조건 {q.code} 의 카테고리 {q.category} 가 없습니다")
        try:
            f = parse(q.rule)
        except FormulaError as exc:
            errors.append(f"{sc}: 자격 조건 {q.code} 수식 오류: {exc}")
            continue
        for v in sorted(f.local_variables()):
            st = stats.get(v)
            if st is None or st.scope == "team" or "season" not in st.levels:
                errors.append(f"{sc}: 자격 조건 {q.code} 가 선수 시즌 지표가 아닌 {v} 를 참조합니다")
        for v in sorted(f.namespaced("ctx")):
            if v not in CONTEXT_VARIABLES:
                errors.append(f"{sc}: 자격 조건 {q.code} 의 문맥 변수 ctx.{v} 가 없습니다 "
                              f"(허용: {', '.join(sorted(CONTEXT_VARIABLES))})")
        for v in sorted(f.variables - f.local_variables()):
            if not v.startswith("ctx."):
                errors.append(f"{sc}: 자격 조건 {q.code} 에서 {v} 는 참조할 수 없습니다")
    return errors


def _check_stat_formula(sc: str, s: ResolvedStat, stats: dict[str, ResolvedStat],
                        constants: dict) -> list[str]:
    errors = []
    for dep in s.depends_on:
        ns, _, rest = dep.partition(".")
        if ns == "lg":
            if rest not in constants:
                errors.append(f"{sc}: 지표 {s.code} 가 정의되지 않은 리그 상수 {dep} 를 참조합니다")
            continue
        if ns == "team":
            # 선수 지표가 소속 팀의 같은 레벨 지표를 참조
            if s.scope != "player":
                errors.append(f"{sc}: team.* 참조는 scope=player 지표에서만 쓸 수 있습니다 ({s.code})")
            target, needed_scope = stats.get(rest), {"team", "both"}
        elif ns in RESERVED_NAMESPACES:
            errors.append(f"{sc}: 지표 {s.code} 수식에서 {dep} 는 참조할 수 없습니다")
            continue
        else:
            target, needed_scope = stats.get(dep), SCOPE_COVERS[s.scope]
        if target is None:
            errors.append(f"{sc}: 지표 {s.code} 가 정의되지 않은 지표 {dep} 를 참조합니다")
            continue
        if target.data_type == "text":
            errors.append(f"{sc}: 지표 {s.code} 가 text 지표 {dep} 를 참조합니다")
        if target.scope not in needed_scope:
            errors.append(f"{sc}: 지표 {s.code}(scope={s.scope}) 가 scope={target.scope} 인 {dep} 를 참조합니다")
        missing = set(s.levels) - set(target.levels)
        if missing:
            errors.append(f"{sc}: 지표 {s.code} 는 {sorted(missing)} 레벨에서 계산되지만 "
                          f"{dep} 는 해당 레벨 값이 없습니다")
    return errors


def _validate_leagues(bundle: ConfigBundle) -> list[str]:
    errors = []
    for lg in bundle.leagues.values():
        meta = lg.league
        where = f"league {meta.code}"
        if not UPPER_CODE_RE.match(meta.code):
            errors.append(f"{where}: 코드 형식 오류")
        if meta.sport not in bundle.sports:
            errors.append(f"{where}: 종목 {meta.sport} 설정이 없습니다")
        try:
            ZoneInfo(meta.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"{where}: 알 수 없는 타임존 {meta.timezone}")
        seen = set()
        for sch in lg.schedules:
            key = (sch.source, sch.job_type)
            if key in seen:
                errors.append(f"{where}: 스케줄 중복 {key}")
            seen.add(key)
            if sch.source not in bundle.sources:
                errors.append(f"{where}: 스케줄 {sch.job_type} 의 소스 {sch.source} 가 없습니다")
            fields = sch.cron.split()
            if len(fields) != 5 or not all(CRON_FIELD_RE.match(f) for f in fields):
                errors.append(f"{where}: 스케줄 {sch.job_type} cron 형식 오류 '{sch.cron}'")
    return errors
