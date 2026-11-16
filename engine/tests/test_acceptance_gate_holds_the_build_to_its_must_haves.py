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

import pytest

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


def _northgate_spec(*, stack_line: bool = True) -> dict:
    directive = add_project.render_brief(_brief(), "Build the Northgate demo.")
    if stack_line:
        directive += "\n\nStack: go."       # what the add-project start appends for a pick
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
        "README.md": "# Northgate\n",
        "docs/design.md": "design\n",
    }
    if go_mod:
        files["go.mod"] = "module northgate\n\ngo 1.22\n"
    if rulebook:
        files["config/api-guidelines.yaml"] = "rules:\n" + "".join(
            f"  - id: R{i}\n" for i in range(20))
        # The rulebook is WIRED: gwlint's loader reads it (BOSSAPP-1233's floor asks for one).
        files["internal/lint/rulebook.go"] = (
            "package lint\n\nfunc LoadRulebook() ([]Rule, error) {\n"
            "\treturn parse(\"config/api-guidelines.yaml\")\n}\n")
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
        for n, tags, text in re.findall(r"(?m)^ITEM (\d+) \[([^\]]+)\]: (.*)$", prompt):
            named = re.findall(r"[\w./<>-]+\.(?:ya?ml|go)\b", text)
            missing = [f for f in named if "<" not in f and not list(root.rglob(Path(f).name))]
            if missing:
                lines.append(f"ITEM {n}: MISSING — looked for {', '.join(missing)}; not in the tree")
            else:
                loader = ("; LOADED BY internal/lint/rulebook.go:LoadRulebook"
                          if "must be loaded" in tags else "")
                lines.append(f"ITEM {n}: MET — cmd/northgate/main.go, cmd/gwlint/main.go{loader}")
        verdict = "NOT MET" if any("MISSING" in ln for ln in lines) else "MET"
        return {"summary": "\n".join(lines) + f"\nACCEPTANCE: {verdict}", "cost_usd_micros": 0}


class Says:
    """A reviewer that says exactly `reply` (the reviewer's concrete inputs, scripted)."""

    def __init__(self, reply: str):
        self.reply = reply

    def run_role(self, prompt, cwd, *, read_only=False, **_):
        return {"summary": self.reply, "cost_usd_micros": 0}


def _check(root: Path, spec: dict, reviewer, *, source=None) -> dict:
    """What the pipeline's acceptance seat does (governed_code_task_service._run_acceptance_check)."""
    items = ag.acceptance_items(spec)
    stack = ag.stack_manifest_check(root, spec)
    reply = reviewer.run_role(ag.acceptance_prompt(items, title="Northgate", stack=stack,
                                                   tree=ag.tree_listing(root)),
                              root, read_only=True)
    return ag.judge(items, reply["summary"], stack, root=root, source=source,
                    dropped=ag.items_dropped(spec))


def _all_met(n: int, cite: str = "cmd/northgate/main.go", loader: str = "") -> str:
    return "\n".join(f"ITEM {i}: MET — {cite}" + (f"; LOADED BY {loader}" if loader else "")
                     for i in range(1, n + 1)) + "\nACCEPTANCE: MET"


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
    # Room is reserved for what the start appends (the Stack: line), so it is never cut.
    reserved = add_project.render_brief(brief, "words " * 4000, reserve=500)
    assert len(reserved) + 500 <= add_project.MAX_DIRECTIVE


def test_the_new_work_directive_keeps_the_brief_whole():
    from boss_engine.services.project_experience import understanding

    done = ["x" * 1500]
    out = understanding.directive("words " * 5000, {"title": "T", "done_when": done})
    assert done[0] in out and len(out) <= add_project.MAX_DIRECTIVE


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
    assert not any("mTLS" in c for c in out["acceptance_criteria"])
    assert prd_acceptance_inputs({"acceptance_criteria": ["a"]}, "# PRD\n\n## Goals\n- g\n") == {
        "acceptance_criteria": ["a"]}


class _Rows:
    """A connection whose lineage holds `rows` [(id, spec, fctx)] (the build-rows query only)."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql, params):
        root = params["r"]
        hits = [(r[0], json.dumps(r[1])) for r in self.rows
                if (r[0] == root or r[2].get("root_run_id") == root)
                and not r[2].get("is_followup") and not r[2].get("deferred_from_run")]
        return type("R", (), {"all": lambda self: hits,
                              "first": lambda self: hits[0] if hits else None})()


def test_a_follow_up_inherits_its_roots_items():
    own = {"instructions": "Fix the two MINOR findings", "acceptance_criteria": ["Fixed: a"]}
    conn = _Rows([("crun_root", _northgate_spec(), {})])
    spec, source = ag.items_spec(conn, run_id="crun_f", own_spec=own,
                                 fctx={"is_followup": True, "root_run_id": "crun_root"})
    texts = [i["text"] for i in ag.acceptance_items(spec)]
    assert LONG_MUST_HAVE in texts and RULEBOOK in texts
    assert source["run_id"] == "crun_root" and source["root"] is True
    # A root run (or a repair child, whose spec is its parent's) is held to its own.
    assert ag.items_spec(conn, run_id="crun_r", own_spec=own, fctx={})[0] == own


def test_a_web_app_lineage_without_a_root_row_resolves_the_original_build():
    """start_web_app_build / prd_decomposition mint a root id with no code_task_runs row: the
    original build is the earliest non-follow-up run of the lineage, never the follow-up."""
    own = {"instructions": "Fix", "acceptance_criteria": ["Fixed: [MAJOR] x"]}
    conn = _Rows([("crun_build1", _northgate_spec(), {"root_run_id": "wapp_root"}),
                  ("crun_f", own, {"root_run_id": "wapp_root", "is_followup": True})])
    spec, source = ag.items_spec(conn, run_id="crun_f", own_spec=own,
                                 fctx={"is_followup": True, "root_run_id": "wapp_root"})
    assert source["run_id"] == "crun_build1"
    assert RULEBOOK in [i["text"] for i in ag.acceptance_items(spec)]


def test_a_follow_up_is_held_to_the_union_of_every_slice_of_a_decomposed_prd():
    """prd_decomposition starts slices 2..N as non-follow-ups sharing the root id, each with
    its own items: a follow-up is held to all of them, not slice 1's alone."""
    s1 = {"acceptance_criteria": ["slice one: the gateway routes"]}
    s2 = {"acceptance_criteria": ["slice two: gwlint has 20 rules"]}
    s3 = {"acceptance_criteria": ["slice three: the demo README"]}
    conn = _Rows([("crun_s1", s1, {"root_run_id": "wapp_r"}),
                  ("crun_s2", s2, {"root_run_id": "wapp_r", "decomposition_index": 2}),
                  ("crun_s3", s3, {"root_run_id": "wapp_r", "decomposition_index": 3}),
                  ("crun_f", {"acceptance_criteria": ["Fixed: x"]},
                   {"root_run_id": "wapp_r", "is_followup": True})])
    spec, source = ag.items_spec(conn, run_id="crun_f", own_spec={},
                                 fctx={"is_followup": True, "root_run_id": "wapp_r"})
    texts = [i["text"] for i in ag.acceptance_items(spec)]
    assert texts == ["slice one: the gateway routes", "slice two: gwlint has 20 rules",
                     "slice three: the demo README"]
    assert source["builds"] == ["crun_s1", "crun_s2", "crun_s3"]


