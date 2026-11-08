"""Project › Settings (contract §11): the project's own settings in one place, each section
listing what is set (names only, never a secret value) and, as `actions`, the EXISTING write
endpoint that changes it. No new write path: the old screens keep working and link here.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import captain as captain_service
from . import demo_logins as demo
from .common import as_json, href, iso, project_href, repos
from .deployments import host_label
from .health import env_label

STEP_UP = "step_up"


def _action(label: str, method: str, path: str, *, requires: str = "", body: Any = None,
            permission: str = "", open_href: str = "") -> dict[str, Any]:
    """An existing write route (`href`, `method`, `body`) and, as `open`, the console screen where
    a person makes that change with its own form today (a console link; the page never sends the
    write itself from here)."""
    out: dict[str, Any] = {"label": label, "method": method, "href": path}
    if open_href:
        out["open"] = open_href
    if requires:
        out["requires"] = requires
    if permission:
        out["permission"] = permission
    if body is not None:
        out["body"] = body
    return out


#: Where the demo users' names came from (§11 `users_from`), in words for the footnote.
USERS_FROM_WORDS = {"manifest": "boss.deploy.yaml", "repository": "the app's seed",
                    "none": ""}
#: The demo-login environment switch (screen 13): Staging, Preview, Prod — always the three.
_SWITCH = (("staging", "Staging"), ("preview", "Preview"), ("production", "Prod"))


def _switch_key(env: str) -> str:
    key = str(env or "").lower()
    if key in ("prod", "production"):
        return "production"
    if key == "preview":
        return "preview"
    return "staging"


def _demo_section(connection: Connection, project_id: str) -> dict[str, Any]:
    """Demo logins per environment (13-02…13-07): who signs in, the reveal (owner/admin with
    step-up, audited), and Rotate — one generated password per environment."""
    listing = demo.listing(connection, project_id)
    items = []
    by_switch: dict[str, dict[str, Any]] = {}
    for env in listing["environments"]:
        for row in env["logins"]:
            items.append({"env": env["key"], "env_label": env["label"], "user": row["user"],
                          "label": row["label"], "password_key": row["password_key"],
                          "reveal": row["reveal"]})
        slot = by_switch.setdefault(_switch_key(env["key"]), {"envs": [], "logins": []})
        slot["envs"].append(env["key"])
        slot["logins"].extend(env["logins"])
    environments = []
    for key, label in _SWITCH:
        slot = by_switch.get(key)
        environments.append({
            "key": key, "label": label, "env": (slot or {}).get("envs", [None])[0],
            "logins": (slot or {}).get("logins", []),
            "empty": None if slot else (
                "No production yet: demo logins appear once production is deployed."
                if key == "production" else
                "No preview yet: the first build's preview generates its password."
                if key == "preview" else
                "Not deployed yet: staging's password is generated on the first deploy."),
            "rotate": ({"label": "Rotate", "method": "POST",
                        "href": f"/v1/projects/{project_id}/demo-logins/rotate?env="
                                f"{(slot or {}).get('envs', [key])[0]}",
                        "requires": STEP_UP, "permission": "secret:manage"} if slot else None)})
    return {"key": "demo_logins", "label": "Demo logins", "icon": "key", "items": items,
            "users_from": listing["source"],
            "users_from_words": USERS_FROM_WORDS.get(listing["source"], ""),
            "environments": environments,
            "hint": None if listing["users"] else
            "No demo users found · add demo_logins: to boss.deploy.yaml",
            "actions": [_action(f"Reveal {e['label']}", "GET", demo.reveal_href(project_id, e["key"]),
                                requires=STEP_UP, permission="secret:manage")
                        for e in listing["environments"]]}


def _ago(value: Any) -> str:
    from .common import age

    got = age(value)
    return f"{got} ago" if got else ""


def _required_names(connection: Connection, project_id: str) -> dict[str, set[str]]:
    """name → the environments whose deploy needs it: each active target's newest manifest
    (`secret_env_keys`, `app_secrets`), and the onboarding agent's "yours" secrets for staging."""
    out: dict[str, set[str]] = {}
    for r in connection.execute(text(
            "SELECT t.environment, s.manifest FROM deployment_targets t LEFT JOIN LATERAL ("
            " SELECT manifest FROM stack_deployments WHERE target_id = t.id "
            " ORDER BY created_at DESC LIMIT 1) s ON TRUE "
            "WHERE t.project_id = :p AND t.status = 'active'"), {"p": project_id}).mappings():
        m = as_json(r["manifest"], {}) or {}
        for name in list(m.get("secret_env_keys") or []) + list(m.get("app_secrets") or []):
            out.setdefault(str(name), set()).add(str(r["environment"] or "staging"))
    found = connection.execute(text(
        "SELECT facts FROM project_onboarding_runs WHERE project_id = :p "
        "ORDER BY started_at DESC LIMIT 1"), {"p": project_id}).scalar()
    for name in ((as_json(found, {}) or {}).get("secrets") or {}).get("yours") or []:
        out.setdefault(str(name), set()).add("staging")
    return out


