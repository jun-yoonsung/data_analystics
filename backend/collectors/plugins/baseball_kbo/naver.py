"""네이버 스포츠 경기 API(api-gw.sports.naver.com) 응답 → 정규화 DTO. 순수 함수.

응답 구조는 이전 kbo-dashboard 프로젝트(jun-yoonsung/kbo-dashboard, kbo_scraper.py)가
실제로 수집·사용하던 필드만 사용한다. 그 밖의 필드는 확인되지 않았으므로 읽지 않는다.

  일정: GET /schedule/games?fields=basic,schedule,baseball&fromDate=..&toDate=..&categoryId=kbo
        result.games[]: gameId, roundCode, gameDateTime, homeTeamName, awayTeamName,
                        homeTeamScore, awayTeamScore, statusCode, statusInfo, stadium,
                        homeStarterName, awayStarterName, winPitcherName, losePitcherName, broadChannel
  기록: GET /schedule/games/{gameId}/record
        result.recordData.gameInfo: hName, aName
        result.recordData.battersBoxscore.{home,away}[]:  name, ab, hit, hr, rbi, run, sb, bb, kk, inn1..innN
        result.recordData.pitchersBoxscore.{home,away}[]: name, inn, er, hit, r, bb, kk, hr, pa, bf
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from collectors.core import dto as d
from collectors.plugins.baseball_kbo.common import naver_innings_to_outs, team_ref, to_int

KST = ZoneInfo("Asia/Seoul")

# roundCode → 스테이지. 대시보드에서 확인된 코드만 매핑한다.
#  kbo_as(올스타전)는 기록 집계 대상이 아니라 제외한다. 포스트시즌 코드는 아직 확인되지 않아,
#  처음 보는 코드는 경기를 버리고 경고로 남긴다 (코드 확인 후 여기에 추가).
ROUND_STAGES: dict[str, d.StageDTO] = {
    "kbo_r": d.StageDTO(season_label="", code="REG", name_ko="정규시즌", stage_type="regular", sort_order=1),
    "kbo_e": d.StageDTO(season_label="", code="PRE", name_ko="시범경기", stage_type="preseason", sort_order=0),
}
IGNORED_ROUNDS = frozenset({"kbo_as"})

# statusCode → 경기 상태 (대시보드 STATUS_LABEL 에서 확인된 값)
STATUS: dict[str, d.MatchStatus] = {
    "BEFORE": "scheduled",
    "LIVE": "live",
    "RESULT": "final",
    "CANCEL": "cancelled",
}

# 타자 박스스코어 필드 → 원시 지표
BATTER_FIELDS = {"ab": "bat.AB", "hit": "bat.H", "hr": "bat.HR", "rbi": "bat.RBI", "run": "bat.R",
                 "sb": "bat.SB", "bb": "bat.BB", "kk": "bat.SO"}
# 투수 박스스코어 필드 → 원시 지표.
#  이 객체에는 이번 경기 기록과 시즌 누적(gameCount, w, l, era 등)이 섞여 있어 경기 기록만 고른다.
#  'bf' 는 이름과 달리 이번 경기 투구 수, 'pa' 가 상대 타자 수다 (대시보드에서 ab+사구=pa 로 검증된 내용).
PITCHER_FIELDS = {"er": "pit.ER", "hit": "pit.H", "r": "pit.R", "bb": "pit.BB", "kk": "pit.SO",
                  "hr": "pit.HR", "pa": "pit.TBF", "bf": "pit.NP"}

_INNING_KEY = re.compile(r"^inn\d+$")


# ---------------------------------------------------------------------------
# 일정·결과
# ---------------------------------------------------------------------------
def parse_schedule(data: dict) -> d.NormalizedBundle:
    warnings: list[str] = []
    teams: dict[str, d.TeamDTO] = {}
    venues: dict[str, d.VenueDTO] = {}
    stages: dict[tuple[str, str], d.StageDTO] = {}
    seasons: dict[str, d.SeasonDTO] = {}
    rows: list[dict] = []

    for g in (data.get("result") or {}).get("games") or []:
        game_id = g.get("gameId")
        round_code = g.get("roundCode")
        if not game_id:
            warnings.append("gameId 없는 경기 항목 (무시)")
            continue
        if round_code in IGNORED_ROUNDS:
            continue
        stage_tpl = ROUND_STAGES.get(round_code)
        if stage_tpl is None:
            warnings.append(f"경기 {game_id}: 알 수 없는 roundCode '{round_code}' (무시, naver.ROUND_STAGES 확인 필요)")
            continue
        status = STATUS.get(g.get("statusCode"))
        if status is None:
            warnings.append(f"경기 {game_id}: 알 수 없는 statusCode '{g.get('statusCode')}' (무시)")
            continue
        scheduled_at = _parse_datetime(g.get("gameDateTime"))
        if scheduled_at is None:
            warnings.append(f"경기 {game_id}: gameDateTime '{g.get('gameDateTime')}' 해석 불가 (무시)")
            continue
        home_name, away_name = g.get("homeTeamName"), g.get("awayTeamName")
        if not home_name or not away_name:
            warnings.append(f"경기 {game_id}: 팀 이름 없음 (무시)")
            continue

        season_label = str(scheduled_at.year)
        seasons.setdefault(season_label, d.SeasonDTO(label=season_label, start_year=scheduled_at.year))
        stage = stage_tpl.model_copy(update={"season_label": season_label})
        stages.setdefault((season_label, stage.code), stage)
        home_id, home = team_ref(home_name, warnings)
        away_id, away = team_ref(away_name, warnings)
        teams.setdefault(home_id, home)
        teams.setdefault(away_id, away)
        stadium = (g.get("stadium") or "").strip() or None
        if stadium:
            venues.setdefault(stadium, d.VenueDTO(external_id=stadium, name_ko=stadium))

        final_or_live = status in ("final", "live")
        attrs = {k: v for k, v in {
            "round_code": round_code,
            "status_info": g.get("statusInfo"),
            "home_starter": g.get("homeStarterName"),
            "away_starter": g.get("awayStarterName"),
            "win_pitcher": g.get("winPitcherName"),
            "lose_pitcher": g.get("losePitcherName"),
            "broadcast": g.get("broadChannel"),
        }.items() if v not in (None, "")}
        rows.append({
            "external_id": str(game_id), "season_label": season_label, "stage_code": stage.code,
            "home_team_external_id": home_id, "away_team_external_id": away_id,
            "venue_external_id": stadium, "scheduled_at": scheduled_at, "status": status,
            "home_score": to_int(g.get("homeTeamScore")) if final_or_live else None,
            "away_score": to_int(g.get("awayTeamScore")) if final_or_live else None,
            "attrs": attrs,
        })

    # 더블헤더: 같은 날 같은 대진은 시작 시각 순으로 1, 2차전
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["scheduled_at"].date(), r["home_team_external_id"], r["away_team_external_id"])].append(r)
    for group in groups.values():
        for n, r in enumerate(sorted(group, key=lambda x: (x["scheduled_at"], x["external_id"])), start=1):
            r["game_number"] = n

    return d.NormalizedBundle(
        seasons=list(seasons.values()),
        stages=list(stages.values()),
        venues=list(venues.values()),
        teams=list(teams.values()),
        matches=[d.MatchDTO(**r) for r in rows],
        warnings=warnings,
    )


def _parse_datetime(value) -> datetime | None:
    """'2026-09-27T14:00:00' (타임존 없음, 한국 시각) → aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# 박스스코어
