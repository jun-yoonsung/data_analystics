"""KBO 플러그인 공용: 구단 표, 이닝 표기 변환, 숫자 파싱. 모두 순수 함수."""
from __future__ import annotations

import re

from collectors.core import dto as d

# KBO 구단 (계승 관계 기준 한 구단 = 한 팀).
#  code: 현역 구단은 KBO 기록실 팀 코드 (LG, KT, SK, NC, OB, HT, LT, SS, HH, WO).
#        해체 구단은 KBO 코드가 확인되지 않아 이 플랫폼 내부 코드(HD, SB)를 쓴다.
#  name_ko 는 마지막(현재) 명칭이다. 옛 명칭은 former 에 두고, 소스의 옛 표기(예: 1982년 'MBC')는 계승 구단으로 연결한다.
#  (core.team_name_history 기록은 writer 가 아직 지원하지 않아 attrs.former_names 로 남긴다.)
KBO_TEAMS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    #      약칭     정식 명칭          옛 명칭(소스 표기)
    "LG": ("LG",   "LG 트윈스",      ("MBC",)),
    "KT": ("KT",   "KT 위즈",        ()),
    "SK": ("SSG",  "SSG 랜더스",     ("SK",)),
    "NC": ("NC",   "NC 다이노스",    ()),
    "OB": ("두산", "두산 베어스",    ("OB",)),
    "HT": ("KIA",  "KIA 타이거즈",   ("해태",)),
    "LT": ("롯데", "롯데 자이언츠",  ()),
    "SS": ("삼성", "삼성 라이온즈",  ()),
    "HH": ("한화", "한화 이글스",    ("빙그레",)),
    "WO": ("키움", "키움 히어로즈",  ("우리", "히어로즈", "넥센")),
    # 해체 구단
    "HD": ("현대", "현대 유니콘스",  ("삼미", "청보", "태평양")),
    "SB": ("쌍방울", "쌍방울 레이더스", ()),
}
# 소스마다 표기가 달라 약칭·정식 명칭·코드·옛 명칭을 모두 받는다 (대소문자 무시)
_TEAM_ALIASES: dict[str, str] = {}
for _code, (_short, _full, _former) in KBO_TEAMS.items():
    for _alias in (_code, _short, _full, _full.replace(" ", ""), *_former):
        _TEAM_ALIASES[_alias.upper()] = _code


def team_code(name: str | None) -> str | None:
    """소스의 팀 표기 → 구단 코드. 모르는 표기는 None."""
    if not name:
        return None
    return _TEAM_ALIASES.get(name.strip().upper())


def team_ref(name: str, warnings: list[str]) -> tuple[str, d.TeamDTO]:
    """팀 표기 → (외부 ID, TeamDTO).

    두 소스(일정·기록)가 같은 외부 ID·정식 명칭을 쓰도록 구단 코드로 통일한다.
    writer 는 소스별로 외부 ID 를 매핑하지만, 처음 보는 팀은 정식 명칭(name_ko)으로 기존 팀을 찾으므로
    두 소스의 팀이 같은 core.team 행으로 연결된다.
    모르는 표기(신생 구단 등)는 표기 그대로 팀을 만들고 경고를 남긴다.
    """
    code = team_code(name)
    if code is None:
        msg = f"알 수 없는 팀 표기 '{name}' — common.KBO_TEAMS 에 추가 필요"
        if msg not in warnings:
            warnings.append(msg)
        ext = f"name:{name.strip()}"
        return ext, d.TeamDTO(external_id=ext, name_ko=name.strip())
    short, full, former = KBO_TEAMS[code]
    attrs = {"former_names": list(former)} if former else {}
    if code in ("HD", "SB"):
        attrs["defunct"] = True
    return code, d.TeamDTO(external_id=code, name_ko=full, short_name_ko=short, code=code, attrs=attrs)


_KBO_IP_RE = re.compile(r"^(?:(\d+))?\s*(?:([12])/3)?$")


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
