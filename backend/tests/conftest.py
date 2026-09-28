"""공용 테스트 픽스처.

db_url: 모듈마다 임시 DB 를 만들어 마이그레이션 + 설정 동기화까지 끝낸 URL.
TEST_DATABASE_URL (관리자 권한 접속 URL) 이 없으면 DB 테스트는 건너뛴다.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.settings import get_database_url
from config_sync.loader import DEFAULT_CONFIG_DIR, load_config
from config_sync.sync import sync_bundle
from config_sync.validate import validate_bundle

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
BACKEND_DIR = Path(__file__).resolve().parents[1]


def _normalize(url: str) -> str:
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        return get_database_url()
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL")
        else:
            os.environ["DATABASE_URL"] = old


@pytest.fixture(scope="module")
def db_url():
    if not ADMIN_URL:
        pytest.skip("TEST_DATABASE_URL 미설정")
    admin = create_engine(_normalize(ADMIN_URL), isolation_level="AUTOCOMMIT")
    name = f"sports_test_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(_normalize(ADMIN_URL)).set(database=name).render_as_string(hide_password=False)

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    with engine.begin() as conn:
        bundle = load_config(DEFAULT_CONFIG_DIR)
        validate_bundle(bundle)
        sync_bundle(conn, bundle)
    engine.dispose()

    yield url

    if old is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