# ---------------------------------------------------------------------------
def parse_record(data: dict, game_id: str) -> d.NormalizedBundle:
    warnings: list[str] = []
    rd = (data.get("result") or {}).get("recordData") or {}
    info = rd.get("gameInfo") or {}
    side_names = {"home": info.get("hName"), "away": info.get("aName")}
    if not side_names["home"] or not side_names["away"]:
        raise ValueError(f"경기 {game_id}: recordData.gameInfo 에 팀 이름(hName/aName)이 없습니다")

    teams: dict[str, d.TeamDTO] = {}
    side_team: dict[str, str] = {}
    for side, name in side_names.items():
        ext, team = team_ref(name, warnings)
        teams[ext] = team
        side_team[side] = ext

    players: dict[str, d.PlayerDTO] = {}
    # (선수 외부 ID) → 지표. 투타 겸업 선수는 한 행에 bat.*, pit.* 가 함께 들어간다.
    stats: dict[str, dict] = {}
    player_team: dict[str, str] = {}

    def player_id(side: str, name: str) -> str:
        # 이전 대시보드는 박스스코어의 선수 코드 필드를 쓰지 않아 필드가 확인되지 않았다.
        # 확인 전까지는 '구단코드:이름' 을 외부 ID 로 쓴다 (한계: 같은 팀 동명이인, 시즌 중 이적).
        ext = f"{side_team[side]}:{name}"
        players.setdefault(ext, d.PlayerDTO(external_id=ext, name_ko=name))
        player_team[ext] = side_team[side]
        return ext

    for side in ("away", "home"):
        for row in (rd.get("battersBoxscore") or {}).get(side) or []:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            values = _batter_stats(row)
            stats.setdefault(player_id(side, name), {}).update(values)

        for row in (rd.get("pitchersBoxscore") or {}).get(side) or []:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            ext = player_id(side, name)
            values = _pitcher_stats(row, warnings, f"경기 {game_id} 투수 {name}")
            stats.setdefault(ext, {}).update(values)

    return d.NormalizedBundle(
        teams=list(teams.values()),
        players=list(players.values()),
        player_match_stats=[
            d.PlayerMatchStatDTO(match_external_id=game_id, player_external_id=ext,
                                 team_external_id=player_team[ext], stats=values)
            for ext, values in stats.items() if values
        ],
        warnings=warnings,
    )


