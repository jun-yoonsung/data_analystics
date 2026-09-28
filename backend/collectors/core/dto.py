"""정규화 DTO: 수집 플러그인이 소스 고유 구조를 변환해 내놓는 공통 형식.

- 엔티티 참조는 모두 **소스의 외부 ID** 로 한다. 내부 ID 변환은 공통 코드(resolver)가 담당한다.
- 기록 값(stats)은 config.stat_definition 의 원시 지표 코드를 키로 쓴다. 파생 지표는 넣지 않는다.
- 필드가 None 이면 "소스가 제공하지 않음" 이므로 기존 값을 덮어쓰지 않는다 (부분 갱신).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

StatValue = int | float | str
MatchStatus = Literal["scheduled", "live", "final", "postponed", "cancelled", "suspended", "forfeit"]


class _DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PeriodRef(_DTO):
    """경기 구간 참조: 구간 코드 + 순번 (예: INN 7, Q 4, OT 1)."""

    code: str
    seq: int = Field(ge=1)


class SeasonDTO(_DTO):
    label: str
    start_year: int
    start_date: date | None = None
    end_date: date | None = None
    status: Literal["upcoming", "active", "completed"] | None = None


class StageDTO(_DTO):
    season_label: str
    code: str
    name_ko: str
    stage_type: Literal["preseason", "regular", "postseason", "group", "knockout", "cup", "relegation"]
    start_date: date | None = None
    end_date: date | None = None
    sort_order: int = 0


class VenueDTO(_DTO):
    external_id: str
    name_ko: str
    name_en: str | None = None
    city: str | None = None
    capacity: int | None = None
    attrs: dict = Field(default_factory=dict)


class TeamDTO(_DTO):
    external_id: str
    name_ko: str
    name_en: str | None = None
    short_name_ko: str | None = None
    code: str | None = None
    city: str | None = None
    attrs: dict = Field(default_factory=dict)


class PlayerDTO(_DTO):
    external_id: str
    name_ko: str
    name_en: str | None = None
    birth_date: date | None = None
    nationality: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    attrs: dict = Field(default_factory=dict)


class PeriodScoreDTO(_DTO):
    period: PeriodRef
    home_score: int | None = None
    away_score: int | None = None
    duration_sec: int | None = None
    stats: dict[str, StatValue] = Field(default_factory=dict)


class MatchDTO(_DTO):
    external_id: str
    season_label: str
    stage_code: str
    home_team_external_id: str
    away_team_external_id: str
    venue_external_id: str | None = None
    scheduled_at: datetime
    game_number: int = 1
    round_label: str | None = None
    status: MatchStatus
    home_score: int | None = None
    away_score: int | None = None
    winner: Literal["home", "away", "draw"] | None = None
    result_type: str | None = None
    attendance: int | None = None
    duration_sec: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attrs: dict = Field(default_factory=dict)
    # None = 이 문서에 구간 정보 없음 (기존 구간 유지)
    periods: list[PeriodScoreDTO] | None = None

    @field_validator("scheduled_at", "started_at", "ended_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("시각은 타임존 정보가 있어야 합니다")
        return v


class LineupDTO(_DTO):
    match_external_id: str
    team_external_id: str
    player_external_id: str
    is_starter: bool
    order_no: int | None = None
    position_code: str | None = None
    shirt_no: str | None = None
    attrs: dict = Field(default_factory=dict)


class PlayerMatchStatDTO(_DTO):
    match_external_id: str
    player_external_id: str
    team_external_id: str
    period: PeriodRef | None = None       # None = 경기 전체
    position_code: str | None = None
    is_starter: bool | None = None
    stats: dict[str, StatValue]


class TeamMatchStatDTO(_DTO):
    match_external_id: str
    team_external_id: str
    period: PeriodRef | None = None
    stats: dict[str, StatValue]


class PlayerSeasonStatDTO(_DTO):
    season_label: str
    stage_code: str
    player_external_id: str
    team_external_id: str | None = None   # None = 시즌 합산
    stats: dict[str, StatValue]


class TeamSeasonStatDTO(_DTO):
    season_label: str
    stage_code: str
    team_external_id: str
    stats: dict[str, StatValue]


class StandingDTO(_DTO):
    season_label: str
    stage_code: str
    team_external_id: str
    as_of_date: date
    rank: int = Field(ge=1)
    group_code: str = ""
    stats: dict[str, StatValue] = Field(default_factory=dict)


class RosterEntryDTO(_DTO):
    player_external_id: str
    team_external_id: str
    season_label: str
    valid_from: date
    valid_to: date | None = None
    status: Literal["active", "reserve", "injured", "loan", "military", "released"]
    jersey_no: str | None = None
    position_code: str | None = None


class EventParticipantDTO(_DTO):
    player_external_id: str
    role: str
    ord: int = 1


class EventDTO(_DTO):
    match_external_id: str
    seq: int = Field(ge=1)
    event_type: str
    period: PeriodRef | None = None
    team_external_id: str | None = None
    clock_sec: int | None = None
    elapsed_sec: int | None = None
    score_home_after: int | None = None
    score_away_after: int | None = None
    description: str | None = None
    attrs: dict = Field(default_factory=dict)
    participants: list[EventParticipantDTO] = Field(default_factory=list)


class NormalizedBundle(_DTO):
    """문서 하나를 정규화한 결과. 순서대로 저장된다 (시즌 → ... → 이벤트)."""

    seasons: list[SeasonDTO] = Field(default_factory=list)
    stages: list[StageDTO] = Field(default_factory=list)
    venues: list[VenueDTO] = Field(default_factory=list)
    teams: list[TeamDTO] = Field(default_factory=list)
    players: list[PlayerDTO] = Field(default_factory=list)
    matches: list[MatchDTO] = Field(default_factory=list)
    lineups: list[LineupDTO] = Field(default_factory=list)
    player_match_stats: list[PlayerMatchStatDTO] = Field(default_factory=list)
    team_match_stats: list[TeamMatchStatDTO] = Field(default_factory=list)
    player_season_stats: list[PlayerSeasonStatDTO] = Field(default_factory=list)
    team_season_stats: list[TeamSeasonStatDTO] = Field(default_factory=list)
    standings: list[StandingDTO] = Field(default_factory=list)
    rosters: list[RosterEntryDTO] = Field(default_factory=list)
    events: list[EventDTO] = Field(default_factory=list)
    # 파싱 중 발견한 문제 (예: 알 수 없는 표기). ingest_run.warnings 로 모인다
    warnings: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in type(self).model_fields if f != "warnings")
