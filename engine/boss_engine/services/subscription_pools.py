"""Multi-account pools per provider: several Claude or Codex subscriptions used as one (BOSSAPP-886).

WHY. A team with three Claude seats was capped at one seat's rate window: BOSS spent on exactly one
subscription per provider (model_router.accounts), and pooling was listed as future work.

THE SHAPE. A project's pool for one engine (claude_code | codex) holds MEMBERS. Each member is a
subscription login BOSS already knows how to use, stored the way the single login is:

  * its credential in the secret store, in the SAME subscription namespace, under its own name
    (`secret_name`: CLAUDE_CODE_OAUTH_TOKEN__POOL_<id> / CODEX_AUTH_JSON__POOL_<id>). It is read
    with cli_harness.stored_credential — the one resolver — and a codex member's rotated login is
    written back with cli_harness.sync_rotated_credential on a spec whose `auth_file_secret` is
    the member's name — the one sync seam. There is no second resolver and no second writer
    (BOSSAPP-1033's lesson).
  * its rate windows in subscription_accounts under `account_key` (<engine>:pool:<member id>):
    while a pooled run is in flight the LLM context carries that key, and
    model_router.accounts.account_key answers it, so the CLI's own rate_limit_events and limit
    messages land on the member that spent them.

CHOOSING. `assign` gives a run the member with the most headroom (model_router.headroom.assess
over its windows; nothing known reads as plenty), ties by position then least recently used, and
PINS it for the task (subscription_pool_pins) so one build stays on one account while that
account has room. A member at its limit is skipped; the pool is EXHAUSTED only when every active
member is, and PoolExhausted says when the earliest one resets.

WITHOUT A POOL nothing changes: `assignment_for` answers None and every launcher runs on the
single login exactly as before.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import emit_event, make_id

_log = logging.getLogger("boss.subscription_pools")

ENGINES = ("claude_code", "codex")
#: The single login's store name per engine; a member's is this plus __POOL_<member id>.
BASE_SECRET = {"claude_code": "CLAUDE_CODE_OAUTH_TOKEN", "codex": "CODEX_AUTH_JSON"}
MEMBER_STATUSES = ("active", "disabled", "needs_reauth")


class PoolError(ValueError):
    """A pool or member that cannot be saved as given."""


class PoolExhausted(RuntimeError):
    """Every active member of the pool is at its limit."""

    def __init__(self, message: str, retry_at: Optional[datetime]) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def exhausted_words(pool_name: str, retry_at: Optional[datetime]) -> str:
    """Every seat at its limit, in the words provider_limits.classify_limit reads as a capacity
    limit ("usage limit", "try again at <ISO>"), so the run takes the ordinary at-limit path
    (wait for the reset or move to another source) with the reset time — never a person's
    question (BOSSAPP-1343 review)."""
    when = (f" Try again at {retry_at.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC."
            if retry_at else "")
    return (f"You've hit your usage limit on every account in the {pool_name} pool.{when} "
            "Add an account to the pool, or wait for the reset.")


@dataclass(frozen=True)
class Assignment:
    """Which member a run spends on. Never carries a credential."""

    pool_id: str
    member_id: str
    label: str
    engine: str
    secret_name: str
    account_key: str
    pinned: bool
    #: Why this member, when it is not the plain choice: a run moved off an account that hit its
    #: limit says so here (and on the run's LLM context, and as an event). "" otherwise.
    reason: str = ""


def secret_name_for(engine: str, member_id: str) -> str:
    return f"{BASE_SECRET[engine]}__POOL_{member_id.upper()}"


def _store(settings: Any):
    from .secret_backends import backend_for, default_secret_backend

    return backend_for(default_secret_backend(settings), settings)


def _store_key(name: str) -> str:
    """The namespaced key BOSS writes a subscription credential under (cli_harness reads both)."""
    from .cli_harness import SUBSCRIPTION_SECRET_SCOPE
    from .secrets_service import ref_key_for

    return ref_key_for(SUBSCRIPTION_SECRET_SCOPE, name)


def _validate_credential(engine: str, credential: str) -> str:
    value = str(credential or "").strip()
    if not value:
        raise PoolError("the account's login is required; it goes to the secret store only")
    if engine == "codex":
        try:
            parsed = json.loads(value)
        except ValueError:
            raise PoolError("a Codex login is the CLI's auth.json; this is not valid JSON") from None
        if not isinstance(parsed, dict):
            raise PoolError("a Codex login is the CLI's auth.json object")
    return value


# ── reading ──────────────────────────────────────────────────────────────────────────────────

def _member_row(row: Any) -> dict[str, Any]:
    iso = lambda v: v.isoformat() if v is not None and hasattr(v, "isoformat") else v  # noqa: E731
    return {"id": row["id"], "pool_id": row["pool_id"], "label": row["label"],
            "secret_name": row["secret_name"], "account_key": row["account_key"],
            "position": int(row["position"]), "status": row["status"],
            "proof": row.get("last_proof") if isinstance(row.get("last_proof"), dict) else None,
            "primary": bool(row.get("is_primary")),
            "last_assigned_at": iso(row["last_assigned_at"]), "created_by": row["created_by"],
            "created_at": iso(row["created_at"])}


def _org(connection: Connection, project_id: str) -> Optional[str]:
    from .org_models import org_of

    return org_of(connection, project_id)


def _owns(connection: Connection, project_id: str, alias: str = "s") -> tuple[str, dict[str, Any]]:
    """SQL for "this pool is visible to the project": its own project pool, or its ORG's pool."""
    return (f"({alias}.project_id = :p OR ({alias}.scope = 'org' AND {alias}.org_id = :org))",
            {"p": project_id, "org": _org(connection, project_id)})


