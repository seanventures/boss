"""No host deploy of an app built from an `insecure_by_design` template (APP_TEMPLATES_AND_SECURITY_LOOP.md
§3.6: "a hard check in governed_host_deploy.py, not a UI hide").

The insecure banking variant carries planted, catalogued flaws for training: it is previewed, never
deployed. The check is PER APP (operator decision, 2026-10-09): a lab project may hold the insecure
app AND ordinary apps (the "Bank attacker" that tests it), and the ordinary ones deploy.

"Per app" must not be sidestepped by naming a target freely, so in a project that holds an insecure
application (any state but `abandoned`) a deploy is refused unless BOSS can tell it ships an app that
is NOT insecure. Refused when:

1. any app it can be attributed to is an insecure app: the target's app (its `app_name`, resolved the
   way every app view resolves an app: key, name or run id), the build it deploys (the release
   packet's `code_task_run_id`, a stack deploy's `run_id`) or the branch it deploys (a build's
   `boss/code/<root run>` branch);
2. it cannot be attributed to any app of the project (a target named after nothing BOSS built, and no
   build or build branch given), or it names a build or build branch BOSS cannot place: BOSS cannot
   tell, so it does not deploy;
3. it deploys from the repository an insecure app builds into, and either that app's change was
   merged (its code is on the default branch now) or the ref is not one BOSS can place (a bare sha or
   an unknown branch could be the insecure app's commit). The default branch while the insecure
   change is unmerged, and another app's own build branch, are fine.

`deploy_refusal` is called by every deploy planner (resource_plane.plan_deployment_run,
governed_host_deploy.plan_host_deployment, stack_deploy.plan_stack_deploy) and again when a
governed host deploy applies, so an approval recorded before the template was applied still cannot
ship it. It fails CLOSED: if BOSS cannot read the applications, it refuses.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .manifest import INSECURE_BY_DESIGN

ACTION = ("That app is a training app built from a template that is insecure by design (planted flaws): "
          "preview it, never deploy it. The project's other apps deploy from their own targets.")
ACTION_UNATTRIBUTED = ("Name the deploy target after the app it deploys (as Factory › Apps names it), or "
                       "deploy from that app's build, so BOSS can tell it is not the insecure app.")
ACTION_SHARED_REPO = ("Deploy the other app from its own build branch, or keep the training app in a "
                      "repository of its own.")

_BUILD_BRANCH = re.compile(r"^(?:refs/heads/)?boss/code/([A-Za-z0-9_-]+)$")
_DEFAULT_REFS = {"", "main", "master", "head", "refs/heads/main", "refs/heads/master"}


class Refusal(str):
    """The headline (a str, as every caller uses it) plus the action that goes with it."""

    action: str = ACTION

    def __new__(cls, headline: str, action: str = ACTION) -> "Refusal":
        made = super().__new__(cls, headline)
        made.action = action
        return made


def insecure_applications(connection: Any, project_id: str) -> list[dict]:
    rows = connection.execute(text(
        "SELECT a.app_key, a.status, a.code_task_run_id, t.template_key, t.version "
        "FROM app_template_applications a JOIN app_templates t ON t.id = a.template_id "
        "WHERE a.project_id = :p AND a.status <> 'abandoned' "
        "AND COALESCE(t.manifest -> 'labels', '[]'::jsonb) ? :label "
        "ORDER BY a.created_at"), {"p": project_id, "label": INSECURE_BY_DESIGN}).mappings().all()
    return [dict(r) for r in rows]


def _repo_name(raw: Any) -> str:
    """A repository's name, comparable across the forms BOSS stores (owner/name, a URL, .git)."""
    s = str(raw or "").strip().lower().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return s.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _run_repos(connection: Any, project_id: str, run_ids: set[str]) -> set[str]:
    if not run_ids:
        return set()
    rows = connection.execute(text(
        "SELECT spec->>'repo' FROM code_task_runs WHERE project_id = :p AND id = ANY(:ids)"),
        {"p": project_id, "ids": sorted(run_ids)}).all()
    return {_repo_name(r) for (r,) in rows if r}


def _merged(connection: Any, project_id: str, app_keys: set[str]) -> bool:
    from .apply import refresh_statuses

    for key in sorted(app_keys):
        refresh_statuses(connection, project_id, key)
    return bool(connection.execute(text(
        "SELECT 1 FROM app_template_applications a JOIN app_templates t ON t.id = a.template_id "
        "WHERE a.project_id = :p AND a.app_key = ANY(:k) AND a.status = 'applied' "
        "AND COALESCE(t.manifest -> 'labels', '[]'::jsonb) ? :label LIMIT 1"),
        {"p": project_id, "k": sorted(app_keys), "label": INSECURE_BY_DESIGN}).first())


