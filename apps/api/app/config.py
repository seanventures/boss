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
    anthropic_api_key: Optional[str] = Field(
        default=None, alias="ANTHROPIC_API_KEY"
    )
    google_api_key: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY")
    xai_api_key: Optional[str] = Field(default=None, alias="XAI_API_KEY")
    slack_webhook_url: Optional[str] = Field(
        default=None, alias="BOSS_SLACK_WEBHOOK_URL"
    )
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
    core_pack_id: str = Field(default="core_v1", alias="BOSS_CORE_PACK_ID")
    cursor_cli_enabled: bool = Field(default=False, alias="BOSS_CURSOR_CLI_ENABLED")
    local_model_enabled: bool = Field(default=False, alias="BOSS_LOCAL_MODEL_ENABLED")
    local_model_id: Optional[str] = Field(
        default=None, alias="BOSS_LOCAL_MODEL_ID"
    )
    daily_cap_usd_micros: int = Field(
        default=10_000_000, alias="BOSS_DAILY_CAP_USD_MICROS"
    )
    monthly_cap_usd_micros: int = Field(
        default=200_000_000, alias="BOSS_MONTHLY_CAP_USD_MICROS"
    )
    burst_cap_usd_micros_per_min: int = Field(
        default=100_000, alias="BOSS_BURST_CAP_USD_MICROS_PER_MIN"
    )
    software_factory_pr_budget_usd_micros: int = Field(
        default=500_000, alias="BOSS_SOFTWARE_FACTORY_PR_BUDGET_USD_MICROS"
    )
    comms_message_budget_usd_micros: int = Field(
        default=50_000, alias="BOSS_COMMS_MESSAGE_BUDGET_USD_MICROS"
    )
    slack_real_send_allowed: bool = Field(
        default=False, alias="BOSS_SLACK_REAL_SEND_ALLOWED"
    )
    enable_sandbox_merge: bool = Field(
        default=False, alias="BOSS_ENABLE_SANDBOX_MERGE"
    )
    sandbox_merge_method: str = Field(
        default="squash", alias="BOSS_SANDBOX_MERGE_METHOD"
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

    @property
    def specpack_root(self) -> Path:
        return (self.repo_root / "packages" / "specpack").resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