def get_pool(connection: Connection, project_id: str, engine: str,
             scope: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The pool a project's runs use for `engine`: its OWN pool when it has one (an override for
    that project alone), else its org's. `scope` asks for exactly the project's or the org's."""
    where, args = _owns(connection, project_id)
    if scope in ("project", "org"):
        where += " AND s.scope = :sc"
        args["sc"] = scope
    pool = connection.execute(text(
        f"SELECT s.* FROM subscription_pools s WHERE s.engine = :e AND {where} "
        "ORDER BY (s.scope = 'project') DESC LIMIT 1"), {**args, "e": engine}).mappings().first()
    return _load_pool(connection, pool)


def _load_pool(connection: Connection, pool: Any) -> Optional[dict[str, Any]]:
    if pool is None:
        return None
    members = connection.execute(text(
        "SELECT * FROM subscription_pool_members WHERE pool_id = :i ORDER BY position, created_at"),
        {"i": pool["id"]}).mappings().all()
    return {"id": pool["id"], "project_id": pool["project_id"], "engine": pool["engine"],
            "scope": pool["scope"], "name": pool["name"], "status": pool["status"],
            "members": [_member_row(m) for m in members]}


def headroom_of(connection: Connection, members: list[dict[str, Any]], engine: str,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """{member id: Headroom} from each member's own windows. Nothing known reads as plenty."""
    from .model_router import accounts
    from .model_router.headroom import assess

    rows = accounts.load(connection, [m["account_key"] for m in members]) if members else {}
    return {m["id"]: assess(engine, rows.get(m["account_key"]) or {}, thresholds={}, now=now)
            for m in members}


def describe(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """Every pool of the project with each member's headroom — metadata only, never a login."""
    out = []
    where, args = _owns(connection, project_id)
    found = [_load_pool(connection, r) for r in connection.execute(text(
        f"SELECT s.* FROM subscription_pools s WHERE {where} ORDER BY s.engine, "
        "(s.scope = 'project') DESC"), args).mappings().all()]
    for pool in found:
        engine = pool["engine"]
        rooms = headroom_of(connection, pool["members"], engine)
        for m in pool["members"]:
            h = rooms.get(m["id"])
            m["adopted_install_login"] = m["account_key"].endswith(":install") \
                or m["secret_name"] == BASE_SECRET[engine]
            m["headroom"] = {"state": h.state, "left": h.left,
                             "resets_at": h.resets_at.isoformat() if h.resets_at else None,
                             "detail": h.detail} if h else None
            m.pop("secret_name", None)
        out.append(pool)
    return out


def scope_of(connection: Connection, *, pool_id: Optional[str] = None,
             member_id: Optional[str] = None) -> Optional[str]:
    """'org' or 'project' for a pool (or a member's pool), None when there is none. The API asks
    it first: a change to the ORG's pool needs the permission at org level (BOSSAPP-1343
    review), not only a project binding."""
    if member_id:
        return connection.execute(text(
            "SELECT s.scope FROM subscription_pool_members m JOIN subscription_pools s "
            "ON s.id = m.pool_id WHERE m.id = :i"), {"i": member_id}).scalar()
    if pool_id:
        return connection.execute(text("SELECT scope FROM subscription_pools WHERE id = :i"),
                                  {"i": pool_id}).scalar()
    return None


# ── writing ──────────────────────────────────────────────────────────────────────────────────

def create_pool(connection: Connection, project_id: str, *, engine: str, name: str,
                actor: str, settings: Any = None, scope: str = "project") -> dict[str, Any]:
    """`scope="org"` makes the ORG's pool (every project in the org uses it); "project" makes this
    project's own, which overrides the org's for it alone. An org pool is anchored on the org's
    model home project (where the install's login lives), else on this project."""
    if engine not in ENGINES:
        raise PoolError(f"engine must be one of: {', '.join(ENGINES)}")
    if scope not in ("project", "org"):
        raise PoolError("scope must be org or project")
    label = " ".join(str(name or "").split())[:80] or f"{engine} pool"
    org = _org(connection, project_id)
    if scope == "org" and not org:
        scope = "project"
    if get_pool(connection, project_id, engine, scope=scope) is not None:
        raise PoolError(f"this {'org' if scope == 'org' else 'project'} already has a {engine} "
                        "pool; add accounts to it")
    anchor = project_id
    if scope == "org":
        from .org_models import home_project

        anchor = home_project(connection, org) or project_id
    pid = make_id("spool")
    connection.execute(text(
        "INSERT INTO subscription_pools (id, project_id, engine, name, created_by, scope, org_id) "
        "VALUES (:i, :p, :e, :n, :a, :sc, :o)"),
        {"i": pid, "p": anchor, "e": engine, "n": label, "a": actor, "sc": scope,
         "o": org if scope == "org" else None})
    emit_event(connection, anchor, "subscription_pool.created", actor, "subscription_pool",
               pid, {"engine": engine, "name": label, "scope": scope})
    if settings is not None:
        adopt_install_login(connection, settings, project_id, engine, scope=scope)
    return get_pool(connection, project_id, engine, scope=scope)


def _pool_by_id(connection: Connection, project_id: str, pool_id: str) -> dict[str, Any]:
    where, args = _owns(connection, project_id)
    row = connection.execute(text(
        f"SELECT s.* FROM subscription_pools s WHERE s.id = :i AND {where}"),
        {**args, "i": pool_id}).mappings().first()
    if row is None:
        raise LookupError(f"no pool {pool_id!r} in this project")
    return _load_pool(connection, row)


def add_member(connection: Connection, settings: Any, project_id: str, pool_id: str, *,
               label: str, credential: str, actor: str, status: str = "active"
               ) -> dict[str, Any]:
    """Store one account's login (the store only — never this table, never a response) and add it."""
    pool = _pool_by_id(connection, project_id, pool_id)
    engine = pool["engine"]
    value = _validate_credential(engine, credential)
    name = " ".join(str(label or "").split())[:80]
    if not name:
        raise PoolError("name the account, e.g. the seat's owner")
    if any(m["label"].lower() == name.lower() for m in pool["members"]):
        raise PoolError(f"the pool already has an account named {name!r}")
    mid = make_id("spm")
    secret = secret_name_for(engine, mid)
    _store(settings).put(_store_key(secret), value)
    position = max([m["position"] for m in pool["members"]] + [-1]) + 1
    try:
        connection.execute(text(
            "INSERT INTO subscription_pool_members (id, pool_id, label, secret_name, account_key, "
            "position, created_by, status) VALUES (:i, :pool, :l, :s, :k, :pos, :a, :st)"),
            {"i": mid, "pool": pool_id, "l": name, "s": secret,
             "k": f"{engine}:pool:{mid}", "pos": position, "a": actor, "st": status})
    except Exception:
        try:
            _store(settings).delete(_store_key(secret))
        except Exception:  # noqa: BLE001
            pass
        raise
    emit_event(connection, project_id, "subscription_pool.member_added", actor,
               "subscription_pool", pool_id, {"member_id": mid, "label": name, "engine": engine})
    member = next(m for m in _pool_by_id(connection, project_id, pool_id)["members"]
                  if m["id"] == mid)
    member.pop("secret_name", None)
    return member


def adopt_install_login(connection: Connection, settings: Any, project_id: str,
                        engine: str, scope: Optional[str] = None) -> Optional[dict[str, Any]]:
    """MIGRATION: the install's single login (`claude_code:install` — the one stored under the
    plain subscription name) becomes ACCOUNT 1 of the engine's pool, and the primary. Nothing is
    copied: the member points at the SAME store key and keeps the install's rate windows
    (account_key), so the single-login path and the pool read one login. Idempotent: a pool that
    already holds it, or an install with no stored login, changes nothing (returns None)."""
    from .cli_harness import stored_credential
    from .model_router import accounts

    pool = get_pool(connection, project_id, engine, scope=scope)
    if pool is None:
        return None
    base = BASE_SECRET[engine]
    if any(m["secret_name"] == base for m in pool["members"]):
        return None
    try:
        held = stored_credential(settings, base, include_env=False)
    except Exception:  # noqa: BLE001 — an unreadable store adopts nothing
        held = None
    if not held:
        return None
    from .llm_telemetry import get_llm_context

    ctx = get_llm_context() or {}
    key = accounts.account_key(connection, settings, project_id, engine) \
        if not ctx.get("pool_account") else f"{engine}:install"
    if key.startswith(f"{engine}:pool:"):
        key = f"{engine}:install"
    label = "Account 1"
    taken = {m["label"].lower() for m in pool["members"]}
    n = 1
    while label.lower() in taken:
        n += 1
        label = f"Account {n}"
    mid = make_id("spm")
    position = min([m["position"] for m in pool["members"]] + [1]) - 1 if pool["members"] else 0
    connection.execute(text(
        "INSERT INTO subscription_pool_members (id, pool_id, label, secret_name, account_key, "
        "position, is_primary, created_by) VALUES (:i, :pool, :l, :s, :k, :pos, :pr, 'system')"),
        {"i": mid, "pool": pool["id"], "l": label, "s": base, "k": key, "pos": position,
         "pr": not any(m["primary"] for m in pool["members"])})
    emit_event(connection, project_id, "subscription_pool.member_added", "system",
               "subscription_pool", pool["id"],
               {"member_id": mid, "label": label, "engine": engine, "adopted_install_login": True})
    return next(m for m in _pool_by_id(connection, project_id, pool["id"])["members"]
                if m["id"] == mid)


def add_signed_in_account(connection: Connection, settings: Any, project_id: str, engine: str,
                          credential: str, *, actor: str, label: Optional[str] = None,
                          scope: str = "org") -> dict[str, Any]:
    """The server-side Sign in's "add another account": the pool (the ORG's by default) is made
    if there is none, the install's existing login is adopted as account 1 first (so adding never
    replaces it), then the NEW login is stored as its own member. It starts `needs_reauth`: out
    of routing until its Connect proof passes (prove_member). The sign-in exposes no email or
    plan, so the label is "Account N" unless named; rename it in Setup."""
    if get_pool(connection, project_id, engine, scope=scope) is None:
        create_pool(connection, project_id, engine=engine, name="", actor=actor,
                    scope=scope)
    pool = get_pool(connection, project_id, engine, scope=scope)
    adopt_install_login(connection, settings, project_id, engine, scope=scope)
    pool = get_pool(connection, project_id, engine, scope=scope)
    taken = {m["label"].lower() for m in pool["members"]}
    name = " ".join(str(label or "").split())[:80]
    n = len(pool["members"]) + 1
    while not name or name.lower() in taken:
        name = f"Account {n}"
        n += 1
    return add_member(connection, settings, project_id, pool["id"], label=name,
                      credential=credential, actor=actor, status="needs_reauth")


# ── the Connect proof for an added account ───────────────────────────────────────────────────

def _run_proof(settings: Any, engine: str, credential: str) -> dict[str, Any]:
    """One tiny real turn on THIS account's login (never the install's): the CLI answers
    BOSS_OK or it does not. Metadata only comes back — the credential never leaves here."""
    import os
    import shutil
    import subprocess
    import tempfile

    from .setup_service import PROOF_REPLY, cli_auth_failed

    binary = shutil.which("claude" if engine == "claude_code" else "codex")
    if binary is None:
        return {"ok": False, "error": f"the {engine} CLI is not installed on the BOSS server"}
    prompt = f"Reply with exactly: {PROOF_REPLY}. Do not create or modify any file."
    with tempfile.TemporaryDirectory(prefix="boss-pool-proof-") as work:
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY")}
        if engine == "claude_code":
            env["CLAUDE_CODE_OAUTH_TOKEN"] = credential
            argv = [binary, "-p", prompt, "--tools", ""]
        else:
            home = os.path.join(work, "codex-home")
            os.makedirs(home)
            with open(os.path.join(home, "auth.json"), "w") as fh:
                fh.write(credential)
            os.chmod(os.path.join(home, "auth.json"), 0o600)
            env["CODEX_HOME"] = home
            argv = [binary, "exec", "--skip-git-repo-check", "--color", "never",
                    "--sandbox", "read-only", prompt]
        try:
            done = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True,
                                  timeout=90)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "the proof timed out after 90 s"}
        except OSError as exc:
            return {"ok": False, "error": f"the proof could not start ({type(exc).__name__})"}
    said = f"{done.stdout or ''}\n{done.stderr or ''}"
    if credential and credential in said:
        said = said.replace(credential, "[redacted]")
    if done.returncode == 0 and PROOF_REPLY in said:
        return {"ok": True}
    if cli_auth_failed(said):
        return {"ok": False, "error": "the provider refused this account's sign-in: sign in "
                                      "to it again"}
    return {"ok": False, "error": (said.strip()[:200] or "the proof got no reply")}


