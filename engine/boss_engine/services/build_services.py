"""BUILD SERVICES — a throwaway PostgreSQL database per build run.

The build sandbox has Node, Python and Playwright but no database server and no Docker, so an
app whose tests need a real Postgres could only mock it — and a build gate that passes on a
mock is evidence of nothing. A run that asks for it now gets its own database on a separate
Postgres server (`build-services` in community-compose.yml; any server BOSS can reach as a
superuser via BOSS_BUILD_SERVICES_PG_URL). NEVER the BOSS database.

ASKING FOR IT (opt in; nothing changes for a run that does not):

* the repo's ``boss.verify.yaml`` declares ``services: [postgres]``, or
* the project setting ``build_services`` is ``postgres``.

WHAT A RUN GETS. At run start (after the clone, before any agent) BOSS creates, on the
build-services server, one role and one database with the same random name
(``<prefix><16 hex>``) and a random 48-hex-char password:

* the role is LOGIN only: no superuser, no createdb/createrole, no replication, no bypassrls,
  a connection limit; it owns its database and nothing else;
* CONNECT on that database is revoked from PUBLIC: no other role (another run's) may connect;
* the server's pg_hba (compose) has ``host sameuser all all scram-sha-256``: a run role can
  authenticate only to the database named like itself, whatever grants say;
* the password is sent to the server as a SCRAM-SHA-256 verifier computed here, so its
  plaintext never reaches the server (or its statement log). It is held only in this process's
  memory and in the run's own process environment; it is never written to the BOSS database,
  an event, the activity log or a run log — every place BOSS writes run output scrubs the live
  values (``scrub``; redaction.py and sandbox.py call it);
* the role and database carry a comment {"boss": "build-services", "run": sha256(run id)[:24],
  "install": ..., "created": epoch}: no run id, no secret, enough for the reaper.

``DATABASE_URL`` (libpq form, ``postgresql://role:pw@host:port/db``) and ``PGHOST PGPORT PGUSER
PGPASSWORD PGDATABASE`` are injected into the environment of every process the run spawns
(owned_process, after the agent allow-list — which otherwise withholds DATABASE_URL on purpose).

WHAT ISOLATES RUN A FROM RUN B:
  1. credentials: B's password exists only in B's processes (each run is its own uid; another
     uid cannot read /proc/<pid>/environ or B's 2770 run directory) and in the API's memory;
  2. authorization: A's role has no CONNECT on B's database (revoked from PUBLIC; only B's role,
     its owner, has it) and pg_hba ``sameuser`` refuses A's role on any database but A's;
  3. catalog: pg_authid (password verifiers) is superuser-only; a run sees other databases'
     random names and hashed-run comments, nothing that grants access;
  4. network: the sandbox uid pool may reach the build-services port only (entrypoint.sh adds
     BOSS_BUILD_SERVICES_SANDBOX_PORT, which is not 5432, so the BOSS database stays rejected).
     The network fence is per pool, not per uid: per-run separation is 1-3.

ENDING. The run's own ``finally`` drops the database (WITH FORCE: open sessions are ended) and
the role. A run whose process died never gets there: the resource reaper (``reap``) drops any
database/role of this prefix and install whose run is no longer live and that is older than a
grace period. Each database is also an ``owned_resources`` row (kind ``build_database``).

UNAVAILABLE IS SAID, NEVER PAPERED OVER. A run that asked for Postgres when no server is
configured, the server is unreachable, or the run's commands cannot reach a network (the docker
command runtime, a microVM) fails with "Build services unavailable: <why>" — it never runs its
tests without the database it declared.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import quote, unquote, urlsplit

_log = logging.getLogger("boss.build_services")

SERVER_URL_ENV = "BOSS_BUILD_SERVICES_PG_URL"
PASSWORD_FILE_ENV = "BOSS_BUILD_SERVICES_PG_PASSWORD_FILE"
RUN_HOST_ENV = "BOSS_BUILD_SERVICES_PG_RUN_HOST"     # host:port the run is told, when it differs
PREFIX_ENV = "BOSS_BUILD_SERVICES_PREFIX"
DEFAULT_PREFIX = "boss_run_"
SUPPORTED = ("postgres",)
SETTING_KEY = "build_services"
CONNECTION_LIMIT = 32
REAP_MIN_AGE_SECONDS = 300
RESOURCE_KIND = "build_database"
_NAME_OK = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
#: The variables a run is given. Kept here so the brief, the docs and the tests name the same set.
ENV_NAMES = ("DATABASE_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE")


class BuildServicesUnavailable(RuntimeError):
    """The run declared a service BOSS cannot give it. The message is the operator's next step."""

    def __init__(self, why: str) -> None:
        super().__init__(f"Build services unavailable: {why}")


