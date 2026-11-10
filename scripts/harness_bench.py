#!/usr/bin/env python3
"""THE HARNESS BENCHMARK RUNNER (BOSSAPP-959). Entered as `scripts/harness-bench`.

    scripts/harness-bench run --engine scripted [--tasks all|<ids>] [--repeat N]
                              [--baseline NAME] [--save-baseline NAME] [--dry-run] ...
    scripts/harness-bench run --engine claude_code --budget-usd 20 --allow-unisolated ...
    scripts/harness-bench compare <run-id | report.json> --baseline NAME
    scripts/harness-bench report                  the harness score: latest run per engine/level
    scripts/harness-bench estimate --history-db-url URL [--engine claude_code]

WHAT A RUN DOES. For every task of bench/harness/suite.yaml (× --repeat), it seeds the task's
pinned fixture into a LOCAL GitHub (the e2e gate's: git smart-HTTP + the REST API on loopback,
tests/e2e/gate/remote.py), starts the build exactly as the API does
(code_task_service.start_code_task) and runs it exactly as the worker does (run_code_task) through
BOSS's REAL governed pipeline on the chosen engine — the engines gate's way (tests/e2e/gate/
engines.py), in this process. It then reads what the build cost and did from the database
(harness_bench/metrics.py), clones the pull request's head and runs the task's acceptance checks
itself: the task's own tests again, files that must exist or change, and tests held by the
benchmark that the agent never saw. Each result is stored (harness_bench_runs / _results) and
written to a JSON report; with --baseline it is compared (harness_bench/compare.py) and the exit
code says whether a guard failed.

WHERE. Only on a database named boss_test* (--db-url, else BOSS_BENCH_DATABASE_URL, else
DATABASE_URL): the runner refuses any other, as scripts/e2e/devapi.py does, so it can never touch
a live install. It applies the schema itself and works in a throwaway project per run.

ENGINES. `scripted` (no model, no cost, deterministic: the plumbing and the measuring) or a real
engine: `claude_code`, `codex`, `native:<model>` (or any registered harness, `name[:model]`). A real
engine spends money: it needs --budget-usd, and on a host without boss-sandbox-exec its sessions run
unisolated as you, so it also needs --allow-unisolated. Exit codes: 0 ok, 1 a guard failed (worse),
2 refused, not comparable, or incomplete.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "tests" / "e2e" / "gate", ROOT / "apps" / "worker", ROOT / "apps" / "api",
           ROOT / "engine", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# A build entered without a person, as the engines gate enters one: identity in shadow, with a
# fixed test-only server key (never a real one). The benchmark measures the pipeline, not who may
# start it.
os.environ.setdefault("BOSS_AUTHZ_MODE", "shadow")
# What the builds store, and any build database they ask for, stay out of the checkout and out of
# any real install's names.
os.environ.setdefault("BOSS_ARTIFACTS_DIR",
                      str(Path(tempfile.gettempdir()) / "boss-harness-bench-artifacts"))
os.environ.setdefault("BOSS_BUILD_SERVICES_PREFIX", "boss_test_bench_svc_")
os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY",
                      "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")

from boss_engine.harness_bench import LEVELS, REPORT_SCHEMA, compare as cmp_mod  # noqa: E402
from boss_engine.harness_bench import metrics as metrics_mod  # noqa: E402
from boss_engine.harness_bench import store  # noqa: E402
from boss_engine.harness_bench.suite import (  # noqa: E402
    SCRIPTED_FAULTS, SuiteError, load_suite, resolve, rules_for, select_tasks, tree_files,
)

OWNER = "bench"
EXIT_OK, EXIT_WORSE, EXIT_REFUSED = 0, 1, 2
#: The harness settings a run's config fingerprint is made of (what a harness change changes).
KNOBS = ("code_repair_max_retries", "code_review_inrun_max_rounds", "code_review_repair_max_rounds",
         "code_review_panel_size", "code_gate_never_block", "open_pr_when_checks_fail",
         "model_routing_mode", "pipeline_profile", "governed_coding_pipeline")
#: Repair children (a cold review-repair round, a checks-failing continuation) followed per task.
MAX_LINEAGE = 4
#: What every benchmark build runs with: the governed pipeline, whatever the host's default.
FORCED = {"enable_code_task": True, "governed_coding_pipeline": True}
#: Knobs the pipeline reads with getattr(settings, name, default) instead of a declared field.
UNDECLARED_KNOBS = {"code_review_inrun_max_rounds": 2}
_TEST_DB = re.compile(r"^boss_test\w*$")
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


class Refused(Exception):
    """The run cannot start (or cannot be compared): said in words, exit code 2."""


def say(msg: str) -> None:
    print(f"[bench] {msg}", flush=True)


# ── where it may run ─────────────────────────────────────────────────────────────────────────

def database_url(arg: Optional[str]) -> str:
    """The test database URL (psycopg driver named). Refuses any database not named boss_test*."""
    from sqlalchemy.engine import make_url

    raw = arg or os.environ.get("BOSS_BENCH_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not raw:
        raise Refused("no database: pass --db-url (a boss_test* database) or set "
                      "BOSS_BENCH_DATABASE_URL")
    if raw.startswith(("postgresql://", "postgres://")):
        raw = "postgresql+psycopg://" + raw.split("://", 1)[1]
    url = make_url(raw)
    if not url.drivername.startswith("postgresql"):
        raise Refused(f"the benchmark runs on Postgres only, not {url.drivername}")
    if not _TEST_DB.fullmatch(url.database or ""):
        raise Refused(f"the benchmark runs only on a database named boss_test*, not "
                      f"{url.database!r}: it never runs against a live install")
    return raw


def parse_engine(spec: str) -> tuple[str, Optional[str]]:
    """'native:qwen' -> ('native', 'qwen'); 'claude_code' -> ('claude_code', None)."""
    name, _, model = (spec or "").partition(":")
    name = name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise Refused(f"--engine {spec!r}: scripted, claude_code, codex or native:<model>")
    if name == "native" and not model.strip():
        raise Refused("--engine native needs a model: native:<model>")
    return name, (model.strip() or None)


def engine_preflight(harness: str, *, allow_unisolated: bool, budget_usd: Optional[float],
                     dry_run: bool) -> list[str]:
    """Why a real engine cannot run here (an empty list: it can)."""
    if harness == "scripted":
        return []
    why = []
    from boss_engine.services import code_agent  # noqa: F401 — registers the built-in harnesses
    from boss_engine.services.harness_registry import UnknownHarness, get_harness, missing_runtime

    try:
        get_harness(harness)
    except UnknownHarness as exc:
        why.append(str(exc))
    for binary in missing_runtime(harness):
        why.append(f"{binary.binary} is not on PATH")
    if harness == "native" and not (os.environ.get("OPENAI_API_KEY")):
        why.append("the native engine needs OPENAI_API_KEY (and OPENAI_BASE_URL for a non-OpenAI API)")
    if not dry_run and (budget_usd is None or budget_usd <= 0):
        why.append("a real engine spends money: pass --budget-usd (the most the whole run may "
                   "spend, at list price)")
    if not shutil.which("boss-sandbox-exec") and not allow_unisolated:
        why.append("this host has no boss-sandbox-exec, so the engine's sessions would run "
                   "UNISOLATED as you: pass --allow-unisolated to accept that (or run it in the "
                   "worker image)")
    return why


def parse_sets(pairs: list[str]) -> dict[str, Any]:
    """--set key=value -> {key: value}, the value read as YAML (2, true, fixed). Unknown keys refused."""
    import yaml

    from boss_engine.config import get_settings

    settings = get_settings()
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise Refused(f"--set {pair!r}: key=value")
        if not hasattr(settings, key) and key not in UNDECLARED_KNOBS:
            raise Refused(f"--set {key}: no such setting")
        out[key] = yaml.safe_load(value)
    return out


def boss_revision() -> tuple[str, bool]:
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    sha = git("rev-parse", "HEAD") or "unknown"
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    return sha, dirty


def run_config(*, suite, engine_spec, model, level, settings, overrides, caps, extra_faults,
               scripted: bool) -> tuple[dict[str, Any], str]:
    knobs = {k: getattr(settings, k, UNDECLARED_KNOBS.get(k)) for k in KNOBS}
    knobs.update({k: v for k, v in overrides.items() if k in KNOBS})
    config = {
        "engine": engine_spec, "model": model, "level": level,
        "profile": (suite.get("defaults") or {}).get("profile"),
        "risk_tier": (suite.get("defaults") or {}).get("risk_tier"),
        "settings": knobs,
        "overrides": dict(sorted(overrides.items())),
    }
    if scripted:
        config["scripted"] = {"caps": caps, "extra_faults": sorted(extra_faults)}
    fp = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return config, fp


# ── the world: a test database and a local GitHub ────────────────────────────────────────────

class World:
    def __init__(self, db_url: str, work: Path):
        os.environ["DATABASE_URL"] = db_url
        from sqlalchemy import create_engine, text

        from boss_engine.db import ensure_schema

        self.text = text
        self.work = work
        self.engine = create_engine(db_url, future=True)
        t0 = time.monotonic()
        ensure_schema(self.engine)
        say(f"schema applied in {time.monotonic() - t0:.1f}s")
        from apps.api.app.bootstrap_ports import build_ports
        from boss_engine.config import get_settings
        from boss_engine.ports import configure_engine_ports

        configure_engine_ports(build_ports(get_settings(), self.engine))
        from remote import LocalGitHub

        self.hub = LocalGitHub(work / "github").start()
        self._undo = self.hub.point_boss_at_me()
        # The local GitHub's CI answers at once with one passing check: the benchmark measures the
        # harness, not a CI queue (without it every PR waits sandbox_ci_timeout_seconds for checks
        # that never come).
        self.hub.world.checks = {"*": [{"name": "bench-ci"}]}
        with contextlib.suppress(ImportError):
            from boss_engine.services import transient_retry

            transient_retry.DELAYS = (0.2, 0.2)
        self.project = f"p_bench{uuid.uuid4().hex[:10]}"
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_bench', 'harness bench') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_bench', "
                           "'org_bench', 'harness bench') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, 'ws_bench', 'harness bench', :p)"), {"p": self.project})

    def now(self):
        with self.engine.begin() as c:
            return c.execute(self.text("SELECT now()")).scalar()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._undo()
        with contextlib.suppress(Exception):
            self.hub.stop()
        with contextlib.suppress(Exception):
            self.engine.dispose()


def _git(*args: str, cwd: Optional[Path] = None, timeout: int = 120) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def _safe_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _SAFE_ENV if k in os.environ}
    env.update(extra or {})
    return env


def _run_command(command: str, cwd: Path, timeout: int = 300) -> dict[str, Any]:
    started = time.monotonic()
    try:
        done = subprocess.run(shlex.split(command), cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=_safe_env({"CI": "1"}))
        ok, code, out = done.returncode == 0, done.returncode, (done.stdout + done.stderr)
    except subprocess.TimeoutExpired as exc:
        ok, code, out = False, None, f"timed out after {timeout}s: {exc}"
    except OSError as exc:
        ok, code, out = False, None, f"could not run: {exc}"
    return {"command": command, "ok": ok, "exit_code": code,
            "seconds": round(time.monotonic() - started, 1), "tail": out[-1500:]}


def acceptance_checks(suite: dict[str, Any], task: dict[str, Any], *, bare: Path, branch: str,
                      base_sha: str, pull_request: Optional[str], work: Path) -> dict[str, Any]:
    """The benchmark's own checks on the pull request's head. Never the agent's account."""
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail[:600]})

    acc = task["acceptance"]
    if not pull_request or not branch:
        check("a pull request was opened", False, "no pull request, so nothing to accept")
        return {"passed": False, "checks": checks}
    dest = work / "accept" / f"{task['id']}-{uuid.uuid4().hex[:6]}"
    try:
        _git("clone", "--quiet", str(bare), str(dest))
        _git("checkout", "--quiet", branch, cwd=dest)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        check("the pull request's branch can be checked out", False, str(exc))
        return {"passed": False, "checks": checks}
    head = _git("rev-parse", "HEAD", cwd=dest)
    check("the branch carries a change", head != base_sha, f"head {head[:10]} base {base_sha[:10]}")
    changed = set(_git("diff", "--name-only", base_sha, "HEAD", cwd=dest).splitlines())
    for rel in acc.get("expect_changed") or []:
        check(f"{rel} changed", rel in changed, "changed" if rel in changed else "not changed")
    for rel in acc.get("expect_files") or []:
        check(f"{rel} exists", (dest / rel).is_file())
    for command in task["test_commands"]:
        r = _run_command(command, dest)
        check(f"the task's own tests pass on a clean checkout: {command}", r["ok"],
              f"exit {r['exit_code']} in {r['seconds']}s: {r['tail'][-400:]}")
    held = dest / "bench_acceptance"
    if held.exists():
        # Whatever the branch put here is replaced: the held tests are the benchmark's, not the
        # agent's, and an agent cannot plant its own copy.
        shutil.rmtree(held)
    shutil.copytree(resolve(suite, acc["hidden"]), held)
    for inc in acc.get("include") or []:
        src = resolve(suite, inc)
        shutil.copy2(src, held / src.name)
    for command in acc["commands"]:
        r = _run_command(command, dest)
        check(f"held acceptance tests: {command}", r["ok"],
              f"exit {r['exit_code']} in {r['seconds']}s: {r['tail'][-600:]}")
    return {"passed": all(c["ok"] for c in checks), "checks": checks, "head": head,
            "changed_files": sorted(changed)}


