"""THE ENGINES GATE (tier A of the end-to-end release gate). No model is called.

BOSS is a harness independent of any model: its BEHAVIOUR — the stages, the gates, the fix rounds,
the pull request, the events, the build profile and the failure words — must be identical on every
engine; only quality and speed may vary. This gate runs BOSS's REAL governed build pipeline
(governed_code_task_service.run_governed_code_task, entered through code_task_service.
start_code_task as the API enters it) end to end on the scripted conformance engine
(services/scripted_engine.py), which plays a deterministic script per stage with no model, and
asserts the same expectations for every engine profile:

  * "poorest": no sessions, no read-only mode, no turn limit, no cost — BOSS's fallbacks run
    (a warm fix round on a seeded session, a reviewer on a copy of the tree);
  * "richest": every capability Claude Code has.

Scenarios: a clean build; a failing test (the fix round repairs it); a reviewer BLOCKER (its warm
fix round); a server that crashes on start (the app-start proof fails it, the round repairs it);
and an engine that cannot run at all (the failure words). Each scenario has ONE expectation,
written once, that every profile must meet: the stage sequence, the gate verdicts, whether a fix
round ran, the run's end state, the PR, the profile's fields and the failure words.

    python3 tests/e2e/gate/engines.py --out results.json        (needs BOSS_E2E_GATE_PG_URL)
    scripts/e2e-gate.sh engines                                 (the same, in a throwaway container)

The world is local: a throwaway Postgres with BOSS's full schema, and a local GitHub (git
smart-HTTP + the REST API, tests/e2e/gate/remote.py) that the clone and the pull request reach
through BOSS's own two seams for a GitHub address (repo_host.clone_url_for, GitHubAdapter).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

# A build entered without a person, as the engine suite enters one (engine/tests/conftest.py):
# identity runs in shadow, with a fixed test-only server key. The gate tests the pipeline's
# behaviour per engine, not who may start it (the plumbing gate's approval checks do that).
os.environ.setdefault("BOSS_AUTHZ_MODE", "shadow")
os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY",
                      "dGVzdC1vbmx5LWlkZW50aXR5LXNlcnZlci1rZXktLS0tLS0tLS0tLS0tLS0=")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FIXTURE = HERE.parent / "fixtures" / "plain-node" / "plain"
OWNER = "gate"
RESULTS: list[dict[str, Any]] = []

#: The engine profiles every scenario is run on. The expectations do not know which one ran.
PROFILES = {
    "poorest": "",
    "richest": "resume,read_only,turn_limit,reports_cost,streams_turns",
}

#: What every engine must show, per scenario (the claude_code path's expectations, written once).
#: `gates_fail` are gates allowed to fail on the FIRST verdict; `final` is the run's end status.
SCENARIOS: dict[str, dict[str, Any]] = {
    "clean": {"faults": "", "final": "completed", "pr": True, "fix_round": False,
              "stages": ["write_tests", "implement", "review_code", "security_check",
                         "consistency_check", "check_tests"]},
    "failing_test": {"faults": "failing_test", "final": "completed", "pr": True,
                     "fix_round": True,
                     "stages": ["write_tests", "implement", "review_code"]},
    "blocker": {"faults": "blocker", "final": "completed", "pr": True, "fix_round": True,
                "stages": ["write_tests", "implement", "review_code"]},
    "crash_on_start": {"faults": "crash_on_start", "final": "completed", "pr": True,
                       "fix_round": True,
                       "stages": ["write_tests", "implement"]},
    "engine_crash": {"faults": "crash", "final": "failed", "pr": False, "fix_round": False,
                     "stages": [], "words": "engine crashed"},
}


class CheckFailed(Exception):
    pass


def check(name: str):
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        def run(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            row: dict[str, Any] = {"check": name, "bug": "engines"}
            try:
                detail = fn(*args, **kwargs)
                row.update(status="PASS", detail=" ".join(str(detail or "").split())[:400])
                return True
            except CheckFailed as exc:
                row.update(status="FAIL", detail=" ".join(str(exc).split())[:700])
                return False
            except Exception as exc:  # noqa: BLE001
                tb = traceback.extract_tb(exc.__traceback__)
                where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb)
                              if "boss_engine" in f.filename), "")
                row.update(status="FAIL", detail=" ".join(f"{type(exc).__name__}: {exc}".split())[:500]
                           + (f" (at {where})" if where else ""))
                traceback.print_exc()
                return False
            finally:
                row["seconds"] = round(time.monotonic() - started, 1)
                RESULTS.append(row)
                print(f"[gate] {row['status']:4} {name} ({row['seconds']}s) "
                      f"{row.get('detail', '')[:300]}", flush=True)
        return run
    return wrap


# ── The world ────────────────────────────────────────────────────────────────────────────────

def _database(pg_url: str, name: str) -> str:
    import psycopg
    from psycopg import sql

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    base, _, _ = pg_url.rpartition("/")
    return f"{base}/{name}"


class World:
    """A full-schema database with one project, a local GitHub, and BOSS pointed at both."""

    def __init__(self, pg_url: str, work: Path):
        self.work = work
        url = _database(pg_url, "boss_test_e2egate_engines")
        self.url = url.replace("postgresql://", "postgresql+psycopg://")
        os.environ["DATABASE_URL"] = self.url
        from sqlalchemy import create_engine, text

        from boss_engine.db import ensure_schema

        self.engine = create_engine(self.url, future=True)
        ensure_schema(self.engine)
        self.text = text
        self.project = f"p_eng{uuid.uuid4().hex[:8]}"
        with self.engine.begin() as c:
            c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_gate', 'gate') "
                           "ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_gate', "
                           "'org_gate', 'gate') ON CONFLICT DO NOTHING"))
            c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                           "VALUES (:p, 'ws_gate', 'gate', :p)"), {"p": self.project})
        # The engine boundary, as the API process composes it (apps/api/app/main.py): the
        # Postgres-backed runtime the code-task step runs on.
        from apps.api.app.bootstrap_ports import build_ports
        from boss_engine.config import get_settings
        from boss_engine.ports import configure_engine_ports

        configure_engine_ports(build_ports(get_settings(), self.engine))
        from remote import LocalGitHub

        self.hub = LocalGitHub(work / "github").start()
        self.stack = contextlib.ExitStack()
        self._patch_github()
        try:
            from boss_engine.services import transient_retry

            transient_retry.DELAYS = (0.2, 0.2)      # the policy, with the gate's patience
        except ImportError:
            pass

    def seed(self, repo: str) -> None:
        self.hub.seed(OWNER, repo, {"README.md": f"# {repo}\n\nA BOSS engines-gate repository.\n"})

    def _patch_github(self) -> None:
        """BOSS's two GitHub seams, pointed at the local one (never github.com)."""
        from boss_engine.services import github_adapter, repo_host

        hub = self.hub
        real_init = github_adapter.GitHubAdapter.__init__

        def init(adapter, settings, owner=None, repo=None, token=None, api_base_url=None):
            real_init(adapter, settings, owner=owner, repo=repo, token=token or "gate-local-token",
                      api_base_url=hub.api_url)

        def clone_url_for(host, owner, repo, token=""):
            return hub.git_url(owner, repo)

        github_adapter.GitHubAdapter.__init__ = init
        repo_host.clone_url_for = clone_url_for
        self.stack.callback(setattr, github_adapter.GitHubAdapter, "__init__", real_init)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.stack.close()
        with contextlib.suppress(Exception):
            self.hub.stop()
        with contextlib.suppress(Exception):
            self.engine.dispose()


