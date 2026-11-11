"""One review session sits every LLM seat over one diff (BOSSAPP-1000, audit 997 §7.3).

review_code + security_check + consistency_check ran as three separate full CLI sessions over
the SAME inline change — $375.98 of the panel's $389.91/month, 22.7% of all ledgered spend —
each re-onboarding onto a text the others had already read. One session (review_panel) now
reads the change once and casts each enabled seat's verdict in its own marked section. The
verdict grammar is review_verdict's, unchanged; a missing section falls back to that seat's
own session; a refute panel of k > 1 and the fix lane keep their old shapes; and on repair
rounds the security seat is scoped to the repair delta under repair_review_scope's guardrails
instead of always re-reading the full change.
"""
from __future__ import annotations

import inspect

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import review_panel as rp
from boss_engine.services.review_verdict import parse_consistency, parse_security, parse_verdict

ANSWER = """REVIEWED: src/slug.js, src/server.js, test/slug.test.js
=== CODE REVIEW ===
Reviewed src/slug.js and src/server.js against the criteria.
FINDING: MINOR src/server.js:12 — the port fallback is untested.
VERDICT: SURVIVED
=== SECURITY REVIEW ===
No injection, auth, secret or traversal issue reachable from input.
VERDICT: CLEAN
=== CONSISTENCY REVIEW ===
The implementation and tests agree with the criteria.
CONSISTENT: YES
"""


def _sections():
    return [("review_code", "judge correctness"), ("security_check", "judge security"),
            ("consistency_check", "judge consistency")]


# ── the prompt ─────────────────────────────────────────────────────────────────────────────────

def test_the_combined_prompt_asks_for_one_section_per_seat_and_shares_the_change_once():
    common = "TASK: slugs\n\nTHE CHANGE UNDER REVIEW — judge THIS change directly.\n=== FILE: x ==="
    prompt = rp.combined_prompt(_sections(), common=common)
    for head in ("=== CODE REVIEW ===", "=== SECURITY REVIEW ===", "=== CONSISTENCY REVIEW ==="):
        assert head in prompt
    assert prompt.count("THE CHANGE UNDER REVIEW") == 1
    assert "end THAT SEAT'S SECTION" in prompt
    # Each seat's own instructions ride along unchanged.
    for words in ("judge correctness", "judge security", "judge consistency"):
        assert words in prompt


def test_a_subset_of_seats_makes_a_subset_prompt():
    prompt = rp.combined_prompt([("review_code", "a"), ("security_check", "b")], common="c")
    assert "=== CODE REVIEW ===" in prompt and "=== SECURITY REVIEW ===" in prompt
    assert "=== CONSISTENCY REVIEW ===" not in prompt


# ── the split ──────────────────────────────────────────────────────────────────────────────────

def test_the_answer_splits_into_the_seats_own_summaries():
    parts = rp.split(ANSWER, list(rp.SEATS))
    assert parts["review_code"].endswith("VERDICT: SURVIVED")
    assert "FINDING: MINOR src/server.js:12" in parts["review_code"]
    assert parts["security_check"].endswith("VERDICT: CLEAN")
    assert parts["consistency_check"].endswith("CONSISTENT: YES")
    # One seat's words never leak into another's gate.
    assert "CLEAN" not in parts["review_code"]
    assert "CONSISTENT" not in parts["security_check"]


def test_each_section_parses_with_the_same_verdict_grammar():
    parts = rp.split(ANSWER, list(rp.SEATS))
    assert parse_verdict(parts["review_code"]) == "survived"
    assert parse_security(parts["security_check"]) == "clean"
    assert parse_consistency(parts["consistency_check"]) == "consistent"


def test_the_reviewed_line_before_the_first_heading_stays_with_the_first_section():
    parts = rp.split(ANSWER, list(rp.SEATS))
    assert parts["review_code"].startswith("REVIEWED: src/slug.js")


