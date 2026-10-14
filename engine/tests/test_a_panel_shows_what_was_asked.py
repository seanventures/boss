"""A panel shows what the person asked — never the prompt BOSS built around it.

The shared server, 2026-10-04: 4 of 22 panels showed "--- RESEARCH ALREADY DONE FOR THIS --- BOSS
researched this before convening you … ## What was researched {" in the hero line, the cards
and the drawer, and a dossier-promoted panel was titled "Produce a PRD for this
evidence-backed opportunity …". The operator's words are stored apart from the scaffolding
when a panel is convened; older rows have it taken off deterministically on read.
"""
from __future__ import annotations

from boss_engine.services import discovery_service as ds
from boss_engine.services import meeting_brief as mb
from boss_engine.services import meeting_service as ms
from boss_engine.services.meeting_rooms import typed_result

ASK = ("Build a lead triage inbox.\nWho it is for: a small team drowning in inbound that is mostly "
       "noise.\nNothing sends without a human pressing send.")
# Exactly how the shared server's rows read: research clipped mid-JSON, findings as inline objects.
RESEARCH = (
    "--- RESEARCH ALREADY DONE FOR THIS ---\n"
    "BOSS researched this before convening you. Argue from it: agree, disagree, or say what it "
    "missed — do not re-derive it from scratch.\n\n# Build a lead triage inbox.\n\n"
    "## What was researched\n{\n \"questions\": [\n  \"Would users trust an AI decline?\",\n"
    "  \"Standalone or a layer on Gmail?\"\n ],\n \"hypothesis\": \"Small teams have no lightw\n\n"
    "## What the research found\n"
    "- {\"angle\": \"competitor\", \"finding\": \"Instantly already drafts replies\", "
    "\"citation_url\": \"https://instantly.ai/x\"}\n"
)
SCAFFOLD = ("---", "{", "\"questions\"", "BOSS researched this", "RESEARCH ALREADY", "[boss:",
            "WHAT THIS PANEL IS FOR", "Produce a PRD")


def _clean(text: str) -> bool:
    return not any(s in text for s in SCAFFOLD)


def test_the_brief_is_the_ask_and_the_research_is_readable_context():
    got = mb.split_brief(f"{ASK}\n\n{RESEARCH}")
    assert got["brief"] == ASK
    ctx = got["context"]
    assert "### Research already done for this" in ctx
    assert "- **Questions**" in ctx and "  - Would users trust an AI decline?" in ctx
    assert "- Competitor: Instantly already drafts replies (https://instantly.ai/x)" in ctx
    assert "{" not in ctx and "BOSS researched this" not in ctx


def test_a_workflow_framed_directive_keeps_only_the_ask():
    framed = (f"{ms.PANEL_PURPOSE_MARKER}\nYou are the panel inside BOSS's standard workflow.\n"
              f"{ms.PANEL_ASK_MARKER}\n\nAdd snoozing to the inbox [boss:step=meeting].\n\n{RESEARCH}")
    got = mb.split_brief(framed)
    assert got["brief"] == "Add snoozing to the inbox."
    assert "### What this panel is for" in got["context"]
    assert _clean(got["brief"])


def test_a_stored_brief_wins_over_anything_read_out_of_the_prompt():
    assert mb.split_brief(f"{ASK}\n\n{RESEARCH}", stored="Just the ask")["brief"] == "Just the ask"


def test_a_promoted_outcome_is_named_for_the_outcome_old_and_new():
    run = {"id": "drun_1", "title": "Lead triage inbox", "seed": ASK, "state": {
        "candidate_outcomes": [{"outcome": "A narrow-scope 'batch triage' prototype: user pastes a batch",
                                "target_pilot": "2-3 small agencies"}]}}
    directive, title = ds.build_promote_directive(run, 0)
    assert title == "Narrow-scope batch triage prototype — lead triage inbox"
    # An old row, titled with the template before this fix, reads the same on the way out.
    assert mb.panel_title("Produce a PRD for this evidence-backed opportunity, surfaced by", directive) \
        == "Narrow-scope batch triage prototype — lead triage inbox"
    got = mb.split_brief(directive)
    assert got["brief"] == ("A narrow-scope 'batch triage' prototype: user pastes a batch\n"
                            "Who it is for: 2-3 small agencies")
    assert _clean(got["brief"])


def test_a_go_no_go_says_its_verdict_and_keeps_its_dissent():
    state = {"decision_items": [{"recommendation": {"verdict": "go"}}],
             "dissent_records": [{"role": "QA / Test", "position": "no-go", "reason": "Flake open"}],
             "action_items": [{"title": "Record residual risk"}]}
    got = typed_result("go_no_go", state)
    assert got["verdict"] == "go"
    assert got["dissent"] == [{"role": "QA / Test", "position": "no-go", "reason": "Flake open"}]
    assert got["action_items"][0]["title"] == "Record residual risk"
    assert typed_result("go_no_go", {"synthesis": "Verdict: NO-GO."})["verdict"] == "no_go"
    assert typed_result("prd_panel", state)["verdict"] is None


# ── A recorded vote names no machine voter ─────────────────────────────────────────────────

def test_a_counted_vote_has_no_invented_voters():
    """The console records counts, not voters. Inventing ids for them put "r0: voted to reject"
    on the meeting's record; unnamed votes are one record in words."""
    from boss_engine.services.meeting_consensus import tally_votes

    got = tally_votes([{"vote": "approve"}, {"vote": "reject"}, {"vote": "reject"}], eligible_count=3)
    assert got["dissent_records"] == [{"role": "panel", "position": "reject", "reason": "2 seats voted to reject"}]
    named = tally_votes([{"voter": "QA / Test", "vote": "reject", "reason": "Flake open"}], eligible_count=4)
    assert named["dissent_records"][0] == {"role": "QA / Test", "position": "reject", "reason": "Flake open"}
    assert named["dissent_records"][1]["reason"].startswith("Only 1 of the 2 votes needed were cast")
