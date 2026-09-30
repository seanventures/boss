"""The v2 receipts feed — what BOSS actually did, with the evidence attached.

Two defects shaped this, both real:

  "Verified today: 100" was the console's own page size. It fetched the hundred most recent
  receipts and counted them, so it read 100 for a project with a million and for a project with
  exactly a hundred.

  "The API restarted while this deployment was running" leaves a receipt at in_progress
  forever, because nothing moves it afterwards. Counting those as done reports a clean day for
  a day that lost work.
"""

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services.console_v2 import receipt_feed

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class FakeConn:
    """Answers the page query with rows and the aggregate query with counts."""

    def __init__(self, rows, totals=None):
        self.rows = rows
        self.totals = totals or {}
        self.statements = []

    def execute(self, statement, params=None):
        raw = str(statement)
        self.statements.append((raw, dict(params or {})))
        conn = self
        is_count = "count(*)" in raw.lower()

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in conn.rows]

            def first(self):
                return dict(conn.totals) if is_count else (dict(conn.rows[0]) if conn.rows else None)

        return _R()


def receipt(rid, *, kind="deployment.apply", status="succeeded", scope=None, ref="dep_1",
            err=None, age_min=0, boundary="internal"):
    return {
        "id": rid,
        "effect_kind": kind,
        "effect_scope": scope if scope is not None else {"app_name": "Shorty"},
        "status": status,
        "provider_ref": ref,
        "result": {},
        "error": err,
        "data_boundary": boundary,
        "created_at": NOW - timedelta(minutes=age_min),
        "updated_at": NOW - timedelta(minutes=age_min),
    }


# --------------------------------------------------------------------------- #
# A count must be a count
# --------------------------------------------------------------------------- #
def test_the_window_total_is_not_the_page_size():
    """"Verified today: 100" was the page size wearing a total's name. The aggregate is
    computed over the whole window, separately from the rows shown."""
    out = receipt_feed(FakeConn([receipt(f"r{i}") for i in range(60)],
                                {"total": 12_402, "unfinished": 3, "failed": 11}), "p")
    assert out["window_total"] == 12_402
    assert out["shown"] == 60
    assert out["window_total"] != out["shown"]


def test_the_totals_come_from_a_separate_aggregate_query():
    conn = FakeConn([receipt("r1")], {"total": 5, "unfinished": 0, "failed": 0})
    receipt_feed(conn, "p")
    assert len(conn.statements) == 2
    assert "count(*)" in conn.statements[1][0].lower()
    assert "limit" not in conn.statements[1][0].lower()


def test_the_page_is_named_so_it_cannot_be_read_as_a_total():
    out = receipt_feed(FakeConn([receipt("r1")], {"total": 900}), "p")
    assert "shown" in out and "window_total" in out
    assert "total" not in out          # no unqualified 'total' to misread


# --------------------------------------------------------------------------- #
# An unfinished effect is not a success
# --------------------------------------------------------------------------- #
def test_an_in_progress_receipt_is_marked_unfinished():
    """The API restarting mid-effect leaves this behind, and nothing moves it afterwards."""
    out = receipt_feed(FakeConn([receipt("r1", status="in_progress")],
                                {"total": 1, "unfinished": 1, "failed": 0}), "p")
    assert out["items"][0]["unfinished"] is True
    assert out["window_unfinished"] == 1


def test_a_completed_receipt_is_not_marked_unfinished():
    out = receipt_feed(FakeConn([receipt("r1", status="succeeded")],
                                {"total": 1, "unfinished": 0, "failed": 0}), "p")
    assert out["items"][0]["unfinished"] is False


def test_unfinished_and_failed_are_counted_separately():
    """They mean different things: one lost the work, the other refused it."""
    out = receipt_feed(FakeConn([receipt("r1")],
                                {"total": 100, "unfinished": 4, "failed": 7}), "p")
    assert out["window_unfinished"] == 4
    assert out["window_failed"] == 7


# --------------------------------------------------------------------------- #
# Evidence, not claims
# --------------------------------------------------------------------------- #
def test_the_provider_reference_is_carried():
    """This is what makes a receipt evidence rather than an assertion: it can be checked
    against the system that actually did the work."""
    out = receipt_feed(FakeConn([receipt("r1", ref="ghpr_88")], {"total": 1}), "p")
    assert out["items"][0]["provider_ref"] == "ghpr_88"


def test_a_failure_carries_its_message():
    out = receipt_feed(FakeConn([receipt("r1", status="failed",
                                         err={"message": "host unreachable"})], {"total": 1}), "p")
    assert out["items"][0]["error"] == "host unreachable"


def test_a_success_has_no_invented_error():
    out = receipt_feed(FakeConn([receipt("r1")], {"total": 1}), "p")
    assert out["items"][0]["error"] is None


def test_the_scope_says_what_the_effect_acted_on():
    out = receipt_feed(FakeConn([receipt("r1", scope={"app_name": "Living Colors"})],
                                {"total": 1}), "p")
    assert out["items"][0]["scope"] == "Living Colors"


