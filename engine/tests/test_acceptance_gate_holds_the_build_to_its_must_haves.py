"""THE ACCEPTANCE GATE (BOSSAPP-1211 / RCA-E) and THE UNCUT BRIEF (BOSSAPP-1212 / RCA-F).

THE CASE (Northgate, 2026-11-15): a Go API gateway + gwlint brief. The build shipped missing
about fifteen must-haves, the rulebook among them. Its reviewers said so, but under the community
default profile `lean` their verdicts were advisory, the PR opened, and the follow-up run's spec
was two MINOR findings, so its merge card said "Automated checks: passed". The brief's must-haves
had been cut at 200 characters and its done-when at 400, and the PRD's seventeen tested-and-
gating criteria never became the build's acceptance criteria.

The reviewer in these tests is scripted (no model): the scripted conformance engine, and a fake
that reads the tree the way a reviewer would."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from boss_engine.services import acceptance_gate as ag
from boss_engine.services import checks_failing as cf
from boss_engine.services import fix_lane_triage
from boss_engine.services import governed_code_task_service as g
from boss_engine.services.pipeline_profiles import resolve_profile
from boss_engine.services.project_experience import add_project
from boss_engine.services.scripted_engine import ScriptedEngine
from boss_engine.services.workflow_capability_runner import prd_acceptance_inputs

from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

LONG_MUST_HAVE = (
    "northgate-gateway: reverse-proxy API gateway (Go) with host/path/method routing, "
    "multi-tenant config/gateway.yaml + config/tenants/<tenant>.yaml, and hot reload that keeps "
    "the last good config on error, logs the rejected file with its line and reason, and counts "
    "it in config_reload_total{result=\"rejected\"} so the demo can show the fail-closed path")
RULEBOOK = ("gwlint CLI: validate tenant YAML against a JSON Schema and an api-guidelines.yaml "
            "rulebook of at least 20 security/guideline rules, with exit codes 0/1/2")
DONE_WHEN = (
    "make run-staging boots the gateway and mock upstreams; a valid JWT request reaches the "
    "upstream, an unauthenticated one gets 401, the 11th request/second gets 429; editing "
    "config/tenants/lumen-pay.yaml hot-reloads without restart and a broken edit is rejected "
    "while the previous config keeps serving; gwlint validate exits 1 on lumen-pay.bad.yaml "
    "naming each violation with a line and fix, gwlint --fix writes a corrected file and lists "
    "the rules that need a human")

PRD = """# PRD: Northgate API Gateway + gwlint

## 5. Proposed Solution
Go, with a Python script for the docs build (rejected: Node).

## 8. Acceptance Criteria — labeled, not implied

Every item below carries an explicit status.

### Tested-and-gating
1. `make run-staging` boots the gateway and mock upstreams.
2. A request without a valid JWT gets 401.
3. `gwlint validate examples/lumen-pay.bad.yaml` exits 1, reports >=5 distinct rule IDs.
4. The api-guidelines.yaml rulebook holds at least 20 rules.

### Built-but-unverified-in-demo (shipped if time allows, not gating)
- mTLS client certificate support
- Circuit breaker behaviour under sustained upstream failure

