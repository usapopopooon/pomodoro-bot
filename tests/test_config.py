from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings


def test_async_url_rewrites_postgres_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    s = Settings()
    assert s.async_database_url == "postgresql+asyncpg://u:p@h:5432/db"


def test_async_url_rewrites_postgresql_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    s = Settings()
    assert s.async_database_url == "postgresql+asyncpg://u:p@h:5432/db"


def test_sync_url_strips_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    s = Settings()
    assert s.sync_database_url == "postgresql://u:p@h:5432/db"


def test_discord_guild_ids_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("DISCORD_GUILD_IDS", "1,2,3")
    s = Settings()
    assert s.discord_guild_ids == [1, 2, 3]


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setattr(
        "src.config.Settings.model_config",
        {"env_file": None, "extra": "ignore"},
    )
    with pytest.raises(ValidationError):
        Settings(discord_token="")


def test_refresh_minutes_defaults_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.delenv("POMO_REFRESH_MINUTES", raising=False)
    s = Settings()
    assert s.pomo_refresh_minutes == 1


def test_refresh_minutes_parses_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("POMO_REFRESH_MINUTES", "3")
    s = Settings()
    assert s.pomo_refresh_minutes == 3


def test_refresh_minutes_clamped_to_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Zero / negative would make the phase loop spin — clamp up to 1.
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("POMO_REFRESH_MINUTES", "0")
    s = Settings()
    assert s.pomo_refresh_minutes == 1
