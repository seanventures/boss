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


PROJECTS = [{"id": "northpeak", "slug": "northpeak", "name": "NorthPeak"},
            {"id": "proj_bank_a", "slug": "northpeak-bank-a", "name": "NorthPeak Bank E2 arm A"},
            {"id": "proj_bank_ap", "slug": "northpeak-bank-ap", "name": "NorthPeak Bank E2 arm AP"},
            {"id": "proj_bank_b", "slug": "northpeak-bank-b", "name": "NorthPeak Bank E2 arm B"}]


class StubWorkspace:
    """The arm's clone, for tests about BOSS and the scripts: every checkout is a no-op."""

    def __init__(self, repo_dir):
        self.dir = Path(repo_dir)

    def head(self):
        return "0" * 40

    def sync_main(self):
        return "0" * 40

    def checkout_pr(self, number, head_branch):
        return "1" * 40


class StubHost:
    def __init__(self):
        self.merges = []

    def find_pr(self, repo, head_branch):
        return 1

    def pull_request(self, repo, number):
        return {"number": number, "url": f"https://github.com/{repo}/pull/{number}", "state": "OPEN", "merged": False,
                "head_ref": "boss/code/x", "head_sha": "1" * 40, "base": "main"}

    def merge(self, repo, number, sha, method="merge"):
        self.merges.append((repo, number, sha))
        return "2" * 40


class FakeBoss:
    def __init__(self, *, decision_kind="spec_signoff", knowledge_setting=True, final="completed"):
        self.calls: list[tuple] = []
        self.bodies: list[dict] = []
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

    def projects(self):
        return PROJECTS

    def start_code_task(self, body):
        self.bodies.append(body)
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
                fulfilment_dir=str(tmp_path / "fulfilment"), out=str(tmp_path / "out"), clone_dir=str(tmp_path / "clones"))
    base.update(overrides)
    return SimpleNamespace(**base)


def _run(tmp_path, boss, **overrides):
    return run_py.run(_args(tmp_path, **overrides), client=boss, evaluator=lambda t: {"solved": t.id != "T3",
                                                                                     "tests_passed": 3, "tests_total": 4},
                      prepare=lambda t: None, sleep=lambda s: None, host=StubHost(), workspace=StubWorkspace)


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


# --- grading BOSS's pull request, and accepting it (BOSSAPP-758) -----------------------------------------------------
# A fake GitHub (one bare repository: main, branches, pull requests as refs/pull/N/head, gh's merge) and a fake BOSS
# that opens a real PR on it. The arm's clone is a real clone, so these prove what the harness checks out and grades.
IDENT = ("-c", "user.name=Case Study", "-c", "user.email=case-study@example.invalid")


