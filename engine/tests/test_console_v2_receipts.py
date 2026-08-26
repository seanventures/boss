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
