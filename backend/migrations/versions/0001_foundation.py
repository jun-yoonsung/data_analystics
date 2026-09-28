"""기반: 스키마, 역할, 공통 함수

Revision ID: 0001
Revises: 
"""
from migrations.sqlfile import run_sql_file

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0001_foundation.up.sql")


def downgrade() -> None:
    run_sql_file("0001_foundation.down.sql")