def test_a_follow_up_of_an_item_less_build_is_no_items_not_a_failure(tmp_path):
    conn = _Rows([("crun_root", {"instructions": "tidy the logs"}, {})])
    spec, source = ag.items_spec(conn, run_id="crun_f", own_spec={},
                                 fctx={"is_followup": True, "root_run_id": "crun_root"})
    assert source["root"] is True and ag.acceptance_items(spec) == []
    rec = ag.judge([], None, {}, root=tmp_path, source=source)
    assert rec["verdict"] == "no_items" and ag.passed(rec)


def test_a_follow_up_whose_root_cannot_be_found_is_not_run_never_its_own_items(tmp_path):
    own = {"instructions": "Fix", "acceptance_criteria": ["Fixed: [MAJOR] x"]}
    spec, source = ag.items_spec(_Rows([]), run_id="crun_f", own_spec=own,
                                 fctx={"is_followup": True, "root_run_id": "wapp_gone"})
    assert spec == {} and source["root"] is False
    rec = ag.judge([], None, {}, root=tmp_path, source=source)
    assert rec["verdict"] == "not_run" and not ag.passed(rec)


# ── RCA-E: the gate fails closed ───────────────────────────────────────────────────────────

def test_the_items_are_the_must_haves_the_prd_gating_list_and_the_criteria():
    items = ag.acceptance_items(_northgate_spec())
    sources = [i["source"] for i in items]
    assert sources[:3] == ["must_have"] * 3
    assert sources.count("prd_gating") == 4
    assert all(len(i["text"]) <= ag.MAX_ITEM_CHARS for i in items)


def test_items_over_the_total_cap_are_said_and_never_met(tmp_path):
    n = ag.MAX_TOTAL_ITEMS
    spec = {"acceptance_criteria": [f"criterion {i}" for i in range(n + 5)]}
    assert len(ag.acceptance_items(spec)) == n and ag.items_dropped(spec) == 5
    (tmp_path / "a.go").write_text("package a\n")
    rec = ag.judge(ag.acceptance_items(spec), _all_met(n, "a.go"), {}, root=tmp_path, dropped=5)
    assert rec["verdict"] == "not_run" and not ag.passed(rec)
    assert "5 further items" in ag.must_have_table(rec) and "5 not checked" in ag.headline(rec)
    assert "Automated checks: passed" not in ag.card_words(None, rec, "passed")


class BatchReviewer:
    """Answers every ITEM its prompt lists MET (code evidence), except the numbers in `fail`;
    `broken_pass` makes that pass (0-based) raise. Counts its passes."""

    def __init__(self, fail=(), broken_pass=None):
        self.fail, self.broken_pass, self.passes, self.sizes = set(fail), broken_pass, 0, []

    def __call__(self, prompt):
        import re

        nums = [int(n) for n in re.findall(r"(?m)^ITEM (\d+) \[", prompt)]
        self.sizes.append(len(nums))
        self.passes += 1
        if self.broken_pass == self.passes - 1:
            raise RuntimeError("the engine fell over")
        lines = [f"ITEM {n}: {'MISSING — absent' if n in self.fail else 'MET — a.go'}"
                 for n in nums]
        verdict = "NOT MET" if self.fail & set(nums) else "MET"
        return {"summary": "\n".join(lines) + f"\nACCEPTANCE: {verdict}", "cost_usd_micros": 1}


def _sixty_items_across_four_slices():
    builds = [{"id": f"crun_s{k}", "spec": {"acceptance_criteria": [
        f"slice {k} criterion {i}" for i in range(15)]}} for k in range(1, 5)]
    return ag.acceptance_items(ag.union_spec(builds))


def test_sixty_items_across_four_slices_are_all_judged_in_passes(tmp_path):
    (tmp_path / "a.go").write_text("package a\n")
    items = _sixty_items_across_four_slices()
    assert len(items) == 60 and [i["n"] for i in items] == list(range(1, 61))
    ask = BatchReviewer()
    rec, cost = ag.run_check(items, ask=ask, title="t", stack={}, root=tmp_path)
    assert ask.sizes == [ag.MAX_ITEMS, 20] and cost == 2
    assert rec["verdict"] == "met" and rec["counts"]["met"] == 60 and ag.passed(rec)
    assert ag.card_words(None, rec, "passed") == "Automated checks: passed"


def test_one_unmet_item_in_the_last_pass_fails_the_sixty(tmp_path):
    (tmp_path / "a.go").write_text("package a\n")
    rec, _ = ag.run_check(_sixty_items_across_four_slices(), ask=BatchReviewer(fail={55}),
                          title="t", stack={}, root=tmp_path)
    assert rec["verdict"] == "not_met" and rec["missing"] == [55]


