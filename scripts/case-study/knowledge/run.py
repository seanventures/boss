#!/usr/bin/env python3
"""Knowledge Graph case-study harness (docs/product/KNOWLEDGE_GRAPH_CASE_STUDY.md §4, §6, §7).

Two task sets share it: NorthPeak Retail on Saleor (T1..T12) and the banking set on Apache
Fineract (B1..B4, docs/product/KNOWLEDGE_GRAPH_BANKING_CASE_STUDIES.md cases 1 and 3).

It drives BOSS ONLY through its public HTTP API, as an operator would: code tasks, decisions,
settings, budget and llm-calls. It never imports BOSS internals, so the run shows the real
product. Results go to artifacts/case-study/knowledge/<date>/ (gitignored); report.py turns
them into report.md.

    run.py estimate --stage smoke
    run.py validate-suites --saleor-dir PATH [--tasks T1,T4]    # no model spend
    run.py validate-suites --fineract-dir PATH --tasks B1..B4  # no model spend
    run.py run --stage smoke --arm A --engine E1 --tasks T1..T4 [--repeat 1] [--resume]
               --approved-by NAME --boss-url URL --project-id ID --saleor-dir PATH --fulfilment-dir PATH

`run` spends model credit. It refuses unless:
- the operator has approved the named stage (`--approved-by`, recorded in every result);
- the tasks, arm and engine are inside that stage; and
- BOSS reserves the stage's budget ceiling (POST /v1/budget/reserve).

Credentials come from the environment, never from files in this tree: BOSS_API_KEY (or
BOSS_CASE_STUDY_API_KEY), and GitHub push access for the forks through the local git config.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
TASKS_DIR = HERE / "tasks"
ARTIFACTS = REPO / "artifacts" / "case-study" / "knowledge"
TERMINAL = {"completed", "failed", "cancelled", "simulated"}
KNOWLEDGE_SETTING = "knowledge_graph"          # the per-project switch (project_settings.KNOBS): on | keyword | off
CONTROL_ARM = "A"                              # runs as BOSS is, with or without the switch


class HarnessError(RuntimeError):
    pass


class NeedsOperator(HarnessError):
    """A decision the scripted answers do not cover: stop, never guess."""


# --- configuration -----------------------------------------------------------------------------
def load_config() -> dict:
    return yaml.safe_load((HERE / "harness.yaml").read_text())


def load_answers() -> dict:
    return yaml.safe_load((HERE / "answers.yaml").read_text())


def expand_tasks(spec: str) -> list[str]:
    """'T1..T4', 'B1..B4' or 'T1,T3' -> ['T1', ...]."""
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"([A-Z])(\d+)\.\.\1(\d+)", part)
        out += [f"{m[1]}{i}" for i in range(int(m[2]), int(m[3]) + 1)] if m else [part]
    return out


@dataclass
class Task:
    id: str
    title: str
    brief: str
    repo: str
    evaluate: dict
    fault_patch: Path | None = None
    dir: Path = field(default_factory=Path)
    meta: dict = field(default_factory=dict)


def load_task(task_id: str) -> Task:
    d = TASKS_DIR / task_id
    if not (d / "task.yaml").exists():
        raise HarnessError(f"{task_id}: no task folder yet (T1–T4 are built in Phase 0)")
    meta = yaml.safe_load((d / "task.yaml").read_text())
    return Task(id=meta["id"], title=meta["title"], brief=(d / "brief.md").read_text(), repo=meta["repo"],
                evaluate=meta["evaluate"], fault_patch=(d / meta["fault_patch"]) if meta.get("fault_patch") else None,
                dir=d, meta=meta)


def stage_builds(config: dict, stage: str) -> list[tuple[str, str, str, int]]:
    """Every (engine, arm, task, repeat) build a stage runs."""
    s = config["stages"][stage]
    builds = []
    for engine in s["engines"]:
        for arm in s["arms"]:
            for task in s["tasks"]:
                total = s.get("repeats", 1) + s.get("extra_repeats", {}).get(task, 0)
                builds += [(engine, arm, task, r) for r in range(1, total + 1)]
    return builds


def estimate(config: dict, stage: str) -> dict:
    builds = stage_builds(config, stage)
    engines = config["engines"]
    lo = sum(engines[e]["build_estimate_usd"]["low"] for e, *_ in builds)
    hi = sum(engines[e]["build_estimate_usd"]["high"] for e, *_ in builds)
    ceiling = sum(engines[e]["build_ceiling_usd"] for e, *_ in builds)
    return {"stage": stage, "builds": len(builds), "usd_low": round(lo, 2), "usd_high": round(hi, 2),
            "ceiling_usd": round(ceiling, 2)}


# --- BOSS's public API --------------------------------------------------------------------------
class BossClient:
    """The few public endpoints the harness uses (apps/api)."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 60):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> Any:
        url = self.base + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Boss-Api-Key": self.key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]
            raise HarnessError(f"{method} {path} -> {err.code}: {detail}") from err

    # settings / budget
    def set_setting(self, project_id: str, key: str, value: str) -> Any:
        return self.request("PUT", f"/v1/settings/{key}", {"project_id": project_id, "value": value,
                                                          "actor": "case-study-harness"})

    def reserve_budget(self, project_id: str, usd: float, reason: str) -> Any:
        return self.request("POST", "/v1/budget/reserve", {
            "project_id": project_id, "category": "case_study", "amount_usd_micros": int(usd * 1_000_000),
            "reason": reason, "expires_in_minutes": 24 * 60})

    # work
    def start_code_task(self, body: dict) -> Any:
        return self.request("POST", "/v1/code-tasks", body)

    def code_task(self, run_id: str) -> Any:
        return self.request("GET", f"/v1/code-tasks/{run_id}")

    def decision(self, decision_id: str) -> Any:
        return self.request("GET", f"/v1/decisions/{decision_id}")

    def answer_decision(self, decision_id: str, verb: str, note: str) -> Any:
        return self.request("POST", f"/v1/decisions/{decision_id}/{verb}", {"actor": "case-study-harness", "note": note})

    def llm_calls(self, project_id: str, run_id: str) -> list[dict]:
        items, offset = [], 0
        while True:
            page = self.request("GET", "/v1/llm-calls", query={"project_id": project_id, "code_task_run_id": run_id,
                                                               "limit": 200, "offset": offset})
            batch = page.get("items", page if isinstance(page, list) else [])
            items += batch
            if len(batch) < 200:
                return items
            offset += 200

    def retrospective(self, run_id: str) -> Any:
        return self.request("GET", f"/v1/code-tasks/{run_id}/retrospective")


