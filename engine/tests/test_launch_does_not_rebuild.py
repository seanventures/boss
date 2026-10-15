"""Pressing Launch on something already built must not build it again.

BugBox was built, completed, and opened a pull request. Pressing Launch a second time would
have convened a fresh panel, published another PRD, auto-approved it and started another build
-- twenty minutes and several dollars for a second copy of what the operator already had, with
nothing about the click saying that was what it meant.

The existing guard did not help. It asks "is this happening right now", which catches a
double-press and nothing else; once a run finishes, the guard stops applying and the button
silently becomes a spend button.

Building again stays possible. It just has to be asked for, and the asking is what was missing.
"""

from __future__ import annotations

import inspect

from apps.api.app import main as api_main


SOURCE = inspect.getsource(api_main.start_qa_benchmark_app_endpoint)
CODE = "\n".join(ln for ln in SOURCE.splitlines() if not ln.lstrip().startswith("#"))


def test_launch_checks_whether_the_work_already_exists():
    assert "completed_benchmark_build" in CODE


def test_it_answers_with_the_existing_build_instead_of_starting_one():
    assert '"already_built"' in CODE
    # and it returns before anything is convened
    assert CODE.index('"already_built"') < CODE.index("convene_prd_meeting_core(")


def test_building_again_is_possible_but_must_be_asked_for():
    """A guard that cannot be overridden becomes the next complaint."""
    assert '.get("force")' in CODE


def test_the_answer_tells_the_caller_what_to_do():
    """An API that refuses without saying how to proceed just moves the confusion."""
    assert "force=true" in SOURCE


def test_the_lookup_finds_finished_work_not_running_work():
    """The distinction the original guard missed entirely."""
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.completed_benchmark_build)
    assert "r.status = 'completed'" in source
    assert "pull_request_url" in source


def test_a_failed_build_is_not_reported_as_built():
    """It ordered completed-first and still took the newest row when there was none, so a
    benchmark whose build was KILLED reported itself built -- and "View BugBox" landed the
    operator on the failure."""
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.completed_benchmark_build)
    assert "AND r.status = 'completed'" in source
    assert "if row is None:" in source


def test_the_lookup_is_keyed_on_the_app_not_on_how_it_was_launched():
    """The first version keyed on the meeting's directive fingerprint, so it recognised only
    benchmarks convened through the panel with the canonical wording. ApproveIt -- built from
    a PRD meeting typed by hand -- carried none of that and showed "Launch" over an app that
    plainly existed."""
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.completed_benchmark_build)
    for key in ("qa_benchmark", "benchmark_id", "meeting_fingerprint", "ILIKE"):
        assert key in source, key
    # The keys decide WHETHER a run is this app. They must not decide WHICH build to open:
    # ranking by key strength made a well-labelled old build outrank the newer one the
    # operator was actually using. Provenance is still reported so a weak match can be doubted.
    assert "matched_on" in source


def test_the_in_flight_guard_is_still_there_for_double_clicks():
    """The new check answers a different question; it must not replace the old one."""
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.active_duplicate_meeting)
    assert "'running'" in source


def test_the_button_offers_the_existing_build_before_offering_to_repeat_it():
    """The UI half. A response the card ignores changes nothing an operator sees."""
    import pathlib

    # The benchmark card moved verbatim out of code-tasks-ui.tsx when that file became the
    # Factory shell (factory spec §4.1); it lives with the build's other working parts now.
    ui = pathlib.Path(__file__).resolve().parents[2] / "apps/web/app/factory/legacy-build-parts.tsx"
    text = ui.read_text()
    assert '"existing"' in text
    assert "Build it again" in text
    # The plain press must never force.
    assert "launch(false)" in text
    assert "launch(true)" in text


def test_the_catalog_reports_which_benchmarks_are_already_built():
    """A guard the operator only discovers by pressing is not an interface.

    The button said "Launch BugBox" hours after BugBox was built. The press was safe by then --
    the API answers with the existing run -- but the card still invited it, which is the same
    confusion one step later. The catalog now carries `built`, so the card can say so at rest.
    """
    catalog = inspect.getsource(api_main.list_qa_benchmark_apps_endpoint)
    assert "completed_benchmark_build" in catalog
    assert "project_id" in catalog
    # A catalog read must not fail because one entry could not be resolved.
    assert "except Exception" in catalog


def test_the_card_reads_that_state_before_any_click():
    import pathlib

    # The benchmark card moved verbatim out of code-tasks-ui.tsx when that file became the
    # Factory shell (factory spec §4.1); it lives with the build's other working parts now.
    ui = pathlib.Path(__file__).resolve().parents[2] / "apps/web/app/factory/legacy-build-parts.tsx"
    text = ui.read_text()
    assert "benchmark.built?.run_id" in text
    assert "!startedId && alreadyBuilt" in text


def test_view_opens_the_newest_build_not_the_best_labelled_one():
    """"View ApproveIt" opened a superseded build.

    The lookup ranked by how well-labelled a run was, which sounds principled and picked the
    wrong app: the old `QA benchmark: ApproveIt` carries a benchmark id, so it outranked the
    newer PRD-driven ApproveIt the operator wanted -- the one with a live preview and the pull
    request they had open.

    Key strength answers "is this the same app". It says nothing about WHICH build to open.
    """
    from boss_engine.services import meeting_service

    source = inspect.getsource(meeting_service.completed_benchmark_build)
    assert "ORDER BY (p.id IS NOT NULL) DESC, r.created_at DESC" in source
    # The old ranking must be gone, not merely reordered around.
    assert "WHEN r.finalize_ctx->'qa_benchmark'->>'id' = :b THEN 0" not in source


def test_the_badge_and_the_guard_read_one_rule():
    """They disagreed, and the disagreement was visible to an operator.

    The `reportable` split is right: a name or subject match means related work exists, not
    that this benchmark's canary was run here -- its /report 404s. But only the CATALOG applied
    it. The rebuild guard still treated a name match as already-built, so the card offered
    "Launch ApproveIt" and the press was then refused. A guard you can only discover by
    pressing is the defect this area exists to remove.
    """
    from boss_engine.services.meeting_service import REPORTABLE_MATCHES, is_reportable

    assert REPORTABLE_MATCHES == {"benchmark_id", "launched_as_benchmark"}
    assert is_reportable({"matched_on": "benchmark_id"})
    assert not is_reportable({"matched_on": "name"})
    assert not is_reportable(None)

    catalog = inspect.getsource(api_main.list_qa_benchmark_apps_endpoint)
    launch = inspect.getsource(api_main.start_qa_benchmark_app_endpoint)
    assert "is_reportable(" in catalog
    assert "is_reportable(" in launch
    # Neither may carry its own copy of the set.
    for source in (catalog, launch):
        assert '"launched_as_benchmark",' not in source
