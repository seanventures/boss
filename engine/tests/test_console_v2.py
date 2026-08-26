"""Console v2's data layer — the shape the screens read, and the traps it must not repeat.

Every assertion here exists because the equivalent mistake was made in v1 this week.
"""

import inspect

import pytest

from boss_engine.services import console_v2


def test_one_screen_is_one_query_set():
    """v1 issues 82 requests per render and shows nothing until the slowest answers — TTFB 24s,
    of which 13.2s was a single feed only one area displayed. v2's rule is one screen, one
    call, so a page is bounded by its own queries rather than by its unluckiest dependency."""
    src = inspect.getsource(console_v2.company_overview)
    selects = src.upper().count("SELECT")
    assert selects <= 4, f"{selects} queries behind one screen — the v1 pattern is back"


def test_projects_are_lineages_not_runs():
    """Judging an app by its newest run made BOSS refuse to deploy something whose completed
    build was sitting in the same lineage. An app is a LINEAGE."""
    src = inspect.getsource(console_v2)
    assert "root_run_id" in src and "app_id" in src
    assert "_lineage_key" in src


def test_the_lineage_key_never_falls_back_to_a_title():
    """Two unrelated apps sharing a title prefix would merge — which is how one app's
    deployment once appeared on another's card."""
    # code_only strips comments AND docstrings. Without it this matched the docstring that
    # explains why titles are unusable — the seventh time a test in this repo has asserted on
    # prose. If you are reaching for inspect.getsource, reach for code_only instead.
    from engine.tests._source import code_only
    assert "title" not in code_only(console_v2._lineage_key), (
        "lineage grouping must not use the title")


@pytest.mark.parametrize("raw,expected", [
    ("PRD: Invoice Chaser", "Invoice Chaser"),
    ("Shorty — Product Requirements Document", "Shorty"),
    ("Product Requirements Document", ""),
    ("PRD", ""),
])
def test_a_document_type_is_never_a_product_name(raw, expected):
    """A card read "Product Requirements Document" — what the artifact IS, not what the product
    is called. Left through, every app that ships a PRD reads identically."""
    assert console_v2._clean_title(raw) == expected


def test_stage_is_derived_from_what_exists():
    """A status column can be stale while the work has moved on. The furthest-along artefact
    wins — a PR means review, a run means build."""
    done = [{"status": "completed", "pull_request_url": "http://pr/1"}]
    assert console_v2._stage_of(done) == ("SHIPPED", console_v2.DONE)

    building = [{"status": "running", "pull_request_url": None}]
    assert console_v2._stage_of(building) == ("BUILD", console_v2.RUNNING)

    stuck = [{"status": "failed", "pull_request_url": None}]
    assert console_v2._stage_of(stuck) == ("BUILD", console_v2.STUCK)

    nothing = [{"status": "queued", "pull_request_url": None}]
    assert console_v2._stage_of(nothing) == ("DISCOVERY", console_v2.IDLE)


def test_a_cancelled_run_does_not_hide_a_completed_sibling():
    """The Living Colors failure, as a test: one lineage, one cancelled run and one completed
    run. The app is buildable, and the newest run must not decide otherwise."""
    lineage = [
        {"status": "cancelled", "pull_request_url": None},   # newest
        {"status": "completed", "pull_request_url": None},
    ]
    stage, status = console_v2._stage_of(lineage)
    assert status != console_v2.STUCK
    assert status == console_v2.DONE


def test_the_status_vocabulary_is_the_artifact_s():
    for s in ("RUNNING", "GATED", "DONE", "STUCK", "IDLE"):
        assert hasattr(console_v2, s)
    assert console_v2.STAGES == ("DISCOVERY", "BUILD", "REVIEW", "SHIPPED")


def test_autonomy_reports_only_modes_that_exist():
    """The artifact shows a Supervised/autonomous switch. BOSS implements one mode, and a
    switch that changes nothing is worse than an honest label."""
    src = inspect.getsource(console_v2.company_overview)
    assert '"modes_available": ["Supervised"]' in src


@pytest.mark.parametrize("title", [
    "Captain: QUALITY IMPROVE PASS. Measure again",
    "quality improve pass",
    "Goldeneye profile check",
    "Follow-up: deferred review findings — Web app",
    "List the exact names of the tools you can call",
    "CEO command: run these with query_db",
])
def test_boss_housekeeping_is_not_shown_as_a_project(title):
    """The first render of the v2 Projects screen was dominated by BOSS talking to itself.
    A headline list full of internal chatter teaches the operator to ignore the list."""
    assert console_v2._is_internal(title)


@pytest.mark.parametrize("title", [
    "Living Colors",
    "Invoice-Chaser",
    "Build a link shortener with click analytics",
    "Shorty — Re-build",
])
def test_real_apps_are_not_filtered_out(title):
    """The filter must stay narrow: an over-broad rule silently hides real work, which is worse
    than the noise it removes."""
    assert not console_v2._is_internal(title)


def test_every_column_the_shape_reads_is_actually_selected():
    """`steps` reads state.turns, and `state` was missing from the SELECT — so it silently fell
    back to counting runs and looked plausible. A field read but never fetched is the quietest
    kind of wrong number, and this is the second time this week (deployment_target_id was the
    first)."""
    from engine.tests._source import code_only
    src = code_only(console_v2.company_overview)
    select = src[src.upper().index("SELECT"):src.upper().index("FROM CODE_TASK_RUNS")]
    for column in ("id", "title", "status", "spec", "state", "usage", "finalize_ctx",
                   "pull_request_url", "pull_request_number", "created_at", "updated_at"):
        assert column in select, f"the shape reads {column} but the query does not select it"
