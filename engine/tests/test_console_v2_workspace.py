"""The v2 project workspace: one app, one call.

The v1 equivalent of this screen was assembled from the whole 82-feed page render, so opening
one app cost the same as opening everything. These pin the properties that make the one-call
version trustworthy rather than merely fast.
"""

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services.console_v2 import project_workspace

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


class FakeConn:
    """Routes each query to its own rows by the table it names.

    An earlier version returned the SAME rows for every query, so when the workspace started
    reading llm_calls it handed code-task rows to a query that wanted call rows. A fake that
    answers everything identically will agree with any code, including wrong code.
    """

    def __init__(self, rows, calls=()):
        self.rows = rows
        self.calls = list(calls)
        self.statements = []

    def execute(self, statement, params=None):
        raw = str(statement)
        self.statements.append((" ".join(raw.split()), dict(params or {})))
        rows = self.calls if "llm_calls" in raw.lower() else self.rows
        captured = rows

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in captured]

        return _R()


def run(rid, *, title="Shorty — a URL shortener", status="completed", pr=None, age_min=0,
        cost=0, turns=0, err=None, phase="done", **ctx):
    return {
        "id": rid,
        "title": title,
        "status": status,
        "phase": phase,
        "spec": {"instructions": "Build it", "owner": "paramg", "repo": "boss-sandbox"},
        "state": {"turns": turns},
        "usage": {"cost_usd_micros": cost},
        "finalize_ctx": ctx or None,
        "pull_request_url": f"https://github.com/paramg/boss-sandbox/pull/{pr}" if pr else None,
        "pull_request_number": pr,
        "error": err,
        "created_at": NOW - timedelta(minutes=age_min),
        "updated_at": NOW - timedelta(minutes=age_min),
    }


# --------------------------------------------------------------------------- #
# One call
# --------------------------------------------------------------------------- #
def test_the_screen_costs_a_bounded_number_of_queries_and_never_one_per_run():
    """82 feeds per render is what made v1 unusable, and 82 route re-renders came from one
    lookup called once per card.

    The rule is one CALL per screen, not one statement: a second aggregate is fine and is what
    the receipts feed does. What must never appear is a query per item — that is the shape that
    scales with the data and takes the page down when the project gets busy.
    """
    lineage = [run(f"crun_{i}", age_min=i, app_id="app_1") for i in range(12)]
    conn = FakeConn(lineage)
    project_workspace(conn, "proj", "app_1")
    assert len(conn.statements) <= 3, [s for s, _ in conn.statements]

    conn_small = FakeConn([run("crun_1", app_id="app_1")])
    project_workspace(conn_small, "proj", "app_1")
    # Twelve runs must cost the same as one. If it does not, there is a per-item query.
    assert len(conn.statements) == len(conn_small.statements)


def test_the_step_query_asks_for_every_run_at_once():
    """= ANY(:ids), not a loop."""
    conn = FakeConn([run("crun_1", app_id="app_1"), run("crun_2", app_id="app_1")])
    project_workspace(conn, "proj", "app_1")
    call_sql = [s for s, _ in conn.statements if "llm_calls" in s.lower()]
    assert call_sql, "no llm_calls query at all"
    assert "= ANY(:ids)" in call_sql[0]


# --------------------------------------------------------------------------- #
# Missing is not empty
# --------------------------------------------------------------------------- #
def test_an_unknown_app_is_reported_as_not_found():
    """An id that matches nothing and an app with no runs are different facts. Rendering them
    alike sends the operator looking for work that was never there."""
    out = project_workspace(FakeConn([run("crun_1", app_id="app_1")]), "proj", "app_missing")
    assert out["found"] is False
    assert "runs" not in out


# --------------------------------------------------------------------------- #
# The lineage is the unit
# --------------------------------------------------------------------------- #
def test_every_run_in_the_lineage_is_shown():
    conn = FakeConn([
        run("crun_3", age_min=1, app_id="app_1", attempt=3),
        run("crun_2", age_min=2, app_id="app_1", attempt=2),
        run("crun_1", age_min=3, app_id="app_1", attempt=1),
        run("crun_other", age_min=1, app_id="app_2"),
    ])
    out = project_workspace(conn, "proj", "app_1")
    assert [r["id"] for r in out["runs"]] == ["crun_3", "crun_2", "crun_1"]
    assert [r["attempt"] for r in out["runs"]] == [3, 2, 1]


