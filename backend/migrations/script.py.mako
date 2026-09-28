"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from migrations.sqlfile import run_sql_file

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("${up_revision}_CHANGE_ME.up.sql")


def downgrade() -> None:
    run_sql_file("${up_revision}_CHANGE_ME.down.sql")
