"""KBO 시즌 기록·순위·선수 프로필 → 정규화 DTO. 순수 함수.

소스: GitHub 공개 저장소 PsyproLEE/KBO_statics 의 web/public/data/ 파일.
  그 저장소가 매일(00:30 KST 예약) KBO 공식 기록 페이지를 수집해 JSON 으로 올린다.
    현재 시즌: meta.json({updatedAt, season}), hitters.json, pitchers.json, standings.json, players.json
    지난 시즌: season/{연도}.json  ({season, standings, hitters, pitchers}, 1982년부터)

기록 행은 KBO 기록 페이지 표를 그대로 옮긴 것이다 (실제 파일로 확인, tests/fixtures/kbo/).
  타자: {순위, 선수명, 팀명, playerId, detailUrl, AVG, G, PA, AB, R, H, 2B, 3B, HR, TB, RBI, SAC, SF,
         BB, IBB, HBP, SO, GDP, SLG, OBP, OPS, MH, RISP, PH-BA}
  투수: {순위, 선수명, 팀명, playerId, detailUrl, ERA, G, W, L, SV, HLD, WPCT, IP('224 2/3'), H, HR, BB, HBP,
         SO, R, ER, WHIP, CG, SHO, QS, BSV, TBF, NP, AVG, 2B, 3B, SAC, SF, IBB, WP, BK}
  순위: {순위, 팀명, 경기, 승, 패, 무, 승률, 게임차, 최근10경기, 연속, 홈, 방문}
  선수: {playerId: {name, backNo, birth('1993년 02월 12일'), position('외야수(우투좌타)'),
                    body('189cm/75kg'), career, debut, photo, roles}}
"""
from __future__ import annotations

import re
from datetime import date

from collectors.core import dto as d
from collectors.plugins.baseball_kbo.common import kbo_innings_to_outs, team_ref, to_float, to_int

# 기록표 머리글 → 원시 지표. 파생 지표(AVG, OPS, ERA, WHIP ...)는 저장하지 않고 공통 계산기로 다시 계산한다.
HITTER_HEADERS = {
    "G": "bat.G", "PA": "bat.PA", "AB": "bat.AB", "R": "bat.R", "H": "bat.H", "2B": "bat.DBL",
    "3B": "bat.TPL", "HR": "bat.HR", "RBI": "bat.RBI", "SAC": "bat.SH", "SF": "bat.SF", "BB": "bat.BB",
    "IBB": "bat.IBB", "HBP": "bat.HBP", "SO": "bat.SO", "GDP": "bat.GDP",
}
PITCHER_HEADERS = {
    "G": "pit.G", "W": "pit.W", "L": "pit.L", "SV": "pit.SV", "HLD": "pit.HLD", "H": "pit.H",
    "HR": "pit.HR", "BB": "pit.BB", "HBP": "pit.HBP", "SO": "pit.SO", "R": "pit.R", "ER": "pit.ER",
    "CG": "pit.CG", "SHO": "pit.SHO", "QS": "pit.QS", "BSV": "pit.BSV", "TBF": "pit.TBF", "NP": "pit.NP",
    "IBB": "pit.IBB", "WP": "pit.WP", "BK": "pit.BK",
    # IP 는 아웃카운트(pit.OUTS)로 변환
}
# 알고 있지만 저장하지 않는 항목
_SKIPPED = frozenset({"순위", "선수명", "팀명", "playerId", "detailUrl", "AVG", "TB", "SLG", "OBP", "OPS", "MH",
                      "RISP", "PH-BA", "ERA", "WPCT", "WHIP", "IP", "2B", "3B", "SAC", "SF"})
STANDING_KEYS = {"경기": "std.G", "승": "std.W", "패": "std.L", "무": "std.D"}

_BIRTH = re.compile(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})")
_BODY = re.compile(r"(\d+(?:\.\d+)?)\s*cm\s*/\s*(\d+(?:\.\d+)?)\s*kg")
_POSITION = re.compile(r"^([^()]+)(?:\(([^)]*)\))?$")


def _season_parts(label: str) -> tuple[list[d.SeasonDTO], list[d.StageDTO]]:
    return ([d.SeasonDTO(label=label, start_year=int(label))],
            [d.StageDTO(season_label=label, code="REG", name_ko="정규시즌", stage_type="regular", sort_order=1)])


