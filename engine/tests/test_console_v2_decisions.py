"""The v2 decision queue.

The parity map flags this area's risk as "a gate that silently kills work", and that phrase is
literal: a decision carries a deadline_at and a default_action, so an unanswered gate does not
wait — it resolves itself, and the work either proceeds unreviewed or is dropped.

A queue that shows the question but not that consequence reads as a list of things you could
get to later, when it is actually a list of things about to be decided for you.
"""

from datetime import datetime, timedelta, timezone

import pytest

from boss_engine.services.console_v2 import decision_queue as _decision_queue

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def decision_queue(conn, project_id, **kwargs):
    # The fixtures' deadlines are relative to NOW, and the queue now drops a gate whose deadline
    # has passed (it is expired, the rule the inbox uses). Run the queue at the fixtures' clock.
    return _decision_queue(conn, project_id, now=NOW, **kwargs)


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, statement, params=None):
        # RAW, newlines intact. Normalising here would join a `--` comment onto the line
        # below it, so stripping comments afterwards eats the rest of the query.
        self.statements.append((str(statement), dict(params or {})))
        conn = self

        class _R:
            def mappings(self):
                return self

            def all(self):
                return [dict(r) for r in conn.rows]

        return _R()


def gate(did, *, question="Approve the PRD?", status="pending", deadline_min=None,
         default_action=None, risk=1, options=None, rec=None, delegated=None, age_min=0):
    return {
        "id": did,
        "task_id": "task_1",
        "question": question,
        "recommendation": rec or {},
        "options": options if options is not None else [],
        "risk_tier": risk,
        "cost_summary": {},
        "deadline_at": NOW + timedelta(minutes=deadline_min) if deadline_min is not None else None,
        "default_action": default_action,
        "status": status,
        "delegated_to": delegated,
        "created_at": NOW - timedelta(minutes=age_min),
    }


# --------------------------------------------------------------------------- #
# What happens if nobody answers
# --------------------------------------------------------------------------- #
def test_a_gate_that_will_resolve_itself_says_so():
    """This is the whole point of the screen. "approve" plus a deadline means the work
    proceeds unreviewed unless someone intervenes."""
    out = decision_queue(FakeConn([gate("d1", deadline_min=120, default_action="approve")]), "p")
    assert out["items"][0]["if_ignored"] == "approve automatically"


def test_a_gate_that_waits_forever_says_that_instead():
    """"waits for you" and "proceeds in two hours" are opposite facts. Rendering both as a
    pending row makes the second invisible."""
    out = decision_queue(FakeConn([gate("d1")]), "p")
    assert "nothing happens on its own" in out["items"][0]["if_ignored"]


def test_a_default_with_no_deadline_is_described_honestly():
    """It will fire — we just cannot say when."""
    out = decision_queue(FakeConn([gate("d1", default_action="reject")]), "p")
    assert out["items"][0]["if_ignored"] == "reject when it expires"


def test_the_self_resolving_ones_are_counted_separately():
    """A different kind of urgent: these get decided without you."""
    out = decision_queue(FakeConn([
        gate("d1", deadline_min=30, default_action="approve"),
        gate("d2"),
        gate("d3", deadline_min=90, default_action="reject"),
    ]), "p")
    assert out["total"] == 3
    assert out["auto_resolving"] == 2


# --------------------------------------------------------------------------- #
# Order: the ones about to fire come first
# --------------------------------------------------------------------------- #
def test_the_query_orders_by_deadline_with_undated_last():
    """A gate with no deadline must never outrank one that is about to fire on its own."""
    import re as _re

    conn = FakeConn([gate("d1")])
    decision_queue(conn, "p")
    # Strip SQL comments before looking: asserting on the raw text would be asserting on where
    # the explanation happens to sit, not on the ordering.
    sql_text = _re.sub(r"--[^\n]*", " ", conn.statements[0][0]).lower()
    sql_text = " ".join(sql_text.split())
    order = sql_text[sql_text.index("order by"):]
    assert "deadline_at asc nulls last" in order
    # And it must be the FIRST key, or a high-risk gate with no deadline would jump the queue
    # ahead of one that fires in ten minutes.
    assert order.index("deadline_at") < order.index("risk_tier") < order.index("created_at")


