"""The onboarding agent (contract §8, BOSSAPP-913): it takes a new project from "a repository" to
"ready for work, with a ranked backlog and one suggested first piece of work", doing every item
it can itself and asking a person only for what it cannot do.

HOW IT RUNS
-----------
`start` writes one `project_onboarding_runs` row and queues a `project.onboarding` job (bounded:
one attempt, every outside call timed out). The worker hands the leased job to the API
(`POST /v1/projects/onboarding/execute`, like every job whose credentials live in the API), which
calls `execute_job` → `advance`. `advance` is RE-ENTRANT: each pass re-reads the row, does what
is due and records each checklist item as it goes (its state, owner, one-line why), so the
console draws the live diagram from `view`. While the code graph is still being mapped it records
how far it got and queues itself again in 20 s (a waiting job holds no worker), up to
`MAX_WAIT_S`; after that it carries on from GitHub's file list and says so.

WHAT IT DOES (each item is recorded; a failure says why in one line)
  repo_access     setup_actions.check_repository: read / write / pull requests on the repo
  models          setup_view.build_basics: a working model is connected
  knowledge       repo_index.request + status (the code graph), then ambient/dossier.build
  tests           the default branch's CI checks (ci_gate), else the test files the dossier found
  baseline_build  the default branch's CI verdict; with no CI the first build runs the tests in
                  its own sandbox (BOSS never runs a repository's code outside a sandbox)
  deploy_target   an active target, else a verified host this project may use (shared hosts),
                  proposed to a person (creating a target needs their step-up)
  secrets         the app's own files (run_env.needs_from_app_dir): names only; a secret only a
                  person can supply is theirs
  demo users      seed files / login hints (demo_logins.scan_checkout), names only
  backlog         open GitHub issues and PRs, failing CI and the dossier's findings, ranked with
                  the Chief of Staff's scoring (chief_of_staff.scores / priority), plus the open
                  Recommended-roadmap items; the first suggestion becomes `first_work`
It works with the ambient preview OFF: nothing here waits on the Chief of Staff's tick.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, href, iso, project_href, repos, words

log = logging.getLogger(__name__)

JOB_KIND = "project.onboarding"
#: How long a run waits for the code graph before it carries on from GitHub's file list.
MAX_WAIT_S = 15 * 60
#: How soon a waiting pass looks again.
AGAIN_S = 20
#: A run that has not moved for this long is stale: a new start replaces it.
STALE_S = 30 * 60

CHECKLIST: tuple[tuple[str, str], ...] = (
    ("repo_access", "Repo access"), ("models", "Models"), ("knowledge", "Code mapped"),
    ("tests", "Tests"), ("baseline_build", "Baseline build"), ("deploy_target", "Deploy target"),
    ("secrets", "Secrets"),
)
LABELS = dict(CHECKLIST)
STAGES = (("discover", "Discover"), ("ready", "Ready for work"), ("backlog", "Propose backlog"),
          ("first_work", "Work it"), ("deploy", "Deploy"))
#: The Ready-for-work card's rows (screen 10), in its order; `workflow` is read, not probed.
READY_CARD = ("repo_access", "baseline_build", "tests", "workflow", "deploy_target", "secrets")
#: The checklist item's words on the Ready card (screen 10: "Builds clean", "Staging host").
CARD_LABELS = {"repo_access": "Repo access", "baseline_build": "Builds clean", "tests": "Tests run",
               "workflow": "Workflow picked", "deploy_target": "Staging host",
               "secrets": "App secrets", "models": "Models", "knowledge": "Code mapped"}
#: Who does each item (the face on its row; screen 10 shows the agent doing it).
ITEM_AGENT = {"repo_access": "engineering_captain", "models": "engineering_captain",
              "knowledge": "engineering_captain", "tests": "qa", "baseline_build": "builder",
              "workflow": "engineering_captain", "deploy_target": "sre", "secrets": "sre"}
_DISCOVER_KEYS = ("repo_access", "knowledge", "tests")
_READY_KEYS = ("models", "baseline_build", "deploy_target", "secrets")


# ── the outside world, behind one seam (tests replace it; nothing here calls a model) ─────────

class Probes:
    """Every call that leaves BOSS's database. Each one is bounded by its own timeout."""

    def check_repository(self, connection: Connection, settings: Any, project_id: str,
                         binding_id: str) -> dict[str, Any]:
        from ..setup_actions import check_repository

        return check_repository(connection, settings, project_id, binding_id, actor="onboarding")

    def models_ready(self, connection: Connection, settings: Any, project_id: str
                     ) -> tuple[bool, str]:
        from .. import setup_view

        basics = setup_view.build_basics(connection, settings, project_id)
        models = list((basics or {}).get("models") or [])
        working = [m for m in models if setup_view.model_working(m)]
        if working:
            name = str(working[0].get("label") or working[0].get("name") or "A model")
            return True, f"{name} connected"
        return False, "No working model connected" if models else "No model connected yet"

    def reader(self, connection: Connection, settings: Any, project_id: str, repo: str) -> Any:
        from ..ci_gate import reader_for_project

        return reader_for_project(connection, settings, project_id, repo)

    def ci(self, reader: Any, branch: str) -> dict[str, Any]:
        from ..ci_gate import look

        outcome = look(reader, branch)
        checks = outcome.checks or []
        ok = [c for c in checks if c.get("status") == "completed"
              and str(c.get("conclusion") or "").lower() in ("success", "neutral", "skipped")]
        has = bool(checks) or bool(_safe(lambda: reader.has_workflows(branch), False))
        return {"state": outcome.state, "words": outcome.summary, "checks": len(checks),
                "passing": len(ok), "failing": list(outcome.failing or []),
                "url": outcome.failing_url, "has_ci": has}

    def issues(self, reader: Any) -> list[dict[str, Any]]:
        found = reader._get(reader._url("/issues?state=open&per_page=30&sort=updated"),  # noqa: SLF001
                            reader.token) or []
        return [i for i in found if isinstance(i, dict) and not i.get("pull_request")]

    def pulls(self, reader: Any) -> list[dict[str, Any]]:
        found = reader._get(reader._url("/pulls?state=open&per_page=20"), reader.token) or []  # noqa: SLF001
        return [p for p in found if isinstance(p, dict)]

    def tree_paths(self, connection: Connection, project_id: str, repo: str, branch: str
                   ) -> list[str]:
        from ...multirepo.common import org_of_project
        from ..start_project import github_paths

        return github_paths(connection, org_of_project(connection, project_id), repo, branch)

    def request_index(self, connection: Connection, settings: Any, project_id: str) -> None:
        from ..knowledge import repo_index

        repo_index.request(connection, settings, project_id=project_id, actor="onboarding")

    def checkout(self, connection: Connection, settings: Any, project_id: str, repo: str
                 ) -> tuple[Optional[Path], list[str]]:
        from ..ambient.dossier import checkout_for

        _full, root, files = checkout_for(connection, settings, project_id, repo)
        return root, files


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a probe that cannot answer is recorded, never raised
        return default


