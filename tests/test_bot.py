from __future__ import annotations

from src.bot import _build_plan_for_command
from src.core.phase import PhasePlan


def test_build_plan_for_command_uses_defaults_when_options_missing() -> None:
    default_plan = PhasePlan(
        work_seconds=25 * 60,
        short_break_seconds=5 * 60,
        long_break_seconds=15 * 60,
        long_break_every=4,
    )

    plan = _build_plan_for_command(
        default_plan=default_plan,
        work_minutes=None,
        short_break_minutes=None,
        long_break_minutes=None,
        long_break_every=None,
    )

    assert plan == default_plan


def test_build_plan_for_command_overrides_selected_fields() -> None:
    default_plan = PhasePlan(
        work_seconds=25 * 60,
        short_break_seconds=5 * 60,
        long_break_seconds=15 * 60,
        long_break_every=4,
    )

    plan = _build_plan_for_command(
        default_plan=default_plan,
        work_minutes=50,
        short_break_minutes=None,
        long_break_minutes=20,
        long_break_every=3,
    )

    assert plan.work_seconds == 50 * 60
    assert plan.short_break_seconds == 5 * 60
    assert plan.long_break_seconds == 20 * 60
    assert plan.long_break_every == 3