def test_a_pass_that_cannot_run_leaves_its_items_unjudged_never_met(tmp_path):
    (tmp_path / "a.go").write_text("package a\n")
    rec, _ = ag.run_check(_sixty_items_across_four_slices(), ask=BatchReviewer(broken_pass=1),
                          title="t", stack={}, root=tmp_path)
    assert rec["verdict"] == "not_run" and len(rec["unjudged"]) == 20 and not ag.passed(rec)
    words = ag.card_words(None, rec, "passed")
    assert "Automated checks: passed" not in words and "20 not judged" in words


def test_only_the_first_forty_judged_is_never_met(tmp_path):
    """The reviewer's case: 4 slices x 15 criteria, only 40 judged — not met, not passed."""
    (tmp_path / "a.go").write_text("package a\n")
    items = _sixty_items_across_four_slices()
    rec = ag.judge(items, _all_met(ag.MAX_ITEMS, "a.go"), {}, root=tmp_path)
    assert rec["verdict"] == "not_run" and len(rec["unjudged"]) == 20
    assert "Automated checks: passed" not in ag.card_words(None, rec, "passed")


def test_fifteen_items_with_one_answered_is_never_met(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True)
    spec = {"acceptance_criteria": [f"criterion {i}" for i in range(15)]}
    rec = _check(root, spec, Says("ITEM 1: MET — cmd/northgate/main.go\nACCEPTANCE: MET"))
    assert rec["verdict"] == "not_run" and len(rec["unjudged"]) == 14 and not ag.passed(rec)
    assert g.classify_gate_faults(failing=[ag.GATE], evidence={ag.KEY: rec}) == {ag.GATE: "HARNESS"}


def test_all_partial_fails_the_gate(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True)
    spec = {"acceptance_criteria": ["a", "b", "c"]}
    reply = "\n".join(f"ITEM {i}: PARTIAL — cmd/gwlint/main.go has half" for i in (1, 2, 3))
    rec = _check(root, spec, Says(reply + "\nACCEPTANCE: NOT MET"))
    assert rec["verdict"] == "not_met" and rec["partial"] == [1, 2, 3] and not ag.passed(rec)
    assert "only PARTIAL" in ag.findings_summary(rec)


def test_the_summary_line_is_read_and_a_slip_re_asks_the_reviewer(tmp_path):
    """A missing or contradicting summary line is the REVIEWER's slip: not_run (HARNESS, the
    check is asked again), never a pass and never a coder repair round."""
    root = _northgate_tree(tmp_path, rulebook=True)
    spec = {"acceptance_criteria": ["a", "b"]}
    said_not_met = _all_met(2).replace("ACCEPTANCE: MET", "ACCEPTANCE: NOT MET")
    no_line = _all_met(2).replace("\nACCEPTANCE: MET", "")
    for reply in (said_not_met, no_line):
        rec = _check(root, spec, Says(reply))
        assert rec["verdict"] == "not_run" and not ag.passed(rec)
        assert g.classify_gate_faults(failing=[ag.GATE], evidence={ag.KEY: rec}) == {
            ag.GATE: "HARNESS"}
    assert _check(root, spec, Says(_all_met(2)))["verdict"] == "met"


@pytest.mark.parametrize("cite,why", [
    ("README.md", "only documents"),
    ("docs/design.md", "only documents"),
    ("cmd/northgate/handler_does_not_exist.go", "not in the tree"),
    ("it is all there", "no file evidence"),
    ("go.mod", "only manifests/lockfiles"),
    ("go.mod and config/../go.mod", "only manifests/lockfiles"),
])
def test_met_needs_code_evidence_that_exists_in_the_tree(tmp_path, cite, why):
    root = _northgate_tree(tmp_path, rulebook=True)
    rec = _check(root, {"acceptance_criteria": ["a"]}, Says(_all_met(1, cite)))
    assert rec["items"][0]["status"] == ag.PARTIAL and why in rec["items"][0]["evidence"]
    assert rec["verdict"] == "not_met"


def test_a_suffix_of_a_real_path_is_evidence(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True)
    rec = _check(root, {"acceptance_criteria": ["a"]}, Says(_all_met(1, "gwlint/main.go:12")))
    assert rec["verdict"] == "met", rec["items"]


@pytest.mark.parametrize("words", [
    "Alert hub\n\nMust-haves:\n- React to alerts within a minute",
    "Rules engine\n\nMust-haves:\n- Express the rules as YAML",
    "Port the CLI from Python to Rust",
])
def test_words_never_hard_fail_on_a_stack_they_do_not_declare(tmp_path, words):
    (tmp_path / "main.go").write_text("package main\n")
    stack = ag.stack_manifest_check(tmp_path, {"instructions": words})
    assert stack["missing"] == [], stack       # soft at most: a note, never a failure
    assert all(not r["hard"] for r in stack["required"])


def test_a_declared_stack_without_its_manifest_fails(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True, go_mod=False)
    spec = _northgate_spec()
    stack = ag.stack_manifest_check(root, spec)
    assert stack["missing"] == ["go"]
    rec = _check(root, spec, TreeReadingReviewer())
    assert rec["verdict"] == "not_met"
    assert "Required stack 'go' has no manifest" in ag.findings_summary(rec)
    # The structured field (RCA-A) is hard too.
    assert ag.required_stack({"required_stack": ["go"]})[0]["hard"] is True
    # Without the Stack: line, the brief's "(Go)" is a soft note.
    soft = ag.stack_manifest_check(root, _northgate_spec(stack_line=False))
    assert soft["missing"] == [] and soft["notes"]


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
    assert lean["advisory_gate"] is True
    for lane in ("full", fix_lane_triage.LANE_FIX):
        assert ag.SEAT in g.review_panel_seats(lane, 1, lean, ci_files=False)
    evidence = {ag.KEY: rec, ag.SUMMARY_KEY: ag.findings_summary(rec)}
    faults = g.classify_gate_faults(failing=failing, evidence=evidence)
    assert faults == {ag.GATE: "WORK"}
    kw = dict(failing=failing, advisory_gate=True, max_repair_rounds=2, open_pr=True,
              faults=faults, fix_round_ran=True)
    assert g.resolve_gate_failure_action(rounds_used=0, never_block=True, **kw) == "repair"
    assert g.resolve_gate_failure_action(rounds_used=2, never_block=True, **kw) \
        == "ship_with_dissent"
    assert g.resolve_gate_failure_action(rounds_used=2, never_block=False,
                                         open_pr_when_failing=True, **kw) == cf.ACTION
    record = cf.collect(failing, evidence, details={ag.GATE: ag.headline(rec)})
    assert cf.gate_names(record) == [ag.GATE]
    assert any(RULEBOOK in f["text"] for f in record["findings"])
    section = cf.pr_section(record)
    assert "## Checks failing" in section and "Must-haves met" in section
    assert any(RULEBOOK in f["text"] for f in cf.followup_findings(record))
    assert "| MISSING |" in ag.pr_section(rec)