# --------------------------------------------------------------------------------- opt-in
def parse_services(value: Any, where: str = "services") -> tuple[str, ...]:
    """``services: [postgres]`` → ("postgres",). Unknown names are an error naming what exists:
    a typo must not silently run the tests without the database."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{where} must be a list, e.g. services: [postgres]")
    out: list[str] = []
    for item in value:
        name = str(item or "").strip().lower()
        if name not in SUPPORTED:
            raise ValueError(f"{where}: unknown service {item!r}; BOSS provides {list(SUPPORTED)}")
        if name not in out:
            out.append(name)
    return tuple(out)


def requested(manifest_services: Iterable[str] = (), project_setting: Any = None) -> tuple[str, ...]:
    """What this run asked for EXPLICITLY: the repo's declaration plus the project's override
    (Always). `decide` adds what BOSS detects and applies Never."""
    out = list(manifest_services or ())
    if mode_of(project_setting) == ALWAYS and "postgres" not in out:
        out.append("postgres")
    return tuple(out) if mode_of(project_setting) != NEVER else ()


# --------------------------------------------------------------------------------- detection
#: The project's override (setting `build_services`): Auto (default) gives a build its database
#: when BOSS detects the app needs one; Always gives every build one; Never gives none, even
#: when the repo declares it. Older values read as: "postgres" = Always, "off" = Auto.
AUTO, ALWAYS, NEVER = "auto", "always", "never"
MODES = (AUTO, ALWAYS, NEVER)


def mode_of(setting: Any) -> str:
    value = str(setting or "").strip().lower()
    if value in ("always", "postgres", "on", "1", "true"):
        return ALWAYS
    if value in ("never", "none", "false", "0"):
        return NEVER
    return AUTO


_PG_WORDS = re.compile(r"\b(postgres(?:ql)?|psql)\b", re.IGNORECASE)
_PG_URL = re.compile(r"postgres(?:ql)?(?:\+\w+)?://", re.IGNORECASE)
_PY_DEPS = re.compile(r"^\s*(psycopg2?(?:-binary)?|psycopg\[?|asyncpg|pg8000)\b", re.IGNORECASE | re.MULTILINE)
_PLAN_LINE = re.compile(r"^\s*[-*]?\s*database\s*:\s*(postgres(?:ql)?)\b", re.IGNORECASE | re.MULTILINE)
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "vendor"}
_SCAN_FILES = 3000
_SCAN_BYTES = 256_000


def _texts(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if str(k) not in ("instructions", "token", "secret"):
                yield from _texts(v, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value[:200]:
            yield from _texts(v, depth + 1)


def detect_in_code(root: Any) -> list[str]:
    """Where the app's own code says it uses PostgreSQL (bounded scan; never reads secrets out)."""
    import json as _json
    from pathlib import Path

    if not root:
        return []
    base = Path(str(root))
    if not base.is_dir():
        return []
    found: list[str] = []
    seen = 0
    for path in sorted(base.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.relative_to(base).parts[:-1]):
            continue
        if not path.is_file():
            continue
        seen += 1
        if seen > _SCAN_FILES or len(found) >= 3:
            break
        name = path.name.lower()
        rel = path.relative_to(base).as_posix()
        try:
            if path.stat().st_size > _SCAN_BYTES:
                continue
            text_ = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if name.startswith("requirements") and name.endswith(".txt") or name in ("pyproject.toml", "pipfile", "setup.py", "setup.cfg"):
            if _PY_DEPS.search(text_) or re.search(r"[\"'](psycopg2?(?:-binary)?|psycopg|asyncpg)", text_):
                found.append(f"{rel} depends on a PostgreSQL driver")
        elif name == "package.json":
            try:
                pkg = _json.loads(text_)
            except ValueError:
                continue
            deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            hit = [d for d in ("pg", "postgres", "pg-promise", "@neondatabase/serverless", "@vercel/postgres") if d in deps]
            if hit:
                found.append(f"{rel} depends on {hit[0]}")
        elif name == "schema.prisma":
            if re.search(r'provider\s*=\s*"postgres(?:ql)?"', text_):
                found.append(f"{rel} uses the postgresql provider")
        elif name.startswith(("docker-compose", "compose")) and name.endswith((".yml", ".yaml")):
            if re.search(r"image:\s*['\"]?postgres", text_):
                found.append(f"{rel} runs a postgres service")
        elif path.suffix.lower() in (".py", ".ts", ".js", ".mjs", ".cjs", ".env", ".example", ".toml", ".ini", ".yml", ".yaml"):
            if _PG_URL.search(text_):
                found.append(f"{rel} has a postgresql:// URL")
    return found


