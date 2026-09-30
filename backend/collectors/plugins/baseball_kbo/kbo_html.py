"""KBO 공식 홈페이지(www.koreabaseball.com) 기록 페이지 HTML → 정규화 DTO. 순수 함수.

페이지 구조는 이전 kbo-dashboard 프로젝트(kbo_scraper.py)가 실제로 파싱하던 방식만 따른다.

  순위: /record/teamrank/teamrank.aspx
        table.tData tbody tr 의 td 순서 = 순위, 팀명, 경기, 승, 패, 무, 승률, 게임차, 최근10경기, 연속, 홈, 방문
  팀 기록: /Record/Team/Hitter/Basic1.aspx, Basic2.aspx, /Record/Team/Pitcher/Basic1.aspx, Basic2.aspx
        table.tData, thead th = 순위, 팀명, 지표 약어(G, PA, AB, 2B, IP ...), tbody tr 은 팀별 한 행

현재 시즌 페이지는 GET 만으로 받는다. 연도 선택·페이지 이동·팀 필터는 ASP.NET 포스트백(POST)이라
공통 HTTP 클라이언트(GET 전용)로는 지원하지 않는다. 선수 개인 시즌 기록 페이지는 첫 페이지(상위 일부)만
GET 으로 나와 전체 선수를 얻을 수 없으므로 수집하지 않는다 → 선수 시즌 기록은 박스스코어 집계로 만든다.
"""
from __future__ import annotations

from datetime import date

from bs4 import BeautifulSoup

from collectors.core import dto as d
from collectors.plugins.baseball_kbo.common import kbo_innings_to_outs, team_ref, to_float, to_int

STANDINGS_COLUMNS = ["rank", "team", "games", "wins", "losses", "draws",
                     "win_pct", "games_behind", "last10", "streak", "home", "away"]

# 팀 기록표 머리글 약어 → 원시 지표. 파생 지표(AVG, OBP, ERA, WHIP ...)는 저장하지 않고 재계산한다.
TEAM_BATTING_HEADERS = {
    "G": "bat.G", "PA": "bat.PA", "AB": "bat.AB", "R": "bat.R", "H": "bat.H", "2B": "bat.DBL",
    "3B": "bat.TPL", "HR": "bat.HR", "RBI": "bat.RBI", "SAC": "bat.SH", "SF": "bat.SF", "BB": "bat.BB",
    "IBB": "bat.IBB", "HBP": "bat.HBP", "SO": "bat.SO", "GDP": "bat.GDP", "SB": "bat.SB", "CS": "bat.CS",
}
TEAM_PITCHING_HEADERS = {
    "G": "pit.G", "W": "pit.W", "L": "pit.L", "SV": "pit.SV", "HLD": "pit.HLD", "H": "pit.H",
    "HR": "pit.HR", "BB": "pit.BB", "HBP": "pit.HBP", "SO": "pit.SO", "R": "pit.R", "ER": "pit.ER",
    "CG": "pit.CG", "SHO": "pit.SHO", "QS": "pit.QS", "BSV": "pit.BSV", "TBF": "pit.TBF", "NP": "pit.NP",
    "IBB": "pit.IBB", "WP": "pit.WP", "BK": "pit.BK",
    # IP 는 아웃카운트로 변환한다 (아래 _team_row)
}
# 알고 있지만 저장하지 않는 머리글 (파생 지표이거나 원시 지표 정의가 없는 항목)
KNOWN_SKIPPED = frozenset({
    "순위", "팀명", "AVG", "TB", "SLG", "OBP", "OPS", "MH", "RISP", "PH-BA", "ERA", "WPCT", "WHIP",
    "2B", "3B", "SAC", "SF",  # 투구 기록표의 피2루타·피3루타·희생타 (원시 지표 정의 없음)
})


def _season_from_page(soup: BeautifulSoup, fallback: str, warnings: list[str]) -> str:
    """페이지의 시즌(연도) 선택 상자에서 현재 표시 중인 시즌을 읽는다. 없으면 요청 시 시즌."""
    for sel in soup.select("select"):
        name = sel.get("name") or ""
        if name.endswith("ddlSeason$ddlSeason") or name.endswith("ddlYear"):
            opt = sel.select_one("option[selected]")
            value = (opt.get("value") or opt.get_text(strip=True)) if opt else ""
            if value.isdigit() and len(value) == 4:
                if value != fallback:
                    warnings.append(f"페이지 시즌 {value} ≠ 요청 시즌 {fallback} — 페이지 값을 사용")
                return value
    return fallback


def _season_bundle_parts(season_label: str) -> tuple[list[d.SeasonDTO], list[d.StageDTO]]:
    return ([d.SeasonDTO(label=season_label, start_year=int(season_label))],
            [d.StageDTO(season_label=season_label, code="REG", name_ko="정규시즌", stage_type="regular",
                        sort_order=1)])


