from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://boss:boss@localhost:5432/boss"
    worker_id: str = Field(default="worker-local", alias="BOSS_WORKER_ID")
    poll_interval_seconds: int = Field(default=15, alias="BOSS_POLL_INTERVAL_SECONDS")
    github_token: Optional[str] = Field(default=None, alias="BOSS_GITHUB_TOKEN")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