def detect(spec: Any = None, title: str = "", root: Any = None, design: Any = None) -> list[str]:
    """Why this build needs a PostgreSQL database, from every signal BOSS has before the
    database is created; empty when nothing says so.

      the brief or PRD names PostgreSQL;
      a template the build applies needs it (`services` / `needs` naming postgres);
      the planner or architect recorded `database: postgres` (spec.plan / the run's design);
      the app's existing code declares it (drivers, Prisma, compose, connection URLs)."""
    reasons: list[str] = []
    spec = spec if isinstance(spec, dict) else {}
    brief = " ".join([title or "", *[t for k, v in spec.items()
                                     if k not in ("template", "plan", "services", "instructions")
                                     for t in _texts(v)]])
    if _PG_WORDS.search(brief):
        reasons.append("the brief names PostgreSQL")
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    for key in ("services", "needs"):
        wanted = [str(x).lower() for x in (template.get(key) or spec.get(key) or [])] \
            if isinstance(template.get(key) or spec.get(key), (list, tuple)) else []
        if "postgres" in wanted or "postgresql" in wanted or "database:postgres" in wanted:
            reasons.append(f"the template {template.get('key') or ''} needs it".replace("  ", " "))
            break
    plans = [spec.get("plan"), spec.get("database"), design]
    for plan in plans:
        if isinstance(plan, dict) and str(plan.get("database") or "").lower().startswith("postgres"):
            reasons.append("the plan says database: postgres")
            break
        if isinstance(plan, str) and (plan.strip().lower().startswith("postgres") or _PLAN_LINE.search(plan)):
            reasons.append("the plan says database: postgres")
            break
        if isinstance(plan, dict) and isinstance(plan.get("plan"), str) and _PLAN_LINE.search(plan["plan"]):
            reasons.append("the plan says database: postgres")
            break
    reasons.extend(detect_in_code(root))
    return reasons


def decide(manifest_services: Iterable[str] = (), setting: Any = None, *, spec: Any = None,
           title: str = "", root: Any = None, design: Any = None) -> dict[str, Any]:
    """The build's database decision, made BEFORE the database is created: {"services": (...),
    "mode": auto|always|never, "why": [...]}. Never wins over everything, the repo's own
    `services: [postgres]` included; Always needs no reason; Auto needs one."""
    mode = mode_of(setting)
    declared = tuple(manifest_services or ())
    if mode == NEVER:
        return {"services": (), "mode": mode, "why": ["Never, in this project's settings"]}
    why: list[str] = []
    if "postgres" in declared:
        why.append("boss.verify.yaml declares services: [postgres]")
    if mode == ALWAYS:
        why.append("Always, in this project's settings")
    if not why:
        why = detect(spec, title, root, design)
    return {"services": ("postgres",) if why else (), "mode": mode, "why": why}


# --------------------------------------------------------------------------------- server
def prefix() -> str:
    raw = (os.environ.get(PREFIX_ENV) or DEFAULT_PREFIX).strip().lower()
    if not _NAME_OK.match(raw):
        raise BuildServicesUnavailable(f"{PREFIX_ENV} must be lower-case letters, digits and _")
    return raw


@dataclass(frozen=True)
class _Server:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    dbname: str

    def conninfo(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "user": self.user,
                "password": self.password, "dbname": self.dbname, "connect_timeout": 5,
                "application_name": "boss-build-services"}