# ---------------------------------------------------------------------------
# 순위
# ---------------------------------------------------------------------------
def parse_standings(html: str, season_label: str, as_of: date) -> d.NormalizedBundle:
    """정규시즌 팀 순위표. as_of 는 수집 날짜(한국 시각) — 페이지의 기준일 표기는 확인되지 않아 쓰지 않는다."""
    warnings: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    season_label = _season_from_page(soup, season_label, warnings)
    table = soup.select_one("table.tData")
    if table is None:
        raise ValueError("순위표(table.tData)를 찾을 수 없습니다 — 페이지 구조 변경 확인 필요")

    teams: dict[str, d.TeamDTO] = {}
    standings: list[d.StandingDTO] = []
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < len(STANDINGS_COLUMNS):
            continue
        row = dict(zip(STANDINGS_COLUMNS, cells))
        rank = to_int(row["rank"])
        if rank is None or rank < 1:
            warnings.append(f"순위 해석 불가 행: {cells[:3]}")
            continue
        ext, team = team_ref(row["team"], warnings)
        teams[ext] = team
        stats = {k: v for k, v in {
            "std.G": to_int(row["games"]), "std.W": to_int(row["wins"]), "std.L": to_int(row["losses"]),
            "std.D": to_int(row["draws"]),
            # 1위 팀 게임차는 '0.0' 또는 '-' 로 표기될 수 있어 '-' 는 0 으로 본다
            "std.GB": 0.0 if row["games_behind"] == "-" else to_float(row["games_behind"]),
        }.items() if v is not None}
        standings.append(d.StandingDTO(season_label=season_label, stage_code="REG", team_external_id=ext,
                                       as_of_date=as_of, rank=rank, stats=stats))
    if not standings:
        raise ValueError("순위표에 팀 행이 없습니다 — 페이지 구조 변경 확인 필요")

    seasons, stages = _season_bundle_parts(season_label)
    return d.NormalizedBundle(seasons=seasons, stages=stages, teams=list(teams.values()),
                              standings=standings, warnings=warnings)


# ---------------------------------------------------------------------------
# 팀 시즌 기록
# ---------------------------------------------------------------------------
def parse_team_stats(pages: list[tuple[str, str]], season_label: str) -> d.NormalizedBundle:
    """팀 기록표 여러 페이지 [(kind, html), ...] 를 팀별로 합친다. kind = 'batting' | 'pitching'.

    타격·투구 기록이 각각 두 페이지(Basic1, Basic2)로 나뉘어 있는데, 팀 시즌 기록은 (스테이지, 팀) 당
    한 행이라 페이지별로 따로 저장하면 서로 덮어쓴다. 그래서 한 원문 문서에 묶어 받아 여기서 합친다.
    """
    warnings: list[str] = []
    teams: dict[str, d.TeamDTO] = {}
    merged: dict[str, dict[str, int]] = {}
    labels: set[str] = set()
    for kind, html in pages:
        label, page_stats = _parse_team_stat_page(html, kind, season_label, teams, warnings)
        labels.add(label)
        for ext, stats in page_stats.items():
            merged.setdefault(ext, {}).update(stats)
    if len(labels) > 1:
        raise ValueError(f"팀 기록 페이지들의 시즌이 서로 다릅니다: {sorted(labels)}")
    season_label = labels.pop() if labels else season_label

    seasons, stages = _season_bundle_parts(season_label)
    return d.NormalizedBundle(
        seasons=seasons, stages=stages, teams=list(teams.values()),
        team_season_stats=[d.TeamSeasonStatDTO(season_label=season_label, stage_code="REG",
                                               team_external_id=ext, stats=stats)
                           for ext, stats in merged.items() if stats],
        warnings=warnings)


def _parse_team_stat_page(html: str, kind: str, season_label: str, teams: dict[str, d.TeamDTO],
                          warnings: list[str]) -> tuple[str, dict[str, dict[str, int]]]:
    if kind not in ("batting", "pitching"):
        raise ValueError(f"알 수 없는 팀 기록 종류 {kind}")
    header_map = TEAM_BATTING_HEADERS if kind == "batting" else TEAM_PITCHING_HEADERS
    soup = BeautifulSoup(html, "html.parser")
    season_label = _season_from_page(soup, season_label, warnings)
    table = soup.select_one("table.tData")
    if table is None:
        raise ValueError(f"팀 {kind} 기록표(table.tData)를 찾을 수 없습니다 — 페이지 구조 변경 확인 필요")
    headers = [th.get_text(strip=True) for th in table.select("thead th")]
    if len(headers) < 3 or headers[1] != "팀명":
        raise ValueError(f"팀 {kind} 기록표 머리글이 예상과 다릅니다: {headers[:4]}")

    unknown = [h for h in headers[2:] if h not in header_map and h not in KNOWN_SKIPPED and h != "IP"]
    if unknown:
        warnings.append(f"팀 {kind} 기록표의 처리하지 않는 머리글: {', '.join(unknown)}")

    out: dict[str, dict[str, int]] = {}
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cells) < 3 or not cells[1]:
            continue
        ext, team = team_ref(cells[1], warnings)
        teams[ext] = team
        stats = out.setdefault(ext, {})
        for header, value in zip(headers[2:], cells[2:]):
            if header == "IP" and kind == "pitching":
                outs = kbo_innings_to_outs(value)
                if outs is None:
                    warnings.append(f"팀 {cells[1]} 이닝 표기 '{value}' 해석 불가")
                else:
                    stats["pit.OUTS"] = outs
            elif header in header_map:
                v = to_int(value)
                if v is not None:
                    stats[header_map[header]] = v
    if not out:
        raise ValueError(f"팀 {kind} 기록표에 팀 행이 없습니다 — 페이지 구조 변경 확인 필요")
    return season_label, out