def run_task(world: World, suite: dict[str, Any], task: dict[str, Any], *, iteration: int,
             bench_run_id: str, harness: str, model: Optional[str], overrides: dict[str, Any],
             caps: str, extra_faults: list[str], budget_cap_micros: Optional[int],
             timeout_s: int) -> dict[str, Any]:
    """One task, one build, measured. Never raises: an error is the result's `error`."""
    from boss_engine.config import get_settings
    from boss_engine.services import code_task_service, scripted_engine
    from boss_engine.services.jobs import JobLeaseService
    from boss_engine.services.llm_telemetry import clear_llm_context

    import e2e_gate_agent

    result: dict[str, Any] = {"task_id": task["id"], "kind": task["kind"], "iteration": iteration,
                              "success": False}
    repo = f"bench-{task['id']}-{iteration}-{uuid.uuid4().hex[:6]}"
    fixture = resolve(suite, suite["fixtures"][task["fixture"]]["path"])
    files = {rel: data.decode("utf-8") for rel, data in tree_files(fixture).items()}
    bare = world.hub.seed(OWNER, repo, files)
    base_sha = _git("--git-dir", str(bare), "rev-parse", "HEAD")
    result["fixture"] = {"name": task["fixture"], "commit": base_sha,
                         "sha256": suite["fixtures"][task["fixture"]]["sha256"]}
    if harness == "scripted":
        scripted = task.get("scripted") or {}
        faults = sorted(set(scripted.get("faults") or []) | set(extra_faults))
        os.environ.update({"BOSS_SCRIPTED_ENGINE": "1", "BOSS_SCRIPTED_FAULTS": ",".join(faults),
                           "BOSS_SCRIPTED_CAPS": caps, "BOSS_SCRIPTED_APP_DIR": "",
                           "BOSS_SCRIPTED_FIXTURE": str(resolve(suite, scripted["solution"]))})
        scripted_engine.reset()
        result["scripted_faults"] = faults
    defaults = suite.get("defaults") or {}
    update = {**FORCED, "github_token": "gate-local-token", "code_agent_engine": harness,
              "code_review_engine": harness, "target_github_owner": OWNER,
              "target_github_repo": repo, **overrides}
    if budget_cap_micros is not None:
        update["code_task_budget_usd_micros"] = budget_cap_micros
    settings = get_settings().model_copy(update=update)
    jobs = JobLeaseService(settings)
    model_config = {"default": model} if model else ({"default": "scripted"}
                                                     if harness == "scripted" else {})
    since = world.now()
    t0 = time.monotonic()
    run_id = None
    lineage: list[str] = []
    timer = None
    try:
        with world.engine.begin() as c:
            run_id = code_task_service.start_code_task(
                c, project_id=world.project, task_id=None, title=task["title"],
                instructions=task["instructions"].strip(),
                acceptance_criteria=list(task["acceptance_criteria"]),
                test_commands=list(task["test_commands"]), owner=OWNER, repo=repo,
                base_branch="main",
                finalize_ctx={"app_name": task["id"],
                              # The bake-off's batch identity (services/bakeoff.py): the
                              # /v1/bakeoff/{batch}/report aggregation reads a bench run as one.
                              "bakeoff": {"batch_id": bench_run_id, "config": harness,
                                          "task_key": task["id"], "iteration": iteration,
                                          "expect": "pass", "shape": task["kind"]}},
                jobs=jobs, model_config=model_config, open_pr=bool(defaults.get("open_pr", True)),
                allow_duplicate=True, profile=defaults.get("profile"),
                risk_tier=defaults.get("risk_tier"))
        result["code_task_run_id"] = run_id
        lineage.append(run_id)

        def over_time() -> None:
            result["timed_out"] = True
            with contextlib.suppress(Exception), world.engine.begin() as c:
                code_task_service.cancel_run(c, run_id, world.project, actor="harness-bench",
                                             reason=f"over the benchmark's {timeout_s}s limit")

        timer = threading.Timer(timeout_s, over_time)
        timer.daemon = True
        timer.start()
        code_task_service.run_code_task(world.engine, settings, jobs, run_id)
        # THE LINEAGE. A cold review-repair round (or a checks-failing continuation) is a NEW
        # run, queued as the parent ends; the worker would run it next, so the benchmark does,
        # and the task's result is the lineage's: costs summed, success judged on the last run.
        current = run_id
        for _ in range(MAX_LINEAGE):
            with world.engine.begin() as c:
                child = c.execute(world.text(
                    "SELECT id FROM code_task_runs WHERE project_id = :p "
                    "AND finalize_ctx->>'parent_run_id' = :r AND status NOT IN "
                    "('completed', 'failed', 'cancelled', 'blocked') ORDER BY created_at LIMIT 1"),
                    {"p": world.project, "r": current}).scalar()
            if not child or result.get("timed_out"):
                break
            lineage.append(child)
            run_id = current = child
            code_task_service.run_code_task(world.engine, settings, jobs, child)
    except Exception as exc:  # noqa: BLE001 — the run's record says what happened; keep going
        result["error"] = f"{type(exc).__name__}: {exc}"[:500]
        traceback.print_exc()
    finally:
        if timer is not None:
            timer.cancel()
        clear_llm_context()
    wall = time.monotonic() - t0
    until = world.now()
    if run_id is None:
        result.update(metrics={"wall_s": round(wall, 2)}, components={"started": False})
        return result
    with world.engine.begin() as c:
        record = metrics_mod.merge_lineage([
            metrics_mod.read_run(c, rid, project_id=world.project, since=since, until=until)
            for rid in lineage])
    result["lineage"] = list(lineage)
    run = record["run"]
    acceptance = acceptance_checks(suite, task, bare=bare,
                                   branch=(run.get("finalize_ctx") or {}).get("work_branch") or "",
                                   base_sha=base_sha, pull_request=run.get("pull_request_url"),
                                   work=world.work)
    measured = metrics_mod.measure(record, wall_s=wall, acceptance=acceptance)
    result.update(measured)
    result["acceptance"] = acceptance
    # The release gate's own end-of-build checks (scripts/e2e_gate_agent.py), as information.
    result["gate_agent_checks"] = [
        {"check": name, "ok": ok, "detail": detail[:200]}
        for name, ok, detail in e2e_gate_agent.final_checks(run, record["events"])]
    if result.get("timed_out"):
        result["success"] = False
        result.setdefault("components", {})["within_time_limit"] = False
    return result