def prove_member(db_engine: Any, settings: Any, project_id: str, member_id: str, actor: str, *,
                 prove: Optional[Callable[[str, str], dict[str, Any]]] = None,
                 proof_runner: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    """The Connect proof an added account must pass before it is Working: one tiny call on its
    OWN login, run through the governed proof (reservation, lease, receipt) like the main row's.
    Pass -> active and routed. Fail -> stays out of routing (needs_reauth) with the refusal on
    its row. `prove(engine, credential)` and `proof_runner` exist for tests. Returns
    {"ok", "error"}; never raises, never carries the credential."""
    from ..db import begin
    from .cli_harness import stored_credential

    with begin(db_engine) as c:
        row = c.execute(text(
            "SELECT m.id, m.secret_name, m.label, s.engine FROM subscription_pool_members m "
            "JOIN subscription_pools s ON s.id = m.pool_id WHERE m.id = :i"),
            {"i": member_id}).mappings().first()
    if row is None:
        return {"ok": False, "error": "no such account"}
    result: dict[str, Any]
    try:
        credential = stored_credential(settings, row["secret_name"], strict=True,
                                       include_env=False) or ""
        if not credential:
            result = {"ok": False, "error": "the account has no stored login"}
        else:
            run = prove or (lambda eng, cred: _run_proof(settings, eng, cred))
            if proof_runner is None:
                from .setup_service import governed_proof as proof_runner  # type: ignore
            outcome = proof_runner(
                db_engine, settings, project_id, row["engine"], actor,
                prove=lambda: run(row["engine"], credential),
                candidate=_proof_candidate(row["engine"]),
                capacity_group=f"{row['engine']}:pool:{member_id}",
                intent=f"pool-proof-{member_id}")
            result = {"ok": bool(outcome.get("ok")),
                      "error": None if outcome.get("ok") else str(
                          outcome.get("error") or outcome.get("headline")
                          or "the proof did not pass")[:240]}
    except Exception as exc:  # noqa: BLE001 — a proof that could not run is not a pass
        result = {"ok": False, "error": f"the proof could not run ({type(exc).__name__})"}
    stamp = {"ok": result["ok"], "at": datetime.now(timezone.utc).isoformat(),
             "error": result["error"]}
    with begin(db_engine) as c:
        c.execute(text(
            "UPDATE subscription_pool_members SET last_proof = CAST(:pr AS JSONB), "
            "status = CASE WHEN :ok THEN 'active' ELSE 'needs_reauth' END WHERE id = :i"),
            {"pr": json.dumps(stamp), "ok": result["ok"], "i": member_id})
        emit_event(c, project_id, "subscription_pool.member_proved", actor, "subscription_pool",
                   member_id, {"label": row["label"], "ok": result["ok"],
                               "error": result["error"]})
    return result


def _proof_candidate(engine: str) -> dict[str, Any]:
    from .entitlements import WORKSPACE_OPERATOR_ID

    return {"provider": "anthropic" if engine == "claude_code" else "openai",
            "source_kind": "official_cli", "entitlement_kind": "subscription",
            "owner_type": "workspace_operator", "owner_id": WORKSPACE_OPERATOR_ID,
            "cost_mode": "subscription", "auth_method": "official_cli_oauth",
            "connection_status": "connected", "status": "active", "runtime_supported": True,
            "observed_capacity": {"credential_present": True}, "supported_models": [engine],
            "workload_classes_allowed": ["owner_sandbox"], "risk_tier_max": 1,
            "limit_shape": {"max_concurrent": 1}}


def sign_in_store(db_engine: Any, settings: Any, project_id: str, actor: str, engine: str,
                  label: Optional[str] = None, scope: str = "org", *,
                  prove: Optional[Callable[..., dict[str, Any]]] = None,
                  proof_runner: Optional[Callable[..., dict[str, Any]]] = None):
    """The `store` the server-side Sign in hands the printed token to when the person chose
    "Add another account" (subscription_sign_in.start(store=…)): the token becomes a NEW pool
    member, then passes its Connect proof before it is routed. Account 1 is never touched. A
    failed proof is the sign-in's refusal, and shows on the account's row."""
    def store(token: str, *, valid_days: Optional[int] = None) -> None:  # noqa: ARG001
        from ..db import begin
        from .subscription_sign_in import StoreRefused

        try:
            with begin(db_engine) as connection:
                member = add_signed_in_account(connection, settings, project_id, engine,
                                               (token or "").strip(), actor=actor, label=label,
                                               scope=scope)
        except PoolError as exc:
            raise StoreRefused(str(exc)) from None
        proof = prove_member(db_engine, settings, project_id, member["id"], actor,
                             prove=prove, proof_runner=proof_runner)
        if not proof["ok"]:
            raise StoreRefused(f"{member['label']} signed in but did not pass its check, so it "
                               f"is not in use: {proof['error']}")
    return store


def update_member(connection: Connection, settings: Any, project_id: str, member_id: str, *,
                  actor: str, status: Optional[str] = None, position: Optional[int] = None,
                  credential: Optional[str] = None, primary: Optional[bool] = None,
                  label: Optional[str] = None) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT m.*, s.engine FROM subscription_pool_members m JOIN subscription_pools s "
        "ON s.id = m.pool_id WHERE m.id = :i AND " + _owns(connection, project_id)[0]),
        {**_owns(connection, project_id)[1], "i": member_id}).mappings().first()
    if row is None:
        raise LookupError(f"no pool account {member_id!r} in this project")
    if status is not None and status not in MEMBER_STATUSES:
        raise PoolError(f"status must be one of: {', '.join(MEMBER_STATUSES)}")
    if credential is not None:
        if row["secret_name"] == BASE_SECRET[row["engine"]]:
            # THE INSTALL'S OWN LOGIN (adopted as account 1) is replaced only through Setup's
            # Sign in, which keeps its origin and references — never silently from a pool row.
            raise PoolError("this account is the install's own login: replace it with Sign in "
                            "in Setup › Models")
        # A fresh login for this seat: through the same store key the runs read and sync.
        _store(settings).put(_store_key(row["secret_name"]),
                             _validate_credential(row["engine"], credential))
        status = status or "active"
    new_label = " ".join(str(label or "").split())[:80] or None
    if new_label and connection.execute(text(
            "SELECT 1 FROM subscription_pool_members WHERE pool_id = :p AND id <> :i "
            "AND lower(label) = lower(:l)"),
            {"p": row["pool_id"], "i": member_id, "l": new_label}).first():
        raise PoolError(f"the pool already has an account named {new_label!r}")
    if primary:
        # ONE primary per pool: marking one unmarks the rest. A paused account cannot lead.
        connection.execute(text("UPDATE subscription_pool_members SET is_primary = FALSE "
                                "WHERE pool_id = :p"), {"p": row["pool_id"]})
    connection.execute(text(
        "UPDATE subscription_pool_members SET status = COALESCE(:s, status), "
        "position = COALESCE(:pos, position), label = COALESCE(:l, label), "
        "is_primary = COALESCE(CAST(:pr AS BOOLEAN), is_primary) WHERE id = :i"),
        {"s": status, "pos": position, "l": new_label, "pr": primary, "i": member_id})
    emit_event(connection, project_id, "subscription_pool.member_changed", actor,
               "subscription_pool", row["pool_id"],
               {"member_id": member_id, "status": status, "position": position,
                "login_replaced": credential is not None, "primary": primary,
                "label": new_label})
    out = _member_row(connection.execute(text(
        "SELECT * FROM subscription_pool_members WHERE id = :i"), {"i": member_id}
    ).mappings().one())
    out.pop("secret_name", None)
    return out


