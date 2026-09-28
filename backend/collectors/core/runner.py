"""수집 실행기: 리그·작업 하나를 끝까지 처리한다.

  락 획득 → ingest_run 시작 → 플러그인 fetch → raw 저장 → normalize → 저장(writer)
  → 파생 지표·집계·리그 상수(derive) → 리더보드 MV 갱신 → ingest_run 종료 → 실패 시 알림

- 같은 리그는 동시에 한 작업만 실행한다 (PostgreSQL advisory lock).
- 문서 하나의 실패가 전체를 멈추지 않는다: 문서 단위 트랜잭션, 실패 문서는 raw 에 failed 로 남긴다.
- 소스가 수집 비허용 상태면 요청 없이 skipped 로 기록한다 (알림 없음).
"""
from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, text

from collectors.core.alerts import Notifier, alert_run
from collectors.core.http import (CollectionNotAllowed, FetchError, PoliteHttpClient, RobotsDisallowed,
                                  SourcePolicy)
from collectors.core.interface import CollectorPlugin, MatchTarget, NotSupported, RawDocument, RunContext
from collectors.core.raw_store import load_raw, mark_parsed, save_raw
from collectors.core.registry import PluginNotFound, get_plugin
from collectors.core.writer import BundleWriter, LeagueInfo, WriteStats
from stats_engine.catalog import load_catalog
from stats_engine.derive import Deriver

log = logging.getLogger(__name__)

MATCH_JOB_PARTS = {
    "results": frozenset({"summary"}),
    "boxscore": frozenset({"summary", "boxscore"}),
    "events": frozenset({"events"}),
}
JOB_TYPES = ("schedule", "results", "boxscore", "events", "season_stats", "standings", "roster", "players")
MAX_WARNINGS = 200


@dataclass(frozen=True)
class JobRequest:
    league_code: str
    job_type: str
    trigger: str = "manual"           # schedule / manual / reprocess / backfill
    schedule_id: int | None = None
    params: dict = field(default_factory=dict)


@dataclass
class RunResult:
    run_id: int | None
    status: str
    counts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class _Fatal(Exception):
    """실행 전체를 중단해야 하는 오류."""


def _league_info(conn: Connection, league_code: str) -> LeagueInfo:
    r = conn.execute(text("""
        SELECT l.id, l.code, l.sport_id, s.code AS sport_code, l.timezone, l.collector_key, l.settings
        FROM core.league l JOIN config.sport s ON s.id = l.sport_id WHERE l.code = :c
    """), {"c": league_code}).first()
    if r is None:
        raise _Fatal(f"리그 {league_code} 가 없습니다 (sync-config 필요)")
    return LeagueInfo(id=r.id, code=r.code, sport_id=r.sport_id, sport_code=r.sport_code, timezone=r.timezone,
                      collector_key=r.collector_key, settings=r.settings)


def _source(conn: Connection, code: str):
    r = conn.execute(text("SELECT * FROM ingest.data_source WHERE code = :c"), {"c": code}).first()
    if r is None:
        raise _Fatal(f"데이터 소스 {code} 가 없습니다 (sync-config 필요)")
    return r


