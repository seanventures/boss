"""The Knowledge Graph case-study harness (scripts/case-study/knowledge; case study §6, §7).

No model is called here. A fake BOSS stands in for the public API, so these tests prove the
harness's own rules: spend needs an approved stage, decisions are answered by script or stop,
results resume, arm B waits for the knowledge switch, and the report counts failed attempts.
The T1 hidden suite is proven against the reference service when a scratch Postgres is given
(BOSS_CASE_STUDY_PG_DSN).
"""
from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "scripts/case-study/knowledge"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"case_study_{name}", HARNESS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_py = _load("run")
report_py = _load("report")


class FakeBoss:
    def __init__(self, *, decision_kind="spec_signoff", knowledge_setting=True, final="completed"):
        self.calls: list[tuple] = []
        self.decision_kind = decision_kind
        self.knowledge_setting = knowledge_setting
        self.final = final
        self.polls = 0

    def set_setting(self, project_id, key, value):
        self.calls.append(("setting", key, value))
        if not self.knowledge_setting:
            raise run_py.HarnessError("PUT /v1/settings/knowledge_graph -> 400: unknown setting")

    def reserve_budget(self, project_id, usd, reason):
        self.calls.append(("reserve", usd, reason))
        return {"status": "reserved", "reservation_id": "res_1"}

    def start_code_task(self, body):
        self.calls.append(("start", body["title"], body["model_config"]["model"]))
        return {"run_id": f"run_{len(self.calls)}"}

    def code_task(self, run_id):
        self.polls += 1
        status = "running" if self.polls % 2 else self.final
        return {"status": status, "decision_id": "dec_1", "pull_request_url": "https://github.example.com/pr/1"}

    def decision(self, decision_id):
        return {"id": decision_id, "status": "pending", "kind": self.decision_kind, "question": "Sign the spec?"}

    def answer_decision(self, decision_id, verb, note):
        self.calls.append(("answer", decision_id, verb, note))

    def llm_calls(self, project_id, run_id):
        return [{"prompt_tokens": 1000, "completion_tokens": 200, "cache_read_tokens": 5000, "cache_write_tokens": 0,
                 "turns": 3, "cost_usd_micros": 1_250_000}] * 2

    def retrospective(self, run_id):
        return {"elapsed_minutes": 12.5}


def _args(tmp_path, **overrides):
    base = dict(stage="smoke_e1", arm="A", engine="E1", tasks="T1..T4", repeat=1, resume=False, approved_by="operator",
                boss_url="http://127.0.0.1:8000", project_id="northpeak", saleor_dir=str(tmp_path / "saleor"),
                fulfilment_dir=str(tmp_path / "fulfilment"), out=str(tmp_path / "out"))
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(tmp_path, boss, **overrides):
    return run_py.run(_args(tmp_path, **overrides), client=boss, evaluator=lambda t: {"solved": t.id != "T3",
                                                                                     "tests_passed": 3, "tests_total": 4},
                      prepare=lambda t: None, sleep=lambda s: None)


def test_tasks_t1_to_t4_have_a_brief_a_hidden_suite_and_compile():
    for task_id in ("T1", "T2", "T3", "T4"):
        task = run_py.load_task(task_id)
        assert "## Contract" in task.brief and len(task.brief) > 400
        suite = task.dir / task.evaluate["suite"]
        py_compile.compile(str(suite), doraise=True)
        assert "Never shown to agents" in suite.read_text()
    patch = (HARNESS / "tasks/T4/fault.patch").read_text()
    assert patch.startswith("diff --git a/saleor/warehouse/management.py") and patch.count("\n@@ ") == 1


def test_the_smoke_runs_on_the_open_model_first_and_the_sonnet_estimate_matches_the_case_study():
    config = run_py.load_config()
    assert config["stages"]["smoke"]["engines"] == ["E2"]          # operator decision 2026-10-21
    est = run_py.estimate(config, "smoke")
    assert est["builds"] == 8 and (est["usd_low"], est["usd_high"], est["ceiling_usd"]) == (4.8, 15.2, 24.0)
    e1 = run_py.estimate(config, "smoke_e1")
    assert e1["builds"] == 8 and (e1["usd_low"], e1["usd_high"], e1["ceiling_usd"]) == (48.0, 80.0, 120.0)


def test_no_spend_without_an_approved_stage_and_nothing_outside_it(tmp_path):
    boss = FakeBoss()
    with pytest.raises(run_py.HarnessError, match="approval"):
        _run(tmp_path, boss, approved_by=None)
    with pytest.raises(run_py.HarnessError, match="does not cover"):
        _run(tmp_path, boss, tasks="T5")
    with pytest.raises(run_py.HarnessError, match="does not cover"):
        _run(tmp_path, boss, engine="E2")          # smoke_e1 is Sonnet only
    assert boss.calls == []


