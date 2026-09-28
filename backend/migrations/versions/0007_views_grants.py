"""롱포맷 MV + 권한

Revision ID: 0007
Revises: 0006
"""
from migrations.sqlfile import run_sql_file

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0007_views_grants.up.sql")


def downgrade() -> None:
    run_sql_file("0007_views_grants.down.sql")
