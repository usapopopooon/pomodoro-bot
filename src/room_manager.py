from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import StrEnum
from uuid import UUID

import discord

from src.core.phase import Phase, PhasePlan
from src.core.room_state import ParticipantState, RoomState
from src.database.engine import async_session
from src.services import room_service as svc
from src.voice_manager import VoiceManager

logger = logging.getLogger(__name__)


# Voice-clip mappings — module-level so they're trivially overridable in
# tests and don't pull state into the manager. ``one-minute-left`` /
# ``alarm`` / ``pause`` / ``resume`` / ``connected`` are referenced directly
# at the call sites since they don't depend on phase / reason.
_START_CLIP: dict[Phase, str] = {
    Phase.WORK: "start-task",
    Phase.SHORT_BREAK: "start-break",
    Phase.LONG_BREAK: "start-long-break",
}
_END_CLIP: dict[Phase, str] = {
    Phase.WORK: "end-task",
    Phase.SHORT_BREAK: "end-break",
    Phase.LONG_BREAK: "end-long-break",
}
# Only user-actioned ends get an audible "this is the end" cue. Background
# closures (superseded / bot_restart / shutdown / error) skip the cue —
# nobody is around to hear it and we'd be racing the disconnect anyway.
_END_REASON_CUE: dict[str, str] = {
    "owner_ended": "end",
    "auto_empty": "auto-end",
}


class OpResult(StrEnum):
    """Why a button action was accepted / rejected.

    Views turn these into ephemeral user feedback without having to know
    anything about the manager's internals.
    """

    OK = "ok"
    NOT_A_PARTICIPANT = "not_a_participant"
    NOT_OWNER = "not_owner"
    ALREADY_JOINED = "already_joined"
    ALREADY_STARTED = "already_started"
    NOT_YET_STARTED = "not_yet_started"
    ROOM_NOT_FOUND = "room_not_found"
    ANOTHER_ROOM = "another_room"
    OWNER_NOT_IN_VOICE = "owner_not_in_voice"
    NO_GUILD_CONTEXT = "no_guild_context"
    VOICE_UNAVAILABLE = "voice_unavailable"