def test_only_open_gates_are_queued():
    """An answered decision is not waiting on anyone."""
    conn = FakeConn([gate("d1")])
    decision_queue(conn, "p")
    # `expired` is a candidate too: a boundary question an older build let expire still waits
    # on you (decision_actions.never_expires); attention keeps the rest out.
    assert set(conn.statements[0][1]["open"]) == {"pending", "delegated", "revision_requested",
                                                  "expired"}


def test_a_delegated_gate_is_still_open_and_says_who_has_it():
    """Delegated is not resolved. Dropping it from the queue is how a gate goes quiet."""
    out = decision_queue(FakeConn([gate("d1", status="delegated", delegated="ceo")]), "p")
    assert out["items"][0]["status"] == "delegated"
    assert out["items"][0]["delegated_to"] == "ceo"


# --------------------------------------------------------------------------- #
# Enough to decide without leaving the page
# --------------------------------------------------------------------------- #
def test_the_question_and_the_options_travel_together():
    """A gate with no options rendered is a question with no way to answer it."""
    out = decision_queue(FakeConn([gate(
        "d1", question="Ship Shorty to staging?",
        options=[{"id": "yes", "label": "Approve and deploy"}, {"id": "no", "label": "Reject"}],
    )]), "p")
    item = out["items"][0]
    assert item["question"] == "Ship Shorty to staging?"
    assert item["options"] == ["Approve and deploy", "Reject"]


def test_plain_string_options_are_handled():
    out = decision_queue(FakeConn([gate("d1", options=["approve", "reject"])]), "p")
    assert out["items"][0]["options"] == ["approve", "reject"]


def test_the_recommendation_is_carried_when_there_is_one():
    out = decision_queue(FakeConn([gate("d1", rec={"summary": "Approve — gates passed"})]), "p")
    assert out["items"][0]["recommendation"] == "Approve — gates passed"


def test_a_missing_recommendation_is_empty_not_invented():
    """No recommendation and a bland one look the same on screen; only one is honest."""
    out = decision_queue(FakeConn([gate("d1")]), "p")
    assert out["items"][0]["recommendation"] == ""


def test_the_whole_queue_is_one_query():
    conn = FakeConn([gate("d1"), gate("d2")])
    decision_queue(conn, "p")
    assert len(conn.statements) == 1


def test_an_empty_queue_is_reported_as_empty_not_as_an_error():
    out = decision_queue(FakeConn([]), "p")
    assert out["items"] == []
    assert out["total"] == 0
    assert out["auto_resolving"] == 0


# --------------------------------------------------------------------------- #
# Panels
#
# The reference streams a scripted debate — agents speaking in turn, votes tallying, a verdict.
# BOSS's panels are real and produce real minutes, but they are NOT streamed. Animating a
# debate that nobody is watching would be the most convincing lie in the console.
# --------------------------------------------------------------------------- #
from boss_engine.services.console_v2 import panel_sessions  # noqa: E402


def meeting(mid, *, title="Pricing panel", status="completed", participants=None,
            decisions=None, dissent=None, blocked=None, minutes="art_1", age_min=0):
    return {
        "id": mid,
        "title": title,
        "purpose": "Decide the pricing model.",
        "status": status,
        "participants": participants if participants is not None else [{"role": "cpo"}, {"role": "cto"}],
        "decisions": decisions if decisions is not None else [{"summary": "Go freemium"}],
        "dissent_records": dissent or [],
        "minutes_artifact_id": minutes,
        "transcript_artifact_id": None,
        "blocked_state": blocked,
        "completed_at": NOW - timedelta(minutes=age_min),
        "created_at": NOW - timedelta(minutes=age_min + 5),
    }


def test_the_payload_says_panels_are_not_streamed():
    """So the screen cannot animate a debate that is not being watched."""
    out = panel_sessions(FakeConn([meeting("m1")]), "p")
    assert out["streamed"] is False
    assert "not streamed" in out["stream_note"]