def server(url: Optional[str] = None) -> Optional[_Server]:
    """The build-services server, or None when none is configured."""
    raw = (url if url is not None else os.environ.get(SERVER_URL_ENV) or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw.replace("+psycopg2://", "://").replace("+psycopg://", "://"))
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
        raise BuildServicesUnavailable(f"{SERVER_URL_ENV} must be a postgresql:// URL")
    password = unquote(parts.password or "")
    pw_file = (os.environ.get(PASSWORD_FILE_ENV) or "").strip()
    if not password and pw_file:
        try:
            with open(pw_file, encoding="utf-8") as fh:
                password = fh.read().strip()
        except OSError as exc:
            raise BuildServicesUnavailable(
                f"the build-services password file is unreadable ({type(exc).__name__})") from None
    return _Server(host=parts.hostname, port=parts.port or 5432,
                   user=unquote(parts.username or "postgres"), password=password,
                   dbname=(parts.path or "/postgres").lstrip("/") or "postgres")


def _connect(srv: _Server) -> Any:
    import psycopg

    try:
        return psycopg.connect(**srv.conninfo(), autocommit=True)
    except Exception as exc:  # noqa: BLE001 — the reason, never the conninfo
        kind = type(exc).__name__
        raise BuildServicesUnavailable(
            f"cannot reach the build-services Postgres at {srv.host}:{srv.port} ({kind}). "
            "Start it (`docker compose up -d build-services`) or point "
            f"{SERVER_URL_ENV} at a reachable server") from None


def status() -> dict[str, Any]:
    """For posture and the system doctor: configured, reachable — never a credential."""
    try:
        srv = server()
    except BuildServicesUnavailable as exc:
        return {"configured": True, "reachable": False, "problem": str(exc)}
    if srv is None:
        return {"configured": False, "reachable": False,
                "problem": f"{SERVER_URL_ENV} is not set"}
    try:
        with contextlib.closing(_connect(srv)):
            return {"configured": True, "reachable": True, "host": srv.host, "port": srv.port}
    except BuildServicesUnavailable as exc:
        return {"configured": True, "reachable": False, "problem": str(exc)}


# --------------------------------------------------------------------------------- leases
def scram_verifier(password: str, *, iterations: int = 4096, salt: Optional[bytes] = None) -> str:
    """The SCRAM-SHA-256 verifier Postgres stores (RFC 7677 / pg's scram_build_secret): the
    server authenticates against it, so the plaintext never has to be sent."""
    salt = salt or secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def run_tag(run_id: str) -> str:
    return hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:24]


@dataclass
class PostgresLease:
    run_id: str
    name: str                      # the database AND the role
    host: str
    port: int
    password: str = field(repr=False)
    ledger_handle: str = ""

    def env(self) -> dict[str, str]:
        url = (f"postgresql://{quote(self.name)}:{quote(self.password, safe='')}"
               f"@{self.host}:{self.port}/{quote(self.name)}")
        return {"DATABASE_URL": url, "PGHOST": self.host, "PGPORT": str(self.port),
                "PGUSER": self.name, "PGPASSWORD": self.password, "PGDATABASE": self.name}

    def describe(self) -> str:
        """For the run's log and the brief: never the password."""
        return f"postgres database {self.name} on {self.host}:{self.port}"


_LEASES: dict[str, PostgresLease] = {}
_LOCK = threading.Lock()


def _run_endpoint(srv: _Server) -> tuple[str, int]:
    """Where the RUN connects. The API may reach the server under one name and the sandbox under
    another (a published port); BOSS_BUILD_SERVICES_PG_RUN_HOST=host[:port] says so."""
    raw = (os.environ.get(RUN_HOST_ENV) or "").strip()
    if not raw:
        return srv.host, srv.port
    host, _, port = raw.rpartition(":") if ":" in raw else (raw, "", "")
    return (host or raw), int(port) if port.isdigit() else srv.port


