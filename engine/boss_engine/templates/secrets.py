"""An app's secret bindings (F2 §2.4, §2.9 S2.4, S2.6).

The value lives in the vault (kind `app_secret`, AAD bound to org/project/app/env/name); the row
in `app_secret_bindings` holds presence, mode and a shape verdict. Nothing here ever returns a
value, a hash, a prefix or a length to a caller outside this module: `values_for_env` is the one
reader, for the engine (the build gate's scan, a preview or a deploy resolving its environment).

Modes:  test       pasted by the operator; must match the template's `pattern`, must NOT match
                   its `live_pattern`; never accepted for `deploy`-only live use
        generated  BOSS makes a random value (e.g. a webhook signing secret for a local stub)
        stub       the template's test double is used; no value at all
        live       env=deploy only; needs app_secret:live (checked by the route) AND a person's
                   approval of the blocking decision this creates before any deploy may use it
"""
from __future__ import annotations

import json
import re
import secrets as _random
from typing import Optional

from sqlalchemy import text

from ..services.core import make_id
from .manifest import SecretSpec, TemplateManifest
from .service import TemplateError

ENVS = ("build", "preview", "deploy")
MODES = ("test", "live", "generated", "stub")
MAX_VALUE = 8192


def shape_of(value: Optional[str], spec: Optional[SecretSpec]) -> str:
    """matches_test_pattern | matches_live_pattern | unknown. A verdict, never the value."""
    if value is None or spec is None:
        return "unknown"
    if spec.live_pattern and re.fullmatch(spec.live_pattern, value):
        return "matches_live_pattern"
    if spec.pattern and re.fullmatch(spec.pattern, value):
        return "matches_test_pattern"
    return "unknown"


def declared_secret(manifests: list[TemplateManifest], name: str) -> Optional[SecretSpec]:
    for m in manifests:
        found = m.secret(name)
        if found is not None:
            return found
    return None


def put(c, *, org_id: str, project_id: str, app_key: str, name: str, env: str, mode: str,
        value: Optional[str], actor_id: Optional[str], spec: SecretSpec, may_live: bool) -> dict:
    """Write (or replace) one binding. `spec` is the declaring template's SecretSpec: a name no
    template of this app declares is refused before this is called. `may_live` says the caller
    holds app_secret:live (the route checks it; passed in so this is testable)."""
    from ..identity import vault

    if env not in ENVS:
        raise TemplateError(422, "bad_env", f"env must be one of {', '.join(ENVS)}")
    if mode not in MODES:
        raise TemplateError(422, "bad_mode", f"mode must be one of {', '.join(MODES)}")
    if mode in ("generated", "stub") and value is not None:
        raise TemplateError(422, "value_not_allowed", f"mode {mode} takes no value; BOSS provides it")
    if mode in ("test", "live"):
        if not isinstance(value, str) or not value or len(value) > MAX_VALUE or "\x00" in value:
            raise TemplateError(422, "value_required", f"mode {mode} needs a value")
    if mode == "live":
        if env != "deploy":
            raise TemplateError(422, "live_only_for_deploy",
                                "live keys are only for env=deploy; builds and previews use test values")
        if not may_live:
            raise TemplateError(403, "needs_app_secret_live", "setting a live key needs app_secret:live")
    verdict = shape_of(value, spec) if value is not None else "unknown"
    if mode == "test":
        if verdict == "matches_live_pattern":
            raise TemplateError(422, "live_value_in_test_mode",
                                f"that value looks like a LIVE key for {name}; test mode takes a test key")
        if spec.pattern and verdict != "matches_test_pattern":
            raise TemplateError(422, "pattern_mismatch",
                                f"that value is not a {name} test value (the template's pattern)")
    if mode == "live" and spec.live_pattern and verdict != "matches_live_pattern":
        raise TemplateError(422, "pattern_mismatch", f"that value is not a {name} live value")
    if mode == "generated":
        value = _random.token_urlsafe(32)
        verdict = "unknown"

    existing = c.execute(text(
        "SELECT id, vault_item_id FROM app_secret_bindings WHERE project_id = :p AND app_key = :a "
        "AND env = :e AND name = :n FOR UPDATE"),
        {"p": project_id, "a": app_key, "e": env, "n": name}).mappings().first()
    if existing and existing["vault_item_id"]:
        vault.revoke(existing["vault_item_id"], ctx=None, reason="app secret replaced", db=c)
    item_id = None
    if value is not None:
        item_id = vault.store_app_secret(org_id=org_id, project_id=project_id, app_key=app_key,
                                         env=env, name=name, value=value, db=c)
    decision_id = None
    if mode == "live":
        decision_id = _live_decision(c, project_id=project_id, app_key=app_key, name=name, actor_id=actor_id)
    params = {"p": project_id, "a": app_key, "e": env, "n": name, "v": item_id, "m": mode,
              "by": actor_id, "s": verdict, "d": decision_id}
    if existing:
        c.execute(text("UPDATE app_secret_bindings SET vault_item_id = :v, mode = :m, set_by = :by, "
                       "set_at = now(), shape = :s, live_decision_id = :d WHERE id = :id"),
                  {**params, "id": existing["id"]})
    else:
        c.execute(text("INSERT INTO app_secret_bindings (id, project_id, app_key, env, name, vault_item_id, "
                       "mode, set_by, shape, live_decision_id) VALUES (:id, :p, :a, :e, :n, :v, :m, :by, "
                       ":s, :d)"), {**params, "id": make_id("asb")})
    out = {"name": name, "env": env, "mode": mode, "shape": verdict, "set": True}
    if decision_id:
        out["live_decision_id"] = decision_id
        out["note"] = ("stored; a deploy may use it only after a person holding app_secret:live "
                       "approves the decision")
    return out