# ── the commands ─────────────────────────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    suite = load_suite(Path(args.suite) if args.suite else None)
    tasks = select_tasks(suite, args.tasks)
    harness, model = parse_engine(args.engine)
    model = args.model or model
    if args.level not in LEVELS:
        raise Refused(f"--level {args.level!r}: only {', '.join(LEVELS)} exists today "
                      "(guided and stepwise arrive with phase 3)")
    if args.repeat < 1:
        raise Refused("--repeat must be at least 1")
    extra_faults = [f.strip() for f in (args.scripted_faults or "").split(",") if f.strip()]
    if extra_faults and harness != "scripted":
        raise Refused("--scripted-faults applies to the scripted engine only")
    bad = [f for f in extra_faults if f not in SCRIPTED_FAULTS]
    if bad:
        raise Refused(f"unknown scripted fault(s): {', '.join(bad)}")
    why = engine_preflight(harness, allow_unisolated=args.allow_unisolated,
                           budget_usd=args.budget_usd, dry_run=args.dry_run)
    if why:
        raise Refused("; ".join(why))
    db_url = database_url(args.db_url)
    os.environ["DATABASE_URL"] = db_url
    overrides = parse_sets(args.set)
    from boss_engine.config import get_settings

    settings = get_settings().model_copy(update={**FORCED, **overrides})
    config, fingerprint = run_config(suite=suite, engine_spec=args.engine, model=model,
                                     level=args.level, settings=settings, overrides=overrides,
                                     caps=args.scripted_caps, extra_faults=extra_faults,
                                     scripted=harness == "scripted")
    boss_sha, dirty = boss_revision()
    rules = rules_for(suite, args.engine)
    plan = [(i, t["id"]) for i in range(1, args.repeat + 1) for t in tasks]
    say(f"suite {suite['suite']} v{suite['version']} ({suite['_sha256'][:12]}), engine "
        f"{args.engine}, level {args.level}, {len(tasks)} task(s) x {args.repeat} = "
        f"{len(plan)} build(s), config {fingerprint}, BOSS {boss_sha[:10]}{' (dirty)' if dirty else ''}")
    if args.dry_run:
        say("dry run: the plan, and nothing else (no database write, no build, no model call)")
        for i, tid in plan:
            say(f"  pass {i}: {tid}")
        say(f"  config: {json.dumps(config, sort_keys=True)}")
        say(f"  guards: {json.dumps(rules['guards'], sort_keys=True)}")
        if args.baseline:
            base = store.load_baseline(args.baseline)
            say(f"  baseline {args.baseline}: "
                + ("found" if base else "NOT FOUND (a real run would stop here)"))
        return EXIT_OK
    if args.baseline and store.load_baseline(args.baseline) is None and "/" not in args.baseline:
        # Checked again against the database once it is open; a missing file is said up front.
        say(f"baseline {args.baseline}: no file in bench/harness/baselines; looking in the database")

    run_id = store.new_run_id()
    work = Path(tempfile.mkdtemp(prefix="boss-harness-bench-"))
    world = None
    results: list[dict[str, Any]] = []
    status = "failed"
    report_path = Path(args.report) if args.report else work / f"{run_id}.json"
    meta = {"id": run_id, "suite": suite["suite"], "suite_version": str(suite["version"]),
            "suite_sha256": suite["_sha256"], "engine": args.engine, "model": model,
            "level": args.level, "boss_sha": boss_sha, "boss_dirty": dirty, "config": config,
            "config_fingerprint": fingerprint, "rules": rules,
            "task_ids": [t["id"] for t in tasks], "repeat": args.repeat,
            "budget_usd": args.budget_usd, "started_at": store.now_iso()}
    try:
        world = World(db_url, work)
        meta["project_id"] = world.project
        with world.engine.begin() as c:
            store.create_run(c, meta)
        spent_micros = 0
        budget_micros = (int(round(args.budget_usd * 1_000_000))
                         if args.budget_usd is not None else None)
        for iteration, tid in plan:
            task = next(t for t in tasks if t["id"] == tid)
            if budget_micros is not None and spent_micros >= budget_micros:
                r = {"task_id": tid, "kind": task["kind"], "iteration": iteration,
                     "skipped": True, "success": False, "error": "the budget was spent"}
                say(f"pass {iteration} {tid}: SKIPPED (budget {args.budget_usd} USD spent)")
            else:
                cap = None
                if budget_micros is not None:
                    cap = min(int(settings.code_task_budget_usd_micros or budget_micros),
                              budget_micros - spent_micros)
                say(f"pass {iteration} {tid}: building ...")
                r = run_task(world, suite, task, iteration=iteration, bench_run_id=run_id,
                             harness=harness, model=model, overrides=overrides,
                             caps=args.scripted_caps, extra_faults=extra_faults,
                             budget_cap_micros=cap,
                             timeout_s=int((suite.get("defaults") or {}).get("timeout_s", 1800)))
                spent_micros += int(round(float((r.get("metrics") or {}).get("cost_usd") or 0)
                                          * 1_000_000))
                m = r.get("metrics") or {}
                failing = [k for k, v in (r.get("components") or {}).items() if not v]
                say(f"pass {iteration} {tid}: {'PASS' if r.get('success') else 'FAIL'} "
                    f"{m.get('wall_s', 0):.1f}s calls={m.get('model_calls', 0)} "
                    f"turns={m.get('turns', 0)} repair={m.get('repair_rounds', 0)} "
                    f"interventions={m.get('interventions', 0)} cost=${m.get('cost_usd', 0):.4f}"
                    + (f"  failing: {', '.join(failing)}" if failing else "")
                    + (f"  error: {r['error'][:160]}" if r.get("error") else ""))
            results.append(r)
            with world.engine.begin() as c:
                store.add_result(c, run_id, r)
        status = "incomplete" if any(r.get("skipped") for r in results) else "completed"
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        say(f"the run failed: {type(exc).__name__}: {exc}")
        status = "failed"
    finally:
        meta.update(status=status, finished_at=store.now_iso())
        report = {"schema": REPORT_SCHEMA, "run": meta, "results": results,
                  "summary": store.summarize(results)}
        store.write_report(report_path, report)
        say(f"report: {report_path}")
        if world is not None:
            with contextlib.suppress(Exception), world.engine.begin() as c:
                store.finish_run(c, run_id, status=status, summary=report["summary"],
                                 report_path=str(report_path))
    code = EXIT_OK if status == "completed" else EXIT_REFUSED
    try:
        if args.save_baseline and status == "completed":
            path = store.write_report(store.baseline_path(args.save_baseline),
                                      {**report, "run": {**meta, "baseline_name": args.save_baseline}})
            with world.engine.begin() as c:
                store.record_baseline(c, run_id, args.save_baseline)
            say(f"saved as baseline {args.save_baseline}: {path}")
        elif args.save_baseline:
            say(f"NOT saved as baseline {args.save_baseline}: the run is {status}")
        if args.baseline:
            with world.engine.begin() as c:
                base = store.load_baseline(args.baseline, c)
            if base is None:
                say(f"no baseline named {args.baseline}")
                return EXIT_REFUSED
            cmp = cmp_mod.compare(base, report, rules, allow_suite_change=args.allow_suite_change,
                                  allow_cross_engine=args.cross_engine)
            print(cmp_mod.render(cmp), flush=True)
            with contextlib.suppress(Exception), world.engine.begin() as c:
                store.record_comparison(c, run_id, args.baseline, cmp)
            if args.compare_out:
                Path(args.compare_out).write_text(json.dumps(cmp, indent=1, default=str))
            code = max(code, cmp["exit_code"])
    finally:
        if world is not None:
            world.close()
        if not args.keep_work:
            shutil.rmtree(work / "github", ignore_errors=True)
            shutil.rmtree(work / "accept", ignore_errors=True)
    return code


