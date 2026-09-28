"""설정 YAML 스키마 (pydantic).

YAML 은 사람이 쓰기 편하도록 기본값을 많이 두고, loader 가 기본값을 채운
Resolved* 객체로 변환한다. DB 에 들어가는 값은 항상 Resolved* 기준이다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["period", "match", "season"]
Scope = Literal["player", "team", "both"]
DataType = Literal["int", "decimal", "ratio", "percent", "innings", "duration", "text"]
Aggregation = Literal["sum", "avg", "max", "min", "last", "formula", "none"]

# data_type 별 기본 소수점 자리
DEFAULT_DECIMALS: dict[str, int] = {
    "int": 0, "decimal": 2, "ratio": 3, "percent": 1, "innings": 1, "duration": 0, "text": 0,
}


class _Strict(BaseModel):
    # 오타로 잘못된 키를 쓰면 조용히 무시되지 않도록 금지
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# 종목 설정 (config/sports/<sport>.yaml)
# ---------------------------------------------------------------------------
class SportMeta(_Strict):
    code: str
    name_ko: str
    name_en: str
    period_label_ko: str
    score_unit_ko: str
    clock_type: Literal["none", "countdown", "countup"]
    settings: dict = Field(default_factory=dict)


class PeriodDef(_Strict):
    code: str
    name_ko: str
    label_pattern: str
    regulation_count: int = Field(ge=0)
    max_count: int | None = None
    is_overtime: bool = False
    nominal_duration_sec: int | None = None


class PositionDef(_Strict):
    code: str
    name_ko: str
    name_en: str
    group: str


class EventTypeDef(_Strict):
    code: str
    name_ko: str
    participant_roles: list[str] = Field(default_factory=list)
    attr_schema: dict = Field(default_factory=dict)


class StatCategoryDef(_Strict):
    code: str
    name_ko: str
    name_en: str
    # 이 카테고리 지표들의 기본값
    scope: Scope = "both"
    levels: list[Level] = Field(default_factory=lambda: ["match", "season"])


class StatDef(_Strict):
    code: str
    name_ko: str
    name_en: str | None = None          # 기본: abbr
    abbr: str | None = None             # 기본: 코드의 마지막 세그먼트
    category: str | None = None         # 기본: 코드 접두어
    scope: Scope | None = None          # 기본: 카테고리 기본값
    levels: list[Level] | None = None   # 기본: 카테고리 기본값
    unit: str | None = None
    data_type: DataType = "int"
    aggregation: Aggregation | None = None  # 기본: 파생이면 formula, 아니면 sum
    decimals: int | None = None         # 기본: data_type 별
    higher_is_better: bool | None = True
    formula: str | None = None
    hot: bool = False
    description: str | None = None


class LeagueConstantDef(_Strict):
    code: str
    name_ko: str
    formula: str | None = None          # 없으면 수동 입력/수집 값
    decimals: int = 4
    description: str | None = None


class SplitDef(_Strict):
    code: str
    name_ko: str
    source: Literal["match", "period", "event", "player"]
    key: str
    value_labels: dict = Field(default_factory=dict)
    description: str | None = None


class QualificationRuleDef(_Strict):
    code: str
    category: str
    name_ko: str
    rule: str
    provisional: bool = False           # 공식 기준 미확인
    description: str | None = None


class SportConfig(_Strict):
    sport: SportMeta
    periods: list[PeriodDef]
    positions: list[PositionDef]
    event_types: list[EventTypeDef] = Field(default_factory=list)
    stat_categories: list[StatCategoryDef]
    stats: list[StatDef]
    league_constants: list[LeagueConstantDef] = Field(default_factory=list)
    splits: list[SplitDef] = Field(default_factory=list)
    qualification_rules: list[QualificationRuleDef] = Field(default_factory=list)


class CommonConfig(_Strict):
    """모든 종목 공통 설정 (config/sports/_common.yaml)."""

    splits: list[SplitDef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 데이터 소스 (config/sources/<code>.yaml)
# ---------------------------------------------------------------------------
class SourceConfig(_Strict):
    code: str
    name: str
    base_url: str
    terms_url: str | None = None
    robots_url: str | None = None
    terms_note: str | None = None
    # 약관·robots.txt 확인 전에는 false. 수집기는 false 인 소스에 요청을 보내지 않는다.
    collection_allowed: bool = False
    min_interval_ms: int = Field(default=3000, ge=500)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent: str | None = None


# ---------------------------------------------------------------------------
# 리그 (config/leagues/<code>.yaml)
# ---------------------------------------------------------------------------
class LeagueMeta(_Strict):
    code: str
    sport: str
    name_ko: str
    name_en: str
    country: str = Field(min_length=2, max_length=2)
    gender: Literal["men", "women", "mixed"]
    tier: int = 1
    timezone: str
    collector_key: str | None = None
    settings: dict = Field(default_factory=dict)


class ScheduleDef(_Strict):
    job_type: Literal["schedule", "results", "boxscore", "events", "season_stats", "standings",
                      "roster", "players"]
    source: str
    cron: str
    offseason_policy: Literal["skip", "run", "weekly"] = "skip"
    enabled: bool = True
    params: dict = Field(default_factory=dict)


class LeagueConfig(_Strict):
    league: LeagueMeta
    schedules: list[ScheduleDef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 기본값이 채워진 지표 정의
# ---------------------------------------------------------------------------
class ResolvedStat(_Strict):
    code: str
    category: str
    name_ko: str
    name_en: str
    abbr: str
    scope: Scope
    levels: tuple[Level, ...]
    unit: str | None
    data_type: DataType
    aggregation: Aggregation
    decimals: int
    higher_is_better: bool | None
    is_derived: bool
    formula: str | None
    depends_on: tuple[str, ...]
    is_hot: bool
    display_order: int
    description: str | None
