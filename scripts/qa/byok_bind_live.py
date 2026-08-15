#!/usr/bin/env python3
"""Bind a live env model key into local_vault SecretRef (BOSS-369 slice 11).

Default is dry-run. Apply from inside goldeneye boss-app api so the key
never leaves the box.

Source (first present, min 8 chars): OPENAI_API_KEY, GROQ_API_KEY, GROQ.
POST /v1/providers/easy-connect (PUT is 405 on current runtimes).

Never prints the key or last4. Public apply output is only
secret_ref_id + provider. If easy-connect returns 422/503, write
docs/operations/BYOK_BIND_BLOCKER.md with flag/schema names only.

Not GA. No --env-template.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

GATE_NAME = "byok_bind_live"
DEFAULT_API_URL = "https://boss-api.local.sean.network"
IN_CONTAINER_API_URL = "http://127.0.0.1:8000"
DEFAULT_PROJECT_ID = "boss-dogfood"
DEFAULT_ACTOR = "executive_console"
DEFAULT_BACKEND = "local_vault"
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
EASY_CONNECT_PATH = "/v1/providers/easy-connect"
MIN_SECRET = 8
SOURCE_ENV_ORDER = ("OPENAI_API_KEY", "GROQ_API_KEY", "GROQ")
DROP_KEYS = frozenset(
    {
        "last4",
        "fingerprint",
        "secret_value",
        "raw_secret",
        "plaintext",
        "api_key_value",
        "token_value",
        "password",
        "value",
        "api_key",
        "token",
    }
)
KNOWN_FLAG_NAMES = (
    "BOSS_KMS_MASTER_KEY",
    "BOSS_KMS_MASTER_KEY_FILE",
    "BOSS_KMS_ALLOW_EPHEMERAL_MASTER",
    "BOSS_KMS_VAULT_DIR",
    "local_vault",
    "secret_refs",
    "provider_accounts",
    "ProviderEasyConnectRequest",
)
BLOCKER_RELATIVE = Path("docs/operations/BYOK_BIND_BLOCKER.md")


@dataclass
class Source:
    env_name: Optional[str]
    present: bool
    provider: Optional[str]
    secret_value: str = ""
    base_url: Optional[str] = None


def in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(
        os.environ.get("BOSS_CONTROL_PLANE_URL", "").strip()
    )


def resolve_api_url(cli_value: str | None = None) -> str:
    return (
        (cli_value or "").strip()
        or os.environ.get("BOSS_API_BASE", "").strip()
        or os.environ.get("BOSS_API_URL", "").strip()
        or (IN_CONTAINER_API_URL if in_container() else DEFAULT_API_URL)
    )


def resolve_project_id(cli_value: str | None = None) -> str:
    return (
        (cli_value or "").strip()
        or os.environ.get("PROJECT_ID", "").strip()
        or os.environ.get("BOSS_PROJECT_ID", "").strip()
        or os.environ.get("BOSS_DEFAULT_PROJECT_ID", "").strip()
        or DEFAULT_PROJECT_ID
    )


def resolve_actor(cli_value: str | None = None) -> str:
    return (
        (cli_value or "").strip()
        or os.environ.get("BOSS_ACTOR", "").strip()
        or DEFAULT_ACTOR
    )


def _clean_secret(raw: Any) -> str:
    return str(raw or "").strip()


def _official_openai_base(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "api.openai.com" or host.endswith(".openai.com")


def _provider_for_openai(env: Mapping[str, str]) -> tuple[str, Optional[str]]:
    base = _clean_secret(env.get("OPENAI_BASE_URL"))
    if not base or _official_openai_base(base):
        return "openai", None
    return "openai_compatible", base.rstrip("/")


def discover_source(env: Mapping[str, str] | None = None) -> Source:
    """First usable live model key. Values stay in memory only."""
    blob = env if env is not None else os.environ
    for name in SOURCE_ENV_ORDER:
        secret = _clean_secret(blob.get(name))
        if len(secret) < MIN_SECRET:
            continue
        if name == "OPENAI_API_KEY":
            provider, base_url = _provider_for_openai(blob)
        else:
            provider = "openai_compatible"
            base_url = (
                _clean_secret(blob.get("GROQ_BASE_URL")) or DEFAULT_GROQ_BASE_URL
            ).rstrip("/")
        return Source(
            env_name=name,
            present=True,
            provider=provider,
            secret_value=secret,
            base_url=base_url,
        )
    return Source(env_name=None, present=False, provider=None)


def default_account_label(provider: str | None) -> str:
    return f"live-env-{provider or 'model'}"


def build_request_body(
    source: Source,
    *,
    project_id: str,
    actor: str,
    account_label: str | None = None,
) -> dict[str, Any]:
    if not source.present or not source.provider:
        raise ValueError("no live model key in env")
    body: dict[str, Any] = {
        "project_id": project_id,
        "actor": actor,
        "provider": source.provider,
        "account_label": (account_label or "").strip()
        or default_account_label(source.provider),
        "secret_value": source.secret_value,
        "backend": DEFAULT_BACKEND,
        "read_only": True,
        "test_after": False,
    }
    if source.base_url:
        body["base_url"] = source.base_url
    return body


def _known_secrets(secrets: Iterable[str] | None, extra: Iterable[str] | None = None) -> list[str]:
    out: list[str] = []
    for item in list(secrets or []) + list(extra or []):
        cleaned = _clean_secret(item)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(cleaned) >= MIN_SECRET:
            tail = cleaned[-4:]
            if tail and tail not in out:
                out.append(tail)
    return out


def redact_secrets(text: str, secrets: list[str] | None = None) -> str:
    if not text:
        return text
    redacted = text
    for secret in _known_secrets(secrets):
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    if "X-Boss-Api-Key" in redacted:
        redacted = redacted.replace("X-Boss-Api-Key", "X-Boss-Api-Key(redacted)")
    # Field name itself can identify a key when paired with last4=....
    redacted = re.sub(r"last4\s*=\s*", "", redacted, flags=re.IGNORECASE)
    redacted = re.sub(r"\blast4\b", "[redacted-field]", redacted, flags=re.IGNORECASE)
    return redacted


def sanitize_payload(payload: Any, secrets: list[str] | None = None) -> Any:
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in DROP_KEYS:
                continue
            cleaned[str(key)] = sanitize_payload(value, secrets)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_payload(item, secrets) for item in payload]
    if isinstance(payload, str):
        return redact_secrets(payload, secrets)
    return payload


def public_success(payload: Mapping[str, Any], secrets: list[str] | None = None) -> dict[str, str]:
    secret = payload.get("secret") if isinstance(payload.get("secret"), dict) else {}
    account = (
        payload.get("provider_account")
        if isinstance(payload.get("provider_account"), dict)
        else {}
    )
    secret_ref_id = str(
        payload.get("secret_ref_id") or secret.get("secret_ref_id") or ""
    )
    provider = str(
        payload.get("provider") or account.get("provider") or ""
    )
    public = {"secret_ref_id": secret_ref_id, "provider": provider}
    dumped = json.dumps(public)
    for secret_value in _known_secrets(secrets):
        if secret_value and secret_value in dumped:
            raise RuntimeError("refusing to print identifying secret material")
    if "last4" in dumped.lower():
        raise RuntimeError("refusing to print last4")
    return public


def dry_run_plan(
    source: Source,
    *,
    api_url: str,
    project_id: str,
    actor: str,
    account_label: str | None = None,
) -> dict[str, Any]:
    label = (account_label or "").strip() or default_account_label(source.provider)
    return {
        "ok": source.present,
        "dry_run": True,
        "gate": GATE_NAME,
        "source_env": source.env_name,
        "source_present": source.present,
        "provider": source.provider,
        "backend": DEFAULT_BACKEND,
        "endpoint": f"POST {EASY_CONNECT_PATH}",
        "api_url": api_url.rstrip("/"),
        "project_id": project_id,
        "actor": actor,
        "account_label": label,
        "has_base_url": bool(source.base_url),
        "secret_ref_id": None,
        "ga": False,
    }


def extract_blocker_flags(status: int, payload: Any) -> list[str]:
    flags: list[str] = []
    detail = ""
    if isinstance(payload, dict):
        raw = payload.get("detail")
        if isinstance(raw, str):
            detail = raw
        else:
            detail = json.dumps(payload)
    elif isinstance(payload, str):
        detail = payload
    blob = detail
    for name in KNOWN_FLAG_NAMES:
        if name in blob and name not in flags:
            flags.append(name)
    for match in re.findall(r"\bBOSS_[A-Z0-9_]+\b", blob):
        if match not in flags:
            flags.append(match)
    if status == 503 and "relation" in blob.lower() or (
        status == 503 and "not applied" in blob.lower()
    ):
        for name in ("secret_refs", "provider_accounts"):
            if name not in flags:
                flags.append(name)
    if status == 422:
        if "ProviderEasyConnectRequest" not in flags:
            flags.append("ProviderEasyConnectRequest")
        if "local_vault" not in flags:
            flags.append("local_vault")
    if status == 503 and "local_vault" not in flags:
        flags.append("local_vault")
    return flags


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parents[2], Path.cwd()):
        if (candidate / "docs" / "operations").is_dir():
            return candidate
    return Path.cwd()


def write_blocker_doc(
    path: Path,
    *,
    status: int,
    flags: list[str],
    endpoint: str,
    provider: str | None,
) -> None:
    flag_lines = "\n".join(f"- `{name}`" for name in flags) or "- _(none extracted)_"
    follow = []
    if any(name.startswith("BOSS_KMS_") or name == "local_vault" for name in flags):
        follow.append(
            "Production `local_vault` write-through needs a durable master key "
            "flag. Do not use `--env-template`. Do not enable the test-only "
            "ephemeral master flag on goldeneye."
        )
    if any(name in {"secret_refs", "provider_accounts"} for name in flags):
        follow.append(
            "Schema bind needs the listed relations applied on this runtime."
        )
    follow.append("Do not paste keys into tickets or logs.")
    follow_text = "\n".join(f"- {line}" for line in follow)
    text = f"""# BYOK bind blocker