def _env_words(envs: list[str]) -> str:
    words = sorted({_values_env_label(e).lower() for e in envs})
    return "per env" if len(words) > 1 else (words[0] if words else "staging")


def _deploy_values(connection: Connection, project_id: str) -> dict[str, Any]:
    """ONE ROW PER VALUE NAME (13-08…13-13): DEMO_PASSWORD was listed four times (once per app
    key, each a build's lowercase title). Each row says who manages it and where, in words —
    "BOSS manages · staging", "BOSS manages · per env", "you set · rotated 12d ago",
    "you set · missing in prod" — with its write-only actions (Set, Rotate, Remove) naming the
    existing routes. Never a value."""
    rows = connection.execute(text(
        "SELECT app_key, env, name, mode, set_by, set_at FROM app_secret_bindings "
        "WHERE project_id = :p AND env <> 'build' ORDER BY name, env, app_key"),
        {"p": project_id}).mappings().all()
    apps = sorted({str(r["app_key"]) for r in rows}) or _project_apps(connection, project_id)
    required = _required_names(connection, project_id)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_name.setdefault(str(r["name"]), []).append(dict(r))
    items: list[dict[str, Any]] = []
    for name, bindings in sorted(by_name.items()):
        generated = all(b["mode"] == "generated" for b in bindings)
        envs = [str(b["env"]) for b in bindings]
        newest = max(bindings, key=lambda b: b["set_at"] or 0)
        bound_envs = {_values_env_label(e).lower() for e in envs}
        missing = sorted(e for e in required.get(name, set())
                         if _values_env_label(e).lower() not in bound_envs
                         and not ("deploy" in envs and e not in ("production", "prod")))
        app_key = str(newest["app_key"])
        deploy_env = "deploy" if "deploy" in envs else envs[0]
        put = f"/v1/apps/{app_key}/secrets/{name}"
        item: dict[str, Any] = {
            "name": name, "app_key": app_key, "apps": sorted({str(b["app_key"]) for b in bindings}),
            "envs": envs, "env": deploy_env, "env_label": _values_env_label(deploy_env),
            "count": len(bindings), "mode": "generated" if generated else newest["mode"],
            "set": True, "set_at": iso(newest["set_at"]),
            "managed_by": "boss" if generated else "you",
            "tag": "generated" if generated else None,
            "state": "you" if missing else "done", "missing_in": missing,
            "words": (f"BOSS manages · {_env_words(envs)}" if generated else
                      f"you set · missing in {_values_env_label(missing[0]).lower()}" if missing
                      else f"you set · rotated {_ago(newest['set_at'])}".rstrip()),
            "actions": [_action("Rotate" if not missing else "Set", "PUT", put, requires=STEP_UP,
                                permission="app_secret:write",
                                body={"project_id": project_id, "env": deploy_env,
                                      "mode": "generated" if generated else "test"}),
                        _action("Remove", "DELETE", f"{put}?project_id={project_id}&env="
                                f"{deploy_env}", requires=STEP_UP, permission="app_secret:write")]}
        items.append(item)
    for name, envs_needed in sorted(required.items()):
        if name in by_name:
            continue
        first = sorted(envs_needed)[0]
        app_key = apps[0] if apps else None
        items.append({"name": name, "app_key": app_key, "apps": [], "envs": [], "env": "deploy",
                      "env_label": _values_env_label("deploy"), "count": 0, "mode": None,
                      "set": False, "needed": True, "managed_by": "you", "tag": None,
                      "state": "you", "missing_in": sorted(envs_needed),
                      "words": f"you set · missing in {_values_env_label(first).lower()}",
                      "actions": ([_action("Set", "PUT", f"/v1/apps/{app_key}/secrets/{name}",
                                           requires=STEP_UP, permission="app_secret:write",
                                           body={"project_id": project_id, "env": "deploy",
                                                 "mode": "test"})] if app_key else [])})
    items.sort(key=lambda i: (i["managed_by"] != "boss", i["name"]))
    # The app's setup sheet (Code Tasks › Apps) keeps working for the same values.
    where = href(project_id, area="code_tasks", view="apps")
    for item in items:
        item["href"] = where
    return {"key": "deploy_values", "label": "Deploy values", "icon": "lock", "items": items,
            "apps": apps,
            "add": ({"label": "Add value", "method": "PUT",
                     "href": f"/v1/apps/{apps[0]}/secrets/{{name}}", "requires": STEP_UP,
                     "permission": "app_secret:write",
                     "body": {"project_id": project_id, "env": "deploy", "mode": "test"}}
                    if apps else None),
            "actions": [_action("Open app secrets", "GET", f"/v1/apps/{a}/secrets?project_id="
                                f"{project_id}", permission="app_secret:read_metadata",
                                # PX-43: the app's secrets now live HERE (Settings › Deploy
                                # values); the link lands on that exact section.
                                open_href=href(project_id, area="project", tab="settings",
                                               section="deploy_values"))
                        for a in apps]}


