"""설정 디렉터리(config/) 로드 및 기본값 해석."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from config_sync.models import (
    DEFAULT_DECIMALS,
    CommonConfig,
    LeagueConfig,
    ResolvedStat,
    SourceConfig,
    SportConfig,
    StatDef,
)
from stats_engine.formula import FormulaError, parse

# 저장소 루트의 config/ (컨테이너에서는 CONFIG_DIR 로 지정)
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class ConfigError(Exception):
    """설정 파일 오류. errors 에 모든 오류 메시지를 모아서 보고한다."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass
class LoadedSport:
    config: SportConfig
    stats: list[ResolvedStat]
    path: Path


@dataclass
class ConfigBundle:
    """config/ 전체를 읽은 결과."""

    common: CommonConfig
    sports: dict[str, LoadedSport] = field(default_factory=dict)
    sources: dict[str, SourceConfig] = field(default_factory=dict)
    leagues: dict[str, LeagueConfig] = field(default_factory=dict)


def config_dir_from_env() -> Path:
    return Path(os.environ.get("CONFIG_DIR", DEFAULT_CONFIG_DIR))


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ConfigError([f"{path}: 최상위는 매핑이어야 합니다"])
    return data


def _format_validation_error(path: Path, exc: ValidationError) -> list[str]:
    return [f"{path}: {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def resolve_stat(stat: StatDef, sport: SportConfig, order: int) -> ResolvedStat:
    """YAML 의 축약 지표 정의에 카테고리·data_type 기본값을 채운다."""
    prefix, _, tail = stat.code.rpartition(".")
    category_code = stat.category or prefix
    if not category_code:
        raise ConfigError([f"{sport.sport.code}: 지표 {stat.code} 에 category 가 필요합니다 (접두어 없음)"])
    category = next((c for c in sport.stat_categories if c.code == category_code), None)
    if category is None:
        raise ConfigError([f"{sport.sport.code}: 지표 {stat.code} 의 카테고리 '{category_code}' 가 정의되지 않았습니다"])

    depends_on: tuple[str, ...] = ()
    if stat.formula is not None:
        try:
            depends_on = tuple(sorted(parse(stat.formula).variables))
        except FormulaError as exc:
            raise ConfigError([f"{sport.sport.code}: 지표 {stat.code} 수식 오류: {exc}"]) from exc

    abbr = stat.abbr or tail
    return ResolvedStat(
        code=stat.code,
        category=category_code,
        name_ko=stat.name_ko,
        name_en=stat.name_en or abbr,
        abbr=abbr,
        scope=stat.scope or category.scope,
        levels=tuple(stat.levels or category.levels),
        unit=stat.unit,
        data_type=stat.data_type,
        aggregation=stat.aggregation or ("formula" if stat.formula else "sum"),
        decimals=stat.decimals if stat.decimals is not None else DEFAULT_DECIMALS[stat.data_type],
        higher_is_better=stat.higher_is_better,
        is_derived=stat.formula is not None,
        formula=stat.formula,
        depends_on=depends_on,
        is_hot=stat.hot,
        display_order=order,
        description=stat.description,
    )


def load_config(config_dir: Path | None = None) -> ConfigBundle:
    """config/ 를 읽어 ConfigBundle 을 만든다. 구조 오류는 한꺼번에 ConfigError 로 보고."""
    root = config_dir or config_dir_from_env()
    errors: list[str] = []

    common_path = root / "sports" / "_common.yaml"
    common = CommonConfig()
    if common_path.exists():
        try:
            common = CommonConfig.model_validate(_read_yaml(common_path))
        except ValidationError as exc:
            errors += _format_validation_error(common_path, exc)
    bundle = ConfigBundle(common=common)

    for path in sorted((root / "sports").glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            sport = SportConfig.model_validate(_read_yaml(path))
        except ValidationError as exc:
            errors += _format_validation_error(path, exc)
            continue
        except ConfigError as exc:
            errors += exc.errors
            continue
        if path.stem != sport.sport.code:
            errors.append(f"{path}: 파일명과 sport.code({sport.sport.code})가 다릅니다")
        try:
            stats = [resolve_stat(s, sport, (i + 1) * 10) for i, s in enumerate(sport.stats)]
        except ConfigError as exc:
            errors += exc.errors
            continue
        bundle.sports[sport.sport.code] = LoadedSport(config=sport, stats=stats, path=path)

    for kind, model, target in (
        ("sources", SourceConfig, bundle.sources),
        ("leagues", LeagueConfig, bundle.leagues),
    ):
        for path in sorted((root / kind).glob("*.yaml")):
            try:
                obj = model.model_validate(_read_yaml(path))
            except ValidationError as exc:
                errors += _format_validation_error(path, exc)
                continue
            except ConfigError as exc:
                errors += exc.errors
                continue
            key = obj.code if isinstance(obj, SourceConfig) else obj.league.code
            if key in target:
                errors.append(f"{path}: 코드 중복 {key}")
            target[key] = obj  # type: ignore[index]

    if errors:
        raise ConfigError(errors)
    return bundle