# ---------------------------------------------------------------------------
# 선수·팀 시즌 기록
# ---------------------------------------------------------------------------
def parse_season_stats(season_label: str, hitters: list[dict], pitchers: list[dict],
                       profiles: dict | None = None) -> d.NormalizedBundle:
    """정규시즌 선수 기록 + 팀 기록(소속 선수 합산) + 선수 프로필.

    - 타자·투수 명단에 모두 있는 선수는 한 행(bat.* + pit.*)으로 합친다 (시즌 기록 행은 선수·팀당 하나).
    - 한 시즌에 모든 선수가 0 인 지표는 그 시즌에 집계하지 않은 항목으로 보고 뺀다
      (예: 1982년 투구 수·QS·홀드는 KBO 기록실에 0 으로 표시된다).
    - 팀 기록은 소속 선수 기록의 합이다. KBO 기록실은 이적 선수의 시즌 기록을 현재 소속팀에 모두 표시하므로
      팀별 값은 이적 선수만큼 차이가 날 수 있다. 리그 전체 합계(리그 상수 계산용)는 정확하다.
    """
    warnings: list[str] = []
    teams: dict[str, d.TeamDTO] = {}
    players: dict[str, d.PlayerDTO] = {}
    stats: dict[tuple[str, str], dict[str, int]] = {}

    for kind, rows, header_map in (("타자", hitters, HITTER_HEADERS), ("투수", pitchers, PITCHER_HEADERS)):
        unknown = sorted({k for r in rows for k in r} - set(header_map) - _SKIPPED)
        if unknown:
            warnings.append(f"{kind} 기록의 처리하지 않는 항목: {', '.join(unknown)}")
        for r in rows:
            pid = str(r.get("playerId") or "").strip()
            name = (r.get("선수명") or "").strip()
            team_name = (r.get("팀명") or "").strip()
            if not pid or not name or not team_name:
                warnings.append(f"{kind} 행에 선수 ID·이름·팀이 없음 (무시): {name or pid}")
                continue
            team_id, team = team_ref(team_name, warnings)
            teams.setdefault(team_id, team)
            players.setdefault(pid, _player(pid, name, (profiles or {}).get(pid), warnings))
            values = stats.setdefault((pid, team_id), {})
            for header, code in header_map.items():
                v = to_int(r.get(header))
                if v is not None:
                    values[code] = v
            if kind == "투수":
                ip = r.get("IP")
                outs = kbo_innings_to_outs(ip)
                if outs is not None:
                    values["pit.OUTS"] = outs
                elif ip not in (None, "", "-"):
                    warnings.append(f"투수 {name}({pid}) 이닝 표기 '{ip}' 해석 불가")

    # 시즌 전체가 0 인 지표 = 미집계 항목
    all_codes = {c for v in stats.values() for c in v}
    unrecorded = sorted(c for c in all_codes if c != "pit.OUTS" and all(v.get(c, 0) == 0 for v in stats.values()))
    if unrecorded:
        warnings.append(f"{season_label} 시즌 전체가 0 인 지표는 미집계로 보고 제외: {', '.join(unrecorded)}")
        for v in stats.values():
            for c in unrecorded:
                v.pop(c, None)

    team_totals: dict[str, dict[str, int]] = {}
    for (_, team_id), values in stats.items():
        tot = team_totals.setdefault(team_id, {})
        for c, v in values.items():
            # 경기 수는 합산하면 안 되는 값이라 팀 기록에서는 뺀다
            if c not in ("bat.G", "pit.G"):
                tot[c] = tot.get(c, 0) + v

    seasons, stages = _season_parts(season_label)
    return d.NormalizedBundle(
        seasons=seasons, stages=stages, teams=list(teams.values()), players=list(players.values()),
        player_season_stats=[
            d.PlayerSeasonStatDTO(season_label=season_label, stage_code="REG", player_external_id=pid,
                                  team_external_id=team_id, stats=values)
            for (pid, team_id), values in stats.items() if values],
        team_season_stats=[
            d.TeamSeasonStatDTO(season_label=season_label, stage_code="REG", team_external_id=team_id, stats=tot)
            for team_id, tot in team_totals.items() if tot],
        warnings=warnings)


def _player(pid: str, name: str, profile: dict | None, warnings: list[str]) -> d.PlayerDTO:
    if not profile:
        return d.PlayerDTO(external_id=pid, name_ko=name)
    birth = None
    m = _BIRTH.search(profile.get("birth") or "")
    if m:
        try:
            birth = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            warnings.append(f"선수 {name}({pid}) 생년월일 '{profile.get('birth')}' 해석 불가")
    height = weight = None
    m = _BODY.search(profile.get("body") or "")
    if m:
        height, weight = float(m.group(1)), float(m.group(2))
    attrs: dict = {}
    m = _POSITION.match((profile.get("position") or "").strip())
    if m:
        attrs["position"] = m.group(1).strip()
        if m.group(2):
            attrs["throws_bats"] = m.group(2).strip()      # 예: 우투좌타
    for src, dst in (("backNo", "back_no"), ("career", "career"), ("debut", "debut")):
        if profile.get(src):
            attrs[dst] = str(profile[src]).strip()
    return d.PlayerDTO(external_id=pid, name_ko=(profile.get("name") or name).strip(), birth_date=birth,
                       height_cm=height, weight_kg=weight, attrs=attrs)


# ---------------------------------------------------------------------------
# 순위
# ---------------------------------------------------------------------------
def parse_standings(season_label: str, rows: list[dict], as_of: date) -> d.NormalizedBundle:
    warnings: list[str] = []
    teams: dict[str, d.TeamDTO] = {}
    standings: list[d.StandingDTO] = []
    for r in rows:
        rank = to_int(r.get("순위"))
        team_name = (r.get("팀명") or "").strip()
        if rank is None or rank < 1 or not team_name:
            warnings.append(f"순위 행 해석 불가 (무시): {r}")
            continue
        team_id, team = team_ref(team_name, warnings)
        teams.setdefault(team_id, team)
        values: dict[str, int | float] = {}
        for key, code in STANDING_KEYS.items():
            v = to_int(r.get(key))
            if v is not None:
                values[code] = v
        gb = r.get("게임차")
        gb_value = 0.0 if str(gb).strip() == "-" else to_float(gb)
        if gb_value is not None:
            values["std.GB"] = gb_value
        standings.append(d.StandingDTO(season_label=season_label, stage_code="REG", team_external_id=team_id,
                                       as_of_date=as_of, rank=rank, stats=values))
    if not standings:
        raise ValueError("순위표에 팀 행이 없습니다 — 파일 구조 변경 확인 필요")
    seasons, stages = _season_parts(season_label)
    return d.NormalizedBundle(seasons=seasons, stages=stages, teams=list(teams.values()), standings=standings,
                              warnings=warnings)


def parse_updated_date(meta: dict) -> date | None:
    """meta.json updatedAt('2026-09-30 05:20', 한국 시각) → 날짜."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(meta.get("updatedAt") or ""))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
