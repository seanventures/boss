"""THE RECOVERY AGENT: a failed workflow step is recovered by the step's own agent, not thrown at
the operator.

THE CASE (laptop, HelpDesk run wfr_fb4fb5ded8c84a4dab12, 2026-10-16). The deploy step failed four
times for four different reasons, and every one went to the operator's Retry button — about a
hundred presses in one day:
  1. `npm: command not found` on a Docker host (the app's manifest) — a repair build fixed it;
  2. the app exited on `getaddrinfo EAI_AGAIN res-db` — BOSS's own network wiring, classified
     as the app's, so a second repair build "fixed" a working app;
  3. `docker compose up` refused `Bind for 0.0.0.0:8000 failed: port is already allocated` —
     an earlier compose project of THE SAME APP still held the port;
  4. the fixed cap of two repairs was spent, and the step failed to a person.
Each needed a person only because nothing in BOSS looked at the evidence and decided. The
operator's mandate: BOSS's agents do this work — the model is the brain, BOSS is the harness —
and a run should keep going until it succeeds or there is something only a person can decide.

THE DESIGN.
-----------
When a step fails (deploy and build today; `KINDS` is where preview/smoke plug in), its agent
runs ONE TURN OF A RECOVERY LOOP per failure:

  1. EVIDENCE, read-only, BOSS's own diagnosis (never the operator's host:exec grant): the
     failure (deployment record, steps, redacted log tail, manifest, host facts — or the build's
     status, error, events), the target host's containers as Docker labels them (this app's
     own compose projects, what holds a port, the last lines of this app's exited containers —
     through the deployment's own HostAccess with write=False), every earlier decision and its
     outcome, and BOSS's build.
  2. ONE DECISION from a fixed set (`ACTIONS`), asked of the MODEL through BOSS's router
     (model-agnostic: `model_routing.project_chat_adapter`, tier Balanced, every turn governed
     and metered), as JSON with its reasoning and evidence citations:
       retry            transient (a network blip, a lock, an image pull) — again after a backoff;
       fix_app          the app's own fault — a repair build of the build's lineage with a precise
                        note (deploy_repair / code_task_service.retry_code_task), then redeploy;
       clean_and_retry  a resource held by THIS APP's own earlier release on the target — its
                        compose project taken down (no volumes) as a step of the next governed
                        deployment, then redeploy;
       wait_for_boss    BOSS's own bug — recorded for BOSS's developers (boss_recommendations)
                        and the step parked; it resumes by itself when BOSS's build changes;
       ask_person       only what a person alone can decide (a secret only they have, a host they
                        must change) — ONE question, and what BOSS does after.
     The answer is VALIDATED before anything happens: an action the step kind does not support,
     a project that is not this app's own, a fix with no note, a question that is missing, or
     evidence cited that appears nowhere in what the agent was shown is refused and asked once
     more; then BOSS's coded triage (`coded_decision`) answers instead. With no model available
     (none connected, the route refused, an error) the coded triage answers alone — so the loop
     never depends on a model being up.
  3. EXECUTION through existing governed paths only (the runner's executors): a repair build
     under the build step's own authority, a deployment planned under its own approval rule
     (the clean is a step of that deployment, approved with it and guarded on the host), a step
     queued again by the runner, a failed step for a person. Then the loop observes the next
     outcome, and decides again if it fails.

BUDGET, NOT A COUNT. A window per step: a cost ceiling (`recovery_budget_cents`, default 500 =
$5: the recovery model's turns plus every repair build it started) and a wall-time ceiling
(`recovery_max_minutes`, default 120). Spent → ask_person, with one question. PER CAUSE: the
same cause signature twice in a row with the same action is refused — the agent must choose a
different action or ask a person. A person's Retry, or a BOSS update, opens a fresh window.
`deploy_fix_attempts` stays as a hard ceiling on repair builds per deploy step.

SAID, EVERY TIME. Each decision is on the step's output (`recovery.decisions`: what it saw, what
it chose and why, who answered and what it cost), as `note` ("Harbor: port 8000 held by this
app's earlier release (helpdesk-ai) → cleaning it up and redeploying"), and as the event
`recovery.decided`, so the run page and the step's Ask read it.

THE OPERATOR STOPS IT AT ANY TIME: Stop on the run cancels it, and nothing here moves a step of a
run that is not running or waiting.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
from datetime import timedelta
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, now_utc

log = logging.getLogger(__name__)

RETRY = "retry"
FIX_APP = "fix_app"
CLEAN_AND_RETRY = "clean_and_retry"
WAIT_FOR_BOSS = "wait_for_boss"
ASK_PERSON = "ask_person"
ACTIONS = (RETRY, FIX_APP, CLEAN_AND_RETRY, WAIT_FOR_BOSS, ASK_PERSON)

#: Which actions each step kind can carry out. A kind not listed is not recovered (yet): its
#: failures go to a person as before. Preview/smoke plug in here with their executors.
KINDS: dict[str, tuple[str, ...]] = {
    "deploy": ACTIONS,
    # The deploy step's repair build itself failed: build it again, fix again, BOSS, a person.
    "deploy_repair": (RETRY, FIX_APP, WAIT_FOR_BOSS, ASK_PERSON),
    "build": (RETRY, FIX_APP, WAIT_FOR_BOSS, ASK_PERSON),
}

#: The step-output keys this module owns.
STATE_KEY = "recovery"
PENDING_KEY = "recovery_pending"
#: The await kind of a step parked by the recovery loop, and what it waits for.
AWAIT_KIND = "recovery"
THEN_RUN_AGAIN = "run_again"
THEN_BOSS_UPDATE = "boss_update"

DECIDED_EVENT = "recovery.decided"
RESUMED_EVENT = "recovery.resumed"

#: Settings (project_settings.KNOBS).
SWITCH_SETTING = "step_recovery"
BUDGET_SETTING = "recovery_budget_cents"
MINUTES_SETTING = "recovery_max_minutes"
DEFAULT_BUDGET_CENTS = 500
DEFAULT_MAX_MINUTES = 120

#: The router's purpose: an agent's decision, at the Balanced tier — a need, never a model.
ROUTE_KIND = "agent"
ROUTE_STEP = "step_recovery"
ROUTE_TIER = "balanced"
SKILL_ID = "model.step_recovery"
ACTOR = "boss recovery agent"
MAX_TURNS = 4
MAX_PACK_CHARS = 14_000
MAX_TOOL_RESULT_CHARS = 8_000
MAX_DECISIONS_KEPT = 30
DEFAULT_BACKOFF = 60
MAX_BACKOFF = 1800

_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_UNWIRED = re.compile(r"(?:getaddrinfo|could not translate host name)\W+(?:\w+\W+)?\"?"
                      r"(res-[a-z0-9-]+)", re.I)
_TRANSIENT = re.compile(
    r"tls handshake timeout|i/o timeout|connection reset by peer|temporary failure in name "
    r"resolution|could not get lock|unable to acquire the dpkg|toomanyrequests|"
    r"rate limit|503 service unavailable|502 bad gateway|context deadline exceeded|"
    r"error pulling image|net/http: request canceled|resource temporarily unavailable", re.I)
#: BOSS said so itself: its own explanation names BOSS as the one to change.
_BOSS_SAYS = re.compile(r"update boss|boss's (?:own |network )?(?:wiring|plumbing|bug)|"
                        r"this is boss's|a bug in boss", re.I)
#: Build failures that are BOSS's own (2026-10-16): the planner reading a shared repository
#: root, a retry of an empty tree, a start check given no environment, start output unlogged.
_BOSS_BUILD = re.compile(
    r"(?:repository|repo) root|shared root|empty (?:tree|workspace|checkout)|"
    r"start check (?:had|was given|gave) no env|no environment (?:was )?(?:given|passed) to the "
    r"start|start output (?:was )?not (?:logged|captured)", re.I)
_SECRET_SCAN = re.compile(r"secret (?:scan|literal)|no_committed_secrets|committed secret", re.I)
_PERSON_SECRET = re.compile(r"secret[s]? (?:this target does not have|only you|is required)|"
                            r"needs? (?:a|the) (?:key|token|password) (?:only|from) ", re.I)


# ── BOSS's build ───────────────────────────────────────────────────────────────────────────────

_BUILD_ID: Optional[str] = None


def boss_build() -> str:
    """What BOSS this is: its version and a fingerprint of its engine's source, so an update
    from main without a version bump is still an update. Computed once per process."""
    global _BUILD_ID
    if _BUILD_ID is None:
        from pathlib import Path

        from .. import __version__

        digest = hashlib.sha256()
        root = Path(__file__).resolve().parents[1]
        try:
            for path in sorted(root.rglob("*.py")):
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
        except Exception:  # noqa: BLE001 — the version alone is still an answer
            pass
        _BUILD_ID = f"{__version__}+{digest.hexdigest()[:12]}"
    return _BUILD_ID


# ── Settings ──────────────────────────────────────────────────────────────────────────────────

def _knob(connection: Connection, settings: Any, project_id: str, key: str,
          default: int) -> int:
    from . import project_settings

    try:
        with connection.begin_nested():
            value = project_settings.effective(connection, project_id, settings, key)
    except Exception:  # noqa: BLE001 — the default is a complete answer
        value = getattr(settings, key, None)
    try:
        return int(default if value in (None, "") else value)
    except (TypeError, ValueError):
        return default


def enabled(connection: Connection, settings: Any, project_id: str) -> bool:
    return _knob(connection, settings, project_id, SWITCH_SETTING, 1) == 1


def limits(connection: Connection, settings: Any, project_id: str) -> dict[str, int]:
    cents = max(0, _knob(connection, settings, project_id, BUDGET_SETTING,
                         DEFAULT_BUDGET_CENTS))
    minutes = max(1, _knob(connection, settings, project_id, MINUTES_SETTING,
                           DEFAULT_MAX_MINUTES))
    return {"budget_usd_micros": cents * 10_000, "max_seconds": minutes * 60}


# ── The state on the step ─────────────────────────────────────────────────────────────────────

def state_of(output: Any) -> dict[str, Any]:
    state = output.get(STATE_KEY) if isinstance(output, Mapping) else None
    if not isinstance(state, dict):
        return {}
    return json.loads(json.dumps(state, default=str))


def open_window(state: dict[str, Any], *, why: str = "") -> dict[str, Any]:
    """A fresh budget window (first failure, a person's Retry, a BOSS update): the history is
    kept, the spend and the clock start again, and the per-cause rule starts again."""
    now = now_utc().isoformat()
    windows = int(state.get("windows") or 0) + 1
    decisions = list(state.get("decisions") or [])
    if decisions and why:
        decisions.append({"n": len(decisions) + 1, "at": now, "action": "window",
                          "why": why})
    return {**state, "windows": windows, "started_at": now, "spent_usd_micros": 0,
            "build_ids": [], "decisions": decisions[-MAX_DECISIONS_KEPT:]}


def last_decision(state: Mapping[str, Any]) -> dict[str, Any]:
    for item in reversed(list(state.get("decisions") or [])):
        if isinstance(item, dict) and item.get("action") in ACTIONS:
            return item
        if isinstance(item, dict) and item.get("action") == "window":
            return {}
    return {}


def spent(connection: Connection, state: Mapping[str, Any]) -> int:
    """What this window has cost: the recovery model's turns plus the repair builds it started
    (each build's own metered usage)."""
    total = int(state.get("spent_usd_micros") or 0)
    builds = [str(b) for b in state.get("build_ids") or [] if b]
    if builds:
        try:
            with connection.begin_nested():
                total += int(connection.execute(
                    text("SELECT COALESCE(SUM(COALESCE((usage->>'cost_usd_micros')::bigint, 0)),"
                         " 0) FROM code_task_runs WHERE id = ANY(:ids)"),
                    {"ids": builds}).scalar() or 0)
        except Exception:  # noqa: BLE001 — a build's cost unread is not a reason to stop
            pass
    return total


def budget_left(connection: Connection, state: Mapping[str, Any],
                caps: Mapping[str, int]) -> tuple[bool, str]:
    """(may go on, why not)."""
    used = spent(connection, state)
    if used >= int(caps["budget_usd_micros"]):
        return False, (f"BOSS spent its recovery budget for this step ({_money(used)} of "
                       f"{_money(int(caps['budget_usd_micros']))})")
    started = str(state.get("started_at") or "")
    if started:
        from datetime import datetime

        try:
            age = (now_utc() - datetime.fromisoformat(started)).total_seconds()
        except ValueError:
            age = 0
        if age >= int(caps["max_seconds"]):
            return False, (f"BOSS has been recovering this step for {int(age // 60)} minutes "
                           f"(the most it does by itself is {int(caps['max_seconds']) // 60})")
    return True, ""


def _money(micros: int) -> str:
    return f"${micros / 1_000_000:.2f}"


# ── The cause ─────────────────────────────────────────────────────────────────────────────────

def _failure_text(failure: Mapping[str, Any]) -> str:
    return "\n".join(str(failure.get(k) or "") for k in
                     ("headline", "action", "error", "why", "log_tail", "step"))


def port_held(failure: Mapping[str, Any]) -> Optional[int]:
    from .stack_deploy import PORT_HELD

    found = PORT_HELD.search(_failure_text(failure))
    if not found:
        return None
    return int(next(g for g in found.groups() if g))


def cause_signature(kind: str, failure: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """A stable name for WHY it failed — the same cause gets the same signature across attempts
    and runs ("deploy:port_held:8000", "deploy:missing_tool:npm", "build:secret_scan"), and a
    pattern dict the coded triage and the agent's evidence share."""
    from .deploy_repair import _missing_tool

    family = "deploy" if kind.startswith("deploy") else kind
    said = _failure_text(failure)
    port = port_held(failure)
    if port:
        return f"{family}:port_held:{port}", {"pattern": "port_held", "port": port}
    unwired = _UNWIRED.search(said)
    if unwired:
        return (f"{family}:unwired:{unwired.group(1).lower()}",
                {"pattern": "unwired", "service": unwired.group(1).lower()})
    tool = _missing_tool(failure) if family == "deploy" else ""
    if tool:
        return f"{family}:missing_tool:{tool}", {"pattern": "missing_tool", "tool": tool}
    if _SECRET_SCAN.search(said):
        return f"{family}:secret_scan", {"pattern": "secret_scan"}
    if _BOSS_BUILD.search(said) or _BOSS_SAYS.search(said):
        stem = re.sub(r"\W+", "_", (_BOSS_BUILD.search(said) or _BOSS_SAYS.search(said))
                      .group(0).lower()).strip("_")
        return f"{family}:boss:{stem}", {"pattern": "boss_side"}
    if _TRANSIENT.search(said):
        stem = re.sub(r"\W+", "_", _TRANSIENT.search(said).group(0).lower()).strip("_")
        return f"{family}:transient:{stem}", {"pattern": "transient"}
    headline = str(failure.get("headline") or failure.get("error") or failure.get("why")
                   or "failed")
    norm = re.sub(r"\b[a-z]{2,6}_[0-9a-f]{6,}\b|\b[0-9a-f]{12,}\b|\d+", "#", headline.lower())
    digest = hashlib.sha1(norm.encode()).hexdigest()[:10]
    return (f"{family}:{failure.get('step') or failure.get('phase') or 'step'}:{digest}",
            {"pattern": "other"})


# ── Evidence ──────────────────────────────────────────────────────────────────────────────────

def _scrub(value: Any, limit: int = 3000) -> str:
    from .deploy_repair import _scrub as scrub

    return scrub(str(value or ""))[-limit:]


def host_state(connection: Connection, settings: Any, *, host_id: Optional[str],
               target_dir: str, keep_project: Optional[str], port: Optional[int]
               ) -> dict[str, Any]:
    """The target host's containers as Docker labels them — read-only, through the deployment's
    own HostAccess (write=False), never the operator's host:exec grant.

    `own_projects`: compose projects EVERY container of which was started from under this
    target's directory (`com.docker.compose.project.working_dir`), minus the project the app is
    about to run in (`keep_project`: the resources' shared one, with their data). Only those may
    ever be cleaned. `port_holders`: what publishes `port`, and whether it is this app's.
    Another app's containers are reported only as a port holder, by name and project."""
    from . import stack_deploy
    from .deployment_fleet import get_host

    if not host_id:
        return {"read": False, "why": "no host on record"}
    host = get_host(connection, host_id)
    if not host:
        return {"read": False, "why": "the host is gone"}
    marker = str(target_dir or "").rstrip("/")
    probe = ("echo BOSS_RECOVERY_PROBE; docker ps -a --format '{{.Names}}\t"
             "{{.Label \"com.docker.compose.project\"}}\t"
             "{{.Label \"com.docker.compose.project.working_dir\"}}\t{{.State}}\t{{.Ports}}' "
             "2>&1 | head -300")
    try:
        access = stack_deploy.host_access_for(connection, settings, host)
        listed = access.run(probe, timeout=60, write=False)
    except Exception as error:  # noqa: BLE001 — diagnosis only; the decision goes on without it
        return {"read": False, "why": _scrub(f"{type(error).__name__}: {error}", 300)}
    containers = []
    for line in listed.output.splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or line.startswith("BOSS_RECOVERY_PROBE"):
            continue
        name, project, workdir, state = (p.strip() for p in parts[:4])
        ports = parts[4].strip() if len(parts) > 4 else ""
        own = bool(marker) and (workdir == marker or workdir.startswith(marker + "/"))
        containers.append({"name": name, "project": project, "state": state, "ports": ports,
                           "own": own})
    projects: dict[str, list[dict[str, Any]]] = {}
    for c in containers:
        if c["project"]:
            projects.setdefault(c["project"], []).append(c)
    own_projects = sorted(
        p for p, members in projects.items()
        if all(m["own"] for m in members) and _PROJECT.match(p) and p != keep_project)
    holders = []
    if port:
        for c in containers:
            if re.search(rf"(?:^|[\s,:])(?:[\d.]+|\[::\]|::):{port}->", c["ports"]):
                holders.append({"name": c["name"], "project": c["project"], "own": c["own"],
                                "state": c["state"],
                                "own_project": c["project"] in own_projects})
    exited = [c["name"] for c in containers
              if c["own"] and c["state"] in ("exited", "dead", "restarting")][:3]
    logs = ""
    if exited:
        names = " ".join(shlex.quote(n) for n in exited)
        try:
            tail = access.run(f"for c in {names}; do echo \"--- $c (last lines) ---\"; "
                              "docker logs --tail 30 \"$c\" 2>&1; done", timeout=60,
                              write=False)
            logs = _scrub(tail.output, 3000)
        except Exception:  # noqa: BLE001
            logs = ""
    return {"read": True, "host": host.get("name"), "address": host.get("address"),
            "target_dir": marker, "keep_project": keep_project,
            "own_containers": [c for c in containers if c["own"]][:30],
            "own_projects": own_projects, "port": port, "port_holders": holders,
            "exited_logs": logs, "containers_seen": len(containers)}


def deploy_evidence(connection: Connection, settings: Any, failure: Mapping[str, Any],
                    *, probe: bool = True) -> dict[str, Any]:
    """The deploy failure, as the agent decides on it, with the host's state."""
    target_dir, keep = "", None
    deployment_id = str(failure.get("deployment_id") or "")
    if deployment_id:
        row = connection.execute(
            text("SELECT deploy_dir, manifest FROM stack_deployments WHERE id = :i"),
            {"i": deployment_id}).mappings().first()
        if row:
            target_dir = str(row["deploy_dir"] or "")
            summary = row["manifest"]
            summary = json.loads(summary) if isinstance(summary, str) else (summary or {})
            keep = (summary or {}).get("shared_compose_project")
    if not target_dir and failure.get("manifest_text"):
        try:
            from .app_deploy_manifest import load_manifest

            target_dir = load_manifest(str(failure["manifest_text"])).dir
        except Exception:  # noqa: BLE001
            target_dir = ""
    port = port_held(failure)
    host = host_state(connection, settings, host_id=str(failure.get("host_id") or "") or None,
                      target_dir=target_dir, keep_project=keep, port=port) if probe else {}
    return {
        "failure": {k: (_scrub(v) if k == "log_tail" else v) for k, v in failure.items()
                    if k not in ("manifest_text", "steps")},
        "deployment_steps": failure.get("steps") or [],
        "manifest": str(failure.get("manifest_text") or "")[:3000],
        "host_state": host,
        "own_projects": list(host.get("own_projects") or []),
    }


def build_evidence(connection: Connection, build_run_id: str, why: str) -> dict[str, Any]:
    """A failed build, as the agent decides on it: its status, phase, error and why it ended,
    the gate it stopped at, and its last events."""
    row = connection.execute(
        text("SELECT id, status, phase, error, title, finalize_ctx->>'reason_code' AS reason, "
             "pull_request_number, spec->>'repo' AS repo FROM code_task_runs WHERE id = :i"),
        {"i": build_run_id}).mappings().first()
    events: list[dict[str, Any]] = []
    try:
        with connection.begin_nested():
            events = [{"event": r["event_type"],
                       "data": _scrub(json.dumps(r["data"], default=str), 400)}
                      for r in connection.execute(
                          text("SELECT event_type, data FROM events WHERE entity_id = :i "
                               "ORDER BY created_at DESC LIMIT 15"),
                          {"i": build_run_id}).mappings().all()]
    except Exception:  # noqa: BLE001
        events = []
    build = dict(row) if row else {"id": build_run_id}
    build["error"] = _scrub(build.get("error"), 2000)
    return {"failure": {"build_run_id": build_run_id, "why": _scrub(why, 2000),
                        "error": build.get("error"), "status": build.get("status"),
                        "phase": build.get("phase"), "reason_code": build.get("reason")},
            "build": build, "events": events, "own_projects": []}


# ── The model ─────────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are {agent}, the agent responsible for this workflow step. It failed. \
Your job is to get the run going again by yourself, choosing ONE next action from a fixed set. \
You are the brain; BOSS executes what you choose through its governed paths.

Actions (only those listed under "allowed" may be chosen):
- "retry": the failure is transient (a network blip, a lock, an image pull, a rate limit). \
Give "backoff_seconds".
- "fix_app": the app's own code or manifest is at fault. A repair build of the app runs with \
your "note" — say precisely what to change and why, citing the log — then BOSS deploys again.
- "clean_and_retry": a resource (a port, a container name) is held by THIS app's own earlier \
release on the target. Name the projects to take down in "projects"; ONLY names listed in \
"own_projects" are allowed (never another app's, never the resources' shared project; volumes \
are kept). Then BOSS deploys again.
- "wait_for_boss": the failure is BOSS's own bug (its wiring, its planner, a capability it \
lacks, its image's tools) and no change to the app can fix it. Fill "boss_fix" so BOSS's \
developers can fix it: title, root_cause, proposed_change, files. The step resumes by itself \
when BOSS is updated.
- "ask_person": only for what a person alone can decide or do (a secret only they have, an \
approval, a host they must change). ONE specific "question", and "after": what BOSS will do \
once they answer.

Rules: read the evidence (and use your read tools if you need more). Never choose the same \
action for the same cause as the previous decision — choose a different one or ask a person. \
Cite concrete evidence you were shown (quote a log line, an id, a tool result).

Answer with ONLY a JSON object:
{{"action": "...", "cause": "<one line: why it failed>", "reasoning": "<why this action>", \
"evidence": ["<citation>", ...], "note": "...", "question": "...", "after": "...", \
"backoff_seconds": 60, "projects": ["..."], "boss_fix": {{"title": "...", "root_cause": "...", \
"proposed_change": "...", "files": ["..."]}}}}"""

ANSWER_NOW = "Stop investigating and answer now with ONLY the JSON object."


def routed_adapter(connection: Connection, settings: Any, project_id: str, *, ref: str
                   ) -> tuple[Any, Any]:
    """The project's routed model for a recovery decision, every turn governed."""
    from . import model_routing
    from .model_router.router import RouteRequest

    purpose = RouteRequest(kind=ROUTE_KIND, step=ROUTE_STEP, project_id=project_id, ref=ref,
                           tier=ROUTE_TIER,
                           text="Decide how to recover a failed workflow step from its "
                                "evidence: retry, fix the app, clean its old release, wait for "
                                "a BOSS fix, or ask a person.")
    raw = model_routing.project_chat_adapter(connection, settings, project_id, purpose=purpose)
    return model_routing.governed(raw, connection, settings, project_id, skill_id=SKILL_ID,
                                  ref=ref), raw


def parse_decision(answer: str) -> Optional[dict[str, Any]]:
    from .workflow_precheck_agent import _FENCE

    body = str(answer or "").strip()
    for chunk in [m.group(1) for m in _FENCE.finditer(body)] + [body]:
        i, j = chunk.find("{"), chunk.rfind("}")
        if i < 0 or j <= i:
            continue
        try:
            value = json.loads(chunk[i:j + 1])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("action"):
            return value
    return None


def validate(raw: Mapping[str, Any], *, allowed: tuple[str, ...], evidence: Mapping[str, Any],
             corpus: str, last: Mapping[str, Any], signature: str
             ) -> tuple[Optional[dict[str, Any]], str]:
    """(the decision, "") or (None, why it is refused)."""
    from .workflow_precheck_agent import _citation, grounded

    action = str(raw.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return None, f"'{action}' is not one of {', '.join(ACTIONS)}"
    if action not in allowed:
        return None, f"'{action}' is not allowed for this step (allowed: {', '.join(allowed)})"
    if last.get("signature") == signature and last.get("action") == action:
        return None, (f"the previous decision was already '{action}' for this same cause "
                      f"({signature}); choose a different action or ask a person")
    cited = [c for c in (_citation(e) for e in (raw.get("evidence") or [])) if len(c) >= 4] \
        if isinstance(raw.get("evidence"), list) else \
        ([_citation(raw["evidence"])] if raw.get("evidence") else [])
    if action != ASK_PERSON and not any(grounded(c, corpus) for c in cited):
        return None, "cite evidence you were shown (quote a log line, an id or a tool result)"
    out: dict[str, Any] = {
        "action": action, "cause": str(raw.get("cause") or "")[:300],
        "reasoning": str(raw.get("reasoning") or "")[:1200], "evidence": cited[:6]}
    if action == RETRY:
        try:
            backoff = int(raw.get("backoff_seconds") or DEFAULT_BACKOFF)
        except (TypeError, ValueError):
            backoff = DEFAULT_BACKOFF
        out["backoff_seconds"] = max(5, min(MAX_BACKOFF, backoff))
    if action == FIX_APP:
        note = str(raw.get("note") or "").strip()
        if len(note) < 12:
            return None, "a fix_app needs a precise note: what to change in the app and why"
        out["note"] = note[:3000]
    if action == CLEAN_AND_RETRY:
        own = set(evidence.get("own_projects") or [])
        projects = [str(p).strip() for p in (raw.get("projects") or []) if str(p).strip()]
        if not projects:
            return None, "clean_and_retry needs the projects to take down"
        foreign = [p for p in projects if p not in own]
        if foreign:
            return None, (f"{', '.join(foreign)} is not this app's own earlier release on the "
                          f"target (its own: {', '.join(sorted(own)) or 'none'}); BOSS never "
                          "touches another app's resources")
        out["projects"] = projects
    if action == ASK_PERSON:
        question = str(raw.get("question") or "").strip()
        if len(question) < 8:
            return None, "ask_person needs ONE specific question"
        out["question"] = question[:600]
        out["after"] = str(raw.get("after") or "").strip()[:300]
    if action == WAIT_FOR_BOSS:
        fix = raw.get("boss_fix") if isinstance(raw.get("boss_fix"), Mapping) else {}
        title = str(fix.get("title") or raw.get("cause") or "").strip()
        if not title:
            return None, "wait_for_boss needs boss_fix.title: what BOSS must change"
        out["boss_fix"] = {"title": title[:300],
                           "root_cause": str(fix.get("root_cause") or raw.get("cause") or "")[:1500],
                           "proposed_change": str(fix.get("proposed_change") or "")[:3000],
                           "files": [str(f)[:200] for f in (fix.get("files") or [])][:12]}
    return out, ""


def ask_model(adapter: Any, *, raw_adapter: Any = None, agent: str, evidence: Mapping[str, Any],
              allowed: tuple[str, ...], last: Mapping[str, Any], signature: str,
              tools: Optional[list[dict[str, Any]]] = None,
              run_tool: Optional[Callable[[str, dict], dict]] = None,
              max_turns: int = MAX_TURNS, max_usd_micros: int = 300_000) -> dict[str, Any]:
    """THE MODEL'S DECISION. Pure over its seams (the adapter, the tool runner), so tests drive
    it with a stub model. Never raises. Returns {decision?, refused?, cost, turns, by}."""
    from .redaction import redact_secrets
    from .workflow_precheck_agent import _turn_cost, _who_answered, deferred_wait

    who = _who_answered(raw_adapter if raw_adapter is not None else adapter)
    pack = redact_secrets(json.dumps({**evidence, "allowed": list(allowed),
                                      "cause_signature": signature,
                                      "previous_decision": dict(last) or None},
                                     default=str, indent=1))
    if len(pack) > MAX_PACK_CHARS:
        pack = pack[:MAX_PACK_CHARS] + "\n… (truncated; read more with the tools)"
    corpus = [pack]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(agent=agent)},
        {"role": "user", "content": "THE EVIDENCE (JSON):\n" + pack}]
    spent_micros, turns, asked_again, refused = 0, 0, 0, ""
    can_tool = tools is not None and run_tool is not None
    model_seen = ""
    while turns < max_turns and spent_micros < max_usd_micros:
        final = turns == max_turns - 1
        if final and messages[-1].get("role") == "tool":
            messages.append({"role": "user", "content": ANSWER_NOW})
        try:
            message, usage = adapter.create_chat_completion(
                messages, tools=(tools if (can_tool and not final) else None))
        except Exception as exc:  # noqa: BLE001 — a model failing is a result, not a crash
            # Never waited for here (this runs inside the runner's tick): a turn the per-minute
            # budget defers is answered by BOSS's coded triage instead, and said so.
            deferred = deferred_wait(exc) is not None
            return {"decision": None, "error": ("the model budget deferred the turn: "
                                                if deferred else "")
                    + f"{type(exc).__name__}: {exc}"[:300],
                    "cost_usd_micros": spent_micros, "turns": turns, "by": who}
        turns += 1
        spent_micros += _turn_cost(usage)
        if isinstance(usage, Mapping) and usage.get("model"):
            model_seen = str(usage["model"])
        message = message if isinstance(message, Mapping) else {"content": str(message or "")}
        messages.append(message.get("raw") or {"role": "assistant",
                                               "content": message.get("content") or ""})
        calls = message.get("tool_calls") or []
        if calls and can_tool and not final:
            for call in calls:
                try:
                    args = json.loads(call.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                try:
                    result = run_tool(str(call.get("name") or ""),
                                      args if isinstance(args, dict) else {})
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                body = redact_secrets(json.dumps(result, default=str))[:MAX_TOOL_RESULT_CHARS]
                corpus.append(body)
                messages.append({"role": "tool", "tool_call_id": call.get("id"),
                                 "content": body})
            continue
        raw = parse_decision(str(message.get("content") or ""))
        if raw is None:
            refused = "not the JSON object asked for"
        else:
            decision, refused = validate(raw, allowed=allowed, evidence=evidence,
                                         corpus="\n".join(corpus), last=last,
                                         signature=signature)
            if decision is not None:
                by = {**who, "model": model_seen or who.get("model") or ""}
                return {"decision": decision, "cost_usd_micros": spent_micros, "turns": turns,
                        "by": by}
        if asked_again >= 1:
            break
        asked_again += 1
        messages.append({"role": "user", "content": f"Refused: {refused}. Answer again with "
                                                    "ONLY the JSON object."})
    return {"decision": None, "refused": refused or "no answer within the turn cap",
            "cost_usd_micros": spent_micros, "turns": turns, "by": who}


# ── BOSS's coded triage (no model, or the model's answer refused) ─────────────────────────────

def coded_decision(kind: str, evidence: Mapping[str, Any], *, signature: str,
                   pattern: Mapping[str, Any], last: Mapping[str, Any],
                   allowed: tuple[str, ...]) -> dict[str, Any]:
    """BOSS's own triage of the classes it knows, then the per-cause rule. Always answers."""
    failure = evidence.get("failure") or {}
    said = _failure_text(failure)
    host = evidence.get("host_state") or {}
    kind_of = pattern.get("pattern")
    choice: dict[str, Any]
    if _PERSON_SECRET.search(said):
        # A secret only a person has: nothing BOSS changes in the app or itself supplies it.
        choice = _ask(failure, host)
    elif kind_of == "port_held":
        port = pattern.get("port")
        holders = host.get("port_holders") or []
        own = sorted({h["project"] for h in holders if h.get("own_project")})
        foreign = [h for h in holders if not h.get("own_project")]
        if own and not foreign:
            choice = {"action": CLEAN_AND_RETRY, "projects": own,
                      "cause": f"port {port} held by this app's earlier release "
                               f"({', '.join(own)})",
                      "reasoning": "Docker labels say every container of that project was "
                                   "started from this target's own directory."}
        else:
            who = ", ".join(f"{h['name']} ({h['project'] or 'no project'})" for h in foreign) \
                or "something BOSS cannot see"
            choice = {"action": ASK_PERSON, "cause": f"port {port} held by {who}",
                      "question": f"Port {port} on {host.get('address') or 'the target'} is "
                                  f"held by {who}, which is not this app's. Free that port, or "
                                  "tell BOSS which port this app should publish instead?",
                      "after": "BOSS deploys again on its own once the port is free."}
    elif kind_of in ("unwired", "boss_side") or str(failure.get("cause") or "") == "boss":
        choice = {"action": WAIT_FOR_BOSS,
                  "cause": str(failure.get("headline") or failure.get("why")
                               or "BOSS's own fault")[:300],
                  "reasoning": "BOSS's own explanation names BOSS, not the app, as what must "
                               "change; a repair build of the app would change nothing.",
                  "boss_fix": {"title": str(failure.get("headline") or failure.get("why")
                                            or signature)[:300],
                               "root_cause": str(failure.get("action") or "")[:1500],
                               "proposed_change": "", "files": []}}
    elif kind_of == "transient":
        choice = {"action": RETRY, "backoff_seconds": DEFAULT_BACKOFF,
                  "cause": "a transient failure", "reasoning": "it clears by itself"}
    elif kind_of in ("missing_tool", "secret_scan") or str(failure.get("cause") or "") == "app":
        choice = {"action": FIX_APP, "cause": str(failure.get("headline") or
                                                  failure.get("why") or signature)[:300],
                  "reasoning": "BOSS placed the failure on the app.",
                  "note": ("Remove the committed secret literal and read the value from the "
                           "environment instead (process.env / os.environ), with a safe "
                           "development default that is not secret-shaped."
                           if kind_of == "secret_scan" else "")}
    elif kind in ("build", "deploy_repair") and not last:
        choice = {"action": RETRY, "backoff_seconds": 0, "cause": "the build failed",
                  "reasoning": "one fresh attempt of the same lineage before asking anyone"}
    else:
        choice = _ask(failure, host)
    choice["evidence"] = [str(failure.get("headline") or failure.get("why") or "")[:300]]
    if choice["action"] not in allowed or (last.get("signature") == signature
                                           and last.get("action") == choice["action"]):
        repeated = last.get("action") == choice["action"]
        choice = {**_ask(failure, host, repeated=repeated, cause=choice.get("cause")),
                  "evidence": choice["evidence"]}
    return choice


def _ask(failure: Mapping[str, Any], host: Mapping[str, Any], *, repeated: bool = False,
         cause: Any = None) -> dict[str, Any]:
    headline = str(failure.get("headline") or failure.get("why") or failure.get("error")
                   or "the step failed").strip().rstrip(".")
    action = str(failure.get("action") or "").strip()
    lead = ("BOSS already tried that for this same cause. " if repeated else "")
    return {"action": ASK_PERSON, "cause": str(cause or headline)[:300],
            "reasoning": "only a person can change what this needs" if not repeated else
                         "the same cause came back after the same action",
            "question": (f"{lead}{headline}. {action} — can you do that (or tell BOSS what to do "
                         "instead)?").replace(". —", " —")[:600],
            "after": "Press Retry on this step when it is done; the recovery starts again."}


# ── One decision ──────────────────────────────────────────────────────────────────────────────

def agent_of(connection: Connection, project_id: str, step: Mapping[str, Any]) -> dict[str, str]:
    """The step's own agent: its approver role (the deploy's release manager), else its captain,
    by the name the console shows (persona callsign / display name)."""
    role = str(((step.get("approval") or {}) if isinstance(step.get("approval"), Mapping)
                else {}).get("approver_role") or step.get("captain") or "").strip().lower()
    if not role:
        return {"role": "", "name": "BOSS"}
    try:
        with connection.begin_nested():
            row = connection.execute(
                text("SELECT role_key, display_name, spec FROM roles WHERE project_id = :p "
                     "AND role_key = :r"), {"p": project_id, "r": role}).mappings().first()
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        return {"role": role, "name": role.replace("_", " ").title()}
    try:
        from .agent_persona import persona_of

        name = persona_of({"role_key": row["role_key"], "spec": row["spec"]}).get("callsign")
    except Exception:  # noqa: BLE001
        name = None
    return {"role": role, "name": str(name or row["display_name"] or role)}


def _read_tools(connection: Connection, settings: Any, project_id: str
                ) -> tuple[Optional[list[dict[str, Any]]], Optional[Callable[[str, dict], dict]]]:
    """BOSS's fundamentals READS every agent holds (logs, run history, diagnoses, config, code,
    the deployments and hosts) — the pre-check agent's set, read-only. None when unavailable:
    the agent then decides from the evidence it was given."""
    try:
        from ..context import EngineContext
        from .workflow_precheck_agent import read_tools

        schemas, run = read_tools(settings)
        ctx = EngineContext(settings=settings, engine=getattr(connection, "engine", None))
        return schemas, (lambda name, args: run(ctx, project_id, name, args))
    except Exception as error:  # noqa: BLE001
        log.info("recovery agent read tools unavailable: %s", error)
        return None, None


def decide(connection: Connection, settings: Any, *, project_id: str, run_id: str,
           step_id: str, kind: str, evidence: dict[str, Any], state: dict[str, Any],
           agent: Mapping[str, str], adapter: Any = None) -> dict[str, Any]:
    """One decision, recorded on `state`. Never raises."""
    allowed = KINDS[kind]
    signature, pattern = cause_signature(kind, evidence.get("failure") or {})
    evidence = {**evidence, "pattern": pattern}
    last = last_decision(state)
    caps = limits(connection, settings, project_id)
    ok, why_not = budget_left(connection, state, caps)
    result: dict[str, Any] = {}
    if not ok:
        failure = evidence.get("failure") or {}
        decision = {**_ask(failure, evidence.get("host_state") or {}),
                    "reasoning": why_not, "evidence": []}
        decision["question"] = (f"{why_not}, and it still fails: "
                                f"{str(failure.get('headline') or failure.get('why') or '')}"
                                f". Should BOSS keep going (raise '{BUDGET_SETTING}' or "
                                f"'{MINUTES_SETTING}' in Govern), or will you change what it "
                                "needs?")[:600]
        by: dict[str, Any] = {"engine": "coded", "model": "", "source": "budget"}
    else:
        try:
            if adapter is None:
                adapter, raw = routed_adapter(connection, settings, project_id,
                                              ref=f"recovery:{run_id}:{step_id}")
            else:
                raw = adapter
            schemas, run_tool = _read_tools(connection, settings, project_id)
            left = max(1, int(caps["budget_usd_micros"]) - spent(connection, state))
            result = ask_model(adapter, raw_adapter=raw, agent=agent.get("name") or "BOSS",
                               evidence=evidence, allowed=allowed, last=last,
                               signature=signature, tools=schemas, run_tool=run_tool,
                               max_usd_micros=min(left, 300_000))
        except Exception as exc:  # noqa: BLE001 — no model may run this: BOSS's triage answers
            result = {"decision": None, "error": f"{type(exc).__name__}: {exc}"[:300],
                      "cost_usd_micros": 0, "by": {}}
        decision = result.get("decision")
        by = dict(result.get("by") or {})
        if decision is None:
            decision = coded_decision(kind, evidence, signature=signature, pattern=pattern,
                                      last=last, allowed=allowed)
            by = {"engine": "coded", "model": "", "source": "BOSS triage",
                  **({"model_said": result.get("refused") or result.get("error")}
                     if (result.get("refused") or result.get("error")) else {})}
    cost = int(result.get("cost_usd_micros") or 0)
    decisions = list(state.get("decisions") or [])
    entry = {"n": len(decisions) + 1, "at": now_utc().isoformat(), "signature": signature,
             "pattern": pattern.get("pattern"), **decision, "by": by, "cost_usd_micros": cost,
             "agent": agent.get("name")}
    entry["sentence"] = sentence(agent.get("name") or "BOSS", entry)
    state.update({"spent_usd_micros": int(state.get("spent_usd_micros") or 0) + cost,
                  "decisions": (decisions + [entry])[-MAX_DECISIONS_KEPT:],
                  "agent": agent.get("name"), "role": agent.get("role"),
                  "boss_build": boss_build()})
    emit_event(connection, project_id, DECIDED_EVENT, agent.get("role") or ACTOR,
               "workflow_run", run_id,
               {"step_id": step_id, "action": entry["action"], "signature": signature,
                "cause": entry.get("cause"), "reasoning": str(entry.get("reasoning") or "")[:500],
                "by": by, "cost_usd_micros": cost, "sentence": entry["sentence"],
                "decision": entry["n"]})
    return entry


def record_ask(connection: Connection, *, project_id: str, run_id: str, step_id: str,
               state: dict[str, Any], agent: Mapping[str, str], failure: Mapping[str, Any],
               reason: str, signature: str) -> dict[str, Any]:
    """The chosen action could not be carried out (a ceiling, a refused grant): the agent asks
    a person instead, and says why — a decision of its own on the step."""
    decisions = list(state.get("decisions") or [])
    entry = {"n": len(decisions) + 1, "at": now_utc().isoformat(), "signature": signature,
             **_ask(failure, {}), "reasoning": reason[:600], "evidence": [],
             "by": {"engine": "coded", "model": "", "source": "BOSS triage"},
             "cost_usd_micros": 0, "agent": agent.get("name")}
    entry["sentence"] = sentence(agent.get("name") or "BOSS", entry)
    state["decisions"] = (decisions + [entry])[-MAX_DECISIONS_KEPT:]
    emit_event(connection, project_id, DECIDED_EVENT, agent.get("role") or ACTOR,
               "workflow_run", run_id,
               {"step_id": step_id, "action": ASK_PERSON, "signature": signature,
                "reasoning": reason[:500], "sentence": entry["sentence"], "decision": entry["n"]})
    return entry


def ask_error(entry: Mapping[str, Any]) -> str:
    """What the failed step says when the agent asks a person: why BOSS stopped, the question,
    and what happens after."""
    why = str(entry.get("reasoning") or "").strip().rstrip(".")
    return " ".join(p for p in (
        f"BOSS did not fix it by itself: {why}." if why else "",
        f"{entry.get('agent') or 'BOSS'} asks: {entry.get('question') or ''}".strip(),
        str(entry.get("after") or "")) if p).strip()


def resumes_on_update(entry: Mapping[str, Any], failure: Mapping[str, Any]) -> bool:
    """A failure BOSS placed on itself or on the host: worth re-evaluating after a BOSS update
    even when a person was asked (a BOSS fix may make the person's change unnecessary)."""
    return (str(entry.get("pattern") or "") in ("boss_side", "unwired")
            or str(failure.get("cause") or "") in ("host", "boss"))


_DOING = {
    RETRY: "trying again",
    FIX_APP: "fixing the app and deploying again",
    CLEAN_AND_RETRY: "cleaning it up and deploying again",
    WAIT_FOR_BOSS: "recorded for BOSS's developers; resumes by itself after BOSS is updated",
    ASK_PERSON: "asking you",
}


def sentence(agent: str, entry: Mapping[str, Any]) -> str:
    """"Harbor: port 8000 held by this app's earlier release (helpdesk-ai) → cleaning it up and
    deploying again"."""
    action = str(entry.get("action") or "")
    doing = _DOING.get(action, action)
    if action == RETRY and entry.get("backoff_seconds"):
        doing = f"trying again in {int(entry['backoff_seconds'])}s"
    if action == FIX_APP and str(entry.get("pattern") or "") and \
            str(entry.get("signature") or "").startswith("build"):
        doing = "fixing the app in a repair build"
    if action == ASK_PERSON and entry.get("question"):
        doing = f"asking you: {entry['question']}"
    cause = str(entry.get("cause") or "it failed").strip().rstrip(".")
    return f"{agent}: {cause} → {doing}"[:600]


def recommend_boss_fix(connection: Connection, settings: Any, *, project_id: str, run_id: str,
                       step_id: str, entry: Mapping[str, Any], evidence: Mapping[str, Any]
                       ) -> dict[str, Any]:
    """A wait_for_boss decision, written down for BOSS's developers (boss_recommendations)."""
    from . import boss_recommendations

    fix = entry.get("boss_fix") or {}
    failure = evidence.get("failure") or {}
    lines = [str(e) for e in entry.get("evidence") or [] if e]
    tail = str(failure.get("log_tail") or "").strip().splitlines()[-6:]
    return boss_recommendations.record(
        connection, settings, project_id=project_id, signature=str(entry.get("signature")),
        title=str(fix.get("title") or entry.get("cause") or "BOSS-side failure"),
        symptom=str(failure.get("headline") or failure.get("why") or failure.get("error")
                    or ""),
        evidence=lines + tail, root_cause=str(fix.get("root_cause") or ""),
        proposed_change=str(fix.get("proposed_change") or ""),
        files=list(fix.get("files") or []),
        ref={"run_id": run_id, "step_id": step_id,
             "build_id": failure.get("build_run_id"),
             "deployment_id": failure.get("deployment_id")},
        actor=str(entry.get("agent") or ACTOR), run_id=run_id, boss_version=boss_build())


def run_again_handle(entry: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """The await handle of a step the loop will run again (retry, clean_and_retry)."""
    wait = int(entry.get("backoff_seconds") or 0)
    return {"kind": AWAIT_KIND, "then": THEN_RUN_AGAIN, "step_kind": kind,
            "action": entry.get("action"), "decision": entry.get("n"),
            "not_before": (now_utc() + timedelta(seconds=wait)).isoformat(),
            **({"retire_projects": list(entry.get("projects") or [])}
               if entry.get("action") == CLEAN_AND_RETRY else {})}


def boss_update_handle(entry: Mapping[str, Any], *, kind: str,
                       build_run_id: Optional[str] = None) -> dict[str, Any]:
    return {"kind": AWAIT_KIND, "then": THEN_BOSS_UPDATE, "step_kind": kind,
            "decision": entry.get("n"), "boss_build": boss_build(),
            "signature": entry.get("signature"),
            **({"build_run_id": build_run_id} if build_run_id else {})}


def due(handle: Mapping[str, Any]) -> bool:
    """Whether a parked step's wait is over: its backoff passed, or BOSS's build changed."""
    if handle.get("then") == THEN_BOSS_UPDATE:
        return str(handle.get("boss_build") or "") != boss_build()
    from datetime import datetime

    try:
        return now_utc() >= datetime.fromisoformat(str(handle.get("not_before")))
    except (TypeError, ValueError):
        return True


__all__ = ["ACTIONS", "ASK_PERSON", "AWAIT_KIND", "CLEAN_AND_RETRY", "DECIDED_EVENT", "FIX_APP",
           "KINDS", "PENDING_KEY", "RESUMED_EVENT", "RETRY", "STATE_KEY", "WAIT_FOR_BOSS",
           "agent_of", "ask_model", "boss_build", "boss_update_handle", "budget_left",
           "build_evidence", "cause_signature", "coded_decision", "decide", "deploy_evidence",
           "due", "enabled", "host_state", "last_decision", "limits", "open_window",
           "parse_decision", "ask_error", "record_ask", "resumes_on_update",
           "recommend_boss_fix", "routed_adapter", "run_again_handle", "sentence", "spent",
           "state_of", "validate"]
