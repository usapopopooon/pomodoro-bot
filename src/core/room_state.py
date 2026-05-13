from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from src.core.phase import Phase, PhasePlan, next_phase

if TYPE_CHECKING:
    import discord


@dataclass
class ParticipantState:
    user_id: int
    task: str | None = None
    joined_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class RoomState:
    """In-memory view of a running room.

    Persistence is intentionally thin: completed pomodoros and lifecycle events
    go to the DB, but phase timing state lives here only. On bot restart, the
    room is closed with ``bot_restart`` and must be recreated via the panel.

    ``lock`` serialises button-driven mutations. ``wake_event`` signals the
    phase loop whenever state changes — the loop uses ``wait_for(wake_event,
    timeout=remaining)`` to sleep until either the phase naturally ends
    (timeout) or a user acts on the room (event fires).
    """

    room_id: UUID
    guild_id: int | None
    channel_id: int
    created_by: int
    plan: PhasePlan

    phase: Phase = Phase.WORK
    phase_started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    paused_at: datetime | None = None
    paused_accumulated: timedelta = field(default_factory=timedelta)
    completed_work_phases: int = 0

    # Set to True once the ``one-minute-left`` voice cue has fired for the
    # current phase, so the phase loop doesn't replay it on every refresh
    # tick that lands inside the final 60 seconds. Reset alongside the
    # phase clock in :meth:`reset_current_phase`.
    one_minute_cue_played: bool = False

    # ``has_started`` gates the phase loop. ``/pomo`` creates a room with
    # ``has_started=False`` and shows the Control Panel; the owner presses
    # Start to flip this flag, which kicks off the actual timer.
    has_started: bool = False

    # Per-phase mention toggles. The phase progress message is edited in
    # place across the room's lifetime — without a separate ping post,
    # users wouldn't get a notification on phase boundaries. Defaulting
    # these to off keeps the channel quiet by default; owners opt in per
    # phase via the 🔔 button on the Control Panel.
    # Memory-only — resets on every ``/pomo``.
    notify_work: bool = False
    notify_short_break: bool = False
    notify_long_break: bool = False

    participants: dict[int, ParticipantState] = field(default_factory=dict)

    # ``message`` is the persistent Control Panel message.
    # Phase-transition announcements live as separate channel messages —
    # we keep only the most recent one around so we can strip its buttons
    # when a new phase begins.
    message: discord.Message | None = None
    last_phase_message: discord.Message | None = None
    task_handle: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)

    # ------------------------------------------------------------------
    # Timer math
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        return self.paused_at is not None

    @property
    def phase_duration_seconds(self) -> int:
        return self.plan.duration_of(self.phase)

    def elapsed(self, now: datetime | None = None) -> timedelta:
        now = now or datetime.now(UTC)
        paused_total = self.paused_accumulated
        if self.paused_at is not None:
            paused_total = paused_total + (now - self.paused_at)
        return (now - self.phase_started_at) - paused_total

    def remaining(self, now: datetime | None = None) -> timedelta:
        remaining = timedelta(seconds=self.phase_duration_seconds) - self.elapsed(now)
        return remaining if remaining.total_seconds() > 0 else timedelta(0)

    def pause(self, now: datetime | None = None) -> None:
        if self.paused_at is not None:
            return
        self.paused_at = now or datetime.now(UTC)

    def resume(self, now: datetime | None = None) -> None:
        if self.paused_at is None:
            return
        now = now or datetime.now(UTC)
        self.paused_accumulated += now - self.paused_at
        self.paused_at = None

    def reset_current_phase(self) -> None:
        self.phase_started_at = datetime.now(UTC)
        self.paused_at = None
        self.paused_accumulated = timedelta()
        # Re-arm the one-minute-left cue so the *new* clock period plays it.
        self.one_minute_cue_played = False

    def advance_phase(self, *, count_completion: bool) -> Phase:
        if count_completion and self.phase is Phase.WORK:
            self.completed_work_phases += 1
        transition = next_phase(
            self.phase,
            completed_work_phases=self.completed_work_phases,
            plan=self.plan,
        )
        self.phase = transition.next_phase
        self.reset_current_phase()
        return self.phase

    # ------------------------------------------------------------------
    # Participants (memory mirror; DB is source of truth)
    # ------------------------------------------------------------------

    def add_participant(
        self, user_id: int, task: str | None = None
    ) -> ParticipantState:
        existing = self.participants.get(user_id)
        if existing is not None:
            if task is not None:
                existing.task = task
            return existing
        p = ParticipantState(user_id=user_id, task=task)
        self.participants[user_id] = p
        return p

    def remove_participant(self, user_id: int) -> ParticipantState | None:
        return self.participants.pop(user_id, None)

    def set_participant_task(self, user_id: int, task: str | None) -> bool:
        p = self.participants.get(user_id)
        if p is None:
            return False
        p.task = task
        return True

    def has_participant(self, user_id: int) -> bool:
        return user_id in self.participants

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.created_by

    def notify_enabled_for(self, phase: Phase) -> bool:
        return {
            Phase.WORK: self.notify_work,
            Phase.SHORT_BREAK: self.notify_short_break,
            Phase.LONG_BREAK: self.notify_long_break,
        }[phase]

    def set_notify_for(self, phase: Phase, enabled: bool) -> None:
        if phase is Phase.WORK:
            self.notify_work = enabled
        elif phase is Phase.SHORT_BREAK:
            self.notify_short_break = enabled
        else:
            self.notify_long_break = enabled

    def next_owner_after_leave(self, leaving_user_id: int) -> int | None:
        """Return the earliest-joined remaining participant, or ``None``."""
        candidates = [
            p for uid, p in self.participants.items() if uid != leaving_user_id
        ]
        if not candidates:
            return None
        earliest = min(candidates, key=lambda p: p.joined_at)
        return earliest.user_id