def test_the_full_tree_passes(tmp_path):
    root = _northgate_tree(tmp_path, rulebook=True)
    rec = _check(root, _northgate_spec(), TreeReadingReviewer())
    assert rec["verdict"] == "met" and ag.passed(rec), rec["items"]


@pytest.mark.parametrize("profile", [{}, {"panel_checks": ["regression"]},
                                     {"panel_checks": ["review_code"]}])
def test_the_seat_always_sits_whatever_a_custom_profile_lists(profile):
    for tier in (0, 1, 2):
        assert ag.SEAT in g.review_panel_seats("full", tier, profile, ci_files=False)


def test_the_scripted_conformance_engine_plays_the_acceptance_seat(tmp_path):
    import shutil

    from boss_engine.services import scripted_engine as se

    shutil.copytree(se._DEFAULT_FIXTURE, tmp_path, dirs_exist_ok=True)  # noqa: SLF001
    spec = {"acceptance_criteria": ["slug lowercases and joins words with hyphens",
                                    "GET /?q= answers the slug as JSON"]}
    eng_ = ScriptedEngine(faults={"acceptance_missing"}, caps=set(), app_dir="")
    bad = _check(tmp_path, spec, eng_)
    assert bad["verdict"] == "not_met" and bad["missing"] == [1]
    # Still missing until a fix turn DELIVERS it (writes the marker), not until the fault expires.
    assert _check(tmp_path, spec, eng_)["missing"] == [1]
    (tmp_path / se.ACCEPTANCE_MARKER).write_text("export const delivered = true;\n")
    assert _check(tmp_path, spec, eng_)["verdict"] == "met"
    assert _check(tmp_path, spec, ScriptedEngine(faults=set(), caps=set(),
                                                 app_dir=""))["verdict"] == "met"


def test_the_gate_is_a_hard_floor_fact_and_convergeable():
    assert ag.GATE in g._ADVISORY_HARD_FLOOR
    assert ag.GATE in g._CONVERGEABLE
    assert ag.SEAT in g._ALL_PANEL_SEATS
    assert cf.SEAT_OF_GATE[ag.GATE] == (ag.SEAT, ag.SUMMARY_KEY)


def test_the_acceptance_seat_is_a_registered_reasoning_stage():
    from boss_engine.services import code_agent, token_efficiency

    assert ag.SEAT in code_agent.REASONING_STAGES and ag.SEAT in code_agent.STAGE_THINKING_CAPS
    assert ag.SEAT in token_efficiency.KNOWN_STAGES


def test_an_unanswered_reviewer_is_asked_again_before_anything_ships():
    import inspect

    src = inspect.getsource(g)
    block = src[src.index('evidence["review_escalated_turns"] = escalated'):]
    block = block[:block.index("gate_faults = classify_gate_faults")]
    assert "_run_acceptance_check(max_turns=escalated)" in block


def test_an_advisory_ship_queues_its_major_findings_not_only_the_minor_ones():
    review = ("FINDING: MAJOR internal/auth/jwt.go:14 — JWKS refresh failure falls back to "
              "accepting unverified tokens\nFINDING: MINOR README.md:3 — typo\nVERDICT: REFUTED")
    deferred = [{"severity": "minor", "text": "README.md:3 — typo"}]
    out = g.advisory_followup_findings(["code_review_passed"], {"review_summary": review},
                                       deferred)
    texts = " | ".join(f["text"] for f in out)
    assert "JWKS refresh failure" in texts
    assert sum("typo" in f["text"] for f in out) == 1


@pytest.mark.parametrize("acc,expect", [
    (None, "Must-haves: not checked"),
    ({"verdict": "not_met", "items": [{"n": 1, "status": "partial"}],
      "counts": {"met": 0, "partial": 1, "missing": 0}}, "1 partial"),
    ({"verdict": "met", "items": []}, "Automated checks: passed"),
])
def test_the_card_never_says_passed_unless_the_must_haves_were_met(acc, expect):
    words = ag.card_words(None, acc, "passed")
    assert expect in words
    if acc is None or acc["verdict"] != "met":
        assert "Automated checks: passed" not in words


# ── the merge card speaks for the whole pull request ───────────────────────────────────────

PR = "https://g.test/a/b/pull/7"


def _run(c, project_id, *, fctx, status="completed", pr=None, state=None, spec=None,
         passed=None):
    from sqlalchemy import text

    rid = f"crun_{uuid.uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, state, finalize_ctx, "
        "pull_request_url, created_at) VALUES (:i, :p, :s, 'Northgate', CAST(:sp AS JSONB), "
        "CAST(:st AS JSONB), CAST(:f AS JSONB), :u, clock_timestamp())"),
        {"i": rid, "p": project_id, "s": status, "sp": json.dumps(spec or {}),
         "st": json.dumps(state or {}), "f": json.dumps(fctx), "u": pr})
    if passed is not None:
        c.execute(text(
            "INSERT INTO quality_gates (id, project_id, code_task_run_id, risk_tier, status, "
            "fail_closed, gates_passed, gates_failed, evidence) VALUES (:q, :p, :r, 1, 'pass', "
            "true, CAST(:gp AS JSONB), '[]'::jsonb, '{}'::jsonb)"),
            {"q": f"qg_{uuid.uuid4().hex[:12]}", "p": project_id, "r": rid,
             "gp": json.dumps(passed)})
    return rid