def _project_apps(connection: Connection, project_id: str) -> list[str]:
    """The app a value belongs to when none is bound yet: its deploy target's app (the route
    canonicalises the key, app_links.canonical_app_key), else the project itself."""
    got = connection.execute(text(
        "SELECT DISTINCT app_name FROM deployment_targets WHERE project_id = :p "
        "AND status = 'active' AND app_name IS NOT NULL ORDER BY app_name"),
        {"p": project_id}).scalars().all()
    return [str(a) for a in got] or [project_id]


def _values_env_label(env: Any) -> str:
    """A deploy value's environment in words: `deploy` is what staging and the deploy targets
    run with, `preview` the per-build preview."""
    key = str(env or "deploy").lower()
    return {"deploy": "Staging", "preview": "Preview"}.get(key) or env_label(key)


def _port_of(url: Any) -> str:
    import re as _re

    found = _re.search(r":(\d{2,5})(?:/|$)", str(url or ""))
    return found.group(1) if found else ""


def _targets(connection: Connection, project_id: str) -> dict[str, Any]:
    """This project's targets (13-14…13-16): "Staging ✓ docker · port 8004" with the server it
    runs on (its name; the address opens it in Infra), and Production "! no host yet · Pick host"
    while it has none. The verified hosts it could use are listed too (data), never as titles
    by bare address."""
    from ..deployment_fleet import usable_hosts_sql

    rows = connection.execute(text(
        "SELECT t.id, t.app_name, t.environment, t.auto_deploy, t.paused, t.host_id, t.target_kind, "
        "t.public_url, t.domains, h.name AS host_name, h.address AS host_address, "
        "(SELECT s.manifest->>'kind' FROM stack_deployments s WHERE s.target_id = t.id "
        " ORDER BY s.created_at DESC LIMIT 1) AS stack_kind "
        "FROM deployment_targets t "
        "LEFT JOIN deployment_hosts h ON h.id = t.host_id WHERE t.project_id = :p "
        "AND t.status = 'active' ORDER BY (lower(t.environment) IN ('production', 'prod')), "
        "t.environment, t.app_name"),
        {"p": project_id}).mappings().all()
    hosts = connection.execute(text(
        f"SELECT id, name, address, status, project_id FROM deployment_hosts WHERE "
        f"status <> 'retired' AND {usable_hosts_sql()} ORDER BY name"),
        {"p": project_id}).mappings().all()
    items = []
    for r in rows:
        domains = r["domains"] if isinstance(r["domains"], list) else []
        port = _port_of(r["public_url"] or (domains[0] if domains else ""))
        kind = "docker" if (r["stack_kind"] or "") in ("compose", "docker", "container") or \
            r["target_kind"] in ("host", "docker", "container") else str(r["target_kind"] or "")
        items.append({
            "kind": "target", "id": r["id"], "app": r["app_name"], "env": r["environment"],
            "env_label": env_label(r["environment"]), "host": r["host_address"] or r["host_name"],
            "host_name": host_label(r["host_name"], r["host_address"], r["environment"]),
            "host_href": href(project_id, area="infra", host=r["host_id"]) if r["host_id"] else None,
            "words": " · ".join(w for w in (kind, f"port {port}" if port else "",
                                            "paused" if r["paused"] else "") if w),
            "state": "done", "policy": "auto" if r["auto_deploy"] else "person",
            "paused": bool(r["paused"]), "href": href(project_id, area="infra", target=r["id"]),
            "actions": [_action("Edit", "PATCH", f"/v1/deployment-targets/{r['id']}",
                                permission="deployment_target:manage",
                                open_href=href(project_id, area="infra", target=r["id"])),
                        _action("Auto-deploy", "POST",
                                f"/v1/deployment-targets/{r['id']}/auto-deploy",
                                permission="deployment_target:manage",
                                open_href=href(project_id, area="infra", target=r["id"]))]})
    if not any(str(r["environment"] or "").lower() in ("production", "prod") for r in rows):
        items.append({"kind": "missing", "id": "production", "env": "production",
                      "env_label": "Production", "words": "no host yet", "state": "you",
                      "action": {"label": "Pick host",
                                 "open": href(project_id, area="infra", add="target",
                                              env="production")}})
    used = {str(r["host_id"]): r["environment"] for r in rows if r["host_id"]}
    items += [{"kind": "host", "id": h["id"],
               "name": host_label(h["name"], h["address"], used.get(str(h["id"]))),
               "address": h["address"],
               "status": h["status"], "shared": h["project_id"] != project_id,
               "href": href(project_id, area="infra", host=h["id"])} for h in hosts]
    return {"key": "targets", "label": "Targets & hosts", "icon": "server", "items": items,
            "actions": [_action("Add a target", "POST", "/v1/deployment-targets", requires=STEP_UP,
                                permission="deployment_target:manage",
                                body={"project_id": project_id},
                                open_href=href(project_id, area="infra", add="target")),
                        _action("Add a server", "POST", "/v1/deployment-hosts",
                                permission="host:manage", body={"project_id": project_id},
                                open_href=href(project_id, area="infra", add="server"))]}