# --- scripted approvals -------------------------------------------------------------------------
def answer_for(card: dict, answers: dict) -> tuple[str, str]:
    """The scripted human: the same rule answers the same decision in both arms."""
    kind = card.get("kind") or card.get("decision_type") or card.get("type")
    for rule in answers.get("rules", []):
        if rule["match"].get("kind") == kind:
            verb = {"approve": "approve", "reject": "reject", "reply": "approve"}[rule["answer"]]
            return verb, rule["reason"]
    raise NeedsOperator(f"decision {card.get('id')} ({kind or 'no kind'}): {str(card.get('question'))[:160]}")


# --- one build ----------------------------------------------------------------------------------
def usage_totals(calls: list[dict]) -> dict:
    keys = ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens", "turns")
    out = {k: sum(int(c.get(k) or 0) for c in calls) for k in keys}
    out["calls"] = len(calls)
    out["usd"] = round(sum(int(c.get("cost_usd_micros") or 0) for c in calls) / 1e6, 4)
    return out


def drive(client: BossClient, task: Task, *, project_id: str, repo: str, engine: dict, answers: dict,
          poll_s: float = 15, deadline_s: float = 4 * 3600, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Start the task as a code task and see it to a terminal state, answering decisions by script."""
    started = time.monotonic()
    run = client.start_code_task({
        "project_id": project_id, "title": f"{task.id}: {task.title}", "instructions": task.brief,
        "repo": repo, "base_branch": "main", "open_pr": True,
        "model_config": {"engine": engine["engine"], "model": engine["model"]}})
    run_id = run["run_id"]
    answered: list[dict] = []
    while True:
        state = client.code_task(run_id)
        decision_id = state.get("decision_id")
        if decision_id and decision_id not in {a["id"] for a in answered}:
            card = client.decision(decision_id)
            if card.get("status") == "pending":
                verb, note = answer_for(card, answers)
                client.answer_decision(decision_id, verb, note)
                answered.append({"id": decision_id, "verb": verb})
        if state.get("status") in TERMINAL:
            break
        if time.monotonic() - started > deadline_s:
            raise HarnessError(f"{task.id}: run {run_id} did not finish in {deadline_s / 3600:.0f} h")
        sleep(poll_s)
    calls = client.llm_calls(project_id, run_id)
    retro = {}
    try:
        retro = client.retrospective(run_id)
    except HarnessError:
        pass
    return {"run_id": run_id, "status": state.get("status"), "pr_url": state.get("pull_request_url"),
            "decisions": answered, "usage": usage_totals(calls),
            "wall_minutes": round(retro.get("elapsed_minutes") or (time.monotonic() - started) / 60, 1)}


# --- evaluation (hidden suites) -----------------------------------------------------------------
def pytest_counts(output: str) -> tuple[int, int]:
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", output))
    failed = sum(int(n) for n in re.findall(r"(\d+) (?:failed|error)", output))
    return passed, passed + failed


def collection_error(output: str) -> bool:
    """A suite that cannot even be imported proves nothing: its failure must not count as valid."""
    return bool(re.search(r"ERROR collecting|ImportError while importing|errors? during collection", output))


def evaluate_saleor(task: Task, saleor_dir: Path, *, pytest_cmd: list[str] | None = None) -> dict:
    """Copy the hidden suite into the checked-out fork, run it with Saleor's pytest, remove it."""
    target = saleor_dir / task.evaluate["install_as"]
    shutil.copyfile(task.dir / task.evaluate["suite"], target)
    try:
        cmd = (pytest_cmd or ["uv", "run", "pytest", "-q", "-p", "no:cacheprovider", "--reuse-db"]) + [str(target)]
        proc = subprocess.run(cmd, cwd=saleor_dir, capture_output=True, text=True, timeout=3600)
        output = proc.stdout + proc.stderr
        passed, total = pytest_counts(output)
        return {"solved": proc.returncode == 0 and total > 0, "tests_passed": passed, "tests_total": total,
                "collection_error": collection_error(output), "tail": output[-1500:]}
    finally:
        target.unlink(missing_ok=True)


def evaluate_service(task: Task, repo_dir: Path, *, scratch_dsn: str, secret: str, port: int = 18080) -> dict:
    """Build the service's Dockerfile, run it against a scratch Postgres, run the HTTP suite."""
    tag = f"northpeak-casestudy-{task.id.lower()}"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(repo_dir)], check=True, capture_output=True, timeout=1800)
    name = f"{tag}-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "-e", f"PORT={port}",
                    "-e", f"DATABASE_URL={scratch_dsn}", "-e", f"NORTHPEAK_WEBHOOK_SECRET={secret}", tag],
                   check=True, capture_output=True)
    try:
        env = {**os.environ, "NORTHPEAK_URL": f"http://127.0.0.1:{port}", "NORTHPEAK_DB_DSN": scratch_dsn,
               "NORTHPEAK_WEBHOOK_SECRET": secret}
        for _ in range(60):
            if subprocess.run(["curl", "-fsS", f"{env['NORTHPEAK_URL']}/healthz"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               str(task.dir / task.evaluate["suite"])], env=env, capture_output=True, text=True,
                              timeout=900)
        passed, total = pytest_counts(proc.stdout + proc.stderr)
        return {"solved": proc.returncode == 0 and total > 0, "tests_passed": passed, "tests_total": total,
                "tail": (proc.stdout + proc.stderr)[-1500:]}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _junit_class(install_to: str) -> str | None:
    """'fineract-loan/src/test/java/org/x/FooTest.java' -> 'org.x.FooTest' (None for a resource)."""
    m = re.search(r"src/test/java/(.+)\.java$", install_to)
    return m[1].replace("/", ".") if m else None


