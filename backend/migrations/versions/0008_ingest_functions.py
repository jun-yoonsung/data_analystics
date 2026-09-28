"""수집 워커용 함수 권한 (이벤트 파티션 생성 위임, 리그 락 키)

Revision ID: 0008
Revises: 0007
"""
from migrations.sqlfile import run_sql_file

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file("0008_ingest_functions.up.sql")


def downgrade() -> None:
    run_sql_file("0008_ingest_functions.down.sql")