def test_arm_b_waits_for_the_knowledge_switch(tmp_path):
    boss = FakeBoss(knowledge_setting=False)
    with pytest.raises(run_py.HarnessError, match="arm B needs"):
        _run(tmp_path, boss, arm="B")
    assert not [c for c in boss.calls if c[0] in ("reserve", "start")]
    # Arm A runs as BOSS is today, with or without the switch.
    assert len(_run(tmp_path, FakeBoss(knowledge_setting=False), tasks="T1")) == 1


def test_a_run_drives_boss_answers_by_script_records_usage_and_resumes(tmp_path):
    boss = FakeBoss()
    results = _run(tmp_path, boss)
    assert [r["task"] for r in results] == ["T1", "T2", "T3", "T4"]
    assert ("setting", "knowledge_graph", "off") in boss.calls
    reserve = [c for c in boss.calls if c[0] == "reserve"][0]
    assert reserve[1] == 8 * 15.0 and "approved by operator" in reserve[2]
    assert [c[2] for c in boss.calls if c[0] == "start"] == ["claude-sonnet-5"] * 4
    answer = [c for c in boss.calls if c[0] == "answer"][0]
    assert answer[2] == "approve" and answer[3] == "Matches the brief."
    r1 = json.loads((tmp_path / "out/E1/A/T1-r1.json").read_text())
    assert r1["usage"]["usd"] == 2.5 and r1["usage"]["turns"] == 6 and r1["wall_minutes"] == 12.5
    assert r1["approved_by"] == "operator" and r1["evaluation"]["solved"] is True
    # Resume: finished builds are read back, not run again.
    again = FakeBoss()
    _run(tmp_path, again, resume=True)
    assert not [c for c in again.calls if c[0] == "start"]


def test_an_unscripted_decision_stops_the_sequence(tmp_path):
    results = _run(tmp_path, FakeBoss(decision_kind="something_new"))
    assert len(results) == 1 and results[0]["status"] == "needs_operator"
    assert "something_new" in results[0]["error"]


def test_a_fork_holding_a_hidden_suite_is_refused(tmp_path):
    leak = tmp_path / "saleor/saleor/warehouse/tests/test_hidden_t4_no_oversell.py"
    leak.parent.mkdir(parents=True)
    leak.write_text("")
    with pytest.raises(run_py.HarnessError, match="hidden suite present"):
        _run(tmp_path, FakeBoss(), tasks="T4")


def test_the_report_counts_failed_attempts_and_draws_charts(tmp_path):
    rows = []
    for arm, costs, solved in (("A", [4.0, 6.0, 8.0, 2.0], [1, 1, 0, 1]), ("B", [3.0, 4.0, 5.0, 2.0], [1, 1, 1, 1])):
        for i, (cost, ok) in enumerate(zip(costs, solved), start=1):
            rows.append({"task": f"T{i}", "arm": arm, "engine": "E1", "repeat": 1, "stage": "smoke",
                         "approved_by": "operator", "saleor_sha": "9ae360ae76b4", "wall_minutes": 10 + i,
                         "usage": {"usd": cost, "turns": 5, "prompt_tokens": 10, "completion_tokens": 2},
                         "evaluation": {"solved": bool(ok)}})
    for r in rows:
        path = tmp_path / "res" / r["engine"] / r["arm"] / f"{r['task']}-r1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(r))
    summary = report_py.summarize(report_py.load(tmp_path / "res"))
    assert summary[("E1", "A")]["usd_per_solved"] == round(20.0 / 3, 2)      # T3's failed $8 is counted
    assert summary[("E1", "B")]["usd_per_solved"] == 3.5
    out = report_py.render(report_py.load(tmp_path / "res"), tmp_path / "report")
    text = out.read_text()
    assert "INCOMPLETE" in text and "| E1 | T3 | 0/1 | 8.0 |" in text
    assert (tmp_path / "report/cost_E1.svg").read_text().startswith("<svg")