def _cleanup(eng, project):
    from sqlalchemy import text

    with eng.begin() as c:
        c.execute(text("DELETE FROM quality_gates WHERE project_id = :p"), {"p": project})
        c.execute(text("DELETE FROM code_task_runs WHERE project_id = :p"), {"p": project})


#: Both root items met; the rulebook item (2) names its loader, as the floor requires.
ROOT_MET = ("ITEM 1: MET — cmd/northgate/main.go\nITEM 2: MET — cmd/gwlint/main.go; LOADED BY "
            "internal/lint/rulebook.go:LoadRulebook\nACCEPTANCE: MET")


def _rec(reply: str, root: Path, n: int = 2) -> dict:
    return ag.judge(ag.acceptance_items(_northgate_spec())[:n], reply, {}, root=root)


def test_a_clean_follow_up_on_a_pr_whose_parent_shipped_failing_is_never_passed(
        eng, project, tmp_path):
    from sqlalchemy import text

    tree = _northgate_tree(tmp_path, rulebook=True)
    missing = _rec("ITEM 1: MET — cmd/northgate/main.go\nITEM 2: MISSING — no rulebook\n"
                   "ACCEPTANCE: NOT MET", tree)
    with eng.begin() as c:
        root = _run(c, project, pr=PR, spec=_northgate_spec(), state={ag.KEY: missing},
                    fctx={"advisory_failing_gates": ["code_review_passed", "consistent",
                                                     "nothing_broke"]})
        c.execute(text("UPDATE code_task_runs SET finalize_ctx = finalize_ctx || "
                       "jsonb_build_object('root_run_id', id) WHERE id = :i"), {"i": root})
        _run(c, project, status="repairing", fctx={"root_run_id": root,
                                                   "advisory_failing_gates": ["app_serves"]})
        # An earlier, merged PR of the same lineage is not this PR.
        _run(c, project, pr="https://g.test/a/b/pull/3",
             fctx={"root_run_id": root, "advisory_failing_gates": ["security_clean"]})
        # A later build of THIS PR passed the whole suite: nothing_broke clears.
        _run(c, project, pr=PR, fctx={"root_run_id": root}, passed=["nothing_broke"])
        follow = _run(c, project, status="running",
                      fctx={"root_run_id": root, "is_followup": True})
    try:
        with eng.begin() as c:
            card, acceptance = ag.merge_card_checks(c, run_id=follow, evidence={},
                                                    checks_failing=None, pr_url=PR)
        assert card is not None, "a clean follow-up must not read as passed"
        assert set(cf.gate_names(card)) == {"code_review_passed", "consistent", ag.GATE}
        assert all(root in (gate.get("detail") or "") for gate in card["gates"])
        assert any("no rulebook" in f["text"] for f in card["findings"])
        assert acceptance["verdict"] == "not_met"
        assert ag.card_words(card, acceptance, "passed").startswith("Automated checks: failing")

        # A follow-up's met against its OWN items clears nothing.
        _o = _rec(ROOT_MET, tree)
        own_met = {**_o, "source": {**_o["source"], "run_id": follow, "root": False}}
        with eng.begin() as c:
            card1, _ = ag.merge_card_checks(c, run_id=follow, evidence={ag.KEY: own_met},
                                            checks_failing=None, pr_url=PR)
        assert ag.GATE in cf.gate_names(card1)
        # Met against the ROOT's items clears acceptance; the refuted review does not clear.
        _m = _rec(ROOT_MET, tree)
        root_met = {**_m, "source": {**_m["source"], "run_id": root, "root": True}}
        assert root_met["verdict"] == "met"
        with eng.begin() as c:
            card2, acc2 = ag.merge_card_checks(c, run_id=follow, evidence={ag.KEY: root_met},
                                               checks_failing=None, pr_url=PR)
        assert set(cf.gate_names(card2)) == {"code_review_passed", "consistent"}
        assert acc2["verdict"] == "met"
    finally:
        _cleanup(eng, project)


def test_a_lineage_never_checked_says_so_and_nothing_failing_keeps_a_plain_card(eng, project):
    with eng.begin() as c:
        root = _run(c, project, pr="https://g.test/a/b/pull/8", fctx={})
    try:
        with eng.begin() as c:
            card, acc = ag.merge_card_checks(c, run_id=root, evidence={}, checks_failing=None,
                                             pr_url="https://g.test/a/b/pull/8")
        assert card is None and acc is None
        assert "not checked" in ag.card_words(card, acc, "passed")
    finally:
        _cleanup(eng, project)


def test_slice_three_meeting_its_items_never_clears_slice_twos_gaps(eng, project, tmp_path):
    """A decomposed PRD: slice 2's check failed, slice 3's check met ITS OWN items. The shared
    PR's card keeps slice 2's gaps; only a later check covering slice 2's items clears them."""
    tree = _northgate_tree(tmp_path, rulebook=True)
    items2 = ag.acceptance_items({"acceptance_criteria": ["slice two: gwlint has 20 rules"]})
    items3 = ag.acceptance_items({"acceptance_criteria": ["slice three: the demo README"]})
    s2_bad = ag.judge(items2, "ITEM 1: MISSING — no rulebook\nACCEPTANCE: NOT MET", {},
                      root=tree)
    s3_met = ag.judge(items3, _all_met(1), {}, root=tree)
    assert s2_bad["verdict"] == "not_met" and s3_met["verdict"] == "met"
    with eng.begin() as c:
        s1 = _run(c, project, pr=PR, fctx={})
        _run(c, project, pr=PR, fctx={"root_run_id": s1, "decomposition_index": 2},
             state={ag.KEY: s2_bad})
        s3 = _run(c, project, pr=PR, fctx={"root_run_id": s1, "decomposition_index": 3},
                  state={ag.KEY: s3_met})
        follow = _run(c, project, status="running",
                      fctx={"root_run_id": s1, "is_followup": True})
    try:
        with eng.begin() as c:
            card, acc = ag.merge_card_checks(c, run_id=s3, evidence={ag.KEY: s3_met},
                                             checks_failing=None, pr_url=PR)
        assert ag.GATE in cf.gate_names(card), "slice 3's met must not clear slice 2's gaps"
        assert acc["missing"] == [1]
        # A follow-up held to the union (slice 2 + 3 items) that meets them clears it.
        union = ag.judge(items2 + [{**items3[0], "n": 2}], _all_met(2), {}, root=tree)
        with eng.begin() as c:
            card2, _ = ag.merge_card_checks(c, run_id=follow, evidence={ag.KEY: union},
                                            checks_failing=None, pr_url=PR)
        assert card2 is None or ag.GATE not in cf.gate_names(card2)
    finally:
        _cleanup(eng, project)


