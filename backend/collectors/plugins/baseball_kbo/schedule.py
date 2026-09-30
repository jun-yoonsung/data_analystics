"""KBO 경기 일정·결과 → 정규화 DTO. 순수 함수.

소스: GitHub 공개 저장소 comographer/kbo-crawler 의 data/raw/{연도}/ 파일.
  그 저장소가 매일(23:55 KST) KBO 일정 웹서비스 응답을 월별로 그대로 저장한다.
    정규시즌:   schedule_{연도}_{월}.json
    포스트시즌: postseason/postseason_{연도}_{월}.json

응답 구조 (실제 파일로 확인, tests/fixtures/kbo/):
  {"rows": [{"row": [셀, ...]}, ...]}, 셀 = {"Text": HTML, "Class": ...}
  날짜 셀(Class='day', 예: '09.01(화)')은 그 날 첫 경기 행에만 있고 다음 행들은 이어받는다 (RowSpan).
  나머지 셀 순서: 시각, 대진(play), 리뷰/프리뷰 링크, 하이라이트 링크, 중계, (빈 칸), 구장, 비고
    대진: <span>원정</span><em><span class="win">3</span><span>vs</span><span class="lose">1</span></em><span>홈</span>
          점수 class = win / lose / same(무승부). 경기 전·취소 경기는 점수가 없다.
    링크 href 의 gameId = 날짜 + 원정 코드 + 홈 코드 + 더블헤더 번호(0: 단일, 1·2: 1·2차전)
    비고: '-' 정상, '우천취소'·'그라운드사정'·'폭염취소'·'미세먼지취소'·'강풍취소'·'기타' 등 취소 사유
  '이동일' 처럼 경기가 없는 행은 대진 칸이 비어 있다.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from collectors.core import dto as d
from collectors.plugins.baseball_kbo.common import team_ref, to_int

KST = ZoneInfo("Asia/Seoul")

STAGES = {
    "regular": d.StageDTO(season_label="", code="REG", name_ko="정규시즌", stage_type="regular", sort_order=1),
    "postseason": d.StageDTO(season_label="", code="POST", name_ko="포스트시즌", stage_type="postseason",
                             sort_order=2),
}

_GAME_ID = re.compile(r"gameId=(\w+)")
_DAY = re.compile(r"^(\d{1,2})\.(\d{1,2})")
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_TAG = re.compile(r"<[^>]+>")
_SCORE_CLASSES = {"win", "lose", "same"}


def parse_schedule(data: dict, year: int, kind: str) -> d.NormalizedBundle:
    """월별 일정 파일 하나. kind = 'regular' | 'postseason'."""
    if kind not in STAGES:
        raise ValueError(f"알 수 없는 일정 종류 {kind}")
    if "rows" not in data:
        raise ValueError("일정 응답에 rows 가 없습니다 — 파일 구조 변경 확인 필요")
    season_label = str(year)
    stage = STAGES[kind].model_copy(update={"season_label": season_label})
    warnings: list[str] = []
    teams: dict[str, d.TeamDTO] = {}
    venues: dict[str, d.VenueDTO] = {}
    matches: list[d.MatchDTO] = []
    current_day: date | None = None

    for i, r in enumerate(data.get("rows") or [], start=1):
        cells = r.get("row") or []
        if cells and cells[0].get("Class") == "day":
            current_day = _parse_day(cells[0].get("Text"), year)
            cells = cells[1:]
        texts = [c.get("Text") or "" for c in cells]
        if len(texts) < 8 or not texts[1].strip():
            continue                                  # 이동일 등 경기 없는 행
        if current_day is None:
            warnings.append(f"{i}번째 행: 날짜를 알 수 없어 무시")
            continue
        try:
            match = _parse_row(texts, current_day, season_label, stage.code, teams, venues, warnings)
        except ValueError as exc:
            warnings.append(f"{current_day} {i}번째 행: {exc} (무시)")
            continue
        matches.append(match)

    return d.NormalizedBundle(
        seasons=[d.SeasonDTO(label=season_label, start_year=year)] if matches else [],
        stages=[stage] if matches else [],
        venues=list(venues.values()), teams=list(teams.values()), matches=matches, warnings=warnings)


def _parse_day(text: str | None, year: int) -> date | None:
    m = _DAY.match((text or "").strip())
    return date(year, int(m.group(1)), int(m.group(2))) if m else None


def _parse_row(texts: list[str], day: date, season_label: str, stage_code: str,
               teams: dict[str, d.TeamDTO], venues: dict[str, d.VenueDTO], warnings: list[str]) -> d.MatchDTO:
    time_text, play, review, highlight, broadcast, _, stadium, note = texts[:8]

    soup = BeautifulSoup(play, "html.parser")
    names = [s.get_text(strip=True) for s in soup.find_all("span", recursive=False)]
    if len(names) != 2 or not all(names):
        raise ValueError(f"대진 해석 불가 '{_TAG.sub(' ', play).strip()}'")
    away_id, away = team_ref(names[0], warnings)
    home_id, home = team_ref(names[1], warnings)
    teams.setdefault(away_id, away)
    teams.setdefault(home_id, home)

    scores = [s for s in (soup.find("em").find_all("span") if soup.find("em") else [])
              if _SCORE_CLASSES & set(s.get("class") or [])]
    note = _TAG.sub("", note).strip()
    if len(scores) == 2:
        status, away_score, home_score = "final", to_int(scores[0].get_text()), to_int(scores[1].get_text())
    elif note and note != "-":
        # KBO 는 취소 경기를 나중에 새 경기(새 gameId)로 다시 편성한다 → 이 경기는 연기로 본다
        status, away_score, home_score = "postponed", None, None
    else:
        status, away_score, home_score = "scheduled", None, None

    m = _GAME_ID.search(review + highlight)
    if m:
        game_id = m.group(1)
    elif away.code and home.code:
        # 취소 경기는 링크가 없어 KBO 형식(날짜+원정+홈+0)으로 만든다
        game_id = f"{day:%Y%m%d}{away.code}{home.code}0"
    else:
        game_id = f"{day:%Y%m%d}-{away_id}-{home_id}"
    dh = game_id[-1]
    game_number = int(dh) if dh in "12" else 1

    tm = _TIME.search(time_text)
    scheduled_at = datetime.combine(day, time(int(tm.group(1)), int(tm.group(2))) if tm else time(0, 0), KST)

    stadium = _TAG.sub("", stadium).strip() or None
    if stadium:
        venues.setdefault(stadium, d.VenueDTO(external_id=stadium, name_ko=stadium))
    channels = [c.strip() for c in re.split(r"<br\s*/?>", broadcast) if _TAG.sub("", c).strip()]
    attrs = {k: v for k, v in {
        "broadcast": channels or None,
        "cancel_reason": note if status == "postponed" else None,
        "time_unknown": True if not tm else None,
    }.items() if v is not None}

    return d.MatchDTO(
        external_id=game_id, season_label=season_label, stage_code=stage_code,
        home_team_external_id=home_id, away_team_external_id=away_id, venue_external_id=stadium,
        scheduled_at=scheduled_at, game_number=game_number, status=status,
        home_score=home_score, away_score=away_score, attrs=attrs)
