"""Alembic 실행 환경.

마이그레이션 본문은 migrations/sql/*.sql 에 순수 SQL 로 두고,
versions/*.py 는 해당 SQL 파일을 실행하는 얇은 래퍼만 가진다.
"""
from alembic import context
from sqlalchemy import create_engine, pool

from app.settings import get_database_url

config = context.config


def run_migrations_offline() -> None:
    context.configure(url=get_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # 마이그레이션 하나를 하나의 트랜잭션으로 실행 (실패 시 해당 단계 전체 롤백)
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
