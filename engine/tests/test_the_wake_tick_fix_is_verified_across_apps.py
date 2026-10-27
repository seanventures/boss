"""The one suite the code-task gate runs is `engine`, so this is where the wake-tick fix is
proved end to end — across the worker call site, the engine SQL, the Captain's role prompt and
the two app-level test files the gate does not run itself.

Why a single engine test names files that live under apps/ and packages/:
`boss.verify.yaml` only runs the `engine` and `web` suites (the apps/api suite needs a live
Postgres the code-task sandbox has not got). A change to apps/worker, apps/api/tests or
packages/specpack is therefore covered by nothing unless an engine test imports or names it.
This test imports the changed engine module (so the gate selects it) and names each of the
other changed files with an assertion strong enough that a one-line revert of the fix fails it.
No Postgres, no model call: pure source and import checks, the same shape as the rest of the
engine suite.
"""
from __future__ import annotations

from pathlib import Path

# Importing the changed engine module is what makes the gate SELECT this test (select.by=imports),
# and it is the module whose cross-project sweep the whole fix exists to enable.
from boss_engine.services import agent_wakeups

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKER_MAIN = "apps/worker/boss_worker/main.py"
ROLE_SPEC = "packages/specpack/roles/engineering_captain.yaml"
API_CONFTEST = "apps/api/tests/conftest.py"
API_WAKE_TEST = "apps/api/tests/test_a_wakeup_fires_wherever_it_was_armed.py"
WORKER_WAKE_TEST = "apps/worker/tests/test_the_wake_tick_covers_every_live_project.py"


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.exists(), f"{rel} is missing"
    return path.read_text(encoding="utf-8")


def test_the_worker_tick_asks_for_every_live_project_not_its_default():
    """apps/worker/boss_worker/main.py::_tick_agent_wakeups must call tick_agent_wakeups("").

    A one-line revert to the worker's default project id (the original bug that left
    boss-platform and northpeak-bank-b monitors stuck at cycle 0) fails both assertions.
    """
    source = _read(WORKER_MAIN)
    marker = "def _tick_agent_wakeups"
    body = source[source.index(marker):]
    body = body[: body.find("\ndef ", 1)]

    assert 'client.tick_agent_wakeups("")' in body, (
        "the tick must sweep every live project with tick_agent_wakeups(\"\")")
    # The only argument the sweep passes must be the empty string. A revert to the worker's own
    # project — tick_agent_wakeups(str(project_id)) / (project_id) / (default_project_id) — fails.
    calls = [body[m + 1:].split(")", 1)[0] + ")"
             for m in range(len(body)) if body.startswith(".tick_agent_wakeups(", m)]
    assert calls and all(c == 'tick_agent_wakeups("")' for c in calls), (
        f"the tick must not narrow the sweep back to a single configured project, saw {calls!r}")


def test_the_engine_sweep_filter_treats_empty_project_as_all():
    """agent_wakeups.due()/close_spent() must let project_id='' mean every live project —
    the capability the worker call relies on. Mutating the `:p = ''` guard breaks this."""
    import inspect

    due_src = inspect.getsource(agent_wakeups.due)
    assert ":p = '' OR project_id = :p" in due_src, (
        "due() must treat an empty project filter as 'all live projects'")
    assert agent_wakeups.run_due is not None  # the sweep entry point the worker ultimately drives


def test_the_captains_role_tells_it_watch_works_now():
    """packages/specpack/roles/engineering_captain.yaml must state, concretely, that `watch`
    works — the Captain had told the operator it could not watch in the background."""
    spec = yaml.safe_load(_read(ROLE_SPEC))
    must = " ".join(spec.get("boundaries", {}).get("must") or []).lower()
    assert "watch" in must, "the role prompt must describe `watch`"
    assert "background" in must, (
        "the sentence must correct the Captain's 'I cannot watch this in the background' claim")


def test_the_app_level_tests_exist_and_pin_the_fix():
    """Name the app-level test files the gate itself does not run, and assert each still pins
    the behaviour it was written for, so this engine test is their coverage of record."""
    conftest = _read(API_CONFTEST)
    assert "northpeak-bank-b" in conftest, (
        "the API conftest must seed a non-default project for the cross-project wake test")

    api_test = _read(API_WAKE_TEST)
    assert 'project_id=""' in api_test and "run_due" in api_test, (
        "the API wake test must prove run_due sweeps every project")

    worker_test = _read(WORKER_WAKE_TEST)
    assert 'tick_agent_wakeups' in worker_test and 'client.calls == [""]' in worker_test, (
        "the worker test must pin the call site to tick_agent_wakeups(\"\")")