def test_a_follow_up_that_no_check_saw_does_not_inherit_an_earlier_met(eng, project, tmp_path):
    tree = _northgate_tree(tmp_path, rulebook=True)
    met = ag.judge(ag.acceptance_items({"acceptance_criteria": ["a"]}), _all_met(1), {},
                   root=tree)
    with eng.begin() as c:
        root = _run(c, project, pr=PR, fctx={}, state={ag.KEY: met})
        follow = _run(c, project, status="running",
                      fctx={"root_run_id": root, "is_followup": True})
    try:
        with eng.begin() as c:
            card, acc = ag.merge_card_checks(c, run_id=follow, evidence={},
                                             checks_failing=None, pr_url=PR)
        assert acc["verdict"] == "not_checked" and root in acc["why"]
        words = ag.card_words(card, acc, "passed")
        assert f"not checked since {root}" in words and "Automated checks: passed" not in words
    finally:
        _cleanup(eng, project)


# ---------------------------------------------------------------------------------------------
# BOSSAPP-1233 review: a file that IS the deliverable stands; a wiring claim is verified
# ---------------------------------------------------------------------------------------------

def _one(text: str, reply: str, root: Path) -> dict:
    return ag.parse_acceptance(reply, [{"n": 1, "text": text, "source": "ticket"}], root=root)[0]


@pytest.mark.parametrize("files,item,why", [
    ({"docker-compose.yml": "services: {}\n", "Makefile": "run-staging:\n\tdocker compose up\n"},
     "Provide `docker-compose.yml` for both plus two tiny mock upstreams",
     "docker-compose.yml defines staging and production; Makefile run-staging starts it"),
    ({"examples/lumen-pay.good.yaml": "tenant: lumen-pay\n",
      "examples/lumen-pay.bad.yaml": "tenant: lumen-pay\n"},
     "`examples/`: `lumen-pay.good.yaml`, `lumen-pay.bad.yaml`",
     "examples/lumen-pay.good.yaml and examples/lumen-pay.bad.yaml (5 violations)"),
    ({".github/workflows/lint.yml": "jobs: {}\n"}, "CI runs gwlint on every tenant change",
     ".github/workflows/lint.yml runs gwlint validate tenants/"),
    ({"Makefile": "test:\n\tgo test ./...\n"}, "`make test` runs the tests",
     "Makefile target test runs go test"),
])
def test_a_data_file_that_is_the_deliverable_is_met_on_the_reviewers_word(tmp_path, files, item,
                                                                         why):
    for rel, body in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(body)
    got = _one(item, f"ITEM 1: MET — {why}\n", tmp_path)
    assert got["status"] == "met", got["evidence"]