def remove_member(connection: Connection, settings: Any, project_id: str, member_id: str, *,
                  actor: str) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT m.id, m.pool_id, m.secret_name, s.engine FROM subscription_pool_members m JOIN "
        "subscription_pools s ON s.id = m.pool_id WHERE m.id = :i AND "
        + _owns(connection, project_id)[0]),
        {**_owns(connection, project_id)[1], "i": member_id}).mappings().first()
    if row is None:
        raise LookupError(f"no pool account {member_id!r} in this project")
    connection.execute(text("DELETE FROM subscription_pool_members WHERE id = :i"),
                       {"i": member_id})
    adopted = row["secret_name"] == BASE_SECRET.get(row["engine"])
    if not adopted:
        # THE INSTALL'S LOGIN STAYS: an adopted account 1 points at the install-wide login,
        # which every project and the single-login path use; leaving the pool never deletes it
        # (disconnecting it is Setup's). Only a seat's own login is deleted with the seat.
        try:
            _store(settings).delete(_store_key(row["secret_name"]))
        except Exception:  # noqa: BLE001 — the account is gone either way; say so in the log
            _log.warning("could not delete the stored login of pool account %s", member_id)
    emit_event(connection, project_id, "subscription_pool.member_removed", actor,
               "subscription_pool", row["pool_id"],
               {"member_id": member_id, "install_login_kept": adopted})
    return {"removed": member_id}


