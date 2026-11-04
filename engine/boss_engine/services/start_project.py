"""Starting a project, the simple way (BOSSAPP-810): a sample, an imported repo, or an idea.

The console's New project flow has three doors — Try a sample, Import a repo, New app from an
idea — and a project page that asks one question, "What should we add?". Everything they DO goes
through the paths that already exist:

  * a project is POST /v1/projects (services/projects.create);
  * an imported repository is PUT /v1/projects/{id}/repos/{repo} (multirepo.project_repos), and
    its code is mapped by the knowledge tick's repository index (knowledge/repo_index) — the
    same path a knowledge-graph project's Import step takes;
  * the sample is the `hello-world` built-in template applied as a new app
    (POST /v1/templates/hello-world/apply) through the smallest workflow that still plans,
    builds, reviews, opens a PR and previews (`minor_feature`);
  * work starts through POST /v1/delivery-workflows/start, the one start door.

This module only READS: what BOSS can tell about a repository before and after it is imported
(`repo_facts`, `detect`), and the project page's one view (`view`): the repository, how far its
code is mapped, which workflow Start runs and what it is likely to cost, and each piece of work
with its state and the one thing to do next.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: The built-in template the sample builds (templates/apps/hello-world).
SAMPLE_TEMPLATE = "hello-world"
#: The sample's project name, and its stack.
SAMPLE_NAME = "Hello World"
SAMPLE_STACK = "node"
#: The smallest catalogue workflow that plans (the panel's PRD), builds (with its inner review and
#: tests), opens a pull request and starts a preview. Fast Fix has no plan and no preview.
SAMPLE_WORKFLOW = "minor_feature"

#: Adding to an imported app: the class of work a small feature is, and the catalogue workflow a
#: project uses for it when nobody mapped that class (only `project.new` is mapped by default).
FEATURE_CLASS = "feature.minor"
FEATURE_WORKFLOW = "minor_feature"
#: A project with no repository yet builds a new app: its default (`project.new`) workflow.
NEW_APP_CLASS = "project.new"

#: The example chips under "What should we add?".
EXAMPLES: tuple[str, ...] = ("Add an API endpoint", "Fix a bug", "Add tests", "Upgrade a library")

#: "What happens next": the five tiles, and who acts at each.
NEXT_STEPS: tuple[dict[str, Any], ...] = (
    {"key": "plan", "label": "Plan", "who": "You approve", "you": True},
    {"key": "build", "label": "Build", "who": "BOSS", "you": False},
    {"key": "review", "label": "Review & test", "who": "BOSS", "you": False},
    {"key": "pr", "label": "Pull request", "who": "You merge", "you": True},
    {"key": "preview", "label": "Preview", "who": "BOSS", "you": False},
)

_LIVE_RUN = ("queued", "validating", "running", "waiting", "paused", "blocked")
_OPEN_GATE = ("pending", "delegated")
_PLAN_STEPS = ("meeting", "spec_review", "research")


# ── what a repository is, from its file paths ──────────────────────────────────────────────────

_EXT_LANG = {
    ".java": "Java", ".kt": "Kotlin", ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".go": "Go", ".rs": "Rust",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".swift": "Swift", ".scala": "Scala",
}
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|\.(test|spec)\.[a-z]+$|_test\.[a-z]+$|"
                        r"(^|/)test_[^/]+\.py$|Tests?\.java$")


def _names(paths: Iterable[str]) -> list[str]:
    return [str(p).strip().lstrip("./") for p in paths if str(p or "").strip()]


def detect(paths: Iterable[str], *, language: Optional[str] = None) -> dict[str, Any]:
    """What a repository is, read from its file paths alone (no file is opened).

    Returns {language, framework, build, tests, runs_as}: each None when the paths do not say —
    never a guess presented as a fact. `language` (GitHub's own answer, from the registry) wins
    over counting extensions."""
    files = _names(paths)
    base = {PurePosixPath(f).name for f in files}
    shallow = [f for f in files if f.count("/") <= 1]
    shallow_names = {PurePosixPath(f).name for f in shallow}

    def has(*names: str) -> bool:
        return any(n in shallow_names for n in names)

    lang = language or None
    if not lang:
        counts: dict[str, int] = {}
        for f in files:
            found = _EXT_LANG.get(PurePosixPath(f).suffix.lower())
            if found:
                counts[found] = counts.get(found, 0) + 1
        lang = max(counts, key=counts.get) if counts else None

    framework = build = runs_as = None
    if has("pom.xml"):
        build = "Maven"
    elif has("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
        build = "Gradle"
    elif has("package.json"):
        build = "pnpm" if has("pnpm-lock.yaml") else "Yarn" if has("yarn.lock") else "npm"
    elif has("pyproject.toml", "setup.py", "requirements.txt"):
        build = "Poetry" if has("poetry.lock") else "pip"
    elif has("go.mod"):
        build = "Go modules"
    elif has("Cargo.toml"):
        build = "Cargo"

    if any(re.search(r"(^|/)src/main/resources/application\.(properties|ya?ml)$", f) for f in files) \
            or any(PurePosixPath(f).name.endswith("Application.java") for f in files):
        framework, runs_as = "Spring", "Web API"
    elif any(n.startswith("next.config.") for n in base):
        framework, runs_as = "Next.js", "Web app"
    elif any(n.startswith("vite.config.") for n in base):
        framework, runs_as = "Vite", "Web app"
    elif "angular.json" in base:
        framework, runs_as = "Angular", "Web app"
    elif "manage.py" in shallow_names:
        framework, runs_as = "Django", "Web app"
    elif "index.html" in shallow_names:
        runs_as = "Web page"

    tests = any(_TEST_PATH.search(f) for f in files) if files else None
    return {"language": lang, "framework": framework, "build": build, "tests": tests,
            "runs_as": runs_as}


# ── a repository's file list ────────────────────────────────────────────────────────────────────

def _indexed_paths(connection: Connection, project_id: str, full_name: str, limit: int = 5000) -> list[str]:
    """The files the code graph holds for this repository in this project (empty until mapped)."""
    rows = connection.execute(text(
        "SELECT name FROM kg_nodes WHERE project_id = :p AND repo = :r AND kind = 'file' "
        "AND valid_to IS NULL LIMIT :n"), {"p": project_id, "r": full_name, "n": limit}).scalars().all()
    return [str(r) for r in rows if r]


def _read_token(connection: Connection, org_id: str, full_name: str) -> Optional[str]:
    """A credential that can READ this one repository: an installation token minted for it alone,
    else the org's token when it reaches it. The registry's own probe takes the same path."""
    from ..identity import github_app, github_setup, vault
    from ..multirepo.common import norm_repo

    if github_setup.app_ready(connection, org_id):
        try:
            tok = github_app.mint_installation_token([full_name], {"contents": "read", "metadata": "read"},
                                                     db=connection)
            github_setup._account_for(tok)  # noqa: SLF001 — revoked like every control-plane token
            return tok.token
        except Exception:  # noqa: BLE001 — not reachable by the App
            pass
    org = github_setup.org_token(connection, org_id)
    if org is not None and norm_repo(full_name) in {norm_repo(r) for r in org["repos"]}:
        return vault.org_github_token(org["id"], db=connection)
    return None


def github_paths(connection: Connection, org_id: str, full_name: str, branch: str) -> list[str]:
    """The repository's paths at `branch`, from GitHub's tree (one call; a truncated tree is still
    plenty to tell a build file). Empty when GitHub cannot be asked."""
    from ..identity import github_app

    token = _read_token(connection, org_id, full_name)
    if not token:
        return []
    resp = github_app._call("GET", f"/repos/{full_name}/git/trees/{branch}?recursive=1",  # noqa: SLF001
                            token=token)
    if resp.status != 200:
        return []
    tree = (resp.json() or {}).get("tree") or []
    return [str(t.get("path")) for t in tree if isinstance(t, dict) and t.get("type") == "blob"]


# ── mapping progress ───────────────────────────────────────────────────────────────────────────

def mapping(item: Optional[dict[str, Any]]) -> dict[str, Any]:
    """How far the code is mapped, from one repository's index line (repo_index.status)."""
    if not item:
        return {"state": "not_started", "percent": 0, "done": False}
    state = str(item.get("status") or "pending")
    files, parsed = int(item.get("files") or 0), int(item.get("parsed") or 0)
    nodes = int(((item.get("graph") or {}).get("nodes")) or 0)
    if state == "indexed" or (state not in ("queued", "running", "pending") and nodes > 0):
        return {"state": "done", "percent": 100, "done": True}
    if state == "running":
        pct = min(99, round(parsed * 100 / files)) if files else 5
        return {"state": "running", "percent": pct, "done": False}
    if state in ("failed", "too_large"):
        return {"state": "failed", "percent": 0, "done": False, "error": item.get("error") or state}
    return {"state": "queued", "percent": 0, "done": False}


def repo_facts(connection: Connection, project_id: str, repo_id: str, *,
               paths: Optional[list[str]] = None) -> dict[str, Any]:
    """What BOSS reads from one registry repository (Import › BOSS reads it): its branch and what
    it is, and how far its code is mapped when this project already has it. `paths` overrides the
    file list (tests); otherwise the project's code graph, else GitHub's tree."""
    from ..multirepo import registry
    from ..multirepo.common import org_of_project
    from .knowledge import repo_index

    org_id = org_of_project(connection, project_id)
    row = registry.get_row(connection, org_id, repo_id)
    full_name = str(row["full_name"])
    branch = str(row.get("default_branch") or "main")
    item = next((r for r in repo_index.status(connection, project_id)["repositories"]
                 if str(r.get("repo") or "").lower() == full_name.lower()), None)
    source = "given"
    if paths is None:
        paths = _indexed_paths(connection, project_id, full_name)
        source = "graph"
        if not paths:
            try:
                paths = github_paths(connection, org_id, full_name, branch)
                source = "github"
            except Exception:  # noqa: BLE001 — facts are a help, never a failed page
                paths = []
    found = detect(paths or [], language=row.get("language"))
    return {"repo_id": row["id"], "full_name": full_name, "branch": branch,
            "description": row.get("description"), "private": row.get("private"),
            "facts": found, "read_from": source if paths else None,
            "mapping": mapping(item), "in_project": item is not None,
            "suggested_name": suggested_name(full_name)}


def suggested_name(full_name: str) -> str:
    """A project name from a repository: `acme/order-service` -> `Order service`."""
    tail = str(full_name or "").rsplit("/", 1)[-1]
    words = re.sub(r"[-_.]+", " ", tail).strip()
    return (words[:1].upper() + words[1:])[:120] if words else "My project"


# ── which workflow Start runs ──────────────────────────────────────────────────────────────────

def _mapped(connection: Connection, project_id: str, work_class: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT d.workflow_key, d.name, v.spec FROM workflow_mappings m "
        "JOIN workflow_versions v ON v.id = m.workflow_version_id "
        "JOIN workflow_definitions d ON d.id = v.workflow_definition_id "
        "WHERE m.project_id = :p AND m.selector_kind = 'work_class' AND m.selector_value = :c "
        "AND m.enabled ORDER BY m.priority DESC LIMIT 1"), {"p": project_id, "c": work_class}).mappings().first()
    return dict(row) if row else None


