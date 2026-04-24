from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.database.models import Base

# NOTE: we intentionally read DATABASE_URL directly from the environment
# here instead of going through ``src.config.settings``. The Settings object
# validates DISCORD_TOKEN at construction time, which would force anyone
# running ``alembic upgrade head`` (including the Railway boot script) to
# also set DISCORD_TOKEN — but migrations have nothing to do with Discord.

DEFAULT_DATABASE_URL = "postgresql://pomodoro:pomodoro@localhost:5432/pomodoro"


def _sync_database_url() -> str:
    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    # Heroku / Railway hand out ``postgres://`` historically.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # The runtime uses asyncpg; alembic runs on a sync driver (psycopg2) so
    # strip the async suffix if it's there.
    return url.replace("postgresql+asyncpg://", "postgresql://")


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = _sync_database_url()
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
