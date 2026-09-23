"""Alembic environment for ROVA. The DDL in migrations/sql/0001_initial.sql is
authoritative and hand-written (A1, A3.1); Alembic here is purely the runner
that applies it forward-only (A2.14, B1.6)."""
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

db_url = os.environ.get("ROVA_DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
