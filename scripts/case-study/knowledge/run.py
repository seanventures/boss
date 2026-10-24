#!/usr/bin/env python3
"""Knowledge Graph case-study harness (docs/product/KNOWLEDGE_GRAPH_CASE_STUDY.md §4, §6, §7).

Two task sets share it: NorthPeak Retail on Saleor (T1..T12) and the banking set on Apache
Fineract (B1..B4, docs/product/KNOWLEDGE_GRAPH_BANKING_CASE_STUDIES.md cases 1 and 3).

It drives BOSS ONLY through its public HTTP API, as an operator would: code tasks, decisions,
settings, budget and llm-calls. It never imports BOSS internals, so the run shows the real
product. Results go to artifacts/case-study/knowledge/<date>/ (gitignored); report.py turns
them into report.md.

    run.py estimate --stage smoke
    run.py setup --set banking --github-owner OWNER [--dry-run]  # repos, projects, switch, App reach, clones
    run.py validate-suites --saleor-dir PATH [--tasks T1,T4]    # no model spend
    run.py validate-suites --fineract-dir PATH --tasks B1..B4  # no model spend
    run.py run --stage bank_smoke --arm A --engine E2 --tasks B1..B4 [--repeat 1] [--resume]
               --approved-by NAME [--boss-url URL] [--project-id ID] [--fineract-dir PATH]

Each task is graded on the head commit of the pull request BOSS opened for it, and the next task starts from the
accepted result: a passing PR is merged into the arm repository's main (BOSS's own merge decision, approved by
answers.yaml; failing that, `gh pr merge` and the record says so), a failing one is not.

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

    # projects, settings and repositories (setup, and resolving a run's project)
    def projects(self) -> list[dict]:
        return self.request("GET", "/v1/projects").get("items", [])

    def routes(self) -> dict:
        """BOSS's own route list (its OpenAPI paths), to see whether this BOSS offers a route before using it."""
        return self.request("GET", "/openapi.json").get("paths", {})

    def create_project(self, name: str, slug: str) -> Any:
        return self.request("POST", "/v1/projects", {"name": name, "slug": slug})

    def settings(self, project_id: str) -> Any:
        return self.request("GET", "/v1/settings", query={"project_id": project_id})

    def refresh_repos(self, project_id: str) -> Any:
        return self.request("POST", "/v1/github/repos/refresh", {"project_id": project_id})

    def registry(self, project_id: str) -> Any:
        return self.request("GET", "/v1/github/repos", query={"project_id": project_id})

    def allow_repo(self, project_id: str, repo_id: str) -> Any:
        return self.request("PATCH", f"/v1/github/repos/{repo_id}", {"project_id": project_id, "allowed": True})

    def project_repos(self, project_id: str) -> Any:
        return self.request("GET", f"/v1/projects/{project_id}/repos")

    def add_project_repo(self, project_id: str, repo_id: str, base_branch: str = "main") -> Any:
        return self.request("PUT", f"/v1/projects/{project_id}/repos/{repo_id}", {"base_branch": base_branch})


def resolve_project(client, ref: str) -> dict:
    """A BOSS project by id, slug or name (GET /v1/projects)."""
    for project in client.projects():
        if ref in (project.get("id"), project.get("slug"), project.get("name")):
            return project
    raise HarnessError(f"BOSS has no project {ref!r}: run `run.py setup` first (README). Nothing was started.")


# --- scripted approvals -------------------------------------------------------------------------
#: The kind BOSS gives the merge decision on a pull request it opened (decision_actions.kind_of).
MERGE_KIND = "pull_request"


def is_merge_decision(card: dict) -> bool:
    return (card.get("kind") == MERGE_KIND
            or (card.get("recommendation") or {}).get("action") == "review_pull_request")


def answer_for(card: dict, answers: dict, *, after: str | None = None) -> tuple[str, str]:
    """The scripted human: the same rule answers the same decision in both arms. A rule with `after: grading`
    (the merge decision) answers only once the harness has graded the work, never while BOSS is working."""
    kind = card.get("kind") or card.get("decision_type") or card.get("type")
    for rule in answers.get("rules", []):
        if rule["match"].get("kind") == kind and rule.get("after") == after:
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


def split_repo(full_name: str) -> tuple[str, str]:
    """'owner/name' -> ('owner', 'name'). BOSS's POST /v1/code-tasks takes the two as separate fields: `owner`
    and `repo` (the bare name), and clones github.com/<owner>/<repo>. A full name in `repo` is not split there."""
    owner, sep, name = (full_name or "").partition("/")
    if not sep or not owner or not name or "/" in name:
        raise HarnessError(f"{full_name!r} is not a GitHub owner/name")
    return owner, name


