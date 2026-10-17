"""Fix and redeploy: a workflow's deploy that fails for the APP's reason is repaired by BOSS.

THE CASE (laptop, HelpDesk run wfr_fb4fb5ded8c84a4dab12, 2026-10-16). The build passed and
opened PR #228. Its deploy step planned a stack deployment from helpdesk-ai/boss.deploy.yaml —
`kind: commands`, `install: ["npm install …", "npm start &"]` — onto a host that runs Docker and
has no Node. The deployment ran prepare, upload, environment, switch, then `install 1` printed
`bash: line 1: npm: command not found` (exit 127) and the step failed. The build could not be
retried (it had succeeded) and the deploy pins the build's commit, so the only way to get a fix
in was a new run. The operator's rule: an issue found at the deployment step is not sent back
to a person to fix in code — the deploy agent fixes it, with the context, and the operator can
talk to it about it as usual.

THE DESIGN, FOR THE CLASS.
--------------------------
* WHOSE FIX IT IS is decided where the failure is known (stack_deploy: `cause` on a refused
  plan and on a failed deployment's error). `app`: a program the machine lacks for a
  `kind: commands` manifest (preflight, before anything is written), a manifest that is invalid
  or does not fit, the app's own install/start/health failure. `host`: the machine, its access,
  its disk, Docker's daemon, sudo, BOSS's own plumbing — those stay failures for a person. A
  person's "no" (a rejected deployment) is never in either class and is never repaired.
* THE REPAIR IS A BUILD OF THE SAME LINEAGE (code_task_service.retry_code_task on the build that
  produced the commit), told exactly what failed: the step and command, the redacted log tail,
  what the machine has, the manifest, and what to change. It builds onto the kept work and
  pushes to the same pull request.
* THE DEPLOY STEP WAITS ON IT (the step's await handle becomes that code task, marked
  `then: redeploy`). When the repair completes, the step is queued again with `redeploy` on its
  output and plans a NEW deployment of the pull request's new head — under that deployment's
  own approval rule, never the earlier answer. When the repair fails, the step fails with both
  causes. At most `deploy_fix_attempts` repairs per step (project setting, default 2).
* EVERY MOVE IS SAID on the step's output (`note`, `deploy_fixes`) and as events
  (`deploy.fix_started`, `deploy.redeploying`), so the run page reads "Deploy failed: `npm`
  missing on 2.29.2.202 → fixing the app (build crun_…) → redeploying", and the step's Ask
  (turn_subject) carries the deployment, its failure, the host facts and the fixes.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .stack_deploy import CAUSE_APP, RUNTIME_TOOLS, host_has

log = logging.getLogger(__name__)

#: The await-handle marker on a deploy step that waits for its repair build.
THEN_REDEPLOY = "redeploy"
#: The step-output keys this module owns.
FIXES_KEY = "deploy_fixes"
REDEPLOY_KEY = "redeploy"
FIX_EVENT = "deploy.fix_started"
REDEPLOY_EVENT = "deploy.redeploying"
#: The setting (project_settings.KNOBS) that caps repairs per deploy step.
CAP_SETTING = "deploy_fix_attempts"
DEFAULT_CAP = 2
#: How much of a deployment's log a repair build and the Ask are shown.
LOG_TAIL_CHARS = 3000
MANIFEST_CHARS = 4000


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _scrub(value: str) -> str:
    """Secret-shaped text out of anything a build or a model is shown. The log tail is already
    redacted by value when stored (stack_deploy.redact); this is the second, shape-based net."""
    from .agent_baseline import redact_for_agent

    cleaned = redact_for_agent(str(value or ""))
    return cleaned if isinstance(cleaned, str) else str(value or "")


def fix_cap(connection: Connection, project_id: str, settings: Any) -> int:
    from . import project_settings

    try:
        value = project_settings.effective(connection, project_id, settings, CAP_SETTING)
    except Exception:  # noqa: BLE001 — the default is a complete answer
        value = None
    try:
        return max(0, int(DEFAULT_CAP if value in (None, "") else value))
    except (TypeError, ValueError):
        return DEFAULT_CAP


def _host(connection: Connection, host_id: Optional[str]) -> dict[str, Any]:
    if not host_id:
        return {}
    row = connection.execute(
        text("SELECT id, name, address, status, last_preflight FROM deployment_hosts "
             "WHERE id = :h"), {"h": host_id}).mappings().first()
    if not row:
        return {}
    pf = _json(row["last_preflight"]) or {}
    pf = pf if isinstance(pf, dict) else {}
    return {"id": row["id"], "name": row["name"], "address": row["address"],
            "status": row["status"], "facts": known_facts(pf)}


def known_facts(last_preflight: Mapping[str, Any]) -> dict[str, Any]:
    """What the host's record says it has: the facts a deploy preflight recorded, else what the
    host check (deployment_fleet.verify_host) said about Docker. Never a guess beyond that."""
    facts = last_preflight.get("facts") if isinstance(last_preflight, Mapping) else None
    if isinstance(facts, dict) and facts.get("tools"):
        return dict(facts)
    if isinstance(last_preflight, Mapping) and last_preflight.get("reachable"):
        problems = " ".join(str(p) for p in last_preflight.get("problems") or [])
        return {"tools": {"docker": "docker not found" not in problems}, "versions": {},
                "from": "host check"}
    return {}


def facts_sentence(facts: Optional[Mapping[str, Any]]) -> str:
    """"has Docker 27.1 (compose v2.29), git; has no node, npm, python3" — or ""."""
    facts = facts or {}
    tools = facts.get("tools") or {}
    if not tools:
        return ""
    versions = facts.get("versions") or {}

    def said(tool: str) -> str:
        version = str(versions.get(tool) or "").strip()
        for prefix in ("Docker version ", "Python ", "go version "):
            version = version.removeprefix(prefix)
        version = version.split(",")[0].strip()
        if tool == "docker":
            compose = str(versions.get("compose") or "").strip()
            name = "Docker" + (f" ({version})" if version else "")
            return name + (f", Docker Compose {compose}" if compose else "")
        return f"{tool} ({version})" if version else tool

    has = [said(t) for t in host_has(facts)]
    lacks = [t for t in RUNTIME_TOOLS if tools.get(t) is False] + sorted(
        t for t, present in tools.items() if present is False and t not in RUNTIME_TOOLS)
    parts = []
    if has:
        parts.append("has " + ", ".join(has))
    if lacks:
        parts.append("has no " + ", ".join(lacks))
    return "; ".join(parts)


# ── What failed ──────────────────────────────────────────────────────────────────────────────

def _command_of(manifest_text: str, step_name: str) -> str:
    """The manifest command a failed deployment step ran ("install: install 1" → its `run`)."""
    if not manifest_text or ": " not in str(step_name or ""):
        return ""
    section, name = str(step_name).split(": ", 1)
    try:
        from .app_deploy_manifest import load_manifest

        manifest = load_manifest(manifest_text)
    except Exception:  # noqa: BLE001 — the command is an aid
        return ""
    for step in getattr(manifest, section, ()) or ():
        if step.name == name:
            return step.run
    return ""


def failure_of_deployment(connection: Connection, deployment_id: str) -> Optional[dict[str, Any]]:
    """A failed stack deployment, as the repair is told it and the Ask shows it."""
    row = connection.execute(
        text("SELECT id, status, error, failure_reason, steps, log_tail, manifest, "
             "manifest_text, host_id, target_id, commit_sha, code_task_run_id, preflight "
             "FROM stack_deployments WHERE id = :i"), {"i": deployment_id}).mappings().first()
    if not row:
        return None
    error = _json(row["error"]) or {}
    error = error if isinstance(error, dict) else {}
    steps = _json(row["steps"]) or []
    step = str(error.get("step") or next(
        (s.get("name") for s in steps if isinstance(s, dict) and s.get("status") == "failed"),
        "") or "")
    summary = _json(row["manifest"]) or {}
    host = _host(connection, row["host_id"])
    preflight = _json(row["preflight"]) or {}
    facts = (preflight.get("facts") if isinstance(preflight, dict) else None) \
        or host.get("facts") or {}
    target = connection.execute(
        text("SELECT app_name FROM deployment_targets WHERE id = :t"),
        {"t": row["target_id"]}).scalar()
    return {
        "phase": "apply", "deployment_id": row["id"], "status": row["status"],
        "cause": str(error.get("cause") or ""),
        "headline": str(error.get("headline") or row["failure_reason"] or ""),
        "action": str(error.get("action") or ""),
        "step": step, "command": _command_of(str(row["manifest_text"] or ""), step),
        "log_tail": _scrub(str(row["log_tail"] or "")[-LOG_TAIL_CHARS:]),
        "steps": [{"name": s.get("name"), "status": s.get("status")}
                  for s in steps if isinstance(s, dict)],
        "host_id": row["host_id"], "host_name": host.get("name"),
        "host_address": host.get("address"), "host_facts": facts,
        "target_id": row["target_id"], "target_name": target,
        "manifest_path": (summary or {}).get("manifest_path") if isinstance(summary, dict)
        else None,
        "manifest_text": str(row["manifest_text"] or "")[:MANIFEST_CHARS],
        "commit": row["commit_sha"], "build_run_id": row["code_task_run_id"],
    }


def failure_of_refusal(connection: Connection, refused: Mapping[str, Any],
                       build_run_id: str) -> dict[str, Any]:
    """A plan the deploy refused (stack_deploy.plan_for_workflow_step's `deploy_failure`)."""
    failure = dict(refused)
    host = _host(connection, str(failure.get("host_id") or ""))
    failure.setdefault("host_facts", host.get("facts") or {})
    failure.update({"host_name": host.get("name"), "host_address": host.get("address"),
                    "build_run_id": build_run_id, "step": failure.get("step") or "preflight"})
    failure["manifest_text"] = str(failure.get("manifest_text") or "")[:MANIFEST_CHARS]
    return failure


def is_app_side(failure: Optional[Mapping[str, Any]]) -> bool:
    """Only a failure BOSS PLACED on the app is repaired; anything unplaced is a person's."""
    return bool(failure) and str(failure.get("cause") or "") == CAUSE_APP


def _missing_tool(failure: Mapping[str, Any]) -> str:
    import re

    found = re.search(r"has no `([^`]+)`", str(failure.get("headline") or ""))
    if found:
        return found.group(1)
    found = re.search(r"(?:^|\n)(?:bash: )?(?:line \d+: )?([A-Za-z0-9._+-]+): (?:command )?"
                      r"not found", str(failure.get("log_tail") or ""))
    return found.group(1) if found else ""


def short_cause(failure: Mapping[str, Any]) -> str:
    tool = _missing_tool(failure)
    if tool:
        return f"`{tool}` missing"
    headline = str(failure.get("headline") or "it failed").strip().rstrip(".")
    return headline if len(headline) <= 90 else headline[:88] + "…"


def where(failure: Mapping[str, Any]) -> str:
    return str(failure.get("host_address") or failure.get("host_name") or "the target machine")


def _placed(failure: Mapping[str, Any]) -> str:
    """"HelpDesk AI · staging on vm-1 (2.29.2.202)"."""
    name, address = failure.get("host_name"), failure.get("host_address")
    machine = f"{name} ({address})" if name and address else str(name or address or
                                                                  "its machine")
    return f"{failure.get('target_name') or 'the deploy target'} on {machine}"


def progress_note(failure: Mapping[str, Any], build_run_id: str) -> str:
    return (f"Deploy failed: {short_cause(failure)} on {where(failure)} → fixing the app "
            f"(build {build_run_id}) → redeploying")


def _what_to_change(failure: Mapping[str, Any]) -> str:
    tool = _missing_tool(failure)
    facts = failure.get("host_facts") or {}
    docker = bool((facts.get("tools") or {}).get("docker"))
    if tool and docker:
        return (f"The machine has Docker and no `{tool}`. Switch boss.deploy.yaml to "
                "`kind: compose`: add a Dockerfile that builds and starts the app, a "
                "docker-compose.yml that runs it on $PORT (with a postgres service if the app "
                "uses Postgres, or keep the manifest's `resources:`), `compose: {file: "
                "docker-compose.yml}`, `requires: {docker: true}`, no `install:`, and a health "
                "check on {target.public_url} at a route the app really serves (/readyz or /).")
    if tool:
        return (f"The machine has no `{tool}` and no Docker. Write the manifest for what it has"
                f" ({', '.join(host_has(facts)) or 'see above'}), or make the install steps "
                f"install `{tool}` before using it.")
    step = str(failure.get("step") or "")
    if step == "health":
        return ("The app did not answer its health check. Make it listen on $PORT on all "
                "interfaces, start it in the background for kind: commands (nohup … &) or as "
                "the container's command for kind: compose, and health-check a route it serves.")
    if failure.get("phase") == "plan":
        return f"{failure.get('action') or ''} Change the app or its manifest so this holds."
    return ("Fix the error the command printed (the log tail above) in the app or its "
            "manifest, so this step succeeds on this machine.")


def fix_note(failure: Mapping[str, Any], *, attempt: int, cap: int) -> str:
    """The repair build's brief: what failed, where, on what machine, and what to change."""
    lines = [
        f"THE DEPLOY OF THIS APP FAILED, AND THE CAUSE IS THE APP'S (automatic fix {attempt} of "
        f"{cap}). Fix the app so it deploys on its target machine; keep everything else as it "
        "is, keep boss.deploy.yaml in the app's own folder, and keep it in this pull request.",
        f"Target: {_placed(failure)}; commit {str(failure.get('commit') or '')[:12]}.",
        f"What failed: {failure.get('step') or failure.get('phase')}"
        + (f" — `{failure.get('command')}`" if failure.get("command") else "") + ".",
        f"BOSS said: {failure.get('headline') or ''} {failure.get('action') or ''}".strip(),
    ]
    facts = facts_sentence(failure.get("host_facts"))
    if facts:
        lines.append(f"The machine {facts}.")
    log_tail = str(failure.get("log_tail") or "").strip()
    if log_tail:
        lines.append(f"Log tail (redacted):\n```\n{log_tail[-LOG_TAIL_CHARS:]}\n```")
    manifest = str(failure.get("manifest_text") or "").strip()
    if manifest:
        lines.append(f"The manifest ({failure.get('manifest_path') or 'boss.deploy.yaml'}) "
                     f"as deployed:\n```yaml\n{manifest}\n```")
    lines.append(f"What to change: {_what_to_change(failure)}")
    return "\n\n".join(lines)


# ── Before the build: what the machine has ───────────────────────────────────────────────────

def _deploy_host_id(connection: Connection, project_id: str, run_id: str,
                    run_input: Mapping[str, Any]) -> Optional[str]:
    """The host this run's deploy step ships to, as far as it can be told before the build:
    the step's (or the run's) host or target, else the project's one verified host."""
    from . import workflows

    wanted: dict[str, Any] = {}
    try:
        steps = workflows._run_spec(connection, run_id).get("steps") or []
    except Exception:  # noqa: BLE001 — no spec, no deploy host to name
        steps = []
    for step in steps:
        binding = (step.get("binding") or {}) if isinstance(step, dict) else {}
        if binding.get("capability") != "deploy":
            continue
        for name in ("host_id", "target_id"):
            value = (binding.get("inputs") or {}).get(name)
            if isinstance(value, dict) and value.get("from_input"):
                value = run_input.get(str(value["from_input"]))
            if isinstance(value, str) and value.strip():
                wanted.setdefault(name, value.strip())
    for name in ("host_id", "target_id"):
        value = run_input.get(name)
        if isinstance(value, str) and value.strip():
            wanted.setdefault(name, value.strip())
    if wanted.get("target_id") and not wanted.get("host_id"):
        host = connection.execute(
            text("SELECT host_id FROM deployment_targets WHERE id = :t AND project_id = :p"),
            {"t": wanted["target_id"], "p": project_id}).scalar()
        if host:
            wanted["host_id"] = host
    if wanted.get("host_id"):
        return str(wanted["host_id"])
    verified = connection.execute(
        text("SELECT id FROM deployment_hosts WHERE project_id = :p AND status = 'verified' "
             "LIMIT 2"), {"p": project_id}).scalars().all()
    return str(verified[0]) if len(verified) == 1 else None


def host_brief(connection: Connection, project_id: str, context: Mapping[str, Any]) -> str:
    """Said to a build whose workflow deploys it onto a known machine: what that machine has,
    and the rule for the manifest — written for the machine, not for the builder's laptop."""
    run_id = str(context.get("run_id") or "")
    if not run_id:
        return ""
    run_input = context.get("input") if isinstance(context.get("input"), dict) else {}
    try:
        host = _host(connection, _deploy_host_id(connection, project_id, run_id, run_input))
    except Exception as error:  # noqa: BLE001 — a brief without the host is the old brief
        log.warning("deploy host for %s not read: %s", run_id, error)
        return ""
    facts = facts_sentence(host.get("facts"))
    if not facts:
        return ""
    docker = bool(((host.get("facts") or {}).get("tools") or {}).get("docker"))
    rule = ("Write boss.deploy.yaml for what that machine has: use kind: compose (a Dockerfile "
            "and a docker-compose.yml) when it has Docker and not the app's runtime; use "
            "kind: commands only with programs it has." if docker else
            "Write boss.deploy.yaml for what that machine has: kind: commands, using only "
            "programs it has (or installing what it lacks in the install steps).")
    return (f"THE MACHINE THIS APP DEPLOYS TO: {host.get('name')} ({host.get('address')}) "
            f"{facts}. {rule}")


# ── For the step's Ask ───────────────────────────────────────────────────────────────────────

def deployment_facts(connection: Connection, deployment_id: str) -> dict[str, Any]:
    """The deployment behind a deploy step, for the deploy agent's Ask: its steps and states,
    the failure (step, command, redacted log tail), the host facts and the manifest."""
    failure = failure_of_deployment(connection, deployment_id)
    if not failure:
        return {}
    out: dict[str, Any] = {
        "deployment": f"{deployment_id} — {failure['status']}",
        "deployment steps": ", ".join(f"{s['name']}: {s['status']}" for s in failure["steps"]),
        "deployed to": f"{_placed(failure)}, commit {str(failure.get('commit') or '')[:12]}",
    }
    if failure["status"] == "failed":
        out["deployment failure"] = (f"{failure.get('step') or 'unknown step'}"
                                     + (f" ran `{failure['command']}`" if failure["command"]
                                        else "")
                                     + f": {failure['headline']} {failure['action']}".rstrip()
                                     + f" (cause: {failure.get('cause') or 'unplaced'})")
    facts = facts_sentence(failure.get("host_facts"))
    if facts:
        out["the machine"] = facts
    if failure.get("manifest_text"):
        out["manifest"] = failure["manifest_text"]
    out["_log_tail"] = failure.get("log_tail") or ""
    return out


def fixes_sentence(fixes: list[Mapping[str, Any]]) -> str:
    return "; ".join(
        f"fix {f.get('attempt')}: {f.get('cause')} on {f.get('host') or 'the machine'} → build "
        f"{f.get('build_run_id')}" + (f" (after deployment {f['deployment_id']})"
                                     if f.get("deployment_id") else "")
        for f in fixes if isinstance(f, Mapping))
