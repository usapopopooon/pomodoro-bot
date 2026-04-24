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
    go to the DB, but tick-by-tick timer state lives here only. On bot
    restart, the room is closed with ``bot_restart`` and recreated via the
    panel. A per-room ``lock`` serialises button-driven mutations so two
    users can't pause/skip at the same instant.
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

    participants: dict[int, ParticipantState] = field(default_factory=dict)

    message: discord.Message | None = None
    task_handle: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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

    def next_owner_after_leave(self, leaving_user_id: int) -> int | None:
        """Return the earliest-joined remaining participant, or ``None``."""
        candidates = [
            p for uid, p in self.participants.items() if uid != leaving_user_id
        ]
        if not candidates:
            return None
        earliest = min(candidates, key=lambda p: p.joined_at)
        return earliest.user_id