def test_an_unfamiliar_scope_still_says_something():
    """Knowing an effect had a scope we cannot summarise beats being shown nothing."""
    out = receipt_feed(FakeConn([receipt("r1", scope={"widget_id": "w1", "zone": "b"})],
                                {"total": 1}), "p")
    assert out["items"][0]["scope"]


def test_an_empty_scope_is_empty_not_guessed():
    out = receipt_feed(FakeConn([receipt("r1", scope={})], {"total": 1}), "p")
    assert out["items"][0]["scope"] == ""


def test_the_window_is_reported_with_the_numbers():
    """A count with no window attached cannot be compared with anything."""
    out = receipt_feed(FakeConn([], {"total": 0}), "p", hours=6)
    assert out["window_hours"] == 6
    assert conn_hours(FakeConn([], {"total": 0}), 6) == 6


def conn_hours(conn, hours):
    receipt_feed(conn, "p", hours=hours)
    return conn.statements[0][1]["h"]


def test_an_empty_window_reports_zero_rather_than_nothing():
    out = receipt_feed(FakeConn([], {"total": 0, "unfinished": 0, "failed": 0}), "p")
    assert out["items"] == []
    assert out["window_total"] == 0
    assert out["shown"] == 0


# --------------------------------------------------------------------------- #
# Making the ledger readable
#
# The operator's report: "I'm unable to correlate any work from the Receipts." Accurate. Over
# 24h this project recorded 1,901 receipts, and of the 60 most recent, 46 were BOSS's own
# heartbeat. The four that recorded real work were buried under ticks, and the scope column
# showed the NAMES of the scope fields rather than their values.
# --------------------------------------------------------------------------- #
def only(out, rid):
    return next(i for i in out["items"] if i["id"] == rid)


def test_the_scope_shows_what_was_acted_on_not_the_names_of_the_fields():
    """It read "project_id, window" and "action, minute, steward_id" -- field labels where the
    values should be."""
    out = receipt_feed(FakeConn([receipt("r1", kind="process_manager.tick", scope={
        "project_id": "boss-dogfood", "action": "advance", "minute": 42})]), "p")
    scope = only(out, "r1")["scope"]
    assert "advance" in scope and "42" in scope
    # project_id is true of every row on the screen, so it says nothing.
    assert "boss-dogfood" not in scope


def test_a_named_subject_wins_over_key_value_pairs():
    out = receipt_feed(FakeConn([receipt("r1", scope={"app_name": "Invoice-Chaser",
                                                     "attempt": 2})]), "p")
    assert only(out, "r1")["scope"] == "Invoice-Chaser"


def test_boss_keeping_itself_alive_is_marked_as_routine():
    """1,901 receipts in a day, and the work is four of them. Newest-first with no distinction
    buries the work under the heartbeat."""
    out = receipt_feed(FakeConn([
        receipt("tick", kind="process_manager.tick"),
        receipt("watch", kind="metatron.watch"),
        receipt("sre", kind="ai_sre.overwatch"),
        receipt("work", kind="code_task.spend_cap_failover"),
    ]), "p")
    assert only(out, "tick")["routine"] is True
    assert only(out, "watch")["routine"] is True
    assert only(out, "sre")["routine"] is True
    assert only(out, "work")["routine"] is False
    assert out["shown_routine"] == 3


def test_each_effect_carries_the_family_it_belongs_to():
    out = receipt_feed(FakeConn([
        receipt("a", kind="code_task.opened_pr"),
        receipt("b", kind="deployment.apply"),
        receipt("c", kind="budget.llm_spend.settle"),
        receipt("d", kind="agent_process.review_cto"),
        receipt("e", kind="meeting.convene"),
    ]), "p")
    fam = {i["id"]: i["family"] for i in out["items"]}
    assert fam == {"a": "CODE", "b": "DEPLOY", "c": "SPEND",
                   "d": "PROCESS", "e": "PANEL"}


def test_an_effect_says_what_it_did_in_words_as_well_as_in_its_identifier():
    """`code_task.spend_cap_failover` is precise and unreadable. Both halves matter."""
    out = receipt_feed(FakeConn([receipt("r1", kind="code_task.spend_cap_failover",
                                         scope={"code_task_run_id": "crun_abc"})]), "p")
    item = only(out, "r1")
    assert "spend cap failover" in item["summary"]
    assert "crun_abc" in item["summary"]
    assert item["effect"] == "code_task.spend_cap_failover"   # the identifier survives


def test_a_failed_or_unfinished_effect_says_so_in_its_sentence():
    out = receipt_feed(FakeConn([
        receipt("f", kind="deployment.apply", status="failed"),
        receipt("p", kind="deployment.apply", status="in_progress"),
    ]), "p")
    assert "failed" in only(out, "f")["summary"]
    assert "still running" in only(out, "p")["summary"]


def test_a_receipt_that_names_a_run_carries_the_link_back_to_it():
    """The correlation the operator was missing: this effect belongs to that piece of work."""
    out = receipt_feed(FakeConn([receipt("r1", scope={"code_task_run_id": "crun_abc"})]), "p")
    assert only(out, "r1")["run_id"] == "crun_abc"
    out2 = receipt_feed(FakeConn([receipt("r2", scope={"app_name": "Shorty"})]), "p")
    assert only(out2, "r2")["run_id"] is None
