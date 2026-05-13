from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.core.phase import PhasePlan
from src.database.engine import async_session, dispose_engine
from src.room_manager import RoomManager
from src.services import room_service as svc
from src.ui.embeds import control_panel_embed
from src.ui.panel_views import ControlPanelView
from src.voice_manager import VoiceManager

logger = logging.getLogger(__name__)

ORPHAN_PANEL_NOTICE = (
    "このポモドーロは Bot の再起動で終了しました。`/pomo` で作り直してください。"
)


def _build_default_plan() -> PhasePlan:
    return PhasePlan(
        work_seconds=settings.pomo_work_seconds,
        short_break_seconds=settings.pomo_short_break_seconds,
        long_break_seconds=settings.pomo_long_break_seconds,
        long_break_every=settings.pomo_long_break_every,
    )


class PomodoroBot(commands.Bot):
    """Single-command Discord bot.

    Only ``/pomo`` is exposed as a slash command; it posts a room panel in the
    channel and every other interaction (join / leave / task / pause / …)
    happens through the panel's buttons. With just one command there's no
    need for a cog or an ``app_commands.Group`` — the handler lives directly
    on this class and is registered against the tree in ``setup_hook``.
    """

    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Slash commands + button interactions don't need message_content;
        # keep intents minimal.
        super().__init__(
            command_prefix="!pomodoro-unused!",
            intents=intents,
            activity=discord.Game(name="タイマーを回しています"),
        )
        # One VoiceManager per bot instance. Multi-bot deploys end up with
        # one connection slot per guild *per bot*, which is the whole point
        # of running multiple identities in the first place.
        self.voice_manager: VoiceManager = VoiceManager()
        self.room_manager: RoomManager = RoomManager(
            default_plan=_build_default_plan(),
            refresh_seconds=settings.pomo_refresh_minutes * 60,
            voice_manager=self.voice_manager,
        )

    async def setup_hook(self) -> None:
        # DB-side reconciliation first: close orphan rooms so the channel
        # uniqueness index is free for fresh panels. Scoped to this bot's
        # identity so multi-bot deployments don't trample each other.
        orphans = await self._reconcile_orphaned_rooms(self._self_user_id())

        self.tree.add_command(
            discord.app_commands.Command(
                name="pomo",
                description=(
                    "このチャンネルにポモドーロのコントロールパネルを出します"
                ),
                callback=self._cmd_pomo,
            )
        )

        if settings.discord_guild_ids:
            for gid in settings.discord_guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                logger.info("synced commands to guild %d", gid)
        else:
            await self.tree.sync()
            logger.info("synced commands globally")

        # Discord-side cleanup has to wait for the gateway to be ready —
        # ``fetch_*`` works without cache but still needs a live connection.
        # Spawn it as a detached task so ``setup_hook`` can return promptly.
        if orphans:
            asyncio.create_task(
                self._strip_orphan_panels(orphans),
                name="pomo-strip-orphan-panels",
            )

    async def on_ready(self) -> None:
        logger.info("bot ready as %s (guilds=%d)", self.user, len(self.guilds))

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """End the room when the bot is the last one left in its VC.

        Without this, the timer keeps ticking and audio cues fire into an
        empty channel after the only human disconnects — wasted work and
        a stranded bot. We watch for non-bot members leaving the channel
        the bot is currently sitting in, and tear the room down as soon
        as the human headcount hits zero.
        """
        if member.bot:
            return
        guild = member.guild
        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            return
        bot_channel = voice_client.channel
        if bot_channel is None:
            return
        # Only act when the member left (or was moved out of) the bot's VC.
        # Mute/deafen toggles fire this event too but keep the same channel.
        was_in_bot_channel = (
            before.channel is not None and before.channel.id == bot_channel.id
        )
        still_in_bot_channel = (
            after.channel is not None and after.channel.id == bot_channel.id
        )
        if not was_in_bot_channel or still_in_bot_channel:
            return
        if any(not m.bot for m in bot_channel.members):
            return
        ended_state = await self.room_manager.end_voice_room_if_any(
            guild.id, reason="voice_empty"
        )
        if ended_state is None:
            # No room claims this VC (orphan connection) — still drop it so
            # the bot doesn't loiter.
            await self.voice_manager.disconnect(guild.id)
            return
        await self._announce_voice_empty_shutdown(ended_state)

    async def _announce_voice_empty_shutdown(self, state: object) -> None:
        """Post a one-liner to the room's text channel explaining the auto-end.

        The control panel embed is also rewritten with the same reason, but
        that's an in-place edit users can miss if they've scrolled away.
        A fresh channel message bumps the channel and makes the cause
        unambiguous.
        """
        panel = getattr(state, "message", None)
        channel = getattr(panel, "channel", None)
        if channel is None:
            return
        try:
            await channel.send(
                "🔇 VC に誰もいなくなったので、ポモドーロを自動的に終了しました。"
            )
        except discord.HTTPException:
            logger.warning(
                "voice-empty notice send failed room_id=%s",
                getattr(state, "room_id", None),
            )

    async def close(self) -> None:
        active = len(self.room_manager.active_rooms())
        logger.info("shutting down: closing %d live rooms", active)
        await self.room_manager.end_all(reason="shutdown")
        # ``end_all`` already drops VC connections for each room as it
        # closes them, but a stray idle connection (no live room) is still
        # possible if e.g. ``toggle_voice`` succeeded but ``/pomo`` was
        # already gone. Belt-and-braces.
        await self.voice_manager.disconnect_all()
        await super().close()
        await dispose_engine()

    # ------------------------------------------------------------------
    # /pomo — the only slash command
    # ------------------------------------------------------------------

    async def _cmd_pomo(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        # ``interaction.channel`` can be a ``CategoryChannel`` etc. which
        # isn't messageable; reject anything we can't post into.
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                "このチャンネルではパネルを作成できません。", ephemeral=True
            )
            return

        # Defer FIRST. Discord invalidates the interaction token after ~3s;
        # the DB query below can take longer than that on a cold Railway
        # connection, which used to raise "Unknown interaction" (10062)
        # when defer() was called afterwards.
        await interaction.response.defer(thinking=True)

        # ``/pomo`` should feel idempotent to a first-time user: run it,
        # get a panel, every time. If a previous room is still active in
        # this channel, close it quietly and put a fresh panel in its
        # place. The old panel message is automatically edited into its
        # "ended" embed by ``RoomManager.end``.
        async with async_session() as db:
            existing = await svc.get_active_room_in_channel(db, channel.id)
        if existing is not None:
            await self.room_manager.end(existing.id, reason="superseded")
            # Safety net for the rare case where memory and DB disagree
            # (e.g. a previous partial failure). ``end_room`` is a no-op
            # once the row is already closed.
            async with async_session() as db:
                await svc.end_room(db, existing.id, reason="superseded")

        try:
            state = await self.room_manager.create_setup(
                guild_id=interaction.guild.id if interaction.guild else None,
                channel_id=channel.id,
                created_by=interaction.user.id,
                bot_user_id=self._self_user_id(),
            )
        except IntegrityError:
            # True concurrent race: another user hit /pomo in this same
            # channel at the same instant. Their panel wins, ours aborts.
            logger.info("concurrent panel creation lost race channel=%s", channel.id)
            await interaction.followup.send(
                "同時に他のメンバーがパネルを作成しました。"
                "そちらのパネルをお使いください。",
                ephemeral=True,
            )
            return

        # The Control Panel lives as the persistent anchor message; phase
        # announcements post separately once the owner hits Start. Note
        # we pass ``has_started=False`` so the Start button is enabled.
        view = ControlPanelView(self.room_manager, state.room_id, has_started=False)
        message = await interaction.followup.send(
            embed=control_panel_embed(state), view=view, wait=True
        )
        await self.room_manager.attach_message(state.room_id, message)
        # Auto-join the creator so they're the first participant — this is
        # UX-friendly and also guarantees a valid owner when phases start.
        await self.room_manager.join(state.room_id, interaction.user.id)

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    def _self_user_id(self) -> int | None:
        """Return ``self.user.id`` if the gateway login already resolved it.

        Returns ``None`` only in a narrow window during cold start before
        ``setup_hook`` (or in tests with no real login). Callers tolerate
        ``None`` — single-bot deploys never see it, and a missing ID just
        means reconciliation falls back to the unscoped sweep.
        """
        return self.user.id if self.user is not None else None

    async def _reconcile_orphaned_rooms(
        self, bot_user_id: int | None
    ) -> list[svc.OrphanRoom]:
        """Close any rooms this bot left active in a previous run.

        Timer state lives in memory so we can't resume; mark rooms as
        ``bot_restart`` and return their channel / message ids so the caller
        can strip the dead panels' buttons. Without that step, clicking a
        button on an old panel shows "Interaction failed" with no context.

        Scoped to ``bot_user_id`` when known: a multi-bot deploy must not
        sweep peers' rooms.
        """
        async with async_session() as db:
            orphans = await svc.mark_all_active_rooms_ended(
                db, reason="bot_restart", bot_user_id=bot_user_id
            )
        if orphans:
            logger.info("closed %d orphaned room(s) from previous run", len(orphans))
        return orphans

    async def _strip_orphan_panels(self, orphans: list[svc.OrphanRoom]) -> None:
        """Edit each orphan's panel + phase message to halt live updates.

        Two things to clean up per orphan: the Control Panel (replaced with
        the orphan notice + view stripped) and the live phase-progress
        message (its ``<t:UNIX:R>`` line frozen, view stripped — otherwise
        Discord keeps re-rendering it as "X 分前" forever even though
        the timer is dead).

        Best-effort: if the channel or any single message is gone, log
        and move on. There's nothing actionable left anyway.
        """
        await self.wait_until_ready()
        from src.ui.embeds import freeze_phase_content

        stripped_panels = 0
        frozen_phases = 0
        for orphan in orphans:
            if orphan.message_id is None and orphan.phase_message_id is None:
                continue
            try:
                channel = await self.fetch_channel(orphan.channel_id)
            except (discord.NotFound, discord.Forbidden):
                continue
            if not isinstance(channel, discord.abc.Messageable):
                continue
            if orphan.message_id is not None:
                try:
                    message = await channel.fetch_message(orphan.message_id)
                    await message.edit(
                        content=ORPHAN_PANEL_NOTICE, embed=None, view=None
                    )
                    stripped_panels += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    logger.warning(
                        "failed to strip orphan panel channel=%s message=%s",
                        orphan.channel_id,
                        orphan.message_id,
                    )
            if orphan.phase_message_id is not None:
                try:
                    phase_msg = await channel.fetch_message(orphan.phase_message_id)
                    await phase_msg.edit(
                        content=freeze_phase_content(phase_msg.content), view=None
                    )
                    frozen_phases += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    logger.warning(
                        "failed to freeze orphan phase message channel=%s message=%s",
                        orphan.channel_id,
                        orphan.phase_message_id,
                    )
        if stripped_panels or frozen_phases:
            logger.info(
                "orphan cleanup: stripped %d panel(s), froze %d phase message(s)",
                stripped_panels,
                frozen_phases,
            )