# ── One build ────────────────────────────────────────────────────────────────────────────────

def run_build(world: World, *, faults: str, caps: str, tag: str) -> dict[str, Any]:
    """Start a build as the API does and run it as the worker does, on the scripted engine."""
    from boss_engine.config import get_settings
    from boss_engine.services import code_task_service, scripted_engine
    from boss_engine.services.build_profile import compute_profile
    from boss_engine.services.jobs import JobLeaseService
    from boss_engine.services.llm_telemetry import clear_llm_context

    os.environ["BOSS_SCRIPTED_ENGINE"] = "1"
    os.environ["BOSS_SCRIPTED_FAULTS"] = faults
    os.environ["BOSS_SCRIPTED_CAPS"] = caps
    os.environ["BOSS_SCRIPTED_FIXTURE"] = str(FIXTURE)
    os.environ["BOSS_SCRIPTED_APP_DIR"] = ""
    scripted_engine.reset()
    repo = f"eng-{tag}-{uuid.uuid4().hex[:6]}"
    world.seed(repo)
    settings = get_settings().model_copy(update={
        "enable_code_task": True, "governed_coding_pipeline": True,
        "github_token": "gate-local-token",
        "code_agent_engine": "scripted", "code_review_engine": "scripted",
        "target_github_owner": OWNER, "target_github_repo": repo,
    })
    jobs = JobLeaseService(settings)
    with world.engine.begin() as c:
        run_id = code_task_service.start_code_task(
            c, project_id=world.project, task_id=None,
            title=f"Slug service ({tag})",
            instructions=("Build a tiny plain Node ESM service at the repository root: slug(text) "
                          "in src/slug.js and an HTTP server in src/server.js answering "
                          "GET /?q=<text> with {\"slug\": ...} on $PORT. Tests under node --test."),
            acceptance_criteria=["slug lowercases and joins words with hyphens",
                                 "GET /?q= answers the slug as JSON"],
            test_commands=["npm test"], owner=OWNER, repo=repo, base_branch="main",
            finalize_ctx={"app_name": "slug"}, jobs=jobs,
            model_config={"default": "scripted"}, open_pr=True, allow_duplicate=True,
            # THE FULL REVIEW PANEL. With no project default the community edition picks `lean`
            # (pipeline_profile_service.default_profile_name), which seats only `regression`, so
            # the three reviewer seats this gate compares across engines would never run.
            # `balanced` at tier 1 seats every reviewer without the design chain or the
            # plan-approval gate (a person's decision would park an unattended gate run).
            profile="balanced", risk_tier=1)
    started = time.monotonic()
    error = None
    try:
        code_task_service.run_code_task(world.engine, settings, jobs, run_id)
    except Exception as exc:  # noqa: BLE001 — the run's own record says what happened
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        clear_llm_context()
    t = world.text
    with world.engine.begin() as c:
        run = dict(c.execute(t("SELECT * FROM code_task_runs WHERE id=:i"), {"i": run_id})
                   .mappings().one())
        events = [dict(r) for r in c.execute(t(
            "SELECT event_type, data FROM events WHERE entity_id=:i ORDER BY created_at, id"),
            {"i": run_id}).mappings()]
        calls = [dict(r) for r in c.execute(t(
            "SELECT stage, engine, model, ok, turns FROM llm_calls WHERE code_task_run_id=:i "
            "ORDER BY created_at"), {"i": run_id}).mappings()]
        gate_row = c.execute(t(
            "SELECT status, gates_passed, gates_failed FROM quality_gates "
            "WHERE code_task_run_id=:i"), {"i": run_id}).mappings().first()
        turns = [dict(r) for r in c.execute(t(
            "SELECT stage, direction, tool_name, engine FROM agent_turns "
            "WHERE code_task_run_id=:i ORDER BY created_at, turn_index"),
            {"i": run_id}).mappings()]
    profile = None
    with contextlib.suppress(Exception):
        profile = compute_profile(run_id, world.engine)
    gates = {}
    if gate_row is not None:
        gates = {**{g: True for g in gate_row["gates_passed"] or []},
                 **{g: False for g in gate_row["gates_failed"] or []}}
    return {"run_id": run_id, "run": run, "events": events, "calls": calls, "turns": turns,
            "gates": gates,
            "profile": profile, "error": error, "seconds": round(time.monotonic() - started, 1),
            "script": [dict(x) for x in scripted_engine.run_state(run_id).calls]}