def test_a_running_panel_is_marked_deliberating():
    out = panel_sessions(FakeConn([meeting("m1", status="running")]), "p")
    assert out["sessions"][0]["deliberating"] is True
    assert out["deliberating"] == 1


def test_a_completed_panel_is_not_deliberating():
    out = panel_sessions(FakeConn([meeting("m1", status="completed")]), "p")
    assert out["sessions"][0]["deliberating"] is False


def test_dissent_is_carried_separately_from_the_decision():
    """A panel that agreed and a panel that overruled an objection produce the same decision
    and are not the same event. BOSS records this and v1 never showed it."""
    out = panel_sessions(FakeConn([meeting(
        "m1",
        decisions=[{"summary": "Ship it"}],
        dissent=[{"note": "Legal objected: consent copy unresolved"}],
    )]), "p")
    session = out["sessions"][0]
    assert session["decisions"] == ["Ship it"]
    assert "Legal objected" in session["dissent"][0]


def test_a_blocked_panel_is_neither_running_nor_concluded():
    """Showing it as either loses the fact that it needs someone."""
    out = panel_sessions(FakeConn([meeting(
        "m1", status="blocked", blocked={"reason": "waiting on the PRD"},
    )]), "p")
    session = out["sessions"][0]
    assert session["blocked"] is True
    assert "waiting on the PRD" in session["blocked_reason"]
    assert out["blocked"] == 1


def test_a_panel_with_no_minutes_says_so():
    out = panel_sessions(FakeConn([meeting("m1", minutes=None)]), "p")
    assert out["sessions"][0]["has_minutes"] is False


def test_panelists_are_listed():
    out = panel_sessions(FakeConn([meeting("m1", participants=[{"role": "cpo"}, "cto"])]), "p")
    assert out["sessions"][0]["panelists"] == ["cpo", "cto"]


def test_the_title_is_cleaned_like_everywhere_else():
    out = panel_sessions(FakeConn([meeting("m1", title="Shorty — Re-build PRD")]), "p")
    assert out["sessions"][0]["title"] == "Shorty"


def test_a_framed_meeting_title_reads_as_its_subject_in_v2():
    """Review of milestone/console-b (2026-10-02): v2 sessions cleaned meetings.title with the
    product-name cleaner only, so an older workflow-convened meeting was still named by the
    panel framing there while the v1 Meetings cards showed its subject."""
    from boss_engine.services import meeting_service as ms

    stored = ("PRD meeting: " + ms.PANEL_PURPOSE_MARKER
              + "\nYou are the panel inside BOSS's weekly-newsletter workflow. That wo")
    out = panel_sessions(FakeConn([meeting("m1", title=stored)]), "p")
    title = out["sessions"][0]["title"]
    assert title == "PRD meeting: panel for the weekly-newsletter workflow"
    assert "---" not in title


# --------------------------------------------------------------------------- #
# What was actually said
#
# The reference shows each panelist's argument. BOSS records the same thing as
# {speaker, message, phase} turns in the transcript artifact, and the console has never
# displayed them — a panel whose reasoning is invisible is indistinguishable from a coin toss
# with minutes attached.
# --------------------------------------------------------------------------- #
def test_the_transcript_joins_rather_than_costing_a_call_per_session():
    """One query whatever the session count. A read per session is the N+1 that made v1 slow."""
    conn = FakeConn([meeting("m1")])
    panel_sessions(conn, "p")
    assert len(conn.statements) == 1
    assert "LEFT JOIN artifacts" in conn.statements[0][0]


def test_turns_are_read_from_the_transcript():
    out = panel_sessions(FakeConn([dict(meeting("m1"), transcript={
        "transcript": [
            {"speaker": "cto", "message": "Ship it.", "phase": "review"},
            {"speaker": "qa", "message": "Two tests missing.", "phase": "review"},
        ]
    })]), "p")
    turns = out["sessions"][0]["turns"]
    assert [t["speaker"] for t in turns] == ["cto", "qa"]
    assert turns[0]["text"] == "Ship it."
    assert turns[1]["phase"] == "review"