def _captain(connection: Connection, project_id: str) -> dict[str, Any]:
    cap = captain_service.view(connection, project_id, with_last_said=False)
    return {"key": "captain", "label": "Captain & squad", "icon": "user", "captain": {
        k: cap.get(k) for k in ("agent_id", "name", "role_key", "avatar", "chosen")},
            "items": cap.get("squad") or [], "candidates": cap.get("candidates") or [],
            # Edit squad opens THE SQUAD EDITOR for this project (13-18 / PX-43): Settings ›
            # Captain & squad with the editor open — the project's team (project_agents, app
            # "*"), the same table every new build seeds from — never the org-wide chart.
            "squad_href": href(project_id, area="project", tab="settings", section="captain",
                               edit="squad"),
            "squad_set": {"label": "Edit squad", "method": "POST",
                          "href": f"/v1/projects/{project_id}/agents",
                          "permission": "work_item:manage",
                          "body": {"app": "*", "action": "set", "role_keys": ["…"]}},
            "actions": [_action("Change captain", "POST", f"/v1/projects/{project_id}/captain",
                                requires=STEP_UP, permission="project:update",
                                body={"agent_id": "…"})]}


#: The project's autonomy level in the catalogue's ONE naming (task_classes.LEVEL_NAMES, 13-20);
#: kept as a name for older imports.
from ..ambient.task_classes import LEVEL_NAMES as LEVEL_WORDS  # noqa: E402


