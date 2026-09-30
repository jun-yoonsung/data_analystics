# KBO 플러그인 테스트 픽스처 — 실제 파일

GitHub 공개 저장소에서 2026-09-30 에 받은 **실제 파일**이다 (가공하지 않음).

| 파일 | 원본 | 확인하는 내용 |
|---|---|---|
| `gh_schedule_2026_09.json` | [comographer/kbo-crawler](https://github.com/comographer/kbo-crawler) `data/raw/2026/schedule_2026_09.json` (커밋 db9e975) | 종료·예정·우천취소, 무승부, 중계 여러 개 |
| `gh_schedule_2026_03.json` | 같은 저장소 `data/raw/2026/schedule_2026_03.json` | 개막월, 무승부 |
| `gh_schedule_2023_10.json` | 같은 저장소 `data/raw/2023/schedule_2023_10.json` | 더블헤더(gameId 끝자리 1·2) |
| `gh_postseason_2025_10.json` | 같은 저장소 `data/raw/2025/postseason/postseason_2025_10.json` | 포스트시즌 |
| `gh_postseason_2025_12_empty.json` | 같은 저장소 `data/raw/2025/postseason/postseason_2025_12.json` | 경기 없는 달(빈 rows) |
| `gh_stats_meta.json`, `gh_stats_hitters.json`, `gh_stats_pitchers.json`, `gh_stats_players.json`, `gh_stats_standings.json` | [PsyproLEE/KBO_statics](https://github.com/PsyproLEE/KBO_statics) `web/public/data/*.json` (커밋 a23afb4) | 2026 시즌 선수 기록·프로필·순위 |
| `gh_stats_season_1982.json` | 같은 저장소 `web/public/data/season/1982.json` | 지난 시즌, 옛 구단명, 당시 미집계 항목(0 표시) |

두 저장소 모두 라이선스 파일이 없다. 테스트 목적으로만 저장소에 포함한다.