def _err(error: BaseException) -> str:
    said = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
    return said[:200]


# ── the record ──────────────────────────────────────────────────────────────────────────────

def _item(key: str, state: str, *, owner: str = "agent", detail: str = "", sub: str = "",
          action: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"key": key, "label": LABELS[key], "state": state, "owner": owner,
            "detail": detail[:300], "sub": words(sub, 6) if sub else "", "action": action,
            "at": datetime.now(timezone.utc).isoformat()}


def latest(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM project_onboarding_runs WHERE project_id = :p "
        "ORDER BY started_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    return dict(row) if row else None


def _get(connection: Connection, run_id: str, *, lock: bool = False) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT * FROM project_onboarding_runs WHERE id = :i" + (" FOR UPDATE" if lock else "")),
        {"i": run_id}).mappings().first()
    return dict(row) if row else None


def _record(engine: Any, run_id: str, *, items: Optional[list[dict[str, Any]]] = None,
            **fields: Any) -> None:
    """Write checklist items and fields in their own short transaction (the UI polls this)."""
    with engine.begin() as c:
        row = _get(c, run_id, lock=True)
        if row is None:
            return
        checklist = as_json(row["checklist"], {}) or {}
        for item in items or []:
            checklist[item["key"]] = item
        sets = ["checklist = CAST(:checklist AS JSONB)", "updated_at = now()"]
        params: dict[str, Any] = {"i": run_id, "checklist": json.dumps(checklist, default=str)}
        for key, value in fields.items():
            if key in ("discover", "facts", "backlog", "first_work"):
                if key == "facts":
                    value = {**(as_json(row["facts"], {}) or {}), **(value or {})}
                sets.append(f"{key} = CAST(:{key} AS JSONB)")
                params[key] = json.dumps(value, default=str)
            elif key in ("state", "error", "job_id"):
                sets.append(f"{key} = :{key}")
                params[key] = value
            elif key == "finished":
                sets.append("finished_at = now()" if value else "finished_at = NULL")
        c.execute(text(f"UPDATE project_onboarding_runs SET {', '.join(sets)} WHERE id = :i"),
                  params)


# ── starting ────────────────────────────────────────────────────────────────────────────────

def start(connection: Connection, project_id: str, *, trigger: str = "manual",
          actor: str = "boss", enqueue: bool = True) -> dict[str, Any]:
    """Begin (or return the live) onboarding run for a project and queue its job. Idempotent: a
    run still moving is returned as it is; a finished or stale one is replaced by a new run."""
    from .. import projects_archive
    from ..jobs import JOB_PRIORITY_REQUESTED, enqueue_job

    projects_archive.refuse_new_work(connection, project_id, "onboarding")
    current = latest(connection, project_id)
    now = datetime.now(timezone.utc)
    if current and current["state"] == "running":
        moved = current["updated_at"]
        moved = moved if moved.tzinfo else moved.replace(tzinfo=timezone.utc)
        if (now - moved).total_seconds() < STALE_S:
            return view(connection, project_id)
        connection.execute(text(
            "UPDATE project_onboarding_runs SET state = 'failed', error = :e, finished_at = now(), "
            "updated_at = now() WHERE id = :i"),
            {"i": current["id"], "e": "Stopped moving for 30 minutes; started again."})
    run_id = f"onb_{uuid.uuid4().hex[:16]}"
    first = [_item(key, "todo") for key, _ in CHECKLIST]
    connection.execute(text(
        "INSERT INTO project_onboarding_runs (id, project_id, trigger, started_by, checklist, "
        "discover) VALUES (:i, :p, :t, :a, CAST(:c AS JSONB), '[]'::jsonb)"),
        {"i": run_id, "p": project_id, "t": trigger, "a": actor,
         "c": json.dumps({i["key"]: i for i in first})})
    if enqueue:
        _queue(connection, project_id, run_id, pass_no=1, delay=0,
               priority=JOB_PRIORITY_REQUESTED, enqueue_job=enqueue_job)
    from ..ambient._common import emit

    emit(connection, project_id, "project.onboarding_started", actor, "project", project_id,
         {"run_id": run_id, "trigger": trigger})
    return view(connection, project_id)


def _queue(connection: Connection, project_id: str, run_id: str, *, pass_no: int, delay: int,
           priority: Optional[int] = None, enqueue_job: Optional[Callable[..., Any]] = None
           ) -> Optional[str]:
    if enqueue_job is None:
        from ..jobs import enqueue_job
    if priority is None:
        from ..jobs import JOB_PRIORITY_REQUESTED as priority  # noqa: N811
    job = enqueue_job(connection, project_id, JOB_KIND, {"run_id": run_id, "pass": pass_no},
                      priority=priority, max_attempts=1, run_after_seconds=delay,
                      dedupe_key=f"onboarding:{run_id}:{pass_no}", data_boundary="internal")
    job_id = (job or {}).get("id")
    if job_id:
        connection.execute(text("UPDATE project_onboarding_runs SET job_id = :j WHERE id = :i"),
                           {"j": job_id, "i": run_id})
    return job_id


def execute_job(engine: Any, settings: Any, *, job_id: str, worker_id: str, lease_token: str,
                probes: Optional[Probes] = None) -> dict[str, Any]:
    """The worker's leased `project.onboarding` job, run here (the API holds the credentials)."""
    from ..jobs import JobLeaseService, LeaseNotFoundError

    with engine.begin() as c:
        try:
            job = JobLeaseService(settings).require_active_lease(c, worker_id, job_id, lease_token)
        except LeaseNotFoundError as exc:
            return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else \
        json.loads(job.get("payload") or "{}")
    run_id = str(payload.get("run_id") or "")
    return advance(engine, settings, run_id, pass_no=int(payload.get("pass") or 1), probes=probes)


# ── one pass ────────────────────────────────────────────────────────────────────────────────