def project_level(classes: list[dict[str, Any]]) -> str:
    """ONE level for the project: the level most of its task classes are at (ties → the
    lower, never more autonomy than most classes have)."""
    from collections import Counter

    from ..ambient import task_classes as tc

    if not classes:
        return "A1"
    counts = Counter(c["level"] for c in classes)
    top = max(counts.values())
    return min((lvl for lvl, n in counts.items() if n == top), key=tc.level_rank)


def set_project_level(connection: Connection, project_id: str, level: str, *, actor: str
                      ) -> dict[str, Any]:
    """Set every task class of this project to `level` (13-20), each clamped to what its
    catalogue allows; a class's project ceiling is raised to the level when it was lower (the
    person chose it). Each change is recorded like any level change (ambient_autonomy_changes)."""
    from ..ambient import policy
    from ..ambient import task_classes as tc

    if level not in tc.LEVELS:
        raise ValueError(f"level must be one of {', '.join(tc.LEVELS)}")
    for c in policy.matrix(connection, project_id):
        target = min(level, c["max_ceiling"], key=tc.level_rank)
        ceiling = max(target, c["ceiling"], key=tc.level_rank)
        if c["level"] == target and c["ceiling"] == ceiling:
            continue
        policy.set_level(connection, project_id, c["task_class"], actor=actor, level=target,
                         ceiling=ceiling, reason=f"Project level set to {level} in Settings")
    return _autonomy(connection, project_id)


def _week_ambient_spend(connection: Connection, project_id: str) -> float:
    from ..ambient.policy import BUDGET_CATEGORY
    from ..included_spend import MONEY_SQL

    micros = connection.execute(text(
        f"SELECT COALESCE(sum({MONEY_SQL}), 0) FROM cost_receipts WHERE project_id = :p AND "
        "category = :c AND created_at >= now() - interval '7 days'"),
        {"p": project_id, "c": BUDGET_CATEGORY}).scalar() or 0
    return round(int(micros) / 1_000_000, 2)


def _captain_authority_words(connection: Connection, project_id: str) -> dict[str, Any]:
    """The project's Captain authority, in words: on (and who sponsors it) or off."""
    try:
        from ...identity import captain_authority

        with connection.begin_nested():
            grant = captain_authority.get(connection, project_id)
            if grant is None:
                return {"state": "none", "sponsor": None}
            name = connection.execute(text("SELECT display_name FROM identity_principals WHERE id = :i"),
                                      {"i": grant.user_principal_id}).scalar()
            return {"state": "on" if grant.status.value == "active" else grant.status.value,
                    "sponsor": name or grant.user_principal_id}
    except Exception:  # noqa: BLE001 — identity not set up on this install
        return {"state": "none", "sponsor": None}


