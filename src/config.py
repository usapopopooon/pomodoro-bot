from __future__ import annotations

from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.constants import (
    DEFAULT_LONG_BREAK_EVERY,
    DEFAULT_LONG_BREAK_SECONDS,
    DEFAULT_REFRESH_MINUTES,
    DEFAULT_SHORT_BREAK_SECONDS,
    DEFAULT_WORK_SECONDS,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://pomodoro:pomodoro@localhost:5432/pomodoro"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: str = ""
    # NoDecode: pydantic-settings would otherwise try to JSON-decode the env
    # string; we take raw input and split it in the validator below.
    discord_guild_ids: Annotated[list[int], NoDecode] = []

    database_url: str = DEFAULT_DATABASE_URL

    pomo_work_seconds: int = DEFAULT_WORK_SECONDS
    pomo_short_break_seconds: int = DEFAULT_SHORT_BREAK_SECONDS
    pomo_long_break_seconds: int = DEFAULT_LONG_BREAK_SECONDS
    pomo_long_break_every: int = DEFAULT_LONG_BREAK_EVERY
    # How often (in whole minutes) the phase message is re-rendered to
    # advance the ASCII bar. Clamped to >=1 below.
    pomo_refresh_minutes: int = DEFAULT_REFRESH_MINUTES

    log_level: str = "INFO"

    @field_validator("pomo_refresh_minutes", mode="after")
    @classmethod
    def _clamp_refresh_minutes(cls, v: int) -> int:
        return max(1, v)

    @field_validator("discord_guild_ids", mode="before")
    @classmethod
    def _split_guild_ids(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            return [int(s) for s in v.split(",") if s.strip()]
        return v

    @model_validator(mode="after")
    def _validate_required(self) -> Settings:
        if not self.discord_token or not self.discord_token.strip():
            raise ValueError("DISCORD_TOKEN environment variable is required.")
        return self

    @property
    def async_database_url(self) -> str:
        """Normalise the URL for asyncpg.

        Heroku/Railway provide ``postgres://`` or ``postgresql://``; SQLAlchemy
        needs the explicit asyncpg driver.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def sync_database_url(self) -> str:
        """Alembic migrations run on a sync driver; strip the async suffix."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url.replace("postgresql+asyncpg://", "postgresql://")


settings = Settings()
