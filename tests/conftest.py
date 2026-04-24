"""Test bootstrapping.

``src.config`` validates required env vars at import time, so we populate
dummy values here — before any ``src.*`` modules import.
"""

from __future__ import annotations

import os

os.environ.setdefault("DISCORD_TOKEN", "test-token-for-testing")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro_test",
)
