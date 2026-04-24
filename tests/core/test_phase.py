from __future__ import annotations

from src.core.phase import Phase, PhasePlan, next_phase


def _plan(every: int = 4) -> PhasePlan:
    return PhasePlan(
        work_seconds=1500,
        short_break_seconds=300,
        long_break_seconds=900,
        long_break_every=every,
    )


def test_work_to_short_break_when_not_at_long_break_boundary() -> None:
    plan = _plan()
    t = next_phase(Phase.WORK, completed_work_phases=1, plan=plan)
    assert t.next_phase is Phase.SHORT_BREAK


def test_work_to_long_break_at_boundary() -> None:
    plan = _plan(every=4)
    t = next_phase(Phase.WORK, completed_work_phases=4, plan=plan)
    assert t.next_phase is Phase.LONG_BREAK


def test_short_break_to_work() -> None:
    plan = _plan()
    t = next_phase(Phase.SHORT_BREAK, completed_work_phases=1, plan=plan)
    assert t.next_phase is Phase.WORK


def test_long_break_to_work() -> None:
    plan = _plan()
    t = next_phase(Phase.LONG_BREAK, completed_work_phases=4, plan=plan)
    assert t.next_phase is Phase.WORK


def test_zero_completions_goes_to_short_break() -> None:
    plan = _plan(every=1)
    # Before first completion; guard against treating 0 as a long-break trigger
    t = next_phase(Phase.WORK, completed_work_phases=0, plan=plan)
    assert t.next_phase is Phase.SHORT_BREAK