def test_a_transcript_stored_as_json_text_is_still_read():
    import json as _json

    out = panel_sessions(FakeConn([dict(meeting("m1"), transcript=_json.dumps({
        "transcript": [{"speaker": "cto", "message": "Ship it."}]
    }))]), "p")
    assert out["sessions"][0]["turns"][0]["text"] == "Ship it."


def test_no_stance_is_invented():
    """BOSS does not record FOR/AGAINST per turn. Guessing would put words in an agent's mouth
    on the record."""
    out = panel_sessions(FakeConn([dict(meeting("m1"), transcript={
        "transcript": [{"speaker": "cto", "message": "I have concerns about the cost."}]
    })]), "p")
    assert out["sessions"][0]["turns"][0]["stance"] is None


def test_a_recorded_stance_is_kept():
    out = panel_sessions(FakeConn([dict(meeting("m1"), transcript={
        "transcript": [{"speaker": "cto", "message": "Yes.", "stance": "FOR"}]
    })]), "p")
    assert out["sessions"][0]["turns"][0]["stance"] == "FOR"


def test_a_turn_with_no_text_is_dropped():
    out = panel_sessions(FakeConn([dict(meeting("m1"), transcript={
        "transcript": [{"speaker": "cto"}, {"speaker": "qa", "message": "Fine."}]
    })]), "p")
    assert len(out["sessions"][0]["turns"]) == 1


def test_an_unattributed_turn_says_so_rather_than_being_assigned():
    out = panel_sessions(FakeConn([dict(meeting("m1"), transcript={
        "transcript": [{"message": "Someone said this."}]
    })]), "p")
    assert out["sessions"][0]["turns"][0]["speaker"] == "(unattributed)"


def test_a_malformed_transcript_yields_no_turns_rather_than_crashing():
    for bad in ("not json", {"transcript": "nope"}, {"other": []}, None, 42):
        out = panel_sessions(FakeConn([dict(meeting("m1"), transcript=bad)]), "p")
        assert out["sessions"][0]["turns"] == [], bad


# --------------------------------------------------------------------------- #
# The deliberation lives in the MINUTES
#
# Reading only the transcript found nothing on twenty real sessions and reported "no transcript
# recorded" — while the reasoning sat in the other artifact. Real BOSS meetings write minutes
# carrying round1_perspectives, round2_debate and a synthesis; the transcript path is only
# taken by simulated meetings.
# --------------------------------------------------------------------------- #
MINUTES = {
    "round1_perspectives": {
        "Product Manager": "Freemium wins discovery.",
        "CFO": "Free processing costs $0.14/user.",
    },
    "round2_debate": {
        "Product Manager": "Cap the free tier at three reports.",
        "CFO": "Then I can model break-even.",
    },
    "synthesis": "Freemium with a paid deep-report upsell.",
}


def test_the_deliberation_is_read_from_the_minutes():
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes=MINUTES)]), "p")
    turns = out["sessions"][0]["turns"]
    assert len(turns) == 4
    assert {t["speaker"] for t in turns} == {"Product Manager", "CFO"}


def test_both_rounds_are_kept_and_labelled():
    """Two rounds is richer than the reference's one, and which round an argument came from
    changes what it means — round 2 is a response."""
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes=MINUTES)]), "p")
    phases = [t["phase"] for t in out["sessions"][0]["turns"]]
    assert phases[:2] == ["round 1", "round 1"]
    assert phases[2:] == ["round 2", "round 2"]


def test_the_synthesis_is_the_verdict():
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes=MINUTES)]), "p")
    assert out["sessions"][0]["synthesis"] == "Freemium with a paid deep-report upsell."


def test_minutes_stored_as_json_text_are_still_read():
    import json as _json

    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes=_json.dumps(MINUTES))]), "p")
    assert len(out["sessions"][0]["turns"]) == 4


def test_a_transcript_wins_when_one_exists():
    """A simulated meeting has a real transcript; it should not be replaced by the minutes."""
    out = panel_sessions(FakeConn([dict(
        meeting("m1"),
        transcript={"transcript": [{"speaker": "fixture", "message": "phase recorded"}]},
        minutes=MINUTES,
    )]), "p")
    assert [t["speaker"] for t in out["sessions"][0]["turns"]] == ["fixture"]


