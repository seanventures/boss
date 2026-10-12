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
        decision_id = _live_decision(c, project_id=project_id, app_key=app_key, name=name, actor_id=actor_id,
                                     shape=verdict)
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


_SHAPE_WORDS = {"matches_live_pattern": "matches the template's live-key pattern",
                "matches_test_pattern": "matches the template's test-key pattern",
                "unknown": "the template declares no pattern to check it against"}


def app_display_name(c, project_id: str, app_key: str) -> str:
    """The name a person knows the app by (the Factory's), not its key."""
    from ..services.agent_assignment import app_key as key_of
    from ..services.factory_view import app_name

    rows = c.execute(text(
        "SELECT id, title, spec, finalize_ctx FROM code_task_runs WHERE project_id = :p "
        "ORDER BY created_at DESC LIMIT 500"), {"p": project_id}).mappings().all()
    for r in rows:
        try:
            name = app_name(dict(r))
        except Exception:  # noqa: BLE001
            continue
        if name and key_of(name) == app_key:
            return name
    return app_key


def _person(c, actor_id: Optional[str]) -> str:
    if not actor_id:
        return "BOSS"
    try:
        with c.begin_nested():
            found = c.execute(text("SELECT display_name FROM identity_principals WHERE id = :i"),
                              {"i": actor_id}).scalar()
    except Exception:  # noqa: BLE001 — a name is a nicety; the id still says who
        found = None
    return str(found or actor_id)


