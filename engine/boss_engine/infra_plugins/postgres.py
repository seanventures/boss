"""official.postgres: PostgreSQL 17 as a compose service on the app's private network.

provision  `res-<name>` with a data volume and a backups volume, NO published port; a LOGIN role
           that owns one database (not a superuser). The only secret is the app's DATABASE_URL.
           The superuser has a random password nobody keeps (set at init, never stored): admin
           work runs inside the container over the local socket (`docker exec ... psql -U postgres`).
configure  applies `config.migrations.dir/*.sql` (from the app tree BOSS stages in the workdir)
           in name order, each in one transaction AS THE APP, recorded with its checksum in
           `boss_schema_migrations`.
verify     a real round trip AS THE APP (DATABASE_URL, over the app's network): who am I, which
           database, not a superuser, a write and a read in a transaction that is rolled back.
backup     pg_dump (custom format) into the backups volume on the host, then a TESTED RESTORE: a
           disposable server (no network) mounts that volume read-only, pg_restore runs into it,
           and every table's row count and content md5 is compared with the source. A backup
           whose restore does not match is a failed backup.
restore    checks the dump's sha256 against its manifest, pg_restore --clean into the resource,
           then the same comparison against the manifest.
destroy    refused while others depend on it; otherwise the container, both volumes (backups
           included) and the binding.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import (
    HookFailure, OfficialPlugin, Session, env_name, evidence, labels, network_name, new_password,
    project_name, service_name,
)

IMAGE = "postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
#: PostgreSQL 17 with the pgvector extension built in; chosen when the request asks for pgvector.
PGVECTOR_IMAGE = ("pgvector/pgvector:0.8.1-pg17-bookworm"
                  "@sha256:3e8b3adfd27b5707128f60956f62a793c3c9326ea8cfaf0eab7adccb5d700b21")

#: Every user table, its exact row count and an md5 of its sorted rows: `schema.table|n|md5`.
#: Both sides of every backup/restore comparison run THIS query.
FINGERPRINT_SQL = r"""
SELECT format('%I.%I', table_schema, table_name) || '|' ||
  (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text || '|' ||
  (xpath('/row/h/text()', query_to_xml(format('SELECT md5(coalesce(string_agg(t::text, %L ORDER BY t::text), %L)) AS h FROM %I.%I t', ',', '', table_schema, table_name), false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
"""
#: The superuser password is set at init from the kernel's random source and kept by nobody.
START = 'export POSTGRES_PASSWORD="$$(cat /proc/sys/kernel/random/uuid)"; exec docker-entrypoint.sh postgres'

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MIGRATION = re.compile(r"^[0-9A-Za-z_.-]{1,120}\.sql$")
_BACKUP = re.compile(r"^[a-z0-9-]{1,60}-\d{8}T\d{12}Z\.dump$")
PSQL = ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]


def _ident(value: str) -> str:
    v = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "app"
    return v if (v[0].isalpha() or v[0] == "_") else f"d_{v}"


def _fingerprint(text: str) -> dict[str, dict]:
    out = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            out[parts[0]] = {"rows": int(parts[1]), "md5": parts[2]}
    return out


def _password_of(url: str) -> str:
    m = re.match(r"^postgresql://[^:]+:([^@]+)@", url)
    if not m:
        raise HookFailure("state_lost", "DATABASE_URL in the vault is not a postgresql:// URL with a password")
    return m.group(1)


class PostgresPlugin(OfficialPlugin):
    directory = "postgres"

    # -- naming -------------------------------------------------------------------------------
    def image(self, ctx: HookContext) -> str:
        cfg = ctx.resource.config
        if cfg.get("image"):
            return cfg["image"]
        wants_vector = "pgvector" in ctx.resource.features or "vector" in (cfg.get("extensions") or [])
        return PGVECTOR_IMAGE if wants_vector else IMAGE

    def database(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("database") or _ident(ctx.resource.name)

    def role(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("user") or _ident(ctx.resource.name) + "_app"

    def data_volume(self, ctx: HookContext) -> str:
        return f"{service_name(ctx)}-data"

    def backups_volume(self, ctx: HookContext) -> str:
        return f"{service_name(ctx)}-backups"

    # -- validate / plan ----------------------------------------------------------------------
    def _validate(self, ctx: HookContext) -> list[str]:
        out = []
        for key in ("database", "user"):
            v = ctx.resource.config.get(key)
            if v and not _IDENT.fullmatch(v):
                out.append(f"config.{key} must be a lower-case SQL identifier")
        v = ctx.resource.engineVersion
        if v and str(v).split(".")[0] != "17":
            out.append("official.postgres certifies PostgreSQL 17 (engineVersion: \"17\")")
        return out

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        out = [
            PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                          detail=f"{self.image(ctx).split('@')[0]}, no published port"),
            PlannedChange(action="create", target=f"volume:{self.data_volume(ctx)}", detail="data, kept until destroy"),
            PlannedChange(action="create", target=f"volume:{self.backups_volume(ctx)}", detail="pg_dump archives"),
            PlannedChange(action="create", target=f"database:{self.database(ctx)}",
                          detail=f"owned by LOGIN role {self.role(ctx)}, not a superuser"),
            PlannedChange(action="create", target="binding:DATABASE_URL", detail="vault reference"),
        ]
        if mig := ctx.resource.config.get("migrations"):
            out.append(PlannedChange(action="update", target=f"schema:{self.database(ctx)}",
                                     detail=f"apply pending migrations from {mig.get('dir', 'migrations')}"))
        return out

    def _risks(self, ctx: HookContext) -> list[str]:
        if ctx.resource.persistence and not ctx.resource.backups:
            return ["persistent data without backups: set backups: true for pg_dump with a tested restore"]
        return []

    # -- compose ------------------------------------------------------------------------------
    def fragment(self, ctx: HookContext) -> dict:
        svc, lab = service_name(ctx), labels(ctx, self.plugin_id)
        data, backups = self.data_volume(ctx), self.backups_volume(ctx)
        return {
            "services": {svc: {
                "image": self.image(ctx),
                "restart": "unless-stopped",
                "entrypoint": ["sh", "-c", START],
                "environment": {"POSTGRES_USER": "postgres", "POSTGRES_DB": self.database(ctx)},
                "volumes": [f"{data}:/var/lib/postgresql/data", f"{backups}:/backups"],
                "healthcheck": {"test": ["CMD-SHELL", f"pg_isready -U postgres -d {self.database(ctx)}"],
                                "interval": "2s", "timeout": "3s", "retries": 60},
                "labels": {**lab, "boss.pg.database": self.database(ctx), "boss.pg.role": self.role(ctx)},
            }},
            "volumes": {data: {"labels": lab}, backups: {"labels": lab}},
        }

    def _require(self, ctx: HookContext, s: Session) -> str:
        if not self.ref(ctx, "DATABASE_URL"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        return s.require_container(service_name(ctx))

    # -- psql: as the app over the network, or as admin inside the container -------------------
    def psql_app(self, ctx: HookContext, s: Session, url: str, sql: str, *, single_tx: bool = False,
                 what: str = "psql") -> str:
        flags = " ".join(PSQL[1:]) + (" --single-transaction" if single_tx else "")
        out = s.probe(self.image(ctx), ["-c", f'exec psql {flags} -d "$BOSS_PG_URL" -f -'],
                      env={"BOSS_PG_URL": url}, stdin=sql, entrypoint="sh", what=what)
        return s.text(out).strip()

    def psql_admin(self, ctx: HookContext, s: Session, cid: str, sql: str, *, env: dict | None = None,
                   database: str = "", what: str = "psql") -> str:
        out = s.exec(cid, PSQL + ["-U", "postgres", "-d", database or self.database(ctx), "-f", "-"],
                     env=env, stdin=sql, what=what)
        return s.text(out).strip()

    def wait_ready(self, ctx: HookContext, s: Session, cid: str, seconds: float = 90) -> None:
        deadline, last = time.monotonic() + seconds, ""
        while time.monotonic() < deadline:
            out = s.exec(cid, PSQL + ["-U", "postgres", "-d", self.database(ctx), "-c", "SELECT 1"], check=False)
            if out.returncode == 0:
                return
            last = s.scrub(out.stderr)[-200:]
            time.sleep(1.5)
        raise HookFailure("not_ready", f"postgres did not accept connections: {last}", retryable=True)

    # -- provision ----------------------------------------------------------------------------
    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        svc, db, role = service_name(ctx), self.database(ctx), self.role(ctx)
        refs = self.store_bindings(ctx, s, {
            "DATABASE_URL": lambda: f"postgresql://{role}:{new_password()}@{svc}:5432/{db}"})
        app_pw = _password_of(s.get(refs[env_name(ctx, "DATABASE_URL")]))
        before = s.container_id(svc)
        fragment = self.fragment(ctx)
        s.compose_up(fragment, svc)
        cid = s.require_container(svc)
        self.wait_ready(ctx, s, cid)
        # Idempotent: the role is created once; its password is (re)set to the vault's value.
        self.psql_admin(ctx, s, cid, f"""
\\getenv app_pw BOSS_APP_PW
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
  END IF;
END $$;
ALTER ROLE {role} WITH PASSWORD :'app_pw';
ALTER DATABASE {db} OWNER TO {role};
ALTER SCHEMA public OWNER TO {role};
REVOKE CONNECT ON DATABASE {db} FROM PUBLIC;
GRANT CONNECT ON DATABASE {db} TO {role};
""", env={"BOSS_APP_PW": app_pw}, what="creating the app role")
        state = {**ctx.state, "bindingRefs": refs, "service": svc, "project": project_name(ctx),
                 "network": network_name(ctx), "image": self.image(ctx), "database": db, "role": role,
                 "backupsVolume": f"{project_name(ctx)}_{self.backups_volume(ctx)}", "fragment": fragment}
        s.log(f"{svc} {'already running' if before and before == cid else 'started'}; {role} owns {db}")
        return ctx.result(bindings=self.as_bindings(refs), state=state,
                          evidence=[evidence("container", True, "healthcheck", f"{svc} healthy"),
                                    evidence("app_role", True, "config", f"{role} owns {db}; not a superuser")])

    # -- configure: migrations ----------------------------------------------------------------
    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx, s)
        ev = [evidence("binding", True, "config", "DATABASE_URL is a vault reference")]
        state = dict(ctx.state)
        if mig := ctx.resource.config.get("migrations"):
            applied, total = self.migrate(ctx, s, mig.get("dir", "migrations"))
            state["migrations"] = total
            ev.append(evidence("migrations", True, "migration",
                               f"{len(applied)} applied ({', '.join(applied) or 'none pending'}); {len(total)} total"))
        return ctx.result(bindings=self.as_bindings(ctx.state["bindingRefs"]), state=state, evidence=ev)

    def migrate(self, ctx: HookContext, s: Session, directory: str) -> tuple[list[str], list[str]]:
        root = Path(ctx.workdir).resolve()
        base = (root / directory).resolve()
        if not str(base).startswith(str(root)):
            raise HookFailure("invalid_request", "config.migrations.dir must stay inside the app tree")
        if not base.is_dir():
            raise HookFailure("migrations_missing", f"migrations directory {directory!r} is not in the app tree "
                              "BOSS staged for this operation")
        files = sorted(p for p in base.iterdir() if p.is_file() and p.suffix == ".sql")
        bad = [p.name for p in files if not _MIGRATION.fullmatch(p.name)]
        if bad:
            raise HookFailure("invalid_request", f"migration file names must be [0-9A-Za-z_.-]+.sql: {', '.join(bad)}")
        url = s.get(self.ref(ctx, "DATABASE_URL"))
        done = self.psql_app(ctx, s, url, """
CREATE TABLE IF NOT EXISTS boss_schema_migrations (
  version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());
SELECT version || '|' || checksum FROM boss_schema_migrations ORDER BY version;
""", what="reading applied migrations")
        have = dict(line.split("|", 1) for line in done.splitlines() if "|" in line)
        applied = []
        for p in files:
            body = p.read_text(encoding="utf-8")
            digest = hashlib.sha256(body.encode()).hexdigest()
            if p.name in have:
                if have[p.name] != digest:
                    raise HookFailure("migration_changed", f"{p.name} was applied with a different checksum; "
                                      "add a new migration instead of editing an applied one")
                continue
            self.psql_app(ctx, s, url, body + f"\nINSERT INTO boss_schema_migrations (version, checksum) "
                          f"VALUES ('{p.name}', '{digest}');\n", single_tx=True, what=f"migration {p.name}")
            applied.append(p.name)
            s.log(f"applied migration {p.name}")
        return applied, [p.name for p in files]

    # -- verify / observe ---------------------------------------------------------------------
    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        cid = self._require(ctx, s)
        url = s.get(self.ref(ctx, "DATABASE_URL"))
        nonce = new_password(6)
        out = self.psql_app(ctx, s, url, f"""
SELECT current_user || '|' || current_database() || '|' || current_setting('server_version') || '|' ||
       (SELECT CASE WHEN rolsuper THEN 'super' ELSE 'plain' END FROM pg_roles WHERE rolname = current_user);
BEGIN;
CREATE TEMP TABLE boss_verify (id int, note text) ON COMMIT DROP;
INSERT INTO boss_verify VALUES (1, '{nonce}');
SELECT note FROM boss_verify WHERE id = 1;
ROLLBACK;
""", what="verifying postgres").splitlines()
        who = out[0].split("|") if out else []
        published = s.published_ports(cid)
        ev = [evidence("connect_as_app", len(who) == 4, "connection",
                       f"user {who[0]} on {who[1]}, PostgreSQL {who[2]}" if len(who) == 4 else "no answer"),
              evidence("write_read_row", len(out) >= 2 and out[1] == nonce, "connection",
                       "wrote and read a row; rolled back"),
              evidence("app_not_superuser", len(who) == 4 and who[3] == "plain", "config", "the app role is not a superuser"),
              evidence("no_public_port", not published, "config",
                       "no host port published" if not published else f"published: {', '.join(published)}")]
        if all(e["passed"] for e in ev):
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": "postgres did not pass verify"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx, s)
        out = self.psql_app(ctx, s, s.get(self.ref(ctx, "DATABASE_URL")), """
SELECT numbackends || '|' || pg_database_size(datname) || '|' || xact_commit || '|' || xact_rollback
FROM pg_stat_database WHERE datname = current_database();
""", what="observing postgres")
        n = (out.split("|") + ["0"] * 4)[:4]
        return ctx.result(observed={"healthy": True, "connections": int(n[0]), "sizeBytes": int(n[1]),
                                    "commits": int(n[2]), "rollbacks": int(n[3])},
                          evidence=[evidence("stats", True, "observe", "pg_stat_database read")])

    # -- backup with a tested restore ---------------------------------------------------------
    def fingerprint(self, ctx: HookContext, s: Session, cid: str) -> dict:
        return _fingerprint(self.psql_admin(ctx, s, cid, FINGERPRINT_SQL, what="fingerprinting tables"))

    def do_backup(self, ctx: HookContext, s: Session) -> OperationResult:
        cid = self._require(ctx, s)
        db = self.database(ctx)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = f"{re.sub('[^a-z0-9-]', '-', ctx.resource.name)}-{stamp}.dump"
        before = self.fingerprint(ctx, s, cid)
        out = s.exec(cid, ["sh", "-c", 'pg_dump -U postgres -Fc -d "$1" -f "/backups/$2" && '
                           'sha256sum "/backups/$2" && wc -c < "/backups/$2"', "sh", db, name],
                     timeout=900, what="pg_dump").stdout.split()
        after = self.fingerprint(ctx, s, cid)
        sha, size = out[0], int(out[-1])
        restored = self.test_restore(ctx, s, name)
        quiet = before == after
        if quiet:
            match = restored == before
            detail = (f"restored {len(restored)} tables, {sum(t['rows'] for t in restored.values())} rows in a "
                      "disposable server; every row count and content checksum matches the source" if match else
                      f"the restored copy differs from the source: {self._diff(before, restored)}")
        else:  # writes landed during the dump: the dump is a consistent snapshot, the comparison is not
            match = set(restored) == set(before)
            detail = (f"the source changed during the dump; restored {len(restored)} tables "
                      "(table set matches; row checksums not comparable)")
        manifest = {"file": name, "sha256": sha, "bytes": size, "image": self.image(ctx), "database": db,
                    "created": stamp, "tables": before if quiet else restored}
        s.exec(cid, ["sh", "-c", 'cat > "/backups/$1"', "sh", name[:-5] + ".json"],
               stdin=json.dumps(manifest, sort_keys=True), what="writing the backup manifest")
        artifact = f"volume:{ctx.state.get('backupsVolume')}:{name}"
        ev = [evidence("pg_dump", True, "backup", f"{size} bytes, sha256 {sha[:16]}"),
              evidence("tested_restore", match, "restore", detail)]
        state = {**ctx.state, "lastBackup": name}
        if not match:
            return ctx.result(OperationStatus.FAILED, evidence=ev, artifacts=[artifact], state=state,
                              errors=[{"code": "restore_test_failed", "message": detail[:1000]}])
        s.log(f"backup {name}: {detail}")
        return ctx.result(evidence=ev, artifacts=[artifact, artifact[:-5] + ".json"], state=state)

    @staticmethod
    def _diff(a: dict, b: dict) -> str:
        return "; ".join(f"{t}: source {a.get(t, {}).get('rows', 'absent')} rows, restored "
                         f"{b.get(t, {}).get('rows', 'absent')}" for t in sorted(set(a) | set(b)) if a.get(t) != b.get(t))[:400]

    def test_restore(self, ctx: HookContext, s: Session, name: str) -> dict:
        """Restore into a DISPOSABLE server (no network, backups mounted read-only), fingerprint it,
        remove it."""
        cmd = ["docker", "run", "-d", "--network", "none", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
               "-v", f"{ctx.state['backupsVolume']}:/backups:ro"]
        for k, v in {**labels(ctx, self.plugin_id), "boss.disposable": "restore-test"}.items():
            cmd += ["--label", f"{k}={v}"]
        tmp = s.run(cmd + [self.image(ctx)], what="starting a disposable postgres").stdout.strip()
        try:
            deadline = time.monotonic() + 90
            while True:
                # pg_isready also answers initdb's temporary server; a TCP query needs the final one
                ok = s.run(["docker", "exec", tmp, "psql", "-U", "postgres", "-h", "127.0.0.1", "-XAtc", "SELECT 1"],
                           check=False).returncode == 0
                if ok:
                    break
                if time.monotonic() > deadline:
                    raise HookFailure("restore_test_failed", "the disposable postgres never became ready", retryable=True)
                time.sleep(1)
            s.exec(tmp, ["pg_restore", "-U", "postgres", "-h", "127.0.0.1", "--create", "--no-owner", "--no-privileges",
                         "--exit-on-error", "-d", "postgres", f"/backups/{name}"], timeout=900,
                   what="pg_restore into the disposable server")
            out = s.exec(tmp, PSQL + ["-U", "postgres", "-h", "127.0.0.1", "-d", self.database(ctx), "-f", "-"],
                         stdin=FINGERPRINT_SQL, what="fingerprinting the restored copy")
            return _fingerprint(s.text(out))
        finally:
            s.run(["docker", "rm", "-f", "-v", tmp], check=False)

    # -- restore into the resource ------------------------------------------------------------
    def do_restore(self, ctx: HookContext, s: Session) -> OperationResult:
        cid = self._require(ctx, s)
        name = ctx.resource.config.get("restoreFrom") or ctx.state.get("lastBackup")
        if not name or not _BACKUP.fullmatch(name):
            raise HookFailure("invalid_request", "nothing to restore: name a backup taken by this plugin "
                              "(config.restoreFrom) or take one first")
        manifest = json.loads(s.exec(cid, ["cat", f"/backups/{name[:-5]}.json"], what="reading the backup manifest").stdout)
        sha = s.exec(cid, ["sha256sum", f"/backups/{name}"], what="checking the backup").stdout.split()[0]
        if sha != manifest.get("sha256"):
            raise HookFailure("backup_corrupt", f"{name} does not match the checksum recorded when it was taken")
        s.exec(cid, ["pg_restore", "-U", "postgres", "--clean", "--if-exists", "--exit-on-error",
                     "-d", self.database(ctx), f"/backups/{name}"], timeout=900, what="pg_restore")
        now = self.fingerprint(ctx, s, cid)
        want = manifest.get("tables")
        match = now == want
        detail = (f"{len(now)} tables, {sum(t['rows'] for t in now.values())} rows match the backup" if match
                  else f"restored data differs from the backup: {self._diff(want or {}, now)}")
        ev = [evidence("pg_restore", True, "restore", f"restored {name}"),
              evidence("rows_match_backup", match, "restore", detail)]
        if not match:
            return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "restore_mismatch", "message": detail}])
        return ctx.result(evidence=ev, state=ctx.state)

    # -- destroy ------------------------------------------------------------------------------
    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx), [self.data_volume(ctx), self.backups_volume(ctx)])
        removed += self.delete_bindings(ctx)
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed) or "nothing left")],
                          state={})
