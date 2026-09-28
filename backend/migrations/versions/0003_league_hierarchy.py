"""데이터 소스 + 리그 계층 + 리그 상수

Revision ID: 0003
Revises: 0002
"""
from migrations.sqlfile import run_sql_file

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0003_league_hierarchy.up.sql")


def downgrade() -> None:
    run_sql_file("0003_league_hierarchy.down.sql")