class RoomManager:
    """Owns live rooms, keyed by ``room_id``.

    Two phases per room life:
      * **Setup** — ``/pomo`` posts a Control Panel. ``has_started`` is
        False; the phase loop hasn't been kicked off. Participants can
        still join, owner can edit the plan, and no timer is ticking.
      * **Running** — owner presses Start on the Control Panel, which
        calls :meth:`begin_phases`. The phase loop starts and posts a
        phase-transition message (with a PNG timer image) every time a
        phase flips.

    The phase loop sleeps until the current phase naturally ends, and is
    woken early via ``state.wake_event`` whenever the user pauses, skips,
    resets, or updates the plan. No 10-second ticking.
    """

    def __init__(
        self,
        *,
        default_plan: PhasePlan,
        refresh_seconds: int = 60,
        voice_manager: VoiceManager | None = None,
    ) -> None:
        self._rooms: dict[UUID, RoomState] = {}
        self._default_plan = default_plan
        # How long to sleep between phase-message refreshes. Defaults to
        # one minute — matches the minute-granular "N分 / M分" clock in
        # the ASCII bar, so text and bar advance in step.
        self._refresh_seconds = max(1, refresh_seconds)
        self._registry_lock = asyncio.Lock()
        # Voice playback is optional — tests / DM-only flows pass ``None``
        # and every voice call short-circuits to a no-op.
        self._voice = voice_manager

    @property
    def default_plan(self) -> PhasePlan:
        return self._default_plan

    @property
    def voice(self) -> VoiceManager | None:
        return self._voice

    def get(self, room_id: UUID) -> RoomState | None:
        return self._rooms.get(room_id)

    def active_rooms(self) -> list[RoomState]:
        return list(self._rooms.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_setup(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        created_by: int,
        plan: PhasePlan | None = None,
        bot_user_id: int | None = None,
    ) -> RoomState:
        """Create a room in **setup** state.

        No phase loop is started; the caller is expected to post a Control
        Panel and later call :meth:`begin_phases` when the owner is ready.

        ``bot_user_id`` is stamped on the row so multi-bot deploys can scope
        startup reconciliation to their own rooms.
        """
        plan = plan or self._default_plan
        async with async_session() as db:
            row = await svc.create_room(
                db,
                guild_id=guild_id,
                channel_id=channel_id,
                created_by=created_by,
                work_seconds=plan.work_seconds,
                short_break_seconds=plan.short_break_seconds,
                long_break_seconds=plan.long_break_seconds,
                long_break_every=plan.long_break_every,
                bot_user_id=bot_user_id,
            )
            await svc.record_event(
                db,
                room_id=row.id,
                event_type="room_created",
                payload={"created_by": created_by},
            )

        state = RoomState(
            room_id=row.id,
            guild_id=guild_id,
            channel_id=channel_id,
            created_by=created_by,
            plan=plan,
        )
        async with self._registry_lock:
            self._rooms[row.id] = state

        logger.info(
            "room.created (setup) room_id=%s channel=%s by=%s",
            row.id,
            channel_id,
            created_by,
        )
        return state

    async def begin_phases(self, room_id: UUID, user_id: int) -> OpResult:
        """Flip the room from setup to running and start the phase loop."""
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        if state.has_started:
            return OpResult.ALREADY_STARTED
        if state.message is None:
            # No Control Panel to anchor the channel on — shouldn't happen
            # in normal flow but guard anyway.
            return OpResult.ROOM_NOT_FOUND

        async with state.lock:
            state.has_started = True
            state.phase = Phase.WORK
            state.completed_work_phases = 0
            state.reset_current_phase()
            async with async_session() as db:
                await svc.record_event(db, room_id=room_id, event_type="timer_started")
            # Re-render the Control Panel so the Start button flips to
            # "開始中" (disabled) and the embed status line changes.
            await self._render_control_panel(state)

        state.task_handle = asyncio.create_task(
            self._run_phase_loop(state),
            name=f"pomo-phases-{room_id}",
        )
        return OpResult.OK

    async def attach_message(self, room_id: UUID, message: discord.Message) -> None:
        state = self._rooms.get(room_id)
        if state is None:
            return
        state.message = message
        async with async_session() as db:
            await svc.set_room_message(db, room_id, message.id)

    async def end(self, room_id: UUID, *, reason: str) -> RoomState | None:
        async with self._registry_lock:
            state = self._rooms.pop(room_id, None)
        if state is None:
            return None
        if state.task_handle is not None and not state.task_handle.done():
            state.task_handle.cancel()

        # Voice cue first (so listeners hear "終了" before silence), then
        # drop the connection. Background closures (superseded /
        # bot_restart / shutdown / error) skip the cue — there's nobody
        # waiting on the audio cue and we want the disconnect to be
        # punchy.
        if self._voice is not None and state.guild_id is not None:
            cue = _END_REASON_CUE.get(reason)
            if cue is not None:
                await self._play_cue(state, cue)
            await self._voice.disconnect(state.guild_id)

        async with async_session() as db:
            await svc.end_room(db, room_id, reason=reason)
            await svc.record_event(
                db,
                room_id=room_id,
                event_type="room_ended",
                payload={
                    "reason": reason,
                    "completed": state.completed_work_phases,
                    "participants_at_end": sorted(state.participants),
                },
            )

        # Let the UI layer render the closing embed — the view knows what
        # "ended" looks like and also clears the button row.
        from src.ui.embeds import ended_embed

        if state.message is not None:
            try:
                await state.message.edit(embed=ended_embed(state, reason), view=None)
            except discord.HTTPException:
                logger.warning("room.end edit failed room_id=%s", room_id)

        # Strip buttons off the most recent phase message too so users
        # can't click stale controls, and freeze the live timestamp so
        # the message stops ticking "X 分前" after the room is done.
        if state.last_phase_message is not None:
            from src.ui.embeds import freeze_phase_content

            with contextlib.suppress(discord.HTTPException):
                await state.last_phase_message.edit(
                    content=freeze_phase_content(state.last_phase_message.content),
                    view=None,
                )

        logger.info("room.ended room_id=%s reason=%s", room_id, reason)
        return state

    async def end_all(self, *, reason: str = "shutdown") -> None:
        for state in list(self._rooms.values()):
            await self.end(state.room_id, reason=reason)

    # ------------------------------------------------------------------
    # Participant ops (any user)
    # ------------------------------------------------------------------

    async def join(
        self, room_id: UUID, user_id: int, *, task: str | None = None
    ) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND

        await self._evict_from_other_rooms(user_id, except_room_id=room_id)

        async with state.lock:
            if state.has_participant(user_id):
                return OpResult.ALREADY_JOINED

            async with async_session() as db:
                await svc.join_room(db, room_id=room_id, user_id=user_id, task=task)
                await svc.record_event(
                    db,
                    room_id=room_id,
                    event_type="participant_joined",
                    payload={"user_id": user_id},
                )

            state.add_participant(user_id, task=task)
            await self._render_control_panel(state)
            return OpResult.OK

    async def leave(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND

        should_end_reason: str | None = None
        async with state.lock:
            if not state.has_participant(user_id):
                return OpResult.NOT_A_PARTICIPANT

            async with async_session() as db:
                await svc.leave_room(db, room_id=room_id, user_id=user_id)
                await svc.record_event(
                    db,
                    room_id=room_id,
                    event_type="participant_left",
                    payload={"user_id": user_id},
                )

            was_owner = state.is_owner(user_id)
            heir = state.next_owner_after_leave(user_id) if was_owner else None
            state.remove_participant(user_id)

            if not state.participants:
                should_end_reason = "auto_empty"
            elif was_owner and heir is not None:
                state.created_by = heir
                async with async_session() as db:
                    await svc.update_owner(db, room_id, heir)
                    await svc.record_event(
                        db,
                        room_id=room_id,
                        event_type="ownership_transferred",
                        payload={"from": user_id, "to": heir},
                    )
                await self._render_control_panel(state)
            else:
                await self._render_control_panel(state)

        if should_end_reason is not None:
            await self.end(room_id, reason=should_end_reason)
        return OpResult.OK

    async def set_task(
        self, room_id: UUID, user_id: int, *, task: str | None
    ) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        async with state.lock:
            if not state.has_participant(user_id):
                return OpResult.NOT_A_PARTICIPANT
            async with async_session() as db:
                await svc.set_participant_task(
                    db, room_id=room_id, user_id=user_id, task=task
                )
                await svc.record_event(
                    db,
                    room_id=room_id,
                    event_type="task_updated",
                    payload={"user_id": user_id, "task": task},
                )
            state.set_participant_task(user_id, task)
            await self._render_control_panel(state)
            return OpResult.OK

    async def update_plan(
        self,
        room_id: UUID,
        user_id: int,
        *,
        plan: PhasePlan,
    ) -> OpResult:
        """Replace the cycle plan.

        During setup: just update the plan, no side effects on timing.
        During running: also rewind to WORK phase 1 to avoid surprising
        long-break-every shortening edge cases.
        """
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        async with state.lock:
            previous_completed = state.completed_work_phases
            state.plan = plan
            if state.has_started:
                state.phase = Phase.WORK
                state.completed_work_phases = 0
                state.reset_current_phase()
            async with async_session() as db:
                await svc.update_room_plan(
                    db,
                    room_id,
                    work_seconds=plan.work_seconds,
                    short_break_seconds=plan.short_break_seconds,
                    long_break_seconds=plan.long_break_seconds,
                    long_break_every=plan.long_break_every,
                )
                await svc.record_event(
                    db,
                    room_id=room_id,
                    event_type="plan_updated",
                    payload={
                        "work_seconds": plan.work_seconds,
                        "short_break_seconds": plan.short_break_seconds,
                        "long_break_seconds": plan.long_break_seconds,
                        "long_break_every": plan.long_break_every,
                        "previous_completed": previous_completed,
                    },
                )
            await self._render_control_panel(state)
            # Only the phase loop reads wake_event, so don't bother setting
            # it before the loop has been started (setup state).
            if state.has_started:
                state.wake_event.set()

        # During running, the plan update resets to WORK phase 1 — that's
        # a semantic phase boundary, so post a fresh phase message (history
        # in the channel shows the restart) instead of editing in place.
        if state.has_started:
            await self._post_phase_start_message(state)
        return OpResult.OK

    # ------------------------------------------------------------------
    # Owner-only ops (only meaningful while running)
    # ------------------------------------------------------------------

    async def toggle_pause(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        if not state.has_started:
            return OpResult.NOT_YET_STARTED
        async with state.lock:
            event_type = "resumed" if state.is_paused else "paused"
            if state.is_paused:
                state.resume()
            else:
                state.pause()
            async with async_session() as db:
                await svc.record_event(db, room_id=room_id, event_type=event_type)
            await self._render_control_panel(state)
            state.wake_event.set()
        # Pause / resume is an in-place status change; edit the phase
        # message so the ⏸ marker appears or disappears right away.
        await self._refresh_phase_message(state)
        # Voice cue follows the visual update. ``event_type`` matches the
        # *new* state (we read it inside the lock before flipping), so
        # ``"paused"`` → entering pause means play ``pause.wav``.
        await self._play_cue(state, "pause" if event_type == "paused" else "resume")
        return OpResult.OK

    async def skip(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        if not state.has_started:
            return OpResult.NOT_YET_STARTED
        async with state.lock:
            previous = state.phase
            state.advance_phase(count_completion=False)
            async with async_session() as db:
                await svc.record_event(
                    db,
                    room_id=room_id,
                    event_type="phase_skipped",
                    payload={"from": previous, "to": state.phase},
                )
            state.wake_event.set()
        # Skip is a phase boundary — post a new phase message so the
        # channel history shows the transition.
        await self._post_phase_start_message(state)
        # No "end-X" / "alarm" here: the user *interrupted* the phase
        # rather than letting it finish, so announcing its end would feel
        # disingenuous. Play only the start cue for the new phase.
        await self._play_cue(state, _START_CLIP[state.phase])
        return OpResult.OK

    async def reset(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        if not state.has_started:
            return OpResult.NOT_YET_STARTED
        async with state.lock:
            state.reset_current_phase()
            async with async_session() as db:
                await svc.record_event(db, room_id=room_id, event_type="reset")
            await self._render_control_panel(state)
            state.wake_event.set()
        # Reset keeps the same phase; edit the bar back to 0% in place.
        await self._refresh_phase_message(state)
        return OpResult.OK

    async def set_notify(
        self, room_id: UUID, user_id: int, *, phase: Phase, enabled: bool
    ) -> OpResult:
        """Toggle the per-phase mention setting. Owner-only.

        The new value takes effect on the **next** phase boundary; the
        currently-running phase keeps whatever it was posted with.
        """
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        async with state.lock:
            state.set_notify_for(phase, enabled)
            async with async_session() as db:
                await svc.record_event(
                    db,
                    room_id=room_id,
                    event_type="notify_updated",
                    payload={"phase": phase, "enabled": enabled},
                )
        return OpResult.OK

    async def end_by_owner(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        await self.end(room_id, reason="owner_ended")
        return OpResult.OK

    async def toggle_voice(
        self,
        room_id: UUID,
        user_id: int,
        *,
        voice_channel: discord.VoiceChannel | None,
    ) -> OpResult:
        """Connect / disconnect the bot's voice channel for this room.

        Owner-only. Pre-condition: the owner must currently be in a voice
        channel (caller resolves it from ``interaction.user.voice``). On
        connect we play ``connected.wav`` so the listeners hear an explicit
        "接続しました" cue rather than silence. Re-pressing the button while
        connected disconnects.
        """
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        if state.guild_id is None:
            return OpResult.NO_GUILD_CONTEXT
        if self._voice is None:
            return OpResult.VOICE_UNAVAILABLE

        # Toggle off when already connected, regardless of which channel —
        # owner pressed a second time, they want quiet.
        if self._voice.is_connected(state.guild_id):
            await self._voice.disconnect(state.guild_id)
            return OpResult.OK

        # Connect path: owner must currently be in a VC for us to know
        # where to join. Surfaced to the UI as a clean "join a VC first".
        if voice_channel is None:
            return OpResult.OWNER_NOT_IN_VOICE

        connected = await self._voice.connect(voice_channel)
        if not connected:
            return OpResult.VOICE_UNAVAILABLE
        # Best-effort cue — failures are logged inside ``play_clip`` and
        # don't block the room.
        await self._voice.play_clip(state.guild_id, "connected")
        return OpResult.OK

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _play_cue(self, state: RoomState, clip: str) -> None:
        """Best-effort voice cue. No-op when voice isn't connected.

        Errors inside :meth:`VoiceManager.play_clip` are already swallowed
        at that layer; this wrapper just hides the connection check so the
        phase loop / button handlers don't repeat it.
        """
        if self._voice is None or state.guild_id is None:
            return
        if not self._voice.is_connected(state.guild_id):
            return
        await self._voice.play_clip(state.guild_id, clip)

    async def _maybe_play_one_minute_cue(self, state: RoomState) -> bool:
        """Play ``one-minute-left.wav`` once if the phase is in its final 60s.

        Pulled out of the loop body so it's directly unit-testable. Returns
        True when the cue actually fired so callers can record it; False
        when the call was a no-op (already played, too early, or phase
        already finished).
        """
        if state.one_minute_cue_played:
            return False
        new_remaining = state.remaining().total_seconds()
        if not (0 < new_remaining <= 60):
            return False
        state.one_minute_cue_played = True
        await self._play_cue(state, "one-minute-left")
        return True

    async def _play_phase_transition_cues(
        self,
        state: RoomState,
        *,
        phase_just_ended: Phase,
        next_phase: Phase,
    ) -> None:
        """``end-X`` → ``alarm`` → ``start-Y`` for natural phase transitions.

        Sequenced rather than concurrent — Discord only allows one play per
        voice client at a time, and we want listeners to hear them in
        announcement order anyway.
        """
        await self._play_cue(state, _END_CLIP[phase_just_ended])
        await self._play_cue(state, "alarm")
        await self._play_cue(state, _START_CLIP[next_phase])

    async def _evict_from_other_rooms(
        self, user_id: int, *, except_room_id: UUID
    ) -> None:
        targets = [
            r
            for r in list(self._rooms.values())
            if r.room_id != except_room_id and user_id in r.participants
        ]
        for other in targets:
            await self.leave(other.room_id, user_id)

    async def _run_phase_loop(self, state: RoomState) -> None:
        """Drive the phase message: post at phase boundaries, refresh the
        progress bar every ``self._refresh_seconds`` until the phase ends.

        The loop sleeps for ``min(remaining, refresh_seconds, until_60s)``
        — the third bound exists so we wake exactly when one minute is
        left and can play ``one-minute-left.wav`` once. Wake_event preempts
        the sleep on pause / skip / reset / plan update; those user actions
        own their own message refresh outside the loop.
        """
        try:
            # Visual first.
            await self._post_phase_start_message(state)
            # Audio cues for the very start. Best-effort: if voice isn't
            # connected (yet) these short-circuit instantly.
            await self._play_cue(state, "start")
            await self._play_cue(state, _START_CLIP[state.phase])

            while True:
                if state.is_paused:
                    # Wait indefinitely; resume / skip / etc. set wake_event.
                    await state.wake_event.wait()
                    state.wake_event.clear()
                    continue

                remaining = state.remaining().total_seconds()
                if remaining <= 0:
                    await self._handle_phase_end(state)
                    state.wake_event.clear()
                    continue

                sleep_for = min(remaining, self._refresh_seconds)
                # If we haven't yet played the one-minute-left cue and the
                # 60-second mark is still in the future, schedule a wake-up
                # right at it so the cue lands precisely.
                if not state.one_minute_cue_played and remaining > 60:
                    sleep_for = min(sleep_for, remaining - 60)
                try:
                    await asyncio.wait_for(state.wake_event.wait(), timeout=sleep_for)
                    state.wake_event.clear()
                    # User action — the handler already refreshed or posted
                    # a new message. Loop and re-evaluate.
                except TimeoutError:
                    # Tick: maybe play the one-minute cue, then refresh bar.
                    await self._maybe_play_one_minute_cue(state)
                    await self._refresh_phase_message(state)
        except asyncio.CancelledError:
            logger.debug("phase loop cancelled room_id=%s", state.room_id)
            raise
        except Exception:
            logger.exception("phase loop errored room_id=%s", state.room_id)
            await self.end(state.room_id, reason="error")

    async def _handle_phase_end(self, state: RoomState) -> None:
        """Handle the natural end of the current phase.

        ``phase_just_ended`` is read inside the lock so a concurrent ``skip``
        that advanced the phase before we could acquire the lock doesn't
        make us credit the wrong phase or double-advance.
        """
        async with state.lock, async_session() as db:
            phase_just_ended = state.phase
            duration = state.phase_duration_seconds
            credited = 0
            if phase_just_ended is Phase.WORK:
                credited = await svc.record_pomodoros_for_active_participants(
                    db,
                    room_id=state.room_id,
                    duration_seconds=duration,
                )
            state.advance_phase(count_completion=True)
            next_phase = state.phase
            await svc.record_event(
                db,
                room_id=state.room_id,
                event_type="phase_completed",
                payload={
                    "from": phase_just_ended,
                    "to": next_phase,
                    "duration_seconds": duration,
                    "credited_users": credited,
                },
            )

        # Visual update first so the channel reflects the new phase right
        # away; audio cues then reinforce the transition.
        await self._post_phase_start_message(state)
        await self._play_phase_transition_cues(
            state,
            phase_just_ended=phase_just_ended,
            next_phase=next_phase,
        )

    async def _post_phase_start_message(self, state: RoomState) -> None:
        """Post a new phase message and strip buttons off the old one.

        Called at phase boundaries: natural end, skip, and plan-update
        (which resets to WORK phase 1). Pause / reset edit the existing
        message in place via ``_refresh_phase_message`` instead.

        Ordering matters: we send the new message first and only strip
        the old view after that succeeds. If the send fails (bot kicked,
        permissions changed), the previous panel's buttons remain live so
        the user isn't left with zero interactive controls.

        Content is snapshotted under ``state.lock`` so concurrent mutations
        can't tear the build, and ``last_phase_message`` is assigned back
        under the lock too so a racing phase-end + skip don't leave the
        pointer stale.
        """
        from src.ui.embeds import phase_content
        from src.ui.panel_views import PhasePanelView

        channel = state.message.channel if state.message is not None else None
        if channel is None:
            return

        async with state.lock:
            content = phase_content(state)
            previous_phase_message = state.last_phase_message

        view = PhasePanelView(self, state.room_id)
        try:
            # Explicit allowed_mentions: spoiler-wrapped ``||<@uid>||`` should
            # still fire user pings, but never ping @everyone or roles.
            msg = await channel.send(
                content=content,
                view=view,
                allowed_mentions=discord.AllowedMentions(
                    users=True, everyone=False, roles=False
                ),
            )
        except discord.HTTPException:
            logger.warning(
                "phase.announce failed room_id=%s phase=%s",
                state.room_id,
                state.phase,
            )
            return

        async with state.lock:
            state.last_phase_message = msg

        # Send succeeded — now it's safe to retire the old panel. Also
        # strip the live ``<t:...:R>`` line: without this, Discord keeps
        # re-rendering the old message as ``"X 分前"`` forever even after
        # the phase has ended. Message gone / permissions changed — not fatal.
        if previous_phase_message is not None:
            from src.ui.embeds import freeze_phase_content

            with contextlib.suppress(discord.HTTPException):
                await previous_phase_message.edit(
                    content=freeze_phase_content(previous_phase_message.content),
                    view=None,
                )

    async def _refresh_phase_message(self, state: RoomState) -> None:
        """Edit the current phase message in place — bar tick or pause flip.

        Used for periodic refreshes (every ``refresh_seconds``) and for
        pause/reset, which don't need a new message in the channel
        history. If there's no current phase message yet (pre-start) or
        the edit fails, we just log and move on.
        """
        from src.ui.embeds import phase_content

        if state.last_phase_message is None:
            return
        async with state.lock:
            content = phase_content(state)
        try:
            await state.last_phase_message.edit(content=content)
        except discord.HTTPException:
            logger.warning("phase.refresh failed room_id=%s", state.room_id)

    async def _render_control_panel(self, state: RoomState) -> None:
        """Refresh the Control Panel message (participants, plan, status)."""
        from src.ui.embeds import control_panel_embed
        from src.ui.panel_views import ControlPanelView

        if state.message is None:
            return
        try:
            await state.message.edit(
                embed=control_panel_embed(state),
                view=ControlPanelView(
                    self, state.room_id, has_started=state.has_started
                ),
            )
        except discord.HTTPException:
            logger.warning("control panel render failed room_id=%s", state.room_id)

    # Expose for tests
    def _register_for_tests(self, state: RoomState) -> None:
        self._rooms[state.room_id] = state


__all__ = ["OpResult", "ParticipantState", "RoomManager"]
