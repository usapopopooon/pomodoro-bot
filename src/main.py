from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from src.bot import PomodoroBot
from src.config import settings
from src.database.engine import check_database_connection_with_retry

logger = logging.getLogger(__name__)

_bot: PomodoroBot | None = None


def _setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", settings.log_level).upper()
    level = getattr(logging, level_name, None) or logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        logger.info("stop signal received")
        if _bot is not None:
            asyncio.create_task(_bot.close(), name="bot-shutdown")

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows / restricted envs fall back to default handling.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)
    # Ignoring SIGHUP keeps restarts on Railway from killing the loop mid-tick.
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


async def _amain() -> None:
    global _bot
    _setup_logging()

    if not await check_database_connection_with_retry():
        logger.error("database unreachable; aborting startup")
        raise SystemExit(1)

    _bot = PomodoroBot()
    _install_signal_handlers()
    try:
        await _bot.start(settings.discord_token)
    finally:
        if not _bot.is_closed():
            await _bot.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