def test_a_missing_or_decorated_section_is_handled():
    parts = rp.split("## === CODE REVIEW ===\nok\nVERDICT: SURVIVED", list(rp.SEATS))
    assert parse_verdict(parts["review_code"]) == "survived"
    assert parts["security_check"] == "" and parts["consistency_check"] == ""
    assert rp.split("", ["review_code"]) == {"review_code": ""}
    # A model restating a heading: the LAST one wins, as the verdict parsers read markers.
    twice = ("=== SECURITY REVIEW ===\ndraft\n=== SECURITY REVIEW ===\nfinal\nVERDICT: CLEAN")
    assert rp.split(twice, ["security_check"])["security_check"].startswith("final")


# ── the wiring ─────────────────────────────────────────────────────────────────────────────────

def test_the_panel_builds_one_merged_seat_and_keeps_regression():
    ran: list[str] = []

    def mark(label):
        return lambda: ran.append(label)

    seats = g.panel_seats({"review_code", "security_check", "consistency_check", "regression"},
                          [mark("review0")], security=mark("security"),
                          regression=mark("regression"), consistency=mark("consistency"),
                          merged=mark("panel"))
    assert [label for label, _ in seats] == ["panel", "regression"]
    # Without a merged session the panel keeps its old shape (refute panel, fix lane).
    seats = g.panel_seats({"review_code", "security_check", "regression"}, [mark("review0")],
                          security=mark("security"), regression=mark("regression"),
                          consistency=mark("consistency"))
    assert [label for label, _ in seats] == ["review[0]", "security", "regression"]
    # A merged panel still never runs a seat the profile skipped.
    seats = g.panel_seats({"regression"}, [mark("review0")], security=mark("security"),
                          regression=mark("regression"), consistency=mark("consistency"),
                          merged=mark("panel"))
    assert [label for label, _ in seats] == ["regression"]


def test_the_pipeline_merges_only_a_panel_of_one_and_falls_back_per_seat():
    src = inspect.getsource(g._run_governed_code_task)
    block = src[src.index("_merged_seats = "):src.index("guarded = panel_seats(")]
    assert "panel_size == 1 and lane != fix_lane_triage.LANE_FIX" in block
    assert 'len(_merged_seats) >= 2' in block
    # One judged session under its own stage; the same hollow retry as every seat.
    assert "_judge_seat(review_panel.STAGE" in block
    assert "review_panel.combined_prompt(sections, common=common)" in block
    # A missing section runs that seat's own session — the closures the old panel used.
    assert "review_checks[0]()" in block and "_do_security()" in block \
        and "_do_consistency()" in block
    # The merged seat is handed to the one seat-list builder, never run on the side.
    assert "merged=_do_merged_panel if _merge_panel else None" in src


def test_the_merged_session_scopes_security_to_the_repair_delta():
    src = inspect.getsource(g._run_governed_code_task)
    block = src[src.index("_merged_seats = "):src.index("guarded = panel_seats(")]
    assert '_scope_blocks.get("security_check")' in block
    # And the standalone security seat (fallback + re-review path) is scoped the same way.
    assert inspect.getsource(g).count('_scope_blocks.get("security_check")') >= 2


def test_the_merged_stage_is_its_own_ledger_class_and_effort():
    from boss_engine.services.call_classes import class_of
    from boss_engine.services.code_agent import stage_effort
    from boss_engine.services.llm_telemetry import role_for_stage

    assert class_of(rp.STAGE) == "build_stage"
    assert role_for_stage(rp.STAGE) == "adversarial_reviewer"
    assert stage_effort(rp.STAGE) == "high"


def test_the_scripted_engine_answers_the_merged_panel_in_sections(tmp_path):
    from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
    from boss_engine.services.scripted_engine import ScriptedEngine

    fixture = tmp_path / "fixture"
    (fixture / "src").mkdir(parents=True)
    (fixture / "src" / "slug.js").write_text("export const slug = (s) => s;\n")
    engine = ScriptedEngine(faults=set(), caps=set(), fixture=fixture, app_dir="plain")
    set_llm_context(stage=rp.STAGE)
    try:
        prompt = rp.combined_prompt(_sections(), common="TASK: slugs")
        out = engine.run_role(prompt, tmp_path, read_only=True)
    finally:
        clear_llm_context()
    parts = rp.split(out["summary"], list(rp.SEATS))
    assert parse_verdict(parts["review_code"]) == "survived"
    assert parse_security(parts["security_check"]) == "clean"
    assert parse_consistency(parts["consistency_check"]) == "consistent"