def test_no_stance_is_inferred_from_the_minutes():
    """A guessed vote on the record is worse than no vote."""
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes=MINUTES)]), "p")
    assert all(t["stance"] is None for t in out["sessions"][0]["turns"])


def test_an_empty_argument_is_dropped():
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes={
        "round1_perspectives": {"PM": "", "CFO": "Real point."},
    })]), "p")
    assert [t["speaker"] for t in out["sessions"][0]["turns"]] == ["CFO"]


def test_malformed_minutes_yield_nothing_rather_than_crashing():
    for bad in ("not json", {"round1_perspectives": "nope"}, {}, None, 7):
        out = panel_sessions(FakeConn([dict(meeting("m1"), minutes=bad)]), "p")
        assert out["sessions"][0]["turns"] == [], bad
        assert out["sessions"][0]["synthesis"] is None


def test_a_meeting_with_neither_artifact_says_nothing_rather_than_guessing():
    out = panel_sessions(FakeConn([meeting("m1")]), "p")
    assert out["sessions"][0]["turns"] == []
    assert out["sessions"][0]["synthesis"] is None


# --- clipping a long argument -------------------------------------------------------------
#
# A panelist's argument is bounded before it travels, and both halves of that matter: the cut
# must not break the markdown it lands in, and the reader must be told the cut happened.

def test_a_cut_never_leaves_an_unclosed_emphasis():
    """This is the live bug: text[:limit] landed inside **...** and shipped a bare **.

    The renderer then showed two literal asterisks against half a word, because that is exactly
    what the string said.
    """
    # The ** opener must land JUST INSIDE the 4000-char budget and its partner outside it.
    # An earlier version of this test used filler so long that the cut never reached any
    # emphasis at all, so it asserted "balanced" about text that contained none -- and passed
    # against the very bug it was written for.
    head = "filler sentence. " * 230                        # 3910 chars
    bold = "**" + "Responsibilities are the risk. " * 8 + "**"
    assert len(head) < 4000 < len(head + bold), (len(head), len(head + bold))
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes={
        "round1_perspectives": {"PM": head + bold},
    })]), "p")
    text = out["sessions"][0]["turns"][0]["text"]
    assert text.count("**") % 2 == 0, text[-80:]
    assert not text.endswith("**")


def test_an_unclosed_code_fence_is_cut_back_too():
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes={
        "round1_perspectives": {"PM": "x " * 2200 + "```python\nrun()"},
    })]), "p")
    assert out["sessions"][0]["turns"][0]["text"].count("```") % 2 == 0


def test_a_clipped_argument_says_so_and_a_whole_one_does_not():
    """Silence would read as an argument that finished, so the reader would judge reasoning
    they were never shown."""
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes={
        "round1_perspectives": {"Long": "word " * 2000, "Short": "Brief but complete."},
    })]), "p")
    by = {t["speaker"]: t for t in out["sessions"][0]["turns"]}
    assert by["Long"]["truncated"] is True
    assert by["Short"]["truncated"] is False
    assert by["Short"]["text"] == "Brief but complete."


def test_a_cut_lands_on_a_boundary_not_mid_word():
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes={
        "round1_perspectives": {"PM": "alpha bravo charlie " * 400},
    })]), "p")
    text = out["sessions"][0]["turns"][0]["text"]
    assert text.split()[-1] in ("alpha", "bravo", "charlie"), text[-40:]


def test_clipping_does_not_quietly_drop_most_of_the_argument():
    """A boundary search that walked back too far would satisfy every test above by shipping
    almost nothing.

    The realistic shape of that failure is an early paragraph break: one short opening line,
    then five thousand words. Searching back for the last \\n\\n before the limit must not
    land on that first break and throw the argument away.
    """
    out = panel_sessions(FakeConn([dict(meeting("m1"), minutes={
        "round1_perspectives": {"PM": "Short opener.\n\n" + "word " * 3000},
    })]), "p")
    assert len(out["sessions"][0]["turns"][0]["text"]) >= 2000
