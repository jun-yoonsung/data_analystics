# KBO 플러그인 테스트 픽스처 — 합성 데이터 (실제 응답 아님)

이 작업 환경은 `www.koreabaseball.com`, `api-gw.sports.naver.com` 접속이 막혀 있어 실제 응답을 받을 수 없었다.
여기 있는 파일은 이전 프로젝트 [kbo-dashboard](https://github.com/jun-yoonsung/kbo-dashboard) 의
`kbo_scraper.py` 가 **실제로 읽던 필드·선택자만** 사용해 손으로 만든 합성 응답이다.

- 선수 이름(`가상타자1` 등)과 기록 값은 모두 가상이다. 팀 이름만 실제 구단 표기를 쓴다.
- 대시보드가 쓰지 않던 필드(선수 코드, 이닝별 점수, 포스트시즌 roundCode 등)는 넣지 않았다.
- 소스 접속이 가능해지면 실제 응답으로 교체하거나 추가하고, 달라진 점이 있으면 파서와
  `parser_version` 을 고친 뒤 `python -m app.cli reprocess` 로 재처리한다.

| 파일 | 소스 | 대응 URL |
|---|---|---|
| `naver_schedule.json` | naver_sports | `/schedule/games?fields=basic,schedule,baseball&fromDate=2026-09-26&toDate=2026-09-28&categoryId=kbo` |
| `naver_record_20260926LGOB02026.json` | naver_sports | `/schedule/games/20260926LGOB02026/record` |
| `kbo_teamrank.html` | kbo_official | `/record/teamrank/teamrank.aspx` |
| `kbo_team_hitter_basic1.html`, `…basic2.html` | kbo_official | `/Record/Team/Hitter/Basic1.aspx`, `Basic2.aspx` |
| `kbo_team_pitcher_basic1.html`, `…basic2.html` | kbo_official | `/Record/Team/Pitcher/Basic1.aspx`, `Basic2.aspx` |

KBO 팀 기록표의 Basic1/Basic2 머리글 구성은 대시보드 코드에 명시되어 있지 않아, 대시보드가 사용하던 지표 약어로
구성했다. 파서는 머리글 텍스트로 열을 찾으므로(순서 무관) 실제 구성이 달라도 동작하며, 모르는 머리글은 경고로 남긴다.
