from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config import settings

logger = logging.getLogger(__name__)

POOL_SIZE = 5
MAX_OVERFLOW = 5


engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
)

async_session: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def check_database_connection(timeout: float = 10.0) -> bool:
    """Return True when the database answers ``SELECT 1`` within ``timeout``."""

    async def _probe() -> bool:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    try:
        return await asyncio.wait_for(_probe(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        logger.warning("database connection probe failed: %s", exc)
        return False


async def check_database_connection_with_retry(
    retries: int = 3, delay: float = 2.0
) -> bool:
    for attempt in range(1, retries + 1):
        if await check_database_connection():
            return True
        logger.warning("database unreachable (attempt %d/%d)", attempt, retries)
        if attempt < retries:
            await asyncio.sleep(delay)
    return False


async def dispose_engine() -> None:
    await engine.dispose()