def _load_any(ref: str, db_url: Optional[str]) -> dict[str, Any]:
    if ref.endswith(".json") or "/" in ref:
        return store.read_report(Path(ref))
    from sqlalchemy import create_engine

    eng = create_engine(database_url(db_url), future=True)
    try:
        with eng.begin() as c:
            rep = store.load_report(c, ref)
    finally:
        eng.dispose()
    if rep is None:
        raise Refused(f"no benchmark run {ref}")
    return rep


def cmd_compare(args: argparse.Namespace) -> int:
    cand = _load_any(args.run, args.db_url)
    base = None
    if args.baseline.endswith(".json") or "/" in args.baseline:
        base = store.read_report(Path(args.baseline))
    else:
        base = store.load_baseline(args.baseline)
        if base is None and (args.db_url or os.environ.get("BOSS_BENCH_DATABASE_URL")):
            from sqlalchemy import create_engine

            eng = create_engine(database_url(args.db_url), future=True)
            with eng.begin() as c:
                base = store.load_baseline(args.baseline, c)
            eng.dispose()
    if base is None:
        raise Refused(f"no baseline named {args.baseline}")
    rules = cand["run"].get("rules") or {}
    if args.suite or not rules:
        rules = rules_for(load_suite(Path(args.suite) if args.suite else None), cand["run"]["engine"])
    cmp = cmp_mod.compare(base, cand, rules, allow_suite_change=args.allow_suite_change,
                          allow_cross_engine=args.cross_engine)
    if args.json:
        print(json.dumps(cmp, indent=1, default=str))
    else:
        print(cmp_mod.render(cmp))
    return cmp["exit_code"]