def advance(engine: Any, settings: Any, run_id: str, *, pass_no: int = 1,
            probes: Optional[Probes] = None, requeue: bool = True) -> dict[str, Any]:
    """Do what is due for this run and record it. Returns {status, state, again}."""
    probes = probes or Probes()
    with engine.connect() as c:
        row = _get(c, run_id)
    if row is None:
        return {"status": "failed", "retry": False, "error": "no such onboarding run"}
    if row["state"] != "running":
        return {"status": "succeeded", "state": row["state"], "again": False}
    try:
        again = _pass(engine, settings, row, probes)
    except Exception as error:  # noqa: BLE001 — recorded on the run, in one line
        log.warning("onboarding %s failed", run_id, exc_info=True)
        _record(engine, run_id, state="failed", error=f"Onboarding stopped: {_err(error)}",
                finished=True)
        return {"status": "failed", "retry": False, "error": _err(error)}
    if again:
        if requeue:
            with engine.begin() as c:
                _queue(c, row["project_id"], run_id, pass_no=pass_no + 1, delay=AGAIN_S)
        return {"status": "succeeded", "state": "running", "again": True}
    with engine.begin() as c:
        state = _final_state(_get(c, run_id) or row)
    _record(engine, run_id, state=state, finished=True)
    return {"status": "succeeded", "state": state, "again": False}


def _final_state(row: Mapping[str, Any]) -> str:
    checklist = as_json(row["checklist"], {}) or {}
    open_you = [i for i in checklist.values() if i.get("owner") == "you" and i["state"] != "done"]
    if open_you:
        return "needs_you"
    if any(i["state"] == "failed" for i in checklist.values()):
        return "needs_you"
    return "ready"


def _pass(engine: Any, settings: Any, row: Mapping[str, Any], probes: Probes) -> bool:
    """One pass over the checklist. True when it must look again (the code is being mapped)."""
    run_id, project_id = str(row["id"]), str(row["project_id"])
    checklist = as_json(row["checklist"], {}) or {}

    with engine.connect() as c:
        the_repos = repos(c, project_id)
    repo = the_repos[0] if the_repos else None

    # MODELS: cheap, every pass.
    with engine.begin() as c:
        ok, said = _safe(lambda: probes.models_ready(c, settings, project_id),
                         (False, "Could not read Repo and models"))
    _record(engine, run_id, items=[_item(
        "models", "done" if ok else "todo", owner="agent" if ok else "you", detail=said, sub=said,
        action=None if ok else {"label": "Add a model", "href": href(project_id, area="setup",
                                                                     focus="models")})])

    if repo is None:
        add = {"label": "Add repository", "href": project_href(project_id, tab="settings",
                                                               drawer="section:repos")}
        _record(engine, run_id, items=[
            _item("repo_access", "todo", owner="you", detail="This project has no repository "
                  "yet. Add one and onboarding continues by itself.", sub="No repository",
                  action=add),
            *[_item(k, "todo", detail="Waits for the repository") for k in
              ("knowledge", "tests", "baseline_build", "secrets")],
            _deploy_item(engine, project_id)],
            discover=[{"label": "Repository", "state": "waiting", "sub": "Add one"}])
        return False

    full, branch = repo["full_name"], repo["branch"]

    # REPO ACCESS: once per run.
    if (checklist.get("repo_access") or {}).get("state") != "done":
        _record(engine, run_id, items=[_item("repo_access", "now", sub="Checking access")],
                discover=[{"label": "Repo access", "state": "now", "sub": full}])
        access = _repo_access(engine, settings, project_id, repo, probes)
        _record(engine, run_id, items=[access])
        if access["state"] == "failed" and not (access.get("_read")):
            _record(engine, run_id, items=[
                _item(k, "todo", detail="Waits for repository access") for k in
                ("knowledge", "tests", "baseline_build", "secrets")])
            return False

    # KNOWLEDGE: the code graph, then the dossier.
    again, root, paths, dossier_row = _knowledge(engine, settings, run_id, row, project_id, full,
                                                 branch, probes)
    if again:
        return True

    stack = ((as_json(dossier_row.get("facts"), {}) or {}).get("stack") or {}) if dossier_row else {}
    dossier_facts = as_json(dossier_row.get("facts"), {}) if dossier_row else {}

    # TESTS AND BASELINE: the default branch's CI.
    ci = _ci(engine, settings, run_id, project_id, full, branch, dossier_facts or {}, probes)

    # DEPLOY TARGET, SECRETS, DEMO USERS.
    secrets_item, secret_names = _secrets(project_id, root)
    demo_users = []
    if root is not None:
        from .demo_logins import scan_checkout

        demo_users = _safe(lambda: scan_checkout(root, paths), [])
    _record(engine, run_id, items=[_deploy_item(engine, project_id), secrets_item],
            facts={"secrets": secret_names, "demo_users": demo_users,
                   "stack": stack, "ci": ci})

    # BACKLOG AND FIRST WORK.
    backlog = _backlog(engine, settings, project_id, full, ci, dossier_facts or {}, probes)
    first = _first_work(engine, project_id, backlog)
    discover = _discover_nodes(engine, run_id, stack, ci, dossier_facts or {})
    _record(engine, run_id, backlog=backlog, first_work=first, discover=discover)
    return False


def _repo_access(engine: Any, settings: Any, project_id: str, repo: Mapping[str, Any],
                 probes: Probes) -> dict[str, Any]:
    try:
        with engine.begin() as c:
            found = probes.check_repository(c, settings, project_id, repo["binding_id"])
    except Exception as error:  # noqa: BLE001
        return _item("repo_access", "failed", owner="you",
                     detail=f"Could not check {repo['full_name']}: {_err(error)}",
                     sub="Check failed", action={"label": "Check access",
                                                 "href": href(project_id, area="setup",
                                                              focus="repository")})
    checks = found.get("checks") if isinstance(found, dict) else {}
    checks = checks if isinstance(checks, dict) else {}

    def ok(key: str) -> bool:
        value = checks.get(key)
        return bool(isinstance(value, dict) and value.get("ok"))

    read, write, pr = ok("read"), ok("write"), ok("pull_requests")
    sub = " · ".join(w for w, v in (("Read", read), ("Write", write), ("PR", pr)) if v) or "No access"
    if read and write and pr:
        return _item("repo_access", "done", detail=f"BOSS can read, branch and open pull "
                     f"requests on {repo['full_name']}.", sub=sub)
    missing = [w for w, v in (("read", read), ("push branches", write),
                              ("open pull requests", pr)) if not v]
    item = _item("repo_access", "failed", owner="you",
                 detail=f"BOSS cannot {', '.join(missing)} on {repo['full_name']}: give its "
                        "GitHub access that permission.", sub=sub,
                 action={"label": "Fix access", "href": href(project_id, area="setup",
                                                              focus="repository")})
    item["_read"] = read
    return item


