from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.constants import (
    PHASE_COLOR_LONG_BREAK,
    PHASE_COLOR_SHORT_BREAK,
    PHASE_COLOR_WORK,
)


class Phase(StrEnum):
    WORK = "work"
    SHORT_BREAK = "short_break"
    LONG_BREAK = "long_break"

    @property
    def label_ja(self) -> str:
        return {
            Phase.WORK: "作業",
            Phase.SHORT_BREAK: "短休憩",
            Phase.LONG_BREAK: "長休憩",
        }[self]

    @property
    def color(self) -> int:
        return {
            Phase.WORK: PHASE_COLOR_WORK,
            Phase.SHORT_BREAK: PHASE_COLOR_SHORT_BREAK,
            Phase.LONG_BREAK: PHASE_COLOR_LONG_BREAK,
        }[self]


@dataclass(frozen=True, slots=True)
class PhasePlan:
    """Per-user cycle plan. Kept small so alt cycles (50/10 etc.) slot in cleanly."""

    work_seconds: int
    short_break_seconds: int
    long_break_seconds: int
    long_break_every: int

    def duration_of(self, phase: Phase) -> int:
        match phase:
            case Phase.WORK:
                return self.work_seconds
            case Phase.SHORT_BREAK:
                return self.short_break_seconds
            case Phase.LONG_BREAK:
                return self.long_break_seconds


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    next_phase: Phase
    completed_work_phases: int


def next_phase(
    current: Phase,
    *,
    completed_work_phases: int,
    plan: PhasePlan,
) -> PhaseTransition:
    """Compute the next phase given what just finished.

    ``completed_work_phases`` is the count *after* ``current`` ends: when the
    current phase is WORK, pass in the already-incremented count so the
    every-Nth long-break decision uses the right number.
    """
    if current is Phase.WORK:
        hit_long_break = (
            completed_work_phases > 0
            and completed_work_phases % plan.long_break_every == 0
        )
        if hit_long_break:
            return PhaseTransition(Phase.LONG_BREAK, completed_work_phases)
        return PhaseTransition(Phase.SHORT_BREAK, completed_work_phases)
    return PhaseTransition(Phase.WORK, completed_work_phases)
