"""환경 설정."""
import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/sports"


def get_database_url() -> str:
    """DATABASE_URL 환경변수 (SQLAlchemy URL, 드라이버는 psycopg3)."""
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    # postgres:// 또는 postgresql:// 로 주어지면 psycopg3 드라이버로 맞춘다
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url