class CollectionRunner:
    def __init__(self, engine: Engine, *, notifier: Notifier | None = None,
                 http_factory: Callable[[SourcePolicy], PoliteHttpClient] | None = None,
                 now: Callable[[], datetime] | None = None,
                 plugin_factory: Callable[[str], CollectorPlugin] = get_plugin) -> None:
        self.engine = engine
        self.notifier = notifier
        self.http_factory = http_factory or (lambda policy: PoliteHttpClient(policy))
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.plugin_factory = plugin_factory

    # ==================================================================
    def run(self, req: JobRequest) -> RunResult:
        if req.job_type not in JOB_TYPES:
            raise ValueError(f"알 수 없는 작업 종류: {req.job_type}")
        with self.engine.connect() as lock_conn:
            got = lock_conn.execute(text("SELECT pg_try_advisory_lock(util.collect_lock_key(:c))"),
                                    {"c": req.league_code}).scalar_one()
            lock_conn.commit()
            if not got:
                return self.record_skip(req, "같은 리그의 다른 수집 작업이 실행 중입니다")
            try:
                return self._run_locked(req)
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(util.collect_lock_key(:c))"),
                                  {"c": req.league_code})
                lock_conn.commit()

    def record_skip(self, req: JobRequest, reason: str, league_id: int | None = None,
                     source_id: int | None = None) -> RunResult:
        with self.engine.begin() as conn:
            if league_id is None:
                league_id = conn.execute(text("SELECT id FROM core.league WHERE code = :c"),
                                         {"c": req.league_code}).scalar()
            run_id = conn.execute(text("""
                INSERT INTO ingest.ingest_run (schedule_id, league_id, source_id, job_type, trigger, status,
                                               params, error_message, finished_at)
                VALUES (:sch, :l, :src, :job, :trg, 'skipped', CAST(:p AS jsonb), :msg, now()) RETURNING id
            """), {"sch": req.schedule_id, "l": league_id, "src": source_id, "job": req.job_type,
                   "trg": req.trigger, "p": _json(req.params), "msg": reason}).scalar_one()
        log.info("수집 건너뜀 %s/%s: %s", req.league_code, req.job_type, reason)
        return RunResult(run_id, "skipped", error=reason)

    # ==================================================================
    def _run_locked(self, req: JobRequest) -> RunResult:
        try:
            with self.engine.begin() as conn:
                league = _league_info(conn, req.league_code)
                plugin = self.plugin_factory(league.collector_key or "")
                if plugin.sport_code != league.sport_code:
                    raise _Fatal(f"플러그인 {plugin.key} 종목({plugin.sport_code}) ≠ 리그 종목({league.sport_code})")
                source = _source(conn, plugin.source_code)
        except PluginNotFound as exc:
            return self.record_skip(req, str(exc))
        except _Fatal as exc:
            return self.record_skip(req, str(exc))

        if not source.collection_allowed and req.trigger != "reprocess":
            return self.record_skip(req, f"소스 {source.code} 가 수집 비허용 상태입니다 (약관·robots.txt 검토 필요)",
                                     league.id, source.id)

        with self.engine.begin() as conn:
            run_id = conn.execute(text("""
                INSERT INTO ingest.ingest_run (schedule_id, league_id, source_id, job_type, trigger, params)
                VALUES (:sch, :l, :src, :job, :trg, CAST(:p AS jsonb)) RETURNING id
            """), {"sch": req.schedule_id, "l": league.id, "src": source.id, "job": req.job_type,
                   "trg": req.trigger, "p": _json(req.params)}).scalar_one()

        total = WriteStats()
        errors: list[str] = []          # 문서·경기 단위 오류 (한 줄 요약)
        details: list[str] = []         # 치명적 오류 traceback
        docs_ok = 0
        fatal: str | None = None
        http: PoliteHttpClient | None = None
        try:
            with self.engine.connect() as conn:
                catalog = load_catalog(conn, league.sport_id)
            if req.trigger == "reprocess":
                documents = self._raw_documents(league, source.id, req)
            else:
                http = self.http_factory(SourcePolicy(
                    code=source.code, base_url=source.base_url, collection_allowed=source.collection_allowed,
                    min_interval_ms=source.min_interval_ms, max_retries=source.max_retries,
                    user_agent=source.user_agent, robots_url=source.robots_url))
                ctx = self._context(league, req, http)
                documents = ((None, doc) for doc in self._fetch(plugin, ctx, req, errors))

            for raw_id, doc in documents:
                ok = self._process_document(plugin, league, catalog, source.id, run_id, raw_id, doc, total, errors)
                docs_ok += ok

            with self.engine.begin() as conn:
                deriver = Deriver(conn, catalog)
                report = deriver.run(total.match_ids, total.stage_ids)
                conn.execute(text("SELECT util.refresh_stat_views()"))
                total.counts["derived.rows"] += report.derived_rows
                total.counts["aggregated.rows"] += report.aggregated_rows
                if req.job_type == "boxscore":
                    total.warnings += self._missing_boxscores(conn, league, req)
        except (CollectionNotAllowed, RobotsDisallowed) as exc:
            fatal = str(exc)
        except NotSupported as exc:
            fatal = None
            total.warnings.append(f"플러그인이 {exc} 작업을 지원하지 않습니다")
        except Exception as exc:  # 예상치 못한 오류도 실행 로그에 남긴다
            log.exception("수집 실행 오류")
            fatal = f"{type(exc).__name__}: {(str(exc).strip().splitlines() or [''])[0]}"
            details.append(traceback.format_exc(limit=5))

        if fatal:
            status = "failed"
        elif errors:
            status = "partial" if docs_ok else "failed"
        else:
            status = "success"
        warnings = (total.warnings + errors)[:MAX_WARNINGS]
        counts = total.totals()
        if http is not None:
            counts["http_requests"] = http.request_count
        with self.engine.begin() as conn:
            conn.execute(text("""
                UPDATE ingest.ingest_run
                SET status = :st, finished_at = now(), counts = CAST(:c AS jsonb), warnings = CAST(:w AS jsonb),
                    error_message = :err, error_detail = CAST(:det AS jsonb)
                WHERE id = :id
            """), {"id": run_id, "st": status, "c": _json(counts), "w": _json(warnings),
                   "err": fatal or (errors[0] if errors else None),
                   "det": _json({"errors": errors[:50], "traceback": details}) if errors or details else None})
            if http is not None and http.robots_checked:
                conn.execute(text("UPDATE ingest.data_source SET robots_checked_at = now() WHERE id = :id"),
                             {"id": source.id})
            alert_run(conn, run_id, self.notifier)
        return RunResult(run_id, status, counts, warnings, fatal)

    # ==================================================================
    def _context(self, league: LeagueInfo, req: JobRequest, http: PoliteHttpClient) -> RunContext:
        today = self.now().astimezone(ZoneInfo(league.timezone)).date()
        return RunContext(league_code=league.code, sport_code=league.sport_code, timezone=league.timezone,
                          job_type=req.job_type, today=today, http=http, league_settings=league.settings,
                          params=req.params, log=logging.getLogger(f"collector.{league.code}"))

    def _fetch(self, plugin: CollectorPlugin, ctx: RunContext, req: JobRequest,
               errors: list[str]) -> Iterator[RawDocument]:
        p = req.params
        if req.job_type == "schedule":
            date_from = _date(p.get("date_from")) or ctx.today - timedelta(days=int(p.get("days_back", 3)))
            date_to = _date(p.get("date_to")) or ctx.today + timedelta(days=int(p.get("days_ahead", 14)))
            yield from plugin.fetch_schedule(ctx, date_from, date_to)
        elif req.job_type in MATCH_JOB_PARTS:
            for target in self._match_targets(ctx, req):
                try:
                    yield from plugin.fetch_match(ctx, target, MATCH_JOB_PARTS[req.job_type])
                except (CollectionNotAllowed, RobotsDisallowed, NotSupported):
                    raise
                except FetchError as exc:
                    errors.append(f"경기 {target.external_id}: {exc}")
        else:
            season = plugin.season_for_date(ctx.league_code, _date(p.get("date")) or ctx.today)
            fetcher = {"season_stats": plugin.fetch_player_stats, "standings": plugin.fetch_standings,
                       "roster": plugin.fetch_roster, "players": plugin.fetch_roster}[req.job_type]
            yield from fetcher(ctx, season)

    def _match_targets(self, ctx: RunContext, req: JobRequest) -> list[MatchTarget]:
        recheck = int(req.params.get("recheck_days", 1))
        date_from = _date(req.params.get("date_from")) or ctx.today - timedelta(days=recheck)
        date_to = _date(req.params.get("date_to")) or ctx.today
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT m.external_id, x.local_date, x.status
                FROM core.match x
                JOIN core.season s ON s.id = x.season_id
                JOIN core.league l ON l.id = s.league_id
                JOIN ingest.external_id_map m ON m.entity_type = 'match' AND m.entity_id = x.id
                JOIN ingest.data_source ds ON ds.id = m.source_id
                WHERE l.code = :league AND ds.code = :source
                  AND x.local_date BETWEEN :f AND :t AND x.status NOT IN ('cancelled', 'postponed')
                ORDER BY x.scheduled_at
            """), {"league": ctx.league_code, "source": self._source_code(ctx), "f": date_from, "t": date_to}).all()
        return [MatchTarget(external_id=r.external_id, local_date=r.local_date, status=r.status) for r in rows]

    def _source_code(self, ctx: RunContext) -> str:
        return ctx.http.policy.code

    def _raw_documents(self, league: LeagueInfo, source_id: int, req: JobRequest) -> Iterable[tuple[int, RawDocument]]:
        """재처리 대상: 리그의 raw 중 (문서 종류, 외부 키)별 최신 원문."""
        with self.engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT DISTINCT ON (p.document_type, p.external_key) p.*
                FROM ingest.raw_payload p JOIN ingest.ingest_run r ON r.id = p.ingest_run_id
                WHERE p.source_id = :s AND r.league_id = :l
                  AND (CAST(:dt AS text) IS NULL OR p.document_type = :dt)
                  AND (CAST(:since AS timestamptz) IS NULL OR p.fetched_at >= :since)
                ORDER BY p.document_type, p.external_key, p.fetched_at DESC
            """), {"s": source_id, "l": league.id, "dt": req.params.get("document_type"),
                   "since": req.params.get("since")}).all()
        # 일정 → 경기 → 기록 순서가 되도록 문서 종류 우선순위로 정렬
        order = {"schedule": 0, "match_summary": 1, "boxscore": 2, "events": 3}
        rows = sorted(rows, key=lambda r: (order.get(r.document_type, 9), r.fetched_at))
        for r in rows:
            yield r.id, load_raw(r)

    def _process_document(self, plugin: CollectorPlugin, league: LeagueInfo, catalog, source_id: int,
                          run_id: int, raw_id: int | None, doc: RawDocument, total: WriteStats,
                          errors: list[str]) -> bool:
        if raw_id is None:
            with self.engine.begin() as conn:
                stored = save_raw(conn, source_id=source_id, ingest_run_id=run_id, doc=doc,
                                  parser_version=plugin.parser_version)
                raw_id = stored.id
            total.add("ingest.raw_payload", "inserted" if stored.is_new else "unchanged")
        try:
            bundle = plugin.normalize(doc, league.code)
            with self.engine.begin() as conn:
                writer = BundleWriter(conn, league=league, catalog=catalog, source_id=source_id,
                                      ingest_run_id=run_id)
                total.merge(writer.write(bundle))
                mark_parsed(conn, raw_id, plugin.parser_version)
            return True
        except Exception as exc:
            detail = f"{doc.document_type}/{doc.external_key}: {type(exc).__name__}: {exc}"
            first_line = (str(exc).strip().splitlines() or [""])[0]
            log.warning("문서 처리 실패 %s", detail)
            errors.append(f"{doc.document_type}/{doc.external_key}: {type(exc).__name__}: {first_line}")
            with self.engine.begin() as conn:
                mark_parsed(conn, raw_id, plugin.parser_version, error=detail[:2000])
            return False

    def _missing_boxscores(self, conn: Connection, league: LeagueInfo, req: JobRequest) -> list[str]:
        """종료 경기인데 선수 기록이 없는 경우 (소스 지연·파서 오류 징후)."""
        today = self.now().astimezone(ZoneInfo(league.timezone)).date()
        recheck = int(req.params.get("recheck_days", 1))
        rows = conn.execute(text("""
            SELECT x.id, x.local_date FROM core.match x JOIN core.season s ON s.id = x.season_id
            WHERE s.league_id = :l AND x.status = 'final' AND x.local_date BETWEEN :f AND :t
              AND NOT EXISTS (SELECT 1 FROM core.player_match_stat p WHERE p.match_id = x.id)
        """), {"l": league.id, "f": today - timedelta(days=recheck), "t": today}).all()
        return [f"이상 징후: 종료 경기 {r.id}({r.local_date}) 에 선수 기록이 없습니다" for r in rows]


def _date(value) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
