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


def test_single_discord_token_is_promoted_to_tokens_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back-compat: legacy single-token deploys still work.

    Setting just ``DISCORD_TOKEN`` should populate ``discord_tokens`` with
    that one entry so the multi-bot main loop has a uniform list to iterate.
    """
    monkeypatch.setenv("DISCORD_TOKEN", "alpha")
    monkeypatch.delenv("DISCORD_TOKENS", raising=False)
    s = Settings()
    assert s.discord_tokens == ["alpha"]
    assert s.discord_token == "alpha"


def test_discord_tokens_csv_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_TOKENS", "alpha,beta, gamma ")
    s = Settings()
    assert s.discord_tokens == ["alpha", "beta", "gamma"]
    # ``discord_token`` shadows the first one for callers that still want a
    # scalar (e.g. tests, log lines).
    assert s.discord_token == "alpha"


def test_neither_token_var_set_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_TOKENS", raising=False)
    monkeypatch.setattr(
        "src.config.Settings.model_config",
        {"env_file": None, "extra": "ignore"},
    )
    with pytest.raises(ValidationError):
        Settings()
