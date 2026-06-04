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
    infra_real_provision_allowed: bool = Field(
        default=False, alias="BOSS_INFRA_REAL_PROVISION_ALLOWED"
    )
    digitalocean_token: Optional[str] = Field(
        default=None, alias="BOSS_DIGITALOCEAN_TOKEN"
    )
    cloudflare_api_token: Optional[str] = Field(
        default=None, alias="BOSS_CLOUDFLARE_API_TOKEN"
    )
    supabase_access_token: Optional[str] = Field(
        default=None, alias="BOSS_SUPABASE_ACCESS_TOKEN"
    )
    digitalocean_default_image: str = Field(
        default="ubuntu-24-04-x64",
        alias="BOSS_DIGITALOCEAN_DEFAULT_IMAGE",
    )
    digitalocean_default_tags: str = Field(
        default="boss,ttl-managed",
        alias="BOSS_DIGITALOCEAN_DEFAULT_TAGS",
    )
    digitalocean_vpc_uuid: Optional[str] = Field(
        default=None, alias="BOSS_DIGITALOCEAN_VPC_UUID"
    )
    digitalocean_ssh_key_ids: str = Field(
        default="", alias="BOSS_DIGITALOCEAN_SSH_KEY_IDS"
    )
    email_real_send_allowed: bool = Field(
        default=False, alias="BOSS_EMAIL_REAL_SEND_ALLOWED"
    )
    email_smtp_host: Optional[str] = Field(
        default=None, alias="BOSS_EMAIL_SMTP_HOST"
    )
    email_smtp_port: int = Field(default=587, alias="BOSS_EMAIL_SMTP_PORT")
    email_smtp_username: Optional[str] = Field(
        default=None, alias="BOSS_EMAIL_SMTP_USERNAME"
    )
    email_smtp_password: Optional[str] = Field(
        default=None, alias="BOSS_EMAIL_SMTP_PASSWORD"
    )
    email_smtp_use_tls: bool = Field(
        default=True, alias="BOSS_EMAIL_SMTP_USE_TLS"
    )
    email_smtp_use_ssl: bool = Field(
        default=False, alias="BOSS_EMAIL_SMTP_USE_SSL"
    )
    email_from_address: Optional[str] = Field(
        default=None, alias="BOSS_EMAIL_FROM_ADDRESS"
    )
    email_from_name: Optional[str] = Field(
        default=None, alias="BOSS_EMAIL_FROM_NAME"
    )
    require_api_auth: bool = Field(default=False, alias="BOSS_REQUIRE_API_AUTH")
    api_keys: str = Field(default="", alias="BOSS_API_KEYS")
    api_key_principals: str = Field(
        default="",
        alias="BOSS_API_KEY_PRINCIPALS",
    )
    oidc_enabled: bool = Field(default=False, alias="BOSS_OIDC_ENABLED")
    oidc_issuer: Optional[str] = Field(default=None, alias="BOSS_OIDC_ISSUER")
    oidc_audience: Optional[str] = Field(default=None, alias="BOSS_OIDC_AUDIENCE")
    oidc_jwks_url: Optional[str] = Field(default=None, alias="BOSS_OIDC_JWKS_URL")
    oidc_principal_claim: str = Field(
        default="boss_member_id",
        alias="BOSS_OIDC_PRINCIPAL_CLAIM",
    )
    oidc_allowed_algorithms: str = Field(
        default="RS256,ES256",
        alias="BOSS_OIDC_ALLOWED_ALGORITHMS",
    )
    oidc_subject_principals: str = Field(
        default="",
        alias="BOSS_OIDC_SUBJECT_PRINCIPALS",
    )
    openai_model: str = Field(default="gpt-5.2-codex", alias="BOSS_OPENAI_MODEL")
    openai_codex_oauth_enabled: bool = Field(
        default=False, alias="BOSS_OPENAI_CODEX_OAUTH_ENABLED"
    )
    openai_codex_oauth_session_ref: Optional[str] = Field(
        default=None, alias="BOSS_OPENAI_CODEX_OAUTH_SESSION_REF"
    )
    anthropic_claude_oauth_enabled: bool = Field(
        default=False, alias="BOSS_ANTHROPIC_CLAUDE_OAUTH_ENABLED"
    )
    anthropic_claude_oauth_session_ref: Optional[str] = Field(
        default=None, alias="BOSS_ANTHROPIC_CLAUDE_OAUTH_SESSION_REF"
    )
    xai_grok_oauth_enabled: bool = Field(
        default=False, alias="BOSS_XAI_GROK_OAUTH_ENABLED"
    )
    xai_grok_oauth_session_ref: Optional[str] = Field(
        default=None, alias="BOSS_XAI_GROK_OAUTH_SESSION_REF"
    )
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
    boss_edition: str = Field(default="community", alias="BOSS_EDITION")
    account_connect_url: Optional[str] = Field(
        default=None, alias="BOSS_ACCOUNT_CONNECT_URL"
    )
    account_connect_token: Optional[str] = Field(
        default=None, alias="BOSS_ACCOUNT_CONNECT_TOKEN"
    )
    account_connect_timeout_seconds: int = Field(
        default=10, alias="BOSS_ACCOUNT_CONNECT_TIMEOUT_SECONDS"
    )
    enterprise_scim_bearer_tokens: str = Field(
        default="", alias="BOSS_ENTERPRISE_SCIM_BEARER_TOKENS"
    )
    cursor_cli_enabled: bool = Field(default=False, alias="BOSS_CURSOR_CLI_ENABLED")
    local_model_enabled: bool = Field(default=False, alias="BOSS_LOCAL_MODEL_ENABLED")
    local_model_id: Optional[str] = Field(
        default=None, alias="BOSS_LOCAL_MODEL_ID"
    )
    enterprise_model_gateway_enabled: bool = Field(
        default=False, alias="BOSS_ENTERPRISE_MODEL_GATEWAY_ENABLED"
    )
    enterprise_model_gateway_url: Optional[str] = Field(
        default=None, alias="BOSS_ENTERPRISE_MODEL_GATEWAY_URL"
    )
    enterprise_model_gateway_token: Optional[str] = Field(
        default=None, alias="BOSS_ENTERPRISE_MODEL_GATEWAY_TOKEN"
    )
    enterprise_model_gateway_models: str = Field(
        default="enterprise-gateway-auto",
        alias="BOSS_ENTERPRISE_MODEL_GATEWAY_MODELS",
    )
    managed_cloud_model_gateway_enabled: bool = Field(
        default=False, alias="BOSS_MANAGED_CLOUD_MODEL_GATEWAY_ENABLED"
    )
    managed_cloud_model_gateway_models: str = Field(
        default="managed-cloud-gateway-auto",
        alias="BOSS_MANAGED_CLOUD_MODEL_GATEWAY_MODELS",
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
    comms_quiet_hours_enabled: bool = Field(
        default=False, alias="BOSS_COMMS_QUIET_HOURS_ENABLED"
    )
    comms_quiet_hours_start_utc: str = Field(
        default="22:00", alias="BOSS_COMMS_QUIET_HOURS_START_UTC"
    )
    comms_quiet_hours_end_utc: str = Field(
        default="07:00", alias="BOSS_COMMS_QUIET_HOURS_END_UTC"
    )
    comms_throttle_max_queued_per_hour: int = Field(
        default=0, alias="BOSS_COMMS_THROTTLE_MAX_QUEUED_PER_HOUR"
    )
    comms_throttle_defer_minutes: int = Field(
        default=15, alias="BOSS_COMMS_THROTTLE_DEFER_MINUTES"
    )
    slack_real_send_allowed: bool = Field(
        default=False, alias="BOSS_SLACK_REAL_SEND_ALLOWED"
    )
    enable_sandbox_merge: bool = Field(
        default=False, alias="BOSS_ENABLE_SANDBOX_MERGE"
    )
    enable_sandbox_deploy: bool = Field(
        default=False, alias="BOSS_ENABLE_SANDBOX_DEPLOY"
    )
    sandbox_merge_method: str = Field(
        default="squash", alias="BOSS_SANDBOX_MERGE_METHOD"
    )
    sandbox_deploy_environment: str = Field(
        default="staging", alias="BOSS_SANDBOX_DEPLOY_ENVIRONMENT"
    )
    sandbox_deploy_healthcheck_url: Optional[str] = Field(
        default=None, alias="BOSS_SANDBOX_DEPLOY_HEALTHCHECK_URL"
    )
    sandbox_deploy_healthcheck_timeout_seconds: int = Field(
        default=10, alias="BOSS_SANDBOX_DEPLOY_HEALTHCHECK_TIMEOUT_SECONDS"
    )
    sandbox_ci_timeout_seconds: int = Field(
        default=120, alias="BOSS_SANDBOX_CI_TIMEOUT_SECONDS"
    )
    sandbox_ci_poll_interval_seconds: int = Field(
        default=5, alias="BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS"
    )
    sandbox_local_validation_enabled: bool = Field(
        default=False, alias="BOSS_SANDBOX_LOCAL_VALIDATION_ENABLED"
    )
    sandbox_validation_repo_path: Optional[Path] = Field(
        default=None, alias="BOSS_SANDBOX_VALIDATION_REPO_PATH"
    )
    sandbox_validation_commands: str = Field(
        default="", alias="BOSS_SANDBOX_VALIDATION_COMMANDS"
    )
    sandbox_validation_allowed_commands: str = Field(
        default="pnpm,npm,node,python,python3,pytest,make",
        alias="BOSS_SANDBOX_VALIDATION_ALLOWED_COMMANDS",
    )
    sandbox_validation_timeout_seconds: int = Field(
        default=120, alias="BOSS_SANDBOX_VALIDATION_TIMEOUT_SECONDS"
    )
    sandbox_validation_output_limit_chars: int = Field(
        default=12000, alias="BOSS_SANDBOX_VALIDATION_OUTPUT_LIMIT_CHARS"
    )
    sandbox_validation_fetch_remote: bool = Field(
        default=True, alias="BOSS_SANDBOX_VALIDATION_FETCH_REMOTE"
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

    @property
    def api_key_values(self) -> tuple[str, ...]:
        return tuple(
            key.strip()
            for key in self.api_keys.replace("\n", ",").split(",")
            if key.strip()
        )

    @property
    def api_key_principal_map(self) -> dict[str, str]:
        principal_map: dict[str, str] = {}
        for entry in self.api_key_principals.replace("\n", ",").split(","):
            cleaned = entry.strip()
            if not cleaned:
                continue
            delimiter = "=" if "=" in cleaned else ":"
            token, _, member_id = cleaned.partition(delimiter)
            if token.strip() and member_id.strip():
                principal_map[token.strip()] = member_id.strip()
        return principal_map

    @property
    def oidc_algorithm_values(self) -> tuple[str, ...]:
        return tuple(
            algorithm.strip()
            for algorithm in self.oidc_allowed_algorithms.replace("\n", ",").split(",")
            if algorithm.strip()
        )

    @property
    def oidc_subject_principal_map(self) -> dict[str, str]:
        principal_map: dict[str, str] = {}
        for entry in self.oidc_subject_principals.replace("\n", ",").split(","):
            cleaned = entry.strip()
            if not cleaned:
                continue
            delimiter = "=" if "=" in cleaned else ":"
            subject, _, member_id = cleaned.partition(delimiter)
            if subject.strip() and member_id.strip():
                principal_map[subject.strip()] = member_id.strip()
        return principal_map

    @property
    def oidc_configured(self) -> bool:
        return bool(
            self.oidc_issuer
            and self.oidc_audience
            and self.oidc_jwks_url
            and self.oidc_algorithm_values
        )

    @property
    def enterprise_scim_bearer_token_values(self) -> tuple[str, ...]:
        return tuple(
            token.strip()
            for token in self.enterprise_scim_bearer_tokens.replace("\n", ",").split(",")
            if token.strip()
        )

    @property
    def enterprise_model_gateway_model_values(self) -> tuple[str, ...]:
        return tuple(
            model.strip()
            for model in self.enterprise_model_gateway_models.replace("\n", ",").split(",")
            if model.strip()
        )

    @property
    def managed_cloud_model_gateway_model_values(self) -> tuple[str, ...]:
        return tuple(
            model.strip()
            for model in self.managed_cloud_model_gateway_models.replace("\n", ",").split(",")
            if model.strip()
        )

    @property
    def digitalocean_tag_values(self) -> tuple[str, ...]:
        return tuple(
            value.strip()
            for value in self.digitalocean_default_tags.replace("\n", ",").split(",")
            if value.strip()
        )

    @property
    def digitalocean_ssh_key_id_values(self) -> tuple[int, ...]:
        values: list[int] = []
        for raw in self.digitalocean_ssh_key_ids.replace("\n", ",").split(","):
            cleaned = raw.strip()
            if not cleaned:
                continue
            try:
                values.append(int(cleaned))
            except ValueError:
                continue
        return tuple(values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
