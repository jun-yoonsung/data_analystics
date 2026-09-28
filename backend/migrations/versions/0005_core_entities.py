"""공통 엔티티 + 자동 수집 기록

Revision ID: 0005
Revises: 0004
"""
from migrations.sqlfile import run_sql_file

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0005_core_entities.up.sql")


def downgrade() -> None:
    run_sql_file("0005_core_entities.down.sql")