def cmd_report(args: argparse.Namespace) -> int:
    from sqlalchemy import create_engine

    eng = create_engine(database_url(args.db_url), future=True)
    try:
        with eng.begin() as c:
            rows = store.harness_score(c)
    finally:
        eng.dispose()
    print(json.dumps(rows, indent=1, default=str) if args.json else store.render_score(rows))
    return EXIT_OK


ESTIMATE_SQL = """
    SELECT r.id, r.status, EXTRACT(EPOCH FROM (r.updated_at - r.created_at)) AS wall_s,
           COALESCE(SUM(l.cost_usd_micros), 0) AS cost, COUNT(l.id) AS calls,
           COALESCE(SUM(l.prompt_tokens + l.cache_read_tokens + l.cache_write_tokens), 0) AS tin,
           COALESCE(SUM(l.cache_read_tokens), 0) AS cread,
           COALESCE(SUM(l.completion_tokens), 0) AS tout,
           BOOL_OR(l.included_in_subscription) AS included
      FROM code_task_runs r JOIN llm_calls l ON l.code_task_run_id = r.id
     WHERE r.created_at > now() - make_interval(days => :days)
       AND r.status IN ('completed', 'failed', 'blocked')
     GROUP BY r.id, r.status, r.updated_at, r.created_at
    HAVING BOOL_OR(l.engine = :engine)
"""


