"""An operator's .env must reach the container, or it is not configuration -- it is decoration.

The community compose passes a NAMED LIST of variables into api and worker. Anything not on
that list sits in .env with no effect: the operator sets BOSS_CODE_AGENT_MAX_CONCURRENCY, sees
identical behaviour, and reasonably concludes BOSS ignores its own settings -- while the
long-lived install, whose .env is applied by a different mechanism, honours the same key.

"I tested these very thoroughly in reference host why if I change to different VM or local it is
not working and you have to fix - what is this, we cant keep fixing like this for ever!"

That asymmetry is the answer. These tests keep the list honest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from boss_engine.config import Settings

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "scripts/install/community-compose.yml"

#: Their value is a property of the MACHINE, not of the install, so forwarding one into a
#: container hands it a path or address that is wrong there. BOSS_CLAUDE_CLI_PATH on a laptop
#: is /Users/<someone>/.local/bin/claude; inside the container that file does not exist, so
#: forwarding it would break the very CLI it names.
MUST_NOT_FORWARD = {
    "BOSS_CLAUDE_CLI_PATH",
    "BOSS_ARTIFACTS_DIR",  # set per-container to the mounted volume
    "BOSS_CONTROL_PLANE_URL",  # http://api:8000 inside, localhost outside
    "BOSS_SANDBOX_VALIDATION_REPO_PATH",
    "DATABASE_URL",
}

#: Settings whose compose default must equal config.py's, so that an install which sets
#: nothing behaves exactly like a stock one. Deliberately not every forwarded key: a
#: credential's compose default is "" no matter what the field says.
DEFAULT_MUST_MATCH = {
    "BOSS_CODE_AGENT_MAX_CONCURRENCY": "code_agent_max_concurrency",
    "BOSS_CODE_AGENT_MAX_TURNS": "code_agent_max_turns",
    "BOSS_CODE_AGENT_REVIEW_MAX_TURNS": "code_agent_review_max_turns",
    "BOSS_CODE_AGENT_REVIEW_TIMEOUT_SECONDS": "code_agent_review_timeout_seconds",
    "BOSS_CODE_REPAIR_MAX_RETRIES": "code_repair_max_retries",
    "BOSS_SANDBOX_CI_TIMEOUT_SECONDS": "sandbox_ci_timeout_seconds",
    "BOSS_SANDBOX_CI_POLL_INTERVAL_SECONDS": "sandbox_ci_poll_interval_seconds",
    "BOSS_CODE_AGENT_ENGINE": "code_agent_engine",
}

RUNTIME_SERVICES = ("api", "worker")


def _environment(service: str) -> dict:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return compose["services"][service]["environment"]


def _compose_default(raw: str) -> str | None:
    """Read the `x` out of `${VAR:-x}`. Returns None when the entry is not that shape."""
    match = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.*)\}", str(raw).strip())
    return match.group(1) if match else None


@pytest.mark.parametrize("service", RUNTIME_SERVICES)
@pytest.mark.parametrize("variable", sorted(DEFAULT_MUST_MATCH))
def test_the_knob_reaches_the_container(service, variable):
    assert variable in _environment(service), (
        f"{variable} is not passed into the {service} container, so setting it in .env does "
        f"nothing. That is indistinguishable, from the operator's seat, from BOSS ignoring it."
    )


@pytest.mark.parametrize("service", RUNTIME_SERVICES)
@pytest.mark.parametrize("variable,field", sorted(DEFAULT_MUST_MATCH.items()))
def test_an_unset_knob_still_behaves_like_a_stock_install(service, field, variable):
    """`${VAR:-}` does not leave a variable unset -- it SETS it to empty, overriding the real
    default with nothing. For BOSS_CODE_AGENT_ENGINE that turns "claude_code" into "", which
    is a broken install produced by a line that looks like a safe passthrough."""
    default = _compose_default(_environment(service)[variable])
    assert default is not None, f"{variable} must be written as ${{{variable}:-<default>}}"
    expected = str(getattr(Settings(_env_file=None), field))
    assert default == expected, (
        f"{variable} defaults to {default!r} in compose but {expected!r} in config.py, so an "
        f"install that sets nothing does not behave like a stock one."
    )


@pytest.mark.parametrize("service", RUNTIME_SERVICES)
def test_host_specific_values_are_never_forwarded(service):
    """A path that is correct on the host is wrong in the container. Forwarding one is worse
    than forwarding nothing, because it replaces a working default with a broken value."""
    forwarded = set(_environment(service))
    leaked = sorted(forwarded & (MUST_NOT_FORWARD - {"BOSS_ARTIFACTS_DIR", "BOSS_CONTROL_PLANE_URL", "DATABASE_URL"}))
    assert not leaked, f"{service} forwards host-specific values: {leaked}"


@pytest.mark.parametrize("service", RUNTIME_SERVICES)
def test_the_container_specific_values_are_set_explicitly_not_passed_through(service):
    """These three MUST be the container's own value, not the host's. They are in the
    must-not-forward set and also required to be present -- set, but not from .env."""
    environment = _environment(service)
    for variable in ("BOSS_ARTIFACTS_DIR", "DATABASE_URL"):
        assert variable in environment, f"{service} must set {variable} itself"
        assert "${" not in str(environment[variable]) or "BOSS_DB_PASSWORD" in str(
            environment[variable]
        ), f"{service}'s {variable} must not be taken from the host's .env"