def _knowledge(engine: Any, settings: Any, run_id: str, row: Mapping[str, Any], project_id: str,
               full: str, branch: str, probes: Probes
               ) -> tuple[bool, Optional[Path], list[str], Optional[dict[str, Any]]]:
    """(look again?, the checkout, its paths, the dossier row)."""
    from ..ambient import dossier
    from ..knowledge import repo_index
    from ..start_project import mapping

    with engine.connect() as c:
        item = next((r for r in repo_index.status(c, project_id)["repositories"]
                     if str(r.get("repo") or "").lower() == full.lower()), None)
    m = mapping(item)
    indexing = repo_index.Limits.from_settings(settings).enabled
    if m["state"] in ("queued", "not_started") and (item is None or item.get("status") in
                                                    (None, "pending")):
        with engine.begin() as c:
            _safe(lambda: probes.request_index(c, settings, project_id), None)
    started = row["started_at"] if row["started_at"].tzinfo else \
        row["started_at"].replace(tzinfo=timezone.utc)
    waited = (datetime.now(timezone.utc) - started).total_seconds()
    files, parsed = int((item or {}).get("files") or 0), int((item or {}).get("parsed") or 0)
    if indexing and not m["done"] and m["state"] != "failed" and waited < MAX_WAIT_S:
        sub = f"{parsed} / {files} files" if files else "Queued to map"
        _record(engine, run_id, items=[_item("knowledge", "now", sub=sub,
                                             detail="Mapping the code into the knowledge graph.")],
                discover=[{"label": "Repo access", "state": "done", "sub": full},
                          {"label": "Mapping code", "state": "now", "sub": sub}])
        return True, None, [], None

    root: Optional[Path] = None
    paths: list[str] = []
    if m["done"]:
        with engine.connect() as c:
            root, paths = _safe(lambda: probes.checkout(c, settings, project_id, full), (None, []))
    if not paths:
        with engine.connect() as c:
            paths = _safe(lambda: probes.tree_paths(c, project_id, full, branch), [])
    try:
        with engine.begin() as c:
            built = dossier.build(c, project_id=project_id, repo=full, root=root,
                                  paths=None if root is not None else paths, actor="onboarding")
    except Exception as error:  # noqa: BLE001
        _record(engine, run_id, items=[_item(
            "knowledge", "failed", detail=f"Could not build the dossier: {_err(error)}",
            sub="Dossier failed")])
        return False, root, paths, None
    if m["done"]:
        nodes = int(((item or {}).get("graph") or {}).get("nodes") or (item or {}).get("nodes") or 0)
        done = _item("knowledge", "done", detail=f"Code mapped ({files} files, {nodes} graph "
                     "nodes) and the dossier written.", sub=f"{files} files mapped")
    elif m["state"] == "failed":
        done = _item("knowledge", "failed", detail=f"The code graph could not be built "
                     f"({m.get('error') or 'failed'}); the dossier was read from GitHub's file "
                     "list instead.", sub="Graph failed",
                     action={"label": "Map again", "href": href(project_id,
                                                                 area="knowledge")})
    elif not indexing:
        done = _item("knowledge", "skipped", detail="Mapping code is off on this install "
                     "(BOSS_KG_REPO_INDEX); the dossier was read from GitHub's file list.",
                     sub="Mapping off")
    else:
        done = _item("knowledge", "now", detail="The code graph is still being mapped; the "
                     "dossier was read from GitHub's file list meanwhile.",
                     sub=f"{parsed} / {files} files" if files else "Still mapping")
    _record(engine, run_id, items=[done])
    return False, root, paths, built


def _ci(engine: Any, settings: Any, run_id: str, project_id: str, full: str, branch: str,
        facts: Mapping[str, Any], probes: Probes) -> dict[str, Any]:
    tests_found = int((facts.get("tests") or {}).get("files") or 0)
    cmds = facts.get("commands") or {}
    test_cmd = (cmds.get("test") or [None])[0]
    try:
        with engine.connect() as c:
            reader = probes.reader(c, settings, project_id, full)
        result = probes.ci(reader, branch)
    except Exception as error:  # noqa: BLE001
        result = {"state": "unreadable", "words": _err(error), "checks": 0, "passing": 0,
                  "failing": [], "url": None, "has_ci": bool(facts.get("ci"))}
    state, n, good = result.get("state"), int(result.get("checks") or 0), int(result.get("passing") or 0)
    if state == "passed":
        tests = _item("tests", "done", detail=f"{good} of {n} checks pass on {branch}"
                      + (f" ({test_cmd})" if test_cmd else "") + ".", sub=f"{good} / {n} pass")
        base = _item("baseline_build", "done", detail=f"{branch} is green in CI.", sub="CI green")
    elif state == "failed":
        failing = result.get("failing") or []
        tests = _item("tests", "failed", detail=f"{len(failing) or n - good} check(s) fail on "
                      f"{branch}: {', '.join(failing[:3])}.", sub=f"{good} / {n} pass")
        base = _item("baseline_build", "failed", detail=f"{branch} is red in CI; fixing it is "
                     "proposed as the first work.", sub="CI red")
    elif state == "waiting" and n:
        tests = _item("tests", "now", detail=f"CI is still running on {branch}.",
                      sub=f"{good} / {n} done")
        base = _item("baseline_build", "now", detail="Waiting for CI on the default branch.",
                     sub="CI running")
    else:
        why = ("No CI runs on the default branch" if state in ("waiting", "no_ci")
               else f"CI could not be read ({result.get('words') or state})")
        tests = _item("tests", "done" if tests_found else "todo",
                      detail=(f"{tests_found} test files found"
                              + (f"; run with {test_cmd}" if test_cmd else "") + f". {why}."
                              if tests_found else f"No tests found. {why}."),
                      sub=f"{tests_found} test files" if tests_found else "No tests")
        base = _item("baseline_build", "skipped",
                     detail=f"{why}: the first build runs the build and tests in its own "
                            "sandbox and reports them on its pull request.", sub="First build")
    _record(engine, run_id, items=[tests, base])
    return {**result, "tests_found": tests_found, "test_cmd": test_cmd}


def _deploy_item(engine: Any, project_id: str) -> dict[str, Any]:
    from ..deployment_fleet import usable_hosts_sql

    with engine.connect() as c:
        target = c.execute(text(
            "SELECT t.environment, t.app_name, h.name AS host, h.address FROM deployment_targets t "
            "LEFT JOIN deployment_hosts h ON h.id = t.host_id WHERE t.project_id = :p AND "
            "t.status = 'active' ORDER BY (lower(t.environment) IN ('staging', 'stage')) DESC "
            "LIMIT 1"), {"p": project_id}).mappings().first()
        if target:
            where = target["address"] or target["host"] or "its target"
            return _item("deploy_target", "done", detail=f"{target['environment'].title()} "
                         f"deploys to {where}.", sub=f"{target['environment'].title()} ready")
        host = c.execute(text(
            f"SELECT id, name, address, project_id FROM deployment_hosts WHERE status = 'verified' "
            f"AND {usable_hosts_sql()} ORDER BY (project_id = :p) DESC, created_at LIMIT 1"),
            {"p": project_id}).mappings().first()
    if host:
        shared = host["project_id"] != project_id
        where = host["address"] or host["name"]
        return _item("deploy_target", "todo", owner="you",
                     detail=f"Use the verified{' shared' if shared else ''} host {where} for "
                            "staging. Adding a deploy target needs your confirmation.",
                     sub=f"Use {where}",
                     action={"label": "Use for staging", "href": href(
                         project_id, area="infra", add="target", host=host["id"])})
    return _item("deploy_target", "todo", owner="you",
                 detail="No verified server yet: add one and BOSS deploys staging there.",
                 sub="Needs a server", action={"label": "Add a server",
                                               "href": href(project_id, area="infra",
                                                            add="host")})


