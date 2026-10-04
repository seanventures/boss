"""Durable coding-agent task — the pipeline that turns a task spec into a real,
tested, gated pull request.

It composes the pieces built for this feature with the *existing* software-factory
back half:

    reserve budget
      -> EphemeralSandbox.provision (clone the repo)
      -> CodeAgent.run            (edit files + iterate against tests in the sandbox)
      -> BOSS re-runs the tests   (authoritative pass/fail)
      -> propose_pull_request     (commits the diff, opens a receipted PR,
                                   creates the review_pull_request merge gate,
                                   settles the budget)            <-- reused
      -> teardown sandbox

Durability model (see code_task_runs DDL): the sandbox working tree is
filesystem-ephemeral, so the whole pipeline runs inside ONE `code.task` job.
Crash-resumability is provided by the job lease-reclaim (a crash re-runs the
attempt) plus the PR receipt dedupe (dedupe_key=code:task:<run_id>), which makes
re-opening the PR a no-op. If the run already recorded a PR url, the handler is a
no-op (idempotent).
"""
from __future__ import annotations

import json
import re
import logging
from datetime import datetime, timezone
from typing import Optional

from ..config import Settings
from ..ports import get_engine_ports, sql
from .code_agent import CodeTaskSpec, RepoContext, make_code_agent
from .jobs import JOB_PRIORITY_BACKGROUND, JOB_PRIORITY_REQUESTED
from .core import emit_event
from .core import emit_event, make_id
from .github_app_adapter import GitHubAppAdapter
from .iac_verify import is_iac_task
from .llm_telemetry import clear_llm_context, record_llm_call, set_llm_context
from .agent_acts import pr_trailers as _pr_trailers
from .sandbox import SandboxError
from .sandbox_provider import make_sandbox_provider
from .software_factory import SoftwareFactoryService

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Persistence helpers                                                        #
# --------------------------------------------------------------------------- #
def _engine_ports():
    ports = get_engine_ports()
    if ports is None:
        raise RuntimeError(
            "engine ports are not configured; configure EnginePorts at startup"
        )
    return ports


def _transaction(engine):
    return _engine_ports().persistence.transaction(engine)


