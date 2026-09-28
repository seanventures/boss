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
    a2a_redis_prefix: str = Field(default="boss:a2a", alias="BOSS_A2A_REDIS_PREFIX")
    a2a_default_ttl_seconds: int = Field(
        default=3600, alias="BOSS_A2A_DEFAULT_TTL_SECONDS", ge=1, le=604800
    )
    a2a_max_payload_bytes: int = Field(
        default=65536, alias="BOSS_A2A_MAX_PAYLOAD_BYTES", ge=1024
    )
    a2a_dispatch_max_attempts: int = Field(
        default=5, alias="BOSS_A2A_DISPATCH_MAX_ATTEMPTS", ge=1, le=20
    )
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
    # THE BRAIN BEHIND THE CHAT AGENT, CHOSEN SEPARATELY FROM EVERYTHING ELSE.
    #
    # `llm_engine` is global, and flipping it to claude_cli would take the WORKER with it —
    # the worker image has no `claude` binary, so every worker-side completion would start
    # raising "claude CLI not found". The chat agent runs in the API container, which does
    # have it. So the choice is scoped to the one consumer that can honour it.
    #
    # Empty = inherit `llm_engine`, which is what local development and CI want: no Claude
    # CLI installed there, and a default that requires one would break every developer's
    # console. Production sets it explicitly.
    chat_llm_engine: str = Field(default="", alias="BOSS_CHAT_LLM_ENGINE")
    # TYPED DECISIONS (services/typed_decisions.py): questions whose answers are fixed in
    # advance — how much rigor a task needs, which branch a workflow takes. "claude" runs them
    # on the subscription this install already has; "typesafe" sends them to TypeSafe's hosted
    # Jev model, which needs the operator's own TYPESAFE_API_KEY and bills that account.
    # Opus by default: measured on evals/task_rigor it missed 0 of 22 sensitive tasks for about
    # $0.10 a decision, against Haiku's 1 and the keyword lists' 14.
    decision_backend: str = Field(default="claude", alias="BOSS_DECISION_BACKEND")
    decision_model: str = Field(default="opus", alias="BOSS_DECISION_MODEL")
    # With the typesafe backend, a decision below this confidence is asked again of Claude
    # instead of acted on. 0 disables the second opinion.
    decision_fallback_below: float = Field(default=0.7, alias="BOSS_DECISION_FALLBACK_BELOW")
    typesafe_api_key: Optional[str] = Field(default=None, alias="TYPESAFE_API_KEY")
    typesafe_api_url: str = Field(default="https://api.typesafe.ai/v1/systemone",
                                  alias="TYPESAFE_API_URL")
    typesafe_model: str = Field(default="jev-latest", alias="TYPESAFE_MODEL")
    #: Model for that agent — "opus" tracks the latest Opus rather than pinning a dated id.
    chat_model: str = Field(default="", alias="BOSS_CHAT_MODEL")
    #: Model round-trips the chat agent may spend before it must answer. A step that calls
    #: tools produces no text, so this is the investigation budget: four was once the value and
    #: every diagnostic question came back "(reached the step limit while working)".
    chat_max_steps: int = Field(default=14, alias="BOSS_CHAT_MAX_STEPS")
    #: The bounded continuation after a faulted answer. Smaller on purpose — it is a second
    #: chance, not a second investigation.
    chat_continuation_steps: int = Field(default=10, alias="BOSS_CHAT_CONTINUATION_STEPS")
    #: The investigation runner — a real Claude Code session with a shell, the repo, the
    #: database and the fleet. Its own model, because this is the deep-thinking seat and the
    #: chat seat is optimised for latency.
    investigation_model: str = Field(default="opus", alias="BOSS_INVESTIGATION_MODEL")
    #: Turns it may take. Generous on purpose: the failure being fixed is an agent that stopped
    #: after one hypothesis, and forty tool calls over several theories is what debugging is.
    investigation_max_turns: int = Field(default=60, alias="BOSS_INVESTIGATION_MAX_TURNS")
    #: A SELECT-only Postgres role for investigations. Empty falls back to the ordinary
    #: connection with a statement guard in front of it — which stops the accidental
    #: route and not a determined one. Setting this is the real containment.
    #: Where an investigation's sandbox calls BOSS back. Loopback by default: the sandbox
    #: runs inside the API container, so the request never leaves it.
    investigation_api_url: str = Field(default="", alias="BOSS_INVESTIGATION_API_URL")
    claude_cli_timeout: int = Field(default=240, alias="BOSS_CLAUDE_CLI_TIMEOUT")

    # --- one switch for the whole installation (provider agnosticism) ---------
    #
    # "BOSS is not coupled to any one model" used to require setting SIX values in THREE
    # vocabularies — BOSS_LLM_ENGINE and BOSS_CHAT_LLM_ENGINE speak `claude_cli`, the coding
    # lanes speak harness names like `claude_code`/`openrouter`, and the strong/low-cost pair
    # speak a third — and then gave no way to check the result. An operator who missed one was
    # indistinguishable from one who set them all.
    #
    # This is that switch. Set it to a harness name and EVERY surface follows: the coding
    # pipeline, its reviewers, the strong/low-cost routes, the BOSS agent chat, meetings and
    # discovery. Leave it empty and nothing changes — the per-surface settings keep working
    # exactly as before, because an installation that already tuned them should not be
    # silently re-pointed by an upgrade.
    #
    # Verify it with `provider_audit`: config says what was asked for, the llm_calls ledger
    # says what actually happened, and only the second one is evidence.
    provider_lock: str = Field(default="", alias="BOSS_PROVIDER_LOCK")
    #: Per-harness overrides for the CLI specs, as JSON. These flags are a hypothesis until
    #: someone runs the binary, so an operator who finds the real ones must be able to fix
    #: them without waiting for a release:
    #:   {"codex": {"args": ["exec"], "read_only_args": ["--read-only"]}}
    cli_harnesses_json: str = Field(default="", alias="BOSS_CLI_HARNESSES_JSON")

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

    #: harness name -> the dedicated (api-key field, base-url field) the chat surfaces need
    #: bound to reach it. Chat/meetings/discovery all go through OpenAIAdapter, which reads
    #: openai_api_key/openai_base_url, so a lock onto a direct-provider lane has to bind them
    #: or those surfaces would keep calling whatever OPENAI_* happened to be set.
    _LOCK_OPENAI_COMPATIBLE = {
        "openrouter": ("openrouter_api_key", "openrouter_base_url"),
        "groq": ("groq_api_key", "groq_base_url"),
        "deepseek": ("deepseek_api_key", "deepseek_base_url"),
        "ollama": ("ollama_api_key", "ollama_base_url"),
        "vllm": ("vllm_api_key", "vllm_base_url"),
    }
    #: harness names that are a CLI BOSS shells out to rather than an API it calls.
    _LOCK_CLI = {"claude_code": "claude_cli", "codex": "codex", "cursor": "cursor"}

    @model_validator(mode="after")
    def _apply_provider_lock(self):
        """Point every surface at one provider, or leave everything alone."""
        lock = (self.provider_lock or "").strip().lower()
        if not lock:
            return self
        # Coding pipeline: the lanes all speak harness names, so the lock IS the value.
        self.code_agent_engine = lock
        self.code_review_engine = lock
        self.code_agent_strong_engine = lock
        self.code_agent_low_cost_engine = lock
        if lock in self._LOCK_CLI:
            # A CLI harness has no API for the chat surfaces to call, so each needs an adapter
            # that shells out to it. claude_code and codex have one; cursor does not, so a
            # cursor lock leaves chat on the OpenAI-compatible path rather than pretending a
            # backend exists — and says so through the audit.
            chat = self._LOCK_CLI[lock]
            if chat in ("claude_cli", "codex"):
                self.llm_engine = chat
                self.chat_llm_engine = chat
            return self
        # Everything else is an OpenAI-compatible endpoint: take chat off the Claude path and
        # bind the credentials that endpoint actually needs.
        self.llm_engine = lock
        self.chat_llm_engine = lock
        pair = self._LOCK_OPENAI_COMPATIBLE.get(lock)
        if pair:
            key_field, url_field = pair
            key, url = getattr(self, key_field, None), getattr(self, url_field, None)
            if key:
                self.openai_api_key = key
            if url:
                self.openai_base_url = url
            # A lock onto a lane whose Azure credentials are also present would silently
            # dispatch to Azure inside OpenAIAdapter, which takes precedence.
            self.azure_openai_key = None
            self.azure_openai_endpoint = None
            self.azure_openai_deployment = None
        return self

    anthropic_api_key: Optional[str] = Field(
        default=None, alias="ANTHROPIC_API_KEY"
    )
    google_api_key: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY")
    xai_api_key: Optional[str] = Field(default=None, alias="XAI_API_KEY")
    slack_webhook_url: Optional[str] = Field(
        default=None, alias="BOSS_SLACK_WEBHOOK_URL"
    )
    mattermost_base_url: Optional[str] = Field(
        default=None, alias="BOSS_MATTERMOST_BASE_URL"
    )
    mattermost_bot_token: Optional[str] = Field(
        default=None, alias="BOSS_MATTERMOST_BOT_TOKEN"
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
    # GOOGLE / YOUTUBE — publishing to a channel the operator owns.
    #
    # App-level, not per-project: one OAuth client serves every project on this installation,
    # and the thing that differs per project is the REFRESH TOKEN, which is per-connection and
    # lives in the secret store rather than here. The secret below is the app's own client
    # secret, which alone grants nothing — it cannot reach a channel without a user consenting.
    google_oauth_client_id: Optional[str] = Field(
        default=None, alias="BOSS_GOOGLE_OAUTH_CLIENT_ID"
    )
    google_oauth_client_secret: Optional[str] = Field(
        default=None, alias="BOSS_GOOGLE_OAUTH_CLIENT_SECRET"
    )
    #: Where Google sends the operator back. Must match a redirect URI registered on the OAuth
    #: client exactly, including scheme and path. Google never fetches this itself — the
    #: redirect happens in the operator's own browser — so a tailnet-only host works.
    google_oauth_redirect_uri: Optional[str] = Field(
        default=None, alias="BOSS_GOOGLE_OAUTH_REDIRECT_URI"
    )

    # Azure Resource Manager + Azure OpenAI (models also use AZURE_OPENAI_* above)
    azure_tenant_id: Optional[str] = Field(default=None, alias="BOSS_AZURE_TENANT_ID")
    azure_client_id: Optional[str] = Field(default=None, alias="BOSS_AZURE_CLIENT_ID")
    azure_client_secret: Optional[str] = Field(
        default=None, alias="BOSS_AZURE_CLIENT_SECRET"
    )
    azure_subscription_id: Optional[str] = Field(
        default=None, alias="BOSS_AZURE_SUBSCRIPTION_ID"
    )
    # OVHcloud (application key + secret + consumer key)
    ovh_endpoint: str = Field(
        default="https://eu.api.ovh.com/1.0", alias="BOSS_OVH_ENDPOINT"
    )
    ovh_application_key: Optional[str] = Field(
        default=None, alias="BOSS_OVH_APPLICATION_KEY"
    )
    ovh_application_secret: Optional[str] = Field(
        default=None, alias="BOSS_OVH_APPLICATION_SECRET"
    )
    ovh_consumer_key: Optional[str] = Field(
        default=None, alias="BOSS_OVH_CONSUMER_KEY"
    )
    ovh_cloud_project_id: Optional[str] = Field(
        default=None, alias="BOSS_OVH_CLOUD_PROJECT_ID"
    )
    # DigitalOcean optional Spaces / Inference
    do_spaces_key: Optional[str] = Field(default=None, alias="BOSS_DO_SPACES_KEY")
    do_spaces_secret: Optional[str] = Field(default=None, alias="BOSS_DO_SPACES_SECRET")
    do_inference_base_url: Optional[str] = Field(
        default=None, alias="BOSS_DO_INFERENCE_BASE_URL"
    )
    do_inference_api_key: Optional[str] = Field(
        default=None, alias="BOSS_DO_INFERENCE_API_KEY"
    )
    # SSH host (bring-your-own / customer-provided compute) — Docker workload deploys.
    # Gated like DO provisioning; private key held as a SecretRef, never in agent context.
    ssh_host_deploy_allowed: bool = Field(
        default=False, alias="BOSS_SSH_HOST_DEPLOY_ALLOWED"
    )
    # Path to the key, preferred over pasting material into config. Keeps the secret on
    # disk at 0600 instead of in a process environment.
    ssh_host_private_key_file: Optional[str] = Field(
        default=None, alias="BOSS_SSH_HOST_PRIVATE_KEY_FILE")
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
    # Local / self-hosted OpenAI-compatible lanes (the OSS community wedge — no
    # cloud key required). ollama: the OpenAI client insists on a non-empty api
    # key, and Ollama ignores it, so a placeholder default keeps `ollama` a
    # zero-config lane on a dev box. vllm: no conventional port/host exists, so
    # the base URL must be configured explicitly (e.g. http://gpubox:8000/v1).
    # Local inference is $0 — zero-price the models via BOSS_MODEL_PRICES_JSON.
    ollama_api_key: str = Field(default="ollama", alias="OLLAMA_API_KEY")
    ollama_base_url: str = Field(
        default="http://localhost:11434/v1", alias="OLLAMA_BASE_URL"
    )
    vllm_api_key: str = Field(default="vllm", alias="VLLM_API_KEY")
    vllm_base_url: str = Field(default="", alias="VLLM_BASE_URL")
    # Output-token cap for native chat-API calls. Metered gateways (OpenRouter) RESERVE
    # credits for the requested maximum — an uncapped request can 402 on reservation
    # alone even when the actual completion would be affordable. 0 disables the cap.
    native_max_output_tokens: int = Field(
        default=8192, alias="BOSS_NATIVE_MAX_OUTPUT_TOKENS"
    )
    # The platform secret store is the most protected backend BOSS knows about and the
    # first one `default_secret_backend` tries — but it is reached only through these
    # settings. Without them `backend_for("platform_secret_store")` read an attribute that
    # did not exist, raised SecretUnavailable, and every installation silently fell through
    # to a lesser store. A backend nothing can select is not a backend.
    # Infisical, first in the fallback chain. A machine identity rather than a static token:
    # the client id/secret are exchanged for a short-lived access token, so the one long-lived
    # credential BOSS holds is the client secret — prefer the _FILE form to keep it out of the
    # process environment.
    infisical_url: str = Field(default="", alias="BOSS_INFISICAL_URL")
    infisical_client_id: str = Field(default="", alias="BOSS_INFISICAL_CLIENT_ID")
    infisical_client_secret: str = Field(default="", alias="BOSS_INFISICAL_CLIENT_SECRET")
    infisical_client_secret_file: str = Field(
        default="", alias="BOSS_INFISICAL_CLIENT_SECRET_FILE"
    )
    # `infisical bootstrap` returns a machine-identity TOKEN rather than a client id/secret,
    # so a bundled instance authenticates with this and skips the exchange entirely.
    infisical_token: str = Field(default="", alias="BOSS_INFISICAL_TOKEN")
    infisical_token_file: str = Field(default="", alias="BOSS_INFISICAL_TOKEN_FILE")
    infisical_project_id: str = Field(default="", alias="BOSS_INFISICAL_PROJECT_ID")
    infisical_environment: str = Field(default="prod", alias="BOSS_INFISICAL_ENVIRONMENT")
    infisical_secret_path: str = Field(default="/", alias="BOSS_INFISICAL_SECRET_PATH")
    platform_secret_store_url: str = Field(
        default="", alias="BOSS_PLATFORM_SECRET_STORE_URL"
    )
    # The token itself, or a path to read it from. A file keeps it out of the process
    # environment, where anything that dumps env — a crash reporter, a subprocess, a log —
    # would carry it along.
    platform_secret_store_token: str = Field(
        default="", alias="BOSS_PLATFORM_SECRET_STORE_TOKEN"
    )
    platform_secret_store_token_file: str = Field(
        default="", alias="BOSS_PLATFORM_SECRET_STORE_TOKEN_FILE"
    )
    # Codex as a chat backend, mirroring the claude_cli_* settings above it. Separate from the
    # coding harness's own model choice: raising the brain an operator talks to should not be a
    # decision about the cost of every build.
    codex_cli_path: str = Field(default="", alias="BOSS_CODEX_CLI_PATH")
    codex_cli_model: str = Field(default="", alias="BOSS_CODEX_CLI_MODEL")
    codex_cli_timeout: int = Field(default=240, alias="BOSS_CODEX_CLI_TIMEOUT")
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
    # EMPTY, NOT SOMEBODY'S REPOSITORY. These shipped defaulting to `seanheiney/boss-sandbox`,
    # so every install on earth claimed to be pointed at one developer's private sandbox: the
    # setup screen reported "seanheiney/boss-sandbox — GitHub has no such repository visible to
    # this token" as if the operator had configured it, about a repository they have never
    # heard of and cannot see. A default that names a real private repository is worse than no
    # default: unset is a state the product can describe, and `resolve_repository_target` now
    # says "no repository registered for this project" instead of pointing somewhere real.
    target_github_owner: str = Field(default="", alias="BOSS_TARGET_GITHUB_OWNER")
    target_github_repo: str = Field(default="", alias="BOSS_TARGET_GITHUB_REPO")
    # --- Repo-host extension seam (BOSS-267): SCM providers as add-ins ---------------------
    # Which host customer target repos live on: github (default, zero behavior change) or
    # bitbucket. Per-task spec.repo_host overrides this project-level setting.
    target_repo_host: str = Field(default="github", alias="BOSS_TARGET_REPO_HOST")
    # Bitbucket Cloud target (workspace/repo_slug — the owner/repo analog). Fall back to the
    # target_github_* pair so a single-target deployment only sets what differs.
    target_bitbucket_workspace: Optional[str] = Field(
        default=None, alias="BOSS_TARGET_BITBUCKET_WORKSPACE"
    )
    target_bitbucket_repo: Optional[str] = Field(
        default=None, alias="BOSS_TARGET_BITBUCKET_REPO"
    )
    # Bitbucket auth: a workspace/repository access token (Bearer) — preferred — or a
    # username + app password (Basic). Env-indirect like every BOSS connector credential.
    bitbucket_token: Optional[str] = Field(default=None, alias="BOSS_BITBUCKET_TOKEN")
    bitbucket_username: Optional[str] = Field(default=None, alias="BOSS_BITBUCKET_USERNAME")
    bitbucket_app_password: Optional[str] = Field(
        default=None, alias="BOSS_BITBUCKET_APP_PASSWORD"
    )
    # HMAC secret Bitbucket signs webhook deliveries with (X-Hub-Signature). Absent =>
    # POST /v1/webhooks/bitbucket stays fail-closed, mirroring the GitHub webhook.
    bitbucket_webhook_secret: Optional[str] = Field(
        default=None, alias="BOSS_BITBUCKET_WEBHOOK_SECRET"
    )
    # --- MCP-backed repo host (BOSS-269): enterprise SSO/OAuth identity path ---------------
    # URL of the MCP server that fronts the SCM with the USER's identity (SSO/OAuth at the
    # gateway) — repo operations become MCP tool calls; no raw API keys in BOSS.
    repo_mcp_url: Optional[str] = Field(default=None, alias="BOSS_REPO_MCP_URL")
    # Env-var NAME holding the bearer for the MCP endpoint itself (gateway session/token).
    repo_mcp_auth_env: Optional[str] = Field(default=None, alias="BOSS_REPO_MCP_AUTH_ENV")
    # Which SCM the MCP server fronts (drives clone-URL form): bitbucket | github.
    repo_mcp_provider: str = Field(default="bitbucket", alias="BOSS_REPO_MCP_PROVIDER")
    # Optional JSON overrides for tool names ({"create_pull_request": "bb_pr_create", ...}).
    repo_mcp_toolmap: Optional[str] = Field(default=None, alias="BOSS_REPO_MCP_TOOLMAP")
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
    # Optional BOSS-102 S3 sidecar. Unset = engine keeps the Python webhook path
    # and reports honest not_configured. Never required for Community.
    go_webhook_dispatcher_url: Optional[str] = Field(
        default=None, alias="BOSS_GO_WEBHOOK_DISPATCHER_URL"
    )
    go_webhook_dispatcher_token: Optional[str] = Field(
        default=None, alias="BOSS_GO_WEBHOOK_DISPATCHER_TOKEN"
    )
    go_webhook_dispatcher_timeout_seconds: float = Field(
        default=5.0, alias="BOSS_GO_WEBHOOK_DISPATCHER_TIMEOUT_SECONDS", gt=0
    )
    go_webhook_dispatcher_dry_run: bool = Field(
        default=True, alias="BOSS_GO_WEBHOOK_DISPATCHER_DRY_RUN"
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
    # Standalone/pip installs don't have the monorepo layout, so let them point
    # at a spec pack directly instead of relying on repo_root discovery.
    specpack_root_override: str = Field(default="", alias="BOSS_SPECPACK_ROOT")
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
    # Profiles the platform will actually run. A profile named here is refused and downgraded
    # to `balanced` — the whole-platform switch for "this tier is not fit to run yet", which
    # otherwise means hunting every hardcoded `profile="strict"` and putting them all back
    # later. Set BOSS_DISABLED_PROFILES="" to allow everything again.
    disabled_profiles: str = Field(default="strict", alias="BOSS_DISABLED_PROFILES")
    account_connect_url: Optional[str] = Field(
        default=None, alias="BOSS_ACCOUNT_CONNECT_URL"
    )
    account_connect_token: Optional[str] = Field(
        default=None, alias="BOSS_ACCOUNT_CONNECT_TOKEN"
    )
    account_connect_timeout_seconds: int = Field(
        default=10, alias="BOSS_ACCOUNT_CONNECT_TIMEOUT_SECONDS"
    )
    # Local receipt retention for Community (None/0 = indefinite). Cloud-linked
    # projects may override via /v1/cloud/retention into org metadata.
    receipt_retention_days: Optional[int] = Field(
        default=None, alias="BOSS_RECEIPT_RETENTION_DAYS"
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
    mattermost_real_send_allowed: bool = Field(
        default=False, alias="BOSS_MATTERMOST_REAL_SEND_ALLOWED"
    )
    enable_sandbox_merge: bool = Field(
        default=False, alias="BOSS_ENABLE_SANDBOX_MERGE"
    )
    # PRs were opened as drafts, and GitHub REFUSES to merge a draft -- so even an approved
    # merge gate could never land, and every governed PR piled up open forever. The merge gate
    # Decision is the actual safety control, so default to ready-for-review; set this true to
    # go back to drafts.
    open_pr_as_draft: bool = Field(default=False, alias="BOSS_OPEN_PR_AS_DRAFT")
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
    #: The shepherd applies remedies on its own. OFF by default, and it must stay that way.
    #:
    #: Turned on for one afternoon it: opened repair tasks for historical failures nobody had
    #: asked about, retried a build the operator was already retrying by hand (two runs of the
    #: same app, neither aware of the other), and left a 33-minute auto-repair holding the only
    #: worker slot while the operator's actual work waited behind it. Every one of those was
    #: inside its bounds. Bounded autonomous spending is still autonomous spending, and the
    #: person paying has to opt into it.
    shepherd_autopilot_enabled: bool = Field(
        default=False, alias="BOSS_SHEPHERD_AUTOPILOT_ENABLED"
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

    # --- Host shell for the console agent ------------------------------------
    # Lets the BOSS agent run ARBITRARY commands on a registered deployment host, not just the
    # read-only verbs `inspect_host` allows. This is a real shell on a real machine: it can
    # delete containers, edit files and take the box down.
    #
    # Off by default, and the default is the whole point. An operator who wants their agent to
    # have this on their own infrastructure turns it on deliberately; nobody inherits it by
    # upgrading. Every command is written to the event stream with the actor who caused it, so
    # "who ran that" always has an answer.
    agent_host_shell: bool = Field(default=False, alias="BOSS_AGENT_HOST_SHELL")
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
    # Approving a PRD in the console auto-starts a DECOMPOSED build (scaffold + scoped, chained
    # slices — see decisions._start_code_task_after_prd). Safe to enable by default now that the
    # handoff decomposes instead of dumping the whole PRD into one gate-failing task.
    auto_code_task_after_prd: bool = Field(
        default=True, alias="BOSS_AUTO_CODE_TASK_AFTER_PRD"
    )
    # The PRD gate: does a drafted PRD wait for a human before it becomes a build?
    #
    # The panel already contains the review — product, engineering and QA personas argue the
    # spec out before it is written down. Making a person re-affirm that in the console added
    # a queue, not a check: the operator approved every one, and the wait was the only effect.
    #
    # Auto-approval still WRITES the decision and resolves it as `auto_approval`, so the audit
    # trail is identical to a human approval and the PRD is still readable on the card. Set
    # BOSS_MEETING_PRD_AUTO_APPROVE=false to put the human gate back.
    meeting_prd_auto_approve: bool = Field(
        default=True, alias="BOSS_MEETING_PRD_AUTO_APPROVE"
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
    #
    # 1, NOT 3 — THE DEFAULT MUST SURVIVE THE SMALLEST BOX WE TELL PEOPLE TO USE.
    #
    # The only install that has ever run this pipeline for months sets 1 by hand, and nobody
    # wrote that down; a fresh install shipped 3. Each concurrent check is a coding-agent
    # process inside the api container, so on the 4 GB VM in the install docs three of them
    # exhaust the box — and an operator reads the resulting stall as BOSS being broken, which
    # is exactly the misattribution the host-stall work exists to prevent. Raise it on a
    # machine with the memory to spare; the console knob is "Agents at once".
    code_agent_max_concurrency: int = Field(default=1, alias="BOSS_CODE_AGENT_MAX_CONCURRENCY")
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
    # Concurrency & scheduling governance (BOSS-100): VM-pool density ceiling,
    # queue backpressure thresholds, lease-contention ratio. Provider rate limits
    # bind first when prefer_provider_rate_limits is true.
    concurrency_max_vms_per_host: int = Field(
        default=8, alias="BOSS_CONCURRENCY_MAX_VMS_PER_HOST"
    )
    concurrency_max_queue_depth_per_worker: int = Field(
        default=5, alias="BOSS_CONCURRENCY_MAX_QUEUE_DEPTH_PER_WORKER"
    )
    concurrency_backpressure_queue_depth: int = Field(
        default=20, alias="BOSS_CONCURRENCY_BACKPRESSURE_QUEUE_DEPTH"
    )
    concurrency_lease_contention_ratio: float = Field(
        default=0.75, alias="BOSS_CONCURRENCY_LEASE_CONTENTION_RATIO"
    )
    concurrency_lease_expiry_soon_seconds: int = Field(
        default=30, alias="BOSS_CONCURRENCY_LEASE_EXPIRY_SOON_SECONDS"
    )
    concurrency_prefer_provider_rate_limits: bool = Field(
        default=True, alias="BOSS_CONCURRENCY_PREFER_PROVIDER_RATE_LIMITS"
    )
    concurrency_budget_governor_overrides_scale: bool = Field(
        default=True, alias="BOSS_CONCURRENCY_BUDGET_GOVERNOR_OVERRIDES_SCALE"
    )
    concurrency_wip_limit: int = Field(default=5, alias="BOSS_CONCURRENCY_WIP_LIMIT")
    concurrency_max_in_flight: int = Field(
        default=16, alias="BOSS_CONCURRENCY_MAX_IN_FLIGHT"
    )
    # Which CodeAgent implementation runs the edit/test loop:
    #   claude_code = wrap `claude -p` headless WITH its file/bash tools enabled,
    #                 scoped to the ephemeral sandbox cwd (uses the Max plan).
    #   native      = (Phase 1) raw model API agent on the Agent SDK.
    code_agent_engine: str = Field(default="claude_code", alias="BOSS_CODE_AGENT_ENGINE")
    # One-shot spend-cap failover when the builder is claude_code and Anthropic
    # returns the org monthly spend limit. Empty + OPENAI_API_KEY set => "openai".
    # Empty and no OpenAI key => no automatic hop (needs_human / escalate).
    code_agent_failover_engine: str = Field(
        default="", alias="BOSS_CODE_AGENT_FAILOVER_ENGINE"
    )
    # Engine for the ADVERSARY roles (code review / security / consistency / mutation) in the
    # governed pipeline. Defaulting it to a DIFFERENT model than the builder removes self-review
    # bias — the model that wrote the code isn't the one judging it. Empty = same as the builder
    # (single-model). e.g. builder=claude_code, reviewer=native (Azure GPT).
    code_review_engine: str = Field(default="", alias="BOSS_CODE_REVIEW_ENGINE")
    # A MODEL PER LANE, which is what was missing. The knobs above choose a HARNESS for each
    # lane; the only model knobs were per CLASS (strong/low_cost), so "review with Opus" was
    # inexpressible — the reviewer got whatever class its step happened to route to. Picking
    # claude_code as the reviewer when the builder is also claude_code then gave the same model
    # on both sides, which is precisely what the reviewer setting exists to avoid: "a reviewer
    # that is the same model rarely disagrees with itself".
    #
    # Empty keeps today's behaviour exactly — the class model still applies.
    code_review_model: str = Field(default="", alias="BOSS_CODE_REVIEW_MODEL")
    code_agent_model: str = Field(default="", alias="BOSS_CODE_AGENT_MODEL")
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
    # SEVERITY-CALIBRATED REVIEW GATE. The adversarial reviewer emits one
    # `FINDING: <BLOCKER|MAJOR|MINOR|NIT> — …` line per defect; only findings at or above this
    # severity fail `code_review_passed`. Without this the verdict is a single bit, so a NIT-level
    # style note sinks a whole phase exactly like a data-loss bug. Lower-severity findings are
    # still recorded on the run (and optionally spawned as follow-up tasks) — never silently
    # dropped. A review that emits NO structured findings falls back to the binary verdict, so
    # older prompts/models keep their previous fail-closed behaviour.
    code_review_block_severity: str = Field(
        default="major", alias="BOSS_CODE_REVIEW_BLOCK_SEVERITY"
    )
    # Non-blocking findings (below the threshold) become their own queued code task on the same
    # repo/branch, so "ship the phase, fix the nit separately" needs no human bookkeeping.
    code_review_followup_tasks: bool = Field(
        default=True, alias="BOSS_CODE_REVIEW_FOLLOWUP_TASKS"
    )
    # TEST ARBITER. Tests are frozen ground truth: if the test author encoded the criteria WRONG,
    # the implementer can never satisfy it and the run burns every repair round against a bad
    # oracle. When tests fail, an INDEPENDENT third agent (never the implementer, never the test
    # author) rules CODE_WRONG vs TEST_WRONG against the acceptance criteria. Only a TEST_WRONG
    # ruling may amend a test, and the amendment is recorded in evidence — the implementer still
    # never gets write access to the oracle, so `coder_did_not_edit_tests` keeps its meaning.
    code_test_arbiter_enabled: bool = Field(
        default=True, alias="BOSS_CODE_TEST_ARBITER_ENABLED"
    )
    # NEVER BLOCK (autonomy default). When convergence AND repair rounds are exhausted and gates
    # are still red, ship anyway with the unresolved gates recorded on the run and re-queued as
    # their own follow-up task, instead of parking in awaiting_gate_override for a human waiver.
    # Halting there is what turns "10 autonomous tasks" into "10 tickets for the operator" — the
    # human keeps every fact needed to reverse the call, but the program does not stall on it.
    # Set false to restore fail-closed blocking.
    code_gate_never_block: bool = Field(
        default=True, alias="BOSS_CODE_GATE_NEVER_BLOCK"
    )
    # Max agent turns inside the sandbox before we stop and take the diff. 40 is fine for a module
    # but too few for APP-SCALE work (scaffolding a whole Vite/React app is more file-writes than
    # that) — a too-low cap surfaces as error_max_turns and fails the task. 90 fits real app tasks.
    code_agent_max_turns: int = Field(default=90, alias="BOSS_CODE_AGENT_MAX_TURNS")
    # Turn cap for the MICRO-VM GUEST agent (a separate limit from the host cap above). BOSS sends
    # this in the sandbox submit contract; the sandbox-service must honor it to take effect (else it
    # falls back to the service's own ~40 default). Higher lets app-scale tasks finish.
    code_agent_guest_max_turns: int = Field(default=120, alias="BOSS_CODE_AGENT_GUEST_MAX_TURNS")
    # Read-only stages (adversarial/security/consistency review) don't edit code and don't
    # need 40 turns of exploration — a tighter cap cuts review latency a lot without losing
    # rigor (they read the diff + relevant files + judge).
    # 48, not 16. Measured 2026-09-20 on a 39-file build: review_code took 17 turns and did
    # not finish, security_check 17, consistency_check 20 — ALL THREE over a 16-turn cap. The
    # cap was sized when a build was a handful of files; once the panel's PRD reached the
    # builder the apps got materially larger and the reviewers did not get the budget to read
    # them. Escalation doubles from here when a check still does not finish.
    code_agent_review_max_turns: int = Field(default=48, alias="BOSS_CODE_AGENT_REVIEW_MAX_TURNS")
    # How many times a check that DID NOT FINISH (turn cap/timeout/crash) is re-run with
    # a doubled budget before the run ships with "not reviewed" recorded. Escalating the
    # CHECK is the only coherent remedy for a check that never judged the code.
    code_review_max_escalations: int = Field(
        default=2, alias="BOSS_CODE_REVIEW_MAX_ESCALATIONS")
    # How many times an implementer that hit its TURN CAP is resumed (warm session) to
    # finish, before its partial change is judged. Judging a partial change as defective
    # is what turned one small feature into 28 sessions / 96 model-minutes / $39.
    code_impl_max_continuations: int = Field(
        default=3, alias="BOSS_CODE_IMPL_MAX_CONTINUATIONS")
    # How many relevance-ranked files are pre-packed into the coding prompt. The agent
    # otherwise rediscovers the repo every stage (~27 exploration tool-calls vs 16 edits,
    # measured). 0 disables the pack and restores pure self-exploration.
    # --- engine-native coding agent (P0-A / BOSS-407) -------------------
    # The native lane used to be ONE completion returning whole-file rewrites. That is
    # fine for a greenfield single-file task and wrong for a repository: the model could
    # not read a file it was not handed, could not search, and rewrote files it had seen
    # only part of. With tools it explores like the claude_code lane does. Set to false
    # to fall back to the legacy single-shot JSON path (e.g. an endpoint with no
    # function-calling support) — BOSS also falls back automatically and says so.
    native_agent_tools_enabled: bool = Field(
        default=True, alias="BOSS_NATIVE_AGENT_TOOLS_ENABLED"
    )
    # Turn cap for the native lane. Deliberately below the claude_code cap of 90: a cheap
    # model that has not converged in 40 turns is looping, and the VAG economics depend on
    # the cheap lane staying cheap. Each turn is one metered model call.
    native_agent_max_turns: int = Field(
        default=40, alias="BOSS_NATIVE_AGENT_MAX_TURNS"
    )
    # LATENCY: a paced slot is held for a whole role invocation, not per model call
    # (see PacedCodeAgent._paced). The review panel runs its checks concurrently against
    # a per-provider in-flight cap of 3, so a reviewer that wanders for 40 turns starves
    # the lane the other reviewers need. The claude_code lane already separates these
    # (90 build / 16 review); the native lane now does too. Read-only work should be
    # looking, not building.
    native_agent_review_max_turns: int = Field(
        default=12, alias="BOSS_NATIVE_AGENT_REVIEW_MAX_TURNS"
    )
    # --- containerised command execution (P0-D / BOSS-410) --------------
    # Agent-authored commands run as a subprocess inside the API/worker container with
    # full network egress. `subprocess` keeps that behaviour; `docker` runs them in a
    # container with the workspace bind-mounted, NO network, non-root, no capabilities
    # and resource limits. Model calls stay in the control plane either way — the
    # container needs no network precisely because nothing in it should be talking out.
    #
    # Default stays `subprocess` until a live verification run on a real repository is
    # recorded; `docker` fails closed with a readable reason rather than degrading.
    sandbox_command_runtime: str = Field(
        default="subprocess", alias="BOSS_SANDBOX_COMMAND_RUNTIME"
    )
    sandbox_docker_binary: str = Field(default="docker", alias="BOSS_SANDBOX_DOCKER_BINARY")
    sandbox_container_image: str = Field(
        default="python:3.11-slim", alias="BOSS_SANDBOX_CONTAINER_IMAGE"
    )
    #: "none" is the point. Set to "bridge" only for a task that genuinely must fetch.
    sandbox_container_network: str = Field(
        default="none", alias="BOSS_SANDBOX_CONTAINER_NETWORK"
    )
    sandbox_container_memory: str = Field(default="2g", alias="BOSS_SANDBOX_CONTAINER_MEMORY")
    sandbox_container_cpus: str = Field(default="2", alias="BOSS_SANDBOX_CONTAINER_CPUS")
    sandbox_container_pids: int = Field(default=512, alias="BOSS_SANDBOX_CONTAINER_PIDS")

    # --- agent-boundary permissions (P0-E / BOSS-411) -------------------
    # BOSS governs at the WORKFLOW level (decisions, gates, budget) and, until now,
    # not at all inside the sandbox. Factory ships four autonomy levels plus an
    # allow/deny/BLOCK list where the blocklist cannot be bypassed even by an explicit
    # unsafe flag; Warp ships six permission dimensions where the denylist beats
    # everything. This is BOSS's equivalent, enforced in the tool layer.
    #
    #   read_only  — the agent may look and nothing else (review steps get this
    #                regardless of the setting).
    #   edit       — file writes, but no commands at all.
    #   standard   — writes plus allowlisted commands. The default.
    #   full       — writes plus any command the sandbox allowlist permits.
    #
    # The blocklist below is checked at every level including `full`, and there is no
    # setting that turns it off.
    agent_autonomy_level: str = Field(
        default="standard", alias="BOSS_AGENT_AUTONOMY_LEVEL"
    )
    #: Extra commands the agent may run without them being in the sandbox allowlist.
    agent_command_allowlist: str = Field(default="", alias="BOSS_AGENT_COMMAND_ALLOWLIST")
    #: Commands refused for the agent even when the sandbox allowlist would permit them.
    #: Denylist beats allowlist.
    agent_command_denylist: str = Field(default="", alias="BOSS_AGENT_COMMAND_DENYLIST")
    #: Never runnable, at any autonomy level, by any configuration. These are the verbs
    #: that end a run badly and irreversibly: history rewrites, force pushes, remote
    #: mutation, credential exfiltration, and the recursive delete that took 204 tables
    #: on 2026-09-02. Operators may ADD to this; nothing removes an entry.
    agent_command_blocklist: str = Field(
        default="rm,rmdir,dd,mkfs,shutdown,reboot,halt,poweroff,sudo,su,chown,chmod,"
                "curl,wget,nc,ncat,telnet,ssh,scp,sftp,rsync,kubectl,docker,systemctl,"
                "crontab,at,useradd,userdel,passwd,history",
        alias="BOSS_AGENT_COMMAND_BLOCKLIST",
    )

    # Cap on AGENTS.md / CLAUDE.md ingested from the TARGET repo (BOSS-408). Factory caps
    # the equivalent at 80k chars; 32k keeps a small-context local model usable.
    repo_guide_max_chars: int = Field(
        default=32_000, alias="BOSS_REPO_GUIDE_MAX_CHARS"
    )
    code_agent_context_files: int = Field(
        default=14, alias="BOSS_CODE_AGENT_CONTEXT_FILES")
    # Hard wall-clock deadline for the whole review panel AND for a single read-only review
    # invocation — so a slow/rate-limited review (e.g. an OpenAI-gateway retry storm) can't
    # blow the review phase out to 20min. On deadline a check degrades to 'not_run' (security
    # stays strict; adversarial/consistency proceed) rather than hanging or crashing.
    code_agent_review_timeout_seconds: int = Field(
        default=420, alias="BOSS_CODE_AGENT_REVIEW_TIMEOUT_SECONDS"
    )
    # Wall-clock cap for a single CodeAgent invocation. Sized for the raised guest turn cap
    # (~120 turns at ~20s/turn ≈ 2400s + headroom); the sandbox-service scales the VM TTL to
    # this value (VMs live up to 7200s), so a high max_turns is not killed mid-build.
    code_agent_timeout_seconds: int = Field(
        default=2700, alias="BOSS_CODE_AGENT_TIMEOUT_SECONDS"
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
    # Base URL of the Plane instance, e.g. https://plane.example.com (outbound Phase 2).
    plane_base_url: Optional[str] = Field(default=None, alias="PLANE_BASE_URL")
    # Plane workspace slug (for the REST API that resolves webhook label UUIDs -> names). Derived from
    # PLANE_PROJECT_URL when unset.
    plane_workspace: str = Field(default="default", alias="PLANE_WORKSPACE")
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
    # HMAC secret alerting tools sign reliability alert deliveries with (X-Reliability-Signature,
    # sha256 hex over the raw body). Absent => POST /v1/reliability/alert stays fail-closed (RB-10).
    reliability_alert_secret: Optional[str] = Field(
        default=None, alias="RELIABILITY_ALERT_SECRET"
    )
    # Mattermost/Slack incoming-webhook URL for oncall investigation cards (RB-20). Absent => no
    # notifications, everything else unaffected (notify is always best-effort).
    reliability_oncall_webhook: Optional[str] = Field(
        default=None, alias="RELIABILITY_ONCALL_WEBHOOK"
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
    # Community GA honesty gate: when true, refuse microVM *execution* claims with a clear
    # 422 (host connectivity may still be reported). Flip after rootfs bake is proven live.
    # See docs/product/SANDBOX_GA_STATUS.md.
    microvm_claims_frozen: bool = Field(
        default=True, alias="BOSS_MICROVM_CLAIMS_FROZEN"
    )
    # Root dir for ephemeral sandboxes. Production must set this to a volume
    # (e.g. /var/lib/boss/artifacts/sandboxes) so API recreates do not destroy
    # verify-green workspaces needed for preview.
    sandbox_root_path: Optional[Path] = Field(
        default=None, alias="BOSS_SANDBOX_ROOT_PATH"
    )
    # Shallow-clone depth for the ephemeral sandbox.
    sandbox_clone_depth: int = Field(default=1, alias="BOSS_SANDBOX_CLONE_DEPTH")
    # Absolute operator-facing origin for preview View URLs (e.g.
    # https://boss-api.example.com). When set, publicize_preview rewrites
    # loopback binds to {base}/v1/previews/{id}/web/ and rejects a loopback base.
    # Relative gateway paths are used when empty. See docs/product/PREVIEW_GA_CHECKLIST.md.
    preview_public_base: str = Field(
        default="",
        validation_alias=AliasChoices("BOSS_PREVIEW_PUBLIC_BASE", "preview_public_base"),
    )

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

    # In-process rate limiting for high-cost paths (P2 per .ai/security-review.md).
    # Community / single-process: the in-memory limiter in apps/api/app/main.py is
    # the supported control. Redis is the A2A transport, not a rate-limit backend.
    # Multi-replica / public-edge: front with Redis/LB/CDN and optionally disable
    # this limiter via BOSS_RATE_LIMIT_ENABLED=false. Default protects
    # metatron/query, commands/propose, resources/sync per (IP+actor) key.
    rate_limit_enabled: bool = Field(
        default=True, alias="BOSS_RATE_LIMIT_ENABLED"
    )
    rate_limit_requests_per_minute: int = Field(
        # Bumped 10 -> 120 so interactive console/validation sessions aren't
        # throttled on the high-cost paths (propose/query/chat/sync).
        default=120, alias="BOSS_RATE_LIMIT_RPM"
    )

    @property
    def repo_root(self) -> Path:
        # ``config.py`` moved from ``apps/api/app`` to
        # ``engine/boss_engine``. Both layouts are three path components below
        # the repository only if the filename itself is counted; pathlib's
        # ``parents`` collection starts at the containing directory, so the
        # relocated package must use index 2. In the API/worker images the same
        # layout is rooted at ``/app/engine/boss_engine`` and resolves to
        # ``/app``, where db/ and packages/ are copied.
        return Path(__file__).resolve().parents[2]

    @property
    def schema_path(self) -> Path:
        return self.repo_root / "db" / "schema.sql"

    @property
    def artifact_root(self) -> Path:
        return (self.repo_root / self.artifact_dir).resolve()

    @property
    def specpack_root(self) -> Path:
        if self.specpack_root_override:
            return Path(self.specpack_root_override).resolve()
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


# --- configuration drift -----------------------------------------------------------------
#
# Installs drift by accumulating variables nobody reads, and the only way to tell a dead knob
# from a live one is to ask every reader.
#
# THE TRAP, HIT WHILE WRITING THIS: BOSS_LEASE_TTL_SECONDS looks dead from here. It is in no
# engine setting, so a check built on `Settings` alone calls it unread -- and it is nothing of
# the sort: the WORKER has its own settings class, and that knob decides how long a crashed
# worker's job is stuck before another worker reclaims it. A drift report that names a live
# durability knob as dead is worse than no report, because someone will act on it.
#
# So: ask both classes, and count a name as read if ANY of pydantic's routes accept it -- the
# field name, `alias`, or any entry in `validation_alias=AliasChoices(...)`. Reading only
# `alias` marks ~300 real settings as unread.

#: Names the source scan cannot see because they are never written out literally -- built by
#: interpolation, or read by a tool outside this tree. Keep this list tiny; the scan is the
#: real mechanism.
CONSUMED_OUTSIDE_SETTINGS: frozenset[str] = frozenset(
    {
        "BOSS_API_KEYS",  # apps/api: derived from BOSS_API_KEY in compose
        "BOSS_API_KEY_PRINCIPALS",  # same
    }
)

#: Directories with no BOSS source in them. Skipped so the scan stays fast enough to run at
#: boot: without this it walks node_modules and .next and takes seconds instead of ~100ms.
_SKIP_DIRECTORIES = frozenset(
    {".git", "node_modules", ".next", "__pycache__", ".venv", "venv", "dist", "build",
     ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)

#: Extensions that can plausibly contain a reader: Python, shell, compose/k8s YAML, the web
#: app, Dockerfiles and Makefiles. `.env` files are excluded by name below -- a variable
#: appearing in the file that SETS it is not evidence that anything READS it.
_SOURCE_SUFFIXES = frozenset(
    {".py", ".sh", ".bash", ".yml", ".yaml", ".ts", ".tsx", ".js", ".jsx", ".tmpl",
     ".toml", ".cfg", ".mk"}
)

#: Readers with no extension. Matching on the whole name instead of accepting every
#: extensionless file, which is what made the first scan walk binaries and take 12 seconds.
_SOURCE_NAMES = frozenset({"Dockerfile", "Makefile", "makefile"})

#: No reader is a megabyte of one line. This skips lockfiles, bundles and checked-in data.
_MAX_SOURCE_BYTES = 1_000_000


@lru_cache(maxsize=4)
def names_mentioned_in_source(root: str) -> frozenset[str]:
    """Every BOSS_* name that appears literally anywhere in the source tree.

    WHY A SCAN AND NOT A LIST. Three separate hand-built versions of this check accused live
    settings of being dead, because a settings class is not the only reader: BOSS_API_PORT is
    read by the compose file and by doctor.sh, BOSS_LOCAL_SUPERVISOR by a bare `os.getenv`,
    BOSS_LEASE_TTL_SECONDS by the worker's own settings class. Every one of those is invisible
    to a scan of `Settings`, and each wrong accusation invited someone to delete something
    that works.

    Mention is weak evidence of a reader -- a name in a comment counts here. That asymmetry is
    deliberate: the cost of a missed dead variable is an untidy .env, and the cost of a false
    accusation is an operator deleting their own API port.
    """
    import re

    found: set[str] = set()
    pattern = re.compile(rb"BOSS_[A-Z0-9_]+")
    base = Path(root)
    if not base.is_dir():
        return frozenset()
    # os.walk with in-place pruning, NOT rglob: rglob enumerates every file under
    # node_modules before the filter can reject it, which is the difference between 0.3s and
    # 13s and therefore the difference between running this at boot and not.
    import os as _os

    for directory, subdirectories, filenames in _os.walk(base):
        subdirectories[:] = [d for d in subdirectories if d not in _SKIP_DIRECTORIES]
        for filename in filenames:
            path = Path(directory) / filename
            # A variable in the file that SETS it is not evidence that anything READS it.
            if filename == ".env" or filename.startswith(".env."):
                continue
            if path.suffix.lower() not in _SOURCE_SUFFIXES and filename not in _SOURCE_NAMES:
                if not filename.startswith("Dockerfile"):
                    continue
            try:
                if path.stat().st_size > _MAX_SOURCE_BYTES:
                    continue
                blob = path.read_bytes()
            except OSError:
                continue
            found.update(match.decode() for match in pattern.findall(blob))
    return frozenset(found)


def _env_names(model) -> set[str]:
    """Every environment variable name a pydantic-settings class will accept, upper-cased."""
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        names.add(field_name.upper())
        alias = getattr(field, "alias", None)
        if isinstance(alias, str):
            names.add(alias.upper())
        validation_alias = getattr(field, "validation_alias", None)
        if isinstance(validation_alias, str):
            names.add(validation_alias.upper())
        elif isinstance(validation_alias, AliasChoices):
            names.update(
                str(choice).upper()
                for choice in validation_alias.choices
                if isinstance(choice, str)
            )
    return names


def _worker_settings_model():
    """The worker ships as a separate app and may not be importable from the engine alone.

    If it is not on the path, say nothing rather than guess -- an absent reader must never be
    reported as proof that a variable has no reader.
    """
    import importlib

    # Two spellings, both real: the containers run `python -m apps.worker.boss_worker.main`
    # with /app on the path, while a dev shell puts apps/worker on PYTHONPATH directly.
    for module in ("apps.worker.boss_worker.config", "boss_worker.config"):
        try:
            return importlib.import_module(module).WorkerSettings
        except Exception:  # noqa: BLE001 — a diagnostic must never be the thing that breaks
            continue
    return None


def setting_env_names() -> frozenset[str]:
    """Every environment variable name any BOSS settings class will accept."""
    names = _env_names(Settings)
    worker = _worker_settings_model()
    if worker is not None:
        names |= _env_names(worker)
    return frozenset(names)


def unread_boss_variables(environ, source_root=None) -> tuple[str, ...]:
    """BOSS_* variables that are set and that nothing appears to read — for reporting only.

    Returns nothing at all when the readers cannot be fully enumerated: when the worker's
    settings will not import, or when no source tree is available to scan. A partial view of
    the readers can only produce false accusations, and this check has produced three.

    Reported, never enforced. Refusing to boot over a stray variable would strand an install
    for a typo, and the goal is to make drift visible rather than fatal.
    """
    if _worker_settings_model() is None:
        return ()
    root = source_root or _default_source_root()
    if root is None:
        return ()
    mentioned = names_mentioned_in_source(str(root))
    if not mentioned:
        return ()
    known = setting_env_names() | CONSUMED_OUTSIDE_SETTINGS | mentioned
    return tuple(
        sorted(
            key
            for key in environ
            if key.upper().startswith("BOSS_") and key.upper() not in known
        )
    )


def _default_source_root():
    """The tree this package was installed from — the repo in development, /app in a container.

    boss_engine lives at <root>/engine/boss_engine, so the root is two parents up. Returns
    None when that shape does not hold, which makes the check silent rather than wrong.
    """
    root = Path(__file__).resolve().parents[2]
    return root if (root / "engine").is_dir() else None
