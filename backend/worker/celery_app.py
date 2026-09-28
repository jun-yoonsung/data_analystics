"""Celery 앱.

  worker   : celery -A worker.celery_app worker -Q http,browser -l info
  scheduler: celery -A worker.celery_app beat -l info

beat 에는 dispatch 하나만 등록한다. 실제 리그별 일정은 DB(ingest.collection_schedule)에 있다.
Playwright 가 필요한 플러그인(requires_browser)은 'browser' 큐로 보내 메모리 사용이 큰 작업을 분리한다.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import create_engine, text

from app.settings import get_database_url
from collectors.core.alerts import notifier_from_env
from collectors.core.dispatch import find_due
from collectors.core.registry import PluginNotFound, get_plugin
from collectors.core.runner import CollectionRunner, JobRequest

log = logging.getLogger(__name__)

app = Celery("sports", broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
app.conf.update(
    timezone="Asia/Seoul",
    task_default_queue="http",
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "dispatch-collection": {
            "task": "worker.celery_app.dispatch",
            "schedule": crontab(minute=f"*/{int(os.environ.get('DISPATCH_INTERVAL_MIN', '10'))}"),
        },
    },
)

_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_database_url(), pool_pre_ping=True)
    return _engine


def _queue_for(league_code: str) -> str:
    with engine().connect() as conn:
        key = conn.execute(text("SELECT collector_key FROM core.league WHERE code = :c"),
                           {"c": league_code}).scalar()
    try:
        return "browser" if get_plugin(key or "").requires_browser else "http"
    except PluginNotFound:
        return "http"


@app.task(name="worker.celery_app.dispatch")
def dispatch() -> int:
    """due 스케줄을 수집 큐에 넣는다. 비시즌 등은 skipped 로그만 남긴다."""
    runner = CollectionRunner(engine(), notifier=notifier_from_env())
    with engine().begin() as conn:
        jobs = find_due(conn, datetime.now(timezone.utc))
    for job in jobs:
        req_kwargs = {"league_code": job.league_code, "job_type": job.job_type, "trigger": "schedule",
                      "schedule_id": job.schedule_id, "params": job.params}
        if job.skip_reason:
            runner.record_skip(JobRequest(**req_kwargs), job.skip_reason)
            continue
        collect.apply_async(kwargs=req_kwargs, queue=_queue_for(job.league_code))
    return len(jobs)


@app.task(name="worker.celery_app.collect")
def collect(**kwargs) -> dict:
    result = CollectionRunner(engine(), notifier=notifier_from_env()).run(JobRequest(**kwargs))
    return {"run_id": result.run_id, "status": result.status, "error": result.error}
