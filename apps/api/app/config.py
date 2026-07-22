from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+psycopg://boss:boss@localhost:5432/boss",
        alias="DATABASE_URL",
    )
    app_base_url: Optional[str] = Field(default=None, alias="APP_BASE_URL")
    admin_url: Optional[str] = Field(default=None, alias="ADMIN_URL")
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")
    sentry_dsn: Optional[str] = Field(default=None, alias="SENTRY_DSN")
    supabase_url: Optional[str] = Field(default=None, alias="SUPABASE_URL")
    supabase_anon_key: Optional[str] = Field(default=None, alias="SUPABASE_ANON_KEY")
    supabase_service_role_key: Optional[str] = Field(
        default=None, alias="SUPABASE_SERVICE_ROLE_KEY"
    )
    s3_endpoint: Optional[str] = Field(default=None, alias="S3_ENDPOINT")
    s3_access_key_id: Optional[str] = Field(default=None, alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: Optional[str] = Field(default=None, alias="S3_SECRET_ACCESS_KEY")
    s3_bucket: Optional[str] = Field(default=None, alias="S3_BUCKET")
    s3_region: Optional[str] = Field(default=None, alias="S3_REGION")
    temporal_address: Optional[str] = Field(default=None, alias="TEMPORAL_ADDRESS")
    temporal_namespace: Optional[str] = Field(default=None, alias="TEMPORAL_NAMESPACE")
    nextcloud_url: Optional[str] = Field(default=None, alias="NEXTCLOUD_URL")
    webdav_url: Optional[str] = Field(default=None, alias="WEBDAV_URL")
    caldav_url: Optional[str] = Field(default=None, alias="CALDAV_URL")
    carddav_url: Optional[str] = Field(default=None, alias="CARDDAV_URL")
    nextcloud_username: Optional[str] = Field(default=None, alias="NEXTCLOUD_USERNAME")
    nextcloud_password: Optional[str] = Field(default=None, alias="NEXTCLOUD_PASSWORD")
    docuseal_url: Optional[str] = Field(default=None, alias="DOCUSEAL_URL")
    docuseal_api_url: Optional[str] = Field(default=None, alias="DOCUSEAL_API_URL")
    docuseal_token: Optional[str] = Field(default=None, alias="DOCUSEAL_TOKEN")
    map_tile_url: Optional[str] = Field(default=None, alias="MAP_TILE_URL")
    map_style_url: Optional[str] = Field(default=None, alias="MAP_STYLE_URL")
    routing_url: Optional[str] = Field(default=None, alias="ROUTING_URL")
    umami_url: Optional[str] = Field(default=None, alias="UMAMI_URL")
    umami_website_id: Optional[str] = Field(default=None, alias="UMAMI_WEBSITE_ID")
    umami_script_url: Optional[str] = Field(default=None, alias="UMAMI_SCRIPT_URL")
    posthog_host: Optional[str] = Field(default=None, alias="POSTHOG_HOST")
    posthog_key: Optional[str] = Field(default=None, alias="POSTHOG_KEY")
    n8n_url: Optional[str] = Field(default=None, alias="N8N_URL")
    n8n_webhook_url: Optional[str] = Field(default=None, alias="N8N_WEBHOOK_URL")
    meili_url: Optional[str] = Field(default=None, alias="MEILI_URL")
    meili_api_key: Optional[str] = Field(default=None, alias="MEILI_API_KEY")
    meili_search_key: Optional[str] = Field(default=None, alias="MEILI_SEARCH_KEY")
    typebot_viewer_url: Optional[str] = Field(default=None, alias="TYPEBOT_VIEWER_URL")
    typebot_builder_url: Optional[str] = Field(default=None, alias="TYPEBOT_BUILDER_URL")
    admin_username: Optional[str] = Field(default=None, alias="ADMIN_USERNAME")
    admin_email: Optional[str] = Field(default=None, alias="ADMIN_EMAIL")
    admin_password: Optional[str] = Field(default=None, alias="ADMIN_PASSWORD")
    openai_api_key: Optional[str] = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: Optional[str] = Field(default=None, alias="OPENAI_BASE_URL")
    azure_openai_key: Optional[str] = Field(default=None, alias="AZURE_OPENAI_KEY")
    azure_openai_endpoint: Optional[str] = Field(
        default=None, alias="AZURE_OPENAI_ENDPOINT"
    )
    azure_openai_deployment: Optional[str] = Field(
        default=None, alias="AZURE_OPENAI_DEPLOYMENT"
    )
    azure_openai_api_version: str = Field(
        default="2024-12-01-preview", alias="AZURE_OPENAI_API_VERSION"
    )
    plan_budget_usd_micros: int = Field(
        default=1_500_000, alias="BOSS_PLAN_BUDGET_USD_MICROS"
    )
    agent_repo_root: str = Field(default=".", alias="BOSS_AGENT_REPO_ROOT")
    agent_max_steps: int = Field(default=12, alias="BOSS_AGENT_MAX_STEPS")
    agent_autonomous_risk_max: int = Field(
        default=1, alias="BOSS_AGENT_AUTONOMOUS_RISK_MAX"
    )
    agent_router_llm_enabled: bool = Field(
        default=False, alias="BOSS_AGENT_ROUTER_LLM_ENABLED"
    )
    agent_router_budget_usd_micros: int = Field(
        default=100_000, alias="BOSS_AGENT_ROUTER_BUDGET_USD_MICROS", ge=1
    )
    # Completion backend: auto (azure→openai) | openai | azure | claude_cli.
    # claude_cli drives BOSS's own loop but serves each completion via the
    # Claude Code CLI (`claude -p`) on a Max subscription instead of a paid API.
    llm_engine: str = Field(default="auto", alias="BOSS_LLM_ENGINE")
    claude_cli_path: str = Field(default="claude", alias="BOSS_CLAUDE_CLI_PATH")
    claude_cli_model: Optional[str] = Field(default=None, alias="BOSS_CLAUDE_CLI_MODEL")
    claude_cli_timeout: int = Field(default=240, alias="BOSS_CLAUDE_CLI_TIMEOUT")

    @model_validator(mode="after")
    def _azure_satisfies_openai_credential(self):
        # When Azure OpenAI is configured, treat the OpenAI source as
        # credential-present so the entitlement broker routes to it. The
        # adapter then dispatches to Azure internally.
        if not self.openai_api_key and self.azure_openai_key and self.azure_openai_endpoint:
            self.openai_api_key = self.azure_openai_key
        # The Claude-CLI engine has no API key but IS a real model backend, so
        # mark the credential present so intake routes to the real-model path.
        if (self.llm_engine or "").lower() == "claude_cli" and not self.openai_api_key:
            self.openai_api_key = "claude-cli-proxy"
        return self

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
    # AWS provider adapter (Resource Plane) — secret access key resolved via the
    # provider account SecretRef (env:BOSS_AWS_SECRET_ACCESS_KEY), never stored here.
    aws_access_key_id: Optional[str] = Field(
        default=None, alias="BOSS_AWS_ACCESS_KEY_ID"
    )
    aws_region: str = Field(default="us-east-1", alias="BOSS_AWS_REGION")
    aws_role_arn: Optional[str] = Field(default=None, alias="BOSS_AWS_ROLE_ARN")
    # SSH host (bring-your-own / customer-provided compute) — Docker workload deploys.
    # Gated like DO provisioning; private key held as a SecretRef, never in agent context.
    ssh_host_deploy_allowed: bool = Field(
        default=False, alias="BOSS_SSH_HOST_DEPLOY_ALLOWED"
    )
    ssh_host_private_key: Optional[str] = Field(
        default=None, alias="BOSS_SSH_HOST_PRIVATE_KEY"
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
        default=None,
        validation_alias=AliasChoices("SMTP_HOST", "MAIL_HOST", "BOSS_EMAIL_SMTP_HOST"),
    )
    email_smtp_port: int = Field(
        default=587,
        validation_alias=AliasChoices("SMTP_PORT", "MAIL_PORT", "BOSS_EMAIL_SMTP_PORT"),
    )
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
        default=None,
        validation_alias=AliasChoices("MAIL_FROM", "BOSS_EMAIL_FROM_ADDRESS"),
    )
    email_from_name: Optional[str] = Field(
        default=None, alias="BOSS_EMAIL_FROM_NAME"
    )
    require_api_auth: bool = Field(default=False, alias="BOSS_REQUIRE_API_AUTH")
    bootstrap_seed_api_enabled: bool = Field(
        default=False,
        alias="BOSS_BOOTSTRAP_SEED_API_ENABLED",
    )
    api_keys: str = Field(default="", alias="BOSS_API_KEYS")
    api_key_principals: str = Field(
        default="",
        alias="BOSS_API_KEY_PRINCIPALS",
    )
    oidc_enabled: bool = Field(default=False, alias="BOSS_OIDC_ENABLED")
    oidc_issuer: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("OIDC_ISSUER", "BOSS_OIDC_ISSUER"),
    )
    oidc_audience: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("OIDC_CLIENT_ID", "BOSS_OIDC_AUDIENCE"),
    )
    oidc_client_secret: Optional[str] = Field(default=None, alias="OIDC_CLIENT_SECRET")
    keycloak_url: Optional[str] = Field(default=None, alias="KEYCLOAK_URL")
    keycloak_realm: Optional[str] = Field(default=None, alias="KEYCLOAK_REALM")
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
    openai_model: str = Field(
        default="gpt-5.2-codex",
        validation_alias=AliasChoices("OPENAI_MODEL", "BOSS_OPENAI_MODEL"),
    )
    # Extend/correct the token price table (openai_adapter.PRICE_PER_MTOK) per deployment:
    # a JSON object {"model": [input_usd_per_mtok, output_usd_per_mtok]}. Open-model prices
    # vary by host (DeepSeek direct vs OpenRouter vs Fireworks), so accurate
    # $/verified-outcome accounting (VAG Phase 0) needs deployment-specific overrides.
    model_prices_json: str = Field(default="", alias="BOSS_MODEL_PRICES_JSON")
    # Dedicated OpenRouter lane (VAG cheap models). A SEPARATE credential pair from
    # OPENAI_* on purpose: on managed deployments OPENAI_API_KEY is a broker-shared,
    # platform-managed credential — the 'openrouter' engine must not redirect it.
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    # Direct provider lanes (same dedicated-credential principle as OpenRouter above):
    # groq = free-tier fast serving of open models; deepseek = first-party API (data
    # residency: China-hosted — fine for toy/eval workloads, mind it for customer code).
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_base_url: str = Field(
        default="https://api.groq.com/openai/v1", alias="GROQ_BASE_URL"
    )
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL"
    )
    # Output-token cap for native chat-API calls. Metered gateways (OpenRouter) RESERVE
    # credits for the requested maximum — an uncapped request can 402 on reservation
    # alone even when the actual completion would be affordable. 0 disables the cap.
    native_max_output_tokens: int = Field(
        default=8192, alias="BOSS_NATIVE_MAX_OUTPUT_TOKENS"
    )
    # Headless Claude token for the claude_code guest engine (passed into the guest).
    claude_code_oauth_token: str = Field(default="", alias="CLAUDE_CODE_OAUTH_TOKEN")
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
    github_webhook_secret: Optional[str] = Field(
        default=None, alias="BOSS_GITHUB_WEBHOOK_SECRET"
    )
    github_app_bot_login: str = Field(
        default="boss[bot]", alias="BOSS_GITHUB_APP_BOT_LOGIN"
    )
    github_community_model_review_enabled: bool = Field(
        default=False, alias="BOSS_GITHUB_COMMUNITY_MODEL_REVIEW_ENABLED"
    )
    github_community_model_review_budget_usd_micros: int = Field(
        default=250_000,
        alias="BOSS_GITHUB_COMMUNITY_MODEL_REVIEW_BUDGET_USD_MICROS",
        ge=1,
    )
    webhook_timeout_seconds: float = Field(
        default=10.0, alias="BOSS_WEBHOOK_TIMEOUT_SECONDS", gt=0
    )
    webhook_inbound_secret: str = Field(
        default="", alias="BOSS_WEBHOOK_INBOUND_SECRET"
    )
    webhook_inbound_secrets_json: str = Field(
        default="", alias="BOSS_WEBHOOK_INBOUND_SECRETS_JSON"
    )
    webhook_project_map_json: str = Field(
        default="", alias="BOSS_WEBHOOK_PROJECT_MAP_JSON"
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
    skill_trust_keys: str = Field(default="", alias="BOSS_SKILL_TRUST_KEYS")
    skill_signing_private_key: str = Field(
        default="", alias="BOSS_SKILL_SIGNING_PRIVATE_KEY"
    )
    skill_signing_key_id: str = Field(
        default="", alias="BOSS_SKILL_SIGNING_KEY_ID"
    )
    exclude_untrusted_skills: bool = Field(
        default=False, alias="BOSS_EXCLUDE_UNTRUSTED_SKILLS"
    )
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
    enterprise_saml_enabled: bool = Field(
        default=False, alias="BOSS_ENTERPRISE_SAML_ENABLED"
    )
    enterprise_saml_session_ttl_minutes: int = Field(
        default=480, alias="BOSS_ENTERPRISE_SAML_SESSION_TTL_MINUTES"
    )
    enterprise_saml_member_attribute: str = Field(
        default="boss_member_id", alias="BOSS_ENTERPRISE_SAML_MEMBER_ATTRIBUTE"
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
    pipeline_provider: str = Field(
        default="github_actions", alias="BOSS_PIPELINE_PROVIDER"
    )
    jenkins_base_url: str = Field(default="", alias="BOSS_JENKINS_BASE_URL")
    jenkins_user: str = Field(default="", alias="BOSS_JENKINS_USER")
    jenkins_token: str = Field(default="", alias="BOSS_JENKINS_TOKEN")
    jenkins_job: str = Field(default="", alias="BOSS_JENKINS_JOB")
    gitlab_base_url: str = Field(
        default="https://gitlab.com", alias="BOSS_GITLAB_BASE_URL"
    )
    gitlab_token: str = Field(default="", alias="BOSS_GITLAB_TOKEN")
    gitlab_project_id: str = Field(default="", alias="BOSS_GITLAB_PROJECT_ID")
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
        # Build/test executables a coding task may invoke. Wrapper scripts
        # (./mvnw, ./gradlew) are matched by basename — see EphemeralSandbox.
        default=(
            "pnpm,npm,npx,yarn,node,python,python3,pip,pip3,uv,pytest,tox,"
            "make,cmake,ctest,"
            "mvn,mvnw,gradle,gradlew,java,javac,kotlin,"
            "go,cargo,rustc,dotnet,"
            "ruby,bundle,rake,php,composer,bazel"
        ),
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

    # --- Coding agent (clone -> edit -> build -> test -> fix -> PR) -----------
    # Feature flag for the durable `code.task` pipeline.
    enable_code_task: bool = Field(default=False, alias="BOSS_ENABLE_CODE_TASK")
    # When true, code tasks run through the governed coding pipeline (role-separated
    # stages: qa authors tests -> SE implements -> adversarial review -> fail-closed
    # QualityGate) instead of a single agent. See docs/product/GOVERNED_CODING_PIPELINE.md.
    governed_coding_pipeline: bool = Field(
        default=False, alias="BOSS_GOVERNED_CODING_PIPELINE"
    )
    # When a PRD panel publishes, whether to AUTO-start a governed code task seeded from the whole
    # PRD. Default OFF: a PRD is a multi-feature product spec (and discovery tasks yield a document,
    # not code) — auto-dumping it as one code task mis-routes research/spec work into codegen. With
    # this off, the PRD is the deliverable and a human creates scoped coding tasks explicitly.
    auto_code_task_after_prd: bool = Field(
        default=False, alias="BOSS_AUTO_CODE_TASK_AFTER_PRD"
    )
    # Cost/rigor dial for the governed pipeline: lean | balanced | strict (or a custom profile
    # name once the profiles API lands). Scales which steps run and which models run them.
    pipeline_profile: str = Field(default="balanced", alias="BOSS_PIPELINE_PROFILE")
    # Per-step model routing for the pipeline: low-cost steps (write tests, checks) can use a
    # smaller model; hard steps (implement, review) use the strong one. Empty => use the
    # engine's default model for every step (routing is a safe no-op until configured).
    code_agent_low_cost_model: str = Field(default="", alias="BOSS_CODE_AGENT_LOW_COST_MODEL")
    code_agent_strong_model: str = Field(default="", alias="BOSS_CODE_AGENT_STRONG_MODEL")
    # VAG B2: bind a model CLASS to an ENGINE, so "low_cost" can mean a different provider
    # entirely (e.g. low_cost=native/deepseek-chat while strong stays claude_code). Empty =
    # the step's lane engine (build vs review) — exactly the pre-B2 behavior.
    code_agent_low_cost_engine: str = Field(default="", alias="BOSS_CODE_AGENT_LOW_COST_ENGINE")
    code_agent_strong_engine: str = Field(default="", alias="BOSS_CODE_AGENT_STRONG_ENGINE")
    # Lane capacity for the review panel: how many read-only checks run concurrently. Extra checks
    # queue for a free slot (the entitlement broker authorises which model lane each one uses).
    code_agent_max_concurrency: int = Field(default=3, alias="BOSS_CODE_AGENT_MAX_CONCURRENCY")
    # Rate-limit-aware model-call pacing (#111) — backpressure on the model lane so a burst
    # can't trip a provider's rolling rate limit (e.g. Anthropic's 5-hour cap) or overflow to
    # paid usage credits. Default PACED; flip burst_mode on to run wide-open (customers who
    # don't care about usage limits). See services/model_rate_limiter.py.
    #   burst_mode        = true -> no limiting at all (NullRateLimiter).
    #   max_concurrency   = global cap on IN-FLIGHT model calls per provider (AIMD ceiling).
    #   min_interval_ms   = minimum spacing between call STARTS per provider (0 = concurrency-only).
    #   rate_backoff_secs = default backoff after a rate/usage-limit signal (Retry-After wins).
    model_burst_mode: bool = Field(default=False, alias="BOSS_MODEL_BURST_MODE")
    model_max_concurrency: int = Field(default=3, alias="BOSS_MODEL_MAX_CONCURRENCY")
    model_min_interval_ms: int = Field(default=0, alias="BOSS_MODEL_MIN_INTERVAL_MS")
    model_rate_backoff_seconds: int = Field(default=30, alias="BOSS_MODEL_RATE_BACKOFF_SECONDS")
    # Which CodeAgent implementation runs the edit/test loop:
    #   claude_code = wrap `claude -p` headless WITH its file/bash tools enabled,
    #                 scoped to the ephemeral sandbox cwd (uses the Max plan).
    #   native      = (Phase 1) raw model API agent on the Agent SDK.
    code_agent_engine: str = Field(default="claude_code", alias="BOSS_CODE_AGENT_ENGINE")
    # Engine for the ADVERSARY roles (code review / security / consistency / mutation) in the
    # governed pipeline. Defaulting it to a DIFFERENT model than the builder removes self-review
    # bias — the model that wrote the code isn't the one judging it. Empty = same as the builder
    # (single-model). e.g. builder=claude_code, reviewer=native (Azure GPT).
    code_review_engine: str = Field(default="", alias="BOSS_CODE_REVIEW_ENGINE")
    # VAG Phase 1 (verifier hardening / #9): the adversarial code-review stage runs as an
    # N-member REFUTE PANEL — k independent reviewers, each with fresh context, each trying to
    # refute the change. The panel verdict is by quorum and yields a CONFIDENCE score
    # (fraction of reviewers that could NOT refute). Fail-closed: below quorum -> refuted -> gate
    # fails. Ng "Agentic Design Patterns: Reflection" + self-consistency: a single critic is
    # noisy; independent votes de-noise and expose blind spots one critic shares with itself.
    #   panel_size = 1  -> identical to the pre-#9 single-reviewer behavior.
    #   quorum     = min survived-fraction to pass (0.5 = simple majority).
    #   panel_engines = optional comma list of engines to spread reviewers across for MODEL
    #                   DIVERSITY (perspective-diverse verify); empty = all use code_review_engine.
    code_review_panel_size: int = Field(default=1, alias="BOSS_CODE_REVIEW_PANEL_SIZE")
    code_review_panel_quorum: float = Field(default=0.5, alias="BOSS_CODE_REVIEW_PANEL_QUORUM")
    code_review_panel_engines: str = Field(default="", alias="BOSS_CODE_REVIEW_PANEL_ENGINES")
    # AUTONOMOUS REVIEW-REPAIR (BOSS's moat: agents resolve issues in a loop, not a human). When the
    # quality gate fails, feed the findings back into the build and re-review up to this many rounds
    # BEFORE blocking for a human. 0 = legacy (block on first fail).
    code_review_repair_max_rounds: int = Field(
        default=2, alias="BOSS_CODE_REVIEW_REPAIR_MAX_ROUNDS"
    )
    # Max agent turns inside the sandbox before we stop and take the diff.
    code_agent_max_turns: int = Field(default=40, alias="BOSS_CODE_AGENT_MAX_TURNS")
    # Read-only stages (adversarial/security/consistency review) don't edit code and don't
    # need 40 turns of exploration — a tighter cap cuts review latency a lot without losing
    # rigor (they read the diff + relevant files + judge).
    code_agent_review_max_turns: int = Field(default=16, alias="BOSS_CODE_AGENT_REVIEW_MAX_TURNS")
    # Hard wall-clock deadline for the whole review panel AND for a single read-only review
    # invocation — so a slow/rate-limited review (e.g. an OpenAI-gateway retry storm) can't
    # blow the review phase out to 20min. On deadline a check degrades to 'not_run' (security
    # stays strict; adversarial/consistency proceed) rather than hanging or crashing.
    code_agent_review_timeout_seconds: int = Field(
        default=420, alias="BOSS_CODE_AGENT_REVIEW_TIMEOUT_SECONDS"
    )
    # Wall-clock cap for a single CodeAgent invocation.
    code_agent_timeout_seconds: int = Field(
        default=900, alias="BOSS_CODE_AGENT_TIMEOUT_SECONDS"
    )
    # Tools the sandboxed Claude Code agent may use (its OWN native tools here —
    # the inverse of claude_cli_adapter, which denies them). Bash is constrained
    # to the sandbox cwd; network egress is the deployment's responsibility.
    code_agent_allowed_tools: str = Field(
        default="Read,Edit,MultiEdit,Write,Glob,Grep,Bash,TodoWrite",
        alias="BOSS_CODE_AGENT_ALLOWED_TOOLS",
    )
    # VAG Phase 3 repair loop: after a failed gate check, feed the CONCRETE evidence
    # (failing test output, review refutation, security finding) back to the implementer
    # and retry, up to this many times. The verification bar never moves — tests and the
    # review panel stay frozen/independent — so retries can only earn passes, never
    # soften the gate. 0 = off (single-pass, pre-Phase-3 behavior). Executable checks
    # re-run per retry (cheap); the frontier panel re-judges only candidates whose tests
    # pass. Per-run override: spec["repair_retries"].
    code_repair_max_retries: int = Field(default=0, alias="BOSS_CODE_REPAIR_MAX_RETRIES")
    # VAG #105 verifier meta-eval: the quality bar the VERIFIER itself must clear when run over
    # the labeled bugged/clean corpus. min_recall = floor on bug-detection (catch bugs); max_fp_rate
    # = ceiling on false alarms (don't reject clean code). A verifier below the bar fails the gate.
    verifier_meta_min_recall: float = Field(
        default=0.8, alias="BOSS_VERIFIER_META_MIN_RECALL"
    )
    verifier_meta_max_fp_rate: float = Field(
        default=0.2, alias="BOSS_VERIFIER_META_MAX_FP_RATE"
    )
    # VAG #106 mutation gate on the FROZEN TESTS: plant deterministic AST mutations in the impl and
    # require the tests to kill them. min_score = floor on mutation score (killed/run); a surviving
    # mutant is a bug-class the suite can't catch. max_mutants caps cost/time (one test run each);
    # require_property_tests optionally blocks a suite with no Hypothesis property tests.
    mutation_min_score: float = Field(default=0.8, alias="BOSS_MUTATION_MIN_SCORE")
    mutation_max_mutants: int = Field(default=30, alias="BOSS_MUTATION_MAX_MUTANTS")
    mutation_timeout_seconds: int = Field(default=30, alias="BOSS_MUTATION_TIMEOUT_SECONDS")
    mutation_require_property_tests: bool = Field(
        default=False, alias="BOSS_MUTATION_REQUIRE_PROPERTY_TESTS"
    )
    # VAG #110 generator/verifier independence contract: the minimum separation the review panel must
    # have from the builder — off | model (diff model) | family (diff lineage, the default) | provider
    # (diff vendor). A reviewer sharing the builder's family shares its blind spots.
    independence_required_level: str = Field(
        default="family", alias="BOSS_INDEPENDENCE_REQUIRED_LEVEL"
    )
    # VAG #108 bounded generator↔verifier debate: when the panel's confidence lands in the contested
    # band (low, high) — neither a clear survive nor refute — resolve it with a short debate instead of
    # a fail-closed guess. max_rounds caps the cost (each round is up to two model calls).
    debate_max_rounds: int = Field(default=2, alias="BOSS_DEBATE_MAX_ROUNDS")
    debate_contested_low: float = Field(default=0.34, alias="BOSS_DEBATE_CONTESTED_LOW")
    debate_contested_high: float = Field(default=0.66, alias="BOSS_DEBATE_CONTESTED_HIGH")
    # VAG #109 budget-variable repair: escalate the implementer from the cheap tier to the strong tier
    # after this many failed cheap repair attempts (nothing verified = infinite $/verified). The repair
    # DEPTH is what the remaining budget affords (capped by code_repair_max_retries), using this per-
    # attempt cost estimate for the affordability math.
    repair_escalate_after: int = Field(default=1, alias="BOSS_REPAIR_ESCALATE_AFTER")
    repair_est_attempt_cost_usd_micros: int = Field(
        default=200_000, alias="BOSS_REPAIR_EST_ATTEMPT_COST_USD_MICROS"
    )
    # APP-6 (#96) hybrid design-axis routing: spend frontier tokens where design (taste/hierarchy/copy)
    # is decided and cheap OSS on the verified fill-in. Empty engines fall back to strong/low-cost.
    # The design pass is metered as a distinct budget line item (fraction of the task budget) capped at
    # max_passes frontier passes before falling back to OSS.
    design_frontier_engine: str = Field(default="", alias="BOSS_DESIGN_FRONTIER_ENGINE")
    design_oss_engine: str = Field(default="", alias="BOSS_DESIGN_OSS_ENGINE")
    design_pass_budget_fraction: float = Field(default=0.4, alias="BOSS_DESIGN_PASS_BUDGET_FRACTION")
    design_max_passes: int = Field(default=2, alias="BOSS_DESIGN_MAX_PASSES")
    design_est_pass_cost_usd_micros: int = Field(
        default=300_000, alias="BOSS_DESIGN_EST_PASS_COST_USD_MICROS"
    )
    # Per-task budget cap for a code task (model spend), reserved before work.
    code_task_budget_usd_micros: int = Field(
        default=2_000_000, alias="BOSS_CODE_TASK_BUDGET_USD_MICROS"
    )
    # Reactive documentation work is deliberately bounded to one small drafting call.
    # The handler reserves this envelope before invoking a model or writing to GitHub.
    docs_update_budget_usd_micros: int = Field(
        default=75_000, alias="BOSS_DOCS_UPDATE_BUDGET_USD_MICROS"
    )
    docs_trigger_events: str = Field(
        default=(
            "code_task.pr_opened,software_factory.pull_request_merged,"
            "discovery.completed"
        ),
        alias="BOSS_DOCS_TRIGGER_EVENTS",
    )
    docs_debounce_seconds: int = Field(
        default=300, alias="BOSS_DOCS_DEBOUNCE_SECONDS"
    )

    # --- Plane <-> BOSS integration (inbound webhook -> auto code-task) --------
    # A human files a Plane issue labelled boss:coding|boss:bug|boss:design; Plane fires a
    # signed webhook -> we verify it, filter (watched project + boss: label + created),
    # map the issue -> start_code_task, and (Phase 2) sync state back via the REST API.
    # See docs/infra/BOSS_PLANE_INTEGRATION_SPEC.md. Fail-closed: OFF until configured.
    plane_integration_enabled: bool = Field(
        default=False, alias="PLANE_INTEGRATION_ENABLED"
    )
    # Base URL of the Plane instance, e.g. https://plane.local.sean.network (outbound Phase 2).
    plane_base_url: Optional[str] = Field(default=None, alias="PLANE_BASE_URL")
    # Plane workspace slug (for the REST API that resolves webhook label UUIDs -> names). Derived from
    # PLANE_PROJECT_URL when unset.
    plane_workspace: str = Field(default="goldeneye", alias="PLANE_WORKSPACE")
    # Full project URL already provisioned in the ops store (workspace + project id live here);
    # base_url is derived from it when PLANE_BASE_URL is unset.
    plane_project_url: Optional[str] = Field(default=None, alias="PLANE_PROJECT_URL")
    # Outbound REST token. The ops store provisioned this as PLANE_API_KEY; accept the
    # spec's PLANE_API_TOKEN name too so either env wins.
    plane_api_token: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("PLANE_API_KEY", "PLANE_API_TOKEN")
    )
    # HMAC secret Plane signs each delivery with (X-Plane-Signature). Inbound requests whose
    # signature doesn't match are rejected. Absent => the webhook endpoint stays fail-closed.
    plane_webhook_secret: Optional[str] = Field(
        default=None, alias="PLANE_WEBHOOK_SECRET"
    )
    # JSON map: {"<plane_project_id>": {"owner": "...", "repo": "..."}} — routes a ticket to the
    # GitHub repo its PR opens against. Parsed defensively at use (a bad value never crashes boot).
    plane_project_map: Optional[str] = Field(default=None, alias="PLANE_PROJECT_MAP")
    # Comma-separated labels that arm BOSS. Only an issue carrying one of these ever starts a job.
    plane_watch_labels: str = Field(
        default="boss:coding,boss:bug,boss:design", alias="PLANE_WATCH_LABELS"
    )

    # Execution substrate for the GOVERNED pipeline's working stages (write_tests /
    # implement / regression / mutation — the stages that EXECUTE generated code):
    #   local   = host-side EphemeralSandbox subprocesses (pre-hybrid behavior)
    #   microvm = each working stage runs in its own Firecracker VM via the
    #             sandbox-service; the control plane commits each stage's diff to the
    #             work branch (git is the state carrier) and keeps ALL judgment —
    #             design, review panel, gates, Decision, PR — host-side.
    governed_sandbox_provider: str = Field(
        default="local", alias="BOSS_GOVERNED_SANDBOX_PROVIDER"
    )
    # --- IaC verify lane (spec 05 §3.2 / T3) --------------------------------
    # Terraform-compatible binary for the IaC verify lane. "auto" prefers OpenTofu
    # (`tofu`, the MPL-licensed choice — spec 05 Open Decision #2) and falls back to
    # HashiCorp `terraform`; enterprises standardized on one can pin it explicitly.
    iac_tf_binary: str = Field(default="auto", alias="BOSS_IAC_TF_BINARY")
    # Security/compliance scanner for IaC changes: checkov (default) or tfsec. If the
    # preferred scanner is not installed the lane falls back to the other one; with
    # neither installed the security gate fails closed (an IaC change is never allowed
    # through unscanned).
    iac_security_scanner: str = Field(default="checkov", alias="BOSS_IAC_SECURITY_SCANNER")
    # plan-live (scoped read-only creds against a real provider) is NOT part of the
    # default lane. Even when enabled it requires an approval Decision before creds are
    # used; the lane currently records it as a skipped placeholder step (T3 scope —
    # spec 05 §3.2 step 5).
    iac_plan_live_enabled: bool = Field(default=False, alias="BOSS_IAC_PLAN_LIVE_ENABLED")
    # Which SandboxProvider supplies the execution substrate:
    #   local   = per-task EphemeralSandbox as a local subprocess + temp clone (Phase 0/1)
    #   microvm = single-tenant sandbox on a Sandbox Host (container stand-in now,
    #             Kata/Firecracker microVM later); the whole pipeline runs guest-side
    sandbox_provider: str = Field(default="local", alias="BOSS_SANDBOX_PROVIDER")
    # The Sandbox Agent (host service) the microvm provider dispatches tasks to.
    sandbox_agent_url: str = Field(default="http://127.0.0.1:8088", alias="BOSS_SANDBOX_AGENT_URL")
    sandbox_agent_timeout_seconds: int = Field(default=1500, alias="BOSS_SANDBOX_AGENT_TIMEOUT_SECONDS")
    # Shared bearer for the sandbox-service (sent as X-Sandbox-Token). Empty => no auth header.
    sandbox_token: str = Field(default="", alias="BOSS_SANDBOX_TOKEN")
    # Poll cadence while waiting on an async micro-VM task (§2 submit -> poll -> release).
    sandbox_poll_interval_seconds: int = Field(default=5, alias="BOSS_SANDBOX_POLL_INTERVAL_SECONDS")
    # Coding-agent engine that runs INSIDE the sandbox (echo | openai | claude_code).
    # openai = native agent on the raw model API (no CLI/keychain — works headless).
    sandbox_guest_engine: str = Field(default="openai", alias="BOSS_SANDBOX_GUEST_ENGINE")
    # Root dir under which per-task ephemeral sandboxes are cloned and torn down.
    sandbox_root_path: Optional[Path] = Field(
        default=None, alias="BOSS_SANDBOX_ROOT_PATH"
    )
    # Shallow-clone depth for the ephemeral sandbox.
    sandbox_clone_depth: int = Field(default=1, alias="BOSS_SANDBOX_CLONE_DEPTH")

    # --- GitHub App (Phase 2: developer impersonation, revocable) ------------
    # When set, github_app_adapter mints short-lived, repo-scoped installation
    # tokens instead of using the long-lived PAT (github_token).
    github_app_id: Optional[str] = Field(default=None, alias="BOSS_GITHUB_APP_ID")
    github_app_private_key: Optional[str] = Field(
        default=None, alias="BOSS_GITHUB_APP_PRIVATE_KEY"
    )
    github_app_private_key_path: Optional[Path] = Field(
        default=None, alias="BOSS_GITHUB_APP_PRIVATE_KEY_PATH"
    )
    github_app_installation_id: Optional[str] = Field(
        default=None, alias="BOSS_GITHUB_APP_INSTALLATION_ID"
    )

    # Simple rate limiting for high-cost paths (P2 per .ai/security-review.md).
    # In-memory implementation only (see main.py); production requires Redis or
    # edge limiter (nginx, CDN, etc). Default protects metatron/query, commands/propose,
    # resources/sync etc at 10 req/min per (IP+actor) key. Can disable or tune via env.
    rate_limit_enabled: bool = Field(
        default=True, alias="BOSS_RATE_LIMIT_ENABLED"
    )
    rate_limit_requests_per_minute: int = Field(
        default=10, alias="BOSS_RATE_LIMIT_RPM"
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
    def plane_watch_label_values(self) -> tuple[str, ...]:
        """The boss: labels that arm the Plane integration, split from the comma/newline string."""
        return tuple(
            value.strip()
            for value in self.plane_watch_labels.replace("\n", ",").split(",")
            if value.strip()
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