def _live_decision(c, *, project_id: str, app_key: str, name: str, actor_id: Optional[str],
                   shape: str = "unknown") -> str:
    """The blocking decision a person answers before a deploy uses a live key. ONE per app and
    secret while it is open: setting the key again refreshes its evidence, never a second card."""
    from datetime import datetime, timezone

    display = app_display_name(c, project_id, app_key)
    who = _person(c, actor_id)
    at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    evidence = [{"label": "App", "value": display},
                {"label": "Secret", "value": name},
                {"label": "Used by", "value": "deploys only (builds and previews keep the test value)"},
                {"label": "Set by", "value": who},
                {"label": "Set at", "value": at},
                {"label": "Key check", "value": _SHAPE_WORDS.get(shape, _SHAPE_WORDS["unknown"])}]
    recommendation = {
        "kind": "app_secret_live", "app_key": app_key, "app_name": display, "name": name,
        "requested_by": actor_id, "permission": "app_secret:live",
        "reason": "a live key moves real money or reaches real users, so a person approves it before "
                  "any deploy uses it",
        "evidence": evidence,
        "summary": f"{who} set a live {name} for {display}. Approve to let deploys of {display} use it; "
                   "reject to keep deploys on the test value.",
    }
    existing = c.execute(text(
        "SELECT id, recommendation FROM decisions WHERE project_id = :p AND status = 'pending' "
        "AND recommendation->>'kind' = 'app_secret_live' AND recommendation->>'app_key' = :a "
        "AND recommendation->>'name' = :n ORDER BY created_at DESC LIMIT 1 FOR UPDATE"),
        {"p": project_id, "a": app_key, "n": name}).mappings().first()
    question = f"Use the live {name} when {display} is deployed?"
    if existing is not None:
        prior = existing["recommendation"] if isinstance(existing["recommendation"], dict) else {}
        recommendation["times_set"] = int(prior.get("times_set") or 1) + 1
        c.execute(text("UPDATE decisions SET question = :q, recommendation = CAST(:r AS JSONB), "
                       "updated_at = now() WHERE id = :id"),
                  {"q": question, "r": json.dumps(recommendation), "id": existing["id"]})
        return str(existing["id"])
    decision_id = make_id("dec")
    c.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, options, risk_tier, status) "
        "VALUES (:id, :p, :q, CAST(:r AS JSONB), CAST(:o AS JSONB), 3, 'pending')"),
        {"id": decision_id, "p": project_id, "q": question, "r": json.dumps(recommendation),
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


def declared_names(c, *, project_id: str, app_key: str) -> list[str]:
    """The secrets the app's templates declare (any application not abandoned): names only."""
    rows = c.execute(text(
        "SELECT t.manifest -> 'secrets' AS secrets FROM app_template_applications a "
        "JOIN app_templates t ON t.id = a.template_id WHERE a.project_id = :p AND a.app_key = :a "
        "AND a.status <> 'abandoned' ORDER BY a.created_at"),
        {"p": project_id, "a": app_key}).mappings().all()
    names: list[str] = []
    for r in rows:
        raw = r["secrets"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        for spec in raw or []:
            name = spec.get("name") if isinstance(spec, dict) else None
            if name and name not in names:
                names.append(str(name))
    return names


def declared_specs(c, *, project_id: str, app_key: str) -> dict[str, dict]:
    """{name: {"required", "test_default"}} for the secrets the app's templates declare. A name
    two templates declare is required if either requires it."""
    rows = c.execute(text(
        "SELECT t.manifest -> 'secrets' AS secrets FROM app_template_applications a "
        "JOIN app_templates t ON t.id = a.template_id WHERE a.project_id = :p AND a.app_key = :a "
        "AND a.status <> 'abandoned' ORDER BY a.created_at"),
        {"p": project_id, "a": app_key}).mappings().all()
    out: dict[str, dict] = {}
    for r in rows:
        raw = r["secrets"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        for spec in raw or []:
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            name = str(spec["name"])
            required = spec.get("required", True) is not False
            seen = out.get(name)
            out[name] = {"required": required or bool(seen and seen["required"]),
                         "test_default": (seen or {}).get("test_default")
                         or spec.get("test_default") or "none",
                         "spec": spec}
    return out


def ensure_deploy_generated(c, *, org_id: str, project_id: str, app_key: str,
                            actor_id: Optional[str] = None) -> list[str]:
    """A secret BOSS generates for build and preview (test_default=generated, e.g. a session
    signing key) is generated for deploy too, once, when nobody has set one. Without this a
    deploy was refused for "SESSION_SECRET has no deploy value" — a value no person is meant to
    choose. Returns the names it generated. Never touches a binding somebody set."""
    made: list[str] = []
    for name, d in declared_specs(c, project_id=project_id, app_key=app_key).items():
        if d["test_default"] != "generated" or has_binding(
                c, project_id=project_id, app_key=app_key, name=name, env="deploy"):
            continue
        put(c, org_id=org_id, project_id=project_id, app_key=app_key, name=name, env="deploy",
            mode="generated", value=None, actor_id=actor_id,
            spec=SecretSpec.model_validate(d["spec"]), may_live=False)
        made.append(name)
    return made


def deploy_secrets(c, *, project_id: str, app_key: str) -> tuple[list[dict], list[str]]:
    """What a deploy of this app passes, BY NAME, and the words for each required secret it
    cannot pass. Never a value: this is what a plan, a ChangeSet and a decision may carry.

    Required = every secret the app's templates declare, unless declared `required: false` (the
    app runs without it). It is missing when it has no `deploy`
    binding, or its deploy binding is a live key a person has not approved (the decision
    `put` raised). Undeclared deploy bindings (an issued sign-in client, F6) are passed too."""
    rows = c.execute(text(
        "SELECT name, mode, vault_item_id, live_decision_id FROM app_secret_bindings "
        "WHERE project_id = :p AND app_key = :a AND env = 'deploy' ORDER BY name"),
        {"p": project_id, "a": app_key}).mappings().all()
    bound = {r["name"]: r for r in rows}
    passed: list[dict] = []
    missing: list[str] = []
    declared = declared_specs(c, project_id=project_id, app_key=app_key)
    for name in sorted(set(bound) | set(declared)):
        r = bound.get(name)
        if r is None or (r["mode"] != "stub" and not r["vault_item_id"]):
            # Not required: the app runs without it and that feature stays off.
            if name in declared and not declared[name]["required"]:
                continue
            missing.append(f"{name} has no deploy value")
            continue
        if r["mode"] == "live" and not _live_approved(c, r["live_decision_id"]):
            missing.append(f"{name} is a live key a person has not approved yet"
                           + (f" (decision {r['live_decision_id']})" if r["live_decision_id"] else ""))
            continue
        passed.append({"name": name, "mode": r["mode"], "source": "app_secret", "env": "deploy"})
    return passed, missing


def ensure_test_defaults(c, *, org_id: str, project_id: str, app_key: str, manifest: TemplateManifest,
                         actor_id: Optional[str], dry_run: bool = False) -> list[str]:
    """At apply: a secret with test_default generated/stub gets its build and preview bindings
    when none exists. Returns the names that still need an operator's test value. `dry_run`
    answers the same question and writes nothing."""
    needs: list[str] = []
    for spec in manifest.secrets:
        for env in ("build", "preview"):
            have = c.execute(text("SELECT mode FROM app_secret_bindings WHERE project_id = :p AND "
                                  "app_key = :a AND env = :e AND name = :n"),
                             {"p": project_id, "a": app_key, "e": env, "n": spec.name}).scalar()
            if have:
                continue
            if spec.test_default in ("generated", "stub"):
                if dry_run:
                    continue
                put(c, org_id=org_id, project_id=project_id, app_key=app_key, name=spec.name, env=env,
                    mode=spec.test_default, value=None, actor_id=actor_id, spec=spec, may_live=False)
            elif env == "build" and spec.name not in needs:
                needs.append(spec.name)
    return needs
