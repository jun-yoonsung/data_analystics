"""운영 CLI.

사용 예
  python -m app.cli validate-config                 # DB 없이 설정만 검증
  python -m app.cli sync-config --dry-run           # DB 반영 결과 미리보기 (롤백)
  python -m app.cli sync-config                     # DB 반영
  python -m app.cli plugins                         # 등록된 수집 플러그인
  python -m app.cli collect --league KBO --job schedule --param days_back=7
  python -m app.cli reprocess --league KBO [--document-type boxscore]   # 저장된 raw 로 재처리
  python -m app.cli dispatch [--dry-run]            # 지금 due 인 수집 작업 실행 (beat 없이 수동)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine

from app.settings import get_database_url
from config_sync.loader import ConfigError, config_dir_from_env, load_config
from config_sync.sync import sync_bundle
from config_sync.validate import validate_bundle


def _load_and_validate(config_dir: Path):
    bundle = load_config(config_dir)
    validate_bundle(bundle)
    return bundle


def cmd_validate(args: argparse.Namespace) -> int:
    bundle = _load_and_validate(args.config_dir)
    for code, s in sorted(bundle.sports.items()):
        derived = sum(1 for st in s.stats if st.is_derived)
        print(f"  {code}: 지표 {len(s.stats)}개 (파생 {derived}), 구간 {len(s.config.periods)}, "
              f"포지션 {len(s.config.positions)}, 이벤트 {len(s.config.event_types)}, "
              f"스플릿 {len(s.config.splits)}, 자격 조건 {len(s.config.qualification_rules)}")
    print(f"  공통 스플릿 {len(bundle.common.splits)}, 소스 {len(bundle.sources)}, 리그 {len(bundle.leagues)}")
    print("설정 검증 통과")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    bundle = _load_and_validate(args.config_dir)
    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            report = sync_bundle(conn, bundle)
        except Exception:
            trans.rollback()
            raise
        if args.dry_run:
            trans.rollback()
            print("[dry-run] 아래 변경은 롤백되었습니다")
        else:
            trans.commit()
    print("\n".join(report.lines()))
    return 0


def _parse_params(items: list[str]) -> dict:
    params = {}
    for item in items or []:
        key, _, value = item.partition("=")
        params[key] = int(value) if value.lstrip("-").isdigit() else value
    return params


def _print_result(result) -> None:
    print(f"실행 {result.run_id}: {result.status}" + (f" — {result.error}" if result.error else ""))
    for key, value in result.counts.items():
        if key != "by_table":
            print(f"  {key}: {value}")
    for key, value in result.counts.get("by_table", {}).items():
        print(f"    {key}: {value}")
    for w in result.warnings[:20]:
        print(f"  ! {w}")


def cmd_plugins(args: argparse.Namespace) -> int:
    from collectors.core.registry import registered_plugins
    plugins = registered_plugins()
    if not plugins:
        print("등록된 수집 플러그인이 없습니다")
    for key, cls in sorted(plugins.items()):
        print(f"  {key}: 종목={cls.sport_code}, 소스={cls.source_code}, parser_version={cls.parser_version}"
              + (", 브라우저 필요" if cls.requires_browser else ""))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from collectors.core.alerts import notifier_from_env
    from collectors.core.runner import CollectionRunner, JobRequest
    runner = CollectionRunner(create_engine(get_database_url()), notifier=notifier_from_env())
    result = runner.run(JobRequest(league_code=args.league, job_type=args.job,
                                   trigger="backfill" if args.backfill else "manual",
                                   params=_parse_params(args.param)))
    _print_result(result)
    return 0 if result.status in ("success", "skipped") else 2


def cmd_reprocess(args: argparse.Namespace) -> int:
    from collectors.core.runner import CollectionRunner, JobRequest
    params = {"document_type": args.document_type, "since": args.since}
    result = CollectionRunner(create_engine(get_database_url())).run(
        JobRequest(league_code=args.league, job_type="schedule", trigger="reprocess",
                   params={k: v for k, v in params.items() if v}))
    _print_result(result)
    return 0 if result.status == "success" else 2


def cmd_dispatch(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    from collectors.core.alerts import notifier_from_env
    from collectors.core.dispatch import find_due
    from collectors.core.runner import CollectionRunner, JobRequest
    engine = create_engine(get_database_url())
    with engine.connect() as conn:
        trans = conn.begin()
        jobs = find_due(conn, datetime.now(timezone.utc))
        (trans.rollback if args.dry_run else trans.commit)()
    runner = CollectionRunner(engine, notifier=notifier_from_env())
    for job in jobs:
        print(f"  {job.league_code}/{job.job_type} (발화 {job.fire_time:%Y-%m-%d %H:%M %Z})"
              + (f" — 건너뜀: {job.skip_reason}" if job.skip_reason else ""))
        if args.dry_run:
            continue
        req = JobRequest(league_code=job.league_code, job_type=job.job_type, trigger="schedule",
                         schedule_id=job.schedule_id, params=job.params)
        if job.skip_reason:
            runner.record_skip(req, job.skip_reason)
        else:
            _print_result(runner.run(req))
    if not jobs:
        print("due 작업 없음")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="설정 디렉터리 (기본: CONFIG_DIR 환경변수 또는 저장소의 config/)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config", help="설정 YAML 검증 (DB 불필요)").set_defaults(func=cmd_validate)
    p_sync = sub.add_parser("sync-config", help="설정 YAML 을 DB config/리그/스케줄 테이블에 반영")
    p_sync.add_argument("--dry-run", action="store_true", help="반영 후 롤백")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("plugins", help="등록된 수집 플러그인 목록").set_defaults(func=cmd_plugins)
    p_col = sub.add_parser("collect", help="리그 수집 작업 1회 실행")
    p_col.add_argument("--league", required=True)
    p_col.add_argument("--job", required=True,
                       choices=["schedule", "results", "boxscore", "events", "season_stats", "standings",
                                "roster", "players"])
    p_col.add_argument("--param", action="append", metavar="KEY=VALUE",
                       help="작업 파라미터 (date_from=2026-04-01, days_back=7, recheck_days=3 ...)")
    p_col.add_argument("--backfill", action="store_true", help="과거 데이터 적재로 기록")
    p_col.set_defaults(func=cmd_collect)
    p_re = sub.add_parser("reprocess", help="저장된 raw 원문을 현재 파서로 재처리 (소스 재요청 없음)")
    p_re.add_argument("--league", required=True)
    p_re.add_argument("--document-type")
    p_re.add_argument("--since", help="이 시각 이후 수집된 원문만 (ISO 8601)")
    p_re.set_defaults(func=cmd_reprocess)
    p_dis = sub.add_parser("dispatch", help="due 수집 작업을 지금 실행 (Celery 없이)")
    p_dis.add_argument("--dry-run", action="store_true", help="due 목록만 출력")
    p_dis.set_defaults(func=cmd_dispatch)

    args = parser.parse_args(argv)
    args.config_dir = args.config_dir or config_dir_from_env()
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"설정 오류 {len(exc.errors)}건:", file=sys.stderr)
        for e in exc.errors:
            print(f"  - {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
