"""인증 + 분석가 입력 (이력, RLS)

Revision ID: 0006
Revises: 0005
"""
from migrations.sqlfile import run_sql_file

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0006_auth_analyst.up.sql")


def downgrade() -> None:
    run_sql_file("0006_auth_analyst.down.sql")