def set_pool_status(connection: Connection, project_id: str, pool_id: str, *, status: str,
                    actor: str) -> dict[str, Any]:
    if status not in ("active", "disabled"):
        raise PoolError("status must be active or disabled")
    pool = _pool_by_id(connection, project_id, pool_id)
    connection.execute(text(
        "UPDATE subscription_pools SET status = :s, updated_at = now() WHERE id = :i"),
        {"s": status, "i": pool_id})
    emit_event(connection, project_id, "subscription_pool.status", actor, "subscription_pool",
               pool_id, {"status": status})
    return _pool_by_id(connection, project_id, pool_id)


def delete_pool(connection: Connection, project_id: str, pool_id: str, *,
                actor: str) -> dict[str, Any]:
    """Delete an EMPTY pool (BOSSAPP-1042). A pool that still has accounts is refused, never
    cascaded: each account's login lives in the secret store, and only remove_member deletes it
    there — a cascading row delete would leave those logins behind with nothing pointing at them.
    """
    pool = _pool_by_id(connection, project_id, pool_id)
    n = len(pool["members"])
    if n:
        raise PoolError(
            f"remove its {n} account{'s' if n != 1 else ''} first — each Remove also deletes "
            "that account's stored login; then the empty pool can go")
    connection.execute(text("DELETE FROM subscription_pools WHERE id = :i"), {"i": pool_id})
    emit_event(connection, project_id, "subscription_pool.deleted", actor, "subscription_pool",
               pool_id, {"engine": pool["engine"]})
    return {"deleted": pool_id}