def _batter_stats(row: dict) -> dict:
    values: dict[str, int] = {"bat.G": 1}
    for key, code in BATTER_FIELDS.items():
        v = to_int(row.get(key))
        if v is not None:
            values[code] = v
    values.update(inning_result_counts(row))
    # 박스스코어에 타석 수가 없어 구성 요소로 계산한다 (타격방해 출루는 식별할 수 없어 빠진다)
    if "bat.AB" in values:
        values["bat.PA"] = sum(values.get(k, 0) for k in ("bat.AB", "bat.BB", "bat.HBP", "bat.SF", "bat.SH"))
    return values


def inning_result_counts(row: dict) -> dict[str, int]:
    """타자 박스스코어의 이닝별 결과 텍스트(inn1, inn2, ...)에서 2루타·3루타·사구·희생타를 센다.

    표기 규칙(대시보드에서 여러 경기의 전체 코드 집합으로 확인): '좌2' = 좌익수 쪽 2루타,
    '중3' = 3루타, '사구' = 몸에 맞는 공, '…희비' = 희생플라이, '…희번' = 희생번트.
    """
    counts = {"bat.DBL": 0, "bat.TPL": 0, "bat.HBP": 0, "bat.SF": 0, "bat.SH": 0}
    for key, val in row.items():
        if not (_INNING_KEY.match(key) and val):
            continue
        # 한 이닝에 두 타석이 돌면 값이 여러 개일 수 있어 구분자로 나눈다
        for text in re.split(r"[/,\s]+", str(val).strip()):
            if not text:
                continue
            if text == "사구":
                counts["bat.HBP"] += 1
            elif text.endswith("희비"):
                counts["bat.SF"] += 1
            elif text.endswith("희번"):
                counts["bat.SH"] += 1
            elif text[-1] == "2":
                counts["bat.DBL"] += 1
            elif text[-1] == "3":
                counts["bat.TPL"] += 1
    return counts


def _pitcher_stats(row: dict, warnings: list[str], where: str) -> dict:
    values: dict[str, int] = {"pit.G": 1}
    for key, code in PITCHER_FIELDS.items():
        v = to_int(row.get(key))
        if v is not None:
            values[code] = v
    outs = naver_innings_to_outs(row.get("inn"))
    if outs is None:
        warnings.append(f"{where}: 이닝 표기 '{row.get('inn')}' 해석 불가")
    else:
        values["pit.OUTS"] = outs
    return values
