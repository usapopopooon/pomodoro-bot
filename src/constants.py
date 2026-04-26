from __future__ import annotations

from pathlib import Path

# Project-relative path to the directory holding the .wav cues. Resolved
# from this file's location so the path stays valid regardless of the
# working directory the bot is launched from. Override with
# ``POMO_VOICES_DIR`` if cues live somewhere else (e.g. mounted volume).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VOICES_DIR = PROJECT_ROOT / "voices"

DEFAULT_EMBED_COLOR = 0xE74C3C

PHASE_COLOR_WORK = 0xE74C3C
PHASE_COLOR_SHORT_BREAK = 0x2ECC71
PHASE_COLOR_LONG_BREAK = 0x3498DB
PHASE_COLOR_ENDED = 0x95A5A6

PROGRESS_BAR_LENGTH = 20
PROGRESS_BAR_FILLED = "█"
PROGRESS_BAR_EMPTY = "░"

DEFAULT_WORK_SECONDS = 25 * 60
DEFAULT_SHORT_BREAK_SECONDS = 5 * 60
DEFAULT_LONG_BREAK_SECONDS = 15 * 60
DEFAULT_LONG_BREAK_EVERY = 4

# How often the phase message is re-rendered while a phase is running.
# Expressed in whole minutes to match the minute-granular clock in the
# progress bar ("5分 / 25分") — sub-minute refreshes would update the bar
# visually but not the text, which just feels inconsistent.
DEFAULT_REFRESH_MINUTES = 1
