"""The agent could not help with setup, and the reasons were all fixable.

Asked why a GitHub connection failed, it investigated honestly and reported what it hit:

    describe_role: "no role 'the Deployment Manager' on boss-dogfood". Retrying with
                   deployment_manager succeeded.
    read_logs:     "no log file readable from this process".
    git_history:   "fatal: not a git repository".
    ... the recorded event says only "GitHub connection test failed".

Four dead ends, none of them the model's fault: a name it had just been shown, a log that
exists in a table rather than a file, a version question with no version to read, and a
reason that had been discarded. Setup is the first thing every customer touches; answering
questions about it should not require an investigation.
"""
from __future__ import annotations

import inspect

from boss_engine.tools import build_discuss_read_registry
from boss_engine.tools.builtin import _role_name_candidates, build_default_registry


def _tool(name):
    """The tool as REGISTERED. The discuss registry wraps every handler to pin project_id, so
    reading its source finds the wrapper — a test that inspects the wrong function can only
    ever assert that the wrapper exists."""
    return {spec.name: spec for spec in build_default_registry().specs()}[name]


def _admitted(name) -> bool:
    """Is it offered to the agent that answers questions?"""
    return name in {spec.name for spec in build_discuss_read_registry().specs()}


def test_a_role_is_found_by_the_name_a_person_says():
    """"the Deployment Manager" is what the console shows. The table spells it
    deployment_manager. One role, so one lookup."""
    for said in ("the Deployment Manager", "Deployment Manager", "deployment manager"):
        assert "deployment_manager" in _role_name_candidates(said), said


def test_and_the_slug_still_works():
    assert _role_name_candidates("deployment_manager")[0] == "deployment_manager"


def test_a_failed_lookup_says_what_it_tried():
    source = inspect.getsource(_tool("describe_role").handler)
    assert '"tried": candidates,' in source


def test_read_logs_reads_the_log_that_exists_here():
    """In a container the services log to stdout, so the file loop always fails — and the tool
    used to end there. `log_capture` has been writing the same records to `app_logs` all
    along, in every deployment shape, with no docker socket and no mounted volume."""
    source = inspect.getsource(_tool("read_logs").handler)
    assert "app_logs" in source
    assert "log_capture" in source or "BOSS's own capture" in source


def test_and_an_empty_log_is_not_reported_as_an_unavailable_one():
    """Two states that look identical and need opposite responses. Capture writes one row at
    startup, so a truly empty table means the service has not started."""
    source = inspect.getsource(_tool("read_logs").handler)
    assert "means nothing was recorded, NOT that logging is unavailable" in source


def test_git_history_answers_the_question_behind_the_question():
    """"When did the fix ship" on an install with no checkout. Returning git's own "fatal: not
    a git repository" reads as a broken tool; the build stamp is the actual answer, and the
    source in the image is what is executing."""
    source = inspect.getsource(_tool("git_history").handler)
    assert "BOSS_SOURCE_SHA" in source
    assert "has no git history in this install" in source
    assert "read_code" in source


def test_the_build_says_which_source_it_is():
    from boss_engine.services.self_observability import self_snapshot

    build = self_snapshot()["build"]
    assert set(build) == {"source_sha", "built_at", "note"}
    # Unstamped is a real state and says so, rather than inventing a version.
    assert build["source_sha"] or "without a stamp" in build["note"]


def test_one_call_answers_the_whole_setup_question():
    """What the agent had to assemble from events, source and three broken tools."""
    spec = _tool("diagnose_setup")
    assert spec.kind == "read"
    # And the agent that answers questions actually has it.
    assert _admitted("diagnose_setup")
    source = inspect.getsource(spec.handler)
    for expected in ("last_error",              # the REASON a check failed
                     "coding_agents",           # what is installed and connected
                     "connected_engines",
                     "chat_model", "build_model", "role_models",
                     "secret_store",
                     "next_steps"):
        assert expected in source, expected


def test_and_it_reports_what_will_actually_run():
    """A diagnosis that contradicts the console is worse than none: `build_engine_for` reads
    settings.code_agent_engine, which the pipeline layers with the project's choices first."""
    source = inspect.getsource(_tool("diagnose_setup").handler)
    assert "_applied(c, pid, ctx.settings)" in source
    assert "chat_engine_for(c, effective, pid)" in source


def test_and_does_not_hand_back_a_to_do_list_of_vendors():
    """"cursor: curl … | bash" is not a next step for somebody who has never wanted Cursor."""
    source = inspect.getsource(_tool("diagnose_setup").handler)
    assert 'if agent.get("installed") or not any_connected:' in source


def test_it_never_returns_a_credential():
    """It reads connections, agents and stores. None of those may carry a value."""
    source = inspect.getsource(_tool("diagnose_setup").handler)
    for forbidden in ("credential_ref", "token", "secret_value", "api_key"):
        assert f'"{forbidden}"' not in source, forbidden
