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


def test_the_lineage_key_never_merges_two_different_apps():
    """Two unrelated apps must never share a card — that is how one app's deployment once
    appeared on another's.

    This used to forbid the lineage key from touching the title at all, which was a blunt way
    to state it. The refined rule is that a RAW title may not be used but an extracted product
    name may: on the live screen "Shorty" and "Living Colors" each appeared TWICE as separate
    apps because their raw titles differed only by a retry suffix and an artifact type. So the
    property is stated directly instead: same app merges, different apps do not.
    """
    def run(run_id, title, **ctx):
        return {"id": run_id, "title": title, "finalize_ctx": ctx or None}

    key = console_v2._lineage_key
    # A shared PREFIX is not a shared app.
    assert key(run("c1", "Shorty — a URL shortener")) != key(run("c2", "Shorty Pro — the paid one"))
    assert key(run("c1", "Coaching HTTP service (F1+F2)")) != key(run("c2", "Coaching Moments engine (F1)"))
    # An explicit lineage always wins over any name similarity.
    assert key(run("c1", "Shorty", app_id="app_1")) != key(run("c2", "Shorty", app_id="app_2"))
    # And a title too generic to identify anything never merges.
    assert key(run("c1", "PRD")) != key(run("c2", "Spec"))


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
    "reference host profile check",
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


# --------------------------------------------------------------------------- #
# company_overview: which lineages are apps, and what each is called
#
# This function had no tests. Removing the filter that keeps housekeeping runs from becoming
# their own apps changed nothing in the suite, which means every follow-up task could have
# appeared as a separate product on the Projects screen unnoticed.
# --------------------------------------------------------------------------- #
class OverviewConn:
    """Routes by query, because company_overview issues three different ones.

    A fake that returns the same rows to every query agrees with any implementation -- it would
    hand code-task rows to the pending-decisions count and call it a pass.
    """

    def __init__(self, runs):
        self.runs = list(runs)

    def execute(self, statement, params=None):
        raw = str(statement).lower()
        outer = self

        class R:
            def mappings(self):
                return self

            def all(self):
                return outer.runs if "from code_task_runs" in raw else []

            def scalar(self):
                return 0

        return R()


def orun(run_id, title, *, app_id=None, status="completed", micros=0, created=None):
    from datetime import datetime, timezone
    return {
        "id": run_id, "title": title, "status": status, "phase": "done",
        "spec": {"owner": "example-user", "repo": "boss-sandbox", "instructions": ""},
        "state": {}, "usage": {"cost_usd_micros": micros},
        "finalize_ctx": {"app_id": app_id} if app_id else {},
        "pull_request_url": None, "pull_request_number": None,
        "created_at": created or datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
        "updated_at": created or datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    }


def titles(out):
    return [p["title"] for p in out["projects"]]


def test_a_lineage_of_nothing_but_housekeeping_is_not_an_app():
    """Otherwise every follow-up task becomes its own product on the Projects screen."""
    out = console_v2.company_overview(OverviewConn([
        orun("crun_1", "Follow-up: deferred review findings", app_id="app_hk"),
        orun("crun_2", "Shorty", app_id="app_real"),
    ]), "proj")
    assert titles(out) == ["Shorty"]


def test_housekeeping_does_not_rename_the_app_it_ran_against():
    """The same defect the workspace had: the newest title won, and the newest was a chore."""
    from datetime import datetime, timezone
    out = console_v2.company_overview(OverviewConn([
        orun("crun_new", "Follow-up: deferred review findings", app_id="app_1",
             created=datetime(2026, 8, 26, tzinfo=timezone.utc)),
        orun("crun_old", "Invoice-Chaser", app_id="app_1",
             created=datetime(2026, 8, 20, tzinfo=timezone.utc)),
    ]), "proj")
    assert titles(out) == ["Invoice-Chaser"]


def test_housekeeping_spend_counts_toward_the_app_it_ran_against():
    """It was excluded from the group entirely, so its cost vanished from the Projects screen
    while the app's own workspace page counted it -- two screens, two numbers."""
    out = console_v2.company_overview(OverviewConn([
        orun("crun_new", "Follow-up: deferred review findings", app_id="app_1", micros=2_000_000),
        orun("crun_old", "Invoice-Chaser", app_id="app_1", micros=1_000_000),
    ]), "proj")
    assert out["projects"][0]["spend_usd"] == 3.0


def test_an_app_named_only_by_a_chore_uses_that_name_rather_than_untitled():
    """Seen live: the Projects screen said "Untitled app" while its own workspace page showed
    the real name. Both screens have to land on the same one."""
    out = console_v2.company_overview(OverviewConn([
        orun("crun_1", "Product Requirements Document", app_id="app_1"),   # cleans to ""
        orun("crun_2", "Follow-up: deferred review findings", app_id="app_1"),
    ]), "proj")
    assert titles(out) != ["Untitled app"]
    assert "Follow-up" in titles(out)[0]