def work_branch(state: dict, run_id: str) -> str:
    """The branch BOSS's run pushes to (code_task_service.work_branch_for): one per lineage, from the root run."""
    fctx = state.get("finalize_ctx") or {}
    return fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or state.get('id') or run_id}"


def drive(client: BossClient, task: Task, *, project_id: str, repo: str, engine: dict, answers: dict,
          poll_s: float = 15, deadline_s: float = 4 * 3600, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Start the task as a code task and see it to a terminal state, answering decisions by script. The pull
    request's merge decision is left open: the harness grades the PR first (grade_and_merge)."""
    started = time.monotonic()
    owner, name = split_repo(repo)
    run = client.start_code_task({
        "project_id": project_id, "title": f"{task.id}: {task.title}", "instructions": task.brief,
        "owner": owner, "repo": name, "base_branch": "main", "open_pr": True,
        "model_config": {"engine": engine["engine"], "model": engine["model"]}})
    run_id = run["run_id"]
    answered: list[dict] = []
    seen: set[str] = set()
    merge_decision = None
    while True:
        state = client.code_task(run_id)
        decision_id = state.get("decision_id")
        if decision_id and decision_id not in seen:
            card = client.decision(decision_id)
            if card.get("status") == "pending":
                seen.add(decision_id)
                if is_merge_decision(card):
                    merge_decision = decision_id
                else:
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
            "pr_number": state.get("pull_request_number"), "head_branch": work_branch(state, run_id),
            "merge_decision_id": merge_decision or (decision_id if state.get("pull_request_number") else None),
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


# --- the arm's repositories: clone, release, PR checkout -----------------------------------------
def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def prepare_release(repo_dir: Path, task: Task) -> None:
    """The arm's main is the snapshot plus earlier tasks' merged work; a fault task pushes the faulty release onto
    it. Rerun-safe: a main that already holds the fault (a resumed run) is left as it is."""
    if not task.fault_patch:
        return
    try:
        git(repo_dir, "apply", "--reverse", "--check", str(task.fault_patch))
        return                                    # already released
    except subprocess.CalledProcessError:
        pass
    git(repo_dir, "apply", "--check", str(task.fault_patch))
    git(repo_dir, "apply", str(task.fault_patch))
    git(repo_dir, "commit", "-am", f"release: {task.id} (case-study controlled fault)")
    git(repo_dir, "push", "origin", "HEAD:main")


def prepare_saleor(saleor_dir: Path, sha: str, task: Task) -> None:     # the old name, kept for callers
    prepare_release(saleor_dir, task)


prepare_fineract = prepare_saleor      # same rule for the banking fork: B3/B4 push their faulty release


class GitWorkspace:
    """The arm's local clone. Every task starts from origin's main, and is graded on its PR's head commit."""

    def __init__(self, repo_dir: Path):
        self.dir = Path(repo_dir)

    def head(self) -> str:
        return git(self.dir, "rev-parse", "HEAD")

    def _clean(self) -> None:
        # Untracked files go (anything a suite or a build left in the tree); ignored ones stay (Gradle's build
        # cache, Saleor's .venv), so a rerun is not a cold build.
        git(self.dir, "clean", "-fdq")

    def sync_main(self) -> str:
        """Check out origin's main exactly, with nothing local left over: the accepted result so far."""
        git(self.dir, "fetch", "--quiet", "--prune", "origin")
        git(self.dir, "checkout", "--quiet", "-f", "-B", "main", "origin/main")
        git(self.dir, "reset", "--quiet", "--hard", "origin/main")
        self._clean()
        return self.head()

    def checkout_pr(self, number: int, head_branch: str | None) -> str:
        """The PR's head commit, fetched from origin and checked out clean (detached)."""
        local = f"refs/case-study/pr-{number}"
        try:
            git(self.dir, "fetch", "--quiet", "--force", "origin", f"pull/{number}/head:{local}")
        except subprocess.CalledProcessError:
            if not head_branch:
                raise HarnessError(f"PR #{number}: origin has no pull/{number}/head and the head branch is unknown")
            try:
                git(self.dir, "fetch", "--quiet", "--force", "origin", f"refs/heads/{head_branch}:{local}")
            except subprocess.CalledProcessError as exc:
                raise HarnessError(f"PR #{number}: could not fetch pull/{number}/head or {head_branch}: "
                                   f"{(exc.stderr or '').strip()[:300]}") from exc
        git(self.dir, "checkout", "--quiet", "-f", "--detach", local)
        self._clean()
        return self.head()


class GitHubHost:
    """The pull requests on GitHub, through the operator's `gh` login (never a token in this tree)."""

    def _gh(self, *args: str) -> str:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True)
        if proc.returncode != 0:
            raise HarnessError(f"gh {' '.join(args[:2])}: {(proc.stderr or proc.stdout).strip()[:300]}")
        return proc.stdout.strip()

    def pull_request(self, repo: str, number: int) -> dict:
        data = json.loads(self._gh("pr", "view", str(number), "--repo", repo, "--json",
                                   "number,url,state,isDraft,headRefName,headRefOid,baseRefName,mergeCommit"))
        return {"number": data["number"], "url": data.get("url"), "state": data.get("state"),
                "merged": data.get("state") == "MERGED", "draft": bool(data.get("isDraft")),
                "head_ref": data.get("headRefName"), "head_sha": data.get("headRefOid"),
                "base": data.get("baseRefName"), "merge_commit": (data.get("mergeCommit") or {}).get("oid")}

    def find_pr(self, repo: str, head_branch: str) -> int | None:
        out = self._gh("pr", "list", "--repo", repo, "--head", head_branch, "--state", "all", "--json", "number",
                       "--limit", "1")
        found = json.loads(out or "[]")
        return found[0]["number"] if found else None

    def merge(self, repo: str, number: int, sha: str, method: str = "merge") -> str | None:
        """Merge exactly the graded commit: GitHub refuses if the head moved since."""
        if self.pull_request(repo, number)["draft"]:
            self._gh("pr", "ready", str(number), "--repo", repo)
        self._gh("pr", "merge", str(number), "--repo", repo, f"--{method}", "--match-head-commit", sha)
        return self.pull_request(repo, number).get("merge_commit")

    # setup
    def repo(self, repo: str) -> dict | None:
        proc = subprocess.run(["gh", "repo", "view", repo, "--json", "nameWithOwner,isPrivate,defaultBranchRef"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            if "Could not resolve" in proc.stderr or "not found" in proc.stderr.lower():
                return None
            raise HarnessError(f"gh repo view {repo}: {proc.stderr.strip()[:300]}")
        data = json.loads(proc.stdout)
        return {"private": data.get("isPrivate"), "default_branch": (data.get("defaultBranchRef") or {}).get("name")}

    def create_private(self, repo: str, description: str) -> None:
        self._gh("repo", "create", repo, "--private", "--description", description)

    def clone_url(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"


#: Hidden-suite file names, per task set. A fork that holds one has leaked the answer key.
HIDDEN_PATTERNS = ("test_hidden_t*.py", "CaseStudyHidden*.java", "b[0-9]_*.golden")


def hidden_suites_absent(repo_dir: Path) -> None:
    """Hidden suites are never shown to agents: refuse a fork that contains one."""
    hits = [h for pattern in HIDDEN_PATTERNS for h in repo_dir.rglob(pattern)] if repo_dir.exists() else []
    if hits:
        raise HarnessError(f"hidden suite present in the target repository: {hits[0]}")


#: Where `setup` clones the arms' repositories, and where `run` looks for them by default: next to the harness.
CLONES = HERE.parent / "repos"
DIR_ARG = {"fineract": "fineract_dir", "saleor": "saleor_dir", "fulfilment": "fulfilment_dir"}
REPO_LABEL = {"fineract": "Fineract demo repository", "saleor": "Saleor demo repository",
              "fulfilment": "fulfilment repository"}


def fork_for(config: dict, kind: str, engine: str, arm: str) -> dict | None:
    return ((config.get(kind) or {}).get("forks") or {}).get(engine, {}).get(arm)


def repo_target(config: dict, task: Task, args) -> tuple[Path, str, dict]:
    """The local clone, the GitHub owner/name, and the fork entry (its BOSS project) a task works in."""
    fork = fork_for(config, task.repo, args.engine, args.arm)
    if not fork:
        raise HarnessError(f"no {REPO_LABEL[task.repo]} for {args.engine}/{args.arm} in harness.yaml "
                           f"{task.repo}.forks; nothing was started")
    split_repo(fork["repo"])
    flag = DIR_ARG[task.repo]
    given = getattr(args, flag, None)
    path = Path(given) if given else Path(getattr(args, "clone_dir", None) or CLONES) / split_repo(fork["repo"])[1]
    if not given and not path.exists():
        raise HarnessError(f"{task.id} works in {fork['repo']}: pass --{flag.replace('_', '-')} or run "
                           f"`run.py setup` to clone it to {path}. Nothing was started.")
    return path, fork["repo"], fork


# --- grading the PR, and accepting it ------------------------------------------------------------
def grade_and_merge(client, host, ws, task: Task, built: dict, *, repo: str, answers: dict,
                    evaluate: Callable[[Task], dict], merge_cfg: dict | None = None,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """Grade the work BOSS delivered, on its pull request's head commit, and accept it only if it passed.

    No PR: unsolved, nothing to grade. Passed: approve BOSS's merge decision (answers.yaml, after: grading) and
    wait for BOSS to merge; if it does not (a BOSS that leaves merging to a person), merge the graded commit with
    `gh` and record that the harness did. Failed: record it, merge nothing, so the next task starts from the
    unchanged main."""
    out: dict[str, Any] = {}
    number = built.get("pr_number")
    if not number and built.get("head_branch"):
        number = host.find_pr(repo, built["head_branch"])
        if number:
            out["pr_found_by"] = "head branch"
    if not number:
        return {**out, "graded_commit": None,
                "evaluation": {"solved": False, "reason": "BOSS opened no pull request: nothing to grade"},
                "merge": {"merged": False, "why": "no pull request"}}
    pr = host.pull_request(repo, int(number))
    sha = ws.checkout_pr(int(number), pr.get("head_ref") or built.get("head_branch"))
    out["pr"] = {"number": int(number), "url": pr.get("url") or built.get("pr_url"), "head_branch": pr.get("head_ref"),
                 "head_sha": pr.get("head_sha"), "base": pr.get("base"), "state_when_graded": pr.get("state")}
    out["graded_commit"] = sha
    if pr.get("head_sha") and pr["head_sha"] != sha:
        out["pr"]["head_moved"] = True             # graded what was fetched; the merge below refuses a moved head
    try:
        hidden_suites_absent(ws.dir)
    except HarnessError as exc:
        evaluation = {"solved": False, "reason": f"the pull request {exc}"}
    else:
        evaluation = evaluate(task)
    out["evaluation"] = evaluation
    decision_id = built.get("merge_decision_id")
    if pr.get("merged"):
        out["merge"] = {"merged": True, "by": "before grading", "merge_commit": pr.get("merge_commit"),
                        "why": "the PR was already merged when the harness graded it"}
        return out
    if not evaluation.get("solved"):
        out["merge"] = {"merged": False, "decision_id": decision_id, "decision_answered": None,
                        "why": "the hidden suite did not pass on the graded commit; main is unchanged"}
        return out
    out["merge"] = merge_accepted(client, host, repo, int(number), sha, decision_id, answers,
                                  merge_cfg=merge_cfg or {}, sleep=sleep, clock=clock)
    return out


def merge_accepted(client, host, repo: str, number: int, sha: str, decision_id: str | None, answers: dict, *,
                   merge_cfg: dict, sleep: Callable[[float], None], clock: Callable[[], float],
                   poll_s: float = 15) -> dict:
    result: dict[str, Any] = {"decision_id": decision_id, "decision_answered": None}
    why = "the run carries no merge decision"
    if decision_id:
        card = client.decision(decision_id)
        if card.get("status") == "pending" and is_merge_decision(card):
            verb, note = answer_for({**card, "kind": MERGE_KIND}, answers, after="grading")
            client.answer_decision(decision_id, verb, note)
            result["decision_answered"] = verb
            deadline = clock() + float(merge_cfg.get("wait_minutes", 10)) * 60
            while True:
                pr = host.pull_request(repo, number)
                if pr.get("merged"):
                    return {**result, "merged": True, "by": "boss", "merge_commit": pr.get("merge_commit"),
                            "head_at_merge": pr.get("head_sha")}
                status = client.decision(decision_id).get("status")
                if status in ("rejected", "cancelled", "revision_requested") or clock() >= deadline:
                    break
                sleep(poll_s)
            result["boss_decision_status"] = status
            why = (f"BOSS approved the merge but had not merged within {merge_cfg.get('wait_minutes', 10)} min "
                   f"(decision {status})")
        else:
            why = f"the merge decision was {card.get('status')!r}, not pending"
    try:
        commit = host.merge(repo, number, sha, merge_cfg.get("method", "merge"))
    except HarnessError as exc:
        return {**result, "merged": False, "by": None, "why": f"{why}; the harness's merge failed: {exc}"}
    return {**result, "merged": True, "by": "harness (gh)", "merge_commit": commit, "why": why}


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
        prepare: Callable[[Task], None] | None = None, sleep: Callable[[float], None] = time.sleep,
        host: Any = None, workspace: Callable[[Path], Any] | None = None,
        clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """The tasks in order, each from the accepted result of the ones before it (knowledge accumulates): sync the
    arm's clone to origin's main, push a fault task's release, let BOSS work, grade its PR's head commit, merge it
    only if it passed, and record what was graded and whether it was merged."""
    config, answers = load_config(), load_answers()
    est = check_approval(config, args)
    targets = {}
    for task_id in expand_tasks(args.tasks):         # every repository known before any setting or budget moves
        targets[task_id] = repo_target(config, load_task(task_id), args)
    client = client or BossClient(args.boss_url, os.environ.get("BOSS_CASE_STUDY_API_KEY") or os.environ["BOSS_API_KEY"])
    projects = {fork.get("project") for _, _, fork in targets.values()}
    ref = args.project_id or (next(iter(projects)) if len(projects) == 1 else None)
    if not ref:
        raise HarnessError(f"these tasks' repositories are in different projects ({sorted(map(str, projects))}): "
                           "pass --project-id. Nothing was started.")
    project_id = resolve_project(client, ref)["id"]
    host = host or GitHubHost()
    workspace = workspace or GitWorkspace
    # Arm switch through BOSS's own settings: arm B on, arm A (the control) off, arm AP (A′, full-text keyword
    # search only) "keyword". A BOSS without the switch or the value refuses it; then that arm cannot run and
    # says so, and arm A runs as that BOSS is.
    value = config["arms"][args.arm]["knowledge"]
    try:
        client.set_setting(project_id, KNOWLEDGE_SETTING, value)
    except HarnessError as exc:
        if args.arm != CONTROL_ARM:
            raise HarnessError(f"arm {args.arm} needs the {KNOWLEDGE_SETTING!r} project setting with the value {value!r}, "
                               f"which this BOSS does not have yet ({exc}). Nothing was started.") from exc
    reservation = client.reserve_budget(project_id, est["ceiling_usd"],
                                        f"case study {args.stage}: approved by {args.approved_by}")
    if reservation.get("status") not in ("reserved", "ok", "approved"):
        raise HarnessError(f"BOSS refused the stage budget: {reservation}. Nothing was started.")
    out_dir = Path(args.out) if args.out else ARTIFACTS / dt.date.today().isoformat()
    engine = config["engines"][args.engine]
    results = []
    for task_id in expand_tasks(args.tasks):
        task = load_task(task_id)
        repo_dir, repo_name, _ = targets[task_id]
        for repeat in range(1, args.repeat + 1):
            path = result_path(out_dir, args.engine, args.arm, task_id, repeat)
            if args.resume and path.exists():
                results.append(json.loads(path.read_text()))
                continue
            ws = workspace(repo_dir)
            record = {"task": task_id, "arm": args.arm, "engine": args.engine, "model": engine["model"],
                      "repeat": repeat, "stage": args.stage, "approved_by": args.approved_by,
                      "project_id": project_id, "repo": repo_name,
                      "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            if task.repo == "fineract":
                record["fineract_sha"] = config["fineract"]["sha"]
                record["fineract_repo"] = repo_name
                record["expected_reuse"] = [i["id"] for i in task.meta.get("expected_reuse", [])]
            else:
                record["saleor_sha"] = config["saleor"]["sha"]
            record["main_before"] = ws.sync_main()       # the accepted result of the tasks before this one
            hidden_suites_absent(repo_dir)
            (prepare or (lambda t: prepare_release(repo_dir, t)))(task)
            record["release_commit"] = ws.head()           # what BOSS starts from (B3/B4, T4: with the fault)
            try:
                record.update(drive(client, task, project_id=project_id, repo=repo_name,
                                    engine=engine, answers=answers, sleep=sleep))
                record.update(grade_and_merge(
                    client, host, ws, task, record, repo=repo_name, answers=answers,
                    evaluate=evaluator or (lambda t: default_evaluator(t, args, repo_dir)),
                    merge_cfg=config.get("merge"), sleep=sleep, clock=clock))
            except NeedsOperator as exc:
                record.update({"status": "needs_operator", "error": str(exc), "evaluation": {"solved": False}})
            record["main_after"] = ws.sync_main()        # where the next task starts
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2))
            results.append(record)
            if record["status"] == "needs_operator":
                return results           # stop the sequence: knowledge accumulates in order
    return results


def default_evaluator(task: Task, args, repo_dir: Path | None = None) -> dict:
    """The task's hidden suite, run in the arm's clone (checked out at the commit being graded)."""
    if task.evaluate["mode"] == "saleor_pytest":
        return evaluate_saleor(task, Path(repo_dir or args.saleor_dir))
    if task.evaluate["mode"] == "fineract_gradle":
        return evaluate_fineract(task, Path(repo_dir or args.fineract_dir), gradle_cmd=getattr(args, "gradle_cmd", None))
    return evaluate_service(task, Path(repo_dir or args.fulfilment_dir), scratch_dsn=os.environ["NORTHPEAK_SCRATCH_DSN"],
                            secret=os.environ.get("NORTHPEAK_WEBHOOK_SECRET", "case-study-secret"))


# --- setup: the same path for every operator (no model spend) ------------------------------------
#: The note a snapshot's README.md gains under its first heading; nothing else in the tree changes.
README_NOTES = {
    "fineract": ("> **Demo fork of Apache Fineract {tag} used by the BOSS knowledge-graph case study.**\n"
                 "> NorthPeak core banking is a fictional bank. This repository is an unmodified snapshot of\n"
                 "> [apache/fineract](https://github.com/apache/fineract) at tag `{tag}`\n"
                 "> (commit `{sha}`), apart from this note. Apache Fineract is\n"
                 "> licensed under the Apache License 2.0: see `LICENSE_SOURCE` and `NOTICE_SOURCE`.\n"),
    "saleor": ("> **Demo fork of Saleor used by the BOSS knowledge-graph case study.**\n"
               "> NorthPeak Retail is a fictional shop. This repository is an unmodified snapshot of\n"
               "> [saleor/saleor](https://github.com/saleor/saleor) at commit `{sha}`, apart from this note.\n"
               "> Saleor is licensed under the BSD 3-Clause License: see `LICENSE`.\n"),
}
EMPTY_README = ("# NorthPeak fulfilment\n\nThe fulfilment service of NorthPeak Retail, a fictional shop in the BOSS "
                "knowledge-graph case study. Empty until task T1 builds it.\n")
LEGAL = re.compile(r"^(LICENSE|NOTICE|COPYING)", re.I)
APP_STEP = ("GitHub › Settings › Applications › Installed GitHub Apps › {app} › Configure › Repository access › "
            "Only select repositories › add {repo} › Save{url}; then rerun setup")


def build_snapshot(kind: str, config: dict, work: Path) -> Path:
    """One commit on main: the upstream tree at the pinned SHA (every LICENSE*/NOTICE* intact) plus the README
    note, or, for a repository with no upstream, a README alone."""
    cfg = config[kind]
    dest = work / f"{kind}-snapshot"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git(dest, "init", "--quiet", "-b", "main")
    if cfg.get("upstream"):
        git(dest, "fetch", "--quiet", "--depth", "1", cfg["upstream"], cfg["sha"])
        git(dest, "read-tree", "-u", "--reset", "FETCH_HEAD")
        legal = [p for p in git(dest, "ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines() if LEGAL.match(Path(p).name)]
        if not legal or not all((dest / p).exists() for p in legal):
            raise HarnessError(f"{kind} snapshot: the upstream's LICENSE/NOTICE files did not all arrive ({legal})")
        readme = dest / "README.md"
        first, _, rest = readme.read_text().partition("\n")
        note = README_NOTES[kind].format(tag=cfg.get("tag", ""), sha=cfg["sha"])
        readme.write_text(f"{first}\n\n{note}\n{rest.lstrip(chr(10))}")
    else:
        (dest / "README.md").write_text(EMPTY_README)
    git(dest, "add", "-A")
    git(dest, "commit", "--quiet", "-m", cfg.get("snapshot_message") or f"NorthPeak {kind}")
    return dest


def setup_targets(config: dict, set_name: str, owner: str, engine: str | None) -> list[dict]:
    """Every (engine, arm, kind) the set's forks name, refusing a fork on another owner (harness.yaml is the one
    place the names live)."""
    sset = (config.get("sets") or {}).get(set_name)
    if not sset:
        raise HarnessError(f"unknown set {set_name!r}; harness.yaml sets: {sorted(config.get('sets') or {})}")
    out, wrong = [], []
    for kind in sset["repos"]:
        for eng, arms in sorted((config[kind].get("forks") or {}).items()):
            if engine and eng != engine:
                continue
            for arm, fork in arms.items():
                repo_owner, name = split_repo(fork["repo"])
                if repo_owner != owner:
                    wrong.append(fork["repo"])
                out.append({"kind": kind, "engine": eng, "arm": arm, "repo": fork["repo"], "name": name,
                            "project": fork["project"], "knowledge": config["arms"][arm]["knowledge"],
                            "project_name": f"{sset['project_name']} {eng} arm {arm}"})
    if wrong:
        raise HarnessError(f"harness.yaml names {', '.join(wrong)}, not --github-owner {owner}: edit the "
                           f"{'/'.join(sset['repos'])}.forks entries to your owner first (one place names them)")
    if not out:
        raise HarnessError(f"harness.yaml has no {set_name} repositories{f' for {engine}' if engine else ''}")
    return out


def setup(args, *, client=None, host=None, out: Callable[[str], None] = print,
          snapshot: Callable[[str], Path] | None = None) -> list[dict]:
    """Make the set's repositories, projects, settings, GitHub App reach and clones what harness.yaml says.
    Idempotent: what is already in place is reported and left alone. With dry_run, only reads."""
    config = load_config()
    targets = setup_targets(config, args.set, args.github_owner, getattr(args, "engine", None))
    dry = bool(args.dry_run)
    client = client or BossClient(args.boss_url, os.environ.get("BOSS_CASE_STUDY_API_KEY") or os.environ["BOSS_API_KEY"])
    host = host or GitHubHost()
    clones = Path(getattr(args, "clone_dir", None) or CLONES)
    steps: list[dict] = []

    def step(what: str, target: str, status: str, detail: str) -> None:
        steps.append({"step": what, "target": target, "status": status, "detail": detail})
        out(f"{status:<9} {what:<10} {target}: {detail}")

    out(f"setup --set {args.set} --github-owner {args.github_owner}{' --dry-run (reads only)' if dry else ''}")
    # 1. The repositories, one snapshot commit each.
    built: dict[str, Path] = {}
    for t in {t["repo"]: t for t in targets}.values():
        info = host.repo(t["repo"])
        cfg = config[t["kind"]]
        source = f"{cfg['upstream']} @ {cfg['sha'][:12]}" if cfg.get("upstream") else "a README"
        if info:
            step("repo", t["repo"], "ok" if info.get("private") else "operator",
                 "exists, private" if info.get("private") else "exists but is PUBLIC: make it private on GitHub")
            continue
        if dry:
            step("repo", t["repo"], "would", f"create private, one snapshot commit of {source} "
                                             f"(\"{cfg.get('snapshot_message')}\", LICENSE/NOTICE intact)")
            continue
        if t["kind"] not in built:
            built[t["kind"]] = (snapshot or (lambda kind: build_snapshot(kind, config, ARTIFACTS / "setup")))(t["kind"])
        host.create_private(t["repo"], f"BOSS knowledge-graph case study demo ({t['kind']}, arm {t['arm']})")
        git(built[t["kind"]], "push", "--quiet", host.clone_url(t["repo"]), "main:main")
        step("repo", t["repo"], "done", f"created private from {source}")
    # 2. One BOSS project per arm, its knowledge switch, its repositories.
    listed = client.projects()
    projects = {p.get("slug"): p for p in listed}
    projects.update({p.get("id"): p for p in listed})
    offered: list[bool] = []

    def can_create() -> bool:
        if not offered:
            offered.append("post" in {m.lower() for m in (client.routes().get("/v1/projects") or {})})
        return offered[0]

    for t in {t["project"]: t for t in targets}.values():
        if t["project"] in projects:
            step("project", t["project"], "ok", f"exists ({projects[t['project']]['id']})")
        elif not can_create():
            step("project", t["project"], "blocked",
                 "this BOSS has no route to create a project (no POST /v1/projects; the project:create permission "
                 "has none): it needs one before the arms can be kept apart")
        elif dry:
            step("project", t["project"], "would", f"create \"{t['project_name']}\" (POST /v1/projects)")
        else:
            made = client.create_project(t["project_name"], t["project"])
            projects[t["project"]] = {"id": made.get("id") or made.get("project_id"), "slug": t["project"]}
            step("project", t["project"], "done", f"created ({projects[t['project']]['id']})")
    for t in {t["project"]: t for t in targets}.values():
        project = projects.get(t["project"])
        if not project:
            step("knowledge", t["project"], "blocked", f"set knowledge_graph={t['knowledge']} once the project exists")
            continue
        knob = next((k for k in client.settings(project["id"]).get("knobs", []) if k.get("key") == KNOWLEDGE_SETTING), None)
        if knob is None or (knob.get("choices") and t["knowledge"] not in knob["choices"]):
            step("knowledge", t["project"], "blocked",
                 f"this BOSS offers {KNOWLEDGE_SETTING} {knob and knob.get('choices')}; arm {t['arm']} needs "
                 f"{t['knowledge']!r} (keyword: rel/rc10.10-kg f1f7fb6af or later)")
        elif knob.get("value") == t["knowledge"] and knob.get("source") == "project":
            step("knowledge", t["project"], "ok", f"{KNOWLEDGE_SETTING}={t['knowledge']}")
        elif dry:
            step("knowledge", t["project"], "would", f"set {KNOWLEDGE_SETTING}={t['knowledge']} (now {knob.get('value')})")
        else:
            client.set_setting(project["id"], KNOWLEDGE_SETTING, t["knowledge"])
            step("knowledge", t["project"], "done", f"{KNOWLEDGE_SETTING}={t['knowledge']}")
    # 3. The BOSS GitHub App's reach, the org's allow list, and each project's binding.
    # The org's repository list is read through any project: an arm's, or (before they exist) any this key sees.
    any_project = next((projects[t["project"]] for t in targets if t["project"] in projects), None) or \
        (listed[0] if listed else None)
    registry: dict = {"items": [], "connection": {}}
    if any_project:
        if not dry:
            client.refresh_repos(any_project["id"])          # re-read what the App's installation lists
        registry = client.registry(any_project["id"])
    conn = registry.get("connection") or {}
    app = conn.get("app_slug") or "the BOSS app"
    url = f" ({conn['change_url']})" if conn.get("change_url") else ""
    by_name = {i["full_name"].lower(): i for i in registry.get("items", [])}
    for t in {t["repo"]: t for t in targets}.values():
        item = by_name.get(t["repo"].lower())
        if not any_project:
            step("app", t["repo"], "blocked", "the GitHub App's reach is read through a project: none exists yet")
        elif not item:
            step("app", t["repo"], "check" if dry else "operator",
                 ("not in BOSS's repository list yet; setup refreshes it from GitHub. If it is still missing: "
                  if dry else "the BOSS GitHub App cannot reach it: ") + APP_STEP.format(app=app, repo=t["repo"], url=url))
        elif not item.get("allowed"):
            if dry:
                step("app", t["repo"], "would", "reachable; allow it for BOSS (Repo and models › Repositories)")
            else:
                item = client.allow_repo(any_project["id"], item["id"])
                by_name[t["repo"].lower()] = item
                step("app", t["repo"], "done" if item.get("state") == "working" else "operator",
                     f"allowed; checks: {item.get('state')}")
        else:
            step("app", t["repo"], "ok" if item.get("state") == "working" else "operator",
                 f"reachable and allowed; checks: {item.get('state')}"
                 + ("" if item.get("state") == "working" else
                    f" (the App needs read, write and pull-request access: {APP_STEP.format(app=app, repo=t['repo'], url=url)})"))
    for t in targets:
        project, item = projects.get(t["project"]), by_name.get(t["repo"].lower())
        target = f"{t['project']} <- {t['repo']}"
        if not project:
            step("register", target, "blocked", "the project does not exist")
            continue
        bound = {i["full_name"].lower() for i in client.project_repos(project["id"]).get("items", [])}
        if t["repo"].lower() in bound:
            step("register", target, "ok", "registered in the project")
        elif dry or not item or not item.get("allowed"):
            step("register", target, "would" if dry else "blocked",
                 "register it in the project (base branch main)" if dry else "the repository is not allowed for BOSS yet")
        else:
            client.add_project_repo(project["id"], item["id"], "main")
            step("register", target, "done", "registered in the project (base branch main)")
    # 4. A local clone per repository, next to the harness.
    for t in {t["repo"]: t for t in targets}.values():
        path = clones / t["name"]
        if (path / ".git").exists():
            origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=path, capture_output=True, text=True).stdout.strip()
            ok = origin.rstrip("/").removesuffix(".git").lower().endswith(t["repo"].lower()) or origin == host.clone_url(t["repo"])
            step("clone", str(path), "ok" if ok else "operator",
                 "present" if ok else f"present but its origin is {origin}, not {t['repo']}")
        elif dry:
            step("clone", str(path), "would", f"git clone {host.clone_url(t['repo'])}")
        elif not host.repo(t["repo"]):
            step("clone", str(path), "blocked", "the repository does not exist")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", host.clone_url(t["repo"]), str(path)], check=True,
                           capture_output=True, text=True)
            step("clone", str(path), "done", f"cloned {t['repo']}")
    waiting = [s for s in steps if s["status"] in ("operator", "blocked")]
    out(f"{len(steps)} steps: " + ", ".join(f"{n} {k}" for k, n in sorted(
        {s['status']: sum(1 for x in steps if x['status'] == s['status']) for s in steps}.items())))
    if waiting:
        out("Needs the operator before a run: " + "; ".join(f"{s['step']} {s['target']}" for s in waiting))
    return steps


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
    r.add_argument("--project-id", help="default: the arm's project in harness.yaml (its id, slug or name)")
    r.add_argument("--clone-dir", help=f"where setup cloned the arms' repositories (default {CLONES})")
    r.add_argument("--saleor-dir")
    r.add_argument("--fulfilment-dir")
    r.add_argument("--fineract-dir")
    r.add_argument("--gradle-cmd")
    r.add_argument("--out")
    st = sub.add_parser("setup", help="create the set's repositories, BOSS projects, settings and clones (idempotent)")
    st.add_argument("--set", required=True, choices=sorted(load_config().get("sets") or {}))
    st.add_argument("--github-owner", required=True, help="must be the owner harness.yaml's forks name")
    st.add_argument("--engine", choices=["E1", "E2"], help="default: every engine the set's forks name")
    st.add_argument("--boss-url", default="http://127.0.0.1:8000")
    st.add_argument("--clone-dir", help=f"default {CLONES}")
    st.add_argument("--dry-run", action="store_true", help="print the plan; read BOSS and GitHub, change nothing")
    args = p.parse_args(argv)
    try:
        if args.command == "estimate":
            print(json.dumps(estimate(load_config(), args.stage)))
        elif args.command == "validate-suites":
            print(json.dumps(validate_suites(args), indent=2))
        elif args.command == "setup":
            steps = setup(args)
            return 3 if any(s["status"] in ("operator", "blocked") for s in steps) else 0
        else:
            for record in run(args):
                print(json.dumps({k: record.get(k) for k in ("task", "arm", "engine", "repeat", "status", "graded_commit")}
                                 | {"solved": (record.get("evaluation") or {}).get("solved"),
                                    "merged": (record.get("merge") or {}).get("merged")}))
    except HarnessError as exc:
        print(f"run.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