def bind_issued(c, *, org_id: str, project_id: str, app_key: str, name: str, env: str, value: str,
                actor_id: Optional[str]) -> dict:
    """A value BOSS itself issued for the app (its client id, secret and issuer at BOSS's OIDC
    provider, F6): stored like a `generated` binding, with the given value. Never `live`, never for
    a build (the build's tests use the template's fake provider). Not reachable from the PUT
    route: only BOSS's own code binds an issued value."""
    from ..identity import vault

    if env not in ("preview", "deploy"):
        raise TemplateError(422, "bad_env", "an issued value is bound for preview and deploy only")
    if not isinstance(value, str) or not value or len(value) > MAX_VALUE or "\x00" in value:
        raise TemplateError(422, "value_required", "an issued value is a non-empty string")
    existing = c.execute(text(
        "SELECT id, vault_item_id FROM app_secret_bindings WHERE project_id = :p AND app_key = :a "
        "AND env = :e AND name = :n FOR UPDATE"),
        {"p": project_id, "a": app_key, "e": env, "n": name}).mappings().first()
    if existing and existing["vault_item_id"]:
        vault.revoke(existing["vault_item_id"], ctx=None, reason="app secret replaced", db=c)
    item_id = vault.store_app_secret(org_id=org_id, project_id=project_id, app_key=app_key, env=env,
                                     name=name, value=value, db=c)
    params = {"p": project_id, "a": app_key, "e": env, "n": name, "v": item_id, "by": actor_id}
    if existing:
        c.execute(text("UPDATE app_secret_bindings SET vault_item_id = :v, mode = 'generated', set_by = :by, "
                       "set_at = now(), shape = 'unknown', live_decision_id = NULL WHERE id = :id"),
                  {**params, "id": existing["id"]})
    else:
        c.execute(text("INSERT INTO app_secret_bindings (id, project_id, app_key, env, name, vault_item_id, "
                       "mode, set_by, shape) VALUES (:id, :p, :a, :e, :n, :v, 'generated', :by, 'unknown')"),
                  {**params, "id": make_id("asb")})
    return {"name": name, "env": env, "mode": "generated", "set": True}


def has_binding(c, *, project_id: str, app_key: str, name: str, env: str) -> bool:
    """A binding with a stored value exists (presence only)."""
    return bool(c.execute(text(
        "SELECT 1 FROM app_secret_bindings WHERE project_id = :p AND app_key = :a AND env = :e "
        "AND name = :n AND vault_item_id IS NOT NULL"),
        {"p": project_id, "a": app_key, "e": env, "n": name}).first())


def _live_decision(c, *, project_id: str, app_key: str, name: str, actor_id: Optional[str]) -> str:
    """The blocking decision §2.4 requires before a deploy uses a live key."""
    decision_id = make_id("dec")
    c.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, options, risk_tier, status) "
        "VALUES (:id, :p, :q, CAST(:r AS JSONB), CAST(:o AS JSONB), 3, 'pending')"),
        {"id": decision_id, "p": project_id,
         "q": f"Use the live {name} for {app_key} when it is deployed?",
         "r": json.dumps({"kind": "app_secret_live", "app_key": app_key, "name": name,
                          "requested_by": actor_id, "permission": "app_secret:live",
                          "why": "live keys move real money or reach real users (§2.4)"}),
         "o": json.dumps([{"action": "approve", "label": "Use the live key for deploys"},
                          {"action": "reject", "label": "Keep test mode"}])})
    return decision_id


