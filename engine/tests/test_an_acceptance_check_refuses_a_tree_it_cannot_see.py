"""An acceptance check judges the branch it can see, or refuses; its tables are history
(BOSSAPP-1323).

THE REAL ROWS (northgate-gateway, change #2, crun_95aca9c76ddf4f4dbfe4):
  acc_b12b1a24 09:03  25 met / 5 partial  — the reviewer found the Go tree
  acc_594af6ee 09:06  0 met / 30 missing  — "none of these files exist anywhere in the working
                                            tree", "no go.mod exists anywhere in the tree"
The second replaced the first on the run, and the follow-up's brief was built from it. The
reviewer had started in the sandbox's empty HOME (test_a_session_reads_the_tree_it_was_sent);
the branch held 53 Go files.

Now a check is NOT RUN only on PROOF the reviewer did not see the tree: the files it called
"not there" ARE in the tree (or its session never started in the tree: engine_contract refuses
that at the start). Never on wording or on the tree's contents: a correct "go.mod does not
exist, this is Node" verdict and an empty build are MISSING, and the coder is sent (PR #164
review E1). Every check's table is kept, a refusal never replacing a judged one.
"""
from __future__ import annotations

from pathlib import Path

from boss_engine.services import acceptance_gate as ag

#: The real evidence of acc_594af6ee, word for word (items 1-6).
BLIND = [
    "looked for cmd/northgate-gateway/main.go, Makefile (run-staging target), "
    "mocks/echo-upstream/main.go; none of these files exist anywhere in the working tree.",
    "looked for internal/config/watcher.go and config/tenants/lumen-pay.yaml; absent from the "
    "filesystem entirely.",
    "looked for cmd/gwlint/main.go and examples/lumen-pay.bad.yaml; absent.",
    "looked for examples/lumen-pay.good.yaml and cmd/gwlint/main.go; absent.",
    "looked for any _test.go files and README.md; no files exist to run or read.",
    "looked for go.mod declaring the module; no go.mod exists anywhere in the tree.",
]
CRITERIA = [
    "`make run-staging` starts the gateway + mock upstreams; a request with a valid JWT reaches "
    "the upstream",
    "Editing `config/tenants/lumen-pay.yaml` is picked up without restart",
    "`gwlint validate examples/lumen-pay.bad.yaml` exits 1 and names each violation",
    "`gwlint validate examples/lumen-pay.good.yaml` exits 0.",
    "Tests cover the gateway and gwlint",
    "A Go module (go.mod) at the repository root",
]


def _items():
    return ag.acceptance_items({"acceptance_criteria": CRITERIA})