def test_the_sign_test_is_exact():
    assert report_py.sign_test([-1] * 10)["p"] == round(2 / 2 ** 10, 4)
    assert report_py.sign_test([-1, 1, 0])["n"] == 2


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not os.environ.get("BOSS_CASE_STUDY_PG_DSN"), reason="needs a scratch Postgres")
@pytest.mark.parametrize("broken", ["", "signature"])
def test_the_t1_hidden_suite_accepts_the_reference_and_catches_a_broken_service(broken):
    dsn, port, secret = os.environ["BOSS_CASE_STUDY_PG_DSN"], _free_port(), "case-study-selftest"
    env = {**os.environ, "PORT": str(port), "DATABASE_URL": dsn, "NORTHPEAK_WEBHOOK_SECRET": secret,
           "T1_REFERENCE_BREAK": broken}
    server = subprocess.Popen([sys.executable, str(HARNESS / "selftest/t1_reference.py")], env=env)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               str(HARNESS / "tasks/T1/hidden_tests/hidden_t1_fulfilment.py")],
                              env={**env, "NORTHPEAK_URL": f"http://127.0.0.1:{port}", "NORTHPEAK_DB_DSN": dsn},
                              capture_output=True, text=True, timeout=300)
        if broken:
            assert proc.returncode != 0 and "test_a_bad_signature_is_refused" in proc.stdout
        else:
            assert proc.returncode == 0, proc.stdout[-2000:]
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_a_suite_that_cannot_be_imported_never_counts_as_a_valid_failure():
    assert run_py.collection_error("ImportError while importing test module '/x/test_hidden_t3.py'.")
    assert run_py.collection_error("ERROR collecting saleor/x.py")
    assert not run_py.collection_error("3 failed, 2 passed in 4.1s")


# --- the banking set (Fineract, B1..B4; BOSSAPP-758) ---------------------------------------------------------------
BANKING = ("B1", "B2", "B3", "B4")


def test_banking_tasks_have_a_brief_a_hidden_suite_and_their_proofs():
    config = run_py.load_config()
    # 1.15.0, not 1.16.0: BOSS's build image ships JDK 21 and 1.16.0 needs 25 (BOSSAPP-758).
    assert config["fineract"]["tag"] == "1.15.0" and len(config["fineract"]["sha"]) == 40
    assert config["fineract"]["jdk"] == 21
    assert run_py.expand_tasks("B1..B4") == list(BANKING)
    for task_id in BANKING:
        task = run_py.load_task(task_id)
        assert task.repo == "fineract" and task.evaluate["mode"] == "fineract_gradle"
        assert "## Contract" in task.brief and len(task.brief) > 800
        for item in task.evaluate["install"]:
            source = task.dir / item["from"]
            assert source.exists() and item["to"].startswith(task.evaluate["module"] + "/src/test/")
            if source.suffix == ".java":
                assert "hidden acceptance suite" in source.read_text() and source.stem in item["to"]
        if task.fault_patch:                      # B3, B4: one planted fault, in the module the suite runs
            patch = task.fault_patch.read_text()
            assert patch.startswith("diff --git a/fineract-loan/src/main/") and patch.count("\n@@ ") == 1
            assert task.meta["distinct_from"] in BANKING
        else:                                     # B1, B2: a reference refactor and the mutants it must survive
            assert (task.dir / task.evaluate["reference_patch"]).exists() and len(task.evaluate["mutants"]) >= 3
            assert all((task.dir / m).exists() for m in task.evaluate["mutants"])
            assert task.evaluate["behaviour"] and task.evaluate["contract"]


def test_each_learning_pair_scores_reuse_of_what_the_first_task_found():
    for first, second in (("B1", "B2"), ("B3", "B4")):
        taught = {i["id"] for i in run_py.load_task(first).meta["invariants"]}
        expected = run_py.load_task(second).meta["expected_reuse"]
        assert len(expected) >= 4
        for item in expected:
            assert item["from"] == first
            assert item["id"] in taught or item["id"].startswith(f"{first}-"), item["id"]


def test_the_banking_stages_and_their_estimates():
    config = run_py.load_config()
    smoke = run_py.estimate(config, "bank_smoke")
    assert config["stages"]["bank_smoke"]["arms"] == ["A", "AP", "B"]
    assert smoke["builds"] == 12 and (smoke["usd_low"], smoke["usd_high"], smoke["ceiling_usd"]) == (7.2, 22.8, 36.0)
    e1 = run_py.estimate(config, "bank_learning_e1")
    assert e1["builds"] == 8 and (e1["usd_low"], e1["usd_high"], e1["ceiling_usd"]) == (48.0, 80.0, 120.0)


