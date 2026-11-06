"""Demo logins for a project (contract §5, §11): WHO you sign in as, per environment, and the
password BOSS generated for it behind an audited, stepped-up reveal.

User names come from the app itself, never invented:
  1. a `demo_logins:` list in the app's boss.deploy.yaml (app_deploy_manifest._demo_logins),
     read from the newest stack deployment's manifest summary or text;
  2. else the users the onboarding agent found in the repository (its seed file or login-page
     hint: `customer@demo.test`), stored on the onboarding record;
  3. else one row with no user name, saying where to declare them.

Passwords are only ever read through templates/secrets.py (`demo_login_values` for the app's
preview/deploy bindings, `deploy_target_demo_logins` for what a deploy target runs with): only
a value BOSS generated, only a demo-login name. `listing` reads names and presence only;
`reveal` is the one function that returns a value, and its route needs owner/admin + step-up.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import as_json, href

DEFAULT_KEY = "DEMO_PASSWORD"
#: An e-mail-shaped demo user in a seed file or login page (`agent@demo.test`).
DEMO_EMAIL = re.compile(
    r"\b([A-Za-z0-9._+-]{1,40}@(?:demo|example|test|seed)\.(?:test|com|local|dev|org|io))\b")
#: Where an app keeps its demo users.
SEED_PATH = re.compile(r"(seed|fixture|demo|login|signin|sign-in|users?)[^/]*\.(?:m?js|ts|tsx|jsx|py|"
                       r"json|ya?ml|sql|rb|go|java|html|md)$", re.I)
MAX_SCAN_FILES = 40
MAX_SCAN_BYTES = 200_000


def _label_for(user: str) -> str:
    head = user.split("@", 1)[0]
    return re.sub(r"[._-]+", " ", head).strip().title() or user


def users_from_text(blobs: Iterable[str]) -> list[dict[str, str]]:
    """Demo users named in seed files / login hints, in first-seen order (at most 8)."""
    seen: dict[str, dict[str, str]] = {}
    for blob in blobs:
        for found in DEMO_EMAIL.findall(blob or ""):
            user = found.lower()
            if user not in seen:
                seen[user] = {"user": user, "label": _label_for(user), "password_key": DEFAULT_KEY}
            if len(seen) >= 8:
                return list(seen.values())
    return list(seen.values())


def scan_checkout(root: Any, paths: list[str]) -> list[dict[str, str]]:
    """The onboarding agent's read: seed / login files of a checkout (bounded, data only)."""
    from pathlib import Path

    base = Path(root)
    wanted = [p for p in paths if SEED_PATH.search(p) and "node_modules" not in p][:MAX_SCAN_FILES]
    wanted += [p for p in ("README.md", "readme.md") if p in paths]
    blobs = []
    for rel in wanted:
        try:
            with open(base / rel, "rb") as fh:
                blobs.append(fh.read(MAX_SCAN_BYTES).decode("utf-8", "replace"))
        except OSError:
            continue
    return users_from_text(blobs)


def _manifest_users(connection: Connection, project_id: str) -> list[dict[str, str]]:
    from ..app_deploy_manifest import ManifestError, load_manifest

    rows = connection.execute(text(
        "SELECT s.manifest, s.manifest_text FROM stack_deployments s JOIN deployment_targets t "
        "ON t.id = s.target_id WHERE t.project_id = :p ORDER BY s.created_at DESC LIMIT 3"),
        {"p": project_id}).mappings().all()
    for r in rows:
        summary = as_json(r["manifest"], {}) or {}
        listed = summary.get("demo_logins")
        if isinstance(listed, list) and listed:
            return [_person(d) for d in listed if isinstance(d, dict) and d.get("user")]
        if r["manifest_text"] and "demo_logins" in str(r["manifest_text"]):
            try:
                parsed = load_manifest(str(r["manifest_text"]))
            except (ManifestError, Exception):  # noqa: BLE001 — a bad manifest names no users
                continue
            if parsed.demo_logins:
                return [_person(d) for d in parsed.demo_logins]
    return []