# ── What every engine must show ──────────────────────────────────────────────────────────────

FIX_EVENTS = ("code_task.inrun_convergence_round", "code_task.repair_attempt",
              "code_task.implement_continued", "code_task.install_fixed")


def observed(b: dict[str, Any]) -> dict[str, Any]:
    """The engine-independent shape of one build: what the expectations compare."""
    run = b["run"]
    gates = b.get("gates") or {}
    stages = []
    for row in b["script"]:
        if row["stage"] not in stages:
            stages.append(row["stage"])
    kinds = [e["event_type"] for e in b["events"]]
    return {
        "status": run.get("status"),
        "pr": bool(run.get("pull_request_url")),
        "stages": stages,
        "fix_round": any(k in kinds for k in FIX_EVENTS) or sum(
            1 for r in b["script"] if r["stage"] in ("implement", "repair", "solo")) > 1,
        "gates": gates,
        "events": kinds,
        "failure": str(run.get("error") or run.get("failure_reason") or ""),
        "profile_keys": sorted((b.get("profile") or {}).get("totals") or {}),
        "tool_names": sorted({t["tool_name"] for t in b["turns"] if t.get("tool_name")}),
    }


def meets(name: str, obs: dict[str, Any]) -> list[str]:
    exp = SCENARIOS[name]
    bad = []
    if obs["status"] != exp["final"]:
        bad.append(f"status {obs['status']!r}, expected {exp['final']!r} ({obs['failure'][:200]})")
    if obs["pr"] != exp["pr"]:
        bad.append(f"pull request {'opened' if obs['pr'] else 'not opened'}")
    missing = [s for s in exp["stages"] if s not in obs["stages"]]
    if missing:
        bad.append(f"stages never ran: {missing} (ran: {obs['stages']})")
    if exp["fix_round"] and not obs["fix_round"]:
        bad.append("no fix round ran")
    if exp.get("words") and exp["words"] not in obs["failure"].lower():
        bad.append(f"failure words {obs['failure'][:200]!r} do not say {exp['words']!r}")
    stray = [n for n in obs["tool_names"] if n.islower()]
    if stray:
        bad.append(f"engine-specific tool names reached the transcript: {stray}")
    return bad