def _decide(connection: Any, project_id: str, found: list[dict], *, target: Optional[Mapping[str, Any]],
            commit_ref: Optional[str], source_run_ids: list[str]) -> Optional[Refusal]:
    from ..app_links.service import app_index, resolve_app_key

    first = found[0]
    insecure = {str(a["app_key"]) for a in found}
    where = (target or {}).get("repository") or (target or {}).get("repo") or "its repository"

    def named(key: str) -> Refusal:
        app = next((a for a in found if a["app_key"] == key), first)
        return Refusal(f"{key} was built from {app['template_key']} {app['version']}, a template that is "
                       "insecure by design: BOSS never deploys it to a host.")

    def cannot_tell(what: str) -> Refusal:
        return Refusal(f"BOSS cannot tell which app {what}, and this project holds {first['app_key']}, "
                       "an app that is insecure by design.", ACTION_UNATTRIBUTED)

    if target is None:  # nothing to attribute: refused while the project holds an insecure app
        return named(str(first["app_key"]))

    from ..services.agent_assignment import app_key

    by_run, _names = app_index(connection, project_id)
    attributed: set[str] = set()
    named_as = str(target.get("app_name") or "")
    if app_key(named_as) in insecure:   # by its name even when its builds are past the app index
        return named(app_key(named_as))
    target_app = resolve_app_key(connection, project_id, named_as)
    if target_app:
        attributed.add(target_app)
    for run_id in source_run_ids:
        if run_id not in by_run:
            return cannot_tell(f"build {run_id} belongs to")
        attributed.add(by_run[run_id])
    ref = str(commit_ref or "").strip()
    branch = _BUILD_BRANCH.match(ref)
    ref_app = None
    if branch:
        ref_app = by_run.get(branch.group(1))
        if ref_app is None:
            return cannot_tell(f"branch {ref} belongs to")
        attributed.add(ref_app)

    hit = sorted(attributed & insecure)
    if hit:
        return named(hit[0])
    if not attributed:
        return cannot_tell(f"target {target.get('app_name')!r} deploys")

    # The repository the insecure app builds into holds its code on its branch, and on the default
    # branch once merged. A repository BOSS cannot read is assumed to be this one (fail closed).
    insecure_runs = {r for r, k in by_run.items() if k in insecure}
    insecure_runs |= {str(a["code_task_run_id"]) for a in found if a.get("code_task_run_id")}
    repos = _run_repos(connection, project_id, insecure_runs)
    if repos and _repo_name(target.get("repository") or target.get("repo")) not in repos:
        return None
    if _merged(connection, project_id, insecure):
        return Refusal(f"{first['app_key']}'s change from {first['template_key']} (insecure by design) was "
                       f"merged into {where}, so anything deployed from that repository may carry it.",
                       ACTION_SHARED_REPO)
    if ref_app or ref.lower() in _DEFAULT_REFS:
        return None
    return Refusal(f"BOSS cannot place {ref} in {where}, which also holds {first['app_key']}, an app that "
                   "is insecure by design.", ACTION_SHARED_REPO)


def deploy_refusal(connection: Any, project_id: Optional[str], *,
                   target: Optional[Mapping[str, Any]] = None, commit_ref: Optional[str] = None,
                   release_packet: Optional[Mapping[str, Any] | str] = None,
                   source_run_id: Optional[str] = None) -> Optional[Refusal]:
    """The refusal (a str headline with `.action`) for this deploy, or None when it may deploy.

    `target` is the deployment target row (a stack deploy sets `repository` on a copy when it deploys
    another repository); without one the whole project is refused while it holds an insecure app.
    """
    if not project_id:
        return None
    try:
        packet = json.loads(release_packet) if isinstance(release_packet, str) else (release_packet or {})
        runs = [str(r) for r in (packet.get("code_task_run_id"), source_run_id) if r]
        with connection.begin_nested():
            found = insecure_applications(connection, str(project_id))
            if not found:
                return None
            return _decide(connection, str(project_id), found, target=target, commit_ref=commit_ref,
                           source_run_ids=runs)
    except Exception as exc:  # noqa: BLE001 — unknown is refused, never shipped
        return Refusal("BOSS could not check this project's templates for one that is insecure by design "
                       f"({type(exc).__name__}), so it does not deploy.")