def cmd_estimate(args: argparse.Namespace) -> int:
    """Expected cost and time per task from past builds, read-only (no run, no write)."""
    from sqlalchemy import create_engine, text

    if not args.history_db_url:
        raise Refused("--history-db-url is required (it is opened read-only)")
    url = args.history_db_url
    if url.startswith(("postgresql://", "postgres://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    eng = create_engine(url, future=True,
                        connect_args={"options": "-c default_transaction_read_only=on"})
    try:
        with eng.connect() as c:
            rows = [dict(r) for r in c.execute(text(ESTIMATE_SQL),
                                               {"days": args.days, "engine": args.engine}).mappings()]
    finally:
        eng.dispose()

    def q(vals, p):
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(round(p * (len(vals) - 1))))] if vals else None

    done = [r for r in rows if r["status"] == "completed"]
    out = {"engine": args.engine, "days": args.days, "builds": len(rows),
           "completed": len(done)}
    for name, key, scale in (("cost_usd", "cost", 1e-6), ("wall_min", "wall_s", 1 / 60),
                             ("model_calls", "calls", 1), ("tokens_in", "tin", 1),
                             ("tokens_out", "tout", 1)):
        vals = [float(r[key] or 0) * scale for r in done]
        out[name] = {"p50": q(vals, 0.5), "p75": q(vals, 0.75), "p90": q(vals, 0.9)}
    tin = sum(float(r["tin"] or 0) for r in done)
    out["cache_share"] = (sum(float(r["cread"] or 0) for r in done) / tin) if tin else None
    out["on_subscription_share"] = (sum(1 for r in done if r["included"]) / len(done)) if done else None
    print(json.dumps(out, indent=1, default=str))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scripts/harness-bench",
                                 description="The BOSS harness benchmark (BOSSAPP-959).")
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="run the suite on one engine")
    run.add_argument("--engine", default="scripted",
                     help="scripted | claude_code | codex | native:<model> (default scripted)")
    run.add_argument("--model", help="the model the engine runs (default: the engine's own)")
    run.add_argument("--level", default="delegate", help=f"scaffold level: {', '.join(LEVELS)}")
    run.add_argument("--tasks", default="all", help="all, or comma-separated task ids")
    run.add_argument("--repeat", type=int, default=1, help="passes over the suite (spread needs >= 2)")
    run.add_argument("--budget-usd", type=float, help="the most the whole run may spend (list price)")
    run.add_argument("--baseline", help="compare with this baseline (a name or a report path)")
    run.add_argument("--save-baseline", help="save this run as the named baseline")
    run.add_argument("--dry-run", action="store_true", help="print the plan; build nothing")
    run.add_argument("--db-url", help="a boss_test* database (default BOSS_BENCH_DATABASE_URL)")
    run.add_argument("--suite", help="the suite file (default bench/harness/suite.yaml)")
    run.add_argument("--report", help="where to write the JSON report")
    run.add_argument("--compare-out", help="where to write the comparison as JSON")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="a harness setting for this run (recorded in its config)")
    run.add_argument("--scripted-caps", default="resume,read_only,turn_limit,reports_cost,streams_turns",
                     help="the scripted engine's capabilities (default: every one, as Claude Code)")
    run.add_argument("--scripted-faults", default="",
                     help="faults added to every task on the scripted engine (e.g. failing_test)")
    run.add_argument("--allow-unisolated", action="store_true",
                     help="let a real engine run without boss-sandbox-exec (as you, unisolated)")
    run.add_argument("--allow-suite-change", action="store_true")
    run.add_argument("--cross-engine", action="store_true")
    run.add_argument("--keep-work", action="store_true", help="keep the local GitHub and checkouts")
    comp = sub.add_parser("compare", help="compare a run with a baseline")
    comp.add_argument("run", help="a run id (in --db-url) or a report .json")
    comp.add_argument("--baseline", required=True)
    comp.add_argument("--db-url")
    comp.add_argument("--suite", help="judge by this suite's rules instead of the run's own")
    comp.add_argument("--json", action="store_true")
    comp.add_argument("--allow-suite-change", action="store_true")
    comp.add_argument("--cross-engine", action="store_true")
    rep = sub.add_parser("report", help="the harness score: the latest run per engine and level")
    rep.add_argument("--db-url")
    rep.add_argument("--json", action="store_true")
    est = sub.add_parser("estimate", help="expected cost and time per task from past builds")
    est.add_argument("--history-db-url", help="a database with build history (opened read-only)")
    est.add_argument("--engine", default="claude_code")
    est.add_argument("--days", type=int, default=30)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    try:
        return {"run": cmd_run, "compare": cmd_compare, "report": cmd_report,
                "estimate": cmd_estimate}[args.cmd](args)
    except (Refused, SuiteError) as exc:
        print(f"harness-bench: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
