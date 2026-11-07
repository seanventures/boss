"""New work on the Work tab (screen 05, contract §3 "Helm's plan"): what Start will run, drawn
before anyone presses it.

ONE BOX, THREE KINDS. Feature · Bug · Ticket go through THE start door (POST …/work →
start_delivery_workflow). Which workflow that is:

  feature, ticket  the project's mapped small-feature workflow, else Minor Feature
                   (start_project.how_to_start — exactly what the old "What should we add?" ran);
  bug              the project's `bugfix` mapping when it has one, else the feature path;
  picked           any active product workflow of this project, when the person used "change".

HELM'S PLAN is that workflow's REAL steps as a strip, in the order its version lists them:
Plan (the agent who plans it) → You approve (only where a person is really asked: a gate,
or the panel's PRD while BOSS_MEETING_PRD_AUTO_APPROVE is off) → Build → Review → PR →
Staging (the deploy; amber when a person approves it). A workflow with no such step has no
such node.

THE ESTIMATES ARE THIS PROJECT'S HISTORY. Time: the median agent time to the pull request
over the workflow's completed runs here (each step's started → completed, gates and the steps
after the PR left out: a person's wait is not how long the work takes). Cost: the median of
what each completed run spent (workflow_costs — builds, panel and research). With no run yet,
the time is the steps' typical minutes (`basis: typical`) and there is no cost figure — never
an invented one.

AN ISSUE LINK IS KEPT. Work started from a GitHub or Plane issue (a Ticket pasted as a link,
an onboarding candidate `issue:12`, a roadmap item carrying the issue) records `source_ref`
and `source_url` on the run, so the Work row shows the issue chip (#12) linking to it.
"""
from __future__ import annotations

import re
from statistics import median
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import agents, as_json, face, href

KINDS = ("feature", "bug", "ticket")
BUG_CLASS = "bugfix"
#: How many completed runs the time estimate reads (the newest).
HISTORY_RUNS = 20
#: Steps from the deploy on wait on a person's release: not "how long it takes".
_AFTER_THE_PR = ("deploy", "redeploy", "notify", "monitor", "monitoring_pack", "smoke_test",
                 "verify_recovery", "teardown_preview")

_GITHUB_ISSUE = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)", re.I)
_PLANE_ISSUE = re.compile(r"https?://[^\s]*plane[^\s]*/[^\s]*?(?:issues|work-items|browse)/"
                          r"([A-Za-z0-9_-]+)", re.I)
_URL = re.compile(r"https?://\S+")


# ── the issue a piece of work came from ─────────────────────────────────────────────────────

def issue_of(source_ref: Any = None, source_url: Any = None,
             repo: Optional[str] = None) -> Optional[dict[str, Any]]:
    """{provider, number, label, external_url} for an issue link, or None.

    From a URL (GitHub issue or pull request, a Plane work item) or a ref the onboarding agent
    writes (`issue:12`, `pr:7`), resolved against the project's repository."""
    url = str(source_url or "").strip()
    ref = str(source_ref or "").strip()
    for candidate in (url, ref):
        m = _GITHUB_ISSUE.search(candidate)
        if m:
            number = int(m.group(3))
            return {"provider": "github", "number": number, "label": f"#{number}",
                    "kind": "pr" if m.group(2).lower() == "pull" else "issue",
                    "external_url": m.group(0)}
        m = _PLANE_ISSUE.search(candidate)
        if m:
            key = m.group(1)
            return {"provider": "plane", "number": None, "label": key, "kind": "issue",
                    "external_url": _URL.search(candidate).group(0)}
    m = re.fullmatch(r"(issue|pr):(\d+)", ref)
    if m and repo:
        number = int(m.group(2))
        path = "issues" if m.group(1) == "issue" else "pull"
        return {"provider": "github", "number": number, "label": f"#{number}",
                "kind": "issue" if m.group(1) == "issue" else "pr",
                "external_url": url or f"https://github.com/{repo}/{path}/{number}"}
    if url.startswith(("http://", "https://")):
        return {"provider": "link", "number": None, "label": "Issue", "kind": "issue",
                "external_url": url}
    return None


def ticket_link(said: str) -> Optional[str]:
    """The first issue link in what a person pasted as a Ticket."""
    for pattern in (_GITHUB_ISSUE, _PLANE_ISSUE):
        m = pattern.search(said or "")
        if m:
            return _URL.search(said[m.start():]).group(0).rstrip(").,;")
    return None


