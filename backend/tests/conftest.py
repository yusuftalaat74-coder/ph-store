"""A17 — creates database rova_test_<pid>, runs alembic upgrade head, runs
`rova seed --config-only`, yields a TestClient. Each test runs inside a
savepoint that is rolled back after, except migration/seed tests which own
the database (module-scoped fixtures below)."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_DB_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
TEST_DB_NAME = f"rova_test_{os.getpid()}"
TEST_DB_URL = f"postgresql+psycopg://postgres:postgres@localhost:5432/{TEST_DB_NAME}"

os.environ.setdefault("ROVA_JWT_SECRET", "test-secret-please-be-32-characters-min")
os.environ["ROVA_DATABASE_URL"] = TEST_DB_URL
os.environ["ROVA_ENV"] = "test"


@pytest.fixture(scope="session", autouse=True)
def _database():
    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    admin_engine.dispose()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env={**os.environ}, check=True,
    )

    from rova.core.db import get_sessionmaker
    from rova.seed.seed import seed_config_only
    session = get_sessionmaker()()
    seed_config_only(session)
    session.commit()
    session.close()

    yield

    admin_engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{TEST_DB_NAME}'"))
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
    admin_engine.dispose()


@pytest.fixture
def db_engine():
    return create_engine(TEST_DB_URL, future=True)


@pytest.fixture
def session(db_engine):
    """Each test gets a fresh Session bound directly to the database; tests
    that need isolation from each other clean up their own rows (most tests
    here use unique deterministic ids per test)."""
    Session = sessionmaker(bind=db_engine, future=True, expire_on_commit=False)
    s = Session()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def app():
    from rova.domain.hooks import wire
    wire()
    from rova.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c