def test_newest_first():
    conn = FakeConn([
        run("crun_old", age_min=99, app_id="app_1"),
        run("crun_new", age_min=1, app_id="app_1"),
    ])
    out = project_workspace(conn, "proj", "app_1")
    assert out["runs"][0]["id"] == "crun_new"
    assert out["latest_run_id"] == "crun_new"


def test_spend_is_the_lineage_total_not_the_newest_run():
    """Every attempt cost money. Showing only the last one understates what the app cost by
    however many retries it took."""
    conn = FakeConn([
        run("crun_2", age_min=1, app_id="app_1", cost=1_500_000),
        run("crun_1", age_min=2, app_id="app_1", cost=2_250_000),
    ])
    assert project_workspace(conn, "proj", "app_1")["spend_usd"] == 3.75


# --------------------------------------------------------------------------- #
# Deployability, answered from the lineage
# --------------------------------------------------------------------------- #
def test_a_completed_run_in_the_lineage_makes_the_app_deployable():
    """Judging by the NEWEST run made BOSS refuse to deploy something whose completed build was
    sitting two rows down in the same lineage. That is the bug this whole shape exists for."""
    conn = FakeConn([
        run("crun_2", age_min=1, app_id="app_1", status="cancelled"),
        run("crun_1", age_min=2, app_id="app_1", status="completed", pr=111),
    ])
    out = project_workspace(conn, "proj", "app_1")
    assert out["deployable"] is True
    assert out["deployable_reason"] == ""


def test_an_app_with_nothing_reviewed_says_why_it_cannot_ship():
    """A disabled button with no reason is what sends people to the API to find out."""
    conn = FakeConn([run("crun_1", app_id="app_1", status="running")])
    out = project_workspace(conn, "proj", "app_1")
    assert out["deployable"] is False
    assert "PR" in out["deployable_reason"]


def test_a_completed_run_without_a_pr_is_not_shippable():
    """Nothing was reviewed, so there is nothing to merge."""
    conn = FakeConn([run("crun_1", app_id="app_1", status="completed", pr=None)])
    assert project_workspace(conn, "proj", "app_1")["deployable"] is False


# --------------------------------------------------------------------------- #
# A failure must be readable on the card
# --------------------------------------------------------------------------- #
def test_a_failed_run_carries_its_reason():
    """"failed" with no text is a dead end — the operator's only move is to go and read logs."""
    conn = FakeConn([run("crun_1", app_id="app_1", status="failed",
                         err="health gate rejected: /healthz returned 502")])
    out = project_workspace(conn, "proj", "app_1")
    assert "502" in out["runs"][0]["error"]


def test_a_successful_run_has_no_error_text():
    conn = FakeConn([run("crun_1", app_id="app_1", status="completed", pr=1)])
    assert project_workspace(conn, "proj", "app_1")["runs"][0]["error"] is None


# --------------------------------------------------------------------------- #
# Presentation the screen depends on
# --------------------------------------------------------------------------- #
def test_the_title_is_a_product_name():
    conn = FakeConn([run("crun_1", app_id="app_1", title="Shorty — Re-build PRD")])
    assert project_workspace(conn, "proj", "app_1")["title"] == "Shorty"


def test_phases_report_how_far_the_app_has_got():
    conn = FakeConn([run("crun_1", app_id="app_1", status="completed", pr=42)])
    out = project_workspace(conn, "proj", "app_1")
    reached = [p["id"] for p in out["phases"] if p["reached"]]
    assert reached == ["DISCOVERY", "BUILD", "REVIEW", "SHIPPED"]


def test_an_early_app_has_not_reached_the_later_phases():
    conn = FakeConn([run("crun_1", app_id="app_1", status="running")])
    out = project_workspace(conn, "proj", "app_1")
    assert [p["id"] for p in out["phases"] if p["reached"]] == ["DISCOVERY", "BUILD"]


def test_the_pr_number_is_carried_so_the_link_can_be_built():
    """A card once linked to github.com/paramg/boss-sandbox/pull/111 for a PR that was not
    there. The number and the URL travel together, from the run that owns them."""
    conn = FakeConn([run("crun_1", app_id="app_1", status="completed", pr=111)])
    r = project_workspace(conn, "proj", "app_1")["runs"][0]
    assert r["pull_request_number"] == 111
    assert r["pull_request_url"].endswith("/pull/111")