def _git(cwd, *args):
    return subprocess.run(["git", *IDENT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeGitHub:
    def __init__(self, root: Path):
        self.bare = root / "remote.git"
        self.work = root / "author"
        _git(root, "init", "--quiet", "--bare", "-b", "main", str(self.bare))
        _git(root, "clone", "--quiet", str(self.bare), str(self.work))
        (self.work / "ledger.txt").write_text("v0\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "--quiet", "-m", "snapshot")
        _git(self.work, "push", "--quiet", "origin", "HEAD:main")
        self.prs: dict[int, dict] = {}
        self.merges: list[tuple] = []

    def main(self) -> str:
        return _git(self.bare, "rev-parse", "main")

    def file_on_main(self) -> str:
        return _git(self.bare, "show", "main:ledger.txt")

    def open_pr(self, branch: str, content: str) -> int:
        """What BOSS does: a branch from today's main with its change, and a pull request for it."""
        _git(self.work, "fetch", "--quiet", "origin")
        _git(self.work, "checkout", "--quiet", "-B", branch, "origin/main")
        (self.work / "ledger.txt").write_text(content)
        _git(self.work, "commit", "--quiet", "-am", f"BOSS: {branch}")
        number = len(self.prs) + 1
        _git(self.work, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", f"HEAD:refs/pull/{number}/head")
        self.prs[number] = {"head_ref": branch, "merged": False, "merge_commit": None}
        return number

    # the host interface (GitHubHost)
    def pull_request(self, repo, number):
        pr = self.prs[number]
        return {"number": number, "url": f"https://github.com/{repo}/pull/{number}", "draft": False,
                "state": "MERGED" if pr["merged"] else "OPEN", "merged": pr["merged"], "head_ref": pr["head_ref"],
                "head_sha": _git(self.bare, "rev-parse", f"refs/pull/{number}/head"), "base": "main",
                "merge_commit": pr["merge_commit"]}

    def find_pr(self, repo, head_branch):
        return next((n for n, pr in self.prs.items() if pr["head_ref"] == head_branch), None)

    def merge(self, repo, number, sha, method="merge", by="harness"):
        head = _git(self.bare, "rev-parse", f"refs/pull/{number}/head")
        if head != sha:
            raise run_py.HarnessError("Head branch was modified")
        _git(self.work, "fetch", "--quiet", "origin")
        _git(self.work, "checkout", "--quiet", "-B", "main", "origin/main")
        _git(self.work, "merge", "--quiet", "--no-ff", "-m", f"Merge pull request #{number}", sha)
        _git(self.work, "push", "--quiet", "origin", "HEAD:main")
        self.prs[number].update(merged=True, merge_commit=_git(self.work, "rev-parse", "HEAD"))
        self.merges.append((by, number, sha))
        return self.prs[number]["merge_commit"]


class PrBoss(FakeBoss):
    """BOSS working on the fake GitHub: per task, the ledger.txt its PR writes (None: the build fails, no PR)."""

    def __init__(self, github: FakeGitHub, work: dict, *, merges_itself=False, merge_decision=True):
        super().__init__()
        self.github, self.work, self.merges_itself, self.merge_decision = github, work, merges_itself, merge_decision
        self.runs: dict[str, dict] = {}
        self.statuses: dict[str, str] = {}

    def start_code_task(self, body):
        out = super().start_code_task(body)
        self.runs[out["run_id"]] = {"task": body["title"].split(":")[0], "polls": 0}
        return out

    def code_task(self, run_id):
        run = self.runs[run_id]
        run["polls"] += 1
        if run["polls"] == 1:
            return {"id": run_id, "status": "running", "decision_id": f"spec_{run_id}"}
        content = self.work[run["task"]]
        if content is None:
            return {"id": run_id, "status": "failed", "decision_id": None}
        if "pr" not in run:
            run["pr"] = self.github.open_pr(f"boss/code/{run_id}", content)
        return {"id": run_id, "status": "completed", "pull_request_number": run["pr"],
                "pull_request_url": f"https://github.com/x/pull/{run['pr']}",
                "decision_id": f"merge_{run_id}" if self.merge_decision else None}

    def decision(self, decision_id):
        status = self.statuses.get(decision_id, "pending")
        if decision_id.startswith("merge_"):
            return {"id": decision_id, "status": status, "kind": "pull_request", "question": "Approve code change",
                    "recommendation": {"action": "review_pull_request"}}
        return {"id": decision_id, "status": status, "kind": "spec_signoff", "question": "Sign the spec?"}

    def answer_decision(self, decision_id, verb, note):
        super().answer_decision(decision_id, verb, note)
        self.statuses[decision_id] = "approved" if verb == "approve" else "rejected"
        if self.merges_itself and decision_id.startswith("merge_"):
            run = self.runs[decision_id.removeprefix("merge_")]
            sha = _git(self.github.bare, "rev-parse", f"refs/pull/{run['pr']}/head")
            self.github.merge("x", run["pr"], sha, by="boss")


def _graded_run(tmp_path, boss, github, tasks="B1,B2", seen=None, **overrides):
    """Banking tasks in arm A's real clone of the fake GitHub; the 'hidden suite' passes when the graded tree's
    ledger says PASS."""
    clone = tmp_path / "clones" / "northpeak-fineract-a"
    if not clone.exists():
        _git(tmp_path, "clone", "--quiet", str(github.bare), str(clone))
    seen = seen if seen is not None else {}

    def evaluate(task):
        text = (clone / "ledger.txt").read_text()
        seen[task.id] = {"graded_head": _git(clone, "rev-parse", "HEAD"), "ledger": text}
        return {"solved": "PASS" in text, "tests_passed": 1, "tests_total": 1}

    def prepare(task):
        seen.setdefault("started_from", {})[task.id] = (clone / "ledger.txt").read_text()

    args = _args(tmp_path, stage="bank_smoke", engine="E2", arm="A", tasks=tasks, project_id=None,
                 fineract_dir=None, **overrides)
    return run_py.run(args, client=boss, evaluator=evaluate, prepare=prepare, sleep=lambda s: None, host=github,
                      clock=iter(range(0, 10_000, 60)).__next__)


def test_the_pr_head_is_graded_and_a_passing_pr_is_merged_through_bosss_decision(tmp_path):
    github = FakeGitHub(tmp_path)
    boss = PrBoss(github, {"B1": "B1 PASS\n", "B2": "B1 PASS\nB2 PASS\n"}, merges_itself=True)
    seen: dict = {}
    results = _graded_run(tmp_path, boss, github, seen=seen)
    b1, b2 = results
    # Graded on the PR's head commit, not on the clone's main.
    assert b1["graded_commit"] == seen["B1"]["graded_head"] == b1["pr"]["head_sha"]
    assert b1["graded_commit"] != b1["main_before"] and seen["B1"]["ledger"] == "B1 PASS\n"
    assert b1["evaluation"]["solved"] and b1["pr"]["number"] == 1
    # Accepted through BOSS's own merge decision, approved by the scripted answer after grading.
    assert b1["merge"]["merged"] and b1["merge"]["by"] == "boss" and b1["merge"]["decision_answered"] == "approve"
    answer = [c for c in boss.calls if c[0] == "answer" and c[1].startswith("merge_")][0]
    assert "hidden acceptance suite passed" in answer[3]
    # The merge decision was never answered while BOSS worked: only the spec sign-off was.
    assert [c[1].split("_")[0] for c in boss.calls if c[0] == "answer"] == ["spec", "merge", "spec", "merge"]
    # The next task starts from the merged main.
    assert b2["main_before"] == b1["main_after"] == b1["merge"]["merge_commit"]
    assert seen["started_from"]["B2"] == "B1 PASS\n"
    assert github.file_on_main() == "B1 PASS\nB2 PASS"
    saved = json.loads((tmp_path / "out/E2/A/B1-r1.json").read_text())
    assert saved["graded_commit"] == b1["graded_commit"] and saved["merge"]["merged"] is True


def test_a_failed_task_is_not_merged_and_the_next_starts_from_unchanged_main(tmp_path):
    github = FakeGitHub(tmp_path)
    snapshot = github.main()
    boss = PrBoss(github, {"B1": "B1 broke the rules\n", "B2": "B2 PASS\n"}, merges_itself=True)
    seen: dict = {}
    b1, b2 = _graded_run(tmp_path, boss, github, seen=seen)
    assert not b1["evaluation"]["solved"] and b1["graded_commit"] == b1["pr"]["head_sha"]
    assert b1["merge"] == {"merged": False, "decision_id": b1["merge_decision_id"], "decision_answered": None,
                           "why": "the hidden suite did not pass on the graded commit; main is unchanged"}
    assert not [c for c in boss.calls if c[0] == "answer" and c[1] == b1["merge_decision_id"]]
    assert b1["main_after"] == snapshot == b2["main_before"]
    assert seen["started_from"]["B2"] == "v0\n"                  # B2 never sees B1's rejected work
    assert b2["merge"]["merged"] and github.file_on_main() == "B2 PASS"


def test_when_boss_does_not_merge_the_harness_merges_the_graded_commit_and_says_so(tmp_path):
    github = FakeGitHub(tmp_path)
    boss = PrBoss(github, {"B1": "B1 PASS\n"}, merges_itself=False)       # approval recorded, nobody merges
    (b1,) = _graded_run(tmp_path, boss, github, tasks="B1")
    assert b1["merge"]["merged"] and b1["merge"]["by"] == "harness (gh)"
    assert "had not merged within 10 min" in b1["merge"]["why"] and b1["merge"]["decision_answered"] == "approve"
    assert github.merges == [("harness", 1, b1["graded_commit"])]          # exactly the commit it graded
    (tmp_path / "second").mkdir()
    no_decision = FakeGitHub(tmp_path / "second")
    (only,) = _graded_run(tmp_path / "second", PrBoss(no_decision, {"B1": "PASS\n"}, merge_decision=False),
                          no_decision, tasks="B1")
    assert only["merge"]["by"] == "harness (gh)" and only["merge"]["why"] == "the run carries no merge decision"


def test_no_pull_request_means_unsolved_and_nothing_is_graded(tmp_path):
    github = FakeGitHub(tmp_path)
    graded: dict = {}
    (b1,) = _graded_run(tmp_path, PrBoss(github, {"B1": None}), github, tasks="B1", seen=graded)
    assert b1["status"] == "failed" and b1["graded_commit"] is None and "B1" not in graded
    assert b1["evaluation"] == {"solved": False, "reason": "BOSS opened no pull request: nothing to grade"}
    assert b1["merge"]["merged"] is False and github.merges == []


def test_a_pr_found_only_by_its_head_branch_is_still_graded(tmp_path):
    github = FakeGitHub(tmp_path)
    number = github.open_pr("boss/code/run_7", "PASS\n")
    built = {"pr_number": None, "head_branch": "boss/code/run_7", "merge_decision_id": None}
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(github.bare), str(clone))
    out = run_py.grade_and_merge(FakeBoss(), github, run_py.GitWorkspace(clone), run_py.load_task("B1"), built,
                                 repo="paramg/northpeak-fineract-a", answers=run_py.load_answers(),
                                 evaluate=lambda t: {"solved": True})
    assert out["pr_found_by"] == "head branch" and out["pr"]["number"] == number
    assert out["graded_commit"] == github.pull_request("r", number)["head_sha"] and out["merge"]["merged"]


def test_a_pr_that_carries_a_hidden_suite_is_unsolved(tmp_path):
    github = FakeGitHub(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(github.bare), str(clone))
    _git(github.work, "checkout", "--quiet", "-B", "boss/code/leak", "origin/main")
    (github.work / "CaseStudyHiddenB1ContractTest.java").write_text("class X {}")
    _git(github.work, "add", "-A")
    _git(github.work, "commit", "--quiet", "-m", "leak")
    _git(github.work, "push", "--quiet", "origin", "HEAD:refs/pull/1/head")
    github.prs[1] = {"head_ref": "boss/code/leak", "merged": False, "merge_commit": None}
    out = run_py.grade_and_merge(FakeBoss(), github, run_py.GitWorkspace(clone), run_py.load_task("B1"),
                                 {"pr_number": 1}, repo="r/x", answers=run_py.load_answers(),
                                 evaluate=lambda t: {"solved": True})
    assert not out["evaluation"]["solved"] and "hidden suite present" in out["evaluation"]["reason"]
    assert not out["merge"]["merged"]


def test_a_fault_release_is_pushed_onto_main_once(tmp_path):
    github = FakeGitHub(tmp_path)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(github.bare), str(clone))
    for key, value in (("user.name", "Case Study"), ("user.email", "case-study@example.invalid")):
        _git(clone, "config", key, value)
    patch = tmp_path / "fault.patch"
    patch.write_text("diff --git a/ledger.txt b/ledger.txt\n--- a/ledger.txt\n+++ b/ledger.txt\n@@ -1 +1 @@\n-v0\n+v0 FAULT\n")
    task = run_py.Task(id="B3", title="t", brief="", repo="fineract", evaluate={}, fault_patch=patch)
    run_py.prepare_release(clone, task)
    released = github.main()
    assert github.file_on_main() == "v0 FAULT"
    run_py.prepare_release(clone, task)                        # a resumed run: already released, nothing pushed
    assert github.main() == released


def test_boss_gets_the_repository_as_owner_and_bare_name():
    """POST /v1/code-tasks takes `owner` and `repo` (the bare name) separately and clones github.com/<owner>/<repo>
    (apps/api/app/main.py start_code_task_endpoint; request_bodies.CodeTaskStartBody). Sending 'owner/name' as
    `repo` would clone the deployment's default owner's 'owner/name'."""
    boss = FakeBoss()
    task = run_py.load_task("B1")
    run_py.drive(boss, task, project_id="p", repo="paramg/northpeak-fineract-ap", engine={"engine": "e", "model": "m"},
                 answers=run_py.load_answers(), sleep=lambda s: None)
    assert boss.bodies[0]["owner"] == "paramg" and boss.bodies[0]["repo"] == "northpeak-fineract-ap"
    for kind in ("fineract", "saleor", "fulfilment"):
        for arms in run_py.load_config()[kind]["forks"].values():
            for fork in arms.values():
                assert run_py.split_repo(fork["repo"])[0] == "paramg" and fork["project"].startswith("northpeak-")
    with pytest.raises(run_py.HarnessError, match="not a GitHub owner/name"):
        run_py.split_repo("northpeak-saleor")


def test_a_run_finds_its_arms_project_by_slug(tmp_path):
    boss = FakeBoss()
    results = _run(tmp_path, boss, stage="bank_smoke", engine="E2", arm="B", tasks="B1", project_id=None,
                   fineract_dir=str(tmp_path / "f"))
    assert results[0]["project_id"] == "proj_bank_b"
    with pytest.raises(run_py.HarnessError, match="no project 'nowhere'"):
        _run(tmp_path, FakeBoss(), stage="bank_smoke", engine="E2", arm="B", tasks="B1", project_id="nowhere",
             fineract_dir=str(tmp_path / "f"), out=str(tmp_path / "o4"))


# --- setup ---------------------------------------------------------------------------------------------------------
class SetupBoss:
    """BOSS's project, settings and repository endpoints, with or without a project-creation route."""

    def __init__(self, *, projects=(), can_create=True, reachable=(), choices=("on", "keyword", "off")):
        self.projects_ = [dict(p) for p in projects]
        self.can_create, self.choices = can_create, list(choices)
        self.reachable = set(reachable)
        self.registry_rows: dict[str, dict] = {}
        self.bindings: dict[str, set] = {}
        self.settings_: dict[tuple, str] = {}
        self.writes: list[tuple] = []

    def projects(self):
        return self.projects_

    def routes(self):
        return {"/v1/projects": {"get": {}, **({"post": {}} if self.can_create else {})}}

    def create_project(self, name, slug):
        self.writes.append(("project", slug))
        self.projects_.append({"id": f"proj_{slug}", "slug": slug, "name": name})
        return {"id": f"proj_{slug}"}

    def settings(self, project_id):
        value = self.settings_.get((project_id, "knowledge_graph"))
        return {"knobs": [{"key": "knowledge_graph", "choices": self.choices, "value": value or "on",
                           "source": "project" if value else "deployment default"}]}

    def set_setting(self, project_id, key, value):
        self.writes.append(("setting", project_id, value))
        self.settings_[(project_id, key)] = value

    def refresh_repos(self, project_id):
        self.writes.append(("refresh",))
        for name in self.reachable:
            self.registry_rows.setdefault(name.lower(), {"id": f"ghr_{name.split('/')[1]}", "full_name": name,
                                                         "allowed": False, "state": "not_allowed"})

    def registry(self, project_id):
        return {"items": list(self.registry_rows.values()),
                "connection": {"app_slug": "boss-localhost", "change_url": "https://github.com/settings/installations/1"}}

    def allow_repo(self, project_id, repo_id):
        self.writes.append(("allow", repo_id))
        row = next(r for r in self.registry_rows.values() if r["id"] == repo_id)
        row.update(allowed=True, state="working")
        return row

    def project_repos(self, project_id):
        return {"items": [{"full_name": n} for n in self.bindings.get(project_id, set())]}

    def add_project_repo(self, project_id, repo_id, base_branch="main"):
        self.writes.append(("bind", project_id, repo_id))
        row = next(r for r in self.registry_rows.values() if r["id"] == repo_id)
        self.bindings.setdefault(project_id, set()).add(row["full_name"])


class SetupGitHub:
    """GitHub for setup: repositories are bare repositories under a folder."""

    def __init__(self, root: Path, existing=()):
        self.root = root
        self.created: list[str] = []
        for repo in existing:
            _git(root, "init", "--quiet", "--bare", "-b", "main", str(self._path(repo)))
            seed = root / "seed" / repo.replace("/", "_")
            _git(root, "init", "--quiet", "-b", "main", str(seed))
            (seed / "README.md").write_text("# existing\n")
            _git(seed, "add", "-A")
            _git(seed, "commit", "--quiet", "-m", "existing")
            _git(seed, "push", "--quiet", str(self._path(repo)), "main")

    def _path(self, repo):
        return self.root / "gh" / f"{repo}.git"

    def repo(self, repo):
        return {"private": True, "default_branch": "main"} if self._path(repo).exists() else None

    def create_private(self, repo, description):
        self.created.append(repo)
        self._path(repo).parent.mkdir(parents=True, exist_ok=True)
        _git(self.root, "init", "--quiet", "--bare", "-b", "main", str(self._path(repo)))

    def clone_url(self, repo):
        return str(self._path(repo))


def _upstream(tmp_path) -> tuple[str, str]:
    """A stand-in for apache/fineract: a README, a LICENSE and a NOTICE at one commit."""
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "--quiet", "-b", "develop", str(up))
    (up / "README.md").write_text("# Apache Fineract\n\nCore banking.\n")
    (up / "LICENSE_SOURCE").write_text("Apache License 2.0\n")
    (up / "NOTICE_SOURCE").write_text("Apache Fineract\n")
    _git(up, "add", "-A")
    _git(up, "commit", "--quiet", "-m", "1.15.0")
    return f"file://{up}", _git(up, "rev-parse", "HEAD")


BANK_REPOS = ["paramg/northpeak-fineract-a", "paramg/northpeak-fineract-ap", "paramg/northpeak-fineract-b"]


def _setup(tmp_path, boss, github, *, dry_run=False, owner="paramg", snapshot=None):
    lines: list[str] = []
    args = SimpleNamespace(set="banking", github_owner=owner, engine=None, dry_run=dry_run,
                           boss_url="http://boss.invalid", clone_dir=str(tmp_path / "clones"))
    steps = run_py.setup(args, client=boss, host=github, out=lines.append, snapshot=snapshot)
    return steps, lines


def test_setup_builds_repos_projects_switches_and_clones_and_a_rerun_changes_nothing(tmp_path, monkeypatch):
    url, sha = _upstream(tmp_path)
    config = run_py.load_config()
    config["fineract"]["upstream"], config["fineract"]["sha"] = url, sha
    monkeypatch.setattr(run_py, "load_config", lambda: config)
    for key, value in (("GIT_AUTHOR_NAME", "Case Study"), ("GIT_COMMITTER_NAME", "Case Study"),
                       ("GIT_AUTHOR_EMAIL", "cs@example.invalid"), ("GIT_COMMITTER_EMAIL", "cs@example.invalid")):
        monkeypatch.setenv(key, value)
    github = SetupGitHub(tmp_path, existing=BANK_REPOS[:1])      # arm A's repository is already there
    boss = SetupBoss(reachable=BANK_REPOS)
    snapshots = tmp_path / "snap"
    steps, _ = _setup(tmp_path, boss, github,
                      snapshot=lambda kind: run_py.build_snapshot(kind, config, snapshots))
    assert github.created == BANK_REPOS[1:]                     # never recreates one that exists
    tree = tmp_path / "check"
    _git(tmp_path, "clone", "--quiet", github.clone_url(BANK_REPOS[1]), str(tree))
    assert _git(tree, "log", "--format=%s").splitlines() == ["NorthPeak core banking, based on Apache Fineract 1.15.0"]
    assert (tree / "LICENSE_SOURCE").exists() and (tree / "NOTICE_SOURCE").exists()
    readme = (tree / "README.md").read_text()
    assert readme.startswith("# Apache Fineract\n\n> **Demo fork of Apache Fineract 1.15.0") and readme.endswith("Core banking.\n")
    assert {p["slug"] for p in boss.projects_} == {"northpeak-bank-a", "northpeak-bank-ap", "northpeak-bank-b"}
    assert {(pid, v) for (pid, _), v in boss.settings_.items()} == {
        ("proj_northpeak-bank-a", "off"), ("proj_northpeak-bank-ap", "keyword"), ("proj_northpeak-bank-b", "on")}
    assert boss.bindings == {f"proj_northpeak-bank-{s}": {f"paramg/northpeak-fineract-{s}"} for s in ("a", "ap", "b")}
    assert all((tmp_path / "clones" / r.split("/")[1] / ".git").exists() for r in BANK_REPOS)
    assert not [s for s in steps if s["status"] in ("operator", "blocked")]
    # Rerun: everything is reported in place and nothing is written (but the App's list is re-read).
    boss.writes.clear()
    again, _ = _setup(tmp_path, boss, github)
    assert {s["status"] for s in again} == {"ok"} and boss.writes == [("refresh",)]


def test_setup_names_the_operator_step_when_the_github_app_cannot_reach_a_repo(tmp_path):
    github = SetupGitHub(tmp_path, existing=BANK_REPOS)
    boss = SetupBoss(projects=[{"id": f"p_{s}", "slug": f"northpeak-bank-{s}"} for s in ("a", "ap", "b")],
                     reachable=BANK_REPOS[:2])
    steps, lines = _setup(tmp_path, boss, github)
    blocked = [s for s in steps if s["status"] == "operator"]
    assert [s["target"] for s in blocked] == ["paramg/northpeak-fineract-b"]
    assert ("GitHub › Settings › Applications › Installed GitHub Apps › boss-localhost › Configure › Repository access"
            in blocked[0]["detail"]) and "add paramg/northpeak-fineract-b" in blocked[0]["detail"]
    assert "Needs the operator before a run: app paramg/northpeak-fineract-b" in lines[-1]


def test_setup_dry_run_only_reads_and_says_what_a_boss_without_project_creation_blocks(tmp_path):
    github = SetupGitHub(tmp_path)
    boss = SetupBoss(can_create=False, reachable=BANK_REPOS)
    steps, lines = _setup(tmp_path, boss, github, dry_run=True)
    assert boss.writes == [] and github.created == [] and not (tmp_path / "clones").exists()
    assert [s["status"] for s in steps if s["step"] == "repo"] == ["would"] * 3
    projects = [s for s in steps if s["step"] == "project"]
    assert {s["status"] for s in projects} == {"blocked"} and "no POST /v1/projects" in projects[0]["detail"]
    assert lines[0].endswith("--dry-run (reads only)")


def test_setup_refuses_an_owner_harness_yaml_does_not_name():
    with pytest.raises(run_py.HarnessError, match="not --github-owner someone-else"):
        run_py.setup_targets(run_py.load_config(), "banking", "someone-else", None)
    retail = run_py.setup_targets(run_py.load_config(), "retail", "paramg", "E2")
    assert {(t["kind"], t["arm"], t["project"]) for t in retail} == {
        ("saleor", "A", "northpeak-retail-a"), ("saleor", "B", "northpeak-retail-b"),
        ("fulfilment", "A", "northpeak-retail-a"), ("fulfilment", "B", "northpeak-retail-b")}