def ticket_title(said: str, link: Optional[str]) -> Optional[str]:
    """A Ticket's name: the words around the link, else "Issue #12" / the Plane key."""
    rest = " ".join(_URL.sub(" ", said or "").split())
    if rest:
        return None  # the start door names it from the words (titles.shorten)
    found = issue_of(source_url=link)
    if not found:
        return None
    return f"Issue {found['label']}" if found["provider"] == "github" else found["label"]


# ── which workflow Start runs ───────────────────────────────────────────────────────────────

def _definition(connection: Connection, project_id: str, workflow_key: str
                ) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT d.workflow_key, d.name, d.description, v.spec FROM workflow_definitions d "
        "JOIN workflow_versions v ON v.id = d.active_version_id WHERE d.project_id = :p "
        "AND d.workflow_key = :k AND d.status = 'active' AND d.namespace = 'product' LIMIT 1"),
        {"p": project_id, "k": workflow_key}).mappings().first()
    return dict(row) if row else None


def _mapped(connection: Connection, project_id: str, work_class: str) -> Optional[dict[str, Any]]:
    from ..start_project import _mapped as mapped  # noqa: SLF001 — the one mapping reader

    return mapped(connection, project_id, work_class)


def _has_repo(connection: Connection, project_id: str) -> bool:
    return bool(connection.execute(text(
        "SELECT 1 FROM github_repository_bindings WHERE project_id = :p AND status = 'active' "
        "LIMIT 1"), {"p": project_id}).first())


def choose(connection: Connection, project_id: str, kind: str,
           workflow_key: Optional[str] = None) -> dict[str, Any]:
    """What the start door is sent: exactly one of `work_class` / `workflow_key`, plus the
    resolved workflow {key, name, spec} (None when the project has nothing to run — the start
    door then answers its own honest 409). Raises LookupError for a picked workflow that is
    not an active product workflow of this project."""
    from ..start_project import how_to_start

    if workflow_key:
        found = _definition(connection, project_id, workflow_key)
        if found is None:
            raise LookupError(f"This project has no active workflow {workflow_key!r}.")
        return {"workflow_key": workflow_key, "work_class": None, "chosen": "picked",
                "resolved": found}
    if kind == "bug":
        mapped = _mapped(connection, project_id, BUG_CLASS)
        if mapped:
            return {"workflow_key": None, "work_class": BUG_CLASS, "chosen": "mapped",
                    "resolved": mapped}
    start = how_to_start(connection, project_id, has_repo=_has_repo(connection, project_id))
    resolved = None
    if start.get("resolved_key"):
        resolved = _definition(connection, project_id, str(start["resolved_key"])) or {
            "workflow_key": start["resolved_key"], "name": start.get("workflow_name"),
            "spec": {}}
    return {"workflow_key": start.get("workflow_key"), "work_class": start.get("work_class"),
            "chosen": "mapped" if start.get("work_class") and resolved else "default",
            "resolved": resolved}


# ── Helm's plan: the strip ──────────────────────────────────────────────────────────────────

def _cap(step: Mapping[str, Any]) -> str:
    binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
    return str(binding.get("capability") or step.get("capability") or "")


def _gated(step: Mapping[str, Any], prd_auto: bool) -> bool:
    """Whether a person is asked at this step (workflow_capabilities.stops_for_a_person, the one
    rule the catalogue card, the plan and the start preflight read)."""
    from ..deploy_policy import follows_policy
    from ..workflow_capabilities import CAPABILITY_KEYS, stops_for_a_person

    if str(step.get("kind") or "") == "decision":
        return True
    cap = _cap(step)
    approval = step.get("approval") if isinstance(step.get("approval"), dict) else {}
    if follows_policy(step):
        return False
    if cap not in CAPABILITY_KEYS:
        return approval.get("mode") == "manual"
    if approval.get("mode") == "manual" or cap in ("spec_review", "architecture", "ux_review"):
        return stops_for_a_person(cap, approval.get("mode"), prd_auto_approve=prd_auto)
    return False


def _who(step: Mapping[str, Any]) -> Optional[str]:
    from ..command_center import step_agent

    who = step_agent(step, {}, {})
    return who.get("role_key")


