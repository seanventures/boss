"""Every scheduler skips an archived project, and a new scheduler cannot forget to (BOSSAPP-768 S5).

An archived project takes no new work (services/projects_archive.py, spec §2.7). That promise is
only as good as the LAST scheduler somebody adds: one tick that forgets the archive check fires a
schedule, probes an app or spends a model turn for a project its owner put away. So this test
does not trust a list of the schedulers that exist today. It finds them:

1. Every function in the engine named like a scheduler entry point (tick, *_tick, run_due,
   *_due, sweep*, claim, claim_job) must be either GUARDED — its body (or the one helper it
   names, in the same file) calls `projects_archive` — or EXEMPT with the reason it starts no
   work for a project.
2. Every API route the worker calls (apps/worker/boss_worker/control_plane.py) must be either a
   LEASE route (it executes a job the worker leased: `jobs.claim_job` never leases an archived
   project's job), a TICK route whose scheduler is guarded, or EXEMPT with its reason.

A new name or a new worker route fails here until somebody decides which it is.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "boss_engine"
WORKER_CLIENT = ROOT / "apps" / "worker" / "boss_worker" / "control_plane.py"

SCHEDULER_NAME = re.compile(
    r"^(tick|tick_\w+|\w+_tick|run_due|\w+_due|sweep|sweep_\w+|claim|claim_job)$")

#: entry point -> the function in the same file whose body names projects_archive (itself, or the
#: query helper it picks its work with).
GUARDED: dict[str, str] = {
    "services/jobs.py::claim_job": "claim_job",                   # the lease: every job
    "services/knowledge/repo_index.py::enqueue_due": "_due_rows",
    "services/knowledge/ingest.py::tick": "tick",
    "services/knowledge/ingest.py::ingest_queued": "ingest_queued",
    "services/knowledge/ingest.py::ingest_due": "ingest_due",
    "services/knowledge/ingest.py::reconcile_projections": "reconcile_projections",
    "services/knowledge/embeddings.py::embed_due": "embed_due",
    "services/workflow_schedules.py::run_due": "run_due",
    "services/agent_wakeups.py::run_due": "due",
    "services/monitor_scheduler.py::tick": "tick",
    "services/monitor_scheduler.py::claim": "claim",
    "services/deployment_health.py::sweep": "sweep",
    "services/workflow_capability_runner.py::tick": "tick",
    "services/workflow_capability_runner.py::projects_with_work": "projects_with_work",
    "services/agent_process.py::tick_due": "tick_due",
    "services/focused_agents.py::tick_due": "tick_due",
    "services/factory_flow.py::tick": "tick",
    "services/process_manager.py::tick": "tick",
    "services/person_wait.py::poll_due": "poll_due",
    # Autonomous agents (BOSSAPP-902): the Chief of Staff's tick triages live projects only
    # (and its owners' turn picks items with live_sql); the learning tick runs detectors for
    # live projects only; the SRE squad's watch skips an archived project.
    "services/ambient/chief_of_staff.py::tick": "tick",
    "services/ambient/roadmap.py::tick": "tick",
    "services/incident_squad.py::tick": "tick",
}

#: New work refused at its door (ProjectArchived -> 409), checked the same way.
NEW_WORK_DOORS: dict[str, str] = {
    "services/code_task_service.py::start_code_task": "start_code_task",
    "services/workflows.py::start_run": "start_run",
    "services/workflow_schedules.py::create": "create",
    "services/agent_wakeups.py::arm_monitor": "arm_monitor",
    "services/agent_wakeups.py::schedule": "schedule",
    "services/knowledge/repo_index.py::request": "request",
}

_IDENTITY = "identity housekeeping (credentials, keys, grants, runs): no project work"
_CLEANUP = "cleanup of what already ran or died; starts nothing"
_RECOVERY = ("recovers work already in flight; any new attempt goes through start_code_task / "
             "enqueue_job, which refuse an archived project")
_TARGETS = "probes only the targets deployment_health.sweep picked, which skips archived projects"

EXEMPT: dict[str, str] = {
    "identity/claim.py::claim": "the owner claim at install time, not a scheduler",
    "services/ambient/signals.py::sweep": "records what the sensors saw; starts no work — the "
                                          "Chief of Staff's tick (guarded) never triages an "
                                          "archived project's signals",
    "services/ambient/answers.py::sweep": "carries a person's answer; called only from the "
                                          "Chief of Staff's triage of a project its guarded "
                                          "tick picked",
    "services/ambient/outcomes.py::sweep": "writes the outcomes time decides on ledger rows; "
                                           "starts no work",
    "services/ambient/board.py::claim": "an agent's lease on an item a guarded tick handed it; "
                                        "opens nothing new",
    "services/lineage_sandbox.py::claim": "provisions or reuses a sandbox for a run.id already "
                                          "started elsewhere (that start refused an archived "
                                          "project); claim itself starts no new project work",
    "identity/github_setup.py::sweep_control_plane_tokens": _IDENTITY,
    "identity/grants.py::expire_due": _IDENTITY,
    "identity/maintenance.py::tick": _IDENTITY,
    "identity/migration.py::retire_due": _IDENTITY,
    "identity/runs.py::renew_due": _IDENTITY,
    "identity/signing_keys.py::rotate_if_due": _IDENTITY,
    "identity/signing_keys.py::rotate_oidc_family_if_due": _IDENTITY,
    "oidc_provider/keys.py::rotate_if_due": _IDENTITY,
    "services/ai_sre_overwatch.py::tick": "observe-only overwatch of the install's home project, "
                                          "which cannot be archived; never remediates",
    "services/ai_sre_overwatch.py::safe_tick": "wraps ai_sre_overwatch.tick",
    "services/app_alerts.py::sweep": _TARGETS,
    "services/app_metrics.py::sweep": _TARGETS,
    "services/incident_signals.py::sweep": _TARGETS,
    "services/benchmark_tuning.py::sweep_terminal_benchmarks": "the install's benchmark ledger",
    "services/build_services.py::reap_tick": _CLEANUP,
    "services/bundled_monitoring.py::tick": "keeps the install's Grafana/Prometheus files current; "
                                            "starts no project work",
    "services/concurrency_harness.py::sweep": "a test harness, not a scheduler",
    "services/focused_agents.py::tick_one": "one agent's tick: called by tick_due (guarded) or a "
                                            "person's press",
    "services/improve_boss.py::_next_due": "install-wide Improve BOSS telemetry",
    "services/improve_boss.py::_first_due": "install-wide Improve BOSS telemetry",
    "services/improve_boss.py::send_due": "install-wide Improve BOSS telemetry",
    "services/improve_boss.py::tick": "install-wide Improve BOSS telemetry",
    "services/meeting_consensus.py::sweep_stalled_meeting_runs": _CLEANUP,
    "services/notify.py::deliver_due": "delivers messages already planned; plans come from the "
                                       "monitor scheduler, which skips archived projects",
    "services/owned_process.py::sweep_orphans": _CLEANUP,
    "services/preview_service.py::sweep_preview_workdirs": _CLEANUP,
    "services/resource_reaper.py::tick": _CLEANUP,
    "services/claude_login.py::refresh_if_due": "install-wide: refreshes the Claude CLI login",
    "services/claude_login.py::tick": "install-wide: refreshes the Claude CLI login",
    "services/codex_login.py::refresh_if_due": "install-wide: refreshes the Codex CLI login",
    "services/codex_login.py::tick": "install-wide: refreshes the Codex CLI login",
    "services/subscription_keepalive.py::sweep": "pings and refreshes an existing subscription "
                                                  "login's credentials; starts no project work",
    "services/scorecards.py::sweep": "records scorecards for units that already finished",
    "services/shepherd_service.py::sweep": _RECOVERY,
    "services/shepherd_service.py::sweep_stale_previews": _CLEANUP,
    "services/shepherd_service.py::sweep_abandoned_runs": _CLEANUP,
    "services/shepherd_service.py::sweep_starved_work": _RECOVERY,
    "services/stop_watch.py::tick": "ends the processes of builds that were stopped",
    "services/system_doctor.py::tick": "the host's health, not a project's",
    "services/workflow_capability_runner.py::sweep_gate_timeouts":
        "runs only for the projects projects_with_work returned (archived ones are dropped)",
}

_LEASE = "executes a job the worker leased; jobs.claim_job never leases an archived project's job"
_HOME = ("ticks the worker's configured project, the install's home project, which cannot be "
         "archived; the engine tick is guarded as well")
_HOUSE = "worker or install housekeeping; no project work"

#: Every route the worker calls -> what keeps it from running an archived project's work.
WORKER_ROUTES: dict[str, str] = {
    "/v1/worker/poll": "tick:services/jobs.py::claim_job",
    "/v1/worker/heartbeat": _LEASE, "/v1/worker/complete": _LEASE, "/v1/worker/fail": _LEASE,
    "/v1/self/host-stall": _HOUSE,
    "/v1/activity/retention/prune": _HOUSE,
    "/v1/telemetry/tick": "install-wide Improve BOSS telemetry",
    "/v1/ambient/tick": "tick:services/ambient/chief_of_staff.py::tick",
    "/v1/ambient/learning/tick": "tick:services/ambient/roadmap.py::tick",
    "/v1/qa/benchmark-apps/tuning-sweep": "the install's benchmark ledger",
    "/v1/model-gateway/leases": _LEASE, "/v1/model-gateway/leases/{x}/heartbeat": _LEASE,
    "/v1/model-gateway/leases/{x}/invocations": _LEASE,
    "/v1/model-gateway/leases/{x}/invocations/{x}/settle": _LEASE,
    "/v1/model-gateway/leases/{x}/release": _LEASE,
    "/v1/a2a/messages/{x}/dispatch": _LEASE,
    "/v1/agent-runs/{x}/resume": _LEASE,
    "/v1/change-sets/{x}/merge/execute": _LEASE,
    "/v1/code-tasks/step": _LEASE,
    "/v1/code-tasks/{x}/redelivered": _LEASE,
    "/v1/code-tasks/{x}/review-pr-again/run": _LEASE,
    "/v1/comms/outbox/{x}/deliver": _LEASE,
    "/v1/delivery/deployments/{x}/apply": _LEASE,
    "/v1/deployments/stack/{x}/execute": _LEASE,
    "/v1/discoveries/{x}/execute": _LEASE,
    "/v1/docs/update/execute": _LEASE,
    "/v1/github/community/jobs/{x}/execute": _LEASE,
    "/v1/github/reconciliation-runs/{x}/execute": _LEASE,
    "/v1/infra/droplets": _LEASE,
    "/v1/infra/platform/operations/execute": _LEASE,
    "/v1/infra/resources/{x}/terminate": _LEASE,
    "/v1/investigations/{x}/run": _LEASE,
    "/v1/knowledge/repositories/index/execute": _LEASE,
    "/v1/meetings/step": _LEASE,
    "/v1/openai/plans": _LEASE,
    "/v1/orchestration/next-move/execute": _LEASE,
    "/v1/resources/sync": _LEASE,
    "/v1/security/scans/{x}/run": _LEASE,
    "/v1/self-fixes/{x}/build": _LEASE,
    "/v1/skills/{x}/invoke": _LEASE,
    "/v1/software-factory/deployments": _LEASE,
    "/v1/software-factory/merges": _LEASE,
    "/v1/software-factory/proposals": _LEASE,
    "/v1/webhook-deliveries/{x}/deliver": _LEASE,
    "/v1/work-orchestration/tracker-sync/execute": _LEASE,
    "/v1/delivery/health/sweep": "tick:services/deployment_health.py::sweep",
    "/v1/monitoring/tick": "tick:services/monitor_scheduler.py::tick",
    "/v1/knowledge/ingest/tick": "tick:services/knowledge/ingest.py::tick",
    "/v1/delivery-workflows/schedules/tick": "tick:services/workflow_schedules.py::run_due",
    "/v1/delivery-workflows/run-capabilities":
        "tick:services/workflow_capability_runner.py::projects_with_work",
    "/v1/agent/wakeups/tick": "tick:services/agent_wakeups.py::run_due",
    "/v1/agent-processes/tick-all": "tick:services/agent_process.py::tick_due",
    "/v1/focused-agents/tick-all": "tick:services/focused_agents.py::tick_due",
    "/v1/factory/flow/tick": "tick:services/factory_flow.py::tick",
    "/v1/process-manager/tick": "tick:services/process_manager.py::tick",
    "/v1/metatron/watch": _HOME + " (observe-only)",
    "/v1/setup/checks": "re-checks the install's connections; starts no project work",
    "/v1/setup/subscriptions/keep-alive": "pings and refreshes an existing subscription "
                                           "login's credentials; starts no project work",
    "/v1/shepherd/watch": _RECOVERY,
    "/v1/shepherd/autopilot": _RECOVERY,
}


def _functions(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text())
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _source(key: str, helper: str) -> str:
    file = ENGINE / key.split("::")[0]
    text = file.read_text()
    node = _functions(file).get(helper)
    assert node is not None, f"{key}: no function {helper!r} in {file.relative_to(ROOT)}"
    return ast.get_source_segment(text, node) or ""


def _discovered() -> set[str]:
    found = set()
    for path in sorted(ENGINE.rglob("*.py")):
        rel = path.relative_to(ENGINE).as_posix()
        for name in _functions(path):
            if SCHEDULER_NAME.match(name):
                found.add(f"{rel}::{name}")
    return found


def test_every_scheduler_entry_point_is_decided():
    found = _discovered()
    assert len(found) > 30, "the scheduler-name pattern no longer finds the engine's schedulers"
    undecided = sorted(found - set(GUARDED) - set(EXEMPT))
    assert not undecided, (
        "new scheduler entry points that say nothing about archived projects: "
        f"{undecided}. Skip an archived project (services/projects_archive.py: is_archived, "
        "drop_archived, live_ids, or live_sql() in the query that picks the work) and add it to "
        "GUARDED, or add it to EXEMPT with the reason it starts no work for a project.")
    stale = sorted((set(GUARDED) | set(EXEMPT)) - found
                   - {"services/knowledge/ingest.py::ingest_queued",
                      "services/knowledge/ingest.py::reconcile_projections",
                      "services/workflow_capability_runner.py::projects_with_work"})
    assert not stale, f"entries for functions that no longer exist: {stale}"


def test_every_guarded_scheduler_and_new_work_door_checks_the_archive():
    missing = [f"{key} (via {helper})" for key, helper in {**GUARDED, **NEW_WORK_DOORS}.items()
               if "projects_archive" not in _source(key, helper)]
    assert not missing, f"these no longer skip or refuse an archived project: {missing}"


def _worker_routes() -> set[str]:
    source = WORKER_CLIENT.read_text()
    return {re.sub(r"\{[^}]*\}", "{x}", m) for m in re.findall(r'"(/v1/[^"]*)"', source)}


def test_every_route_the_worker_calls_is_decided():
    calls = _worker_routes()
    assert len(calls) > 40, "the worker client pattern no longer matches control_plane.py"
    undecided = sorted(calls - set(WORKER_ROUTES))
    assert not undecided, (
        f"the worker calls routes that say nothing about archived projects: {undecided}. "
        "Classify each in WORKER_ROUTES: a lease route (it runs a leased job), a tick route "
        "naming its guarded scheduler, or exempt with the reason.")
    assert not sorted(set(WORKER_ROUTES) - calls), "WORKER_ROUTES names routes the worker no longer calls"
    for route, said in WORKER_ROUTES.items():
        if said.startswith("tick:"):
            key = said[len("tick:"):]
            assert key in GUARDED, f"{route} names {key}, which is not a guarded scheduler"
