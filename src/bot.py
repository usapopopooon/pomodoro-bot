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
from src.ui.embeds import room_embed
from src.ui.room_panel import RoomPanelView

logger = logging.getLogger(__name__)

ORPHAN_PANEL_NOTICE = (
    "🍅 このポモドーロは Bot の再起動で終了しました。`/pomo` で作り直してください。"
)

ROOM_ALREADY_ACTIVE_MESSAGE = (
    "このチャンネルにはすでにアクティブなポモドーロがあります。"
    "終了してから新しいパネルを作成してください。"
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
            activity=discord.Game(name="🍅 /pomo"),
        )
        self.room_manager: RoomManager = RoomManager(
            default_plan=_build_default_plan(),
            tick_seconds=settings.pomo_tick_seconds,
        )

    async def setup_hook(self) -> None:
        # DB-side reconciliation first: close orphan rooms so the channel
        # uniqueness index is free for fresh panels.
        orphans = await self._reconcile_orphaned_rooms()

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

        async with async_session() as session:
            existing = await svc.get_active_room_in_channel(session, channel.id)
        if existing is not None:
            await interaction.response.send_message(
                ROOM_ALREADY_ACTIVE_MESSAGE, ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        # The pre-check above is best-effort: two users firing ``/pomo``
        # simultaneously can both pass it, and the second ``create_room``
        # hits the partial-unique index on ``channel_id``. Catch that as a
        # friendly ephemeral message instead of an unhandled traceback.
        try:
            state = await self.room_manager.create_and_start(
                guild_id=interaction.guild.id if interaction.guild else None,
                channel_id=channel.id,
                created_by=interaction.user.id,
                channel=channel,
            )
        except IntegrityError:
            logger.info("concurrent panel creation lost race channel=%s", channel.id)
            await interaction.followup.send(ROOM_ALREADY_ACTIVE_MESSAGE, ephemeral=True)
            return

        view = RoomPanelView(self.room_manager, state.room_id)
        message = await interaction.followup.send(
            embed=room_embed(state), view=view, wait=True
        )
        await self.room_manager.attach_message(state.room_id, message)
        # Auto-join the creator so they're the first participant.
        await self.room_manager.join(state.room_id, interaction.user.id)

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def _reconcile_orphaned_rooms(self) -> list[svc.OrphanRoom]:
        """Close any rooms left active by a previous process.

        Timer state lives in memory so we can't resume; mark rooms as
        ``bot_restart`` and return their channel / message ids so the caller
        can strip the dead panels' buttons. Without that step, clicking a
        button on an old panel shows "Interaction failed" with no context.
        """
        async with async_session() as db:
            orphans = await svc.mark_all_active_rooms_ended(db, reason="bot_restart")
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
