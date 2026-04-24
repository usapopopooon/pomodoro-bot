"""Fixtures for DB-backed service tests.

The whole module is skipped when Postgres isn't reachable. CI brings up a
``postgres:17-alpine`` service container and runs ``alembic upgrade head``
before pytest — locally you can do the same with ``docker compose up -d db``
and ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.engine import async_session, check_database_connection, engine


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _require_database() -> AsyncGenerator[None, None]:
    if not await check_database_connection(timeout=3.0):
        pytest.skip(
            "database not reachable; start Postgres and run `alembic upgrade head`",
            allow_module_level=True,
        )
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a session and truncate pomodoro tables after the test.

    Services commit inside their own transactions, so wrapping in a rolled-back
    outer transaction doesn't isolate them. Truncating after each test keeps
    things simple and fast for a small schema.
    """
    async with async_session() as session:
        yield session

    async with async_session() as cleanup:
        await cleanup.execute(
            text(
                "TRUNCATE pomodoro_rooms, room_participants, "
                "pomodoros, room_events RESTART IDENTITY CASCADE"
            )
        )
        await cleanup.commit()