def _env_word(connection: Connection, project_id: str) -> str:
    from .health import env_label

    env = connection.execute(text(
        "SELECT environment FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        "ORDER BY (lower(environment) IN ('production', 'prod')), created_at LIMIT 1"),
        {"p": project_id}).scalar()
    return env_label(env) if env else "Deploy"


def strip(connection: Connection, project_id: str, spec: Mapping[str, Any]
          ) -> list[dict[str, Any]]:
    """The workflow's steps as the plan strip's nodes, in its own order:
    {key, label, type: agent|you|todo, agent?, steps[]}."""
    from ..workflow_capabilities import prd_auto_approved
    from .work import _station_of  # noqa: SLF001 — THE station rule every row uses

    prd_auto = prd_auto_approved()
    roster = agents(connection, project_id)
    nodes: list[dict[str, Any]] = []

    def add(key: str, label: str, kind: str, step_id: str, agent: Optional[dict] = None) -> None:
        if nodes and nodes[-1]["key"] == key:
            nodes[-1]["steps"].append(step_id)
            if agent and not nodes[-1].get("agent"):
                nodes[-1]["agent"] = agent
            return
        node: dict[str, Any] = {"key": key, "label": label, "type": kind, "steps": [step_id]}
        if agent:
            node["agent"] = agent
        nodes.append(node)

    built = False
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        sid, cap = str(step.get("id") or ""), _cap(step)
        station = _station_of(cap, str(step.get("kind") or ""))
        gated = _gated(step, prd_auto)
        if station == "ship":
            if cap in ("deploy", "redeploy"):
                if built:
                    _review_and_pr(nodes)
                add("ship", _env_word(connection, project_id), "you" if gated else "todo", sid)
            continue
        if station == "plan":
            add("plan", "Plan", "agent", sid, face(roster, _who(step)))
            if cap == "meeting" and not prd_auto:
                add("approve", "You approve", "you", sid)
            continue
        if gated:
            add("approve", "You approve", "you", sid)
            continue
        if station == "design":
            add("design", "Design", "agent", sid, face(roster, _who(step)))
        elif station == "build":
            built = True
            add("build", "Build", "todo", sid, face(roster, _who(step)))
        elif station == "review":
            add("review", "Review", "todo", sid, face(roster, _who(step)))
        elif station == "pr":
            add("pr", "PR", "todo", sid)
    if built:
        _review_and_pr(nodes)
    return nodes


def _review_and_pr(nodes: list[dict[str, Any]]) -> None:
    """A build reviews and opens a pull request itself (its review gates; its PR): a workflow
    with no review step still shows Review and PR after its Build, never "skipped"."""
    keys = [n["key"] for n in nodes]
    if "build" not in keys:
        return
    at = max(i for i, k in enumerate(keys) if k == "build") + 1
    while at < len(nodes) and nodes[at]["key"] == "review":
        at += 1
    if "review" not in keys:
        nodes.insert(at, {"key": "review", "label": "Review", "type": "todo", "steps": []})
        at += 1
    if "pr" not in keys:
        at = max(i for i, n in enumerate(nodes) if n["key"] in ("build", "review")) + 1
        nodes.insert(at, {"key": "pr", "label": "PR", "type": "todo", "steps": []})


# ── the estimates, from this project's history ──────────────────────────────────────────────

def history_minutes(connection: Connection, project_id: str, workflow_key: str
                    ) -> tuple[Optional[int], int]:
    """(median agent minutes to the PR, runs read) over the workflow's newest completed runs."""
    runs = connection.execute(text(
        "SELECT r.id, r.workflow_version_id FROM workflow_runs r JOIN workflow_definitions d "
        "ON d.id = r.workflow_definition_id WHERE r.project_id = :p AND d.workflow_key = :k "
        "AND r.status = 'completed' AND COALESCE(r.mode, 'normal') <> 'test' "
        "ORDER BY r.completed_at DESC NULLS LAST, r.created_at DESC LIMIT :n"),
        {"p": project_id, "k": workflow_key, "n": HISTORY_RUNS}).mappings().all()
    if not runs:
        return None, 0
    specs = {str(r["id"]): as_json(r["spec"], {}) or {} for r in connection.execute(text(
        "SELECT id, spec FROM workflow_versions WHERE id = ANY(:ids)"),
        {"ids": list({str(r["workflow_version_id"]) for r in runs})}).mappings()}
    steps = connection.execute(text(
        "SELECT DISTINCT ON (workflow_run_id, step_id) workflow_run_id, step_id, step_kind, "
        "decision_id, started_at, completed_at FROM workflow_step_runs "
        "WHERE workflow_run_id = ANY(:ids) AND status = 'succeeded' "
        "ORDER BY workflow_run_id, step_id, iteration DESC, created_at DESC"),
        {"ids": [str(r["id"]) for r in runs]}).mappings().all()
    caps_of = {}
    for r in runs:
        spec = specs.get(str(r["workflow_version_id"]), {})
        caps_of[str(r["id"])] = {str(s.get("id")): _cap(s) for s in spec.get("steps") or []
                                 if isinstance(s, dict)}
    per_run: dict[str, float] = {}
    for s in steps:
        rid = str(s["workflow_run_id"])
        cap = caps_of.get(rid, {}).get(str(s["step_id"]), "")
        if (s["step_kind"] == "decision" or s["decision_id"] or cap in _AFTER_THE_PR
                or not s["started_at"] or not s["completed_at"]):
            continue
        seconds = (s["completed_at"] - s["started_at"]).total_seconds()
        if seconds > 0:
            per_run[rid] = per_run.get(rid, 0.0) + seconds
    values = [v / 60.0 for v in per_run.values() if v > 0]
    if not values:
        return None, 0
    return max(1, int(round(median(values)))), len(values)


def history_cost(connection: Connection, project_id: str, workflow_key: str
                 ) -> Optional[dict[str, Any]]:
    """{median_usd, runs, metered} from workflow_costs (what each completed run spent)."""
    try:
        from ..workflow_costs import workflow_costs

        with connection.begin_nested():
            found = workflow_costs(connection, project_id).get("costs", {}).get(workflow_key)
    except Exception:  # noqa: BLE001 — an estimate is a help, never a failed plan
        return None
    if not found or not found.get("runs"):
        return None
    return {"median_usd": found.get("median_usd"), "runs": found["runs"],
            "metered": found.get("metered"), "low_usd": found.get("low_usd"),
            "high_usd": found.get("high_usd")}


def round_minutes(minutes: Optional[int]) -> Optional[int]:
    """"≈ 25 min": to the nearest 5 from ten minutes on."""
    if not minutes:
        return None
    return minutes if minutes < 10 else int(round(minutes / 5.0)) * 5


def estimate(connection: Connection, project_id: str, workflow_key: str,
             spec: Mapping[str, Any]) -> dict[str, Any]:
    from ..start_project import minutes_to_pr

    minutes, runs = history_minutes(connection, project_id, workflow_key)
    basis = "history"
    if minutes is None:
        minutes, basis = (minutes_to_pr(dict(spec)) or None), "typical"
    cost = history_cost(connection, project_id, workflow_key)
    usd = cost.get("median_usd") if cost else None
    return {"minutes": round_minutes(minutes), "minutes_basis": basis if minutes else None,
            "minutes_runs": runs,
            "usd": round(float(usd), 2) if usd is not None else None,
            "usd_basis": "history" if usd is not None else None,
            "usd_runs": cost["runs"] if cost else 0,
            "metered": bool(cost and cost.get("metered"))}


# ── the read: GET /v1/projects/{id}/work/plan ───────────────────────────────────────────────

#: What the old New build called each size (Quick · Small · Spec first · Major), kept on the
#: workflow it maps to so a person who knew the sizes finds them in the Workflow chip.
WAS_SIZE = {"fast_fix": "Quick", "minor_feature": "Small", "major_feature": "Spec first · Major"}


def steps(connection: Connection, project_id: str, spec: Mapping[str, Any],
          roster: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    """The workflow's REAL steps, in its own order, for the Workflow chip's numbered strip and
    its Steps list: {id, label, you (a person approves here), agent?}. Fan-out and join markers
    are not steps anyone does."""
    from ..workflow_capabilities import CAPABILITIES, prd_auto_approved

    prd_auto = prd_auto_approved()
    roster = roster if roster is not None else agents(connection, project_id)
    out = []
    for step in spec.get("steps") or []:
        if not isinstance(step, dict) or str(step.get("kind") or "") in ("parallel", "join"):
            continue
        cap = _cap(step)
        found = CAPABILITIES.get(cap)
        label = (step.get("label") or step.get("name") or (found.label if found else "")
                 or str(step.get("id") or "").replace("_", " ").capitalize())
        who = _who(step)
        # The panel's PRD waits on a person while auto-approval is off (the strip's own rule).
        you = _gated(step, prd_auto) or (cap == "meeting" and not prd_auto)
        out.append({"id": str(step.get("id") or ""), "capability": cap, "label": str(label),
                    "you": you, **({"approve": _APPROVES.get(cap) or f"Approve {str(label).lower()}"}
                                   if you else {}),
                    **({"agent": face(roster, who)} if who else {})})
    return out


#: What a person approves at a step that waits on them, in their words.
_APPROVES = {"meeting": "the PRD", "spec_review": "the spec", "ux_review": "how it looks",
             "deploy": "the deploy", "redeploy": "the deploy", "release_approval": "the release",
             "merge": "merge PR"}


def options(connection: Connection, project_id: str, kind: str,
            with_steps: bool = False) -> list[dict[str, Any]]:
    """Every active product workflow this project can start, the kind's own first. With
    `with_steps`, each carries its step count, the steps a person approves, its estimate from
    this project's history and the old size it replaces (the Workflow chip's popover)."""
    from ..workflow_catalogue import CATALOGUE

    classes = {e["key"]: e.get("work_class") for e in CATALOGUE}
    fits = {"feature": ("feature.minor", "feature.major"), "ticket": ("feature.minor",
                                                                      "feature.major"),
            "bug": ("bugfix",)}[kind]
    rows = connection.execute(text(
        "SELECT d.workflow_key, d.name, d.description, v.spec FROM workflow_definitions d "
        "JOIN workflow_versions v ON v.id = d.active_version_id WHERE d.project_id = :p "
        "AND d.status = 'active' AND d.namespace = 'product' ORDER BY d.name"),
        {"p": project_id}).mappings().all()
    roster = agents(connection, project_id) if with_steps else None
    out = []
    for r in rows:
        work_class = classes.get(str(r["workflow_key"]))
        if work_class == "project.new":
            continue  # a new app is not work on this one
        one = {"key": r["workflow_key"], "name": r["name"],
               "detail": r["description"] or "", "work_class": work_class,
               "fits": work_class in fits}
        if with_steps:
            spec = as_json(r["spec"], {}) or {}
            listed = steps(connection, project_id, spec, roster)
            est = estimate(connection, project_id, str(r["workflow_key"]), spec)
            one.update({"steps": len(listed),
                        "approvals": [s.get("approve") or s["label"] for s in listed if s["you"]],
                        "minutes": est.get("minutes"), "usd": est.get("usd"),
                        "was": WAS_SIZE.get(str(r["workflow_key"]))})
        out.append(one)
    out.sort(key=lambda o: (not o["fits"], o["work_class"] == "any", o["name"]))
    return out


# ── the chips' defaults: the tracker, the targets, who is told ─────────────────────────────

def tracker(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    """Where "create an issue" files it: the project's connected work tracker, else GitHub Issues
    on its repository through its GitHub connection. None when neither exists."""
    from ..work_orchestration import connected_tracker
    from .common import repos

    try:
        with connection.begin_nested():
            row = connected_tracker(connection, project_id)
    except Exception:  # noqa: BLE001 — no tracker table on an old engine: the repo's issues
        row = None
    if row is not None:
        config = row.get("configuration") or {}
        where = (f"{config.get('owner')}/{config.get('repo')}"
                 if config.get("owner") and config.get("repo") else
                 str(config.get("project_identifier") or config.get("project_key")
                     or config.get("project_id") or ""))
        return {"provider": str(row["provider_key"]), "name": str(row.get("display_name")
                                                                  or row["provider_key"]),
                "where": where, "connected": True}
    repo = next(iter(repos(connection, project_id)), None)
    if repo:
        return {"provider": "github_issues", "name": "GitHub", "where": repo["full_name"],
                "connected": False}
    return None


def create_issue(connection: Connection, project_id: str, *, title: str,
                 body: str) -> dict[str, Any]:
    """File the work as an issue (the Issue chip's "create"): {issue} or {error}."""
    from ..work_orchestration import connected_tracker, with_credential
    from ..work_tracker_adapters import get_adapter
    from .common import repos

    row = None
    try:
        with connection.begin_nested():
            row = connected_tracker(connection, project_id)
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        repo = next(iter(repos(connection, project_id)), None)
        if not repo or "/" not in repo["full_name"]:
            return {"error": "This project has no tracker and no repository to file it in."}
        owner, name = repo["full_name"].split("/", 1)
        row = {"provider_key": "github_issues", "configuration": {
            "owner": owner, "repo": name, "credential": "github_connection",
            "provider_key": "github_issues"}}
    try:
        config = with_credential(connection, project_id, dict(row["configuration"] or {}))
        made = get_adapter(str(row["provider_key"])).create_issue(
            config, title=title[:200], description_html=body.replace("\n", "<br>"),
            description_text=body)
    except Exception as exc:  # noqa: BLE001 — the work still starts; the issue is a mirror
        return {"error": f"The issue was not created ({type(exc).__name__})."}
    if made is None or not made.external_url:
        return {"error": "The tracker did not create the issue."}
    found = issue_of(source_url=made.external_url) or {
        "provider": str(row["provider_key"]), "label": made.external_key or "Issue",
        "external_url": made.external_url, "kind": "issue", "number": None}
    return {"issue": found}


def targets(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Where the work deploys after merge: the project's active targets, staging first;
    production only with a person (the deploy step's gate)."""
    from .common import web_url
    from .health import env_label

    rows = connection.execute(text(
        "SELECT id, app_name, environment, domains, public_url FROM deployment_targets "
        "WHERE project_id = :p AND status = 'active' ORDER BY "
        "(lower(environment) IN ('production', 'prod')), environment, app_name"),
        {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        domains = r["domains"] if isinstance(r["domains"], list) else []
        url = web_url(r["public_url"] or (domains[0] if domains else None)) or ""
        port = re.search(r":(\d{2,5})(?:/|$)", url)
        out.append({"id": r["id"], "label": env_label(r["environment"]),
                    "environment": r["environment"], "app": r["app_name"], "url": url,
                    "port": int(port.group(1)) if port else None,
                    "prod": str(r["environment"] or "").lower() in ("production", "prod")})
    return out


def tell(connection: Connection, project_id: str) -> dict[str, Any]:
    """Who hears when the work needs someone or its PR is ready: the project's own routes
    (Settings › Notifications), as names."""
    try:
        from .notify_routes import view as routes_view

        with connection.begin_nested():
            got = routes_view(connection, project_id)
    except Exception:  # noqa: BLE001 — no routes table: the console is told
        return {"routes": [{"event": "needs_you", "label": "Needs you", "words": "Console"},
                           {"event": "pr_ready", "label": "PR ready", "words": "Console"}]}
    out = []
    for row in got.get("routes") or []:
        if not isinstance(row, Mapping) or row.get("event") == "incidents":
            continue
        out.append({"event": row.get("event"), "label": row.get("label"),
                    "words": row.get("words") or "Console"})
    return {"routes": out}


def defaults(connection: Connection, project_id: str) -> dict[str, Any]:
    """The New work modal's other chips, from this project's own record."""
    found = targets(connection, project_id)
    staging = next((t for t in found if not t["prod"]), None)
    return {"tracker": tracker(connection, project_id), "targets": found,
            "target": staging["id"] if staging else None, "tell": tell(connection, project_id),
            "priority": "normal"}


def plan(connection: Connection, project_id: str, kind: str = "feature",
         workflow_key: Optional[str] = None, detail: bool = False) -> dict[str, Any]:
    """Helm's plan for new work of `kind`: the workflow, its strip, the estimates and the
    workflows "change" offers. With `detail` (the New work modal), the workflow's real steps,
    each option's steps and estimate, and the other chips' defaults. Raises LookupError for an
    unknown picked workflow."""
    from ..workflow_catalogue import work_class_label

    kind = kind if kind in KINDS else "feature"
    chosen = choose(connection, project_id, kind, workflow_key)
    resolved = chosen["resolved"]
    roster = agents(connection, project_id)
    captain = face(roster, "engineering_captain")
    out: dict[str, Any] = {"kind": kind, "planner": captain, "workflow": None, "nodes": [],
                           "steps": [], "estimate": None,
                           "options": options(connection, project_id, kind, with_steps=detail)}
    if detail:
        out["defaults"] = defaults(connection, project_id)
    if resolved is None:
        return out
    spec = as_json(resolved.get("spec"), {}) or {}
    key = str(resolved["workflow_key"])
    out["workflow"] = {"key": key, "name": resolved.get("name") or key,
                       "chosen": chosen["chosen"],
                       "work_class": chosen.get("work_class"),
                       "why": ("you picked it" if chosen["chosen"] == "picked" else
                               f"this project's workflow for {work_class_label(chosen['work_class'])}"
                               if chosen.get("work_class") else "the default for a change"),
                       "href": href(project_id, area="delivery_workflows", tab="library", wf=key)}
    out["nodes"] = strip(connection, project_id, spec)
    out["steps"] = steps(connection, project_id, spec, roster)
    out["estimate"] = estimate(connection, project_id, key, spec)
    return out


__all__ = ["KINDS", "WAS_SIZE", "choose", "create_issue", "defaults", "estimate", "issue_of",
           "options", "plan", "round_minutes", "steps", "strip", "targets", "tell",
           "ticket_link", "ticket_title", "tracker"]