## 9. Risks
- none
"""


def _brief() -> dict:
    return add_project.shape_brief({
        "title": "Northgate API Gateway + gwlint",
        "problem": "Automate tenant API onboarding.",
        "must_haves": [LONG_MUST_HAVE, RULEBOOK,
                       "Demo content: examples/lumen-pay.good.yaml and lumen-pay.bad.yaml"],
        "done_when": DONE_WHEN,
    })


def _northgate_spec() -> dict:
    directive = add_project.render_brief(_brief(), "Build the Northgate demo.")
    instructions = (directive + "\n\n---\n\nTHE PRODUCT SPEC BELOW IS THE BRIEF.\n\n" + PRD)
    return {"instructions": instructions,
            **prd_acceptance_inputs({"acceptance_criteria": [DONE_WHEN]}, PRD)}


def _northgate_tree(root: Path, *, rulebook: bool, go_mod: bool = True) -> Path:
    files = {
        "cmd/northgate/main.go": "package main\nfunc main() {}\n",
        "internal/config/reload.go": "package config // hot reload keeps the last good config\n",
        "cmd/gwlint/main.go": "package main // gwlint validate / --fix, exit codes 0/1/2\n",
        "examples/lumen-pay.good.yaml": "tenant: lumen-pay\n",
        "examples/lumen-pay.bad.yaml": "tenant: lumen-pay\n",
        "Makefile": "run-staging:\n\tdocker compose up\n",
        "config/gateway.yaml": "listen: :8080\n",
        "config/tenants/lumen-pay.yaml": "tenant: lumen-pay\n",
    }
    if go_mod:
        files["go.mod"] = "module northgate\n\ngo 1.22\n"
    if rulebook:
        files["config/api-guidelines.yaml"] = "rules:\n" + "".join(
            f"  - id: R{i}\n" for i in range(20))
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


class TreeReadingReviewer:
    """A scripted reviewer that judges each item by whether the files it names are in the tree
    (what a reviewer that opened the tree would find) — no model, deterministic."""

    def run_role(self, prompt, cwd, *, read_only=False, model=None, **_):
        import re

        assert read_only, "the acceptance reviewer never edits"
        root = Path(cwd)
        lines = []
        for n, text in re.findall(r"(?m)^ITEM (\d+) \[\w+\]: (.*)$", prompt):
            named = re.findall(r"[\w./<>-]+\.(?:ya?ml|go)\b", text)
            missing = [f for f in named if "<" not in f and not list(root.rglob(Path(f).name))]
            if missing:
                lines.append(f"ITEM {n}: MISSING — looked for {', '.join(missing)}; not in the tree")
            else:
                lines.append(f"ITEM {n}: MET — cmd/northgate/main.go, cmd/gwlint/main.go")
        verdict = "NOT MET" if any("MISSING" in ln for ln in lines) else "MET"
        return {"summary": "\n".join(lines) + f"\nACCEPTANCE: {verdict}", "cost_usd_micros": 0}


def _check(root: Path, spec: dict, reviewer) -> dict:
    """What the pipeline's acceptance seat does (governed_code_task_service._run_acceptance_check)."""
    items = ag.acceptance_items(spec)
    stack = ag.stack_manifest_check(root, spec)
    reply = reviewer.run_role(ag.acceptance_prompt(items, title="Northgate", stack=stack,
                                                   tree=ag.tree_listing(root)),
                              root, read_only=True)
    return ag.judge(items, reply["summary"], stack)


# ── RCA-F: the brief is never cut mid-requirement ──────────────────────────────────────────

def test_long_must_haves_and_done_when_round_trip_through_render_brief_uncut():
    brief = _brief()
    assert brief["must_haves"][0] == LONG_MUST_HAVE and len(LONG_MUST_HAVE) > 200
    assert brief["done_when"] == DONE_WHEN and len(DONE_WHEN) > 400
    rendered = add_project.render_brief(brief, "words " * 4000)
    assert LONG_MUST_HAVE in rendered and DONE_WHEN in rendered
    assert ag.brief_must_haves(rendered)[0] == LONG_MUST_HAVE
    # The cap is the directive's (MAX_DIRECTIVE), and only the quoted words give way to it.
    assert len(rendered) <= add_project.MAX_DIRECTIVE
    assert "In the person's words:" in rendered and rendered.endswith("…")


def test_the_brief_keeps_more_than_six_must_haves():
    many = [f"must-have number {i} " + "x" * 300 for i in range(10)]
    brief = add_project.shape_brief({"title": "t", "must_haves": many, "done_when": "d"})
    assert brief["must_haves"] == [" ".join(m.split()) for m in many]


