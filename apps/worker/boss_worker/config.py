from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://boss:boss@localhost:5432/boss"
    control_plane_url: str = Field(
        default="http://127.0.0.1:8000", alias="BOSS_CONTROL_PLANE_URL"
    )
    worker_id: str = Field(default="worker-local", alias="BOSS_WORKER_ID")
    poll_interval_seconds: int = Field(default=15, alias="BOSS_POLL_INTERVAL_SECONDS")
    lease_ttl_seconds: int = Field(default=60, alias="BOSS_LEASE_TTL_SECONDS")
    github_token: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("BOSS_GITHUB_TOKEN", "GITHUB_TOKEN")
    )
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
