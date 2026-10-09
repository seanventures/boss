"""What a code agent's subprocess is allowed to see in its environment.

THIS EXISTS BECAUSE THE ANSWER USED TO BE "EVERYTHING". The agent was spawned with
``env={**os.environ, ...}``, so it inherited the entire environment of the control-plane
container: DATABASE_URL, deploy tokens, object-store keys, every provider credential.

On 2026-09-02 an agent's QA step ran the API test suite — which is its job — and the suite did
what an integration suite does: cleared the tables before running. The DATABASE_URL it found in
its environment was production. 204 tables.

The test fixture was not the defect and deleting it would not have helped; an agent holding
production credentials can reach the database through a migration, a psql, or three lines of
Python it writes itself. The defect was that the credentials were there to find.

So: DENY BY DEFAULT. A variable reaches the agent only if it is named here, and a short list of
credential-shaped names can never be allowed through by any mechanism at all. If a build breaks
because something is missing, the withheld names are logged — that is a five-minute fix, and the
alternative is what already happened.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, Mapping

log = logging.getLogger(__name__)


#: Exact names the agent needs to run at all: a shell, a toolchain, and its own model
#: credential. Anything genuinely required and missing shows up in the withheld-names log.
ALLOW_EXACT: frozenset[str] = frozenset({
    # A working process.
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "TERM", "TZ", "HOSTNAME",
    # Keep test/build commands noninteractive in the sandbox.
    "CI",
    # Text handling. A build that cannot decode UTF-8 fails in ways nobody enjoys debugging.
    "LANG", "LC_ALL", "LC_CTYPE",
    # TLS trust. Without these, every https call from the sandbox fails.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "CURL_CA_BUNDLE",
    # The agent's own model access. This is a credential, and it is the one it cannot work
    # without — it buys model turns, not access to customer data.
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "CLAUDE_CONFIG_DIR",
    # Committing inside the sandbox. Identity only; no push credential.
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    # Never block a clone waiting for a password prompt nobody can answer.
    "GIT_TERMINAL_PROMPT",
    # Shared tool caches — the reason this env is built at all (see _shared_tool_caches).
    "npm_config_cache", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
})

#: Prefixes for toolchain configuration. Deliberately narrow: these configure npm, pip and
#: Playwright, and none of them is where anybody keeps a database password.
ALLOW_PREFIXES: tuple[str, ...] = (
    "NPM_CONFIG_", "npm_config_", "PLAYWRIGHT_", "PIP_", "PYTHONDONTWRITE", "PYTHONUNBUFFERED",
    "NODE_OPTIONS",
)

#: Never, by any route. Not via ALLOW_EXACT, not via the operator escape hatch below.
#:
#: DATABASE_URL is first for the obvious reason. The rest are the other ways an agent could
#: reach production: the deploy API, the object store, the search index, the platform's own
#: provider keys. An agent that needs one of these is an agent doing something it should be
#: asking a human for.
NEVER: frozenset[str] = frozenset({
    "DATABASE_URL", "BOSS_DATABASE_URL", "AC_DATABASE_URL", "POSTGRES_PASSWORD", "PGPASSWORD",
    # Named generically on purpose: the engine does not know which platform it is deployed on,
    # and any host-specific "<PLATFORM>_OPS_TOKEN" is caught by SECRET_SHAPED regardless.
    "OPS_TOKEN", "X_OPS_TOKEN",
    "BOSS_API_KEYS", "BOSS_API_KEY", "BOSS_CONSOLE_API_KEY",
    # BOSS's own bearer keys, minted from the host shell: the host tools' key holds every
    # permission the shared key did (step-up exempt), the MCP key a project's (id-sec-final2).
    "BOSS_HOST_API_KEY", "BOSS_MCP_API_KEY", "BOSS_MASTER_KEY", "BOSS_MASTER_KEY_FILE",
    "BOSS_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "S3_ACCESS_KEY", "S3_SECRET_KEY", "MINIO_ROOT_PASSWORD",
    "MEILI_MASTER_KEY", "OPENAI_API_KEY",
})

#: Substrings that mark a name as credential-shaped. Anything matching is withheld unless it is
#: explicitly in ALLOW_EXACT — which is how ANTHROPIC_API_KEY and the OAuth token get through,
#: on purpose and in writing, rather than by accident of pattern matching.
SECRET_SHAPED: tuple[str, ...] = (
    "SECRET", "PASSWORD", "PASSWD", "TOKEN", "CREDENTIAL", "PRIVATE_KEY", "API_KEY", "APIKEY",
)

#: Operator escape hatch: extra names to pass through, comma-separated. It cannot override
#: NEVER — the point of that list is that no configuration change can reopen this hole.
EXTRA_ALLOW_VAR = "BOSS_AGENT_ENV_ALLOW"


def _extra_allowed(source: Mapping[str, str]) -> set[str]:
    raw = (source.get(EXTRA_ALLOW_VAR) or "").strip()
    return {n.strip() for n in raw.split(",") if n.strip()} if raw else set()


#: …and the CLASS: any BOSS_* key or master key a future release adds is never passed either.
NEVER_SHAPED_PREFIX = "BOSS_"
NEVER_SHAPED: tuple[str, ...] = ("API_KEY", "MASTER_KEY")


def is_allowed(name: str, extra: Iterable[str] = ()) -> bool:
    upper = name.upper()
    if upper in {n.upper() for n in NEVER}:
        return False
    if upper.startswith(NEVER_SHAPED_PREFIX) and any(m in upper for m in NEVER_SHAPED):
        return False
    if name in ALLOW_EXACT or name in set(extra):
        return True
    if any(marker in upper for marker in SECRET_SHAPED):
        return False
    return name.startswith(ALLOW_PREFIXES)


def agent_environment(
    source: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment a code agent's subprocess should run with.

    `overrides` (the shared tool caches) are applied after filtering, because they are values
    BOSS computed rather than anything inherited — but they are filtered too, so this function
    is the single place where the answer to "what can the agent see" is decided.
    """
    source = os.environ if source is None else source
    extra = _extra_allowed(source)

    kept: dict[str, str] = {k: v for k, v in source.items() if is_allowed(k, extra)}
    for key, value in (overrides or {}).items():
        if is_allowed(key, extra):
            kept[key] = value

    # A BUILD's cross-app links (app_links.run_env, F5 §5.3): BOSS_LINK_<ALIAS>_* computed for
    # THIS run when it started, added after the filter on purpose (they are values BOSS minted
    # for this build, test-only and scoped to one link), and only inside that build's owner scope.
    from ..app_links import run_env as _link_env

    kept.update(_link_env.current())

    withheld = sorted(set(source) - set(kept))
    if withheld:
        # Names only. Logging a withheld value would defeat the entire point of withholding it.
        log.info(
            "agent environment: passed %d variables, withheld %d (%s)",
            len(kept), len(withheld), ", ".join(withheld[:40]),
        )
    return kept