Live bind of an existing env model key into `local_vault` SecretRef did not
complete. This file lists **flag and schema names only**. It does not contain
secret material.

**Not GA.**

## Attempt

- Endpoint: `{endpoint}`
- Backend: `local_vault`
- Provider: `{provider or "unknown"}`
- HTTP status: `{status}`

## Flag / schema names

{flag_lines}

## Operator follow-up (names only)

{follow_text}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _request(
    method: str,
    url: str,
    *,
    api_key: str | None,
    body: dict[str, Any] | None,
    secrets: list[str],
    timeout: float = 30.0,
) -> tuple[int, Any]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-Boss-Api-Key"] = api_key
    req = Request(url, data=encoded, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            status = int(response.status)
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        status = int(error.code)
    except URLError as error:
        raise RuntimeError(
            redact_secrets(f"BOSS API {method} unreachable: {error.reason}", secrets)
        ) from error
    try:
        payload: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": redact_secrets(raw[:200], secrets)}
    return status, sanitize_payload(payload, secrets)


def emit(payload: Mapping[str, Any], secrets: list[str]) -> None:
    dumped = json.dumps(payload, indent=2, sort_keys=True)
    sys.stdout.write(redact_secrets(dumped, secrets) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bind a live env model key into local_vault (dry-run default)."
    )
    parser.add_argument("--apply", action="store_true", help="POST easy-connect (not default).")
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--actor", default=None)
    parser.add_argument("--account-label", default=None)
    parser.add_argument(
        "--blocker-doc",
        default=None,
        help="Path for 422/503 blocker doc (default docs/operations/BYOK_BIND_BLOCKER.md).",
    )
    args = parser.parse_args(argv)

    api_url = resolve_api_url(args.api_url)
    project_id = resolve_project_id(args.project_id)
    actor = resolve_actor(args.actor)
    source = discover_source()
    secrets = _known_secrets(
        [source.secret_value, os.environ.get("BOSS_API_KEY", "")]
    )

    if not args.apply:
        emit(
            dry_run_plan(
                source,
                api_url=api_url,
                project_id=project_id,
                actor=actor,
                account_label=args.account_label,
            ),
            secrets,
        )
        return 0 if source.present else 2

    if not source.present or not source.provider:
        sys.stderr.write(
            "No live model key in env. Expected OPENAI_API_KEY, GROQ_API_KEY, or GROQ.\n"
        )
        return 2

    api_key = (os.environ.get("BOSS_API_KEY") or "").strip()
    body = build_request_body(
        source,
        project_id=project_id,
        actor=actor,
        account_label=args.account_label,
    )
    status, payload = _request(
        "POST",
        f"{api_url.rstrip('/')}{EASY_CONNECT_PATH}",
        api_key=api_key or None,
        body=body,
        secrets=secrets,
    )
    if 200 <= status < 300:
        emit(public_success(payload if isinstance(payload, dict) else {}, secrets), secrets)
        return 0

    flags = extract_blocker_flags(status, payload)
    blocker_path = Path(args.blocker_doc) if args.blocker_doc else repo_root() / BLOCKER_RELATIVE
    if status in {422, 503}:
        write_blocker_doc(
            blocker_path,
            status=status,
            flags=flags,
            endpoint=f"POST {EASY_CONNECT_PATH}",
            provider=source.provider,
        )
        emit(
            {
                "ok": False,
                "dry_run": False,
                "status": status,
                "blocker": True,
                "blocker_doc": str(blocker_path),
                "flags": flags,
                "provider": source.provider,
            },
            secrets,
        )
        return 1

    sys.stderr.write(
        redact_secrets(f"easy-connect failed: HTTP {status}\n", secrets)
    )
    emit(
        {
            "ok": False,
            "dry_run": False,
            "status": status,
            "blocker": False,
            "provider": source.provider,
        },
        secrets,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
