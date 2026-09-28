"""수집 스케줄 디스패처.

Celery beat 는 이 디스패처 하나만 주기적으로(기본 10분) 실행한다. 리그별 수집 시각은
ingest.collection_schedule 의 cron(리그 타임존 기준)으로 DB 에서 관리하므로, 시각을 바꿔도 재배포가 필요 없다.

due 판정: cron 의 직전 발화 시각이 last_enqueued_at 이후이면 due.
  (MAX_LAG 보다 오래된 발화는 놓친 것으로 보고 건너뜀 — 워커 장애 복구 직후 폭주 방지)
비시즌 판정: 오늘(리그 타임존)이 어떤 시즌의 [start_date - 1일, end_date + 1일] 에도 속하지 않으면 비시즌.
  - offseason_policy=skip  : 건너뜀 (skipped 로그 기록)
  - offseason_policy=weekly: 월요일에만 실행
  - offseason_policy=run   : 항상 실행
  - 시즌 정보가 아직 없는 리그(최초 구축)는 schedule 작업만 실행해 시즌을 발견하게 한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import Connection, text

MAX_LAG = timedelta(hours=6)
SEASON_BUFFER = timedelta(days=1)


@dataclass(frozen=True)
class DueJob:
    schedule_id: int
    league_code: str
    job_type: str
    params: dict
    fire_time: datetime
    skip_reason: str | None = None     # 비시즌 등으로 실행하지 않을 때


def season_state(conn: Connection, league_id: int, today) -> str:
    """'in' / 'off' / 'unknown'(시즌 기간 정보 없음)."""
    row = conn.execute(text("""
        SELECT count(*) FILTER (WHERE start_date IS NOT NULL) AS known,
               count(*) FILTER (WHERE :d BETWEEN start_date - :buf AND end_date + :buf) AS current
        FROM core.season WHERE league_id = :l
    """), {"l": league_id, "d": today, "buf": SEASON_BUFFER}).one()
    if row.known == 0:
        return "unknown"
    return "in" if row.current else "off"


def find_due(conn: Connection, now: datetime) -> list[DueJob]:
    """due 스케줄을 찾고 last_enqueued_at 을 갱신한다 (호출자가 커밋).

    FOR UPDATE SKIP LOCKED 로 디스패처가 여러 개 떠도 같은 작업을 두 번 넣지 않는다.
    """
    rows = conn.execute(text("""
        SELECT cs.id, cs.cron, cs.timezone, cs.offseason_policy, cs.params, cs.last_enqueued_at, cs.job_type,
               l.id AS league_id, l.code AS league_code
        FROM ingest.collection_schedule cs
        JOIN core.league l ON l.id = cs.league_id
        WHERE cs.enabled AND l.is_active
        FOR UPDATE OF cs SKIP LOCKED
    """)).all()
    due: list[DueJob] = []
    for r in rows:
        tz = ZoneInfo(r.timezone)
        local_now = now.astimezone(tz)
        fire = croniter(r.cron, local_now).get_prev(datetime)
        if r.last_enqueued_at is not None and r.last_enqueued_at >= fire:
            continue
        conn.execute(text("UPDATE ingest.collection_schedule SET last_enqueued_at = :n WHERE id = :id"),
                     {"n": now, "id": r.id})
        if local_now - fire > MAX_LAG:
            continue
        today = local_now.date()
        state = season_state(conn, r.league_id, today)
        reason = None
        if state == "unknown" and r.job_type != "schedule":
            reason = "시즌 정보 없음 (schedule 작업으로 시즌을 먼저 수집해야 함)"
        elif state == "off":
            if r.offseason_policy == "skip":
                reason = "비시즌"
            elif r.offseason_policy == "weekly" and today.weekday() != 0:
                reason = "비시즌 (주 1회 월요일에만 실행)"
        due.append(DueJob(schedule_id=r.id, league_code=r.league_code, job_type=r.job_type,
                          params=dict(r.params), fire_time=fire, skip_reason=reason))
    return due