def test_each_banking_arm_has_its_own_private_demo_repository_and_a_prime_sets_keyword(tmp_path):
    forks = run_py.load_config()["fineract"]["forks"]
    repos = {arm: fork["repo"] for arm, fork in forks["E2"].items()}
    assert repos == {"A": "paramg/northpeak-fineract-a", "AP": "paramg/northpeak-fineract-ap",
                     "B": "paramg/northpeak-fineract-b"}
    assert all("kaappu" not in str(fork).lower() for fork in forks["E2"].values())
    boss = FakeBoss()
    results = _run(tmp_path, boss, stage="bank_smoke", engine="E2", arm="AP", tasks="B1..B4", fineract_dir=str(tmp_path / "f"))
    assert ("setting", "knowledge_graph", "keyword") in boss.calls
    assert {r["fineract_repo"] for r in results} == {"paramg/northpeak-fineract-ap"}
    untouched = FakeBoss()
    with pytest.raises(run_py.HarnessError, match="no Fineract demo repository for E1/A"):
        _run(tmp_path, untouched, stage="bank_learning_e1", engine="E1", arm="A", tasks="B1",
             fineract_dir=str(tmp_path / "f"), out=str(tmp_path / "o3"))
    assert untouched.calls == []                   # refused before any setting, budget or start


def test_arm_a_prime_waits_for_a_keyword_switch(tmp_path):
    boss = FakeBoss(knowledge_setting=False)
    with pytest.raises(run_py.HarnessError, match="arm AP needs .* 'keyword'"):
        _run(tmp_path, boss, stage="bank_smoke", engine="E2", arm="AP", tasks="B1..B4", fineract_dir=str(tmp_path / "f"))
    assert not [c for c in boss.calls if c[0] in ("reserve", "start")]


def test_a_banking_run_records_the_fineract_pin_and_the_reuse_to_score(tmp_path):
    boss = FakeBoss()
    results = _run(tmp_path, boss, stage="bank_smoke", engine="E2", arm="B", tasks="B1..B4", fineract_dir=str(tmp_path / "f"))
    assert [r["task"] for r in results] == list(BANKING)
    assert ("setting", "knowledge_graph", "on") in boss.calls
    assert {r["fineract_sha"] for r in results} == {run_py.load_config()["fineract"]["sha"]}
    assert results[0]["expected_reuse"] == [] and "INV-POST-DEDUCT" in results[3]["expected_reuse"]
    with pytest.raises(run_py.HarnessError, match="--fineract-dir"):
        _run(tmp_path, FakeBoss(), stage="bank_smoke", engine="E2", arm="A", tasks="B1", fineract_dir=None, out=str(tmp_path / "o2"))


def test_a_fork_holding_a_java_hidden_suite_or_golden_file_is_refused(tmp_path):
    for leak in ("fineract-loan/src/test/java/x/CaseStudyHiddenB3RepaymentPostingTest.java",
                 "fineract-loan/src/test/resources/case-study/b2_charge_calculations.golden"):
        path = tmp_path / "f" / leak
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        with pytest.raises(run_py.HarnessError, match="hidden suite present"):
            run_py.hidden_suites_absent(tmp_path / "f")
        path.unlink()


def _fake_gradle(script: str) -> str:
    return f"{sys.executable} -c '{script}' {{module}} {{tests}}"


def test_the_fineract_evaluator_reads_junit_counts_and_removes_the_suite(tmp_path):
    task = run_py.load_task("B3")
    cls = "org.apache.fineract.portfolio.loanaccount.domain.transactionprocessor.impl.CaseStudyHiddenB3RepaymentPostingTest"
    write_xml = ("import sys, pathlib; d = pathlib.Path(sys.argv[1], \"build/test-results/test\"); d.mkdir(parents=True, exist_ok=True); "
                 f"(d / \"TEST-org.-SHORT.CaseStudyHiddenB3RepaymentPostingTest.xml\").write_text(\"<testsuite name=\\\"{cls}\\\" tests=\\\"123\\\" failures=\\\"7\\\" errors=\\\"0\\\"/>\"); sys.exit(1)")
    result = run_py.evaluate_fineract(task, tmp_path, gradle_cmd=_fake_gradle(write_xml))
    assert (result["tests_passed"], result["tests_total"], result["solved"], result["collection_error"]) == (116, 123, False, False)
    assert result["by_class"][cls]["failures"] == 7
    assert not list(tmp_path.rglob("CaseStudyHidden*.java"))           # never left behind in the fork


def test_a_fineract_suite_that_does_not_compile_never_counts_as_a_valid_failure(tmp_path):
    task = run_py.load_task("B1")
    broken = "import sys; print(\"> Task :fineract-progressive-loan:compileTestJava FAILED\"); sys.exit(1)"
    result = run_py.evaluate_fineract(task, tmp_path, gradle_cmd=_fake_gradle(broken))
    assert result["collection_error"] and not result["solved"] and result["tests_total"] == 0
    assert not list(tmp_path.rglob("*.golden"))
