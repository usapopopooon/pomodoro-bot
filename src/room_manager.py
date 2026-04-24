from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from uuid import UUID

import discord

from src.core.phase import Phase, PhasePlan
from src.core.room_state import ParticipantState, RoomState
from src.database.engine import async_session
from src.services import room_service as svc

logger = logging.getLogger(__name__)


class OpResult(StrEnum):
    """Why a button action was accepted / rejected.

    Views turn these into ephemeral user feedback without having to know
    anything about the manager's internals.
    """

    OK = "ok"
    NOT_A_PARTICIPANT = "not_a_participant"
    NOT_OWNER = "not_owner"
    ALREADY_JOINED = "already_joined"
    ROOM_NOT_FOUND = "room_not_found"
    ANOTHER_ROOM = "another_room"


class RoomManager:
    """Owns live rooms, keyed by ``room_id``.

    One ``SessionManager`` used to mean one user, one session. ``RoomManager``
    inverts that: many participants can share one room, many rooms can run in
    different channels concurrently, and every button action funnels through
    here so the DB, the in-memory state, and the Discord panel stay in sync.
    """

    def __init__(self, *, default_plan: PhasePlan, tick_seconds: int) -> None:
        self._rooms: dict[UUID, RoomState] = {}
        self._default_plan = default_plan
        self._tick_seconds = tick_seconds
        self._registry_lock = asyncio.Lock()

    @property
    def default_plan(self) -> PhasePlan:
        return self._default_plan

    def get(self, room_id: UUID) -> RoomState | None:
        return self._rooms.get(room_id)

    def active_rooms(self) -> list[RoomState]:
        return list(self._rooms.values())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_and_start(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        created_by: int,
        channel: discord.abc.Messageable,
        plan: PhasePlan | None = None,
    ) -> RoomState:
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

        state.task_handle = asyncio.create_task(
            self._run_loop(state, channel), name=f"pomo-room-{row.id}"
        )
        logger.info(
            "room.created room_id=%s channel=%s by=%s", row.id, channel_id, created_by
        )
        return state

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

        # Evict from any other in-memory room BEFORE acquiring this room's
        # lock. Holding ``state.lock`` while waiting on another room's lock
        # used to deadlock when two users swapped rooms in opposite
        # directions (A: r1→r2, B: r2→r1). ``svc.join_room`` closes the old
        # DB participation; this call mirrors that in memory.
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
            await self._render(state)
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
                await self._render(state)
            else:
                await self._render(state)

        # ``end`` re-acquires registry lock + cancels the loop task, so only
        # call it after releasing ``state.lock``.
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
            await self._render(state)
            return OpResult.OK

    async def update_plan(
        self,
        room_id: UUID,
        user_id: int,
        *,
        plan: PhasePlan,
    ) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        async with state.lock:
            state.plan = plan
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
                    },
                )
            await self._render(state)
            return OpResult.OK

    # ------------------------------------------------------------------
    # Owner-only ops
    # ------------------------------------------------------------------

    async def toggle_pause(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        async with state.lock:
            event_type = "resumed" if state.is_paused else "paused"
            if state.is_paused:
                state.resume()
            else:
                state.pause()
            async with async_session() as db:
                await svc.record_event(db, room_id=room_id, event_type=event_type)
            await self._render(state)
        return OpResult.OK

    async def skip(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
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
            await self._announce_phase(state)
            await self._render(state)
        return OpResult.OK

    async def reset(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        async with state.lock:
            state.reset_current_phase()
            async with async_session() as db:
                await svc.record_event(db, room_id=room_id, event_type="reset")
            await self._render(state)
        return OpResult.OK

    async def end_by_owner(self, room_id: UUID, user_id: int) -> OpResult:
        state = self._rooms.get(room_id)
        if state is None:
            return OpResult.ROOM_NOT_FOUND
        if not state.is_owner(user_id):
            return OpResult.NOT_OWNER
        await self.end(room_id, reason="owner_ended")
        return OpResult.OK

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _evict_from_other_rooms(
        self, user_id: int, *, except_room_id: UUID
    ) -> None:
        """Remove ``user_id`` from every in-memory room except ``except_room_id``.

        ``svc.join_room`` already closes stale DB participations when a user
        switches rooms; this call mirrors a real leave operation so ownership
        transfer, auto-end, and lifecycle events stay consistent. Called from
        ``join`` *before* the target room's lock is acquired so that two
        concurrent swaps in opposite directions cannot cycle on room locks.
        """
        targets = [
            r
            for r in list(self._rooms.values())
            if r.room_id != except_room_id and user_id in r.participants
        ]
        for other in targets:
            # Route through leave() so DB + events + owner logic all stay
            # aligned with explicit "退出" button behaviour.
            await self.leave(other.room_id, user_id)

    async def _run_loop(
        self, state: RoomState, channel: discord.abc.Messageable
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._tick_seconds)
                if state.is_paused:
                    await self._render(state)
                    continue

                if state.remaining().total_seconds() <= 0:
                    await self._handle_phase_end(state, channel)

                await self._render(state)
        except asyncio.CancelledError:
            logger.debug("room loop cancelled room_id=%s", state.room_id)
            raise
        except Exception:
            logger.exception("room loop errored room_id=%s", state.room_id)
            await self.end(state.room_id, reason="error")

    async def _handle_phase_end(
        self, state: RoomState, channel: discord.abc.Messageable
    ) -> None:
        phase_just_ended = state.phase
        duration = state.phase_duration_seconds

        async with state.lock, async_session() as db:
            credited = 0
            if phase_just_ended is Phase.WORK:
                credited = await svc.record_pomodoros_for_active_participants(
                    db,
                    room_id=state.room_id,
                    duration_seconds=duration,
                )
            state.advance_phase(count_completion=True)
            await svc.record_event(
                db,
                room_id=state.room_id,
                event_type="phase_completed",
                payload={
                    "from": phase_just_ended,
                    "to": state.phase,
                    "duration_seconds": duration,
                    "credited_users": credited,
                },
            )
        await self._announce_phase(state, channel=channel)

    async def _announce_phase(
        self,
        state: RoomState,
        *,
        channel: discord.abc.Messageable | None = None,
    ) -> None:
        from src.ui.embeds import transition_message

        target = channel
        if target is None and state.message is not None:
            target = state.message.channel
        if target is None:
            return
        try:
            await target.send(
                transition_message(state.phase, state.completed_work_phases)
            )
        except discord.HTTPException:
            logger.warning("room.announce failed room_id=%s", state.room_id)

    async def _render(self, state: RoomState) -> None:
        from src.ui.embeds import room_embed

        if state.message is None:
            return
        try:
            await state.message.edit(embed=room_embed(state))
        except discord.HTTPException:
            logger.warning("room.render failed room_id=%s", state.room_id)

    # Expose for tests
    def _register_for_tests(self, state: RoomState) -> None:
        self._rooms[state.room_id] = state


__all__ = ["OpResult", "ParticipantState", "RoomManager"]