# --------------------------------------------------------------------------- #
# Run detail: the steps, who ran them, and what each cost
#
# A card that says "attempt 3 — failed" and nothing else is a dead end: the only move left is
# to go and read logs. BOSS records the stage, model, latency and cost of every call.
# --------------------------------------------------------------------------- #
def call(run_id, stage, *, model="claude-opus-5", latency=1200, micros=250_000,
         tokens=4000, turns=5, retries=0, ok=True, engine="claude_code", error=None):
    """One row of llm_calls, with the REAL column names.

    These are per-call rows now, not a per-stage aggregate. The aggregate discarded turns,
    retries and the error text -- and on a failed attempt the error is the only thing worth
    opening the run for.
    """
    return {"run_id": run_id, "stage": stage, "model": model, "engine": engine,
            "total_tokens": tokens, "cost_usd_micros": micros, "latency_ms": latency,
            "turns": turns, "retries": retries, "ok": ok, "error": error}


def test_a_run_lists_the_steps_it_actually_ran():
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "spec"), call("crun_1", "implement"), call("crun_1", "review_code")],
    ), "proj", "app_1")
    steps = out["runs"][0]["steps"]
    assert [s["stage"] for s in steps] == ["spec", "implement", "review_code"]


def test_each_step_carries_its_latency():
    """What the operator asked for, and what tells a slow model apart from a slow pipeline."""
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "implement", latency=8_400)],
    ), "proj", "app_1")
    assert out["runs"][0]["steps"][0]["latency_ms"] == 8_400
    assert out["runs"][0]["latency_ms"] == 8_400


def test_the_run_latency_is_the_sum_of_its_steps():
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "spec", latency=1_000), call("crun_1", "implement", latency=4_000)],
    ), "proj", "app_1")
    assert out["runs"][0]["latency_ms"] == 5_000


def test_each_step_says_which_model_did_the_work():
    """"Which agent made the change" — the model and engine that actually ran it."""
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "implement", model="claude-sonnet-5", engine="claude_code")],
    ), "proj", "app_1")
    step = out["runs"][0]["steps"][0]
    assert step["model"] == "claude-sonnet-5"
    assert step["engine"] == "claude_code"


def test_each_step_carries_its_own_cost():
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "implement", micros=1_750_000)],
    ), "proj", "app_1")
    assert out["runs"][0]["steps"][0]["spend_usd"] == 1.75


def test_a_failed_step_is_marked_not_hidden():
    """A run that failed at one step and succeeded at nine others is a different story from one
    that failed everywhere."""
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "spec", ok=True), call("crun_1", "implement", ok=False)],
    ), "proj", "app_1")
    assert [s["ok"] for s in out["runs"][0]["steps"]] == [True, False]


def test_steps_belong_to_their_own_run():
    """Attributing one attempt's steps to another would misreport what each cost — the same
    mistake as the time-window join that gave two agents identical spend."""
    out = project_workspace(FakeConn(
        [run("crun_2", age_min=1, app_id="app_1"), run("crun_1", age_min=2, app_id="app_1")],
        calls=[call("crun_1", "spec"), call("crun_2", "implement")],
    ), "proj", "app_1")
    by_id = {r["id"]: r for r in out["runs"]}
    assert [s["stage"] for s in by_id["crun_1"]["steps"]] == ["spec"]
    assert [s["stage"] for s in by_id["crun_2"]["steps"]] == ["implement"]


def test_a_run_with_no_recorded_calls_has_no_invented_steps():
    out = project_workspace(FakeConn([run("crun_1", app_id="app_1")]), "proj", "app_1")
    assert out["runs"][0]["steps"] == []
    assert out["runs"][0]["latency_ms"] == 0


# --------------------------------------------------------------------------- #
# The governed pipeline's own record of an attempt
#
# v1 showed the quality checks, who ran them, and what the reviewers wrote. v2 was SELECTing
# state and finalize_ctx and discarding both, so the screen said "attempt 1 - completed" and
# stopped. The operator asked for the v1 content back.
# --------------------------------------------------------------------------- #
def state_run(run_id="crun_1", **state):
    r = run(run_id, app_id="app_1")
    r["state"] = {**(r.get("state") or {}), **state}
    return r


def gates_of(out):
    return {g["label"]: g for g in out["runs"][0]["gates"]}


def test_a_check_that_never_ran_is_not_reported_as_passing_or_failing():
    """Three states, never two. "We did not check" is not "it is fine", and it is not a
    failure either -- colouring it red claims evidence nobody gathered."""
    out = project_workspace(FakeConn([state_run()]), "proj", "app_1")
    sec = gates_of(out)["Security clean"]
    assert sec["skipped"] is True
    assert sec["ok"] is None