def provision(run_id: str, *, install: str = "", server_url: Optional[str] = None,
              owner: Any = None) -> PostgresLease:
    """Create run `run_id`'s database and role and bind them to the run (idempotent per run).
    Raises BuildServicesUnavailable with the reason."""
    with _LOCK:
        existing = _LEASES.get(run_id)
    if existing is not None:
        return existing
    srv = server(server_url)
    if srv is None:
        raise BuildServicesUnavailable(
            f"this run declared services: [postgres], but no build-services server is "
            f"configured ({SERVER_URL_ENV} is not set). Start the build-services container "
            "or remove the declaration")
    from psycopg import sql

    name = f"{prefix()}{secrets.token_hex(8)}"
    password = secrets.token_hex(24)
    comment = json.dumps({"boss": "build-services", "run": run_tag(run_id),
                          "install": install or "", "created": int(time.time())})
    ident = sql.Identifier(name)
    host, port = _run_endpoint(srv)
    lease = PostgresLease(run_id=run_id, name=name, host=host, port=port, password=password,
                          ledger_handle=f"pg:{srv.host}:{srv.port}/{name}")
    # Registered for scrubbing BEFORE the password exists anywhere else.
    _remember_secret(password)
    try:
        with contextlib.closing(_connect(srv)) as conn:
            try:
                conn.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {} PASSWORD {}").format(
                        ident, sql.Literal(CONNECTION_LIMIT),
                        sql.Literal(scram_verifier(password))))
                conn.execute(sql.SQL("COMMENT ON ROLE {} IS {}").format(ident, sql.Literal(comment)))
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(ident, ident))
                conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(ident))
                conn.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                    ident, ident))
                conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    ident, sql.Literal(comment)))
            except Exception as exc:  # noqa: BLE001
                _drop(conn, name)
                raise BuildServicesUnavailable(
                    f"the build-services server refused to create the run's database "
                    f"({type(exc).__name__}: {scrub(str(exc))[:200]})") from None
    except BaseException:
        _forget_secret(password)
        raise
    with _LOCK:
        _LEASES[run_id] = lease
    with contextlib.suppress(Exception):
        from . import resource_ledger

        resource_ledger.record(RESOURCE_KIND, lease.ledger_handle, owner=owner,
                               meta={"database": name, "service": "postgres"})
    _log.info("build-services run=%s %s", run_id, lease.describe())
    return lease


def _drop(conn: Any, name: str) -> None:
    from psycopg import sql

    ident = sql.Identifier(name)
    with contextlib.suppress(Exception):
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(ident))
    with contextlib.suppress(Exception):
        conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident))


def teardown(run_id: str, *, server_url: Optional[str] = None, reason: str = "run_ended") -> bool:
    """Drop run `run_id`'s database and role (it is ended, whatever its sessions were doing).
    True when there was one. Never raises: a teardown that fails is the reaper's to finish."""
    with _LOCK:
        lease = _LEASES.pop(run_id, None)
    if lease is None:
        return False
    try:
        srv = server(server_url)
        if srv is not None:
            with contextlib.closing(_connect(srv)) as conn:
                _drop(conn, lease.name)
        with contextlib.suppress(Exception):
            from . import resource_ledger

            resource_ledger.release(RESOURCE_KIND, lease.ledger_handle, reason)
        _log.info("build-services run=%s dropped %s", run_id, lease.name)
    except Exception as exc:  # noqa: BLE001 — the reaper finishes it
        _log.warning("build-services run=%s drop of %s failed (%s); the reaper will retry",
                     run_id, lease.name, type(exc).__name__)
    finally:
        _forget_secret(lease.password)
    return True


def lease_for(run_id: str) -> Optional[PostgresLease]:
    with _LOCK:
        return _LEASES.get(run_id)


def env_for_owner(owner: Any) -> dict[str, str]:
    """The service variables of the run `owner` belongs to (innermost bound owner wins)."""
    if owner is None:
        return {}
    ids = owner.ids() if hasattr(owner, "ids") else (str(owner),)
    with _LOCK:
        for oid in reversed(ids):
            lease = _LEASES.get(oid)
            if lease is not None:
                return lease.env()
    return {}


def brief(lease: Optional[PostgresLease]) -> str:
    """The sentence the agents read. Names the variables, never their values."""
    if lease is None:
        return ""
    return ("BUILD SERVICES: this run declared services: [postgres]. A throwaway PostgreSQL "
            "database for this run only is ready: DATABASE_URL (postgresql://…) and PGHOST, "
            "PGPORT, PGUSER, PGPASSWORD, PGDATABASE are set in the environment of every command "
            "you run, the tests included. Read them from the environment (never hard-code or "
            "print them); create the schema your tests need; the database is empty at start "
            "and dropped when the run ends.")


