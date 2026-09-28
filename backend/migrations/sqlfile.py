"""SQL 파일 실행 헬퍼."""
from pathlib import Path

from alembic import op

SQL_DIR = Path(__file__).parent / "sql"


def run_sql_file(name: str) -> None:
    """migrations/sql/<name> 을 현재 마이그레이션 트랜잭션에서 실행한다.

    SQLAlchemy text() 는 ':name' 을, 파라미터를 넘긴 드라이버 호출은 '%' 를 바인드 자리로
    해석하므로, 파라미터 없이 DBAPI 커서로 직접 실행한다 (psycopg3 는 이 경우 다중 구문 허용).
    """
    sql = (SQL_DIR / name).read_text(encoding="utf-8")
    if op.get_context().as_sql:
        # 오프라인 모드(--sql): SQL 을 그대로 출력
        op.execute(sql)
        return
    cursor = op.get_bind().connection.dbapi_connection.cursor()
    try:
        cursor.execute(sql)
    finally:
        cursor.close()
