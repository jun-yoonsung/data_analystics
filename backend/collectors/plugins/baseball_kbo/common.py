"""KBO 플러그인 공용: 구단 표, 이닝 표기 변환, 숫자 파싱. 모두 순수 함수."""
from __future__ import annotations

import re

from collectors.core import dto as d

# KBO 구단. 코드는 KBO 공식 기록 페이지 팀 필터 값이다(이전 kbo-dashboard 스크레이퍼에서 사용하던 값).
#  code: (약칭, 정식 명칭)
KBO_TEAMS: dict[str, tuple[str, str]] = {
    "LG": ("LG", "LG 트윈스"),
    "KT": ("KT", "KT 위즈"),
    "SK": ("SSG", "SSG 랜더스"),
    "NC": ("NC", "NC 다이노스"),
    "OB": ("두산", "두산 베어스"),
    "HT": ("KIA", "KIA 타이거즈"),
    "LT": ("롯데", "롯데 자이언츠"),
    "SS": ("삼성", "삼성 라이온즈"),
    "HH": ("한화", "한화 이글스"),
    "WO": ("키움", "키움 히어로즈"),
}
# 소스마다 표기가 다를 수 있어 약칭·정식 명칭·코드를 모두 받는다 (대소문자 무시)
_TEAM_ALIASES: dict[str, str] = {}
for _code, (_short, _full) in KBO_TEAMS.items():
    for _alias in (_code, _short, _full, _full.replace(" ", "")):
        _TEAM_ALIASES[_alias.upper()] = _code


def team_code(name: str | None) -> str | None:
    """소스의 팀 표기 → 구단 코드. 모르는 표기는 None."""
    if not name:
        return None
    return _TEAM_ALIASES.get(name.strip().upper())


def team_ref(name: str, warnings: list[str]) -> tuple[str, d.TeamDTO]:
    """팀 표기 → (외부 ID, TeamDTO).

    두 소스(네이버·KBO)가 같은 외부 ID·정식 명칭을 쓰도록 구단 코드로 통일한다.
    writer 는 소스별로 외부 ID 를 매핑하지만, 처음 보는 팀은 정식 명칭(name_ko)으로 기존 팀을 찾으므로
    두 소스의 팀이 같은 core.team 행으로 연결된다.
    모르는 표기(신생 구단 등)는 표기 그대로 팀을 만들고 경고를 남긴다.
    """
    code = team_code(name)
    if code is None:
        warnings.append(f"알 수 없는 팀 표기 '{name}' — common.KBO_TEAMS 에 추가 필요")
        ext = f"name:{name.strip()}"
        return ext, d.TeamDTO(external_id=ext, name_ko=name.strip())
    short, full = KBO_TEAMS[code]
    return code, d.TeamDTO(external_id=code, name_ko=full, short_name_ko=short, code=code)


_KBO_IP_RE = re.compile(r"^(?:(\d+))?\s*(?:([12])/3)?$")
_NAVER_FRACTIONS = {"⅓": 1, "⅔": 2}


def kbo_innings_to_outs(value: str | None) -> int | None:
    """KBO 기록 페이지 이닝 표기 → 아웃카운트. '6 1/3' → 19, '6' → 18, '2/3' → 2. 해석 불가면 None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    m = _KBO_IP_RE.match(s)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 3 + int(m.group(2) or 0)


def naver_innings_to_outs(value) -> int | None:
    """네이버 박스스코어 이닝 표기 → 아웃카운트. '6 ⅔' → 20, '0 ⅓' → 1, '5' → 15. 해석 불가면 None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    extra = 0
    for ch, outs in _NAVER_FRACTIONS.items():
        if ch in s:
            extra, s = outs, s.replace(ch, "").strip()
    if s == "":
        return extra
    if not s.isdigit():
        return None
    return int(s) * 3 + extra


def to_int(value) -> int | None:
    """'1,234' / 12 / '-' / '' → 정수 또는 None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    s = str(value).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return int(s)
    except ValueError:
        try:
            f = float(s)
        except ValueError:
            return None
        return int(f) if f.is_integer() else None


def to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None