def _secrets(project_id: str, root: Optional[Path]
             ) -> tuple[dict[str, Any], dict[str, list[str]]]:
    from ...templates.run_env import needs_from_app_dir

    if root is None:
        return (_item("secrets", "skipped", detail="The code is not mapped here, so the app's "
                      "files were not read: its first build declares the secrets it needs.",
                      sub="On first build"), {})
    needs = _safe(lambda: needs_from_app_dir(Path(root)), {})
    names = {"generated": sorted(n for n, d in needs.items() if d.kind == "generated"),
             "demo": sorted(n for n, d in needs.items() if d.kind == "demo"),
             "yours": sorted(n for n, d in needs.items()
                             if d.person_must_supply() or (d.kind == "secret" and d.deploy_only))}
    if names["yours"]:
        shown = ", ".join(names["yours"][:4])
        return (_item("secrets", "todo", owner="you",
                      detail=f"The app needs {shown} for deploy; only you can supply "
                             f"{'it' if len(names['yours']) == 1 else 'them'}.",
                      sub=f"{len(names['yours'])} to set",
                      action={"label": "Set secrets", "href": project_href(
                          project_id, tab="settings", drawer="section:deploy_values")}), names)
    count = len(names["generated"]) + len(names["demo"])
    return (_item("secrets", "done", detail=(f"BOSS generates {count} value(s) the app declares"
                                             f" ({', '.join((names['generated'] + names['demo'])[:4])})."
                                             if count else "The app declares no secrets."),
                  sub=f"{count} generated" if count else "None needed"), names)


# ── the backlog ─────────────────────────────────────────────────────────────────────────────

#: Dossier signal kind → (task class, severity, label, work kind).
_SCAN = {"scan.no_ci": ("ci_setup", "medium", "Add CI", "chore"),
         "scan.missing_tests": ("missing_tests", "medium", "Add tests", "chore"),
         "scan.untested_hotspot": ("missing_tests", "low", "Test a hot file", "chore"),
         "scan.todo_cluster": ("todo_triage", "low", "Clear a TODO cluster", "chore"),
         "scan.no_docs": ("doc_drift", "low", "Write a README", "chore"),
         "osv.vulnerable_dependency": ("security_patch", "high", "Patch a dependency", "bug")}


def _score(task_class: str, severity: str, source: str, occurrences: int = 1) -> float:
    from ..ambient.chief_of_staff import priority, scores

    drivers = scores([{"severity": severity, "occurrences": occurrences, "source": source}],
                     task_class, 0.8)
    return round(priority(drivers), 2)


def _candidate(label: str, *, source: str, task_class: str, severity: str, kind: str,
               detail: str = "", ref: str = "", external_url: Optional[str] = None,
               text_for_work: Optional[str] = None, score_source: str = "scan") -> dict[str, Any]:
    digest = hashlib.sha1(f"{source}|{ref or label}".encode()).hexdigest()[:12]
    return {"id": f"cand_{digest}", "label": words(label, 5),
            "detail": detail or label, "source": source, "kind": kind,
            "task_class": task_class, "severity": severity, "ref": ref,
            "external_url": external_url, "text": text_for_work or detail or label,
            "score": _score(task_class, severity, score_source), "state": "proposed"}


