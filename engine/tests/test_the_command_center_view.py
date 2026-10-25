"""The Command Center view's rules, without a database (command-center spec §4.5, §4.11, §6.2).

The reads are pinned against Postgres in apps/api/tests/test_the_command_center_view.py; what
is pinned here is what the view SAYS about what it read: the one state sentence (first matching
rule wins), each Systems dot's tone from its own source, and an ETag that ignores the clock.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services import command_center_view as ccv

NOW = datetime(2026, 10, 4, 15, 40, tzinfo=timezone.utc)

GREEN_BUDGET = {"budget_state": "green", "period": "month", "cap_usd_micros": 200_000_000,
                "actual_usd_micros": 84_000_000, "utilization_ratio": 0.42,
                "resets_at_label": "00:00 UTC on 1 November"}


def counts(**over):
    return {"needs_you": 0, "stopped": 0, "running": 0, "setup": 0, **over}


def status(**kw):
    args = {"counts": counts(), "attention": [], "budget": GREEN_BUDGET, "host": {"show": False},
            "runs_24h": 3, "ever": True, "last_work_at": (NOW - timedelta(hours=3)).isoformat(),
            "now": NOW}
    args.update(kw)
    return ccv.status(**args)


RED = {**GREEN_BUDGET, "budget_state": "red", "utilization_ratio": 1.06, "actual_usd_micros": 212_000_000}
ORANGE = {**GREEN_BUDGET, "budget_state": "orange", "utilization_ratio": 0.874,
          "actual_usd_micros": 174_800_000}
CRITICAL = {"show": True, "severity": "critical", "headline": "This computer is out of memory"}
WARNING = {"show": True, "severity": "warning", "headline": "Memory is running low"}
REFUSED = [{"kind": "budget_refused", "id": "build:r1:budget"}]

#: One fixture per §4.5 row (2-11), each also carrying every LOWER row's trigger, so the first
#: match must win.
ROWS = [
    ("budget_stopped", "Budget stopped work", "red",
     dict(budget=RED, attention=REFUSED, host=CRITICAL, counts=counts(stopped=1, needs_you=2, running=1))),
    ("host_critical", "Host struggling", "red",
     dict(budget=RED, host=CRITICAL, counts=counts(stopped=1, needs_you=2, running=1))),
    ("stopped", "1 stopped", "red", dict(host=WARNING, counts=counts(stopped=1, needs_you=2, running=1))),
    ("needs_you", "2 need you", "amber", dict(host=WARNING, budget=ORANGE, counts=counts(needs_you=2, running=1))),
    ("host_slow", "Host is slow", "amber", dict(host=WARNING, budget=ORANGE, counts=counts(running=1))),
    ("budget_high", "Budget at 87%", "amber", dict(budget=ORANGE, counts=counts(running=1, setup=1))),
    ("running", "3 running", "accent", dict(counts=counts(running=3, setup=1))),
    ("setup", "Finish setup", "amber", dict(counts=counts(setup=1), runs_24h=0, ever=False)),
    ("first_build", "Ready for first build", "accent", dict(runs_24h=0, ever=False)),
    ("quiet", "All quiet", "green", dict()),
]


@pytest.mark.parametrize("key,text,tone,kw", ROWS, ids=[r[0] for r in ROWS])
def test_status_sentence_priority(key, text, tone, kw):
    got = status(**kw)
    assert (got["key"], got["text"], got["tone"]) == (key, text, tone)
    assert len(got["tip"].split()) <= 14


def test_the_sentence_says_one_or_many():
    assert status(counts=counts(needs_you=1))["text"] == "1 needs you"
    assert status(counts=counts(running=1))["text"] == "1 running"
    assert status()["tip"] == "Nothing running or waiting. Last work 3h ago."
    # Red budget with nothing refused is not "stopped work": it is a high budget.
    assert status(budget=RED)["key"] == "budget_high"


def test_budget_at_n_percent_is_the_only_percentage_and_it_is_money():
    tip = status(budget=ORANGE)["tip"]
    assert tip == "$174.80 of $200 this month. Resets 00:00 UTC on 1 November."


# ── Systems ─────────────────────────────────────────────────────────────────────────────────


def model(state="working", label="Claude Code", tip="Working"):
    return {"label": label, "pill": {"state": state, "tone": "green", "label": state, "tip": tip}}


def repo(state="working", name="acme/shop", tip="Connected"):
    return {"full_name": name, "default": True, "binding_id": "b1", "stale": False,
            "pill": {"state": state, "tone": "green", "label": state, "tip": tip}}


def basics(models=None, repos=None, expires=None):
    return {"models": [model()] if models is None else models,
            "repository": {"repos": [repo()] if repos is None else repos,
                           "connection": {"expires_at": expires} if expires else None}}


PROBE = {"oldest_queued": None, "open_jobs": 0, "deadletters": 0, "targets": 1, "calls_hour": 4,
         "failed_hour": 0, "calls_today": 212, "previews": [{"status": "running", "created_at": NOW}],
         "deploy": {"status": "deployed", "created_at": NOW - timedelta(hours=2),
                    "finished_at": NOW - timedelta(hours=2)}}


def dots(**kw):
    args = {"probe": PROBE, "basics": basics(), "budget": GREEN_BUDGET, "host": {"show": False},
            "now": NOW}
    args.update(kw)
    return {d["key"]: d for d in ccv.systems(**args)}


def test_systems_are_seven_dots_in_order_with_short_tips():
    got = ccv.systems(probe=PROBE, basics=basics(), budget=GREEN_BUDGET, host={"show": False}, now=NOW)
    assert [d["key"] for d in got] == ["models", "repo", "budget", "host", "queue", "previews", "deploys"]
    assert all(d["tone"] == "green" for d in got), got
    assert got[0]["tip"] == "Claude Code working · 212 calls today"
    assert all(len(d["tip"].split()) <= 15 for d in got)


def test_systems_tones_come_from_their_sources():
    assert dots(host=CRITICAL)["host"]["tone"] == "red"
    assert dots(host=CRITICAL)["host"]["tip"] == "This computer is out of memory"
    assert dots(host=WARNING)["host"]["tone"] == "amber"
    assert dots(budget=ORANGE)["budget"]["tone"] == "amber"
    assert dots(budget=RED)["budget"]["tip"] == "$212 of $200 this month"
    nocap = dots(budget={"budget_state": "unknown", "cap_usd_micros": 0})["budget"]
    assert (nocap["tone"], nocap["state"]) == ("grey", "No cap")
    soon = (datetime.now(timezone.utc) + timedelta(days=2, hours=1)).isoformat()
    assert dots(basics=basics(expires=soon))["repo"]["tone"] == "amber"
    assert dots(basics=basics(repos=[]))["repo"]["state"] == "Not set up"
    assert dots(basics=basics(repos=[repo(state="token_expired", tip="Token expired")]))["repo"]["tone"] == "red"
    assert dots(probe={**PROBE, "deadletters": 1})["queue"]["tone"] == "red"
    assert dots(probe={**PROBE, "oldest_queued": NOW - timedelta(minutes=7)})["queue"]["tip"] \
        == "Oldest job waiting 7m"
    assert dots(probe={**PROBE, "oldest_queued": NOW - timedelta(minutes=7)})["queue"]["tone"] == "amber"
    assert (dots(basics=basics(models=[]))["models"]["tone"],
            dots(basics=basics(models=[]))["models"]["state"]) == ("grey", "Not set up")
    assert dots(basics=basics(models=[model(state="key_rejected", tip="Key rejected")]))["models"]["tone"] == "red"
    assert dots(basics=basics(models=[model(state="rate_limited", tip="Rate limited until 16:10")])
                )["models"]["tone"] == "amber"
    assert dots(probe={**PROBE, "failed_hour": 3})["models"]["tone"] == "amber"
    assert dots(probe={**PROBE, "previews": []})["previews"]["tone"] == "grey"
    assert dots(probe={**PROBE, "previews": [{"status": "failed", "created_at": NOW - timedelta(hours=1)}]}
                )["previews"]["tone"] == "red"
    assert dots(probe={**PROBE, "targets": 0, "deploy": None})["deploys"]["state"] == "Not set up"
    assert dots(probe={**PROBE, "deploy": {"status": "failed", "created_at": NOW, "finished_at": NOW}}
                )["deploys"]["tone"] == "red"
    assert dots(probe={**PROBE, "deploy": {"status": "running", "created_at": NOW, "finished_at": None}}
                )["deploys"]["tone"] == "amber"
    # Repo and models unreadable: Unknown, never a confident green.
    assert dots(basics=None)["models"]["state"] == "Unknown"


# ── ETag ────────────────────────────────────────────────────────────────────────────────────


def view_at(now):
    y0 = datetime(2026, 10, 3, tzinfo=timezone.utc)
    return {
        "tz": "UTC", "status": {"key": "quiet", "text": "All quiet", "tone": "green", "tip": "x"},
        "counts": {"expired_answerable": 0}, "recent": [],
        "today": {"hours": [{"start": (y0 + timedelta(hours=i)).isoformat(), "work": 0,
                             "runs_finished": 0, "runs_failed": 0, "money_micros": 0,
                             "included_micros": 0} for i in range(48)],
                  "now": now.isoformat(), "runs_finished": 0, "runs_failed": 0,
                  "money_micros": 0, "included_micros": 0, "ever_ran": True},
        "pulse": {"total": 3, "start": "2026-07-06", "last_act_at": None, "top": [],
                  "latest": []},
        "cleanup": None,
        "week": {"days": [], "runs": 0, "passed": 0},
        "budget": {"state": "green", "actual_usd_micros": 1, "cap_usd_micros": 2,
                   "day": {"cap_usd_micros": 0}},
        "host": {"show": False}, "systems": [],
        "generated_at": now.isoformat(),
    }


def test_etag_ignores_the_clock_and_moves_with_state():
    a = ccv.etag_of(view_at(NOW), '"lb-1"')
    assert a == ccv.etag_of(view_at(NOW + timedelta(seconds=40)), '"lb-1"')
    assert a != ccv.etag_of(view_at(NOW), '"lb-2"')             # the board moved
    moved = view_at(NOW)
    moved["today"]["hours"][39]["work"] = 1                      # work landed this hour
    assert a != ccv.etag_of(moved, '"lb-1"')
    acted = copy.deepcopy(view_at(NOW))
    acted["pulse"]["latest"] = [{"run_id": "crun_1", "at": "2026-10-04T10:00:00+00:00",
                                 "verb": "planned"}]
    assert a != ccv.etag_of(acted, '"lb-1"')                     # the team did something
    grouped = copy.deepcopy(view_at(NOW))
    grouped["cleanup"] = {"count": 2, "since": "2026-08-09T00:00:00+00:00", "ids": ["a", "b"]}
    assert a != ccv.etag_of(grouped, '"lb-1"')
    stalled = copy.deepcopy(view_at(NOW))
    stalled["counts"]["stalled"] = 1
    assert a != ccv.etag_of(stalled, '"lb-1"')


def test_the_zone_is_three_midnights_in_the_viewers_zone():
    y0, t0, w0 = ccv.day_bounds(datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc), "Asia/Kolkata")
    assert t0.isoformat() == "2026-10-05T00:00:00+05:30"         # 20:00 UTC is past midnight there
    assert y0.isoformat() == "2026-10-04T00:00:00+05:30"
    assert w0.isoformat() == "2026-09-29T00:00:00+05:30"


def test_an_unknown_zone_falls_back_to_utc_without_asking_twice():
    class Conn:
        calls = 0

        def begin_nested(self):
            import contextlib
            return contextlib.nullcontext()

        def execute(self, *a, **k):
            Conn.calls += 1
            return [("UTC",), ("Asia/Kolkata",)]

    ccv._ZONES = None
    try:
        assert ccv.resolve_zone(Conn(), "Asia/Kolkata") == "Asia/Kolkata"
        assert ccv.resolve_zone(Conn(), "Mars/Olympus") == "UTC"
        assert ccv.resolve_zone(Conn(), "") == "UTC"
        assert Conn.calls == 1
    finally:
        ccv._ZONES = None


# ── From the independent test (D5, D12) ─────────────────────────────────────────────────────

DISK = {"show": True, "severity": "warning", "cause": "disk",
        "headline": "This server's disk is nearly full", "detail": "Right now: disk 85% full."}
REFUSAL = {"period": "day", "cap_usd_micros": 5_000_000, "actual_usd_micros": 6_200_000}


def test_a_refusal_at_the_door_is_budget_stopped_work_without_a_build_row():
    """The budget refuses new work before any build exists, so no budget_refused card does."""
    got = status(budget=RED, refusal=REFUSAL, counts=counts(running=2))
    assert (got["key"], got["text"]) == ("budget_stopped", "Budget stopped work")
    assert got["tip"] == "Over the day cap. Raise it in Govern › Limits."
    # The budget's own state need not be red: the refusal is the budget's rule.
    assert status(budget=ORANGE, refusal=REFUSAL)["key"] == "budget_stopped"


def test_a_full_disk_is_the_host_dots_word_not_the_headline():
    assert status(host=DISK, counts=counts(running=1))["key"] == "running"
    assert status(host=DISK)["key"] == "quiet"
    dot = dots(host=DISK)["host"]
    assert (dot["tone"], dot["state"]) == ("amber", "Disk")
    assert dot["tip"] == "This server's disk is nearly full · Right now: disk 85% full."


def test_only_a_critical_host_outranks_ready_for_first_build():
    assert status(host=WARNING, runs_24h=0, ever=False)["key"] == "first_build"
    assert status(host=CRITICAL, runs_24h=0, ever=False)["key"] == "host_critical"
    assert status(host=WARNING)["key"] == "host_slow"


def test_the_sentence_never_says_running_for_runs_waiting_on_you():
    got = status(counts=counts(running=4, moving=0))
    assert got["key"] != "running"
    assert status(counts=counts(running=4, moving=1))["text"] == "1 running"


def test_not_set_up_is_one_look_on_every_dot():
    """Independent test D13: a dot that is not set up is grey and says `Not set up`, whichever."""
    got = ccv.systems(probe={**PROBE, "previews": [], "targets": 0, "deploy": None},
                      basics=basics(models=[], repos=[]), budget=GREEN_BUDGET, host={"show": False}, now=NOW)
    unset = [d for d in got if d["state"] == "Not set up"]
    assert {d["key"] for d in unset} == {"models", "repo", "previews", "deploys"}
    assert {d["tone"] for d in unset} == {"grey"}


# ── Command Center v2: stalled runs, old findings as one row, the pulse's week ───────────────


def test_a_stalled_run_is_said_after_running_and_before_setup():
    base = dict(attention=[], budget={"budget_state": "green"}, host={"show": False},
                runs_24h=0, ever=True, last_work_at=None, now=NOW)
    said = ccv.status(counts={"moving": 0, "stalled": 6}, **base)
    assert (said["key"], said["text"], said["tone"]) == ("stalled", "6 stalled", "amber")
    assert ccv.status(counts={"moving": 2, "stalled": 6}, **base)["key"] == "running"
    assert ccv.status(counts={"needs_you": 1, "stalled": 6}, **base)["key"] == "needs_you"
    assert ccv.status(counts={"stalled": 6, "setup": 1}, **base)["key"] == "stalled"


def test_old_shepherd_questions_are_one_row_and_new_ones_stay_cards():
    old = NOW - timedelta(days=40)
    items = [
        {"id": f"shepherd:shesc_{i}", "kind": "system_health", "severity": "warn",
         "at": (old + timedelta(minutes=i)).isoformat()} for i in range(10)
    ] + [
        {"id": "shepherd:shesc_new", "kind": "system_health", "severity": "warn",
         "at": (NOW - timedelta(days=1)).isoformat()},
        {"id": "system:sf_1", "kind": "system_health", "severity": "critical",
         "at": old.isoformat()},
        {"id": "dec_1", "kind": "approval", "at": old.isoformat()},
    ]
    keep, group = ccv.group_old_findings(items, NOW)
    assert [a["id"] for a in keep] == ["shepherd:shesc_new", "system:sf_1", "dec_1"]
    assert group["count"] == 10 and len(group["ids"]) == 10
    assert group["title"] == f"10 old builds stuck since {old.strftime('%b')} {old.day}"
    assert ccv.group_old_findings(keep, NOW)[1] is None


def test_the_pulse_starts_on_a_monday_thirteen_weeks_back():
    start = ccv.pulse_start(datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc), "UTC")  # a Sunday
    assert start.date().isoformat() == "2026-06-29" and start.weekday() == 0
    empty = ccv.empty_pulse(datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc), "UTC")
    assert len(empty["days"]) == 7 * 14 and empty["days"][-1]["date"] == "2026-10-04"
