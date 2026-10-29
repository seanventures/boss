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
    """What the agent had to assemble from events, source and three broken tools: now the very
    view Repo and models renders, plus the fixes in order with a console link each."""
    spec = _tool("diagnose_setup")
    assert spec.kind == "read"
    # And the agent that answers questions actually has it.
    assert _admitted("diagnose_setup")
    source = inspect.getsource(spec.handler)
    for expected in ("setup_view.build(", "who_does_what", "verdict", "next_steps", "href"):
        assert expected in source, expected


def test_and_it_reports_what_will_actually_run(monkeypatch):
    """A diagnosis that contradicts the console is worse than none: the chat and the page read
    one view, so whatever the page says about who does each job, the tool says too."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    from boss_engine.services import setup_view

    view = {"verdict": {"label": "Ready", "tone": "green", "tip": "t"},
            "repository": {"connection": None, "repos": [{
                "full_name": "o/r", "pill": {"tone": "green", "label": "Working", "tip": ""}}]},
            "models": [{"label": "Claude Code", "source_id": "anthropic_claude_code",
                        "pill": {"tone": "red", "label": "Login expired", "tip": "Fix it."}}],
            "jobs": [{"job": "build", "resolved": {"label": "Auto · Claude Code"}}]}
    monkeypatch.setattr(setup_view, "build", lambda *_a, **_k: view)
    ctx = SimpleNamespace(begin=lambda: nullcontext(None), settings=None)
    out = _tool("diagnose_setup").handler(ctx, project_id="p", actor="ceo", args={})
    assert out["view"] is view
    assert out["who_does_what"] == {"build": "Auto · Claude Code"}
    assert out["next_steps"] == [{
        "row": "models", "what": "Claude Code", "state": "Login expired", "fix": "Fix it.",
        "href": "/?area=setup&focus=models&source=anthropic_claude_code"}]


def test_it_never_returns_a_credential():
    """It reads connections, agents and stores. None of those may carry a value."""
    source = inspect.getsource(_tool("diagnose_setup").handler)
    for forbidden in ("credential_ref", "token", "secret_value", "api_key"):
        assert f'"{forbidden}"' not in source, forbidden


# -- and it can read the history of a repository this project connected ------------------------

def test_a_clone_uses_the_credential_the_project_connected():
    """`settings.github_token` is the OLD way of naming a target repo, and it is empty on any
    install that connected one through the wizard — the credential lives in the connection and
    its value in a secret store. So reading a private repository's history died with git asking
    for a username on a terminal nobody is at:

        fatal: could not read Username for 'https://github.com': No such device or address

    The resolver lives in the SERVICE layer now. It used to live in tools/builtin.py, which is
    why preview_service could not reach it and went on reading the setting directly.
    """
    from boss_engine.services.github_connections import github_token_for
    from boss_engine.tools.builtin import _repo_root

    assert "token" in inspect.signature(_repo_root).parameters
    source = inspect.getsource(github_token_for)
    assert "github_connections" in source or "GitHubConnectionService" in source
    assert "token_for_repository" in source


def test_the_preview_clone_uses_it_too():
    """THE BUG THIS ALL EXISTS FOR, and the one the resolver did not fix on its own.

    preview_service built its clone URL from `settings.github_token` in three places. On an
    install whose repository was connected through the wizard that is the empty string, so git
    got an anonymous URL for a private repository and every preview on the run failed the same
    way — measured at fifty-plus attempts on one run, while the connection itself reported
    `active` with `last_test_status: passed`.
    """
    from boss_engine.services import preview_service

    source = inspect.getsource(preview_service)
    assert "token = settings.github_token" not in source, \
        "a preview clone is reading the install-wide setting again"
    assert source.count("github_token_for(_c, settings, project_id") == 3, \
        "all three clone paths must resolve the project's own credential"


def test_no_clone_path_anywhere_reads_the_install_wide_setting():
    """THE SAME BUG LIVED IN THREE SERVICES, and fixing one did not find the others.

    preview_service was reported. deploy_service and agent_loop had it too — the same
    `clone_url_for(..., settings.github_token)` shape, which is an anonymous clone of a private
    repository on any install whose repo was connected through the wizard. This walks every
    service and fails if a clone URL is built from the install-wide field again.
    """
    import pathlib, re

    services = pathlib.Path(__file__).resolve().parents[1] / "boss_engine" / "services"
    offenders = []
    for path in sorted(services.glob("*.py")):
        if path.name == "github_connections.py":
            continue  # where the resolver lives, and reads the field on purpose
        text_ = path.read_text()
        if "clone_url_for(" not in text_:
            continue
        for line in text_.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"settings\.github_token", stripped):
                offenders.append(f"{path.name}: {stripped[:70]}")
    assert not offenders, "clone paths reading the install-wide token:\n  " + "\n  ".join(offenders)


def test_and_the_environment_still_wins_where_it_is_set():
    """An install configured the old way must be unaffected — and must not pay for a database
    round trip to discover that."""
    from boss_engine.services.github_connections import github_token_for

    class RefusesToBeUsed:
        def execute(self, *args, **kwargs):
            raise AssertionError("the configured token must be returned without a lookup")

    class Configured:
        github_token = "ghp_from_the_environment"

    assert github_token_for(RefusesToBeUsed(), Configured(), "proj_x", "o", "r") == \
        "ghp_from_the_environment"


def test_a_missing_credential_is_an_empty_string_not_a_traceback():
    """No connection, or an unreadable store, must leave the clone to report the real problem."""
    from boss_engine.services.github_connections import github_token_for

    class Exploding:
        def execute(self, *args, **kwargs):
            raise RuntimeError("store unavailable")

    class Unset:
        github_token = ""

    assert github_token_for(Exploding(), Unset(), "proj_x", "o", "r") == ""


def test_every_caller_passes_it():
    """A resolver nothing calls is the same bug with more code."""
    from boss_engine.tools import builtin

    source = inspect.getsource(builtin)
    assert source.count("_repo_root(ctx.settings, repo, ref,") == 4
    assert source.count("token=_github_token_for(ctx, project_id, repo)") == 4


def test_and_a_missing_token_is_answered_not_raised():
    """No connection, or an unreadable store, must leave the clone to report the real problem
    rather than ending the turn in a traceback."""
    from boss_engine.tools.builtin import _github_token_for

    source = inspect.getsource(_github_token_for)
    assert "except Exception" in source
    assert 'return ""' in source