def test_build_acceptance_criteria_equal_the_prds_gating_list():
    expected = ["`make run-staging` boots the gateway and mock upstreams.",
                "A request without a valid JWT gets 401.",
                "`gwlint validate examples/lumen-pay.bad.yaml` exits 1, reports >=5 distinct "
                "rule IDs.",
                "The api-guidelines.yaml rulebook holds at least 20 rules."]
    out = prd_acceptance_inputs({"acceptance_criteria": [DONE_WHEN], "title": "x"}, PRD)
    assert out["acceptance_criteria"] == expected
    assert out["title"] == "x"
    # The not-gating sub-list never becomes a criterion.
    assert not any("mTLS" in c for c in out["acceptance_criteria"])
    # A PRD without an acceptance section leaves the criteria as they were.
    assert prd_acceptance_inputs({"acceptance_criteria": ["a"]}, "# PRD\n\n## Goals\n- g\n") == {
        "acceptance_criteria": ["a"]}


def test_a_follow_up_inherits_its_roots_items():
    class Conn:
        def execute(self, *_a, **_k):
            return type("R", (), {"first": lambda self: (json.dumps(_northgate_spec()),)})()

    own = {"instructions": "Fix the two MINOR findings", "acceptance_criteria": ["Fixed: a"]}
    spec = ag.items_spec(Conn(), run_id="crun_f", own_spec=own,
                         fctx={"is_followup": True, "root_run_id": "crun_root"})
    texts = [i["text"] for i in ag.acceptance_items(spec)]
    assert LONG_MUST_HAVE in texts and RULEBOOK in texts
    # A root run (or a repair child, whose spec is its parent's) is held to its own.
    assert ag.items_spec(Conn(), run_id="crun_r", own_spec=own, fctx={}) == own


# ── RCA-E: the gate ─────────────────────────────────────────────────────────────────────────

def test_the_items_are_the_must_haves_the_prd_gating_list_and_the_criteria():
    items = ag.acceptance_items(_northgate_spec())
    sources = [i["source"] for i in items]
    assert sources[:3] == ["must_have"] * 3
    assert sources.count("prd_gating") == 4
    assert all(len(i["text"]) <= ag.MAX_ITEM_CHARS for i in items)


def test_the_required_stack_comes_from_the_must_haves_not_the_whole_prd():
    stacks = ag.required_stack(_northgate_spec())
    assert [s["stack"] for s in stacks] == ["go"], stacks   # not the PRD's Python/Node mentions
    assert ag.required_stack({"instructions": "Notes app\n\nMust-haves:\n- no Python, please"}) == []
    # RCA-A's structured field wins when present.
    assert [s["stack"] for s in ag.required_stack({"required_stack": ["rust"]})] == ["rust"]


def test_a_go_brief_without_go_mod_fails_on_the_manifest_alone(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True, go_mod=False)
    rec = _check(root, _northgate_spec(), TreeReadingReviewer())
    assert rec["verdict"] == "not_met" and rec["stack"]["missing"] == ["go"]
    assert "Required stack 'go' has no manifest" in ag.findings_summary(rec)