# --------------------------------------------------------------------------------- reaping
def reap(live_run_ids: Iterable[str], *, install: str = "", server_url: Optional[str] = None,
         min_age: float = REAP_MIN_AGE_SECONDS) -> dict[str, Any]:
    """Drop every database/role of this prefix (and install) whose run is not live and that is
    older than `min_age`. Only objects carrying BOSS's own comment are ever touched."""
    srv = server(server_url)
    if srv is None:
        return {"configured": False, "dropped": []}
    live = set(live_run_ids)
    if "*" in live:
        return {"dropped": [], "kept": "unknown liveness"}
    live_tags = {run_tag(r) for r in live}
    with _LOCK:
        live_tags |= {run_tag(r) for r in _LEASES}
        mine = {lease.name for lease in _LEASES.values()}
    pre = prefix()
    dropped: list[str] = []
    now = time.time()
    with contextlib.closing(_connect(srv)) as conn:
        rows = conn.execute(
            "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database "
            "WHERE starts_with(datname, %s) "
            "UNION ALL SELECT rolname, shobj_description(oid, 'pg_authid') FROM pg_roles "
            "WHERE starts_with(rolname, %s)", (pre, pre)).fetchall()
        seen: dict[str, Optional[str]] = {}
        for name, comment in rows:
            if comment or name not in seen:
                seen[name] = comment
        for name, comment in sorted(seen.items()):
            if name in mine:
                continue
            try:
                meta = json.loads(comment or "")
            except ValueError:
                continue                          # not ours: never touched
            if not isinstance(meta, dict) or meta.get("boss") != "build-services":
                continue
            if install and meta.get("install") and meta.get("install") != install:
                continue                          # another install sharing the server
            if meta.get("run") in live_tags:
                continue
            if now - float(meta.get("created") or now) < min_age:
                continue
            _drop(conn, name)
            dropped.append(name)
            with contextlib.suppress(Exception):
                from . import resource_ledger

                resource_ledger.release(RESOURCE_KIND, f"pg:{srv.host}:{srv.port}/{name}", "reaped")
    if dropped:
        _log.info("build-services reaped %d orphaned database(s): %s", len(dropped), dropped)
    return {"dropped": dropped}


def reap_tick(engine: Any) -> dict[str, Any]:
    """The resource reaper's pass: live = runs this process owns or the DB says are not ended."""
    if server() is None:
        return {"configured": False}
    from . import resource_reaper
    from .resource_ledger import install_id

    live = resource_reaper._live_sandbox_tasks(engine)
    inst = ""
    if engine is not None:
        with contextlib.suppress(Exception), engine.begin() as c:
            inst = install_id(c)
    return reap(live, install=inst)


# --------------------------------------------------------------------------------- scrubbing
_SECRETS: dict[str, int] = {}
_SECRETS_LOCK = threading.Lock()


def _remember_secret(value: str) -> None:
    with _SECRETS_LOCK:
        _SECRETS[value] = _SECRETS.get(value, 0) + 1


def _forget_secret(value: str) -> None:
    # Kept for a while after the run: its output may still be written (a late event, the
    # failure reason). A 48-hex random string never occurs in honest output.
    def _later() -> None:
        with _SECRETS_LOCK:
            n = _SECRETS.get(value, 0) - 1
            if n <= 0:
                _SECRETS.pop(value, None)
            else:
                _SECRETS[value] = n
    timer = threading.Timer(3600, _later)
    timer.daemon = True
    timer.start()


def live_secrets() -> list[str]:
    with _SECRETS_LOCK:
        return list(_SECRETS)


def scrub(value: Any) -> Any:
    """Every live build-service password replaced by [SECRET], in a string or (recursively) a
    dict/list. Cheap when no run holds one."""
    known = live_secrets()
    if not known:
        return value
    return _scrub(value, known, 0)


def _scrub(value: Any, known: list[str], depth: int) -> Any:
    if isinstance(value, str):
        for secret in known:
            if secret in value:
                value = value.replace(secret, "[SECRET]")
        return value
    if depth > 20:
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, known, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, known, depth + 1) for v in value)
    return value
