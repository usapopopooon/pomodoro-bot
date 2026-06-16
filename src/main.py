from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from discord.errors import LoginFailure

from src.bot import PomodoroBot
from src.config import settings
from src.database.engine import check_database_connection_with_retry

logger = logging.getLogger(__name__)

_bots: list[PomodoroBot] = []


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
        logger.info("stop signal received; closing %d bot(s)", len(_bots))
        for bot in _bots:
            if not bot.is_closed():
                asyncio.create_task(bot.close(), name=f"bot-shutdown-{id(bot)}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows / restricted envs fall back to default handling.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)
    # Ignoring SIGHUP keeps restarts on Railway from killing the loop mid-tick.
    with contextlib.suppress(AttributeError, ValueError):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


async def _run_bot(token: str, index: int) -> None:
    """Run one bot end-to-end; ensure ``close()`` even on exceptions."""
    bot = PomodoroBot()
    _bots.append(bot)
    try:
        await bot.start(token)
    except LoginFailure:
        logger.error(
            "bot instance %d failed Discord login: DISCORD_TOKEN is invalid. "
            "Set the raw Bot token from Discord Developer Portal; do not use "
            "the application ID, public key, client secret, or a value prefixed "
            "with 'Bot '.",
            index,
        )
        raise
    except Exception:
        logger.exception("bot instance %d crashed during startup/runtime", index)
        raise
    finally:
        if not bot.is_closed():
            await bot.close()


async def _amain() -> None:
    _setup_logging()

    if not await check_database_connection_with_retry():
        logger.error("database unreachable; aborting startup")
        raise SystemExit(1)

    _install_signal_handlers()

    tokens = settings.discord_tokens
    logger.info("starting %d bot instance(s)", len(tokens))

    # Run every bot concurrently. If one instance exits unexpectedly, let the
    # process fail so the platform shows the real traceback instead of a quiet
    # restart loop.
    await asyncio.gather(*(_run_bot(t, i) for i, t in enumerate(tokens, start=1)))


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("interrupted")


if __name__ == "__main__":
    main()
