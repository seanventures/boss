"""AWS Bedrock Runtime adapter (Converse API) for BOSS model invocation.

Skill IDs (multi-cloud catalog):
  - aws.bedrock.converse
  - aws.bedrock.list_models

Notes
-----
* Uses boto3 clients ``bedrock-runtime`` (Converse) and ``bedrock``
  (ListFoundationModels). boto3 is already required for AWS EC2/S3/RDS in
  resource_plane; this module is a separate model-path adapter so Model
  Gateway can call Bedrock without going through infra ChangeSets.
* Credentials: explicit kwargs override Settings / env
  (``BOSS_AWS_ACCESS_KEY_ID``, ``BOSS_AWS_SECRET_ACCESS_KEY``,
  ``BOSS_AWS_SESSION_TOKEN``, ``BOSS_AWS_REGION``). Missing credentials raise
  ``IntegrationConfigurationError`` — never invent a silent mock response.
* Model invoke is *not* gated by ``BOSS_INFRA_REAL_PROVISION_ALLOWED``
  (that flag is for resource-plane writes). Callers still own budget
  reservation + Receipts before spendy invokes.
* Never return raw AWS secret keys in responses or error strings.
* Cost estimates are approximate public list prices in USD micros; override
  via BOSS_MODEL_PRICES_JSON (provider prefix ``bedrock:``) if needed.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from ..config import Settings
from .github_adapter import IntegrationConfigurationError

# Approximate on-demand Bedrock prices USD per 1M tokens: (input, output).
# Keys match model id substrings / common aliases. Unknown models use DEFAULT.
_BEDROCK_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "anthropic.claude-3-5-sonnet": (3.00, 15.00),
    "anthropic.claude-3-5-haiku": (0.80, 4.00),
    "anthropic.claude-3-sonnet": (3.00, 15.00),
    "anthropic.claude-3-haiku": (0.25, 1.25),
    "anthropic.claude-3-opus": (15.00, 75.00),
    "anthropic.claude-sonnet-4": (3.00, 15.00),
    "anthropic.claude-opus-4": (15.00, 75.00),
    "amazon.titan-text-express": (0.20, 0.60),
    "amazon.titan-text-lite": (0.15, 0.20),
    "amazon.nova-pro": (0.80, 3.20),
    "amazon.nova-lite": (0.06, 0.24),
    "amazon.nova-micro": (0.035, 0.14),
    "meta.llama3-70b": (0.99, 0.99),
    "meta.llama3-8b": (0.30, 0.60),
    "mistral.mistral-large": (4.00, 12.00),
    "mistral.mistral-small": (1.00, 3.00),
    "cohere.command-r-plus": (3.00, 15.00),
    "cohere.command-r": (0.50, 1.50),
}
_DEFAULT_BEDROCK_PRICE = (3.00, 15.00)


class AwsBedrockAdapter:
    """Bedrock Runtime Converse + foundation model catalog."""

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    def _client(
        self,
        service_name: str,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        region: Optional[str] = None,
        session_token: Optional[str] = None,
    ) -> Any:
        """Build a boto3 client for ``bedrock`` or ``bedrock-runtime``.

        Never logs or returns the secret material.
        """
        try:
            import boto3  # noqa: PLC0415 — optional at import time; required at call
        except ImportError as error:
            raise IntegrationConfigurationError(
                "The AWS Bedrock adapter requires the 'boto3' package. "
                "Install it (pip install boto3) to enable aws.bedrock.* skills."
            ) from error

        key_id, secret, reg, token = self._resolve_credentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region=region,
            session_token=session_token,
        )
        kwargs: dict[str, Any] = {
            "service_name": service_name,
            "aws_access_key_id": key_id,
            "aws_secret_access_key": secret,
            "region_name": reg,
        }
        if token:
            kwargs["aws_session_token"] = token
        return boto3.client(**kwargs)

    def _resolve_credentials(
        self,
        *,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        region: Optional[str] = None,
        session_token: Optional[str] = None,
    ) -> tuple[str, str, str, Optional[str]]:
        key_id = (
            (access_key_id or "").strip()
            or (self.settings.aws_access_key_id or "").strip()
            or (os.environ.get("BOSS_AWS_ACCESS_KEY_ID") or "").strip()
        )
        secret = (
            (secret_access_key or "").strip()
            or (os.environ.get("BOSS_AWS_SECRET_ACCESS_KEY") or "").strip()
        )
        reg = (
            (region or "").strip()
            or (self.settings.aws_region or "").strip()
            or (os.environ.get("BOSS_AWS_REGION") or "").strip()
            or "us-east-1"
        )
        token = (
            (session_token or "").strip()
            or (os.environ.get("BOSS_AWS_SESSION_TOKEN") or "").strip()
            or None
        )
        if not key_id:
            raise IntegrationConfigurationError(
                "AWS Bedrock credentials missing: BOSS_AWS_ACCESS_KEY_ID is not configured."
            )
        if not secret:
            raise IntegrationConfigurationError(
                "AWS Bedrock credentials missing: BOSS_AWS_SECRET_ACCESS_KEY is not configured."
            )
        return key_id, secret, reg, token

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    def list_foundation_models(
        self,
        *,
        by_provider: Optional[str] = None,
        by_output_modality: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        region: Optional[str] = None,
        session_token: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Call bedrock ListFoundationModels; return a list of model summaries.

        Each item is a plain dict with model_id, model_name, provider_name,
        input_modalities, output_modalities, and inference_types (when present).
        Secrets are never included.
        """
        client = self._client(
            "bedrock",
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region=region,
            session_token=session_token,
        )
        kwargs: dict[str, Any] = {}
        if by_provider:
            kwargs["byProvider"] = by_provider
        if by_output_modality:
            kwargs["byOutputModality"] = by_output_modality
        try:
            response = client.list_foundation_models(**kwargs)
        except Exception as error:  # noqa: BLE001 — surface as config/integration error
            raise IntegrationConfigurationError(
                f"Bedrock ListFoundationModels failed: {_safe_error(error)}"
            ) from error

        summaries = response.get("modelSummaries") or []
        out: list[dict[str, Any]] = []
        for item in summaries:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "model_id": item.get("modelId"),
                    "model_name": item.get("modelName"),
                    "provider_name": item.get("providerName"),
                    "input_modalities": list(item.get("inputModalities") or []),
                    "output_modalities": list(item.get("outputModalities") or []),
                    "inference_types": list(item.get("inferenceTypesSupported") or []),
                    "customizations": list(item.get("customizationsSupported") or []),
                    "response_streaming": bool(item.get("responseStreamingSupported")),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Converse
    # ------------------------------------------------------------------

    def converse(
        self,
        *,
        model_id: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 1024,
        temperature: Optional[float] = None,
        system: Optional[str | list[dict[str, Any]]] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        region: Optional[str] = None,
        session_token: Optional[str] = None,
        **creds: Any,
    ) -> dict[str, Any]:
        """Invoke Bedrock Runtime Converse.

        Returns::

            {
              "content": "<assistant text>",
              "usage": {"input_tokens": int, "output_tokens": int},
              "model_id": str,
              "provider_ref": str,  # ResponseMetadata RequestId when present
              "stop_reason": str | None,
              "raw_message": dict,  # sanitized assistant message block
            }

        ``messages`` may use OpenAI-style ``{"role","content": str}`` or native
        Bedrock content blocks ``{"role","content": [{"text": ...}]}``.
        Extra credential kwargs (access_key_id / secret_access_key / region /
        session_token) are accepted for SecretRef-bound callers.
        """
        if not (model_id or "").strip():
            raise IntegrationConfigurationError("model_id is required for Bedrock converse.")
        if not messages:
            raise IntegrationConfigurationError("messages must be a non-empty list.")

        # Allow **creds spill for callers that pass a credential bag.
        access_key_id = access_key_id or creds.get("access_key_id")
        secret_access_key = secret_access_key or creds.get("secret_access_key")
        region = region or creds.get("region")
        session_token = session_token or creds.get("session_token")

        client = self._client(
            "bedrock-runtime",
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region=region,
            session_token=session_token,
        )

        payload: dict[str, Any] = {
            "modelId": model_id,
            "messages": _normalize_messages(messages),
            "inferenceConfig": {"maxTokens": int(max_tokens)},
        }
        if temperature is not None:
            payload["inferenceConfig"]["temperature"] = float(temperature)
        system_blocks = _normalize_system(system)
        if system_blocks:
            payload["system"] = system_blocks

        try:
            response = client.converse(**payload)
        except Exception as error:  # noqa: BLE001
            raise IntegrationConfigurationError(
                f"Bedrock Converse failed: {_safe_error(error)}"
            ) from error

        output = response.get("output") or {}
        message = output.get("message") or {}
        content_blocks = message.get("content") or []
        text_parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and "text" in block:
                text_parts.append(str(block.get("text") or ""))
        content = "".join(text_parts)

        usage_raw = response.get("usage") or {}
        input_tokens = int(
            usage_raw.get("inputTokens")
            or usage_raw.get("input_tokens")
            or 0
        )
        output_tokens = int(
            usage_raw.get("outputTokens")
            or usage_raw.get("output_tokens")
            or 0
        )

        meta = response.get("ResponseMetadata") or {}
        request_id = meta.get("RequestId") or meta.get("requestId")

        return {
            "content": content,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            "model_id": model_id,
            "provider_ref": str(request_id) if request_id else f"bedrock:{model_id}",
            "stop_reason": response.get("stopReason"),
            "raw_message": {
                "role": message.get("role"),
                "content": content_blocks,
            },
        }

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def estimate_cost_usd_micros(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        """Estimate cost in USD micros for a completed Bedrock invoke.

        Uses built-in Bedrock price table, then BOSS_MODEL_PRICES_JSON overrides
        via openai_adapter.price_for_model with provider ``bedrock``.
        """
        inp_rate, out_rate = _price_for_bedrock_model(model_id)
        # Allow BOSS_MODEL_PRICES_JSON provider-prefixed overrides when available.
        try:
            from .openai_adapter import price_for_model  # noqa: PLC0415

            override_in, override_out, known = price_for_model(
                model_id, provider="bedrock"
            )
            # price_for_model falls back to DEFAULT OpenAI prices when unknown;
            # only honor when the table actually had a match (known=True) or the
            # override key was provider-prefixed in BOSS_MODEL_PRICES_JSON.
            if known:
                inp_rate, out_rate = override_in, override_out
        except Exception:  # noqa: BLE001 — cost estimation must never break callers
            pass

        # rates are $/Mtok; tokens * $/Mtok = micro-dollars when *1 (since 1e6 micros
        # * 1e-6 Mtok unit cancels). Same convention as openai_adapter.estimate_cost.
        return int(
            round(
                int(input_tokens or 0) * float(inp_rate)
                + int(output_tokens or 0) * float(out_rate)
            )
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _price_for_bedrock_model(model_id: str) -> tuple[float, float]:
    mid = (model_id or "").strip().lower()
    if not mid:
        return _DEFAULT_BEDROCK_PRICE
    # Exact then longest-prefix / substring match for region-qualified ids
    # (e.g. us.anthropic.claude-3-5-sonnet-20241022-v1:0).
    if mid in _BEDROCK_PRICE_PER_MTOK:
        return _BEDROCK_PRICE_PER_MTOK[mid]
    best_key = ""
    best_price = _DEFAULT_BEDROCK_PRICE
    for key, price in _BEDROCK_PRICE_PER_MTOK.items():
        if key in mid and len(key) > len(best_key):
            best_key = key
            best_price = price
    return best_price


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            raise IntegrationConfigurationError(
                "Each Bedrock message must be a dict with role and content."
            )
        role = str(msg.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            # Bedrock Converse only accepts user/assistant in the messages array;
            # system goes in the separate system= parameter.
            if role == "system":
                continue
            raise IntegrationConfigurationError(
                f"Unsupported Bedrock message role: {role or '<empty>'}"
            )
        content = msg.get("content")
        if isinstance(content, str):
            blocks: list[dict[str, Any]] = [{"text": content}]
        elif isinstance(content, list):
            blocks = []
            for block in content:
                if isinstance(block, dict):
                    blocks.append(block)
                elif isinstance(block, str):
                    blocks.append({"text": block})
                else:
                    raise IntegrationConfigurationError(
                        "Bedrock content blocks must be dicts or strings."
                    )
        else:
            raise IntegrationConfigurationError(
                "Bedrock message content must be a string or list of content blocks."
            )
        normalized.append({"role": role, "content": blocks})
    if not normalized:
        raise IntegrationConfigurationError(
            "No user/assistant messages remain after normalization."
        )
    return normalized


def _normalize_system(
    system: Optional[str | list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if system is None:
        return []
    if isinstance(system, str):
        text = system.strip()
        return [{"text": text}] if text else []
    if isinstance(system, list):
        out: list[dict[str, Any]] = []
        for block in system:
            if isinstance(block, dict):
                out.append(block)
            elif isinstance(block, str) and block.strip():
                out.append({"text": block})
        return out
    raise IntegrationConfigurationError(
        "system must be a string or list of content blocks."
    )


def _safe_error(error: Exception) -> str:
    """Stringify provider errors without echoing secret material."""
    text = str(error) or error.__class__.__name__
    # Defensive scrub of common secret-shaped substrings if boto ever embeds them.
    for marker in ("SecretAccessKey", "secret_access_key", "aws_secret", "AKIA"):
        if marker.lower() in text.lower() and len(text) > 80:
            return f"{error.__class__.__name__}: [redacted provider error]"
    return text[:500]