def junit_reports(results_dir: Path, classes: list[str]) -> list[Path]:
    """The JUnit XML files a previous run left for these classes."""
    if not results_dir.exists():
        return []
    return [r for r in results_dir.glob("TEST-*.xml") if ET.parse(r).getroot().get("name") in classes]


def junit_counts(results_dir: Path, classes: list[str]) -> dict[str, dict]:
    """Per test class: tests, failures (assertions) and errors (exceptions) from Gradle's JUnit XML. Reports are
    matched by the suite name inside, since Gradle shortens long report file names."""
    out = {}
    for report in sorted(results_dir.glob("TEST-*.xml")) if results_dir.exists() else []:
        suite = ET.parse(report).getroot()
        cls = suite.get("name")
        if cls not in classes:
            continue
        tests, failures, errors = (int(suite.get(k, 0)) for k in ("tests", "failures", "errors"))
        out[cls] = {"tests": tests, "failures": failures, "errors": errors, "passed": tests - failures - errors}
    return out


def evaluate_fineract(task: Task, fineract_dir: Path, *, gradle_cmd: str | None = None, timeout: int = 3600) -> dict:
    """Install the hidden suite (tests and golden files) into the checkout, run the narrowest Gradle test task that
    covers it, read the JUnit XML, then remove every installed file.

    A suite that does not compile proves nothing, so `collection_error` is set and a validator never counts it as
    a valid failure."""
    ev = task.evaluate
    module, pattern = ev["module"], ev["tests"]
    template = gradle_cmd or load_config()["fineract"]["test_command"]
    cmd = template.format(module=module, tests=pattern)
    installed = []
    for item in ev["install"]:
        target = fineract_dir / item["to"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(task.dir / item["from"], target)
        installed.append(target)
    classes = [c for c in (_junit_class(i["to"]) for i in ev["install"]) if c]
    results_dir = fineract_dir / module / "build" / "test-results" / "test"
    for stale in junit_reports(results_dir, classes):
        stale.unlink()
    env = {**os.environ}
    if os.environ.get("FINERACT_JAVA_HOME"):
        env["JAVA_HOME"] = os.environ["FINERACT_JAVA_HOME"]
    try:
        started = time.monotonic()
        proc = subprocess.run(cmd, shell=True, cwd=fineract_dir, capture_output=True, text=True, timeout=timeout, env=env)
        output = proc.stdout + proc.stderr
        by_class = junit_counts(results_dir, classes)
    finally:
        for target in installed:
            target.unlink(missing_ok=True)
    passed = sum(c["passed"] for c in by_class.values())
    total = sum(c["tests"] for c in by_class.values())
    compile_failed = not by_class and bool(re.search(r"compile\w*Java FAILED|Compilation failed|error: ", output))
    return {"solved": proc.returncode == 0 and total > 0 and passed == total, "tests_passed": passed, "tests_total": total,
            "collection_error": compile_failed or (proc.returncode != 0 and not by_class),
            "by_class": by_class, "seconds": round(time.monotonic() - started, 1), "tail": output[-1500:]}


# --- fork preparation ---------------------------------------------------------------------------
def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def prepare_saleor(saleor_dir: Path, sha: str, task: Task) -> None:
    """The fork's main is the pinned SHA plus earlier tasks' merged work; a fault task pushes the faulty release."""
    if task.fault_patch:
        git(saleor_dir, "apply", "--check", str(task.fault_patch))
        git(saleor_dir, "apply", str(task.fault_patch))
        git(saleor_dir, "commit", "-am", f"release: {task.id} (case-study controlled fault)")
        git(saleor_dir, "push", "origin", "HEAD:main")


prepare_fineract = prepare_saleor      # same rule for the banking fork: B3/B4 push their faulty release

#: Hidden-suite file names, per task set. A fork that holds one has leaked the answer key.
HIDDEN_PATTERNS = ("test_hidden_t*.py", "CaseStudyHidden*.java", "b[0-9]_*.golden")


def hidden_suites_absent(repo_dir: Path) -> None:
    """Hidden suites are never shown to agents: refuse a fork that contains one."""
    hits = [h for pattern in HIDDEN_PATTERNS for h in repo_dir.rglob(pattern)] if repo_dir.exists() else []
    if hits:
        raise HarnessError(f"hidden suite present in the target repository: {hits[0]}")


def repo_target(config: dict, task: Task, args) -> tuple[Path, str]:
    """The local checkout and the BOSS repository name a task works in."""
    if task.repo == "saleor":
        return Path(args.saleor_dir), config["saleor"]["fork_name"]
    if task.repo == "fineract":
        if not getattr(args, "fineract_dir", None):
            raise HarnessError(f"{task.id} works in the Fineract fork: pass --fineract-dir")
        fork = (config["fineract"].get("forks") or {}).get(args.engine, {}).get(args.arm)
        if not fork:
            raise HarnessError(f"no Fineract demo repository for {args.engine}/{args.arm} in harness.yaml "
                               "fineract.forks; nothing was started")
        return Path(args.fineract_dir), fork["repo"]
    return Path(args.fulfilment_dir or "."), config["fulfilment"]["repo_name"]


# --- run ----------------------------------------------------------------------------------------
def result_path(out_dir: Path, engine: str, arm: str, task: str, repeat: int) -> Path:
    return out_dir / engine / arm / f"{task}-r{repeat}.json"


def check_approval(config: dict, args) -> dict:
    if not args.approved_by:
        raise HarnessError("model spend needs the operator's approval of a stage: pass --approved-by NAME "
                           "(run `run.py estimate --stage STAGE` first). Nothing was started.")
    stage = config["stages"].get(args.stage)
    if stage is None:
        raise HarnessError(f"unknown stage {args.stage!r}")
    tasks = expand_tasks(args.tasks)
    outside = [t for t in tasks if t not in stage["tasks"]]
    if outside or args.arm not in stage["arms"] or args.engine not in stage["engines"]:
        raise HarnessError(f"stage {args.stage} does not cover {args.engine}/{args.arm}/{outside or tasks}; "
                           "nothing was started")
    return estimate(config, args.stage)


def run(args, *, client: BossClient | None = None, evaluator: Callable[[Task], dict] | None = None,
        prepare: Callable[[Task], None] | None = None, sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    config, answers = load_config(), load_answers()
    est = check_approval(config, args)
    for task_id in expand_tasks(args.tasks):         # every fork known before any setting or budget moves
        planned = load_task(task_id)
        if planned.repo == "fineract":
            repo_target(config, planned, args)
    client = client or BossClient(args.boss_url, os.environ.get("BOSS_CASE_STUDY_API_KEY") or os.environ["BOSS_API_KEY"])
    # Arm switch through BOSS's own settings: arm B on, arm A (the control) off, arm AP (A′, full-text keyword
    # search only) "keyword". A BOSS without the switch or the value refuses it; then that arm cannot run and
    # says so, and arm A runs as that BOSS is.
    value = config["arms"][args.arm]["knowledge"]
    try:
        client.set_setting(args.project_id, KNOWLEDGE_SETTING, value)
    except HarnessError as exc:
        if args.arm != CONTROL_ARM:
            raise HarnessError(f"arm {args.arm} needs the {KNOWLEDGE_SETTING!r} project setting with the value {value!r}, "
                               f"which this BOSS does not have yet ({exc}). Nothing was started.") from exc
    reservation = client.reserve_budget(args.project_id, est["ceiling_usd"],
                                        f"case study {args.stage}: approved by {args.approved_by}")
    if reservation.get("status") not in ("reserved", "ok", "approved"):
        raise HarnessError(f"BOSS refused the stage budget: {reservation}. Nothing was started.")
    out_dir = Path(args.out) if args.out else ARTIFACTS / dt.date.today().isoformat()
    engine = config["engines"][args.engine]
    results = []
    for task_id in expand_tasks(args.tasks):
        task = load_task(task_id)
        for repeat in range(1, args.repeat + 1):
            path = result_path(out_dir, args.engine, args.arm, task_id, repeat)
            if args.resume and path.exists():
                results.append(json.loads(path.read_text()))
                continue
            repo_dir, repo_name = repo_target(config, task, args)
            hidden_suites_absent(repo_dir)
            (prepare or (lambda t: prepare_saleor(repo_dir, config[t.repo]["sha"], t)
                         if t.repo in ("saleor", "fineract") else None))(task)
            record = {"task": task_id, "arm": args.arm, "engine": args.engine, "model": engine["model"],
                      "repeat": repeat, "stage": args.stage, "approved_by": args.approved_by,
                      "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            if task.repo == "fineract":
                record["fineract_sha"] = config["fineract"]["sha"]
                record["fineract_repo"] = repo_name
                record["expected_reuse"] = [i["id"] for i in task.meta.get("expected_reuse", [])]
            else:
                record["saleor_sha"] = config["saleor"]["sha"]
            try:
                record.update(drive(client, task, project_id=args.project_id, repo=repo_name,
                                    engine=engine, answers=answers, sleep=sleep))
                record["evaluation"] = (evaluator or (lambda t: default_evaluator(t, args)))(task)
            except NeedsOperator as exc:
                record.update({"status": "needs_operator", "error": str(exc), "evaluation": {"solved": False}})
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2))
            results.append(record)
            if record["status"] == "needs_operator":
                return results           # stop the sequence: knowledge accumulates in order
    return results


def default_evaluator(task: Task, args) -> dict:
    if task.evaluate["mode"] == "saleor_pytest":
        return evaluate_saleor(task, Path(args.saleor_dir))
    if task.evaluate["mode"] == "fineract_gradle":
        return evaluate_fineract(task, Path(args.fineract_dir), gradle_cmd=getattr(args, "gradle_cmd", None))
    return evaluate_service(task, Path(args.fulfilment_dir), scratch_dsn=os.environ["NORTHPEAK_SCRATCH_DSN"],
                            secret=os.environ.get("NORTHPEAK_WEBHOOK_SECRET", "case-study-secret"))


# --- suite validation (no model spend) -----------------------------------------------------------
class _Patched:
    """Apply a patch to a checkout for the length of a block, and always take it out again."""

    def __init__(self, repo: Path, patch: Path):
        self.repo, self.patch = repo, patch

    def __enter__(self):
        git(self.repo, "apply", str(self.patch))

    def __exit__(self, *exc):
        git(self.repo, "apply", "-R", str(self.patch))


def _fineract_summary(result: dict) -> dict:
    return {k: result[k] for k in ("solved", "tests_passed", "tests_total", "collection_error", "by_class")}


def _assertion_failures(result: dict) -> tuple[int, int]:
    """(failed by assertion, failed by exception) over every class of a result."""
    by_class = result["by_class"].values()
    return sum(c["failures"] for c in by_class), sum(c["errors"] for c in by_class)


def validate_fineract(task: Task, repo: Path, *, gradle_cmd: str | None = None) -> dict:
    """The banking set's rules, all without model spend.

    Fault tasks (B3, B4): the suite passes on the pinned release, fails with the task's fault patch, and fails
    on assertions, never on compilation or an exception. With `distinct_from`, it also passes with the other
    task's fault applied: the two faults are on different code paths.

    Refactor tasks (B1, B2): the behaviour suite passes on the pinned release (behaviour unchanged); every
    contract test fails there on an assertion (the work is not done yet); the reference refactor in selftest/
    passes both, so a refactor that keeps behaviour passes; and each mutant (a one-line behaviour change in the
    rule family) is caught by the behaviour suite."""
    ev = task.evaluate
    run = lambda: evaluate_fineract(task, repo, gradle_cmd=gradle_cmd)      # noqa: E731
    pristine = run()
    entry: dict[str, Any] = {"pristine": _fineract_summary(pristine)}
    if task.fault_patch:
        with _Patched(repo, task.fault_patch):
            faulty = run()
        assertions, exceptions = _assertion_failures(faulty)
        entry["with_fault"] = {**_fineract_summary(faulty), "assertion_failures": assertions, "exception_failures": exceptions}
        valid = (pristine["solved"] and not faulty["solved"] and not faulty["collection_error"]
                 and assertions > 0 and exceptions == 0)
        if task.meta.get("distinct_from"):
            other = load_task(task.meta["distinct_from"])
            with _Patched(repo, other.fault_patch):
                cross = run()
            entry[f"with_{other.id}_fault"] = _fineract_summary(cross)
            valid = valid and cross["solved"]
        entry["valid"] = valid
        return entry
    by_class = pristine["by_class"]
    simple = {cls.rsplit(".", 1)[-1]: counts for cls, counts in by_class.items()}
    behaviour_ok = all(c in simple and simple[c]["tests"] > 0 and simple[c]["passed"] == simple[c]["tests"]
                       for c in ev["behaviour"])
    contract_fails = all(c in simple and simple[c]["tests"] > 0 and simple[c]["failures"] == simple[c]["tests"]
                         for c in ev["contract"])
    entry["behaviour_passes_on_pin"] = behaviour_ok
    entry["contract_fails_on_pin_by_assertion"] = contract_fails
    valid = behaviour_ok and contract_fails and not pristine["collection_error"]
    if ev.get("reference_patch"):
        with _Patched(repo, task.dir / ev["reference_patch"]):
            reference = run()
        entry["reference_refactor"] = _fineract_summary(reference)
        valid = valid and reference["solved"]
    killed = {}
    for mutant in ev.get("mutants", []):
        with _Patched(repo, task.dir / mutant):
            result = run()
        behaviour = {cls.rsplit(".", 1)[-1]: c for cls, c in result["by_class"].items()}
        caught = sum(behaviour[c]["failures"] + behaviour[c]["errors"] for c in ev["behaviour"] if c in behaviour)
        killed[Path(mutant).stem] = {"caught_by": caught, "of": sum(behaviour[c]["tests"] for c in ev["behaviour"] if c in behaviour),
                                     "collection_error": result["collection_error"]}
        valid = valid and caught > 0 and not result["collection_error"]
    if killed:
        entry["mutants"] = killed
    entry["valid"] = valid
    return entry


def validate_suites(args) -> dict:
    """Each hidden suite must FAIL on the pinned SHA (the work is not done yet), and T4's must PASS on
    the pristine SHA and FAIL with the fault applied. T1's suite is proven against the reference
    service in selftest/ (pass) and a variant without idempotency (fail). The banking set's rules are
    in validate_fineract."""
    out = {}
    saleor = Path(args.saleor_dir) if args.saleor_dir else None
    fineract = Path(args.fineract_dir) if getattr(args, "fineract_dir", None) else None
    for task_id in expand_tasks(args.tasks):
        task = load_task(task_id)
        if task.evaluate["mode"] == "fineract_gradle":
            if fineract is None:
                out[task_id] = "skipped: --fineract-dir not given"
                continue
            if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=fineract, capture_output=True,
                              text=True).stdout.strip():
                raise HarnessError(f"{fineract} has local changes: validate on a clean checkout of the pinned tag")
            out[task_id] = validate_fineract(task, fineract, gradle_cmd=getattr(args, "gradle_cmd", None))
            continue
        if task.evaluate["mode"] == "saleor_pytest":
            if saleor is None:
                out[task_id] = "skipped: --saleor-dir not given"
                continue
            pristine = evaluate_saleor(task, saleor, pytest_cmd=args.pytest_cmd.split() if args.pytest_cmd else None)
            keys = ("solved", "tests_passed", "tests_total", "collection_error")
            entry = {"pristine": {k: pristine[k] for k in keys}}
            if task.fault_patch:
                git(saleor, "apply", str(task.fault_patch))
                try:
                    faulty = evaluate_saleor(task, saleor, pytest_cmd=args.pytest_cmd.split() if args.pytest_cmd else None)
                finally:
                    git(saleor, "apply", "-R", str(task.fault_patch))
                entry["with_fault"] = {k: faulty[k] for k in keys}
                entry["valid"] = pristine["solved"] and not faulty["solved"] and not faulty["collection_error"]
            else:
                entry["valid"] = not pristine["solved"] and not pristine["collection_error"] and pristine["tests_total"] > 1
            out[task_id] = entry
        else:
            out[task_id] = "proven by scripts/tests/test_case_study_harness.py against selftest/t1_reference.py"
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("estimate")
    e.add_argument("--stage", default="smoke")
    v = sub.add_parser("validate-suites")
    v.add_argument("--saleor-dir")
    v.add_argument("--tasks", default="T1..T4")
    v.add_argument("--pytest-cmd", help="override, e.g. '.venv/bin/pytest -q --reuse-db'")
    v.add_argument("--fineract-dir", help="a clean checkout of the pinned Fineract tag (harness.yaml)")
    v.add_argument("--gradle-cmd", help="override harness.yaml fineract.test_command; {module} and {tests} are filled in")
    r = sub.add_parser("run")
    r.add_argument("--stage", required=True)
    r.add_argument("--arm", choices=sorted(load_config()["arms"]), required=True)
    r.add_argument("--engine", choices=["E1", "E2"], required=True)
    r.add_argument("--tasks", required=True)
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--resume", action="store_true")
    r.add_argument("--approved-by")
    r.add_argument("--boss-url", default="http://127.0.0.1:8000")
    r.add_argument("--project-id", required=True)
    r.add_argument("--saleor-dir")
    r.add_argument("--fulfilment-dir")
    r.add_argument("--fineract-dir")
    r.add_argument("--gradle-cmd")
    r.add_argument("--out")
    args = p.parse_args(argv)
    try:
        if args.command == "estimate":
            print(json.dumps(estimate(load_config(), args.stage)))
        elif args.command == "validate-suites":
            print(json.dumps(validate_suites(args), indent=2))
        else:
            for record in run(args):
                print(json.dumps({k: record.get(k) for k in ("task", "arm", "engine", "repeat", "status")}))
    except HarnessError as exc:
        print(f"run.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
