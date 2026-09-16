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
    start = text.index(f"\n  {name}:")
    rest = text[start + 1:]
    following = [rest.index(f"\n  {other}:")
                 for other in ("db", "api", "worker", "web", "redis")
                 if f"\n  {other}:" in rest]
    return rest[:min(following)] if following else rest


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
