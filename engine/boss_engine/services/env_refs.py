"""WHICH OF THIS HOST'S OWN MATERIAL A SECRET REFERENCE MAY NAME (id-sec-final2, the /v1/secret-refs lead).

A secret reference of kind `env` reads an environment variable of the process that resolves it, and
one of kind `local_runner` reads a file on its host. Both were unrestricted: a project Admin (who
holds `secret:manage` and `integration:manage` at their project) could name `env:DATABASE_URL`,
`env:BOSS_MASTER_KEY` or `local_runner:/proc/self/environ` and bind it to an integration whose base
URL they choose — a work tracker's test call then carried the value to that URL as its API key.
`work_tracker_adapters.resolve_credential` read `env:<anything>` straight from the process too.

The rule, in one place, applied where the value is READ (secret_backends.EnvBackend / FileBackend,
resolve_target_secret, work_tracker_adapters.resolve_credential) and where a reference is CREATED
(secret_refs.parse_credential_ref), so no consumer can route around it:

  * an `env` name must be on the ALLOWLIST: the provider credentials BOSS documents (KNOWN, and the
    provider families in KNOWN_PREFIXES), plus what the operator adds on the host in
    BOSS_SECRET_REF_ENV_ALLOW (comma-separated names; `PREFIX*` for a family). A person can never
    extend it: it is the API process's own environment.
  * and it is NEVER one of BOSS's own secrets, whatever the allowlist says (NEVER, NEVER_SHAPED):
    the database, the master key, BOSS's bearer keys and session/egress secrets.
  * a `local_runner` path is never under /proc, /sys, /dev or /etc, nor BOSS's identity directory
    or master-key file.
"""
from __future__ import annotations

import os
import re

#: Provider credentials BOSS's own integrations name as `env:` references.
KNOWN: frozenset[str] = frozenset({
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY", "MISTRAL_API_KEY",
    "PLANE_API_KEY", "PLANE_API_TOKEN", "JIRA_API_TOKEN", "LINEAR_API_KEY", "AZURE_DEVOPS_PAT",
    "ASANA_ACCESS_TOKEN", "BOSS_TRACKER_TOKEN",
    "BOSS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
    "DIGITALOCEAN_TOKEN", "DO_API_TOKEN", "CLOUDFLARE_API_TOKEN",
})

#: Provider families: every variable under one of these is a provider credential or setting.
KNOWN_PREFIXES: tuple[str, ...] = (
    "BOSS_AWS_", "BOSS_AZURE_", "BOSS_GCP_", "BOSS_DIGITALOCEAN_", "BOSS_CLOUDFLARE_", "BOSS_OVH_",
    "BOSS_SUPABASE_", "BOSS_HETZNER_", "BOSS_LINODE_", "BOSS_VULTR_", "BOSS_SSH_",
)

#: A GitHub token the operator named for a connection (GitHub › Easy connect's
#: `env_credential_key`, e.g. BOSS_ACME_GITHUB_TOKEN): the shape names what it is.
KNOWN_SUFFIXES: tuple[str, ...] = ("_GITHUB_TOKEN",)

ALLOW_VAR = "BOSS_SECRET_REF_ENV_ALLOW"

#: BOSS's own secrets. Never resolvable through a reference, not even when allowed.
NEVER: frozenset[str] = frozenset({
    "DATABASE_URL", "BOSS_DATABASE_URL", "AC_DATABASE_URL", "POSTGRES_PASSWORD", "POSTGRES_USER",
    "PGPASSWORD", "PGPASSFILE",
    "BOSS_MASTER_KEY", "BOSS_MASTER_KEY_FILE",
    "BOSS_API_KEY", "BOSS_API_KEYS", "BOSS_CONSOLE_API_KEY", "BOSS_HOST_API_KEY", "BOSS_MCP_API_KEY",
    "BOSS_CONSOLE_PASSWORD", "BOSS_SESSION_SECRET", "BOSS_CSRF_SECRET", "BOSS_SECRET_KEY",
    "BOSS_EGRESS_KEY", "BOSS_EGRESS_SERVICE_KEY", "BOSS_SETUP_TOKEN",
    "BOSS_VAULT_TOKEN", "VAULT_TOKEN", "BOSS_INFISICAL_TOKEN", "INFISICAL_TOKEN",
    "MEILI_MASTER_KEY", "MINIO_ROOT_PASSWORD", "OPS_TOKEN", "X_OPS_TOKEN",
})
#: …and the shape of any future one.
NEVER_SHAPED = re.compile(
    r"(DATABASE_URL|^PG|^POSTGRES_|MASTER_KEY|^BOSS_(?!(AWS|AZURE|GCP|DIGITALOCEAN|CLOUDFLARE|OVH|"
    r"SUPABASE|HETZNER|LINODE|VULTR|SSH)_).*(API_KEYS?|SESSION|CSRF|EGRESS|SIGNING|JWT|IDENTITY))")

_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")


def _operator_allowed() -> tuple[set[str], tuple[str, ...]]:
    raw = os.environ.get(ALLOW_VAR, "")
    names, prefixes = set(), []
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        if item.endswith("*") and len(item) > 1:
            prefixes.append(item[:-1])
        else:
            names.add(item)
    return names, tuple(prefixes)


def env_name_allowed(name: str) -> bool:
    """May a secret reference name this environment variable?"""
    name = (name or "").strip()
    if not _NAME.fullmatch(name) or name == ALLOW_VAR:
        return False
    if name in NEVER or NEVER_SHAPED.search(name):
        return False
    names, prefixes = _operator_allowed()
    return (name in KNOWN or name.startswith(KNOWN_PREFIXES) or name.endswith(KNOWN_SUFFIXES)
            or name in names or name.startswith(prefixes))


def refusal(name: str) -> str:
    """The words for a refused name (never its value)."""
    return (f"A secret reference cannot name the environment variable {name!r}: only the provider "
            f"credentials BOSS knows, or ones the operator lists in {ALLOW_VAR} on the host, and "
            f"never one of BOSS's own secrets.")


_BLOCKED_DIRS = ("/proc", "/sys", "/dev", "/etc")


def _identity_paths() -> tuple[str, ...]:
    out = ["/var/lib/boss/identity"]
    for var in ("BOSS_MASTER_KEY_FILE", "BOSS_EGRESS_SERVICE_KEY_FILE"):
        value = os.environ.get(var)
        if value:
            out += [value, os.path.dirname(value) or value]
    return tuple(os.path.realpath(p) for p in out)


def file_path_allowed(path: str) -> bool:
    """May a `local_runner` (file) reference read this path? Judged on the real path, so a symlink
    into a refused place is refused too."""
    if not path:
        return False
    real = os.path.realpath(os.path.expanduser(path))
    blocked_dirs = _BLOCKED_DIRS + tuple(os.path.realpath(d) for d in _BLOCKED_DIRS)
    for blocked in blocked_dirs + _identity_paths():
        if real == blocked or real.startswith(blocked.rstrip("/") + "/"):
            return False
    base = os.path.basename(real).lower()
    return not (base == ".env" or base.endswith(".env") or base == "master.key")