def test_a_missing_rulebook_fails_acceptance_and_lean_opens_a_labelled_draft_with_a_follow_up(
        tmp_path):
    root = _northgate_tree(tmp_path, rulebook=False)
    rec = _check(root, _northgate_spec(), TreeReadingReviewer())
    assert rec["verdict"] == "not_met"
    missing = [i["text"] for i in rec["items"] if i["status"] == ag.MISSING]
    assert RULEBOOK in missing
    gates = {"code_written": True, "written_tests_pass": True, ag.GATE: ag.passed(rec)}
    failing = [k for k, v in gates.items() if not v]
    assert failing == [ag.GATE]

    lean = resolve_profile("lean")
    assert lean["advisory_gate"] is True and ag.SEAT in lean["panel_checks"]
    # Under lean the seat sits — the fix lane included (a follow-up is usually a fix-lane run).
    for lane in ("full", fix_lane_triage.LANE_FIX):
        assert ag.SEAT in g.review_panel_seats(lane, 1, lean, ci_files=False)
    evidence = {ag.KEY: rec, ag.SUMMARY_KEY: ag.findings_summary(rec)}
    faults = g.classify_gate_faults(failing=failing, evidence=evidence)
    assert faults == {ag.GATE: "WORK"}
    # A fact, not an opinion: the advisory profile repairs it first…
    kw = dict(failing=failing, advisory_gate=True, max_repair_rounds=2, open_pr=True,
              faults=faults, fix_round_ran=True)
    assert g.resolve_gate_failure_action(rounds_used=0, never_block=True, **kw) == "repair"
    # …and past the repair rounds the PR opens with its checks failing — never a clean ship.
    assert g.resolve_gate_failure_action(rounds_used=2, never_block=True, **kw) \
        == "ship_with_dissent"
    assert g.resolve_gate_failure_action(rounds_used=2, never_block=False,
                                         open_pr_when_failing=True, **kw) == cf.ACTION
    # Both terminals record the checks-failing record: a draft labelled PR, its section and
    # the follow-up's findings carry the missing rulebook verbatim.
    record = cf.collect(failing, evidence, details={ag.GATE: ag.headline(rec)})
    assert cf.gate_names(record) == [ag.GATE]
    assert any(RULEBOOK in f["text"] for f in record["findings"])
    section = cf.pr_section(record)
    assert "## Checks failing" in section and "Must-haves met" in section
    follow = cf.followup_findings(record)
    assert any(RULEBOOK in f["text"] for f in follow)
    assert "| MISSING |" in ag.pr_section(rec)


def test_the_full_tree_passes_and_partials_become_follow_ups(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True)
    rec = _check(root, _northgate_spec(), TreeReadingReviewer())
    assert rec["verdict"] == "met" and ag.passed(rec), rec["missing"]
    reply = "ITEM 1: MET — cmd/northgate/main.go\nITEM 2: PARTIAL — cmd/gwlint/main.go has 12 rules"
    items = ag.acceptance_items(_northgate_spec())[:2]
    rec2 = ag.judge(items, reply, {})
    assert rec2["verdict"] == "met" and rec2["partial"] == [2]
    assert ag.partial_followups(rec2)[0]["severity"] == "major"


def test_a_met_without_file_evidence_is_partial_and_a_silent_reviewer_is_not_a_pass():
    items = ag.acceptance_items(_northgate_spec())[:2]
    rec = ag.judge(items, "ITEM 1: MET — it is all there\nITEM 2: MET — see main.go", {})
    assert [i["status"] for i in rec["items"]] == [ag.PARTIAL, ag.MET]
    silent = ag.judge(items, "Looks good to me.", {})
    assert silent["verdict"] == "not_run" and not ag.passed(silent)
    faults = g.classify_gate_faults(failing=[ag.GATE], evidence={ag.KEY: silent})
    assert faults == {ag.GATE: "HARNESS"}


def test_the_scripted_conformance_engine_plays_the_acceptance_seat(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True)
    spec = _northgate_spec()
    ok = _check(root, spec, ScriptedEngine(faults=set(), caps=set()))
    assert ok["verdict"] == "met", ok
    bad = _check(root, spec, ScriptedEngine(faults={"acceptance_missing"}, caps=set()))
    assert bad["verdict"] == "not_met" and bad["missing"] == [1]


def test_the_gate_is_a_hard_floor_fact_and_convergeable():
    assert ag.GATE in g._ADVISORY_HARD_FLOOR
    assert ag.GATE in g._CONVERGEABLE
    assert ag.SEAT in g._ALL_PANEL_SEATS
    assert cf.SEAT_OF_GATE[ag.GATE] == (ag.SEAT, ag.SUMMARY_KEY)