def _autonomy(connection: Connection, project_id: str) -> dict[str, Any]:
    """This project's autonomy level (A0 Ask … A3 Ship) and its ambient budget for the week
    (13-19…13-22). Govern keeps the org-wide caps."""
    from ..ambient import policy
    from ..ambient import task_classes as tc

    classes = policy.matrix(connection, project_id)
    level = project_level(classes)
    amb = policy.settings_for(connection, project_id)
    policy_row = connection.execute(text(
        "SELECT daily_cap_usd_micros, monthly_cap_usd_micros, operational_intensity "
        "FROM budget_policies WHERE scope_type = 'project' AND scope_id = :p "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id}).mappings().first()
    autonomy = href(project_id, area="budget", tab="autonomy")
    week_cap = round(int(amb["daily_cap_usd_micros"]) * 7 / 1_000_000, 2)
    items = [{"task_class": c["task_class"], "title": c.get("title"), "level": c["level"],
              "ceiling": c["ceiling"], "set_by": c.get("set_by"), "at": c.get("updated_at"),
              "href": autonomy} for c in classes]
    return {"key": "autonomy", "label": "Autonomy & budget", "icon": "gauge", "items": items,
            "level": level,
            "levels": [{"key": k, "label": f"{k} {tc.LEVEL_NAMES[k]}", "name": tc.LEVEL_NAMES[k],
                        "meaning": tc.LEVEL_MEANINGS[k]}
                       for k in tc.LEVELS],
            "ambient": {"daily_cap_usd": round(int(amb["daily_cap_usd_micros"]) / 1_000_000, 2),
                        "max_concurrent": amb["max_concurrent"],
                        "week_cap_usd": week_cap,
                        "week_spent_usd": _week_ambient_spend(connection, project_id)},
            "budget": ({"daily_cap_usd": round(int(policy_row["daily_cap_usd_micros"] or 0) / 1e6, 2),
                        "monthly_cap_usd": round(int(policy_row["monthly_cap_usd_micros"] or 0) / 1e6, 2),
                        "intensity": policy_row["operational_intensity"]} if policy_row else None),
            "govern_href": href(project_id, area="budget", tab="limits"),
            "review_builds": {**policy.review_builds_state(connection, project_id),
                              "captain_authority": _captain_authority_words(connection, project_id),
                              "set": {"label": "Review always-on builds", "method": "PUT",
                                      "href": "/v1/ambient/review-builds",
                                      "permission": "autonomy:manage"}},
            "set_level": {"label": "Set level", "method": "PUT",
                          "href": f"/v1/projects/{project_id}/autonomy",
                          "permission": "autonomy:manage"},
            "actions": [_action("Ambient budget", "PUT", "/v1/ambient/settings",
                                permission="autonomy:manage", body={"project_id": project_id},
                                open_href=autonomy),
                        _action("Spending cap", "POST", "/v1/budget/set_intensity",
                                permission="budget:manage", body={"project_id": project_id},
                                open_href=href(project_id, area="budget", tab="limits"))]}


def _repos(connection: Connection, project_id: str) -> dict[str, Any]:
    """The project's repositories (13-23/24): name, branch, and access in words."""
    items = []
    setup = href(project_id, area="setup", focus="repository")
    for r in repos(connection, project_id):
        access = r.get("access") or {}
        known = [v for v in access.values() if v is not None]
        ok = bool(known) and all(known)
        items.append({**r, "href": r.get("external_url") or setup,
                      "access_ok": ok if known else None,
                      "access_words": ("access ✓" if ok else "access ✗") if known else "not checked",
                      "actions": [_action(
                          "Remove", "DELETE", f"/v1/projects/{project_id}/repos/{r['binding_id']}",
                          requires=STEP_UP, permission="repository:manage", open_href=setup)]})
    return {"key": "repos", "label": "Repos", "icon": "git-branch", "items": items,
            "actions": [_action("Add", "PUT",
                                f"/v1/projects/{project_id}/repos/{{repo_id}}", requires=STEP_UP,
                                permission="repository:manage",
                                open_href=href(project_id, area="setup", focus="repository",
                                               open="add")),
                        _action("Repository mode", "PUT", f"/v1/projects/{project_id}/repo-mode",
                                permission="repository:manage", open_href=setup)]}


def _notifications(connection: Connection, project_id: str) -> dict[str, Any]:
    """Who is told about what (13-25…13-28): Needs you, Incidents, PR ready → channels
    (notify_routes; Incidents is the alert routing tree's root)."""
    from . import notify_routes

    try:
        with connection.begin_nested():
            routed = notify_routes.view(connection, project_id)
    except Exception:  # noqa: BLE001 — a read-only reader (or an old schema) still gets a section
        routed = {"routes": [], "contact_points": []}
    notify = href(project_id, area="observability", view="notify")
    return {"key": "notifications", "label": "Notifications", "icon": "bell",
            "items": routed["routes"], "routes": routed["routes"],
            "contact_points": routed["contact_points"],
            "actions": [_action("Contact points", "GET", "/v1/notification-policies",
                                permission="observability:manage", open_href=notify)],
            "href": notify}


