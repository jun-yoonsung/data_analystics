"""운영 CLI.

사용 예
  python -m app.cli validate-config                 # DB 없이 설정만 검증
  python -m app.cli sync-config --dry-run           # DB 반영 결과 미리보기 (롤백)
  python -m app.cli sync-config                     # DB 반영
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("--config-dir", type=Path, default=None,
                        help="설정 디렉터리 (기본: CONFIG_DIR 환경변수 또는 저장소의 config/)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config", help="설정 YAML 검증 (DB 불필요)").set_defaults(func=cmd_validate)
    p_sync = sub.add_parser("sync-config", help="설정 YAML 을 DB config/리그/스케줄 테이블에 반영")
    p_sync.add_argument("--dry-run", action="store_true", help="반영 후 롤백")
    p_sync.set_defaults(func=cmd_sync)

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
