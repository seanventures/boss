"""A CHANGE WITH FAILING CHECKS IS NEVER "READY TO MERGE" (contributed from an enterprise install,
2026-11-20).

A build whose checks still fail after its repair rounds opens its pull request anyway, and its
merge card says CHECKS FAILING (checks_failing.py). The Work row read nothing of it and said
"Ready to merge … built, tested and reviewed", with the Review station green. Now:

  - the Work attempt query reads the merge card's own `checks_failing` record (else the run's);
  - `_merge_links` carries it as {gates, security, words} (checks_failing.for_work), never a bare
    flag, and None when nothing is failing;
  - the Review station is failed while such a change waits on its merge, unless a later attempt
    is still fixing it (that is BOSS at work there).

No database: the SQL is checked for the column, the rest are pure functions.
"""

from __future__ import annotations

import json

from boss_engine.services import checks_failing
from boss_engine.services.project_experience import work

URL = "http://boss-local/local_recipes/recipes/pull/3"
CARD = checks_failing.collect(["tests_pass", "security_clean"])


def _merge(record):
    return {"id": "crun_1", "pull_request_url": URL, "pull_request_number": 3,
            "created_at": "2026-11-20T09:00:00Z", "checks_failing": record}


def test_the_attempt_query_reads_the_merge_cards_record_first():
    sql = work._attempt_sql()  # noqa: SLF001
    assert ("COALESCE(dd.recommendation->'checks_failing', c.finalize_ctx->'checks_failing') "
            "AS checks_failing") in sql


def test_merge_links_carry_the_failing_gates_in_words_security_first():
    merge = _merge(CARD)
    got = work._merge_links([merge], merge)["merge_pr"]  # noqa: SLF001
    assert got["number"] == 3
    assert got["checks_failing"]["gates"] == ["security_clean", "tests_pass"]
    assert got["checks_failing"]["security"] is True
    assert got["checks_failing"]["words"].startswith("“Security")
    # Stored JSON text reads the same way.
    merge = _merge(json.dumps(CARD))
    assert work._merge_links([merge], merge)["merge_pr"]["checks_failing"] == got["checks_failing"]  # noqa: SLF001


def test_nothing_failing_is_none():
    for record in (None, {}, {"gates": []}, "not json", 7):
        merge = _merge(record)
        assert work._merge_links([merge], merge)["merge_pr"]["checks_failing"] is None  # noqa: SLF001


def test_for_work_reads_the_card_record_and_a_bare_gate_list_alike():
    card = checks_failing.for_work({**CARD, "draft": True, "label": checks_failing.LABEL})
    bare = checks_failing.for_work({"gates": ["tests_pass", "security_clean"]})
    assert card == bare
    assert card["gates"] == checks_failing.gate_names(CARD)


STRIP = [{"key": k, "state": s} for k, s in
         (("plan", "skipped"), ("design", "skipped"), ("build", "done"), ("review", "done"),
          ("pr", "waiting"), ("ship", "skipped"))]


def test_review_station_is_failed_while_a_failing_change_waits():
    merge_pr = {"checks_failing": checks_failing.for_work(CARD), "fixing": None}
    out = work._checks_failing_review(STRIP, merge_pr)  # noqa: SLF001
    assert next(s for s in out if s["key"] == "review")["state"] == "failed"
    assert [s["state"] for s in out if s["key"] != "review"] == \
        [s["state"] for s in STRIP if s["key"] != "review"]


def test_review_station_is_left_alone_when_nothing_fails_or_a_fix_runs():
    assert work._checks_failing_review(STRIP, None) == STRIP  # noqa: SLF001
    assert work._checks_failing_review(STRIP, {"checks_failing": None}) == STRIP  # noqa: SLF001
    fixing = {"checks_failing": checks_failing.for_work(CARD), "fixing": {"attempt": "crun_2"}}
    assert work._checks_failing_review(STRIP, fixing) == STRIP  # noqa: SLF001