def delete(c, *, project_id: str, app_key: str, name: str, env: str) -> dict:
    from ..identity import vault

    row = c.execute(text("SELECT id, vault_item_id FROM app_secret_bindings WHERE project_id = :p "
                         "AND app_key = :a AND env = :e AND name = :n FOR UPDATE"),
                    {"p": project_id, "a": app_key, "e": env, "n": name}).mappings().first()
    if row is None:
        raise TemplateError(404, "not_found", "no such secret binding")
    if row["vault_item_id"]:
        vault.revoke(row["vault_item_id"], ctx=None, reason="app secret deleted", db=c)
    c.execute(text("DELETE FROM app_secret_bindings WHERE id = :id"), {"id": row["id"]})
    return {"name": name, "env": env, "deleted": True}


def _live_approved(c, decision_id: Optional[str]) -> bool:
    if not decision_id:
        return False
    status = c.execute(text("SELECT status FROM decisions WHERE id = :d"), {"d": decision_id}).scalar()
    return status in ("approved", "executed")


def list_bindings(c, *, project_id: str, app_key: str) -> list[dict]:
    """Presence, mode and shape only (§2.4): no value, hash, prefix or length."""
    rows = c.execute(text(
        "SELECT name, env, mode, set_by, set_at, last_used_at, shape, live_decision_id "
        "FROM app_secret_bindings WHERE project_id = :p AND app_key = :a ORDER BY name, env"),
        {"p": project_id, "a": app_key}).mappings().all()
    out = []
    for r in rows:
        item = {"name": r["name"], "env": r["env"], "mode": r["mode"], "set_by": r["set_by"],
                "set_at": r["set_at"].isoformat() if r["set_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                "shape": r["shape"] or "unknown"}
        if r["mode"] == "live":
            item["live_approved"] = _live_approved(c, r["live_decision_id"])
            item["live_decision_id"] = r["live_decision_id"]
        out.append(item)
    return out


def values_for_env(c, *, org_id: str, project_id: str, app_key: str, env: str) -> tuple[dict[str, str], list[str]]:
    """THE engine-side reader: {name: value} an environment may receive, and why any binding was
    withheld. A build and a preview never get `live`; a preview refuses a value whose shape is a
    live key even if it was pasted as test; a deploy gets `live` only after the decision."""
    from ..identity import vault

    if env not in ENVS:
        raise ValueError(env)
    rows = c.execute(text(
        "SELECT name, mode, vault_item_id, shape, live_decision_id FROM app_secret_bindings "
        "WHERE project_id = :p AND app_key = :a AND env = :e"),
        {"p": project_id, "a": app_key, "e": env}).mappings().all()
    values: dict[str, str] = {}
    withheld: list[str] = []
    for r in rows:
        if r["mode"] == "stub" or not r["vault_item_id"]:
            continue
        if r["mode"] == "live" and (env != "deploy" or not _live_approved(c, r["live_decision_id"])):
            withheld.append(f"{r['name']}: live key not approved for {env}")
            continue
        if env in ("build", "preview") and r["shape"] == "matches_live_pattern":
            withheld.append(f"{r['name']}: looks like a live key; refused for {env}")
            continue
        values[r["name"]] = vault.read_app_secret(
            r["vault_item_id"], org_id=org_id, project_id=project_id, app_key=app_key, env=env,
            name=r["name"], db=c)
    if values:
        c.execute(text("UPDATE app_secret_bindings SET last_used_at = now() WHERE project_id = :p "
                       "AND app_key = :a AND env = :e AND name = ANY(:n)"),
                  {"p": project_id, "a": app_key, "e": env, "n": list(values)})
    return values, withheld


def ensure_test_defaults(c, *, org_id: str, project_id: str, app_key: str, manifest: TemplateManifest,
                         actor_id: Optional[str]) -> list[str]:
    """At apply: a secret with test_default generated/stub gets its build and preview bindings
    when none exists. Returns the names that still need an operator's test value."""
    needs: list[str] = []
    for spec in manifest.secrets:
        for env in ("build", "preview"):
            have = c.execute(text("SELECT mode FROM app_secret_bindings WHERE project_id = :p AND "
                                  "app_key = :a AND env = :e AND name = :n"),
                             {"p": project_id, "a": app_key, "e": env, "n": spec.name}).scalar()
            if have:
                continue
            if spec.test_default in ("generated", "stub"):
                put(c, org_id=org_id, project_id=project_id, app_key=app_key, name=spec.name, env=env,
                    mode=spec.test_default, value=None, actor_id=actor_id, spec=spec, may_live=False)
            elif env == "build" and spec.name not in needs:
                needs.append(spec.name)
    return needs