def _backlog(engine: Any, settings: Any, project_id: str, full: str, ci: Mapping[str, Any],
             facts: Mapping[str, Any], probes: Probes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if ci.get("state") == "failed":
        failing = ci.get("failing") or []
        n = len(failing) or 1
        out.append(_candidate(f"Fix {n} failing check{'s' if n != 1 else ''}", source="ci",
                              task_class="red_main", severity="high", kind="bug",
                              detail=f"CI fails on the default branch: {', '.join(failing[:5])}.",
                              ref="ci:main", external_url=ci.get("url"), score_source="ci",
                              text_for_work=f"Make the default branch's CI pass again. Failing: "
                                            f"{', '.join(failing[:5]) or 'see CI'}."))
    try:
        with engine.connect() as c:
            reader = probes.reader(c, settings, project_id, full)
        issues = probes.issues(reader)
        pulls = probes.pulls(reader)
    except Exception:  # noqa: BLE001 — GitHub unreachable: the backlog is the dossier's findings
        issues, pulls = [], []
    for issue in issues[:15]:
        labels = {str((lb or {}).get("name") or "").lower() for lb in issue.get("labels") or []
                  if isinstance(lb, dict)}
        bug = bool(labels & {"bug", "defect", "regression"})
        title = str(issue.get("title") or f"Issue #{issue.get('number')}")
        out.append(_candidate(title, source="bug" if bug else "ticket",
                              task_class="issue_triage", severity="medium" if bug else "low",
                              kind="bug" if bug else "ticket", ref=f"issue:{issue.get('number')}",
                              detail=f"#{issue.get('number')} {title}",
                              external_url=issue.get("html_url"), score_source="github",
                              text_for_work=f"{title} (GitHub issue #{issue.get('number')}). "
                                            f"{str(issue.get('body') or '')[:600]}"))
    for pull in pulls[:10]:
        title = str(pull.get("title") or f"PR #{pull.get('number')}")
        out.append(_candidate(f"Finish PR #{pull.get('number')}", source="ticket",
                              task_class="issue_triage", severity="low", kind="ticket",
                              ref=f"pr:{pull.get('number')}", detail=f"Open PR: {title}",
                              external_url=pull.get("html_url"), score_source="pr",
                              text_for_work=f"Review and finish the open pull request "
                                            f"#{pull.get('number')} ({title})."))
    with engine.connect() as c:
        seeded = c.execute(text(
            "SELECT seeded_signals FROM project_dossiers WHERE project_id = :p AND repo = :r"),
            {"p": project_id, "r": full}).scalar()
        signal_ids = [s.get("signal_id") for s in as_json(seeded, []) or [] if isinstance(s, dict)]
        signals = [dict(r) for r in c.execute(text(
            "SELECT id, kind, title, severity FROM signals WHERE id = ANY(:ids)"),
            {"ids": [i for i in signal_ids if i]}).mappings().all()] if signal_ids else []
        recs = [dict(r) for r in c.execute(text(
            "SELECT work_item_id, title, why, kind, priority_score FROM ambient_recommendations "
            "WHERE project_id = :p AND status = 'open' ORDER BY score DESC LIMIT 5"),
            {"p": project_id}).mappings().all()]
    for s in signals:
        klass, sev, label, kind = _SCAN.get(str(s["kind"]), ("roadmap_item", "low",
                                                             str(s["title"]), "chore"))
        out.append(_candidate(label, source="roadmap", task_class=klass,
                              severity=str(s.get("severity") or sev), kind=kind,
                              detail=str(s["title"]), ref=f"signal:{s['id']}",
                              score_source="osv" if s["kind"].startswith("osv") else "scan",
                              text_for_work=str(s["title"])))
    for r in recs:
        item = _candidate(str(r["title"]), source="roadmap", task_class="roadmap_item",
                          severity="medium", kind="feature" if r["kind"] == "feature" else "chore",
                          detail=str(r["why"] or r["title"]), ref=f"rec:{r['work_item_id']}",
                          text_for_work=f"{r['title']}. {r['why'] or ''}")
        item["score"] = max(item["score"], float(r["priority_score"] or 0))
        item["href"] = project_href(project_id, tab="roadmap", drawer=f"rec:{r['work_item_id']}")
        out.append(item)
    seen: set[str] = set()
    ranked = []
    for item in sorted(out, key=lambda i: -i["score"]):
        if item["ref"] in seen:
            continue
        seen.add(item["ref"])
        item.setdefault("href", project_href(project_id, tab="roadmap"))
        ranked.append(item)
    return ranked[:20]


def _first_work(engine: Any, project_id: str, backlog: list[dict[str, Any]]) -> Optional[dict]:
    from ..start_project import how_to_start

    if not backlog:
        return None
    top = backlog[0]
    with engine.connect() as c:
        plan = _safe(lambda: how_to_start(c, project_id, has_repo=True), {}) or {}
    cost = plan.get("cost") or {}
    low, high = cost.get("low_usd"), cost.get("high_usd")
    usd = round((float(low) + float(high)) / 2, 2) if low is not None and high is not None else None
    return {"label": top["label"], "detail": top["detail"], "source": top["source"],
            "kind": top["kind"], "estimate_min": plan.get("minutes_to_pr"),
            "estimate_range_min": [plan.get("minutes_low"), plan.get("minutes_high")],
            "estimate_usd": usd, "external_url": top.get("external_url"),
            "start": {"href": f"/v1/projects/{project_id}/work", "method": "POST",
                      "body": {"kind": top["kind"] if top["kind"] in ("feature", "bug", "ticket")
                               else "ticket", "text": top["text"], "title": top["label"],
                               "source_ref": top["ref"]}},
            "others_href": project_href(project_id, tab="roadmap")}


def _discover_nodes(engine: Any, run_id: str, stack: Mapping[str, Any], ci: Mapping[str, Any],
                    facts: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """What Helm found, as the radial diagram's nodes (screen 10): each label IS the finding —
    "214 files", "FastAPI+React", "142/148 tests", "3 hotspots", "4 docs" — green once found."""
    facts = facts or {}
    with engine.connect() as c:
        row = _get(c, run_id) or {}
    checklist = as_json(row.get("checklist"), {}) or {}

    def state(key: str) -> str:
        item = checklist.get(key) or {}
        st = item.get("state") or "todo"
        return "waiting" if item.get("owner") == "you" and st != "done" else st

    files = int(facts.get("files") or 0)
    stack_words = "+".join(w for w in (stack.get("framework"), stack.get("language")) if w)
    checks, passing = int(ci.get("checks") or 0), int(ci.get("passing") or 0)
    test_files = int((facts.get("tests") or {}).get("files") or 0)
    tests = (f"{passing}/{checks} tests" if checks else
             f"{test_files} test files" if test_files else "No tests yet")
    hotspots = len(facts.get("hotspots") or [])
    docs = int((facts.get("docs") or {}).get("docs") or 0)
    return [
        {"key": "files", "label": f"{files} files" if files else "Mapping code",
         "state": "done" if files else state("knowledge"), "sub": ""},
        {"key": "stack", "label": stack_words or "Stack unknown",
         "state": "done" if stack_words else "skipped", "sub": ""},
        {"key": "tests", "label": tests, "state": state("tests") if checks else
         ("done" if test_files else "skipped"), "sub": ""},
        {"key": "hotspots", "label": f"{hotspots} hotspot{'s' if hotspots != 1 else ''}",
         "state": "done" if files else "todo", "sub": ""},
        {"key": "docs", "label": f"{docs} doc{'s' if docs != 1 else ''}",
         "state": "done" if files else "todo", "sub": ""},
    ]


# ── the view (GET …/onboarding) ─────────────────────────────────────────────────────────────

def _stage_state(items: list[dict[str, Any]]) -> str:
    states = [i.get("state") for i in items]
    if not items:
        return "todo"
    if any(s == "failed" for s in states):
        return "failed"
    if all(s in ("done", "skipped") for s in states):
        return "done"
    if any(s in ("now",) for s in states) or any(s in ("done", "skipped") for s in states):
        return "now"
    return "todo"


def _workflow_item(connection: Connection, project_id: str) -> dict[str, Any]:
    """Which workflow new work runs through (start_project.how_to_start): a read, not a probe."""
    from ..start_project import how_to_start

    plan = _safe(lambda: how_to_start(connection, project_id, has_repo=True), {}) or {}
    name = plan.get("workflow_name")
    if name:
        return {"key": "workflow", "label": CARD_LABELS["workflow"], "state": "done",
                "owner": "agent", "detail": f"New work runs through {name}.", "sub": str(name),
                "action": None}
    return {"key": "workflow", "label": CARD_LABELS["workflow"], "state": "todo", "owner": "agent",
            "detail": "No feature workflow is mapped for this project yet.", "sub": "Not mapped",
            "action": None}


def _deploy_stage(connection: Connection, project_id: str) -> tuple[str, str]:
    """(state, sub) of the Deploy stage: staging live (done), a target set (auto / you approve),
    or not set up."""
    row = connection.execute(text(
        "SELECT t.environment, t.auto_deploy, EXISTS (SELECT 1 FROM stack_deployments s WHERE "
        "s.target_id = t.id AND s.status = 'healthy') AS live FROM deployment_targets t "
        "WHERE t.project_id = :p AND t.status = 'active' ORDER BY "
        "(lower(t.environment) IN ('staging', 'stage')) DESC LIMIT 1"),
        {"p": project_id}).mappings().first()
    if not row:
        return "todo", "not set up"
    env = str(row["environment"] or "staging").lower()
    policy = "auto" if row["auto_deploy"] else "you approve"
    return ("done" if row["live"] else "todo"), f"{env} {policy}"


#: What the agent is doing on each check, as a verb phrase ("Beacon is checking the staging host").
NOW_WORDS = {"repo_access": "checking repo access", "models": "checking the model",
             "knowledge": "mapping the code", "tests": "running the tests",
             "baseline_build": "checking the build", "workflow": "picking the workflow",
             "deploy_target": "checking the staging host", "secrets": "reading the app's secrets"}


def _now_line(items: list[dict[str, Any]], running: bool) -> str:
    """What the agent is doing now, in one line ("Beacon is checking the staging host · :8004")."""
    if not running:
        return ""
    names = {"engineering_captain": "Helm", "qa": "Vera", "builder": "Forge", "sre": "Beacon"}
    doing = next((i for i in items if i.get("state") == "now" and i.get("owner") != "you"), None)
    if not doing:
        return ""
    who = names.get(ITEM_AGENT.get(doing["key"], ""), "Helm")
    verb = NOW_WORDS.get(doing["key"], f"on {CARD_LABELS.get(doing['key'], doing['key']).lower()}")
    what = (doing.get("sub") or "").strip()
    return f"{who} is {verb} · {what}" if what else f"{who} is {verb}"


def _elapsed_words(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    row = latest(connection, project_id)
    if row is None:
        return {"state": "not_started", "run_id": None, "stages": [
            {"key": k, "label": label, "state": "todo", "sub": ""} for k, label in STAGES],
            "discover": {"nodes": []}, "checklist": [], "ready": [], "only_you": [],
            "first_work": None, "backlog": [], "backlog_total": 0, "accepted": [], "now": "",
            "elapsed_s": 0,
            "start": {"href": f"/v1/projects/{project_id}/onboarding/start", "method": "POST"},
            "accept_backlog": {"href": f"/v1/projects/{project_id}/onboarding/backlog/accept",
                               "method": "POST"}}
    checklist = as_json(row["checklist"], {}) or {}
    items = []
    for key, label in CHECKLIST:
        item = dict(checklist.get(key) or _item(key, "todo"))
        item.pop("_read", None)
        item["label"] = label
        item["agent"] = ITEM_AGENT.get(key)
        items.append(item)
    by_key = {i["key"]: i for i in items}
    workflow = _workflow_item(connection, project_id)
    workflow["agent"] = ITEM_AGENT["workflow"]
    by_key["workflow"] = workflow
    # THE READY CARD (screen 10): six rows in its words, then any item only a person can do
    # (a missing model) so nothing that waits on you is hidden.
    ready_rows = [{**by_key[k], "label": CARD_LABELS[k]} for k in READY_CARD if k in by_key]
    ready_rows += [{**i, "label": CARD_LABELS.get(i["key"], i["label"])} for i in items
                   if i["key"] not in READY_CARD and i["owner"] == "you" and i["state"] != "done"]
    backlog = as_json(row["backlog"], []) or []
    accepted = {str(r) for r in connection.execute(text(
        "SELECT facts->>'candidate_id' FROM ambient_recommendations WHERE project_id = :p "
        "AND detector = 'onboarding' AND facts ? 'candidate_id'"), {"p": project_id}).scalars()}
    for b in backlog:
        if b.get("id") in accepted:
            b["state"] = "accepted"
    first = as_json(row["first_work"], None)
    discover_items = [by_key[k] for k in _DISCOVER_KEYS]
    ready_items = [by_key[k] for k in _READY_KEYS]
    discover_state = _stage_state(discover_items)
    ready_state = _stage_state(ready_items)
    if ready_state != "failed" and any(i["owner"] == "you" and i["state"] != "done"
                                       for i in ready_items):
        ready_state = "now" if ready_state != "done" else ready_state
    running = row["state"] == "running"
    started = row["started_at"]
    end = row["finished_at"] or datetime.now(timezone.utc)
    started = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    elapsed = max(0, int((end - started).total_seconds()))
    discover_done_at = max([str(i.get("at") or "") for i in discover_items] or [""])
    took = ""
    if discover_state == "done" and discover_done_at:
        try:
            at = datetime.fromisoformat(discover_done_at)
            took = _elapsed_words(max(0, int((at - started).total_seconds())))
        except ValueError:
            took = ""
    done_rows = sum(1 for r in ready_rows if r["state"] in ("done", "skipped"))
    deploy_state, deploy_sub = _deploy_stage(connection, project_id)
    worked = bool(connection.execute(text(
        "SELECT 1 FROM workflow_runs WHERE project_id = :p AND created_at > :at LIMIT 1"),
        {"p": project_id, "at": started}).first())
    stages = [
        {"key": "discover", "label": "Discover", "state": discover_state,
         "sub": (f"done · {took}" if took else "done") if discover_state == "done"
         else (by_key["knowledge"].get("sub") or "")},
        {"key": "ready", "label": "Ready for work", "state": ready_state,
         "sub": f"{done_rows} of {len(ready_rows)}"},
        {"key": "backlog", "label": "Propose backlog",
         "state": "done" if backlog else ("now" if running and discover_state == "done" else "todo"),
         "sub": f"{len(backlog)} proposed" if backlog else ("drafting" if running else "")},
        {"key": "first_work", "label": "Work it",
         "state": "done" if worked else ("now" if first and not running else "todo"),
         "sub": "verified loop"},
        {"key": "deploy", "label": "Deploy", "state": deploy_state, "sub": deploy_sub},
    ]
    nodes = as_json(row["discover"], []) or []
    only_you = [i for i in items if i["owner"] == "you" and i["state"] != "done"]
    # THE LIVE DIAGRAM (screen 00b): what Helm is doing while it reads — the stack it found, the
    # code being mapped (118 / 214 files), the tests (142 / 148 pass), then the Ready check.
    stack = ((as_json(row["facts"], {}) or {}).get("stack") or {})
    stack_words = " · ".join(w for w in (stack.get("framework"), stack.get("language")) if w)
    know, tests = by_key["knowledge"], by_key["tests"]
    live = [{"key": "stack", "label": "Stack found", "state": "done" if stack_words else (
                "now" if running else "todo"), "sub": stack_words},
            {"key": "mapping", "label": "Mapping code", "state": know.get("state") or "todo",
             "sub": know.get("sub") or ""},
            {"key": "tests", "label": "Tests run", "state": tests.get("state") or "todo",
             "sub": tests.get("sub") or ""},
            {"key": "ready", "label": "Ready check", "state": ready_state if ready_state == "done"
             else "todo", "sub": ""}]
    return {"run_id": row["id"], "state": row["state"], "trigger": row["trigger"],
            "stages": stages, "discover": {"nodes": nodes, "live": live}, "checklist": items,
            "ready": ready_rows, "only_you": only_you, "first_work": first,
            "backlog": backlog[:20], "backlog_total": len(backlog),
            "accepted": sorted(accepted & {str(b.get("id")) for b in backlog}),
            "now": _now_line(items, running),
            "error": row["error"], "started_at": iso(row["started_at"]),
            "finished_at": iso(row["finished_at"]),
            "elapsed_s": elapsed,
            "start": {"href": f"/v1/projects/{project_id}/onboarding/start", "method": "POST"},
            "accept_backlog": {"href": f"/v1/projects/{project_id}/onboarding/backlog/accept",
                               "method": "POST"},
            "href": project_href(project_id, tab="overview", drawer="onboarding")}


# ── accepting the proposed backlog (screen 10 "Accept backlog / Edit") ─────────────────────

#: A backlog candidate's kind → the roadmap's recommendation kind (ambient_recommendations CHECK).
_REC_KIND = {"bug": "hardening", "ticket": "feature", "feature": "feature", "chore": "process"}
_TASK_REC_KIND = {"missing_tests": "test_gap", "ci_setup": "process", "todo_triage": "refactor",
                  "doc_drift": "process", "security_patch": "hardening", "red_main": "hardening"}
LANES = ("next", "later")


def accept_backlog(connection: Connection, project_id: str, ids: list[str], *, actor: str,
                   lane: str = "next", titles: Optional[Mapping[str, str]] = None
                   ) -> dict[str, Any]:
    """Put the chosen candidates of the proposed backlog on the Roadmap, in the order given, on
    the `lane` (Next unless said): each becomes an ACCEPTED roadmap item — planned work on the
    Work tab, a row in Roadmap › Next — and NOTHING STARTS: a build starts only when a person
    starts that item. Idempotent: a candidate already on the roadmap is left where it is.
    `titles` renames candidates (the Edit step: reorder, drop, rename before accepting)."""
    titles = {str(k): " ".join(str(v).split())[:200] for k, v in (titles or {}).items()
              if str(v or "").strip()}
    if lane not in LANES:
        raise ValueError("lane is next or later")
    row = latest(connection, project_id)
    backlog = {str(b.get("id")): b for b in (as_json(row["backlog"], []) or [])} if row else {}
    unknown = [i for i in ids if i not in backlog]
    if unknown:
        raise LookupError(f"Not in the proposed backlog: {', '.join(unknown[:5])}")
    rank0 = int(connection.execute(text(
        "SELECT COALESCE(max((facts->>'rank')::int), 0) FROM ambient_recommendations "
        "WHERE project_id = :p AND status = 'accepted' AND facts->>'lane' = :l"),
        {"p": project_id, "l": lane}).scalar() or 0)
    added, kept = [], []
    for n, cid in enumerate(dict.fromkeys(ids), start=1):
        c = backlog[cid]
        ref = str(c.get("ref") or cid)
        if ref.startswith("rec:"):
            # An open roadmap recommendation: accepted where it is (the ledger and its ticket).
            item_id = ref.split(":", 1)[1]
            status = connection.execute(text(
                "SELECT status FROM ambient_recommendations WHERE work_item_id = :w AND "
                "project_id = :p"), {"w": item_id, "p": project_id}).scalar()
            if status in ("open", "snoozed"):
                from ..ambient import roadmap as ambient_roadmap

                ambient_roadmap.accept(connection, item_id, project_id=project_id, actor=actor,
                                       as_="ticket", plane=False)
            connection.execute(text(
                "UPDATE ambient_recommendations SET facts = facts || CAST(:f AS JSONB) "
                "WHERE work_item_id = :w AND NOT (facts ? 'lane')"),
                {"w": item_id, "f": json.dumps({"lane": lane, "rank": rank0 + n,
                                                "candidate_id": cid})})
            if titles.get(cid):
                connection.execute(text(
                    "UPDATE ambient_recommendations SET title = :t WHERE work_item_id = :w"),
                    {"t": titles[cid], "w": item_id})
            (added if status in ("open", "snoozed") else kept).append(cid)
            continue
        fingerprint = f"onboarding:{hashlib.sha1(ref.encode()).hexdigest()[:16]}"
        exists = connection.execute(text(
            "SELECT work_item_id FROM ambient_recommendations WHERE project_id = :p "
            "AND fingerprint = :f AND status IN ('accepted', 'open', 'snoozed') LIMIT 1"),
            {"p": project_id, "f": fingerprint}).scalar()
        if exists:
            kept.append(cid)
            continue
        item_id = f"wi_{uuid.uuid4().hex[:16]}"
        title = titles.get(cid) or str(c.get("label") or c.get("detail") or "Backlog item")[:200]
        connection.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, description, "
            "ambient, fingerprint, lifecycle, task_class, source, created_by, outcome, "
            "resolved_by, resolved_at) VALUES (:i, :p, 'roadmap', :s, :t, :d, true, :f, "
            "'resolved', 'roadmap_item', 'onboarding', :a, 'accepted onto the roadmap', :a, now())"),
            {"i": item_id, "p": project_id, "s": fingerprint, "t": title,
             "d": str(c.get("text") or c.get("detail") or title)[:4000], "f": fingerprint,
             "a": actor})
        kind = _TASK_REC_KIND.get(str(c.get("task_class") or "")) or \
            _REC_KIND.get(str(c.get("kind") or ""), "feature")
        facts = {"lane": lane, "rank": rank0 + n, "candidate_id": cid, "source": c.get("source"),
                 "ref": ref, "work_kind": c.get("kind"), "external_url": c.get("external_url"),
                 "text": c.get("text"), "onboarding_run_id": row["id"] if row else None}
        connection.execute(text(
            "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, kind, "
            "fingerprint, subject, title, why, proposed_shape, facts, score, priority_score, "
            "status, accepted_as, decided_by, decided_at) VALUES (:w, :p, 'onboarding', :k, :f, "
            ":sub, :t, :y, 'ticket', CAST(:facts AS JSONB), :sc, :sc, 'accepted', 'ticket', :a, "
            "now())"),
            {"w": item_id, "p": project_id, "k": kind, "f": fingerprint, "sub": ref, "t": title,
             "y": str(c.get("detail") or title)[:600], "facts": json.dumps(facts, default=str),
             "sc": float(c.get("score") or 0), "a": actor})
        added.append(cid)
    from ..ambient._common import emit

    emit(connection, project_id, "project.backlog_accepted", actor, "project", project_id,
         {"accepted": added, "already": kept, "lane": lane})
    return {"accepted": added, "already": kept, "lane": lane, "started": [],
            "href": project_href(project_id, tab="roadmap")}


__all__ = ["CHECKLIST", "JOB_KIND", "LANES", "MAX_WAIT_S", "Probes", "accept_backlog", "advance",
           "execute_job", "latest", "start", "view"]
