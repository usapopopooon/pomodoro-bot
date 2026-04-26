"""Per-bot voice connection + clip playback.

Discord allows a bot **at most one voice connection per guild**. Multi-room
parallel rooms in the same guild therefore need separate bot identities —
this is one of the reasons :mod:`src.config` accepts a CSV of tokens. Within
a single bot, this manager keeps the connection state keyed by guild id and
serialises plays so a fresh clip can't talk over a still-playing one.

The path layer is intentionally thin: callers pass a clip name (``"start"``
/ ``"end-task"`` / etc.) and we resolve it against ``constants.VOICES_DIR``.
Missing files are logged and skipped — voice is best-effort, never blocking
on the timer's hot path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import discord

from src.constants import VOICES_DIR

logger = logging.getLogger(__name__)


class VoiceManager:
    """Owns voice connections for one :class:`PomodoroBot` instance.

    State is in-memory only; on bot restart Discord drops the voice session
    and we'd reconnect on the next 🔊 button press. Per-guild locks keep
    overlapping play / disconnect calls from racing each other (e.g., a
    one-minute-left cue arriving while the user is toggling 🔊 off).
    """

    def __init__(self, voices_dir: Path = VOICES_DIR) -> None:
        self._voices_dir = voices_dir
        self._connections: dict[int, discord.VoiceClient] = {}
        # One asyncio.Lock per guild — accessed only from the manager so we
        # don't have to serialise across guilds.
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def is_connected(self, guild_id: int) -> bool:
        client = self._connections.get(guild_id)
        return client is not None and client.is_connected()

    async def connect(self, voice_channel: discord.VoiceChannel) -> bool:
        """Join ``voice_channel``; reuse an existing connection if any.

        Returns False when Discord refuses the join (network blip / missing
        ``Connect`` permission) so the caller can surface a clean error
        without crashing the room.
        """
        guild_id = voice_channel.guild.id
        lock = self._lock_for(guild_id)
        async with lock:
            existing = self._connections.get(guild_id)
            if existing is not None and existing.is_connected():
                if existing.channel.id != voice_channel.id:
                    # Owner moved channels — follow them rather than playing
                    # cues into an empty room.
                    try:
                        await existing.move_to(voice_channel)
                    except discord.HTTPException:
                        logger.warning(
                            "voice.move failed guild=%s ch=%s",
                            guild_id,
                            voice_channel.id,
                        )
                        return False
                return True
            try:
                # ``timeout`` shortens discord.py's default 60s budget so
                # a bad transient connect surfaces to the user in ~15s
                # instead of a minute of silent waiting. ``reconnect`` is
                # left at its default (True) so transient drops mid-
                # session auto-recover.
                client: discord.VoiceClient = await voice_channel.connect(
                    self_deaf=True, timeout=15.0
                )
            except discord.ClientException:
                # Already connected to a different voice in this guild; the
                # client's own state was stale. Reset and bail — caller can
                # retry.
                logger.warning(
                    "voice.connect duplicate guild=%s ch=%s",
                    guild_id,
                    voice_channel.id,
                )
                await self._kill_leftover_voice_client(voice_channel.guild)
                return False
            except (discord.HTTPException, TimeoutError) as e:
                logger.warning(
                    "voice.connect failed guild=%s ch=%s err=%r",
                    guild_id,
                    voice_channel.id,
                    e,
                )
                # discord.py leaves a partial ``VoiceClient`` on the guild
                # whose background runner keeps retrying the handshake
                # forever. Force-disconnect it so the loop actually exits
                # after we surface the error to the user.
                await self._kill_leftover_voice_client(voice_channel.guild)
                return False
            self._connections[guild_id] = client
            return True

    async def _kill_leftover_voice_client(self, guild: discord.Guild) -> None:
        """Force-disconnect any zombie VoiceClient on ``guild``.

        Called from the failure paths in :meth:`connect` to neutralise the
        runaway reconnect loop that discord.py would otherwise spin up in
        the background after our exception bubbled out.
        """
        leftover = guild.voice_client
        if leftover is None:
            return
        with contextlib.suppress(Exception):
            await leftover.disconnect(force=True)

    async def disconnect(self, guild_id: int) -> None:
        """Leave the voice channel for ``guild_id`` if connected.

        Safe to call multiple times — extra calls are no-ops.
        """
        lock = self._lock_for(guild_id)
        async with lock:
            client = self._connections.pop(guild_id, None)
            if client is None:
                return
            try:
                await client.disconnect(force=False)
            except discord.HTTPException:
                logger.debug("voice.disconnect failed guild=%s", guild_id)

    async def disconnect_all(self) -> None:
        """Disconnect every active voice connection — used at shutdown."""
        for guild_id in list(self._connections):
            await self.disconnect(guild_id)

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    async def play_clip(self, guild_id: int, clip_name: str) -> bool:
        """Play ``voices/<clip_name>.wav`` if connected; return success.

        Waits for the clip to finish so callers can chain them naturally
        (e.g. ``end-task`` → ``start-break``). If a clip is already playing
        we stop it first — the newer cue is more relevant than the older.
        """
        client = self._connections.get(guild_id)
        if client is None or not client.is_connected():
            return False

        path = self._voices_dir / f"{clip_name}.wav"
        if not path.is_file():
            logger.warning("voice.clip missing path=%s", path)
            return False

        lock = self._lock_for(guild_id)
        async with lock:
            # Re-check inside the lock — disconnect could have raced.
            client = self._connections.get(guild_id)
            if client is None or not client.is_connected():
                return False
            if client.is_playing():
                client.stop()

            done = asyncio.Event()
            loop = asyncio.get_running_loop()

            def _after(error: Exception | None) -> None:
                # ``after`` runs on the player thread; bounce to our loop
                # so we set the event on the thread that's awaiting it.
                if error is not None:
                    logger.warning(
                        "voice.play errored guild=%s clip=%s err=%s",
                        guild_id,
                        clip_name,
                        error,
                    )
                loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegPCMAudio(str(path))
            try:
                client.play(source, after=_after)
            except discord.ClientException:
                logger.warning(
                    "voice.play rejected guild=%s clip=%s", guild_id, clip_name
                )
                return False

            await done.wait()
            return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[guild_id] = lock
        return lock


__all__ = ["VoiceManager"]