def before_project_purge(connection: Connection, settings: Any, project_id: str) -> dict[str, int]:
    """A project is about to be deleted (projects_delete.purge), and its pools would go with it
    by cascade, leaving their seats' stored logins behind with nothing pointing at them.
    The ORG's pool anchored here moves to another project of the org (the org keeps its seats);
    every pool that goes deletes its seats' own logins first. An adopted install login is never
    deleted (it is the install's, see remove_member)."""
    out = {"moved": 0, "logins_deleted": 0}
    pools = connection.execute(text(
        "SELECT id, engine, scope, org_id FROM subscription_pools WHERE project_id = :p"),
        {"p": project_id}).mappings().all()
    for pool in pools:
        if pool["scope"] == "org" and pool["org_id"]:
            other = connection.execute(text(
                "SELECT p.id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE w.org_id = :o AND p.id <> :p ORDER BY p.created_at, p.id LIMIT 1"),
                {"o": pool["org_id"], "p": project_id}).scalar()
            if other:
                connection.execute(text(
                    "UPDATE subscription_pools SET project_id = :n, updated_at = now() "
                    "WHERE id = :i"), {"n": other, "i": pool["id"]})
                out["moved"] += 1
                continue
        for name in connection.execute(text(
                "SELECT secret_name FROM subscription_pool_members WHERE pool_id = :i"),
                {"i": pool["id"]}).scalars():
            if name == BASE_SECRET.get(pool["engine"]):
                continue
            try:
                _store(settings).delete(_store_key(name))
                out["logins_deleted"] += 1
            except Exception:  # noqa: BLE001 — the purge goes on; the log says which one
                _log.warning("could not delete a pool login while deleting project %s", project_id)
    return out


