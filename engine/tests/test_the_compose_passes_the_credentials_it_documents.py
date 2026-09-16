"""A credential in `.env` that compose never forwards is a credential the container never sees.

The setup doc's simplest route is "write it into the deployment's env file and restart". For
the compose install BOSS ships, that only works if the compose file forwards the variable into
the service. It forwarded ANTHROPIC_API_KEY and nothing else, so an operator following the
documented path would put a valid Codex credential in `.env`, restart, and get "not signed in"
while looking straight at the value they had just set — with nothing anywhere saying why.

This pins the join: every credential a coding harness can be authorised by has to reach the
services that run coding work.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "scripts" / "install" / "community-compose.yml"

#: Services that run coding work and therefore need a subscription credential.
NEEDS_CREDENTIALS = ("api", "worker")


@pytest.fixture(scope="module")
def compose() -> str:
    if not COMPOSE.exists():  # pragma: no cover - engine installed standalone
        pytest.skip("the community compose file is not in this checkout")
    return COMPOSE.read_text()


def _service_block(text: str, name: str) -> str:
    """One service's YAML, up to whatever service is declared next.

    Found by indentation rather than by a list of known names. The list version silently
    mis-sliced the moment a service was added between two it knew about: `db`'s block ran on
    through the new entries and inherited their `profiles:`, reporting an always-on service as
    optional.
    """
    start = text.index(f"\n  {name}:")
    rest = text[start + 1:]
    following = re.search(r"\n  [a-z][a-z0-9-]*:\n", rest)
    return rest[:following.start()] if following else rest


def test_every_harness_credential_reaches_the_services_that_need_it(compose: str) -> None:
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES

    required = {spec.auth_file_secret or spec.token_env
                for spec in BUILTIN_CLI_HARNESSES.values()} - {""}
    required.add("CLAUDE_CODE_OAUTH_TOKEN")     # not a CLI spec; same need

    missing = []
    for service in NEEDS_CREDENTIALS:
        block = _service_block(compose, service)
        for name in sorted(required):
            if f"{name}:" not in block:
                missing.append(f"{service} does not receive {name}")
    assert not missing, (
        "compose drops credentials the setup doc tells operators to set:\n"
        + "\n".join(missing))


def test_a_credential_is_optional_rather_than_required_to_boot(compose: str) -> None:
    """`${VAR}` with no default makes compose warn and blank it; `:-` says optional on purpose.

    Most installations set one subscription, or none. Booting must not depend on all of them.
    """
    for service in NEEDS_CREDENTIALS:
        block = _service_block(compose, service)
        for line in block.splitlines():
            if "CODEX_AUTH_JSON" in line or "CLAUDE_CODE_OAUTH_TOKEN" in line:
                assert ":-}" in line, f"{service}: {line.strip()} should default to empty"


# --- the optional secret store ----------------------------------------------


def test_the_secret_store_is_opt_in(compose: str) -> None:
    """An evaluator should not have to pull a secret store to try the product.

    Both services sit behind a profile, so `docker compose up` is unchanged and
    `--profile secrets` adds them. The file already used this pattern for `web`, so this is the
    shape the stack had rather than a new convention invented for Infisical.
    """
    for service in ("infisical", "infisical-redis"):
        block = _service_block(compose, service)
        assert 'profiles: ["secrets"]' in block, f"{service} is not opt-in"


def test_default_services_did_not_change(compose: str) -> None:
    """Adding an optional store must not alter what a plain `up` starts."""
    always_on = [name for name in ("db", "api", "worker")
                 if 'profiles:' not in _service_block(compose, name)]
    assert always_on == ["db", "api", "worker"]


def test_infisical_shares_the_postgres_boss_already_runs(compose: str) -> None:
    """Otherwise bundling a secret store costs two containers instead of one."""
    block = _service_block(compose, "infisical")

    assert "@db:5432/infisical" in block, "it should use BOSS's Postgres, own database"
    assert (REPO / "scripts" / "install" / "initdb" / "10-infisical.sql").is_file(), (
        "the infisical database is never created")


def test_the_encryption_key_has_no_default(compose: str) -> None:
    """A shipped default is the same key on every installation that forgot to change it.

    Refusing to start is the safe failure. The message names the generator, so the operator is
    one command from fixed rather than one search away.
    """
    block = _service_block(compose, "infisical")

    for required in ("ENCRYPTION_KEY", "AUTH_SECRET"):
        line = [ln for ln in block.splitlines() if ln.strip().startswith(f"{required}:")][0]
        assert ":?" in line, f"{required} has a default; it must refuse to start instead"
        assert "gen_infisical_keys.sh" in line, f"{required} does not say how to fix it"


def test_the_secret_store_settings_reach_the_services(compose: str) -> None:
    """Bootstrap writes these into .env; compose has to carry them into the container.

    Measured on a real local stack: without forwarding, `bootstrap_infisical.sh` reported
    success, .env held all three settings, and BOSS answered "infisical is not configured" —
    configured and unreachable at the same time, with the error naming the thing that was set.
    """
    required = ("BOSS_INFISICAL_URL", "BOSS_INFISICAL_TOKEN", "BOSS_INFISICAL_PROJECT_ID")
    missing = []
    for service in NEEDS_CREDENTIALS:
        block = _service_block(compose, service)
        missing += [f"{service} does not receive {n}" for n in required if f"{n}:" not in block]
    assert not missing, "\n".join(missing)


def test_bootstrap_writes_an_address_the_container_can_reach() -> None:
    """127.0.0.1 inside the api container is that container, not the host.

    The script probes Infisical from the host and BOSS talks to it from the compose network,
    so the address it PROBES and the address it WRITES are different on purpose.
    """
    script = (REPO / "scripts" / "install" / "bootstrap_infisical.sh").read_text()

    assert 'BOSS_URL="${BOSS_INFISICAL_URL:-http://infisical:8080}"' in script
    assert 'echo "BOSS_INFISICAL_URL=$BOSS_URL"' in script, (
        "it writes the host URL, which the container cannot reach")