def rotate_demo_logins(connection: Connection, settings: Any, *, org_id: str, project_id: str,
                       env: str, actor: str) -> dict[str, Any]:
    """ROTATE one environment's demo-login password (13-06): a new value BOSS generates for
    every demo-login name that environment runs with — the app's generated bindings (preview,
    or deploy for staging/production) and each deploy target's stack-generated secret. Values
    are never returned; the next deploy (or preview) signs the demo users in with the new one.
    Audited as `project.demo_logins_rotated`."""
    from ...templates import secrets as app_secrets
    from ...templates.run_env import is_demo_login_name
    from ..core import emit_event
    from ..secret_backends import durable_secret_backend
    from ..secrets_service import put_secret, ref_key_for
    from ..stack_deploy import _GENERATE, _find_secret_ref, _generated_secret_name

    env = str(env or "").strip().lower()
    known = {e["key"]: e for e in demo.environments(connection, project_id)}
    if env not in known:
        raise LookupError(f"No demo login was generated for {env or 'that environment'}.")
    binding_env = "preview" if env == "preview" else "deploy"
    rotated = 0
    for r in connection.execute(text(
            "SELECT app_key, name FROM app_secret_bindings WHERE project_id = :p AND env = :e "
            "AND mode = 'generated'"), {"p": project_id, "e": binding_env}).mappings().all():
        if not is_demo_login_name(str(r["name"])):
            continue
        def regenerate(r=r) -> None:
            app_secrets.put(connection, org_id=org_id, project_id=project_id,
                            app_key=r["app_key"], name=r["name"], env=binding_env,
                            mode="generated", value=None, actor_id=actor,
                            spec=app_secrets.SecretSpec(
                                name=r["name"], purpose="The app's demo login password"),
                            may_live=False)
        try:
            with connection.begin_nested():
                regenerate()
        except Exception:  # noqa: BLE001 — the old value's vault item is gone: nothing to revoke
            connection.execute(text(
                "UPDATE app_secret_bindings SET vault_item_id = NULL WHERE project_id = :p "
                "AND app_key = :a AND env = :e AND name = :n"),
                {"p": project_id, "a": r["app_key"], "e": binding_env, "n": r["name"]})
            regenerate()
        rotated += 1
    redeploy = False
    if env != "preview":
        for t in connection.execute(text(
                "SELECT t.id, s.manifest FROM deployment_targets t JOIN LATERAL (SELECT manifest "
                "FROM stack_deployments WHERE target_id = t.id AND status = 'healthy' "
                "ORDER BY created_at DESC LIMIT 1) s ON TRUE WHERE t.id = ANY(:ids)"),
                {"ids": known[env].get("target_ids") or []}).mappings().all():
            manifest = as_json(t["manifest"], {}) or {}
            stack = str(manifest.get("name") or "")
            for key in manifest.get("generated_env_keys") or []:
                name = _generated_secret_name(stack, str(key))
                if not stack or not is_demo_login_name(str(key)) or not _find_secret_ref(
                        connection, project_id, [ref_key_for(t["id"], name)]):
                    continue
                stored = put_secret(connection, settings, project_id=project_id, name=name,
                                    value=_GENERATE["password"](), secret_kind="api_token",
                                    backend=durable_secret_backend(settings),
                                    scope_type="deployment_target", scope_id=t["id"], actor=actor)
                connection.execute(text(
                    "UPDATE secret_refs SET metadata = metadata - 'last4' WHERE id = :i"),
                    {"i": stored["secret_ref_id"]})
                rotated += 1
                redeploy = True
    emit_event(connection, project_id, "project.demo_logins_rotated", actor, "project", project_id,
               {"env": env, "values": rotated})
    return {"env": env, "rotated": rotated, "redeploy": redeploy,
            "note": ("Rotated. Redeploy so the app signs in with the new password."
                     if redeploy else "Rotated. The next preview uses the new password.")}


def view(connection: Connection, project_id: str) -> dict[str, Any]:
    return {"project_id": project_id,
            "sections": [_demo_section(connection, project_id), _deploy_values(connection, project_id),
                         _targets(connection, project_id), _captain(connection, project_id),
                         _autonomy(connection, project_id), _repos(connection, project_id),
                         _notifications(connection, project_id)],
            "href": project_href(project_id, tab="settings")}


__all__ = ["view"]