def _row_to_run(r) -> dict:
    def _j(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v or {}

    return {
        "id": r["id"], "project_id": r["project_id"], "task_id": r["task_id"],
        "status": r["status"], "phase": r["phase"], "title": r["title"],
        "spec": _j(r["spec"]), "state": _j(r["state"]), "usage": _j(r["usage"]),
        "finalize_ctx": _j(r["finalize_ctx"]), "reservation_id": r["reservation_id"],
        "pull_request_url": r["pull_request_url"], "pull_request_number": r["pull_request_number"],
        "decision_id": r["decision_id"], "error": r["error"],
        # When it started and last moved (factory spec §6.4): additive, for every reader.
        "created_at": _iso_or_none(_get(r, "created_at")),
        "updated_at": _iso_or_none(_get(r, "updated_at")),
    }


def _get(r, key):
    try:
        return r.get(key) if hasattr(r, "get") else r[key]
    except (KeyError, IndexError):
        return None


def _iso_or_none(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def load_code_task_run(connection, run_id: str) -> Optional[dict]:
    if hasattr(connection, "get_run"):
        return connection.get_run(run_id)
    r = connection.execute(
        sql("SELECT id, project_id, task_id, status, phase, title, spec, state, usage, "
             "finalize_ctx, reservation_id, pull_request_url, pull_request_number, decision_id, error, "
             "created_at, updated_at "
             "FROM code_task_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().first()
    return _row_to_run(r) if r else None


#: Title prefixes BOSS gives its OWN maintenance runs. Kept beside the query, because the
#: console filters on the same list and the two drifting apart is the bug this exists to
#: prevent.
#:
#: Deployment-specific prefixes come from the environment rather than being written here. One
#: operator's platform runs "<platform> profile check" against itself, and hardcoding that name
#: in the engine would couple the OSS package to a single deployment -- which the port rules
#: forbid, and rightly: the next operator's housekeeping is called something else.
_DEFAULT_INTERNAL_PREFIXES = ("captain:", "quality improve pass")


def _internal_run_prefixes() -> tuple[str, ...]:
    import os

    extra = tuple(
        p.strip().lower()
        for p in (os.environ.get("BOSS_INTERNAL_RUN_PREFIXES") or "").split(",")
        if p.strip()
    )
    return _DEFAULT_INTERNAL_PREFIXES + extra


INTERNAL_RUN_PREFIXES = _internal_run_prefixes()


def list_code_task_runs(connection, project_id: str, limit: int = 50,
                        *, exclude_internal: bool = False) -> list[dict]:
    """Newest runs for a project, optionally without BOSS's own housekeeping.

    WHY THE FILTER BELONGS HERE AND NOT IN THE CONSOLE
    --------------------------------------------------
    The console fetches N newest and then drops internal runs, so a burst of housekeeping does
    not just add noise -- it consumes the entire window. This has now happened twice. The limit
    was raised 50 -> 200 in August for exactly this reason, and today 200 of the 200 newest runs
    were "Captain: QUALITY IMPROVE PASS", so the Factory rendered an empty grid under a heading
    that said "200 code tasks". Every app the operator had built was still there, just older
    than the window.

    Raising the limit again does not fix it; it only moves it, and this response is already
    1.41 MB at 200. Filtering before the LIMIT means the window holds real work by construction.
    """
    if hasattr(connection, "list_runs"):
        return connection.list_runs(project_id, limit)
    where = "project_id = :p"
    params: dict = {"p": project_id, "lim": int(limit)}
    if exclude_internal:
        # Matched case-insensitively at the START of the title, the same shape the console has
        # always used, so the two agree about what "internal" means.
        clauses = []
        for i, prefix in enumerate(_internal_run_prefixes()):
            clauses.append(f"lower(coalesce(title, '')) NOT LIKE :pre{i}")
            params[f"pre{i}"] = f"{prefix}%"
        where += " AND " + " AND ".join(clauses)
    rows = connection.execute(
        sql("SELECT id, project_id, task_id, status, phase, title, spec, state, usage, "
            "finalize_ctx, reservation_id, pull_request_url, pull_request_number, decision_id, error, "
             "created_at, updated_at "
            f"FROM code_task_runs WHERE {where} ORDER BY created_at DESC LIMIT :lim"),
        params,
    ).mappings().all()
    return [_row_to_run(r) for r in rows]


def is_internal_run(run: dict) -> bool:
    """BOSS's own housekeeping build (the Captain's quality passes): same prefixes as the list."""
    title = str((run or {}).get("title") or "").lower()
    return any(title.startswith(p) for p in _internal_run_prefixes())


def list_internal_code_task_runs(connection, project_id: str, limit: int = 10) -> list[dict]:
    """The newest of BOSS's own builds. Kept OUT of the main window (they used to fill it) and
    read here on their own, so the Factory can still give them a home: they are builds."""
    if hasattr(connection, "list_runs"):
        return [r for r in connection.list_runs(project_id, limit * 4) if is_internal_run(r)][:limit]
    params: dict = {"p": project_id, "lim": int(limit)}
    clauses = []
    for i, prefix in enumerate(_internal_run_prefixes()):
        clauses.append(f"lower(coalesce(title, '')) LIKE :pre{i}")
        params[f"pre{i}"] = f"{prefix}%"
    rows = connection.execute(
        sql("SELECT id, project_id, task_id, status, phase, title, spec, state, usage, "
            "finalize_ctx, reservation_id, pull_request_url, pull_request_number, decision_id, "
            "error, created_at, updated_at "
            f"FROM code_task_runs WHERE project_id = :p AND ({' OR '.join(clauses)}) "
            "ORDER BY created_at DESC LIMIT :lim"),
        params,
    ).mappings().all()
    return [_row_to_run(r) for r in rows]


def count_code_task_runs(
    connection, project_id: str, *, exclude_internal: bool = False
) -> int:
    """How many runs there ACTUALLY are, under the same filter as the list.

    A window with no total behind it cannot tell "these are all of them" from "these are the
    first 200 of 244", and the console has been reporting the first as though it were the
    second. Counting is cheap — it reads no payload columns, which is what made the list
    expensive in the first place.

    Built from the same `where` construction as the list on purpose: a count that disagrees
    with its own list is worse than no count.
    """
    if hasattr(connection, "list_runs"):          # the in-memory test double has no SQL
        return len(connection.list_runs(project_id, 10_000))
    where = "project_id = :p"
    params: dict = {"p": project_id}
    if exclude_internal:
        clauses = []
        for i, prefix in enumerate(_internal_run_prefixes()):
            clauses.append(f"lower(coalesce(title, '')) NOT LIKE :pre{i}")
            params[f"pre{i}"] = f"{prefix}%"
        where += " AND " + " AND ".join(clauses)
    return int(connection.execute(
        sql(f"SELECT count(*) FROM code_task_runs WHERE {where}"), params
    ).scalar() or 0)


_DEPLOYABILITY_NOTE = (
    "\n\nDEPLOYABILITY (required for any network service): bind the host from the HOST env var "
    "(default 0.0.0.0) and the port from the PORT env var, with sensible fallbacks. Do NOT hardcode "
    "127.0.0.1 or a fixed port for the running server — it must run in a container / behind a proxy. "
    "(Unit tests may still bind loopback + an ephemeral port.) Binding 0.0.0.0 here is expected for a "
    "containerized internal tool behind the platform proxy — it is not a vulnerability."
)


def _allowed_sandbox_commands(settings) -> set[str]:
    return {
        item.strip()
        for item in (settings.sandbox_validation_allowed_commands or "").split(",")
        if item.strip()
    }


def preflight_test_commands(settings, test_commands) -> list[str]:
    """Return the test commands the sandbox WILL reject, before a run is created.

    The sandbox enforces an executable allowlist but nothing ever told the caller (or the
    coding agent) what is on it, so a command like `[ -f x ] && npm test` was accepted at
    creation, ran the whole expensive pipeline, and only died at validation time. Catching
    it here turns a multi-dollar failure into an instant, free, actionable error.
    """
    import shlex

    allowed = _allowed_sandbox_commands(settings)
    if not allowed:
        return []
    rejected: list[str] = []
    for cmd in test_commands or []:
        try:
            parts = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        except ValueError:  # unbalanced quotes — let the sandbox be the judge
            continue
        if not parts:
            continue
        executable = parts[0]
        candidate = executable[2:] if executable.startswith("./") else executable
        if "/" in candidate or "\\" in candidate or candidate not in allowed:
            rejected.append(executable)
    return rejected


#: "· retry 2" describes the attempt, not the work.
_RETRY_SUFFIX = re.compile(r"\s*·\s*retry\s*\d+\s*$", re.I)


def work_fingerprint(*, project_id: str, owner: str, repo: str, base_branch: str,
                    title: str, instructions: str) -> str:
    """A stable identity for "this exact piece of work in this exact place".

    Hashed from the inputs a caller controls, BEFORE the engine decorates them, so the same
    request always produces the same fingerprint. Whitespace-normalised because a form and an
    API client disagree about trailing newlines and neither means a different task.
    """
    import hashlib

    # IDENTIFY THE WORK, NOT THE REQUEST TEXT.
    #
    # This hashed the title and instructions verbatim, and every retry mutates BOTH by design:
    # the title gains "· retry N" and the instructions gain an ITERATION FEEDBACK block. So a
    # retry could never collide with its own original — the exact case the guard exists for.
    #
    # Observed: the autopilot retried a build at 19:14 while an operator restarted the same
    # build at 19:16. Two full budgeted pipelines for one app, at two priorities, neither aware
    # of the other, both queued behind an auto-repair nobody asked for.
    stable_title = _RETRY_SUFFIX.sub("", " ".join(title.split()))
    stable_instr = instructions
    for marker in _APPENDED_BLOCKS:
        stable_instr = stable_instr.split(marker)[0]
    parts = [project_id, owner, repo, base_branch, stable_title,
             " ".join(stable_instr.split())]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


#: Blocks the engine APPENDS to a brief while a lineage runs. They describe the attempt, not
#: the work, so they must not change the work's identity.
_APPENDED_BLOCKS = (
    "\n\n=== ITERATION FEEDBACK",
    "\n\n=== OPERATOR STEERING",
)


def active_same_app(connection, project_id: str, repo: str, title: str) -> Optional[str]:
    """An in-flight run building the SAME APP in the same repo, if there is one.

    The fingerprint above is exact: change one sentence of the brief and it is, correctly, a
    different fingerprint. That is too strict to be the only guard. Two runs building "Shorty"
    in one repo at the same time is wrong however the briefs are worded — the second cannot
    produce anything the first is not already producing, and both cost a full pipeline.

    Compares the app NAME, so "Shorty — self-hosted URL shortener" and
    "Shorty — self-hosted URL shortener · retry 2" are recognised as one app.
    """
    from .naming import project_name

    wanted, _blurb, _label = project_name(title)
    if not wanted or wanted == "Untitled app":
        return None

    rows: list = []
    if hasattr(connection, "execute"):
        try:
            rows = connection.execute(
                sql("SELECT id, title FROM code_task_runs "
                    "WHERE project_id = :p AND status IN ('running', 'queued') "
                    "  AND spec->>'repo' = :r "
                    "ORDER BY created_at ASC LIMIT 40"),
                {"p": project_id, "r": repo}).mappings().all()
        except RuntimeError:
            # See active_duplicate: a store can define execute() and still be unable to run
            # SQL. Ask the port rather than letting the guard take down the retry it guards.
            rows = _in_flight_via_port(project_id, repo=repo)
    else:
        rows = _in_flight_via_port(project_id, repo=repo)

    # SAME RULE AS IDENTITY. Comparing with casefold() alone treated punctuation as meaning,
    # so "Incident Timeline Builder." and "Incident-Timeline Builder" were different apps to
    # this guard — it did not refuse the second build, and the console then showed the one
    # product as two projects with two PRs and two bills. A definition good enough to group a
    # card has to be the definition that blocks the duplicate.
    from .naming import identity_key

    for row in rows:
        other, _b, _l = project_name(str(row["title"] or ""))
        if identity_key(other) and identity_key(other) == identity_key(wanted):
            return row["id"]
    return None


def _in_flight_via_port(project_id: str, *, repo: Optional[str] = None) -> list[dict]:
    """In-flight runs for a project, from a store that cannot run SQL.

    Returns [] when the store cannot answer. An unanswerable duplicate check must not block
    legitimate work: a second run is a lesser harm than no runs at all.
    """
    try:
        runs = _engine_ports().persistence.list_runs(project_id, limit=200)
    except Exception:
        return []
    out = [
        r for r in runs
        if str(r.get("status")) in ("running", "queued")
        and (repo is None or (r.get("spec") or {}).get("repo") == repo)
    ]
    # Oldest first, matching the SQL: the run to keep is the one already underway.
    out.sort(key=lambda r: str(r.get("created_at") or ""))
    return out


def active_duplicate(connection, project_id: str, fingerprint: str) -> Optional[str]:
    """The id of an identical run that is STILL IN FLIGHT, if there is one.

    In flight only. Running the same task again after it finished is a legitimate act — that
    is what the Launch button on a finished card is for. What is never intended is the same
    work started twice while the first is still going: nothing can come of the second that the
    first was not already producing, and each one is a full budgeted pipeline.
    """
    if not hasattr(connection, "execute"):
        return _active_duplicate_via_port(project_id, fingerprint)
    try:
        row = connection.execute(
            sql("SELECT id FROM code_task_runs "
                "WHERE project_id = :p AND status IN ('running', 'queued') "
                "  AND spec->>'work_fingerprint' = :fp "
                "ORDER BY created_at ASC LIMIT 1"),
            {"p": project_id, "fp": fingerprint}).first()
    except RuntimeError:
        # HAVING `execute` IS NOT THE SAME AS BEING ABLE TO RUN SQL.
        #
        # The OSS in-memory adapter defines execute() and raises from it, so the hasattr guard
        # above let this through and every retry died with "raw SQL is unavailable" — the
        # duplicate CHECK took down the thing it was protecting.
        #
        # Falling back to the port keeps the protection working there rather than switching it
        # off, which matters: without it the OSS path would happily start the same pipeline
        # twice, and each one is a full budget.
        return _active_duplicate_via_port(project_id, fingerprint)
    return row[0] if row else None


def _active_duplicate_via_port(project_id: str, fingerprint: str) -> Optional[str]:
    """The same question, asked of a store that cannot run SQL.

    Returns None when the store cannot answer at all — an unanswerable duplicate check must not
    block legitimate work, and a second run is a lesser harm than no runs.
    """
    matching = [
        r for r in _in_flight_via_port(project_id)
        if (r.get("spec") or {}).get("work_fingerprint") == fingerprint
    ]
    return str(matching[0].get("id")) if matching else None


# Simulated fixture: create→persist→read without a model, reservation, job, or PR.
# This is NOT a sealed factory path. It exists so Community GA can prove the
# code-task API fail-closes cleanly without spending tokens. Real workers ignore
# status='simulated' (WIP counts only status='running').
SIMULATED_FIXTURE_STATUS = "simulated"
SIMULATED_FIXTURE_PHASE = "fixture"


def is_simulated_run(run: Optional[dict]) -> bool:
    if not isinstance(run, dict):
        return False
    spec = run.get("spec") or {}
    return bool(run.get("status") == SIMULATED_FIXTURE_STATUS or spec.get("simulated"))


def start_code_task(
    connection, *, project_id: str, task_id: Optional[str], title: str,
    instructions: str, acceptance_criteria: list[str], test_commands: list[str],
    owner: str, repo: str, base_branch: str, finalize_ctx: Optional[dict],
    jobs, profile: Optional[str] = None, risk_tier: Optional[int] = None,
    model_config: Optional[dict] = None, open_pr: Optional[bool] = None,
    repair_retries: Optional[int] = None, review_panel_size: Optional[int] = None,
    substrate: Optional[str] = None, repo_host: Optional[str] = None,
    allow_duplicate: bool = False, simulated: bool = False,
    job_priority: Optional[int] = None,
    reference_links: Optional[list[str]] = None,
    role_key: Optional[str] = None,
) -> str:
    """Create the durable run row and enqueue the single `code.task` job.

    ``role_key`` runs the task AS that agent: its held MCP servers reach the harness
    (code_task_mcp). It must name an agent of this project.

    ``simulated=True`` is the no-spend fixture: persist a clearly labeled run with
    fixture evidence and do **not** enqueue a job, reserve budget, or call a model.
    ``open_pr=True`` with ``simulated=True`` fail-closes.

    Refuses to start work that is ALREADY RUNNING, returning the in-flight run instead.

    This is prevention, not detection, and the difference is the whole point. The Task
    Shepherd only sweeps runs that have already FAILED, and its `DUPLICATE_EXECUTION` class
    means a lease reassigned mid-run — not the same task submitted twice. So seven identical
    healthy benchmark runs presented nothing for anything to react to, and sat there queued,
    each one a full budgeted pipeline that could produce nothing the first was not already
    producing. Detecting that after the fact only tells you what you have already spent.

    Deliberately scoped to IN-FLIGHT work: relaunching a finished task is a legitimate act,
    and `allow_duplicate=True` exists for a caller that genuinely wants two at once (a
    bake-off comparing two runs of one spec).
    """
    if simulated and open_pr is True:
        raise ValueError(
            "simulated=true cannot open a PR (fail-closed; fixture has no sandbox or model)"
        )
    settings = getattr(jobs, "settings", None)
    if settings is not None:
        bad = preflight_test_commands(settings, test_commands)
        if bad:
            allowed = ", ".join(sorted(_allowed_sandbox_commands(settings)))
            raise ValueError(
                "test_commands use executables the sandbox will reject: "
                + ", ".join(repr(b) for b in bad)
                + ". Shell builtins and operators (`[`, `test`, `&&`, pipes) are NOT "
                "available — give each test command as a single allowlisted executable "
                "with arguments. Allowed: " + allowed
            )
    # Hash the work, not the boilerplate. A relaunch passes back instructions that already
    # carry the deployability note while the original was hashed before it was added — so the
    # same work produced two different fingerprints and looked like two different tasks.
    from .naming import stable_app_id

    # Stamp the app identity NOW. Doing it only on retry is what split one app into four
    # cards; a run started fresh is precisely the case lineage cannot cover.
    fctx = dict(finalize_ctx or {})
    if not fctx.get("app_id"):
        derived = stable_app_id(project_id, repo, title)
        if derived:
            fctx["app_id"] = derived
    finalize_ctx = fctx

    fingerprint = work_fingerprint(project_id=project_id, owner=owner, repo=repo,
                                   base_branch=base_branch, title=title,
                                   instructions=instructions.replace(_DEPLOYABILITY_NOTE, ""))
    if not allow_duplicate:
        existing = active_duplicate(connection, project_id, fingerprint)
        if not existing:
            # Second, coarser guard. The fingerprint is exact by design — reword one sentence
            # and it is correctly a different fingerprint — which makes it too strict to be the
            # ONLY check. Two runs building the same app in one repo at the same time is wrong
            # however the briefs are worded.
            existing = active_same_app(connection, project_id, repo, title)
        if existing:
            # Idempotent, not an error: the caller wanted this work running, and it is.
            # Raising would make an impatient second click look like a broken button, which
            # is what produced the duplicates in the first place.
            try:
                emit_event(connection, project_id, "code_task.duplicate_suppressed",
                           "code_task_service", "code_task_run", existing,
                           {"title": title, "fingerprint": fingerprint})
            except Exception:  # noqa: BLE001 — never fail a start over its own audit trail
                pass
            return existing

    run_id = make_id("crun")
    spec = {
        "work_fingerprint": fingerprint,
        # Append the note ONCE. Relaunching a task sends back the instructions as stored —
        # note included — and appending unconditionally added a second copy, then a third:
        # 1987 chars became 2447 became 2907, the same boilerplate repeated. Worse, it moved
        # the fingerprint every time, so duplicate detection never matched and three identical
        # runs started from three presses of one button.
        "instructions": (instructions if _DEPLOYABILITY_NOTE.strip() in instructions
                         else instructions + _DEPLOYABILITY_NOTE),
        "acceptance_criteria": acceptance_criteria or [],
        "test_commands": test_commands or [],
        # Material the operator said to build AGAINST — an API doc, a repo whose conventions to
        # follow. Kept on the spec rather than glued onto the instructions so it survives a
        # relaunch intact and does not move the work fingerprint.
        "reference_links": [str(link) for link in (reference_links or []) if str(link).strip()],
        "owner": owner, "repo": repo, "base_branch": base_branch,
    }
    if settings is not None:
        # Enforce-AND-inform: the sandbox allowlist used to be enforced silently, so the
        # coding agent had to guess and kept authoring commands that were rejected at
        # validation time. Carry it on the spec so the stage prompts can state it.
        spec["allowed_test_executables"] = sorted(_allowed_sandbox_commands(settings))
    role = str(role_key or "").strip().lower()
    if role:
        known = connection.execute(
            sql("SELECT 1 FROM roles WHERE project_id = :p AND role_key = :r"),
            {"p": project_id, "r": role}).first()
        if not known:
            raise ValueError(f"no agent {role!r} on this project to run this task as.")
        spec["role_key"] = role
    if substrate:  # per-run substrate override (retry fallback: microvm infra-failure -> local)
        spec["substrate"] = substrate
    if repo_host:  # BOSS-267: per-task SCM provider (github default; bitbucket, ...)
        spec["repo_host"] = repo_host
    # A withdrawn profile is downgraded HERE, at the one place every caller funnels through —
    # the benchmark endpoint, the console, the relaunch button, an agent over MCP. Doing it per
    # caller would mean finding them all again when the profile is re-enabled.
    if profile:
        from .pipeline_profile_service import usable_profile

        effective, substitution = usable_profile(profile, settings)
        if substitution:
            spec["profile_substitution"] = substitution
            logging.getLogger(__name__).info(
                "code_task.profile_downgraded run=%s %s", run_id, substitution)
        profile = effective
    if profile:  # per-task cost/rigor override (else the project setting applies)
        spec["profile"] = profile
    if isinstance(risk_tier, int):
        spec["risk_tier"] = risk_tier
    if model_config:  # VAG B2: per-run (engine, model) routing — see route_for_step
        spec["model_config"] = model_config
    if isinstance(repair_retries, int):  # VAG Phase 3: per-run repair-loop budget override
        spec["repair_retries"] = repair_retries
    bench = (finalize_ctx or {}).get("qa_benchmark") if isinstance((finalize_ctx or {}).get("qa_benchmark"), dict) else {}
    if bench.get("app_root"):
        spec["app_root"] = bench["app_root"]
    if bench.get("project_kind"):
        spec["project_kind"] = bench["project_kind"]
    if isinstance(review_panel_size, int):  # VAG Phase 1 (#9): per-run refute-panel size override
        spec["review_panel_size"] = review_panel_size
    if open_pr is False:  # eval/bake-off runs: verify via the gate, do not open a PR
        spec["open_pr"] = False
    if simulated:
        spec["simulated"] = True
        spec["open_pr"] = False
        # Never persist a live model route on a fixture — it looks like spend happened.
        spec.pop("model_config", None)
    run_status = SIMULATED_FIXTURE_STATUS if simulated else "running"
    run_phase = SIMULATED_FIXTURE_PHASE if simulated else "queued"
    run_state = (
        {
            "simulated": True,
            "summary": "Simulated fixture — no sandbox, no model, no PR, no job.",
            "tests_passed": None,
            "diff_summary": "",
            "evidence": {
                "kind": "factory_fixture",
                "llm_called": False,
                "budget_reserved": False,
                "job_enqueued": False,
                "reason": (
                    "POST /v1/code-tasks simulated=true seals create→persist→read "
                    "without spend. This is not a sealed factory path."
                ),
            },
        }
        if simulated
        else {}
    )
    run_usage = {"cost_usd_micros": 0, "simulated": True, "model": None} if simulated else {}
    run_finalize = dict(finalize_ctx or {})
    if simulated:
        run_finalize["simulated"] = True
        run_finalize["fixture"] = True
    # WHICH WORKFLOW GOVERNS THIS, recorded at creation.
    #
    # The workflow runtime and the coding pipeline were built as separate systems. The console
    # could show 54 workflow definitions and a list of runs with no way to say that either had
    # anything to do with the other, because a run had nowhere to record it.
    #
    # Written into finalize_ctx rather than a new column: additive, no migration, and the same
    # place app_id / origin / execution_substrate already live, so the API returns it and the
    # console reads it with no plumbing. A real foreign key is the better long-term shape — it
    # is what makes "every run of this workflow" a cheap query — and this does not preclude it.
    #
    # Resolution failing is the NORMAL state until a project maps one, so it is never fatal.
    # IS THIS A NEW PROJECT, or more work on an existing one?
    #
    # Only a first-generation run gets an outer workflow. A follow-up, a retry, a deferred
    # findings task or one slice of a decomposition is work INSIDE a project that already has
    # its own run — giving each of them a fresh nine-step pipeline would convene a panel per
    # retry and ask for an approval per slice.
    #
    # `root_run_id` is deliberately NOT in this list, though it names a lineage. A web-app build
    # mints one for the scaffold branch before any task exists (`root = make_id("crun")` in
    # prd_decomposition) — so the FIRST task of a brand new app carries a root_run_id, and
    # treating that as "continuation" would have silently skipped an outer run for the panel
    # path, which is the main way projects are started.
    # AN EXPLICIT CHOICE OVERRIDES ALL OF THIS. "Add to this project" lets the operator pick
    # the path the change should follow, and a picker whose choice is then ignored is worse
    # than no picker. Choosing one makes the change a piece of work with its own outer run;
    # choosing nothing leaves a follow-up as what it was, work inside the project's existing
    # run.
    # A task created BY a workflow step already belongs to that run. Starting another would
    # nest a pipeline inside a step of the pipeline that made it, and each would wait on the
    # other's build.
    # THE GOVERNING RUN MAY ARRIVE INSIDE `origin`.
    #
    # `create_code_task` sets `workflow_run_id` at the top level, because a workflow STEP made
    # the task. A project born from a panel does not go through that tool: approving the
    # spec_review gate launches the build directly, and what it can carry is `origin` — the
    # panel, the decision, and now the run that approval belonged to. Reading both is what makes
    # the guard cover the path projects are actually started on, rather than only the one a tool
    # happens to use. Lifted to the top level so everything downstream — the adopt bookkeeping,
    # the console's "which run governs this" — sees it in the one place it has always looked.
    _chose_a_workflow = bool(run_finalize.get("workflow_version_id"))
    # A WORKFLOW PICKED FOR THIS CHANGE IS NOT OVERRULED BY THE ONE THE PROJECT CAME FROM. A
    # change to a panel-born project carries that panel's run in `origin`; lifting it made the
    # change "already governed", so the workflow the operator had just picked was recorded and
    # never started — the change ran inline under a choice that said otherwise.
    if not run_finalize.get("workflow_run_id") and not _chose_a_workflow:
        _from_origin = (run_finalize.get("origin") or {}).get("workflow_run_id")
        if _from_origin:
            run_finalize["workflow_run_id"] = str(_from_origin)
    _already_governed = bool(run_finalize.get("workflow_run_id"))
    _continuation = ("is_followup", "parent_run_id", "deferred_from_run", "qa_benchmark")
    _is_new_project = (not _already_governed) and (_chose_a_workflow or (
        not any(run_finalize.get(k) for k in _continuation)
        # Slice 1 of a decomposition IS the project; slices 2..N are the rest of the same one.
        and int(run_finalize.get("decomposition_index") or 1) <= 1
    ))

    try:
        from .workflows import workflow_by_version, workflow_for_code_task

        # AN EXPLICIT CHOICE WINS. The caller may name a workflow version — the picker on the
        # New project form does — and the person starting the work saying which path it should
        # follow outranks any mapping resolved on their behalf. Falls through to resolution if
        # the id names nothing in this project, rather than silently recording a choice that
        # was not honoured.
        governing = None
        chosen_id = run_finalize.pop("workflow_version_id", None)
        if chosen_id:
            governing = workflow_by_version(connection, project_id, str(chosen_id))
        if not governing:
            governing = workflow_for_code_task(
                connection, project_id=project_id, task_id=task_id)
        if governing:
            run_finalize["workflow"] = governing
    except Exception:  # noqa: BLE001 — an unmapped task must still run
        pass
    if hasattr(connection, "save_run"):
        connection.save_run(
            run_id,
            {
                "project_id": project_id,
                "task_id": task_id,
                "status": run_status,
                "phase": run_phase,
                "title": title,
                "spec": spec,
                "state": run_state,
                "usage": run_usage,
                "finalize_ctx": run_finalize,
                "reservation_id": None,
                "pull_request_url": None,
                "pull_request_number": None,
                "decision_id": None,
                "error": None,
            },
        )
    else:
        connection.execute(
            sql(
                """
                INSERT INTO code_task_runs (
                    id, project_id, task_id, status, phase, title, spec,
                    state, usage, finalize_ctx
                )
                VALUES (
                    :id, :p, :t, :status, :phase, :title,
                    CAST(:spec AS JSONB), CAST(:state AS JSONB),
                    CAST(:usage AS JSONB), CAST(:fc AS JSONB)
                )
                """
            ),
            {
                "id": run_id,
                "p": project_id,
                "t": task_id,
                "status": run_status,
                "phase": run_phase,
                "title": title,
                "spec": json.dumps(spec),
                "state": json.dumps(run_state),
                "usage": json.dumps(run_usage),
                "fc": json.dumps(run_finalize),
            },
        )
    if simulated:
        emit_event(
            connection,
            project_id,
            "code_task.simulated_fixture",
            "boss",
            "code_task_run",
            run_id,
            {
                "task_id": task_id,
                "title": title,
                "repo": f"{owner}/{repo}",
                "llm_called": False,
                "budget_reserved": False,
                "job_enqueued": False,
            },
        )
        return run_id
    # Work an operator asked for runs at REQUESTED. Work the system decided to do on its own
    # — a shepherd retry — must not queue ahead of a person, so the caller passes BACKGROUND.
    jobs.create_job(connection, project_id=project_id, task_id=task_id,
                    kind="code.task",
                    priority=(job_priority if job_priority is not None
                              else JOB_PRIORITY_REQUESTED),
                    payload={"run_id": run_id})
    emit_event(connection, project_id, "code_task.started", "boss", "code_task_run", run_id,
               {"task_id": task_id, "title": title, "repo": f"{owner}/{repo}"})

    # THE OUTER WORKFLOW RUN, started HERE rather than at the API endpoint.
    #
    # Not every project is created through POST /v1/code-tasks. Approving a PRD calls
    # start_web_app_build / start_decomposed_build directly, so a project born the ordinary way
    # — convene a panel, approve, build — got no outer run at all, and the Workflows page stayed
    # empty while a project ran. This function is the one door every path goes through.
    #
    # Adoption, not repetition: the run carries the ids of the work this project ALREADY has, so
    # `build` waits on this code task instead of starting a second one, and — when the project
    # came from a panel — `meeting` waits on that panel instead of convening another.
    if _is_new_project:
        try:
            from .workflows import start_project_run

            origin = run_finalize.get("origin") or {}
            started = start_project_run(
                connection,
                project_id=project_id,
                actor="boss",
                title=title,
                directive=instructions,
                # THE SAME WORKFLOW THE TASK RECORDS. It was already resolved above — honouring
                # the picker on the New project form, then any mapping, then the default. Letting
                # start_project_run resolve it a second time would let the two disagree: the
                # project page naming one workflow and the run that governs it following another.
                workflow_version_id=(run_finalize.get("workflow") or {}).get("version_id"),
                extra_input={
                    "acceptance_criteria": acceptance_criteria,
                    "test_commands": test_commands,
                    "repository": repo,
                    "adopt_code_task_run_id": run_id,
                    "adopt_meeting_run_id": origin.get("meeting_run_id"),
                    "adopt_discovery_run_id": origin.get("discovery_run_id"),
                    "adopt_decision_id": origin.get("decision_id"),
                },
            )
            if started:
                connection.execute(
                    sql("UPDATE code_task_runs SET finalize_ctx = "
                        "COALESCE(finalize_ctx,'{}'::jsonb) || "
                        "jsonb_build_object('workflow_run_id', CAST(:w AS text)) "
                        "WHERE id = :r"),
                    {"w": started.get("id"), "r": run_id},
                )
        except Exception as exc:  # noqa: BLE001 — a project must start without an outer run
            logger.warning("could not start the outer workflow run for %s: %s", run_id, exc)

        # AND SOMEBODY LEADS IT.
        #
        # The Engineering Captain existed, appeared on the org chart, and did nothing: the
        # swarm that puts it in charge — PRD, QA, UX, certification panel — was bound only for
        # official benchmark runs. So every ordinary project was built by an implementer with
        # nobody above it, and the captain was a name on a chart.
        #
        # Behind a knob, because leading a project costs agent turns: a team that wants builds
        # unled sets captain_leads_projects to 0 and gets exactly today's behaviour.
        try:
            from .project_settings import effective as _effective_setting

            if int(_effective_setting(
                    connection, project_id, getattr(jobs, "settings", None),
                    "captain_leads_projects") or 0):
                from .agent_process import bind_captain_swarm

                bind_captain_swarm(
                    connection,
                    settings=jobs.settings,
                    project_id=project_id,
                    actor="boss",
                    prd=instructions,
                    title=f"Captain swarm — {title}"[:200],
                    attach_run_id=run_id,
                    official=False,
                )
        except Exception as exc:  # noqa: BLE001 — an unled build is better than no build
            logger.warning("could not put a captain on %s: %s", run_id, exc)
    return run_id


def work_branch_for(run: dict) -> str:
    """The branch a run's code lives on — the branch further work must build on.

    Attempts in a lineage share ONE branch derived from the root run, so this is the single
    answer to "where is this app's code", and every caller that needs it should ask here rather
    than re-deriving the naming rule.
    """
    fctx = run.get("finalize_ctx") or {}
    root = fctx.get("root_run_id") or run["id"]
    return f"boss/code/{root}"


def start_followup_task(
    connection, *, parent_run_id: str, instructions: str, jobs,
    title: Optional[str] = None, acceptance_criteria: Optional[list] = None,
    test_commands: Optional[list] = None, profile: Optional[str] = None,
    actor: str = "operator", engine: Optional[str] = None,
    review_panel_size: Optional[int] = None,
) -> dict:
    """Continue work on an app that already exists, from the run that built it.

    BOSS could already do this for itself: when a review deferred a finding, it spawned a task
    on the shipped branch with the parent's repo, profile and lineage. It could not be ASKED to.
    An operator wanting "this app should save to a database instead of React state" had no path
    to it -- the console only offered "run this task again", which rebuilds the same thing.

    What that cost is worth stating plainly, because it is the normal case and not an edge:
    someone had to find the app's branch among ninety-nine of them, identify it by matching
    strings from the built bundle against the source, and hand-write `base_branch` into an API
    payload. Nobody outside this repository could have done it. Yet almost all real software
    work is changing software that already exists.

    Lineage is why this belongs here rather than in a caller: the follow-up shares the parent's
    root, so the preview knows which app it is (a follow-up spawned as its own root once
    previewed a different application entirely), and the clone starts from the branch holding
    the code being changed rather than from base.
    """
    run = load_code_task_run(connection, parent_run_id)
    if run is None:
        raise ValueError(f"code task run not found: {parent_run_id}")
    if is_simulated_run(run):
        raise ValueError(
            "cannot follow up a simulated fixture run; create a real code-task instead"
        )
    spec = run.get("spec") or {}
    fctx = run.get("finalize_ctx") or {}
    parent_title = run.get("title") or "this app"
    root = fctx.get("root_run_id") or run["id"]
    child_fctx = {
        "root_run_id": root,
        "parent_run_id": parent_run_id,
        "is_followup": True,
        "requested_by": actor,
    }
    # Where the outcome BEGAN, carried forward. root_run_id says which attempts are the same
    # work; origin says which panel or dossier it came from. A follow-up that keeps the first
    # and drops the second becomes an orphan on screen -- one app reading as several, which is
    # the state this linkage exists to remove.
    if fctx.get("origin"):
        child_fctx["origin"] = fctx["origin"]
    # WHICH APP this is. Assigned once and inherited by every retry and follow-up, so all the
    # attempts at one app group together — and a deliberate rebuild from scratch, which starts
    # a NEW app_id, correctly does not. Grouping on the title instead was a heuristic that got
    # this exact case wrong: a fresh build of the same app folded into the old one, because a
    # name is not an identity.
    child_fctx["app_id"] = fctx.get("app_id") or fctx.get("root_run_id") or parent_run_id
    # Official canaries must stay official across follow-up. Dropping
    # qa_benchmark made /report 404 and broke the 5/5 lineage.
    if fctx.get("official"):
        child_fctx["official"] = True
    bench = fctx.get("qa_benchmark")
    if isinstance(bench, dict) and bench:
        try:
            from .benchmark_apps import hydrate_official_benchmark

            child_fctx["qa_benchmark"] = hydrate_official_benchmark(bench)
        except Exception:  # noqa: BLE001 — catalog miss must not drop lineage
            child_fctx["qa_benchmark"] = dict(bench)
    parent_design = (run.get("state") or {}).get("design")
    if isinstance(parent_design, dict) and str(parent_design.get("plan") or "").strip():
        child_fctx["inherited_design"] = dict(parent_design)
    model_config = spec.get("model_config")
    engine_name = str(engine or "").strip().lower()
    if engine_name:
        from .spend_cap_failover import OFFICIAL_ENGINES, pin_engine_model_config

        if engine_name not in OFFICIAL_ENGINES:
            raise ValueError(
                f"unknown engine: {engine_name!r} (expected openai|native|claude_code)"
            )
        model_config = pin_engine_model_config(engine_name, model_config)
        if isinstance(child_fctx.get("qa_benchmark"), dict):
            child_fctx["qa_benchmark"]["engine"] = engine_name

    run_id = start_code_task(
        connection,
        project_id=run["project_id"],
        task_id=run.get("task_id"),
        title=(title or f"Change to {parent_title}")[:160],
        instructions=(
            "Continue work on an EXISTING application. Its code is already on this branch: "
            "read it first and build on top of it.\n\n"
            "Do not rebuild the app, restructure it, or restyle it. Change what is asked for "
            "and leave the rest as it is -- someone is already using this.\n\n"
            f"REQUESTED CHANGE:\n{instructions}"),
        acceptance_criteria=acceptance_criteria or [
            "The requested change works in the running app.",
            "Existing behaviour and appearance are unchanged except where the change requires it.",
            "Existing tests still pass.",
        ],
        test_commands=test_commands or spec.get("test_commands") or [],
        owner=spec.get("owner"), repo=spec.get("repo"),
        # THE point of this function: start from the app's own branch, not from base.
        base_branch=work_branch_for(run),
        finalize_ctx=child_fctx,
        jobs=jobs, profile=profile or spec.get("profile"),
        risk_tier=int(spec.get("risk_tier") or 1),
        model_config=model_config,
        repair_retries=spec.get("repair_retries"),
        review_panel_size=review_panel_size if isinstance(review_panel_size, int)
        else spec.get("review_panel_size"),
    )
    official = bool(child_fctx.get("official") or child_fctx.get("qa_benchmark"))
    swarm = None
    try:
        from .agent_process import bind_captain_swarm

        settings = getattr(jobs, "settings", None)
        if settings is not None:
            swarm = bind_captain_swarm(
                connection,
                settings=settings,
                project_id=run["project_id"],
                actor=actor,
                prd=instructions,
                title=f"Captain swarm — {title or parent_title}",
                attach_run_id=run_id,
                official=official,
                engine=engine_name or None,
                review_panel_size=(
                    review_panel_size if isinstance(review_panel_size, int)
                    else (5 if official else None)
                ),
                qa_benchmark=child_fctx.get("qa_benchmark")
                if isinstance(child_fctx.get("qa_benchmark"), dict) else None,
            )
    except Exception:  # noqa: BLE001 — swarm bind must not drop the factory job
        swarm = None
    return {"run_id": run_id, "root_run_id": root,
            "base_branch": work_branch_for(run),
            "owner": spec.get("owner"), "repo": spec.get("repo"),
            "process_run_id": (swarm or {}).get("id") if isinstance(swarm, dict) else None}


def _is_system_failure(error) -> bool:
    """Thin re-export so the retry can classify without importing the whole pipeline."""
    from .governed_code_task_service import is_system_failure
    return is_system_failure(error)


def _prior_failing_gates(fctx_prev: dict, state: dict) -> list[str]:
    """Which gates the previous attempt failed — from whichever path recorded them.

    Three different code paths write this under three different keys, and a retry that reads
    only one of them silently falls back to "re-run everything":
      - `failing_gates`           — a run parked at awaiting_gate_override
      - `advisory_failing_gates`  — an advisory run that shipped its PR with findings (the
                                    normal case now that lean/balanced never block)
      - `dissent.unresolved_gates`— repair rounds spent, shipped with dissent recorded
    """
    for src in (fctx_prev.get("failing_gates"),
                fctx_prev.get("advisory_failing_gates"),
                (fctx_prev.get("unresolved_dissent") or {}).get("unresolved_gates"),
                (state.get("unresolved_dissent") or {}).get("unresolved_gates")):
        if isinstance(src, list) and src:
            return [str(g) for g in src]
    return []


def _carry_open_comments(fctx_prev: dict) -> list:
    """The unresolved review comments from the previous attempt, for the next one."""
    return [c for c in (fctx_prev.get("review_comments") or [])
            if isinstance(c, dict) and (c.get("status") or "open") == "open"]


class CancelRefused(ValueError):
    """The run already ended: there is nothing left to stop."""


TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled", "simulated")


def cancel_run(connection, run_id: str, project_id: str, *, actor: str,
               reason: str = "") -> dict:
    """STOP A BUILD: the run AND its queued job together (cancelling one leaves a zombie that
    shows as running forever), its open stage acts closed as skipped, and who stopped it at
    which phase recorded on the run so every surface says `Stopped by <actor>`.

    INSTANT, WHATEVER THE WORKER IS DOING (factory test D1). The worker holds the run row while
    it checks; a Stop that waited on that lock spun for 30s+ and said nothing. So the request is
    recorded first in its own row (code_task_stop_requests), which every reader overlays
    (`overlay_stop_requests`), and the run is finalized at once only when its rows are free
    (NOWAIT). When they are not, the answer is `stopping` and the worker honours the request at
    its next stage boundary (`honour_stop_request`).

    The one path for the console (POST /v1/code-tasks/{id}/cancel) and MCP cancel_code_task.
    Raises LookupError when there is no such run in the project, CancelRefused when it ended."""
    row = connection.execute(
        sql("SELECT id, status, phase FROM code_task_runs WHERE id=:id AND project_id=:p"),
        {"id": run_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise LookupError(f"no code task run {run_id}")
    if row["status"] in TERMINAL_RUN_STATUSES:
        raise CancelRefused(f"run is already terminal: {row['status']}")
    reason = (reason or "").strip() or f"Stopped by {actor}"
    recorded = connection.execute(
        sql("INSERT INTO code_task_stop_requests (run_id, project_id, actor, reason, phase) "
            "VALUES (:id, :p, :a, :r, :ph) ON CONFLICT (run_id) DO NOTHING RETURNING run_id"),
        {"id": run_id, "p": project_id, "a": actor, "r": reason[:2000],
         "ph": row.get("phase") or ""},
    ).scalar()
    if not recorded:
        raise CancelRefused("this build is already stopping")
    try:
        with connection.begin_nested():
            # Take the rows only if nobody holds them; never queue behind the worker.
            connection.execute(sql("SELECT id FROM code_task_runs WHERE id=:id FOR UPDATE NOWAIT"),
                               {"id": run_id})
            connection.execute(
                sql("SELECT id FROM jobs WHERE project_id=:p AND payload::text LIKE :pat "
                    "AND status NOT IN ('succeeded','cancelled','deadlettered','failed') "
                    "FOR UPDATE NOWAIT"),
                {"p": project_id, "pat": f"%{run_id}%"})
            return _finalize_cancel(connection, run_id, project_id, actor=actor, reason=reason,
                                    phase=row.get("phase"))
    except Exception as exc:  # noqa: BLE001 — the driver's lock error (55P03), nothing else
        if not _lock_not_available(exc):
            raise
    emit_event(connection, project_id, "code_task.stop_requested", actor, "code_task_run",
               run_id, {"reason": reason, "phase": row.get("phase")})
    return {"ok": True, "run_id": run_id, "status": "stopping", "stopped_by": actor,
            "jobs_cancelled": [], "reason": reason}


def _lock_not_available(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == "55P03" or "lock not available" in str(exc).lower()


def _finalize_cancel(connection, run_id: str, project_id: str, *, actor: str, reason: str,
                     phase: Optional[str]) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    connection.execute(
        sql("UPDATE code_task_runs SET status='cancelled', error=:e, updated_at=now(), "
            "finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) || jsonb_build_object("
            "'cancelled_by', CAST(:a AS text), 'cancelled_at', CAST(:t AS text), "
            "'cancelled_phase', CAST(:ph AS text)) "
            "WHERE id=:id AND project_id=:p AND status NOT IN "
            "('completed','failed','cancelled','simulated')"),
        {"id": run_id, "e": reason[:2000], "p": project_id, "a": actor, "t": stamp,
         "ph": phase or ""},
    )
    jobs = connection.execute(
        sql("SELECT id FROM jobs WHERE project_id=:p AND payload::text LIKE :pat "
            "AND status NOT IN ('succeeded','cancelled','deadlettered','failed')"),
        {"p": project_id, "pat": f"%{run_id}%"},
    ).mappings().all()
    for j in jobs:
        connection.execute(
            sql("UPDATE jobs SET status='cancelled', updated_at=now() "
                "WHERE id=:id AND project_id=:p"),
            {"id": j["id"], "p": project_id},
        )
    try:
        from . import agent_acts

        with connection.begin_nested():
            agent_acts.close_code_task(connection, run_id)
    except Exception:  # noqa: BLE001 — the stop stands even if the ledger can't say so
        logger.warning("could not close the acts of cancelled run %s", run_id, exc_info=True)
    try:
        with connection.begin_nested():
            connection.execute(
                sql("UPDATE code_task_stop_requests SET honoured_at = now() "
                    "WHERE run_id = :id AND honoured_at IS NULL"), {"id": run_id})
    except Exception:  # noqa: BLE001 — an older schema without the table still stops
        pass
    emit_event(connection, project_id, "code_task.cancelled", actor, "code_task_run",
               run_id, {"reason": reason, "phase": phase,
                        "jobs_cancelled": [j["id"] for j in jobs]})
    return {"ok": True, "run_id": run_id, "status": "cancelled", "stopped_by": actor,
            "jobs_cancelled": [j["id"] for j in jobs], "reason": reason}


def pending_stop(connection, run_id: str) -> Optional[dict]:
    """The stop somebody asked for that the run has not taken yet, or None."""
    if hasattr(connection, "get_run"):
        return None
    try:
        with connection.begin_nested():
            row = connection.execute(
                sql("SELECT run_id, project_id, actor, reason, phase, requested_at "
                    "FROM code_task_stop_requests WHERE run_id = :id AND honoured_at IS NULL"),
                {"id": run_id}).mappings().first()
    except Exception:  # noqa: BLE001 — no table on an older schema: nothing pending
        return None
    return dict(row) if row else None


def honour_stop_request(connection, run_id: str) -> Optional[dict]:
    """The worker, at a stage boundary with no lock of its own on the run: finish the stop."""
    req = pending_stop(connection, run_id)
    if not req:
        return None
    return _finalize_cancel(connection, run_id, str(req["project_id"]), actor=str(req["actor"]),
                            reason=str(req["reason"] or f"Stopped by {req['actor']}"),
                            phase=req.get("phase"))


def overlay_stop_requests(connection, runs: list[dict]) -> list[dict]:
    """Every reader says `Stopped by <actor>` the moment Stop is pressed, not when the worker
    lets go of the run row: a pending request reads as the cancelled run it is becoming."""
    live = [r for r in runs if r and r.get("status") not in TERMINAL_RUN_STATUSES]
    if not live or hasattr(connection, "get_run"):
        return runs
    try:
        with connection.begin_nested():
            rows = connection.execute(
                sql("SELECT run_id, actor, phase, requested_at FROM code_task_stop_requests "
                    "WHERE run_id = ANY(:ids) AND honoured_at IS NULL"),
                {"ids": [str(r["id"]) for r in live]}).mappings().all()
    except Exception:  # noqa: BLE001
        return runs
    by_id = {str(r["run_id"]): r for r in rows}
    for run in live:
        req = by_id.get(str(run["id"]))
        if not req:
            continue
        fctx = dict(run.get("finalize_ctx") or {})
        fctx.setdefault("cancelled_by", req["actor"])
        fctx.setdefault("cancelled_at", req["requested_at"].isoformat()
                        if hasattr(req["requested_at"], "isoformat") else str(req["requested_at"]))
        fctx.setdefault("cancelled_phase", req.get("phase") or run.get("phase") or "")
        fctx["stopping"] = True
        run["finalize_ctx"] = fctx
        run["status"] = "cancelled"
    return runs


def _comment_key(c: dict) -> tuple:
    return (c.get("stage"), c.get("path"), c.get("line"), c.get("severity"),
            (c.get("body") or "")[:120])


def review_comments_of(run: dict) -> list[dict]:
    """Every review comment on a build, from the panel and from people, each once.

    Stored comments (recorded as the review happens) first; findings still only in the run's
    evidence (`state`, where the pipeline writes it — never `finalize_ctx.evidence`, which
    nothing writes) are parsed and added when no stored comment already says the same."""
    from .review_comments import parse_findings

    fctx = run.get("finalize_ctx") or {}
    stored = [dict(c) for c in (fctx.get("review_comments") or []) if isinstance(c, dict)]
    seen = {_comment_key(c) for c in stored}
    evidence = run.get("state") or {}
    for stage, key in (("review_code", "review_summary"),
                       ("security_check", "security_summary"),
                       ("consistency_check", "consistency_summary")):
        for parsed in parse_findings(evidence.get(key) or "", stage=stage):
            entry = {"id": None, **parsed.to_dict()}
            if _comment_key(entry) in seen:
                continue
            seen.add(_comment_key(entry))
            stored.append(entry)
    return stored


def resolve_review_comment(connection, run_id: str, project_id: str, comment_id: str, *,
                           status: str, actor: str, note: str = "") -> dict:
    """Mark one stored comment addressed or dismissed. A dismissal is recorded, not erased."""
    status = (status or "").lower()
    if status not in ("addressed", "dismissed"):
        raise ValueError("status must be addressed or dismissed")
    row = connection.execute(
        sql("SELECT finalize_ctx FROM code_task_runs WHERE id=:id AND project_id=:p"),
        {"id": run_id, "p": project_id},
    ).mappings().first()
    if row is None:
        raise LookupError(f"no code task run {run_id}")
    fctx = row["finalize_ctx"] or {}
    if isinstance(fctx, str):
        fctx = json.loads(fctx)
    comments = list(fctx.get("review_comments") or [])
    hit = None
    for entry in comments:
        if entry.get("id") == comment_id:
            entry["status"] = status
            entry["resolved_by"] = actor
            if note:
                entry["resolution_note"] = str(note)[:500]
            hit = entry
    if hit is None:
        raise LookupError(f"no review comment {comment_id} on {run_id}")
    connection.execute(
        sql("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
            "|| jsonb_build_object('review_comments', CAST(:e AS JSONB)), "
            "updated_at=now() WHERE id=:id AND project_id=:p"),
        {"e": json.dumps(comments), "id": run_id, "p": project_id})
    return {"ok": True, "run_id": run_id, "comment_id": comment_id, "status": status}


class RetryRefused(ValueError):
    """A retry that would spend a governed attempt for nothing. The message says what to do."""


def refuse_finished_retry(prev: dict, fix_note: str = "") -> None:
    """Refuse a blind retry of a build that finished cleanly.

    The Factory offered "Retry build" on every run that was not running, completed ones
    included, with a tooltip saying it "resumes from where it failed" — and nothing here
    refused, so one click spent a new governed attempt ($4-15 by this module's own estimate)
    re-doing finished work. A completed run is still retryable when its gates recorded a
    failure (an advisory-gate repair), and extend_code_task passes the change it wants as
    `fix_note`, so both keep working.
    """
    if str(prev.get("status") or "") != "completed" or str(fix_note or "").strip():
        return
    fctx = prev.get("finalize_ctx") or {}
    if fctx.get("advisory_failing_gates") or fctx.get("failing_gates"):
        return
    raise RetryRefused(
        "This build already completed and passed its gates, so a retry would spend a new "
        "governed attempt re-doing finished work. To change the app, use 'Change this app' "
        "(extend_code_task); to rebuild anyway, retry with a note saying why."
    )


def retry_code_task(
    engine, settings: Settings, jobs, run_id: str, *, fix_note: str = "",
    profile: Optional[str] = None, repair_retries: Optional[int] = None,
    allow_test_retirement: bool = False, job_priority: Optional[int] = None,
    attempt_ceiling: Optional[int] = None, workflow_version_id: str = "",
) -> dict:
    """Resume a failed/blocked code task as a NEW attempt in the same lineage — carrying the prior
    attempt's findings forward as explicit feedback so the agent fixes the identified issues instead
    of regenerating blind. History is preserved: each attempt links to its parent + root run, and
    accumulates the findings it must address."""
    with _transaction(engine) as c:
        prev = load_code_task_run(c, run_id)
    if prev is None:
        raise ValueError(f"code task run not found: {run_id}")
    if is_simulated_run(prev):
        raise ValueError(
            "cannot retry a simulated fixture run; create a real code-task instead"
        )
    spec = prev.get("spec") or {}
    state = prev.get("state") or {}
    fctx_prev = prev.get("finalize_ctx") or {}
    attempt = int(fctx_prev.get("attempt", 1)) + 1
    root = fctx_prev.get("root_run_id") or run_id
    refuse_finished_retry(prev, fix_note)

    # ---- Failure-loop guardrails (release blocker fix) -----------------------------------
    # An `infra` failure is NOT the task's fault, so re-running the same task on the same
    # broken substrate is a blind loop. Two mechanisms:
    #   1. Substrate fallback: previous attempt died `infra` on the micro-VM substrate ->
    #      this attempt runs the working stages on the LOCAL substrate (degraded mode, flagged).
    #   2. Circuit breaker: already on local and still failing `infra` repeatedly -> refuse the
    #      blind retry with a diagnosis instead of burning another expensive attempt.
    # A CEILING THE CALLER CANNOT TALK ITS WAY PAST.
    #
    # The `not fix_note` escape below exists so a HUMAN can say "I know, proceed". The shepherd
    # passes fix_note on every single retry, so for automated repair that condition is always
    # False and the breaker under it can never fire. Worse, the ledger that is supposed to stop
    # a remedy repeating keys on `task_ref` — and a retry mints a NEW run id, so each child
    # starts with an empty ledger. Bounded per instance, unbounded per lineage; at $4-15 a link.
    #
    # `attempt` is carried in finalize_ctx across the whole lineage and cannot be reset by
    # minting a new run, which is exactly why the bound belongs on it.
    if attempt_ceiling is not None and attempt > attempt_ceiling:
        raise ValueError(
            f"retry ceiling reached: this lineage (root {root}) has already made "
            f"{attempt - 1} attempts and the caller's ceiling is {attempt_ceiling}. "
            f"Last error: {str(prev.get('error') or 'none recorded')[:200]!r}. "
            "Repeating it will not change the outcome — diagnose the cause, or retry "
            "explicitly with a higher ceiling."
        )

    prev_infra = (fctx_prev.get("failure_class") == "infra")
    prev_substrate = (fctx_prev.get("execution_substrate") or "").lower()
    substrate_override = spec.get("substrate")
    substrate_fallback_note = ""
    if prev_infra and prev_substrate == "microvm":
        substrate_override = "local"
        substrate_fallback_note = (
            "NOTE: the previous attempt failed with an infrastructure error on the micro-VM "
            "substrate; this attempt runs on the local substrate (degraded mode).")
    elif prev_infra and prev_substrate == "local_subprocess" and attempt >= 3 and not fix_note:
        raise ValueError(
            f"circuit breaker: {attempt - 1} attempts in this lineage have failed with "
            f"infrastructure errors (last: {str(prev.get('error') or '')[:200]!r}) and the run is "
            "already on the local substrate. A blind retry will fail the same way — fix the "
            "platform issue (see the error above) or pass an explicit fix_note to proceed.")

    work_branch = f"boss/code/{root}"
    pr_url = prev.get("pull_request_url")

    # REFRESH THE BRANCH FROM ITS BASE BEFORE THE AGENT SEES IT.
    # A lineage branch is cut once and then reused for every attempt, so it keeps showing the
    # agent the world as it was when the branch was created. Concretely: PR #66 merged
    # `web-stocks` into main, then an extend asked the agent to consolidate the two stock
    # apps — and it could not, because its working tree never contained web-stocks. It saw one
    # app and correctly left one app.
    #
    # The merge path already knows how to do this (update_pull_request_branch, added for the
    # 405-on-stale-base case). The gap was that nothing refreshed a branch before WORK, only
    # before merging. Best-effort: a refusal here means a real conflict, which the agent is
    # about to be asked to resolve anyway, and a failure must never block the retry.
    if pr_url:
        try:
            from .software_factory import SoftwareFactoryService

            sf = SoftwareFactoryService(settings)
            pr_number = int(str(pr_url).rstrip("/").rsplit("/", 1)[-1])
            if hasattr(sf.github, "update_pull_request_branch"):
                refreshed = sf.github.update_pull_request_branch(pr_number)
                with _transaction(engine) as c:
                    emit_event(
                        c, prev["project_id"],
                        "code_task.branch_refreshed" if refreshed
                        else "code_task.branch_refresh_declined",
                        "boss", "code_task_run", run_id,
                        {"branch": work_branch, "pull_request": pr_number,
                         "why": ("merged the base in so the agent works against current main"
                                 if refreshed else
                                 "base could not be merged cleanly; the agent will see the "
                                 "branch as-is and may need to resolve conflicts")})
        except Exception:  # noqa: BLE001 — never block a retry on a refresh
            pass

    # Prior adversarial findings — only meaningful when the previous attempt did NOT
    # complete (a completed attempt's reviews passed; carrying them as "findings" would
    # be misleading — for a completed task the operator's feedback drives the iteration).
    findings = []
    if prev.get("status") != "completed":
        if prev.get("error"):
            findings.append(f"Outcome of previous attempt: {prev['error']}")
        for label, key in (("Security review", "security_summary"),
                           ("Code review", "review_summary"),
                           ("Consistency review", "consistency_summary")):
            if state.get(key):
                findings.append(f"## {label} findings (previous attempt)\n{state[key]}")

    # The KEY context is that the prior attempt's code is ALREADY in the working tree
    # (the governed run clones the iteration branch) — so the model iterates on real
    # code instead of regenerating from scratch. Feedback drives WHAT to change.
    fb = [
        f"You are ITERATING on this task (attempt {attempt}). The previous attempt's code is "
        f"ALREADY PRESENT in your working tree on branch `{work_branch}`"
        + (f" (open PR: {pr_url})" if pr_url else "")
        + ". DO NOT rebuild from scratch — modify the existing code to address the feedback below "
        "and preserve everything that already works."
    ]
    if fix_note:
        fb.append(f"## Feedback to address (from the operator)\n{fix_note}")
    if findings:
        fb.append("\n\n".join(findings))
    if not fix_note and not findings:
        fb.append("(No specific feedback was provided — review the code for correctness, fix any "
                  "bugs, and improve robustness.)")
    feedback_block = "\n\n".join(fb)

    # Keep the prompt bounded across repeated iterations: strip the deployability note
    # and any prior ITERATION-FEEDBACK block, then re-append the latest. Full history is
    # retained in finalize_ctx.feedback_history, and the prior CODE lives on the branch.
    base_instr = (spec.get("instructions") or "")
    base_instr = base_instr.split("\n\n=== ITERATION FEEDBACK")[0]
    base_instr = base_instr.split(_DEPLOYABILITY_NOTE)[0]
    augmented = f"{base_instr}\n\n=== ITERATION FEEDBACK (attempt {attempt}) ===\n{feedback_block}"

    base_title = (prev.get("title") or "Code task").split(" · retry")[0]
    title = f"{base_title} · retry {attempt - 1}"

    history = list(fctx_prev.get("feedback_history") or [])
    history.append({
        "attempt": attempt, "parent_run_id": run_id, "prev_status": prev.get("status"),
        "fix_note": fix_note or "", "at": datetime.now(timezone.utc).isoformat(),
        **({"substrate_fallback": substrate_fallback_note} if substrate_fallback_note else {}),
    })

    with _transaction(engine) as c:
        new_id = start_code_task(
            c, project_id=prev["project_id"], task_id=prev.get("task_id"), title=title,
            instructions=augmented,
            acceptance_criteria=spec.get("acceptance_criteria") or [],
            test_commands=spec.get("test_commands") or [],
            owner=spec.get("owner") or settings.target_github_owner,
            repo=spec.get("repo") or settings.target_github_repo,
            base_branch=spec.get("base_branch") or "main",
            role_key=spec.get("role_key"),
            finalize_ctx={"parent_run_id": run_id, "root_run_id": root, "attempt": attempt,
                          # AN EXPLICIT CHOICE MAKES THIS A PIECE OF WORK IN ITS OWN RIGHT.
                          # A retry inherits its parent's run; a change the operator chose a
                          # workflow for is a new piece of work following that path.
                          **({"workflow_version_id": workflow_version_id}
                             if workflow_version_id else {}),
                          "feedback_history": history,
                          # UNRESOLVED REVIEW COMMENTS FOLLOW THE WORK, NOT THE ATTEMPT.
                          #
                          # Comments are stored per run, and a retry mints a NEW run — so a
                          # comment left on a finished attempt reached nothing, and the run
                          # that would have acted on it started without it. That is the only
                          # moment a person leaves one: they read the PR, they point at a
                          # line, they press retry. Addressed and dismissed comments stay
                          # behind; carrying those forward would re-litigate decisions the
                          # last attempt already made.
                          **({"review_comments": _carry_open_comments(fctx_prev)}
                             if _carry_open_comments(fctx_prev) else {}),
                          # Carried across every attempt: a retry is the same outcome, and a
                          # console that loses that shows one app as several.
                          **({"origin": fctx_prev["origin"]} if fctx_prev.get("origin") else {}),
                          # Same app across every attempt; see start_followup_task.
                          "app_id": (fctx_prev.get("app_id") or fctx_prev.get("root_run_id")
                                     or run_id),
                          **({"official": True} if fctx_prev.get("official") else {}),
                          **({"qa_benchmark": dict(fctx_prev["qa_benchmark"])}
                             if isinstance(fctx_prev.get("qa_benchmark"), dict)
                             and fctx_prev.get("qa_benchmark") else {}),
                          # Carry the web-app treatment across retries — else the retry loses
                          # web_app_build, infer_risk_tier re-derives tier 2, and it runs the full
                          # design+write_tests+blocking-review pipeline (wrong for a scaffolded app,
                          # and loops on repair). Keep it a lean advisory web build on every attempt.
                          **({"web_app_build": True} if fctx_prev.get("web_app_build") else {}),
                          # RESUME: hand the parent's approved design to the next attempt so it
                          # skips standards+spec+plan+review_plan instead of re-deriving a plan
                          # for a task that has not changed. Guarded on a real plan EXISTING,
                          # not on `plan_ok` — that flag is a "REVISE" substring check and is
                          # false even on runs that shipped clean, so it would disable resume.
                          **({"inherited_design": state["design"]}
                             if isinstance(state.get("design"), dict)
                             and (state["design"].get("plan") or "").strip() else {}),
                          # RESUME FROM WHERE IT FAILED. Which gates failed says which stages
                          # are actually stale: a run rejected for `code_review_passed` has a
                          # perfectly good test suite sitting on the branch, and re-authoring it
                          # costs a full agent session (measured: 24 turns / 8.6 min) to arrive
                          # at the same tests. Carried as data so the child can skip stages
                          # instead of replaying the pipeline from the top.
                          **({"prior_failing_gates": _prior_failing_gates(fctx_prev, state)}
                             if _prior_failing_gates(fctx_prev, state) else {}),
                          # Carry the parent's error so the child can tell a PLATFORM death
                          # (spend cap, timeout) from a code verdict. The former must resume
                          # onto the checkpointed branch, not re-author what already exists.
                          **({"prior_error": str(prev.get("error"))[:400]}
                             if prev.get("error") else {}),
                          # RESUME POINT. Which stages the dead attempt actually finished.
                          # Carried ONLY when the harness died (spend cap, timeout, lease) —
                          # after a real code verdict the stages must re-run, because their
                          # output is what was judged wanting.
                          **({"resume_completed_stages":
                              list((fctx_prev.get("completed_stages") or []))}
                             if (fctx_prev.get("completed_stages")
                                 and _is_system_failure(prev.get("error"))) else {})},
            jobs=jobs, profile=profile or spec.get("profile"), risk_tier=spec.get("risk_tier"),
            model_config=spec.get("model_config"), open_pr=spec.get("open_pr"),
            repair_retries=(repair_retries if isinstance(repair_retries, int)
                            else spec.get("repair_retries")),
            substrate=substrate_override,
            job_priority=job_priority,
        )
        if allow_test_retirement:
            # Declared on the RUN's spec so the gate can read it. Kept out of start_code_task's
            # signature: this is an exception a caller asks for, not a property of every task.
            c.execute(sql("UPDATE code_task_runs SET spec = COALESCE(spec,'{}'::jsonb) "
                          "|| jsonb_build_object('allow_test_retirement', true) WHERE id=:i"),
                      {"i": new_id})
    return {"status": "queued", "run_id": new_id, "attempt": attempt,
            "parent_run_id": run_id, "root_run_id": root}


def override_code_task(engine, settings: Settings, jobs, run_id: str, *,
                       waive_gates: list[str], actor: str = "operator", reason: str = "") -> dict:
    """Human gate override: waive the named failed gate(s) and open the PR from the diff that was
    preserved at gate-failure time — NO re-run. Gates the human does NOT waive still block, and the
    opened PR keeps its own merge-gate Decision (this opens, it does not merge)."""
    from ..schemas import FileChange
    with _transaction(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    if run.get("pull_request_url"):
        return {"status": "completed", "pull_request_url": run["pull_request_url"], "already_open": True}
    fctx = run.get("finalize_ctx") or {}
    failing = fctx.get("failing_gates") or []
    pending = fctx.get("pending_diff")
    if not pending:
        raise ValueError("no preserved diff — only runs blocked at 'awaiting_gate_override' can be overridden.")
    waive = list(waive_gates or [])
    if "*" in waive or "all" in waive:
        waive = list(failing)
    from .factory_view import UNWAIVABLE_GATES
    unwaivable = [g for g in waive if g in UNWAIVABLE_GATES] + [
        g for g in failing if g in UNWAIVABLE_GATES and g not in waive]
    if unwaivable:
        # A PR for an app that does not start is not a PR anyone can review (factory §4.2.1).
        raise ValueError("refused: can't be waived: the app must start "
                         f"({', '.join(sorted(set(unwaivable)))})")
    not_waived = [g for g in failing if g not in waive]
    if not_waived:
        raise ValueError(f"refused: failing gates not waived: {not_waived}. "
                         "Waive them explicitly to proceed (or leave them to keep blocking).")
    waive_gates = waive
    files = [FileChange(path=f["path"], content=f["content"]) for f in pending]
    ports = _engine_ports()
    budget = ports.budget
    sf = SoftwareFactoryService(settings, ports=ports)
    project_id = run["project_id"]
    task_id = run.get("task_id")
    qg = fctx.get("quality_gate_id")
    with _transaction(engine) as c:
        reservation = budget.reserve(c, project_id, category="code_task",
            amount_usd_micros=200_000, reason=f"gate-override PR {run_id[:12]}", expires_in_minutes=15)
        _set_phase(c, run_id, "opening_pr")
        # Use the root-run branch so a waived iteration lands on the SAME branch/PR the
        # iteration chain has been accumulating (create_pull_request reuses the open PR).
        root_run = fctx.get("root_run_id") or run_id
        proposal = sf.propose_pull_request(
            c, project_id, task_id, reservation_id=reservation.get("reservation_id"),
            dedupe_key=f"code:task:{run_id}", title=run["title"],
            branch_name=f"boss/code/{root_run}",
            summary=(f"⚠️ GATE OVERRIDE by {actor}: waived {waive_gates} (QualityGate {qg}).\n"
                     f"Reason: {reason or '(none given)'}\n\n{fctx.get('pr_summary', '')}"),
            files=files, created_by_job_id=fctx.get("created_by_job_id"))
    status = proposal.get("status")
    # `reused`: this build's PR effect already happened (a prior receipt) — the PR is open.
    if status not in ("created", "succeeded", "reused"):
        # No PR opened: the build is still waiting on the operator's waiver. Leaving it parked
        # at `opening_pr` made it read Blocked with nothing to press (the card was gone).
        with _transaction(engine) as c:
            _set_phase(c, run_id, "awaiting_gate_override")
        # `status` last: the proposal's own status ("failed") used to overwrite "error".
        return {**proposal, "status": "error", "detail": f"PR not created: {status}"}
    with _transaction(engine) as c:
        override_error = f"Gate {qg} OVERRIDDEN by {actor}: waived {waive_gates}."
        if hasattr(c, "save_run"):
            c.save_run(
                run_id,
                {
                    "status": "completed",
                    "phase": "done",
                    "pull_request_url": proposal.get("pull_request_url"),
                    "pull_request_number": proposal.get("pull_request_number"),
                    "decision_id": proposal.get("decision_id"),
                    "error": override_error,
                },
            )
        else:
            c.execute(
                sql(
                    "UPDATE code_task_runs SET status='completed', phase='done', "
                    "pull_request_url=:u, pull_request_number=:n, decision_id=:d, "
                    "error=:e, updated_at=now() WHERE id=:id"
                ),
                {
                    "u": proposal.get("pull_request_url"),
                    "n": proposal.get("pull_request_number"),
                    "d": proposal.get("decision_id"),
                    "e": override_error,
                    "id": run_id,
                },
            )
        if not hasattr(c, "save_run"):
            # Who waived what, so Checks says `Waived by <actor>` (factory spec §4.5.5).
            c.execute(sql(
                "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
                "|| jsonb_build_object('waived', CAST(:w AS JSONB)) WHERE id=:id"),
                {"w": json.dumps({"gates": list(waive_gates), "by": actor, "reason": reason,
                                  "at": datetime.now(timezone.utc).isoformat()}),
                 "id": run_id})
        emit_event(c, project_id, "code_task.gate_overridden", actor, "code_task_run", run_id,
                   {"waived_gates": waive_gates, "reason": reason, "quality_gate_id": qg,
                    "pull_request_url": proposal.get("pull_request_url")})
        # VAG #107 ratchet: a human overriding the gate means the gate was wrong to block —
        # capture the (now-blessed) implementation as a CLEAN corpus item so the verifier is
        # measured against this false-fail forever after. Never let it break the override.
        if not hasattr(c, "save_run"):
            try:
                from .verifier_ratchet import tag_run
                tag_run(c, run_id, label="clean", source="gate_override",
                        files=pending, reason=reason or f"override waived {waive_gates}")
            except Exception:  # noqa: BLE001
                logger.exception("ratchet capture failed for override %s", run_id)
    return {"status": "completed", "run_id": run_id, "waived_gates": waive_gates,
            "pull_request_url": proposal.get("pull_request_url"),
            "pull_request_number": proposal.get("pull_request_number"),
            "decision_id": proposal.get("decision_id")}


def _release(
    budget, connection, project_id: str, reservation_id: Optional[str], reason: str,
    run_id: Optional[str] = None,
) -> None:
    """Settle-then-release (BOSS-280): money already burned on model calls is REAL even when
    the run failed — sum the run's llm_calls and settle it as an actual cost receipt, so the
    budget ledger reconciles with true LLM spend instead of silently dropping every failed
    run's cost. Only a run with zero recorded spend releases cleanly. Always fail-soft.

    Settles the *unsettled* delta only: plan-approval pauses (and any earlier reservation for
    the same run) already wrote cost_receipts under skill_or_model_id ``code_task:{run_id}``,
    so a later settle on resume must not re-bill that spend or the ledger double-counts."""
    if not reservation_id:
        return
    try:
        actual = 0
        if run_id and hasattr(connection, "execute"):
            gross = int(connection.execute(
                # Money only: a call a subscription or local model answered is worth its
                # cost_usd_micros at list price but costs nothing (included_spend.py).
                sql("SELECT COALESCE(SUM(cost_usd_micros), 0) FROM llm_calls "
                    "WHERE code_task_run_id = :r AND NOT included_in_subscription"),
                {"r": run_id}).scalar() or 0)
            already = int(connection.execute(
                sql("SELECT COALESCE(SUM(actual_usd_micros), 0) FROM cost_receipts "
                    "WHERE project_id = :p AND skill_or_model_id = :sid"),
                {"p": project_id, "sid": f"code_task:{run_id}"}).scalar() or 0)
            actual = max(0, gross - already)
        if actual > 0:
            # consume() requires a governance receipt (every settled effect gets one).
            from .receipts import ReceiptService
            scope = connection.execute(
                sql("SELECT p.workspace_id, w.org_id FROM projects p "
                    "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"),
                {"p": project_id}).mappings().first()
            receipt, _created = ReceiptService().create_or_reuse(
                connection, scope["org_id"], scope["workspace_id"], project_id,
                dedupe_key=f"budget:settle:{run_id}:{reservation_id}",
                effect_kind="budget.llm_spend.settle",
                effect_scope={"run_id": run_id, "reservation_id": reservation_id},
                payload={"actual_usd_micros": actual, "reason": reason},
            )
            budget.consume(
                connection, project_id, reservation_id,
                receipt_id=receipt["id"], category="code_task",
                skill_or_model_id=f"code_task:{run_id}",
                estimated_usd_micros=actual, actual_usd_micros=actual,
                actor="budget_reconciler",
            )
        else:
            budget.release(connection, project_id, reservation_id, reason)
    except Exception:
        try:
            budget.release(connection, project_id, reservation_id, reason)
        except Exception:
            pass


def _set_phase(connection, run_id: str, phase: str) -> None:
    # Record how long the phase we're LEAVING took, then start the clock on the new
    # one. Timings live in finalize_ctx (merged with ||, so the mid-run state overwrite
    # can't clobber them). Lets us see where a code task spends its time — especially
    # the big 'coding' phase (the whole inner agent loop). Atomic in one statement.
    if hasattr(connection, "save_run"):
        run = connection.get_run(run_id) or {}
        finalize_ctx = dict(run.get("finalize_ctx") or {})
        finalize_ctx["phase_started_at"] = datetime.now(timezone.utc).isoformat()
        connection.save_run(
            run_id, {"phase": phase, "finalize_ctx": finalize_ctx}
        )
        return
    connection.execute(
        sql(
            """
            UPDATE code_task_runs SET
              finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb)
                || jsonb_build_object('phase_started_at', to_jsonb(now()))
                || CASE
                     WHEN finalize_ctx ? 'phase_started_at' AND phase IS NOT NULL THEN
                       jsonb_build_object(
                         'phase_timings',
                         COALESCE(finalize_ctx->'phase_timings', '[]'::jsonb)
                         || jsonb_build_array(jsonb_build_object(
                              'phase', phase,
                              'ms', GREATEST(0, EXTRACT(EPOCH FROM (
                                      now() - (finalize_ctx->>'phase_started_at')::timestamptz)) * 1000)::bigint
                            ))
                       )
                     ELSE '{}'::jsonb
                   END,
              phase = :ph,
              updated_at = now()
            WHERE id = :id
            """
        ),
        {"ph": phase, "id": run_id},
    )


def _persist_sandbox_id(connection, run_id: str, sbx_id: Optional[str]) -> None:
    """Record the micro-VM's sandbox-service task id on the run so the console can
    inspect the retained VM after a failure (browse /work/repo, view logs, hold)."""
    if not sbx_id:
        return
    if hasattr(connection, "save_run"):
        run = connection.get_run(run_id) or {}
        finalize_ctx = dict(run.get("finalize_ctx") or {})
        finalize_ctx["sandbox_task_id"] = sbx_id
        connection.save_run(run_id, {"finalize_ctx": finalize_ctx})
        return
    connection.execute(
        sql("UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
             "|| jsonb_build_object('sandbox_task_id', CAST(:s AS text)), updated_at = now() "
             "WHERE id = :id"),
        {"s": sbx_id, "id": run_id},
    )


def classify_pipeline_exception(reason: str) -> tuple[str, str]:
    """Map a pipeline exception onto the failure taxonomy. Most unexpected exceptions ARE infra
    (platform's fault) — but an agent that exhausted its turn cap is a RECOVERABLE task-shaped
    failure: retrying with more turns / on the existing branch is the fix, and it must NOT trigger
    the infra substrate-fallback or circuit breaker.

    Model TPM / rate-limit / credits / budget exhaustion is needs_human: escalate to a Decision
    rather than retrying forever into the DLQ (W2.5 / W-REL-03).
    """
    r = (reason or "").lower()
    if "error_max_turns" in r or "max turns" in r or "max_turns" in r:
        return "recoverable", "max_turns_exhausted"
    # Model capacity / economics — human or capacity change required; not silent requeue.
    if any(
        sig in r
        for sig in (
            "tpm",
            "tokens per minute",
            "rate limit",
            "rate_limit",
            "ratelimit",
            "too many requests",
            "429",
            "usage limit",
            "spend limit",
            "credit",
            "quota",
            "resource exhausted",
            "overloaded",
            "throttl",
        )
    ):
        return "needs_human", "model_rate_limit"
    if any(
        sig in r
        for sig in (
            "budget reservation denied",
            "budget denied",
            "budget exhaust",
            "reservation denied",
            "insufficient budget",
        )
    ):
        return "needs_human", "budget_exhausted"
    if any(
        sig in r
        for sig in (
            "no such file or directory: 'git'",
            'no such file or directory: "git"',
            "missing git",
            "not a git repository",
        )
    ):
        return "infra", "missing_git"
    if any(
        sig in r
        for sig in (
            "claims_frozen",
            "not claimed for community ga",
            "boss_microvm_claims_frozen",
        )
    ):
        return "needs_human", "microvm_claims_frozen"
    if any(
        sig in r
        for sig in (
            "sandbox host task missing",
            "sandbox task missing",
            "sandbox not found",
            "sandbox task 404",
            "sandbox host 404",
        )
    ) or ("404" in r and "sandbox" in r):
        # Host task is gone — retry/DLQ cannot recreate it. Fail closed.
        return "needs_human", "sandbox_task_missing"
    if any(sig in r for sig in ("sandbox", "microvm", "not allowlisted", "rootfs")):
        return "infra", "sandbox_error"
    if any(
        sig in r
        for sig in (
            "github",
            "pull request",
            "installation token",
            "bad credentials",
            "api.github",
        )
    ):
        return "infra", "github_error"
    return "infra", "pipeline_exception"


def _fail(
    connection, run_id: str, project_id: str, reason: str, *, status: str = "failed",
    failure_class: str = "terminal", reason_code: str = "unknown",
) -> dict:
    """Fail a code task with a TAXONOMY so failures are legible + actionable, not opaque:

      failure_class ∈ {recoverable, terminal, needs_human, infra}
        recoverable — transient/external; a retry could succeed (provider blip, flake).
        terminal    — a genuine dead-end for this attempt (produced nothing usable).
        needs_human — a human decision is required to proceed (ambiguity, policy).
        infra       — BOSS platform/harness problem (not the task's fault).
      reason_code — a short machine code (no_diff, no_test_files, tests_failed, budget_denied,
        provider_error, exception, …) for dashboards + auto-recovery routing.

    Recorded on the run (finalize_ctx) and the event so the console/API can show WHY + WHAT."""
    if hasattr(connection, "save_run"):
        run = connection.get_run(run_id) or {}
        finalize_ctx = dict(run.get("finalize_ctx") or {})
        finalize_ctx.update(
            {"failure_class": failure_class, "reason_code": reason_code}
        )
        connection.save_run(
            run_id,
            {
                "status": status,
                "phase": "done",
                "error": reason[:2000],
                "finalize_ctx": finalize_ctx,
            },
        )
    else:
        connection.execute(
            sql(
                "UPDATE code_task_runs SET status=:st, phase='done', error=:e, "
                "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) "
                "|| jsonb_build_object('failure_class', CAST(:fc AS text), "
                "'reason_code', CAST(:rc AS text)), updated_at=now() WHERE id=:id"
            ),
            {
                "st": status,
                "e": reason[:2000],
                "fc": failure_class,
                "rc": reason_code,
                "id": run_id,
            },
        )
    emit_event(connection, project_id, "code_task.failed", "boss", "code_task_run", run_id,
               {"reason": reason[:300], "status": status,
                "failure_class": failure_class, "reason_code": reason_code})
    return {"status": status, "done": True, "error": reason,
            "failure_class": failure_class, "reason_code": reason_code}


def refused_reservation(engine, jobs, *, run_id: str, project_id: str, reservation: dict,
                        fencing_token: Optional[str]) -> dict:
    """THE ONE READING of a code task's refused budget reservation.

    It used to format the refusal from the reservation's STATUS and block every refusal as
    needing a person — "Budget reservation denied: denied", blocked as needing a person, and explained by model_failures as "Raise the
    daily or monthly cap". A code task holds $2 while it runs; started in the same minute as
    other reserved work, its refusal is the per-minute cap or other work's held headroom, and
    both clear by themselves (`transient`, `retry_after_seconds` from BudgetGovernor.reserve).

    * A refusal only a person can lift blocks the run, in the governor's own words.
    * A transient one hands the job back to the queue for `retry_after_seconds` WITHOUT
      spending an attempt (JobLeaseService.defer_leased) and leaves the run `running`; the
      worker neither completes nor fails a deferred job.
    * Transient with no lease to hand back (not run by a worker): the attempt fails retryably
      and the run is left running, so the job's own retry reserves again.
    """
    message = str(reservation.get("message") or "Budget reservation denied.")
    if not reservation.get("transient"):
        with _transaction(engine) as c:
            return _fail(c, run_id, project_id, f"The budget refused this code task: {message}",
                         status="blocked", failure_class="needs_human",
                         reason_code="budget_denied")
    wait = int(reservation.get("retry_after_seconds") or 30)
    if fencing_token and hasattr(jobs, "defer_leased"):
        import hashlib

        try:
            with _transaction(engine) as c:
                job = c.execute(
                    sql("SELECT id, lease_owner FROM jobs WHERE kind = 'code.task' "
                        "AND payload->>'run_id' = :run_id AND lease_token_hash = :h "
                        "AND status IN ('leased', 'running') LIMIT 1"),
                    {"run_id": run_id,
                     "h": hashlib.sha256(fencing_token.encode("utf-8")).hexdigest()},
                ).mappings().first()
                if job is not None:
                    jobs.defer_leased(c, job["lease_owner"], job["id"], fencing_token,
                                      delay_seconds=wait, reason="budget_wait", message=message)
                    emit_event(c, project_id, "code_task.budget_wait", "boss", "code_task_run",
                               run_id, {"message": message, "refusal": reservation.get("refusal"),
                                        "retry_after_seconds": wait, "job_id": job["id"]})
                    # THE WAIT IS VISIBLE (factory spec §6.4): the build says `Waiting · budget`
                    # from this stamp, cleared when its next stage opens — no event scan per row.
                    c.execute(sql(
                        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb)"
                        " || jsonb_build_object('budget_wait_since', COALESCE("
                        "finalize_ctx->'budget_wait_since', to_jsonb(now()))) WHERE id = :id"), {"id": run_id})
                    return {"status": "running", "done": False, "job_deferred": True,
                            "retry_after_seconds": wait, "error": message}
        except Exception:  # noqa: BLE001 — fall through to a retryable attempt
            logger.exception("could not defer code task %s for the budget", run_id)
    return {"status": "failed", "done": False, "retryable": True, "error": message,
            "reason_code": "budget_wait", "retry_after_seconds": wait}


# --------------------------------------------------------------------------- #
#  The job handler                                                            #
# --------------------------------------------------------------------------- #
def run_code_task(engine, settings: Settings, jobs, run_id: str,
                  *, fencing_token: Optional[str] = None) -> dict:
    """Execute one full code-task attempt. Safe to re-run after a crash."""
    with _transaction(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        return {"status": "not_found", "done": True}
    if run["status"] != "running" or run["pull_request_url"]:
        # already terminal or PR already opened — idempotent no-op. BUT if this is a COMPLETED
        # decomposition step, still advance the chain (a reclaim/re-invoke lands here after the
        # step finished, and the in-process advance may have been lost) — advance() is idempotent.
        if run["status"] == "completed" and (run.get("finalize_ctx") or {}).get("decomposition_plan"):
            try:
                from .prd_decomposition import advance_decomposition
                advance_decomposition(engine, settings, jobs, run_id)
            except Exception:  # noqa: BLE001 — best-effort chain advance
                logger.exception("decomposition advance (idempotent path) failed for %s", run_id)
        return {"status": run["status"], "done": True,
                "pull_request_url": run["pull_request_url"], "decision_id": run["decision_id"]}

    if not settings.enable_code_task:
        with _transaction(engine) as c:
            return _fail(c, run_id, run["project_id"],
                         "Code tasks are disabled (BOSS_ENABLE_CODE_TASK=false).", status="blocked",
                         failure_class="needs_human", reason_code="feature_disabled")

    # Default for all coding tasks: route through the governed, role-separated pipeline.
    # IaC tasks are ALWAYS governed regardless of the flag (spec 05 §3.2 / T4): the
    # native inline lane truncates repo context (40 files / 20KB) and has no IaC
    # verify lane, so Terraform work must never run there.
    iac_task = is_iac_task(run["spec"], run["title"])
    if settings.governed_coding_pipeline or iac_task:
        if iac_task:
            # Auditable routing evidence (mirrors the execution_substrate pattern):
            # record on the run WHY it went governed, not just that it did.
            lane_routing = {
                "lane": "governed",
                "iac_task": True,
                "forced": not settings.governed_coding_pipeline,
                "reason": ("IaC task routed to the governed pipeline: the native lane "
                           "truncates repo context and has no IaC verify lane (spec 05 T4)"),
            }
            with _transaction(engine) as c:
                c.execute(
                    sql("UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx, '{}'::jsonb) "
                         "|| jsonb_build_object('lane_routing', CAST(:lr AS JSONB)), "
                         "updated_at=now() WHERE id=:id"),
                    {"lr": json.dumps(lane_routing), "id": run_id},
                )
        from .governed_code_task_service import run_governed_code_task
        result = run_governed_code_task(engine, settings, jobs, run_id,
                                        fencing_token=fencing_token)
        # Autonomous decomposition chain: when a decomposed build task lands successfully, start
        # the NEXT task in the plan (as a continuation on the shared branch) so BOSS delivers the
        # whole product itself, scaffold -> features -> wire-up, without a human between steps.
        if result.get("status") == "completed":
            try:
                from .prd_decomposition import advance_decomposition
                nxt = advance_decomposition(engine, settings, jobs, run_id)
                if nxt:
                    result = {**result, "decomposition_next_run": nxt}
            except Exception:  # noqa: BLE001 — the chain is best-effort; never fail the finished task
                logger.exception("decomposition advance failed for %s", run_id)
        return result

    project_id = run["project_id"]
    task_id = run["task_id"]
    spec = run["spec"]
    fctx = run["finalize_ctx"] or {}
    ports = _engine_ports()
    budget = ports.budget
    sf = SoftwareFactoryService(settings, ports=ports)

    # 1) Reserve budget for this code task's model spend.
    with _transaction(engine) as c:
        reservation = budget.reserve(
            c, project_id, category="code_task",
            amount_usd_micros=int(settings.code_task_budget_usd_micros),
            reason=f"code task: {run['title'][:80]}",
            expires_in_minutes=30, job_id=fctx.get("created_by_job_id"),
        )
    if reservation.get("status") != "reserved":
        return refused_reservation(engine, jobs, run_id=run_id, project_id=project_id,
                                   reservation=reservation, fencing_token=fencing_token)
    reservation_id = reservation["reservation_id"]
    with _transaction(engine) as c:
        c.execute(sql("UPDATE code_task_runs SET reservation_id=:r WHERE id=:id"),
                  {"r": reservation_id, "id": run_id})

    # 2) Resolve a clone token: prefer a short-lived GitHub-App installation token
    #    (Phase 2 impersonation); fall back to the PAT (Phase 0).
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    base_branch = spec.get("base_branch") or "main"
    # BOSS-267: per-task repo host (spec.repo_host) else the project setting; github default.
    from .repo_host import resolve_repo_host_kind
    repo_host_kind = resolve_repo_host_kind(settings, spec)
    if repo_host_kind == "bitbucket":
        token = settings.bitbucket_token or ""
    else:
        app = GitHubAppAdapter(settings)
        # Shared decision — see repo_token. Was: prefer the App and fail hard when this repo
        # is outside its installation, even with a working PAT configured.
        from .github_app_adapter import repo_token

        # THE PROJECT'S OWN REGISTERED CREDENTIAL, as a third source after the App and the
        # environment. This is the build that used to die on "BOSS_GITHUB_TOKEN is not set"
        # against a repository the operator had just connected in Setup.
        with _transaction(engine) as c:
            token = repo_token(settings, owner, repo,
                               connection=c, project_id=project_id)

    # Feedback-iteration: reuse the ROOT run's branch and clone its accumulated code so the
    # model builds ON prior work (not from base), and update the SAME PR. Mirrors the governed
    # path so iteration preserves context regardless of substrate (local OR micro-VM).
    root_run = fctx.get("root_run_id") or run_id
    work_branch = f"boss/code/{root_run}"
    if root_run != run_id and sf.github.branch_exists(work_branch):
        base_branch = work_branch  # clone the prior attempt's code

    # The provider decides the substrate + WHERE the agent runs.
    #  - microVM/container provider: the whole pipeline runs guest-side; we send a
    #    spec and collect the result (run_code_task).
    #  - local provider: the control plane drives the agent against a host sandbox.
    provider = make_sandbox_provider(settings)
    # Persist the resolved provider, rather than re-deriving it from mutable settings
    # when an operator later asks where this run actually executed.
    from .execution_substrate import normalize_build_substrate
    execution_substrate = normalize_build_substrate(getattr(provider, "kind", "local")).value
    with _transaction(engine) as c:
        c.execute(
            sql("UPDATE code_task_runs SET finalize_ctx=COALESCE(finalize_ctx, '{}'::jsonb) "
                 "|| jsonb_build_object('execution_substrate', CAST(:s AS text)), updated_at=now() "
                 "WHERE id=:id"),
            {"s": execution_substrate, "id": run_id},
        )
    sandbox = None
    try:
        if hasattr(provider, "run_code_task"):
            with _transaction(engine) as c:
                _set_phase(c, run_id, "coding")
            guest_spec = {
                "task_id": run_id, "work_branch": work_branch,
                "owner": owner, "repo": repo, "base_branch": base_branch,
                "title": run["title"], "instructions": spec.get("instructions", ""),
                "acceptance_criteria": spec.get("acceptance_criteria", []),
                "test_commands": spec.get("test_commands", []),
                "engine": settings.sandbox_guest_engine,
                "openai_api_key": settings.openai_api_key, "model": settings.openai_model,
                # provider-agnostic: base_url selects OpenAI/Grok/Together/etc.; claude token for claude_code
                "openai_base_url": settings.openai_base_url,
                "claude_oauth_token": settings.claude_code_oauth_token,
                "azure_endpoint": settings.azure_openai_endpoint,
                "azure_deployment": settings.azure_openai_deployment,
                "azure_api_version": settings.azure_openai_api_version,
                "azure_key": settings.azure_openai_key,
                "token": token, "timeout": settings.code_agent_timeout_seconds,
                "repo_host": repo_host_kind,
            }
            result = provider.run_code_task(guest_spec, tenant_id=project_id)
            # The guest reports ONE aggregate usage for its whole edit/test loop — record it
            # as a single llm_calls row so micro-VM runs still count toward $/verified-outcome
            # (VAG Phase 0). Per-call detail inside the guest needs guest support (later).
            set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id)
            record_llm_call(engine_name=f"guest:{settings.sandbox_guest_engine}",
                            usage=result.usage,
                            cost_usd_micros=int(result.cost_usd_micros or 0),
                            turns=int(result.turns or 0), stage="guest",
                            ok=bool(result.diff))
        else:
            sandbox = provider.acquire(task_id=run_id, tenant_id=project_id)
            with _transaction(engine) as c:
                _set_phase(c, run_id, "provisioning")
            sandbox.provision(owner, repo, base_branch=base_branch,
                              work_branch=work_branch, token=token,
                              repo_host=repo_host_kind)
            with _transaction(engine) as c:
                _set_phase(c, run_id, "coding")
            # VAG Phase 0: attribute the host agent's model calls to this run.
            set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id, stage="solo")
            # RUN AS AN AGENT: its held MCP servers reach the harness (code_task_mcp, E6).
            from .code_task_mcp import prepare as _prepare_mcp

            with _transaction(engine) as c:
                harness_mcp = _prepare_mcp(c, settings, project_id, spec.get("role_key"))
            agent = make_code_agent(settings, mcp=harness_mcp)
            code_spec = CodeTaskSpec(
                task_id=run_id, title=run["title"],
                instructions=spec.get("instructions", ""),
                acceptance_criteria=spec.get("acceptance_criteria", []),
                test_commands=spec.get("test_commands", []),
            )
            try:
                result = agent.run(code_spec, RepoContext(owner=owner, repo=repo,
                                                          base_branch=base_branch), sandbox)
            finally:
                if harness_mcp is not None:
                    harness_mcp.cleanup()

        # 5) Record agent outcome (governance metadata only — never the patch).
        with _transaction(engine) as c:
            _set_phase(c, run_id, "validating")
            c.execute(
                sql("UPDATE code_task_runs SET state=CAST(:s AS JSONB), "
                     "usage=jsonb_build_object('cost_usd_micros', CAST(:cost AS bigint), 'model', CAST(:m AS text)), "
                     "updated_at=now() WHERE id=:id"),
                {"s": json.dumps(result.to_metadata()), "cost": int(result.cost_usd_micros or 0),
                 "m": result.usage.get("model"), "id": run_id},
            )
            # Record the micro-VM id so a failed/incomplete run can be inspected later.
            _persist_sandbox_id(c, run_id, getattr(provider, "last_sandbox_task_id", None))

        if not result.diff:
            with _transaction(engine) as c:
                _release(budget, c, project_id, reservation_id, "no changes produced", run_id=run_id)
                return _fail(c, run_id, project_id, "Agent produced no file changes.",
                             failure_class="terminal", reason_code="no_diff")
        if not result.tests_passed:
            with _transaction(engine) as c:
                _release(budget, c, project_id, reservation_id, "tests did not pass", run_id=run_id)
                return _fail(c, run_id, project_id,
                             "Tests did not pass; not opening a PR. See run state for details.",
                             failure_class="terminal", reason_code="tests_failed")

        # 6) Open the PR via the EXISTING software-factory path (receipt dedupe,
        #    CI wait, merge-gate Decision, budget settle all handled there).
        with _transaction(engine) as c:
            _set_phase(c, run_id, "opening_pr")
            proposal = sf.propose_pull_request(
                c, project_id, task_id,
                reservation_id=reservation_id,
                dedupe_key=f"code:task:{run_id}",
                title=f"{run['title']}",
                branch_name=work_branch,
                # Who made it, as git trailers in the PR body (agent_acts; "" when unsigned).
                summary=result.summary + _pr_trailers(c, run_id),
                files=result.diff,
                created_by_job_id=fctx.get("created_by_job_id"),
            )

        status = proposal.get("status")
        if status not in ("created", "succeeded"):
            # blocked by data-boundary / routing / missing reservation, etc.
            with _transaction(engine) as c:
                c.execute(
                    sql("UPDATE code_task_runs SET status='blocked', phase='done', "
                         "decision_id=:d, error=:e, updated_at=now() WHERE id=:id"),
                    {"d": proposal.get("decision_id"), "e": f"PR not created: {status}", "id": run_id},
                )
            return {"status": "blocked", "done": True, **proposal}

        with _transaction(engine) as c:
            c.execute(
                sql("UPDATE code_task_runs SET status='completed', phase='done', "
                     "pull_request_url=:u, pull_request_number=:n, decision_id=:d, updated_at=now() WHERE id=:id"),
                {"u": proposal.get("pull_request_url"), "n": proposal.get("pull_request_number"),
                 "d": proposal.get("decision_id"), "id": run_id},
            )
            emit_event(c, project_id, "code_task.pr_opened", "boss", "code_task_run", run_id,
                       {"pull_request_url": proposal.get("pull_request_url"),
                        "decision_id": proposal.get("decision_id")})
        # A successful micro-VM is held for the bounded preview window so the app can
        # be served from the exact build rootfs. The sandbox service reaps the hold.
        if hasattr(provider, "release"):
            sid = getattr(provider, "last_sandbox_task_id", None)
            if sid and hasattr(provider, "hold"):
                provider.hold(sid, ttl_seconds=7200, reason="successful build available for preview")
            else:
                provider.release(sid)
        return {"status": "completed", "done": True, **proposal}

    except (SandboxError, RuntimeError) as exc:
        # A blank reason makes every retry blind — always record at least the exception type.
        reason = str(exc).strip() or f"{type(exc).__name__} with no message"
        fclass, rcode = classify_pipeline_exception(reason)
        with _transaction(engine) as c:
            _release(budget, c, project_id, reservation_id, f"code task error: {reason}", run_id=run_id)
            # Retain + record the micro-VM so the failure can be root-caused in the console.
            _persist_sandbox_id(c, run_id, getattr(provider, "last_sandbox_task_id", None))
            return _fail(c, run_id, project_id, reason,
                         failure_class=fclass, reason_code=rcode)
    finally:
        # Its stages end with it: a build that finished, failed or now waits on a person stops
        # showing its agents as working (agent_acts; never raises).
        from .agent_acts import settle_code_task

        settle_code_task(engine, run_id)
        # Request/worker threads are pooled — a stale run_id here would mis-attribute
        # the next job's (or run_research's) llm_calls rows.
        clear_llm_context()
        if sandbox is not None:
            sandbox.teardown()
