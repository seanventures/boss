#!/usr/bin/env python3
"""NorthPeak Retail case-study harness (docs/product/KNOWLEDGE_GRAPH_CASE_STUDY.md §4, §6, §7).

It drives BOSS ONLY through its public HTTP API, as an operator would: code tasks, decisions,
settings, budget and llm-calls. It never imports BOSS internals, so the run shows the real
product. Results go to artifacts/case-study/knowledge/<date>/ (gitignored); report.py turns
them into report.md.

    run.py estimate --stage smoke
    run.py validate-suites --saleor-dir PATH [--tasks T1,T4]    # no model spend
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
KNOWLEDGE_SETTING = "knowledge_graph"          # the per-project switch arm B needs (spec §6, Phase 1+)


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
    """'T1..T4' or 'T1,T3' -> ['T1', ...]."""
    out: list[str] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"T(\d+)\.\.T(\d+)", part)
        out += [f"T{i}" for i in range(int(m[1]), int(m[2]) + 1)] if m else [part]
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


def load_task(task_id: str) -> Task:
    d = TASKS_DIR / task_id
    if not (d / "task.yaml").exists():
        raise HarnessError(f"{task_id}: no task folder yet (T1–T4 are built in Phase 0)")
    meta = yaml.safe_load((d / "task.yaml").read_text())
    return Task(id=meta["id"], title=meta["title"], brief=(d / "brief.md").read_text(), repo=meta["repo"],
                evaluate=meta["evaluate"], fault_patch=(d / meta["fault_patch"]) if meta.get("fault_patch") else None,
                dir=d)


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
    lo, hi = config["build_estimate_usd"]["low"], config["build_estimate_usd"]["high"]
    return {"stage": stage, "builds": len(builds), "usd_low": round(len(builds) * lo, 2),
            "usd_high": round(len(builds) * hi, 2), "ceiling_usd": round(len(builds) * config["build_ceiling_usd"], 2)}


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


# --- fork preparation ---------------------------------------------------------------------------
def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def prepare_saleor(saleor_dir: Path, sha: str, task: Task) -> None:
    """The fork's main is the pinned SHA plus earlier tasks' merged work; T4 pushes the faulty release."""
    if task.fault_patch:
        git(saleor_dir, "apply", "--check", str(task.fault_patch))
        git(saleor_dir, "apply", str(task.fault_patch))
        git(saleor_dir, "commit", "-am", f"release: {task.id} (case-study controlled fault)")
        git(saleor_dir, "push", "origin", "HEAD:main")


def hidden_suites_absent(repo_dir: Path) -> None:
    """Hidden suites are never shown to agents: refuse a fork that contains one."""
    hits = list(repo_dir.rglob("test_hidden_t*.py")) if repo_dir.exists() else []
    if hits:
        raise HarnessError(f"hidden suite present in the target repository: {hits[0]}")


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
    client = client or BossClient(args.boss_url, os.environ.get("BOSS_CASE_STUDY_API_KEY") or os.environ["BOSS_API_KEY"])
    # Arm switch through BOSS's own settings. Until the knowledge switch exists (Phase 1+), arm B
    # cannot run and says so; arm A runs as BOSS is today (no knowledge features).
    try:
        client.set_setting(args.project_id, KNOWLEDGE_SETTING, "on" if args.arm == "B" else "off")
    except HarnessError as exc:
        if args.arm == "B":
            raise HarnessError(f"arm B needs the {KNOWLEDGE_SETTING!r} project setting, which this BOSS does not "
                               f"have yet ({exc}). Nothing was started.") from exc
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
            repo_dir = Path(args.saleor_dir if task.repo == "saleor" else args.fulfilment_dir or ".")
            hidden_suites_absent(repo_dir)
            (prepare or (lambda t: prepare_saleor(Path(args.saleor_dir), config["saleor"]["sha"], t)
                         if t.repo == "saleor" else None))(task)
            record = {"task": task_id, "arm": args.arm, "engine": args.engine, "model": engine["model"],
                      "repeat": repeat, "stage": args.stage, "approved_by": args.approved_by,
                      "saleor_sha": config["saleor"]["sha"], "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            try:
                record.update(drive(client, task, project_id=args.project_id,
                                    repo=config["saleor"]["fork_name"] if task.repo == "saleor"
                                    else config["fulfilment"]["repo_name"],
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
    return evaluate_service(task, Path(args.fulfilment_dir), scratch_dsn=os.environ["NORTHPEAK_SCRATCH_DSN"],
                            secret=os.environ.get("NORTHPEAK_WEBHOOK_SECRET", "case-study-secret"))


# --- suite validation (no model spend) -----------------------------------------------------------
def validate_suites(args) -> dict:
    """Each hidden suite must FAIL on the pinned SHA (the work is not done yet), and T4's must PASS on
    the pristine SHA and FAIL with the fault applied. T1's suite is proven against the reference
    service in selftest/ (pass) and a variant without idempotency (fail)."""
    out = {}
    saleor = Path(args.saleor_dir) if args.saleor_dir else None
    for task_id in expand_tasks(args.tasks):
        task = load_task(task_id)
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
    r = sub.add_parser("run")
    r.add_argument("--stage", required=True)
    r.add_argument("--arm", choices=["A", "B"], required=True)
    r.add_argument("--engine", choices=["E1", "E2"], required=True)
    r.add_argument("--tasks", required=True)
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--resume", action="store_true")
    r.add_argument("--approved-by")
    r.add_argument("--boss-url", default="http://127.0.0.1:8000")
    r.add_argument("--project-id", required=True)
    r.add_argument("--saleor-dir")
    r.add_argument("--fulfilment-dir")
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