def test_a_check_that_ran_and_failed_is_reported_as_failing():
    out = project_workspace(FakeConn([state_run(
        security_verdict="vulnerable", consistency_verdict="drift")]), "proj", "app_1")
    g = gates_of(out)
    assert (g["Security clean"]["skipped"], g["Security clean"]["ok"]) == (False, False)
    assert (g["Consistency"]["skipped"], g["Consistency"]["ok"]) == (False, False)
    assert g["Consistency"]["note"] == "drift"


def test_a_check_that_ran_and_passed_carries_its_evidence():
    out = project_workspace(FakeConn([state_run(
        tests_passed=True, test_exit_code=0,
        adversarial_verdict="survived", security_verdict="clean",
        regressions_ok=True, mutation_caught=True,
        consistency_verdict="consistent")]), "proj", "app_1")
    g = gates_of(out)
    assert g["Code built / tests pass"]["ok"] is True
    assert g["Code built / tests pass"]["note"] == "exit 0"
    assert g["No regressions"]["note"] == "mutation caught"
    assert all(row["ok"] for row in out["runs"][0]["gates"][:5])


def test_a_zero_exit_code_is_not_mistaken_for_no_exit_code():
    """`if exit_code:` would drop the successful case -- 0 is the passing value."""
    out = project_workspace(FakeConn([state_run(tests_passed=True, test_exit_code=0)]),
                            "proj", "app_1")
    assert gates_of(out)["Code built / tests pass"]["skipped"] is False


def test_the_same_model_reviewing_its_own_work_is_stated():
    """The single fact that most changes what a green review is worth."""
    same = project_workspace(FakeConn([state_run(
        build_engine="claude_code", review_engine="claude_code")]), "proj", "app_1")
    assert same["runs"][0]["provenance"]["cross_model"] is False

    cross = project_workspace(FakeConn([state_run(
        build_engine="claude_code", review_engine="codex")]), "proj", "app_1")
    assert cross["runs"][0]["provenance"]["cross_model"] is True


def test_a_missing_reviewer_does_not_read_as_a_cross_model_review():
    """review_engine defaults to the builder, so absence must not look like independence."""
    out = project_workspace(FakeConn([state_run(build_engine="claude_code")]), "proj", "app_1")
    assert out["runs"][0]["provenance"]["cross_model"] is False


def test_the_reviewers_own_words_are_returned_in_order_and_empties_omitted():
    """A panel titled "Security review" with nothing in it is worse than no panel."""
    out = project_workspace(FakeConn([state_run(
        review_summary="Found two issues.", security_summary="   ",
        impl_summary="Built the thing.")]), "proj", "app_1")
    ev = out["runs"][0]["evidence"]
    assert [e["key"] for e in ev] == ["review_summary", "impl_summary"]
    assert ev[0]["title"] == "Adversarial reviewer findings"


def test_a_long_review_is_clipped_on_a_boundary_and_says_so():
    out = project_workspace(FakeConn([state_run(review_summary="word " * 3000)]),
                            "proj", "app_1")
    ev = out["runs"][0]["evidence"][0]
    assert ev["truncated"] is True
    assert ev["text"].count("**") % 2 == 0


def test_a_first_pass_build_that_shipped_with_failing_checks_says_so():
    """Otherwise a green header sits above a red row and the reader has to guess which won."""
    r = run("crun_1", app_id="app_1")
    r["finalize_ctx"] = {**(r.get("finalize_ctx") or {}), "advisory_failing_gates": ["consistency"]}
    out = project_workspace(FakeConn([r]), "proj", "app_1")
    assert out["runs"][0]["advisory"] is True


def test_a_normal_build_is_not_labelled_advisory():
    out = project_workspace(FakeConn([run("crun_1", app_id="app_1")]), "proj", "app_1")
    assert out["runs"][0]["advisory"] is False


def test_a_failed_call_carries_the_error_text():
    """The whole reason to open a failed attempt."""
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "consistency_check", ok=False,
                    error="claude code agent timed out after 420s")],
    ), "proj", "app_1")
    step = out["runs"][0]["steps"][0]
    assert step["ok"] is False
    assert "timed out after 420s" in step["error"]


def test_turns_and_retries_survive_to_the_screen():
    """Both were being discarded by the per-stage aggregate."""
    out = project_workspace(FakeConn(
        [run("crun_1", app_id="app_1")],
        calls=[call("crun_1", "solo", turns=46, retries=2)],
    ), "proj", "app_1")
    assert (out["runs"][0]["steps"][0]["turns"],
            out["runs"][0]["steps"][0]["retries"]) == (46, 2)