def _person(d: dict) -> dict[str, str]:
    """A demo user by name: {user, label, password_key} (the env key that holds the password;
    the manifest calls it `password`). Never a value."""
    return {"user": str(d.get("user")), "label": str(d.get("label") or d.get("user")),
            "password_key": str(d.get("password_key") or d.get("password") or DEFAULT_KEY)}


def _discovered_users(connection: Connection, project_id: str) -> list[dict[str, str]]:
    row = connection.execute(text(
        "SELECT facts FROM project_onboarding_runs WHERE project_id = :p "
        "AND facts ? 'demo_users' ORDER BY started_at DESC LIMIT 1"),
        {"p": project_id}).mappings().first()
    users = (as_json(row["facts"], {}) or {}).get("demo_users") if row else None
    return [_person(u) for u in users or [] if isinstance(u, dict) and u.get("user")]


def users(connection: Connection, project_id: str) -> tuple[list[dict[str, str]], str]:
    """(users, where they came from: manifest | repository | none)."""
    found = _manifest_users(connection, project_id)
    if found:
        return found, "manifest"
    found = _discovered_users(connection, project_id)
    if found:
        return found, "repository"
    return [], "none"


def _env_label(env: str) -> str:
    from .health import env_label

    return env_label(env)


def environments(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Each environment that has a BOSS-generated demo password, by NAME only (no value):
    preview (the app's preview binding) and each deploy target whose healthy deploy generated
    one (or, failing that, the app's deploy binding)."""
    from ...templates.run_env import is_demo_login_name

    out: dict[str, dict[str, Any]] = {}
    rows = connection.execute(text(
        "SELECT DISTINCT env, name FROM app_secret_bindings WHERE project_id = :p "
        "AND mode = 'generated' AND vault_item_id IS NOT NULL"), {"p": project_id}).mappings().all()
    bound = {(r["env"], r["name"]) for r in rows if is_demo_login_name(str(r["name"]))}
    if any(e == "preview" for e, _ in bound):
        out["preview"] = {"key": "preview", "label": "Preview",
                          "keys": sorted(n for e, n in bound if e == "preview")}
    targets = connection.execute(text(
        "SELECT t.id, t.environment, s.manifest FROM deployment_targets t "
        "LEFT JOIN LATERAL (SELECT manifest FROM stack_deployments WHERE target_id = t.id "
        " AND status = 'healthy' ORDER BY created_at DESC LIMIT 1) s ON TRUE "
        "WHERE t.project_id = :p AND t.status = 'active' ORDER BY t.environment"),
        {"p": project_id}).mappings().all()
    deploy_keys = sorted(n for e, n in bound if e == "deploy")
    for t in targets:
        manifest = as_json(t["manifest"], {}) or {}
        keys = sorted(str(k) for k in manifest.get("generated_env_keys") or []
                      if is_demo_login_name(str(k))) or deploy_keys
        if not keys:
            continue
        env = str(t["environment"] or "deploy")
        slot = out.setdefault(env, {"key": env, "label": _env_label(env), "keys": []})
        slot["keys"] = sorted(set(slot["keys"]) | set(keys))
        slot.setdefault("target_ids", []).append(t["id"])
    return list(out.values())


def reveal_href(project_id: str, env: str) -> str:
    return f"/v1/projects/{project_id}/demo-logins/reveal?env={env}"


def rows_for(project_id: str, env: dict[str, Any], people: list[dict[str, str]]
             ) -> list[dict[str, Any]]:
    """The read-only rows (user + reveal) for one environment."""
    reveal = {"href": reveal_href(project_id, env["key"]), "requires": "step_up",
              "method": "GET", "roles": ["owner", "admin"]}
    if not people:
        return [{"user": None, "label": "Demo login", "env": env["key"],
                 "password_key": env["keys"][0] if env.get("keys") else DEFAULT_KEY,
                 "reveal": reveal,
                 "hint": "Name the demo users in boss.deploy.yaml (demo_logins:) to show them here."}]
    keys = set(env.get("keys") or [])
    return [{"user": p["user"], "label": p.get("label") or p["user"], "env": env["key"],
             "password_key": p.get("password_key") or DEFAULT_KEY, "reveal": reveal}
            for p in people if not keys or (p.get("password_key") or DEFAULT_KEY) in keys
            or len(keys) == 1]


def listing(connection: Connection, project_id: str) -> dict[str, Any]:
    """Names only, every environment: what Overview › Try it, Deployments and Settings show."""
    people, source = users(connection, project_id)
    envs = environments(connection, project_id)
    return {"source": source, "users": people,
            "environments": [{**e, "logins": rows_for(project_id, e, people)} for e in envs],
            "settings_href": href(project_id, area="project", tab="settings",
                                  drawer="section:demo_logins")}


def _app_keys(connection: Connection, project_id: str) -> list[str]:
    """The apps of this project that hold a generated secret binding."""
    return [str(r) for r in connection.execute(text(
        "SELECT DISTINCT app_key FROM app_secret_bindings WHERE project_id = :p AND "
        "mode = 'generated' ORDER BY app_key"), {"p": project_id}).scalars().all()]


def reveal(connection: Connection, settings: Any, *, org_id: str, project_id: str,
           env: str) -> dict[str, Any]:
    """The ONE place this module returns a password: {env, logins: [{user, label, password}]}.
    Only values BOSS generated, read through templates/secrets.py: a deploy target's generated
    secret (`target_demo_logins`), else the app's binding for that environment
    (`demo_login_values`, preview / deploy)."""
    from ...templates import secrets as app_secrets

    env = str(env or "").strip().lower()
    known = {e["key"]: e for e in environments(connection, project_id)}
    if env not in known:
        raise LookupError(f"No demo login was generated for {env or 'that environment'}.")
    slot = known[env]
    values: dict[str, str] = {}
    if env != "preview":
        for t in connection.execute(text(
                "SELECT t.id, t.environment, s.manifest FROM deployment_targets t "
                "JOIN LATERAL (SELECT manifest FROM stack_deployments WHERE target_id = t.id "
                " AND status = 'healthy' ORDER BY created_at DESC LIMIT 1) s ON TRUE "
                "WHERE t.id = ANY(:ids) AND t.project_id = :p"),
                {"ids": slot.get("target_ids") or [], "p": project_id}).mappings().all():
            for row in app_secrets.target_demo_logins(connection, settings, project_id=project_id,
                                                      target=dict(t)):
                values.setdefault(str(row["name"]), str(row["value"]))
    if not values:
        for app_key in _app_keys(connection, project_id):
            got = app_secrets.demo_login_values(
                connection, org_id=org_id, project_id=project_id, app_key=app_key,
                env="preview" if env == "preview" else "deploy")
            for name, value in got.items():
                values.setdefault(name, value)
    people, _source = users(connection, project_id)
    logins = []
    for row in rows_for(project_id, slot, people):
        key = row["password_key"]
        value = values.get(key) or (next(iter(values.values())) if len(values) == 1 else None)
        logins.append({"user": row["user"], "label": row["label"], "password_key": key,
                       "password": value})
    return {"project_id": project_id, "env": env, "label": slot["label"], "logins": logins}


def record_reveal(connection: Connection, project_id: str, actor: str, env: str, count: int) -> None:
    """An `events` row: who revealed which environment's demo logins (never the value)."""
    from ..ambient._common import emit

    emit(connection, project_id, "project.demo_logins_revealed", actor, "project", project_id,
         {"env": env, "logins": count})


__all__ = ["DEFAULT_KEY", "environments", "listing", "record_reveal", "reveal", "reveal_href",
           "rows_for", "scan_checkout", "users", "users_from_text"]