def test_an_extensionless_file_is_evidence_only_when_it_is_in_the_tree(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    assert ag.evidence_paths(tmp_path, "Dockerfile builds a static binary")[0] == ["Dockerfile"]
    assert ag.evidence_paths(tmp_path, "Makefile and LICENSE")[0] == []


def _rulebook_tree(root: Path, loader_file: str, loader_body: str) -> None:
    (root / "rules").mkdir()
    (root / "rules/api-guidelines.yaml").write_text("rules:\n  - id: SEC-001\n")
    (root / loader_file).parent.mkdir(parents=True, exist_ok=True)
    (root / loader_file).write_text(loader_body)


RULEBOOK_ITEM = ("**A rulebook, in YAML, readable by non-engineers:** `rules/api-guidelines.yaml`. "
                 "At least 20 rules:")


def test_a_directory_embed_loader_is_met(tmp_path):
    """`//go:embed rules` names a directory, not the file: the named function decides."""
    _rulebook_tree(tmp_path, "internal/lint/rulebook.go",
                   "package lint\n\n//go:embed rules\nvar rulesFS embed.FS\n\n"
                   "func LoadRulebook() ([]Rule, error) { return parse(rulesFS) }\n")
    got = _one(RULEBOOK_ITEM, "ITEM 1: MET — rules/api-guidelines.yaml (24 rules); LOADED BY "
               "internal/lint/rulebook.go:LoadRulebook\n", tmp_path)
    assert got["status"] == "met", got["evidence"]


@pytest.mark.parametrize("loader_file,body,claim,reason", [
    # A test that mentions the YAML only in a comment is not a loader.
    ("internal/lint/lint_test.go",
     "package lint\n// loads rules/api-guidelines.yaml\nfunc TestRules(t *testing.T) {}\n",
     "internal/lint/lint_test.go:TestRules", "is a test"),
    # The named function exists only in a comment of the cited file.
    ("internal/lint/lint.go",
     "package lint\n// TODO: LoadRulebook reads rules/api-guidelines.yaml\nfunc Check() {}\n",
     "internal/lint/lint.go:LoadRulebook", "does not define LoadRulebook"),
    # The named loader file is not in the tree.
    ("internal/lint/lint.go", "package lint\n", "internal/lint/rules.go:LoadRulebook",
     "is not in the tree"),
])
def test_a_wiring_claim_that_does_not_hold_is_partial(tmp_path, loader_file, body, claim, reason):
    _rulebook_tree(tmp_path, loader_file, body)
    got = _one(RULEBOOK_ITEM, f"ITEM 1: MET — rules/api-guidelines.yaml, {loader_file}; "
               f"LOADED BY {claim}\n", tmp_path)
    assert got["status"] == "partial" and reason in got["evidence"]


def test_judgement_is_the_contracts_tag_never_the_reviewers(tmp_path):
    """THE FAIL-OPEN (re-review of #143): "ITEM 1: MISSING (judgement) — no JWKS" read as met.
    A judgement call is a property of the contract item, set at extraction; the reviewer's own
    tag is ignored, and a MISSING is never advisory."""
    (tmp_path / "a.go").write_text("package a\n")
    jwks = {"n": 1, "text": "JWT/OAuth2 (issuer, audience, required scopes, JWKS URL with "
                            "caching)", "source": "ticket"}
    go = {"n": 2, "text": "Written in Go", "source": "ticket"}
    reply = "ITEM 1: MISSING (judgement) — no JWKS\nITEM 2: MET — a.go\nACCEPTANCE: MET\n"
    rec = ag.judge([jwks, go], reply, {}, root=tmp_path)
    assert rec["verdict"] == "not_met" and rec["missing"] == [1] and rec["advisory"] == []
    assert not ag.passed(rec) and "MAJOR — Must-have 1 MISSING" in ag.findings_summary(rec)
    partial = reply.replace("MISSING (judgement) — no JWKS", "PARTIAL (judgement) — HS256 only")
    rec = ag.judge([jwks, go], partial, {}, root=tmp_path)
    assert rec["verdict"] == "not_met" and rec["partial"] == [1]
    # Tagged at extraction: a PARTIAL is advisory; a MISSING still fails.
    naming = {"n": 1, "text": "Plain, concise naming.", "source": "ticket", "judgement": True}
    rec = ag.judge([naming, go], "ITEM 1: PARTIAL — a few long names in a.go\n"
                   "ITEM 2: MET — a.go\nACCEPTANCE: MET\n", {}, root=tmp_path)
    assert rec["verdict"] == "met" and rec["advisory"] == [1] and rec["partial"] == []
    assert "PARTIAL — advisory (taste)" in ag.must_have_table(rec)
    assert "Plain, concise naming. _(taste)_" in ag.must_have_table(rec)
    assert "1 advisory (taste)" in ag.headline(rec)
    assert ag.card_words(None, rec, "passed") == "Automated checks: passed, 1 advisory"
    assert "MAJOR" not in ag.findings_summary(rec)
    rec = ag.judge([naming, go], "ITEM 1: MISSING — none\nITEM 2: MET — a.go\n"
                   "ACCEPTANCE: MET\n", {}, root=tmp_path)
    assert rec["verdict"] == "not_met" and rec["missing"] == [1]
    # The reviewer is told which items are judgement calls; it is never asked to tag them.
    prompt = ag.acceptance_prompt([naming, go], title="t", stack={}, tree="")
    assert "ITEM 1 [ticket, judgement]" in prompt and "(judgement)" not in prompt


@pytest.mark.parametrize("body,claim,reason", [
    # A language keyword or a too-short name is not a loader.
    ("package lint\nfunc LoadRulebook() { os.ReadFile(\"rules/api-guidelines.yaml\") }\n",
     "internal/lint/rules.go:func", "is not a function or variable name"),
    ("package lint\nfunc LoadRulebook() { os.ReadFile(\"rules/api-guidelines.yaml\") }\n",
     "internal/lint/rules.go:ld", "is not a function or variable name"),
    # Only CALLED there, defined nowhere in it.
    ("package lint\nfunc Check() { LoadRulebook(\"rules/api-guidelines.yaml\") }\n",
     "internal/lint/rules.go:LoadRulebook", "does not define LoadRulebook"),
    # Defined, but its code never names the rulebook (only a comment does).
    ("package lint\n// reads rules/api-guidelines.yaml\nfunc LoadRulebook() []Rule "
     "{ return builtin }\n", "internal/lint/rules.go:LoadRulebook",
     "does not name rules/api-guidelines.yaml"),
])
def test_a_loader_claim_must_define_the_function_and_name_the_file(tmp_path, body, claim, reason):
    _rulebook_tree(tmp_path, "internal/lint/rules.go", body)
    got = _one(RULEBOOK_ITEM, f"ITEM 1: MET — rules/api-guidelines.yaml; LOADED BY {claim}\n",
               tmp_path)
    assert got["status"] == "partial" and reason in got["evidence"], got["evidence"]


@pytest.mark.parametrize("body", [
    "package lint\nfunc LoadRulebook() ([]Rule, error) {\n"
    "\tfiles, _ := filepath.Glob(\"rules/*.yaml\")\n\treturn parse(files)\n}\n",
    "package lint\nconst rulesDir = \"rules\"\nfunc (l *Linter) LoadRulebook() error "
    "{ return l.read(rulesDir) }\n",
])
def test_a_loader_that_reads_the_file_by_glob_or_folder_is_met(tmp_path, body):
    _rulebook_tree(tmp_path, "internal/lint/rules.go", body)
    got = _one(RULEBOOK_ITEM, "ITEM 1: MET — rules/api-guidelines.yaml; LOADED BY "
               "internal/lint/rules.go:LoadRulebook\n", tmp_path)
    assert got["status"] == "met", got["evidence"]


def test_an_item_that_needs_a_loader_is_partial_without_one(tmp_path):
    _rulebook_tree(tmp_path, "internal/lint/lint.go", "package lint\nfunc Check() {}\n")
    items = [{"n": 1, "text": RULEBOOK_ITEM, "source": "ticket", "needs_loader": True}]
    got = ag.parse_acceptance("ITEM 1: MET — rules/api-guidelines.yaml (24 rules), "
                              "internal/lint/lint.go\n", items, root=tmp_path)[0]
    assert got["status"] == "partial" and "loader not named" in got["evidence"]
    # The floor holds even when the reader did not tag it: the item names a rules YAML.
    plain = [{**items[0], "needs_loader": False}]
    assert ag.parse_acceptance("ITEM 1: MET — rules/api-guidelines.yaml, internal/lint/lint.go\n",
                               plain, root=tmp_path)[0]["status"] == "partial"
    # An item naming a file that IS the deliverable stands on its evidence.
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    compose = [{"n": 1, "text": "Provide `docker-compose.yml` for both", "source": "ticket"}]
    assert ag.parse_acceptance("ITEM 1: MET — docker-compose.yml\n", compose,
                               root=tmp_path)[0]["status"] == "met"
    assert "must be loaded" in ag.acceptance_prompt(items, title="t", stack={}, tree="")


def test_reviewer_passes_are_capped_and_the_rest_is_never_met(tmp_path):
    (tmp_path / "a.go").write_text("package a\n")
    n = ag.MAX_PASSES * ag.MAX_ITEMS + 7
    items = ag.acceptance_items({"acceptance_criteria": [f"criterion {i}" for i in range(n)]})
    ask = BatchReviewer()
    rec, _ = ag.run_check(items, ask=ask, title="t", stack={}, root=tmp_path)
    assert ask.passes == ag.MAX_PASSES
    assert rec["verdict"] == "not_met" and rec["items_not_judged"] == 7 and not ag.passed(rec)
    assert "7 items not judged" in ag.must_have_table(rec)
    assert "7 not judged" in ag.headline(rec)


def test_the_kan9_repro_a_reader_tagging_real_requirements_judgement_never_passes(tmp_path):
    """THE REVIEWER'S REPRO (final check of #143): the reader tagged the JWKS item and both
    "At least 20 rules" items `judgement`; PARTIAL answers on them read as advisory and the card
    said passed. The floor drops `judgement` on any checkable item, so the verdict is not met."""
    from boss_engine.services.acceptance import contract as ac

    from ._acceptance_fixtures import load_northgate_v1

    ng = load_northgate_v1()
    words = ac.ticket_items(ng.ticket())
    jwks = next(w for w in words if "JWKS URL with caching" in w)
    rulebook = [w for w in words if "At least 20 rules:" in w]
    tagged = ac._tagged(words, {"judgement_items": [jwks, *rulebook], "loader_items": []})
    items = [{"n": k, **t} for k, t in enumerate(tagged, start=1)]
    assert not any(i.get("judgement") for i in items if i["text"] in [jwks, *rulebook])
    # The floor ADDS needs_loader on the rulebook items (they name a rules YAML).
    assert all(i.get("needs_loader") for i in items if i["text"] in rulebook)
    (tmp_path / "main.go").write_text("package main\n")
    bad = {i["n"] for i in items if i["text"] in [jwks, *rulebook]}
    reply = "\n".join(f"ITEM {i['n']}: " + ("PARTIAL — HS256 shared secret; 5 hard-coded checks"
                                            if i["n"] in bad else "MET — main.go")
                      for i in items) + "\nACCEPTANCE: NOT MET\n"
    rec = ag.judge(items, reply, {}, root=tmp_path)
    assert rec["verdict"] == "not_met" and sorted(rec["partial"]) == sorted(bad)
    assert rec["advisory"] == [] and not ag.passed(rec)
    assert not ag.card_words(None, rec, "passed").startswith("Automated checks: passed")
    # Even a contract stored before the floor (tags set on the item itself) is floored here.
    stored = [{**i, "judgement": True} if i["n"] in bad else i for i in items]
    assert ag.judge(stored, reply, {}, root=tmp_path)["verdict"] == "not_met"


@pytest.mark.parametrize("text,allowed", [
    ("Plain, concise naming. Someone at the customer will read this code.", True),
    ("Clear, readable error messages.", True),
    ("Clean, idiomatic Go; `go vet` and `go test ./...` clean.", False),          # backticks
    ("JWT/OAuth2 with JWKS caching", False),                                        # standard
    ("Rate limits answer 429", False),                                              # a digit
    ("Serve the report at /v1/usage", False),                                       # endpoint
    ("The response must be readable", False),                                       # must
    ("At least twenty rules", False),                                               # at least
    ("Returns the right exit code", False),                                         # exit code
    ("Readable config.yaml", False),                                                # a file
])
def test_the_judgement_floor(text, allowed):
    from boss_engine.services.acceptance import contract as ac

    assert ac.judgement_allowed(text) is allowed


@pytest.mark.parametrize("text,needs", [
    ("**A rulebook, in YAML:** `rules/api-guidelines.yaml`. At least 20 rules:", True),
    ("Validate every tenant file against `schema/tenant.schema.json`", True),
    ("Provide `docker-compose.yml` for both environments", False),
    ("`examples/`: `lumen-pay.good.yaml`, `lumen-pay.bad.yaml`", False),
    ("CI runs gwlint via `.github/workflows/lint.yml` using the rules", False),
    ("**JSON Schema** for `tenants/<tenant>.yaml`", False),              # a placeholder path
])
def test_the_needs_loader_floor(text, needs):
    from boss_engine.services.acceptance import contract as ac

    assert ac.needs_loader_floor(text) is needs


def test_a_not_met_summary_over_advisory_items_only_is_met(tmp_path):
    (tmp_path / "a.go").write_text("package a\n")
    items = [{"n": 1, "text": "Plain, concise naming.", "source": "ticket", "judgement": True},
             {"n": 2, "text": "Written in Go", "source": "ticket"}]
    rec = ag.judge(items, "ITEM 1: PARTIAL — long names\nITEM 2: MET — a.go\n"
                   "ACCEPTANCE: NOT MET\n", {}, root=tmp_path)
    assert rec["verdict"] == "met" and rec["advisory"] == [1]
    # A NOT MET with nothing advisory and everything met is still the reviewer's slip.
    rec = ag.judge(items[1:], "ITEM 2: MET — a.go\nACCEPTANCE: NOT MET\n", {}, root=tmp_path)
    assert rec["verdict"] == "not_run"