# ── choosing ─────────────────────────────────────────────────────────────────────────────────

def install_login_refused(connection: Connection, settings: Any, project_id: str,
                          engine: str) -> bool:
    """Is the install's own login for `engine` refused (expired / revoked)? Setup's one status
    function (subscription_sign_in.last_login_rejected) over the project and its org's model
    home. An adopted account 1 IS that login, so it cannot take work while this holds.
    Never raises: anything unreadable is "not refused"."""
    try:
        from . import org_models
        from .subscription_sign_in import last_login_rejected

        with connection.begin_nested():
            home = org_models.home_project(connection, org_models.org_of(connection, project_id))
            return bool(last_login_rejected(connection, [project_id, home or ""], engine,
                                            settings))
    except Exception:  # noqa: BLE001
        return False


def routable(connection: Connection, settings: Any, project_id: str, engine: str,
             members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The members that may take work: active, and — for the adopted install login — only while
    the install's login is not refused (the engine-wide login state, BOSSAPP-1343 review)."""
    live = [m for m in members if m["status"] == "active"]
    if settings is not None and any(m.get("secret_name") == BASE_SECRET[engine] for m in live) \
            and install_login_refused(connection, settings, project_id, engine):
        live = [m for m in live if m.get("secret_name") != BASE_SECRET[engine]]
    return live


def router_account(connection: Connection, settings: Any, project_id: Optional[str],
                   engine: str, now: Optional[datetime] = None) -> Optional[str]:
    """The account the model router reads for `engine` when the project routes through a pool:
    the seat a new build would be given (the primary with plenty of room, else the most room);
    with every seat at its limit, the one that resets first — so the router's headroom is the
    POOL's, not the install login's (BOSSAPP-1343 review). None without an active pool."""
    from .model_router.headroom import EXHAUSTED, PLENTY

    if not project_id or engine not in ENGINES:
        return None
    pool = None
    for scope in ("project", "org"):
        candidate = get_pool(connection, project_id, engine, scope=scope)
        if candidate is not None and candidate["status"] == "active":
            pool = candidate
            break
    live = routable(connection, settings, project_id, engine, pool["members"]) if pool else []
    if not live:
        return None
    rooms = headroom_of(connection, live, engine, now=now)
    usable = [m for m in live if rooms[m["id"]].state != EXHAUSTED]
    if not usable:
        far = datetime.max.replace(tzinfo=timezone.utc)
        return min(live, key=lambda m: rooms[m["id"]].resets_at or far)["account_key"]
    primary = next((m for m in usable if m.get("primary") and rooms[m["id"]].state == PLENTY),
                   None)
    best = primary or max(usable, key=lambda m: (rooms[m["id"]].left
                                                 if rooms[m["id"]].left is not None else 1.0))
    return best["account_key"]


def assign(connection: Connection, project_id: str, engine: str, task_ref: Optional[str], *,
           now: Optional[datetime] = None, settings: Any = None) -> Optional[Assignment]:
    """The member this run spends on, or None when the project has no active pool for `engine`.
    Raises PoolExhausted when every active member is at its limit. With `settings`, an adopted
    install login whose login is refused is skipped (`routable`)."""
    from .model_router.headroom import EXHAUSTED, PLENTY

    # PROJECT POOL -> ORG POOL -> (None: the single install login). A project's own pool
    # overrides the org's for that project; a paused one hands over to the org's.
    pool = None
    for scope in ("project", "org"):
        candidate = get_pool(connection, project_id, engine, scope=scope)
        if candidate is not None and candidate["status"] == "active":
            pool = candidate
            break
    if pool is None:
        return None
    live = routable(connection, settings, project_id, engine, pool["members"])
    if not live:
        return None
    rooms = headroom_of(connection, live, engine, now=now)
    usable = [m for m in live if rooms[m["id"]].state != EXHAUSTED]
    if not usable:
        resets = [rooms[m["id"]].resets_at for m in live if rooms[m["id"]].resets_at]
        earliest = min(resets) if resets else None
        raise PoolExhausted(exhausted_words(pool["name"], earliest), earliest)
    ref = str(task_ref or "").strip()
    pinned = None
    pin = None
    if ref:
        pin = connection.execute(text(
            "SELECT member_id FROM subscription_pool_pins WHERE pool_id = :p AND task_ref = :t"),
            {"p": pool["id"], "t": ref}).scalar()
        pinned = next((m for m in usable if m["id"] == pin), None)
    # THE PRIMARY LEADS while it has plenty of room; otherwise the most headroom wins.
    primary = next((m for m in usable if m.get("primary")
                    and rooms[m["id"]].state == PLENTY), None)
    chosen = pinned or primary or sorted(
        usable, key=lambda m: (-(rooms[m["id"]].left if rooms[m["id"]].left is not None else 1.0),
                               m["position"], m["last_assigned_at"] or ""))[0]
    reason = ""
    if ref and pinned is None:
        old = next((m for m in live if m["id"] == pin), None) if pin else None
        if old is not None and old["id"] != chosen["id"]:
            reason = (f"moved off {old['label']} (at its limit"
                      + (f", resets {rooms[old['id']].resets_at.isoformat()}"
                         if rooms[old['id']].resets_at else "")
                      + f") to {chosen['label']}")
            emit_event(connection, project_id, "subscription_pool.moved", "system",
                       "subscription_pool", pool["id"],
                       {"task_ref": ref, "from": old["label"], "to": chosen["label"],
                        "engine": engine, "reason": reason})
        connection.execute(text(
            "INSERT INTO subscription_pool_pins (pool_id, task_ref, member_id) VALUES (:p, :t, :m) "
            "ON CONFLICT (pool_id, task_ref) DO UPDATE SET member_id = EXCLUDED.member_id, "
            "pinned_at = now()"), {"p": pool["id"], "t": ref, "m": chosen["id"]})
    connection.execute(text(
        "UPDATE subscription_pool_members SET last_assigned_at = clock_timestamp() WHERE id = :i"),
        {"i": chosen["id"]})
    return Assignment(pool_id=pool["id"], member_id=chosen["id"], label=chosen["label"],
                      engine=engine, secret_name=chosen["secret_name"],
                      account_key=chosen["account_key"], pinned=pinned is not None,
                      reason=reason)


def assignment_for(engine: str, settings: Any = None) -> Optional[Assignment]:
    """The launchers' seam: the member this run spends on, from the run's LLM context (its
    project, and its run as the task pin). Also puts the member's account on the context so
    the run's rate-limit observations land on it. None — no project, no pool, no database — means
    the run uses the single login exactly as before. PoolExhausted propagates."""
    if engine not in ENGINES:
        return None
    try:
        from .llm_telemetry import get_llm_context, set_llm_context

        ctx = get_llm_context()
    except Exception:  # noqa: BLE001
        return None
    project_id = ctx.get("project_id")
    if not project_id:
        return None
    task_ref = ctx.get("workflow_run_id") or ctx.get("run_id")
    try:
        from ..db import begin, get_engine

        engine_db = ctx.get("db_engine") or get_engine(settings)
        with begin(engine_db) as c:
            with c.begin_nested():
                where, args = _owns(c, project_id)
                exists = c.execute(text(
                    f"SELECT 1 FROM subscription_pools s WHERE s.engine = :e AND {where} "
                    "AND s.status = 'active'"), {**args, "e": engine}).first()
            if not exists:
                return None
            chosen = assign(c, project_id, engine, task_ref, settings=settings)
    except PoolExhausted:
        raise
    except Exception:  # noqa: BLE001 — no table, no database: the single login, as before
        _log.debug("pool lookup failed; using the single login", exc_info=True)
        return None
    if chosen is not None:
        set_llm_context(pool_account=chosen.account_key, pool_engine=engine,
                        pool_route_reason=chosen.reason or None)
    return chosen


def mark_needs_reauth(settings: Any, member_id: str) -> None:
    """A member whose login the provider refused is taken out of rotation until a person replaces
    its login (update_member with a credential). Never raises."""
    try:
        from ..db import begin, get_engine

        with begin(get_engine(settings)) as c:
            c.execute(text("UPDATE subscription_pool_members SET status = 'needs_reauth' "
                           "WHERE id = :i AND status = 'active'"), {"i": member_id})
    except Exception:  # noqa: BLE001
        _log.warning("could not mark pool account %s as needing a new login", member_id)



def mark_refused_from_context() -> bool:
    """A run's engine was refused its login (harness_failures kind auth_refused): when that run
    was spending on a pool member (its account key rides on the LLM context), THAT member goes
    out of rotation (needs_reauth) and the pool's other accounts carry on. Covers every engine
    and launcher, so it needs no per-launcher wiring. Never raises; True when a member was marked."""
    try:
        from .llm_telemetry import get_llm_context

        ctx = get_llm_context() or {}
        key = ctx.get("pool_account")
        db = ctx.get("db_engine")
        if not key or db is None:
            return False
        with db.begin() as c:
            done = c.execute(text(
                "UPDATE subscription_pool_members SET status = 'needs_reauth' "
                "WHERE account_key = :k AND status = 'active'"), {"k": key})
            return bool(done.rowcount)
    except Exception:  # noqa: BLE001
        _log.warning("could not take a refused pool account out of rotation", exc_info=True)
        return False