def _go_branch(root: Path) -> Path:
    for rel, body in {
        "go.mod": "module northgate\n\ngo 1.23\n",
        "Makefile": "run-staging:\n\tdocker compose up\n",
        "cmd/northgate-gateway/main.go": "package main\n\nfunc main() {}\n",
        "cmd/gwlint/main.go": "package main\n\nfunc main() {}\n",
        "internal/config/watcher.go": "package config\n",
        "schema/schema.go": "package schema\n",
        "schema/tenant.schema.json": "{}\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    return root


def _ask(answers):
    reply = "\n".join(f"ITEM {n}: {st} — {why}" for n, (st, why) in enumerate(answers, 1))
    return lambda prompt: {"summary": reply + "\nACCEPTANCE: NOT MET", "cost_usd_micros": 0}


def test_a_blind_reviewer_on_a_real_tree_is_not_run_never_missing(tmp_path):
    root = _go_branch(tmp_path)
    rec, _ = ag.run_check(_items(), ask=_ask([("MISSING", w) for w in BLIND]), title="t",
                          stack={}, root=root)
    assert rec["verdict"] == "not_run" and rec["refused"] is True
    assert "did not see the tree" in rec["why"]
    assert rec["missing"] == [] and rec["counts"]["missing"] == 0
    assert all(i["status"] == ag.UNJUDGED for i in rec["items"])
    assert not ag.passed(rec)
    assert "MISSING" not in ag.findings_summary(rec)


def test_an_empty_build_is_missing_and_the_coder_is_sent(tmp_path):
    """A build that produced nothing: every item really is absent — a CODE problem."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rec, _ = ag.run_check(_items(), ask=_ask([("MISSING", w) for w in BLIND]), title="t",
                          stack={}, root=empty)
    assert rec["verdict"] == "not_met" and not rec.get("refused")
    assert len(rec["missing"]) == len(CRITERIA)


def test_a_node_build_of_a_go_brief_stays_missing(tmp_path):
    """The reviewer is RIGHT: no go.mod, this is Node. Its evidence names Node files that do
    exist ("this is Node: package.json, src/server.js") — that is not a claim they are absent."""
    node = tmp_path / "node"
    for rel in ("package.json", "src/server.js", "src/gwlint.js"):
        (node / rel).parent.mkdir(parents=True, exist_ok=True)
        (node / rel).write_text("{}\n" if rel.endswith(".json") else "export {}\n")
    answers = [("MISSING", "looked for cmd/northgate-gateway/main.go and a Makefile; absent. "
                           "The gateway is Node (src/server.js).")] * 5
    answers.append(("MISSING", "no go.mod exists anywhere in the tree; this is a Node project "
                               "(package.json, src/server.js)."))
    rec, _ = ag.run_check(_items(), ask=_ask(answers), title="t", stack={}, root=node)
    assert rec["verdict"] == "not_met" and not rec.get("refused")
    assert rec["missing"] == list(range(1, 7))
    assert "MISSING" in ag.findings_summary(rec)


def test_a_session_that_never_started_in_its_tree_is_not_run(tmp_path):
    """A blind SESSION (its cwd outside the tree) is refused at the start
    (engine_contract.session_tree_problem): its pass fails, so the check is not run."""
    from boss_engine.services.engine_contract import EngineSessionFailed

    def ask(prompt):
        raise EngineSessionFailed("claude not started: the session's tree is outside the run's "
                                  "sandbox directory")
    rec, _ = ag.run_check(_items(), ask=ask, title="t", stack={}, root=_go_branch(tmp_path))
    assert rec["verdict"] == "not_run" and rec["missing"] == []
    assert "EngineSessionFailed" in rec["why"]


def test_a_reviewer_that_sees_the_tree_still_finds_real_gaps(tmp_path):
    root = _go_branch(tmp_path)
    answers = [("MET", "cmd/northgate-gateway/main.go and Makefile")] * 5 + [
        ("MISSING", "the README has no quick start section")]
    rec, _ = ag.run_check(_items(), ask=_ask(answers), title="t", stack={}, root=root)
    assert not rec.get("refused") and rec["verdict"] == "not_met"
    one_blind = [("MET", "cmd/gwlint/main.go")] * 5 + [("MISSING", BLIND[5])]
    rec2, _ = ag.run_check(_items(), ask=_ask(one_blind), title="t", stack={}, root=root)
    assert not rec2.get("refused"), "one absent file is a finding, not a blind reviewer"


def test_every_table_is_history_and_a_refusal_never_replaces_a_judged_one(tmp_path):
    root = _go_branch(tmp_path)
    history: list = []
    judged: dict = {}
    first, _ = ag.run_check(_items(), ask=_ask(
        [("MET", "cmd/northgate-gateway/main.go")] * 5 + [("PARTIAL", "go.mod only")]),
        title="t", stack={}, root=root)
    stood = ag.keep_history(history, first, judged)
    assert stood is first and first["counts"]["met"] == 5
    blind, _ = ag.run_check(_items(), ask=_ask([("MISSING", w) for w in BLIND]), title="t",
                            stack={}, root=root)
    stood = ag.keep_history(history, blind, judged)
    assert stood["counts"]["met"] == 5, "the judged table stands"
    assert "did not see the tree" in stood["later_check_refused"]
    assert [h["counts"]["met"] for h in history] == [5, 0]
    assert [h["refused"] for h in history] == [False, True]
    import json

    json.dumps(history)            # it rides the run's state as plain JSON


def test_the_pipeline_keeps_the_history_on_the_run():
    import inspect

    from boss_engine.services import governed_code_task_service as g

    src = inspect.getsource(g)
    assert "_acceptance_gate.keep_history(_acceptance_history, rec," in src
    assert "_acceptance_gate.HISTORY_KEY: _acceptance_history," in src
