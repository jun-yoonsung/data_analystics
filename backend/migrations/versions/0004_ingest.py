"""수집 관리 (스케줄, 로그, raw, ID 매핑)

Revision ID: 0004
Revises: 0003
"""
from migrations.sqlfile import run_sql_file

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0004_ingest.up.sql")


def downgrade() -> None:
    run_sql_file("0004_ingest.down.sql")
