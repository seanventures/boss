from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+psycopg://boss:boss@localhost:5432/boss"
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.2-codex", alias="BOSS_OPENAI_MODEL")
    github_token: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("BOSS_GITHUB_TOKEN", "GITHUB_TOKEN")
    )
    github_owner: str = Field(default="seanheiney", alias="BOSS_GITHUB_OWNER")
    github_repo: str = Field(default="boss", alias="BOSS_GITHUB_REPO")
    target_github_owner: str = Field(
        default="seanheiney", alias="BOSS_TARGET_GITHUB_OWNER"
    )
    target_github_repo: str = Field(
        default="boss-sandbox", alias="BOSS_TARGET_GITHUB_REPO"
    )
    default_org_id: str = Field(default="org_boss", alias="BOSS_DEFAULT_ORG_ID")
    default_workspace_id: str = Field(
        default="ws_boss", alias="BOSS_DEFAULT_WORKSPACE_ID"
    )
    default_project_id: str = Field(
        default="boss-dogfood", alias="BOSS_DEFAULT_PROJECT_ID"
    )
    artifact_dir: Path = Field(default=Path("artifacts"), alias="BOSS_ARTIFACTS_DIR")
    daily_cap_usd_micros: int = Field(
        default=10_000_000, alias="BOSS_DAILY_CAP_USD_MICROS"
    )
    monthly_cap_usd_micros: int = Field(
        default=200_000_000, alias="BOSS_MONTHLY_CAP_USD_MICROS"
    )
    burst_cap_usd_micros_per_min: int = Field(
        default=100_000, alias="BOSS_BURST_CAP_USD_MICROS_PER_MIN"
    )

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def schema_path(self) -> Path:
        return self.repo_root / "db" / "schema.sql"

    @property
    def artifact_root(self) -> Path:
        return (self.repo_root / self.artifact_dir).resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