def _installed(connection: Connection, project_id: str, workflow_key: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT d.workflow_key, d.name, v.spec FROM workflow_definitions d "
        "JOIN workflow_versions v ON v.id = d.active_version_id "
        "WHERE d.project_id = :p AND d.workflow_key = :k AND d.status = 'active' LIMIT 1"),
        {"p": project_id, "k": workflow_key}).mappings().first()
    return dict(row) if row else None


def how_to_start(connection: Connection, project_id: str, *, has_repo: bool) -> dict[str, Any]:
    """What Start sends: a project with a repository adds a feature to it (the project's mapped
    small-feature workflow, else Minor Feature); one without builds a new app (its project.new
    default). Exactly one of `work_class` / `workflow_key` is set — the start door's two ways."""
    from . import workflow_capabilities

    mode = "feature" if has_repo else "new_app"
    work_class = FEATURE_CLASS if has_repo else NEW_APP_CLASS
    chosen = _mapped(connection, project_id, work_class)
    out: dict[str, Any] = {"mode": mode, "work_class": None, "workflow_key": None}
    if chosen:
        out["work_class"] = work_class
    elif has_repo:
        chosen = _installed(connection, project_id, FEATURE_WORKFLOW)
        if chosen:
            out["workflow_key"] = FEATURE_WORKFLOW
    if not chosen:
        # Nothing to name: the start door answers with the honest 409 about the missing mapping.
        out["work_class"] = work_class
        out.update(workflow_name=None, minutes=None, cost=None)
        return out
    spec = chosen.get("spec") or {}
    out["workflow_name"] = chosen["name"]
    out["resolved_key"] = chosen["workflow_key"]
    out["minutes"] = workflow_capabilities.estimate_minutes(spec) if isinstance(spec, dict) else None
    out["cost"] = _cost(connection, project_id, str(chosen["workflow_key"]))
    return out