def test_an_advisory_ship_queues_its_major_findings_not_only_the_minor_ones():
    review = ("FINDING: MAJOR internal/auth/jwt.go:14 — JWKS refresh failure falls back to "
              "accepting unverified tokens\nFINDING: MINOR README.md:3 — typo\nVERDICT: REFUTED")
    deferred = [{"severity": "minor", "text": "README.md:3 — typo"}]
    out = g.advisory_followup_findings(["code_review_passed"], {"review_summary": review},
                                       deferred)
    texts = " | ".join(f["text"] for f in out)
    assert "JWKS refresh failure" in texts
    assert sum("typo" in f["text"] for f in out) == 1


# ── the merge card speaks for the whole lineage ────────────────────────────────────────────

def _run(c, project_id, *, fctx, status="completed", pr=None, state=None, spec=None):
    from sqlalchemy import text

    rid = f"crun_{uuid.uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, state, finalize_ctx, "
        "pull_request_url) VALUES (:i, :p, :s, 'Northgate', CAST(:sp AS JSONB), "
        "CAST(:st AS JSONB), CAST(:f AS JSONB), :u)"),
        {"i": rid, "p": project_id, "s": status, "sp": json.dumps(spec or {}),
         "st": json.dumps(state or {}), "f": json.dumps(fctx), "u": pr})
    return rid


def test_a_clean_follow_up_on_a_pr_whose_parent_shipped_failing_is_never_passed(eng, project):
    from sqlalchemy import text

    missing = ag.judge(ag.acceptance_items(_northgate_spec())[:2],
                       "ITEM 1: MET — cmd/northgate/main.go\nITEM 2: MISSING — no rulebook", {})
    with eng.begin() as c:
        root = _run(c, project, pr="https://g.test/a/b/pull/7", spec=_northgate_spec(),
                    state={ag.KEY: missing},
                    fctx={"advisory_failing_gates": ["code_review_passed", "consistent"]})
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
        # A repair attempt that never shipped is not counted.
        _run(c, project, status="repairing", fctx={"root_run_id": root,
                                                   "advisory_failing_gates": ["app_serves"]})
        follow = _run(c, project, status="running",
                      fctx={"root_run_id": root, "is_followup": True})
    try:
        with eng.begin() as c:
            # The follow-up's own checks are clean (no checks_failing of its own).
            card, acceptance = ag.merge_card_checks(c, run_id=follow, evidence={},
                                                    checks_failing=None)
        assert card is not None, "a clean follow-up must not read as passed"
        names = cf.gate_names(card)
        assert set(names) == {"code_review_passed", "consistent", ag.GATE}
        assert "app_serves" not in names
        assert all(root in (gate.get("detail") or "") for gate in card["gates"])
        assert any("no rulebook" in f["text"] for f in card["findings"])
        assert acceptance["verdict"] == "not_met"
        assert cf.decision_summary(card).startswith("CHECKS FAILING")

        # The follow-up's own acceptance check meets the root's must-haves: that gate clears,
        # the parent's refuted review does not (a follow-up re-judges only its own delta).
        met = ag.judge(ag.acceptance_items(_northgate_spec())[:2],
                       "ITEM 1: MET — cmd/northgate/main.go\nITEM 2: MET — "
                       "config/api-guidelines.yaml", {})
        with eng.begin() as c:
            card2, acc2 = ag.merge_card_checks(c, run_id=follow, evidence={ag.KEY: met},
                                               checks_failing=None)
        assert set(cf.gate_names(card2)) == {"code_review_passed", "consistent"}
        assert acc2["verdict"] == "met"
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})


def test_a_lineage_with_nothing_failing_keeps_a_plain_card(eng, project):
    from sqlalchemy import text

    with eng.begin() as c:
        root = _run(c, project, pr="https://g.test/a/b/pull/8", fctx={})
    try:
        with eng.begin() as c:
            card, _ = ag.merge_card_checks(c, run_id=root, evidence={}, checks_failing=None)
        assert card is None
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})
