"""The brief handed to the coding agent must ask for the app we actually want.

The scaffold was made full-stack, but the instructions sent with it still described a
frontend-only project -- and worse, actively steered away from a backend:

    "a hosted Postgres the frontend talks to DIRECTLY, NO backend server"
    "if the keys are still the YOUR_... placeholders, DO NOT call Supabase --
     keep the mock success state"

So the agent built an app that only pretended to save, because that is what it was told to
build. Changing the seed alone would not have fixed it; it would merely have left the brief
contradicting the code on the branch.

Acceptance was the other half. The three criteria were "build passes", "UI renders", "few
files" -- all satisfied by an app that forgets everything on reload.
"""

from __future__ import annotations

import inspect
import re

from boss_engine.services import prd_decomposition


SOURCE = inspect.getsource(prd_decomposition.start_web_app_build)


def test_the_brief_does_not_tell_the_agent_to_skip_the_backend():
    """The literal instruction that produced the in-memory app."""
    assert "NO backend server" not in SOURCE
    assert "keep the mock success state" not in SOURCE


def test_the_brief_describes_the_scaffold_that_is_actually_on_the_branch():
    """A brief describing files that are not there is worse than no brief."""
    for expected in ("server/index.js", "server/db.js", "server/migrations", "npm start"):
        assert expected in SOURCE, expected


def test_the_brief_forbids_keeping_application_data_in_component_state():
    assert re.search(r"[Dd]o NOT keep application data in useState", SOURCE)
    assert "localStorage" in SOURCE


def test_acceptance_requires_data_to_survive_a_reload():
    """The criterion whose absence let a forgetful app pass every check."""
    assert "still there after a full" in SOURCE
    assert "reload" in SOURCE


def test_verification_runs_more_than_a_build():
    """`npm run build` cannot fail on an app that saves nothing; the e2e suite can."""
    assert 'test_commands=["npm run verify"]' in SOURCE
    assert 'test_commands=["npm run build"]' not in SOURCE


def test_the_seed_and_the_brief_agree():
    """One decision, stated twice, is a drift waiting to happen -- this is that drift's test."""
    signature = inspect.signature(prd_decomposition.seed_web_scaffold)
    assert signature.parameters["kind"].default == "fullstack"
    from boss_engine.services.scaffold_service import scaffold_project

    files = scaffold_project(name="x", kind="fullstack")["files"]
    for path in ("server/index.js", "server/db.js"):
        assert path in files
        assert path in SOURCE
