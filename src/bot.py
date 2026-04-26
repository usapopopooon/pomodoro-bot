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
            activity=discord.Game(name="/pomo"),
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
        """Edit each orphan panel message to remove its view.

        Best-effort: if the channel or message is gone, just move on — there's
        nothing actionable left anyway.
        """
        await self.wait_until_ready()
        stripped = 0
        for orphan in orphans:
            if orphan.message_id is None:
                continue
            try:
                channel = await self.fetch_channel(orphan.channel_id)
            except (discord.NotFound, discord.Forbidden):
                continue
            if not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                message = await channel.fetch_message(orphan.message_id)
                await message.edit(content=ORPHAN_PANEL_NOTICE, embed=None, view=None)
                stripped += 1
            except (discord.NotFound, discord.Forbidden):
                continue
            except discord.HTTPException:
                logger.warning(
                    "failed to strip orphan panel channel=%s message=%s",
                    orphan.channel_id,
                    orphan.message_id,
                )
        if stripped:
            logger.info("stripped %d orphan panel(s)", stripped)
