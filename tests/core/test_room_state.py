from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.core.phase import Phase, PhasePlan
from src.core.room_state import RoomState


def _room(
    *,
    created_by: int = 1,
    plan: PhasePlan | None = None,
) -> RoomState:
    return RoomState(
        room_id=uuid4(),
        guild_id=None,
        channel_id=1000,
        created_by=created_by,
        plan=plan or PhasePlan(10, 2, 4, 2),
    )


# ---------------------------------------------------------------------------
# Timer math
# ---------------------------------------------------------------------------


def test_remaining_decreases_with_elapsed_time() -> None:
    state = _room()
    state.phase_started_at = datetime.now(UTC) - timedelta(seconds=3)
    assert 6.5 < state.remaining().total_seconds() <= 7.0


def test_pause_freezes_elapsed_time() -> None:
    state = _room()
    state.phase_started_at = datetime.now(UTC) - timedelta(seconds=1)
    state.pause()
    paused_at = state.paused_at
    assert paused_at is not None
    # Remaining should still be ~9 even 5 seconds after pause.
    faked_now = paused_at + timedelta(seconds=5)
    assert 8.5 < state.remaining(faked_now).total_seconds() <= 9.0


def test_resume_accumulates_paused_duration() -> None:
    state = _room()
    state.phase_started_at = datetime.now(UTC) - timedelta(seconds=1)
    state.pause()
    # Pretend they were paused for 3 seconds, then tap resume.
    state.paused_at = datetime.now(UTC) - timedelta(seconds=3)
    state.resume()
    assert state.paused_accumulated >= timedelta(seconds=3)


def test_cycle_work_short_work_long_with_every_two() -> None:
    state = _room(plan=PhasePlan(10, 2, 4, 2))
    assert state.advance_phase(count_completion=True) is Phase.SHORT_BREAK
    assert state.completed_work_phases == 1
    assert state.advance_phase(count_completion=False) is Phase.WORK
    assert state.advance_phase(count_completion=True) is Phase.LONG_BREAK
    assert state.completed_work_phases == 2


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------


def test_add_participant_is_idempotent() -> None:
    state = _room()
    p1 = state.add_participant(42, task="math")
    p2 = state.add_participant(42)
    assert p1 is p2
    assert len(state.participants) == 1


def test_add_participant_updates_task_when_rejoining() -> None:
    state = _room()
    state.add_participant(42, task="math")
    state.add_participant(42, task="english")
    assert state.participants[42].task == "english"


def test_set_participant_task_fails_for_unknown_user() -> None:
    state = _room()
    assert state.set_participant_task(999, "x") is False


def test_is_owner_checks_created_by_not_membership() -> None:
    state = _room(created_by=7)
    assert state.is_owner(7)
    assert not state.is_owner(42)
    # Owner doesn't need to be a participant for ``is_owner`` to be true.
    assert state.is_owner(7)


def test_next_owner_after_leave_picks_earliest_remaining() -> None:
    state = _room(created_by=1)
    state.add_participant(1)
    state.participants[1].joined_at = datetime(2026, 4, 24, 10, 0, tzinfo=UTC)
    state.add_participant(2)
    state.participants[2].joined_at = datetime(2026, 4, 24, 10, 5, tzinfo=UTC)
    state.add_participant(3)
    state.participants[3].joined_at = datetime(2026, 4, 24, 10, 2, tzinfo=UTC)

    # Owner (1) leaves; earliest remaining is user 3 (joined at 10:02).
    assert state.next_owner_after_leave(1) == 3


def test_next_owner_after_leave_returns_none_when_empty() -> None:
    state = _room(created_by=1)
    state.add_participant(1)
    assert state.next_owner_after_leave(1) is None