def _cost(connection: Connection, project_id: str, workflow_key: str) -> Optional[dict[str, Any]]:
    try:
        from .workflow_costs import workflow_costs

        with connection.begin_nested():
            return workflow_costs(connection, project_id).get("costs", {}).get(workflow_key)
    except Exception:  # noqa: BLE001 — an estimate is a help, never a failed page
        return None


# ── the work in this project ───────────────────────────────────────────────────────────────────

def _work(connection: Connection, project_id: str, limit: int = 8) -> list[dict[str, Any]]:
    """Each piece of work (a product workflow run), newest first, with its state in words, a tone,
    and the one thing to do: review a plan, open a PR, watch it, or open it."""
    runs = [dict(r) for r in connection.execute(text(
        "SELECT r.id, r.status, r.created_at, r.input->>'title' AS title, d.name AS workflow, "
        "d.workflow_key FROM workflow_runs r JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
        "WHERE r.project_id = :p AND d.namespace = 'product' ORDER BY r.created_at DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()]
    if not runs:
        return []
    ids = [r["id"] for r in runs]
    gates: dict[str, str] = {}
    for g in connection.execute(text(
            "SELECT s.workflow_run_id, s.step_id FROM workflow_step_runs s "
            "JOIN decisions dd ON dd.id = s.decision_id "
            "WHERE s.workflow_run_id = ANY(:ids) AND s.status = 'waiting' AND dd.status = ANY(:open) "
            "ORDER BY s.created_at"), {"ids": ids, "open": list(_OPEN_GATE)}).mappings().all():
        gates.setdefault(str(g["workflow_run_id"]), str(g["step_id"]))
    prs: dict[str, str] = {}
    for p in connection.execute(text(
            "SELECT workflow_run_id, output->>'pull_request_url' AS url FROM workflow_step_runs "
            "WHERE workflow_run_id = ANY(:ids) AND coalesce(output->>'pull_request_url', '') <> '' "
            "ORDER BY completed_at DESC NULLS LAST"), {"ids": ids}).mappings().all():
        prs.setdefault(str(p["workflow_run_id"]), str(p["url"]))
    out = []
    for r in runs:
        rid, status = str(r["id"]), str(r["status"])
        gate, pr = gates.get(rid), prs.get(rid)
        if gate:
            plan = gate in _PLAN_STEPS
            state, tone = ("Waiting for your plan approval" if plan else "Waiting for your approval"), "amber"
            action = {"kind": "approve", "label": "Review plan" if plan else "Review"}
        elif pr and status not in ("failed", "deadlettered", "cancelled"):
            state, tone = "Pull request ready", "green"
            action = {"kind": "pr", "label": "Open PR", "url": pr}
        elif status in _LIVE_RUN:
            state, tone = "Working", "blue"
            action = {"kind": "watch", "label": "Watch"}
        elif status == "completed":
            state, tone = "Done", "green"
            action = {"kind": "open", "label": "Open"}
        elif status == "cancelled":
            state, tone = "Stopped", "grey"
            action = {"kind": "open", "label": "Open"}
        else:
            state, tone = "Needs a look", "red"
            action = {"kind": "open", "label": "Open"}
        out.append({"run_id": rid, "title": r["title"] or r["workflow"], "workflow": r["workflow"],
                    "workflow_key": r["workflow_key"], "status": status, "state": state, "tone": tone,
                    "action": action, "pull_request_url": pr,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None})
    return out


# ── the project page ───────────────────────────────────────────────────────────────────────────

def view(connection: Connection, project_id: str) -> dict[str, Any]:
    """The project page's one read (GET /v1/projects/{id}/start). Raises LookupError for no such
    project."""
    from .knowledge import repo_index

    project = connection.execute(text(
        "SELECT id, name, description, project_type, archived_at FROM projects WHERE id = :p"),
        {"p": project_id}).mappings().first()
    if project is None:
        raise LookupError(f"There is no project {project_id}.")
    bound = connection.execute(text(
        "SELECT full_name, default_branch FROM github_repository_bindings WHERE project_id = :p "
        "AND status = 'active' ORDER BY (role = 'primary') DESC, created_at LIMIT 1"),
        {"p": project_id}).mappings().first()
    repo = None
    if bound:
        full_name = str(bound["full_name"])
        item = next((r for r in repo_index.status(connection, project_id)["repositories"]
                     if str(r.get("repo") or "").lower() == full_name.lower()), None)
        repo = {"full_name": full_name, "branch": bound["default_branch"] or (item or {}).get("branch") or "main",
                "mapping": mapping(item)}
    return {
        "project_id": project["id"], "name": project["name"], "description": project["description"],
        "project_type": project["project_type"] or "app", "archived": project["archived_at"] is not None,
        "repo": repo,
        "start": how_to_start(connection, project_id, has_repo=repo is not None),
        "examples": list(EXAMPLES), "next": [dict(s) for s in NEXT_STEPS],
        "work": _work(connection, project_id),
        "sample": {"template": SAMPLE_TEMPLATE, "name": SAMPLE_NAME, "stack": SAMPLE_STACK,
                   "workflow_key": SAMPLE_WORKFLOW},
    }


__all__ = ["EXAMPLES", "FEATURE_CLASS", "FEATURE_WORKFLOW", "NEXT_STEPS", "SAMPLE_NAME",
           "SAMPLE_STACK", "SAMPLE_TEMPLATE", "SAMPLE_WORKFLOW", "detect", "how_to_start",
           "mapping", "repo_facts", "suggested_name", "view"]
