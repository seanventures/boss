"""The repair loop's scratch space must not be shipped to the browser.

`finalize_ctx.pending_diff` is how the verifier ratchet re-applies a change after a gate
failure: {path, content} for every file the agent wrote. The engine needs it; a browser never
does. It was being serialized into the console on every page load — measured on goldeneye at
11.4 MB of HTML and 18-67 seconds per page, including EIGHT complete package-lock.json files
at 139-176 KB each. The console looked broken because it was shipping engine internals.
"""

from __future__ import annotations

import json

from apps.api.app.main import without_internal_bulk

LOCKFILE = "x" * 176_100  # the real size measured on goldeneye


def _run() -> dict:
    return {
        "id": "crun_x",
        "status": "completed",
        "finalize_ctx": {
            "work_branch": "boss/code/crun_x",
            "attempt": 2,
            "pending_diff": [
                {"path": "package-lock.json", "content": LOCKFILE},
                {"path": "src/app.py", "content": "print('hi')\n"},
            ],
        },
    }


def test_file_bodies_do_not_cross_the_api_boundary():
    out = without_internal_bulk(_run())
    blob = json.dumps(out)
    assert LOCKFILE not in blob
    assert len(blob) < 1_000, f"payload still {len(blob)} bytes"


def test_paths_and_sizes_survive_so_a_diff_view_can_still_describe_the_change():
    out = without_internal_bulk(_run())
    pending = out["finalize_ctx"]["pending_diff"]
    assert [f["path"] for f in pending] == ["package-lock.json", "src/app.py"]
    assert pending[0]["bytes"] == 176_100
    assert pending[1]["bytes"] == len("print('hi')\n")


def test_the_rest_of_finalize_ctx_is_untouched():
    """Only the bulk goes. work_branch and attempt are what the console legitimately reads."""
    out = without_internal_bulk(_run())
    assert out["finalize_ctx"]["work_branch"] == "boss/code/crun_x"
    assert out["finalize_ctx"]["attempt"] == 2
    assert out["status"] == "completed"


def test_the_engines_own_copy_is_not_mutated():
    """The verifier ratchet reads pending_diff from the DB and must keep full contents."""
    run = _run()
    without_internal_bulk(run)
    assert run["finalize_ctx"]["pending_diff"][0]["content"] == LOCKFILE


def test_runs_without_a_pending_diff_pass_through_unchanged():
    for run in ({"id": "a"},
                {"id": "b", "finalize_ctx": {}},
                {"id": "c", "finalize_ctx": {"pending_diff": None}},
                {"id": "d", "finalize_ctx": "not-a-dict"}):
        assert without_internal_bulk(run) == run


def test_a_malformed_entry_does_not_crash_the_response():
    run = {"id": "e", "finalize_ctx": {"pending_diff": ["oops", {"path": "x.py"}]}}
    out = without_internal_bulk(run)
    assert out["finalize_ctx"]["pending_diff"][0] == "oops"
    assert out["finalize_ctx"]["pending_diff"][1] == {"path": "x.py", "bytes": 0}


def test_both_list_and_detail_endpoints_apply_it():
    """Opening ONE run must not ship a lockfile either — fixing only the list misses its twin."""
    import inspect

    from apps.api.app import main

    list_src = inspect.getsource(main.list_code_tasks_endpoint)
    detail_src = inspect.getsource(main.get_code_task_endpoint)
    # The list goes through the summariser, which applies without_internal_bulk AND the size
    # rule; the detail applies only the former, so opening one run still shows the narratives.
    assert "summarise_run_for_list" in list_src
    assert "without_internal_bulk" in detail_src


# --------------------------------------------------------------------------- #
# The size rule, which is what stops the field-at-a-time chase
# --------------------------------------------------------------------------- #
from apps.api.app.main import (  # noqa: E402
    LIST_STRING_BUDGET, summarise_run_for_list, trim_for_list,
)


def test_stripping_by_name_was_not_enough_which_is_why_the_rule_is_size():
    """Naming `pending_diff` shaved 11.4 MB to 10.0 and left seven more fields behind it.

    Measured on the live 200-row response, the bulk was spread across
    ratchet.files[].content, spec.instructions, state.design.plan, state.design.spec,
    state.security_summary, state.design.standards, state.design.plan_review and
    state.review_summary. A rule about SIZE covers all of them — and the ones nobody has
    added yet.
    """
    run = {
        "id": "crun_x",
        "spec": {"instructions": "i" * 50_000},
        "state": {"design": {"plan": "p" * 40_000, "spec": "s" * 40_000},
                  "security_summary": "sec" * 10_000},
    }
    out = summarise_run_for_list(run)
    for value in (out["spec"]["instructions"], out["state"]["design"]["plan"],
                  out["state"]["design"]["spec"], out["state"]["security_summary"]):
        assert len(value) < LIST_STRING_BUDGET + 80


def test_the_ratchets_second_copy_of_the_file_bodies_also_goes():
    run = {"id": "c", "finalize_ctx": {"ratchet": {"files": [
        {"path": "package-lock.json", "content": "x" * 176_100}]}}}
    out = summarise_run_for_list(run)
    assert out["finalize_ctx"]["ratchet"]["files"] == [
        {"path": "package-lock.json", "bytes": 176_100}]


def test_short_values_and_scalars_are_untouched():
    """The console reads small scalars off a list row; none of them may change."""
    run = {"id": "crun_x", "status": "completed", "title": "Build a todo app",
           "state": {"files_changed": 3, "additions": 120, "url": "http://x/y",
                     "error": None},
           "spec": {"repo": "boss-sandbox", "owner": "paramg"}}
    assert summarise_run_for_list(run) == run


def test_truncation_says_why_it_is_short_rather_than_silently_clipping():
    out = trim_for_list("z" * 5000)
    assert out.startswith("z" * LIST_STRING_BUDGET)
    assert "5000 chars" in out and "open the run" in out


def test_the_detail_endpoint_is_not_trimmed():
    """A list is bounded; opening ONE run must still give the whole thing."""
    import inspect

    from apps.api.app import main

    detail = inspect.getsource(main.get_code_task_endpoint)
    assert "summarise_run_for_list" not in detail, "detail must keep the narratives"
    assert "without_internal_bulk" in detail, "…but still not ship file bodies"