def same_behaviour(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Two engine profiles, one scenario: the behaviour must be identical."""
    diff = []
    for key in ("status", "pr", "gates", "profile_keys", "tool_names"):
        if a[key] != b[key]:
            diff.append(f"{key}: {a[key]!r} vs {b[key]!r}")
    ea = [e for e in a["events"] if e.startswith("code_task.")]
    eb = [e for e in b["events"] if e.startswith("code_task.")]
    if sorted(set(ea)) != sorted(set(eb)):
        diff.append(f"events differ: only first {sorted(set(ea) - set(eb))}, "
                    f"only second {sorted(set(eb) - set(ea))}")
    return diff


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--scenario", action="append", default=[])
    args = ap.parse_args()
    pg_url = os.environ["BOSS_E2E_GATE_PG_URL"]
    work = Path(tempfile.mkdtemp(prefix="boss-e2egate-engines-"))
    started = time.monotonic()
    world = None
    try:
        world = World(pg_url, work)
        names = args.scenario or list(SCENARIOS)
        for name in names:
            seen: dict[str, dict[str, Any]] = {}
            for profile, caps in PROFILES.items():
                holder: dict[str, Any] = {}

                @check(f"{name} on the {profile} engine")
                def one(name=name, profile=profile, caps=caps, holder=holder) -> str:
                    b = run_build(world, faults=SCENARIOS[name]["faults"], caps=caps,
                                  tag=f"{name}-{profile}".replace("_", "-"))
                    obs = observed(b)
                    holder["obs"] = obs
                    Path(work / f"{name}-{profile}.json").write_text(
                        json.dumps({**b, "observed": obs}, default=str, indent=1))
                    bad = meets(name, obs)
                    if bad:
                        raise CheckFailed("; ".join(bad))
                    return (f"{obs['status']}, stages {obs['stages']}, fix round "
                            f"{obs['fix_round']}, gates {obs['gates']}")

                one()
                if "obs" in holder:
                    seen[profile] = holder["obs"]
            if len(seen) == 2 and all(r["status"] == "PASS" for r in RESULTS[-2:]):
                @check(f"{name}: every engine behaves the same")
                def compare(seen=seen) -> str:
                    diff = same_behaviour(seen["poorest"], seen["richest"])
                    if diff:
                        raise CheckFailed("; ".join(diff))
                    return "status, PR, gates, events, profile fields and tool names identical"

                compare()
    except Exception as exc:  # noqa: BLE001
        RESULTS.append({"check": "gate set-up", "bug": "", "status": "FAIL", "seconds": 0,
                        "detail": f"{type(exc).__name__}: {exc}"[:500]})
        traceback.print_exc()
    finally:
        if world is not None:
            world.close()
        keep = Path(args.out).parent / "engines"
        with contextlib.suppress(Exception):
            keep.mkdir(parents=True, exist_ok=True)
            for f in work.glob("*.json"):
                (keep / f.name).write_text(f.read_text())
    Path(args.out).write_text(json.dumps({"results": RESULTS,
                                          "seconds": round(time.monotonic() - started, 1)}, indent=1))
    return 1 if any(r["status"] != "PASS" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
