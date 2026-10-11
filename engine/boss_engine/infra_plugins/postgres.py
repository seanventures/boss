"""official.postgres: PostgreSQL as a compose service on the app's private network.

provision  a `res-<name>` service, a named data volume, NO published port; a LOGIN role that owns
           one database (not a superuser). The superuser is kept for the plugin's own work.
configure  DATABASE_URL as a vault reference; applies `config.migrations.dir/*.sql` in name order,
           each in one transaction, recorded with its checksum in `boss_schema_migrations`.
verify     a real round trip AS THE APP (with DATABASE_URL, over the app's network): who am I,
           which database, a write and a read in a transaction that is rolled back.
backup     pg_dump (custom format) to `backups/`, then a TESTED RESTORE: a disposable server
           from the same image, pg_restore into it, and every table's row count and content
           checksum compared with the source. A backup that does not restore is a failed backup.
restore    pg_restore --clean into the resource, then the same comparison against the backup's
           manifest.
destroy    refused for an attached resource, one others depend on, or without an approved
           delete plan; otherwise the container, its volume and its vault secrets.
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
    HookFailure, OfficialPlugin, Session, env_prefix, evidence, labels, network_name, new_password,
    project_name, service_name,
)

IMAGE = "postgres:17.6-alpine@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"
#: PostgreSQL 17 with the pgvector extension built in; chosen when the request asks for pgvector.
PGVECTOR_IMAGE = ("pgvector/pgvector:0.8.1-pg17-bookworm"
                  "@sha256:3e8b3adfd27b5707128f60956f62a793c3c9326ea8cfaf0eab7adccb5d700b21")

#: Every user table, its exact row count and an md5 of its sorted rows, one `schema.table|n|md5`
#: line each. Both sides of every backup/restore comparison run THIS query.
FINGERPRINT_SQL = r"""
SELECT format('%I.%I', table_schema, table_name) || '|' ||
  (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text || '|' ||
  (xpath('/row/h/text()', query_to_xml(format('SELECT md5(coalesce(string_agg(t::text, %L ORDER BY t::text), %L)) AS h FROM %I.%I t', ',', '', table_schema, table_name), false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
"""

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_MIGRATION = re.compile(r"^[0-9A-Za-z_.-]{1,120}\.sql$")


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


class PostgresPlugin(OfficialPlugin):
    directory = "postgres"

    # -- naming -------------------------------------------------------------------------------
    def image(self, ctx: HookContext) -> str:
        if ctx.state.get("image"):
            return ctx.state["image"]
        cfg = ctx.resource.config
        if cfg.get("image"):
            return cfg["image"]
        wants_vector = "pgvector" in ctx.resource.features or "vector" in (cfg.get("extensions") or [])
        return PGVECTOR_IMAGE if wants_vector else IMAGE

    def database(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("database") or _ident(ctx.resource.name)

    def role(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("user") or _ident(ctx.resource.name) + "_app"

    def volume(self, ctx: HookContext) -> str:
        return f"{service_name(ctx)}-data"

    # -- validate / plan ----------------------------------------------------------------------
    def _validate(self, ctx: HookContext) -> list[str]:
        out = []
        for key in ("database", "user"):
            v = ctx.resource.config.get(key)
            if v and not _IDENT.fullmatch(v):
                out.append(f"config.{key} must be a lower-case SQL identifier")
        if v := ctx.resource.engineVersion:
            if not str(v).split(".")[0] == "17":
                out.append("official.postgres certifies PostgreSQL 17 (engineVersion: \"17\")")
        return out

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        out = [
            PlannedChange(action="create", target=f"service:{service_name(ctx)}",
                          detail=f"{self.image(ctx).split('@')[0]}, no published port"),
            PlannedChange(action="create", target=f"volume:{self.volume(ctx)}", detail="data, kept until destroy"),
            PlannedChange(action="create", target=f"database:{self.database(ctx)}",
                          detail=f"owned by LOGIN role {self.role(ctx)}, not a superuser"),
            PlannedChange(action="create", target="secret:DATABASE_URL", detail="vault reference"),
        ]
        if mig := ctx.resource.config.get("migrations"):
            out.append(PlannedChange(action="update", target=f"schema:{self.database(ctx)}",
                                     detail=f"apply pending migrations from {mig.get('dir', 'migrations')}"))
        return out

    def _risks(self, ctx: HookContext) -> list[str]:
        if ctx.resource.persistence and not ctx.resource.backups:
            return ["persistent data without backups: set backups: true for pg_dump with a tested restore"]
        return []

    # -- secrets and state --------------------------------------------------------------------
    def _secrets(self, ctx: HookContext, s: Session) -> dict:
        """Mint once, reuse forever: refs live in state, values in the vault."""
        st = ctx.state or {}
        if st.get("adminPasswordRef"):
            return {"admin": s.get(st["adminPasswordRef"]), "app": s.get(st["appPasswordRef"]),
                    "refs": {k: st[k] for k in ("adminPasswordRef", "appPasswordRef", "adminUrlRef", "urlRef")}}
        if s.container_id(service_name(ctx)):
            raise HookFailure("state_lost", f"{service_name(ctx)} exists but its secret references are not in "
                              "the state BOSS handed back; refusing to mint a password its data does not know")
        admin, app = new_password(), new_password()
        db, role, host = self.database(ctx), self.role(ctx), service_name(ctx)
        refs = {
            "adminPasswordRef": s.put("ADMIN_PASSWORD", admin).secretRef,
            "appPasswordRef": s.put("APP_PASSWORD", app).secretRef,
            "adminUrlRef": s.put("ADMIN_URL", f"postgresql://postgres:{admin}@{host}:5432/{db}").secretRef,
            "urlRef": s.put("DATABASE_URL", f"postgresql://{role}:{app}@{host}:5432/{db}").secretRef,
        }
        return {"admin": admin, "app": app, "refs": refs}

    def _state(self, ctx: HookContext, refs: dict) -> dict:
        return {**ctx.state, **refs, "project": project_name(ctx), "network": network_name(ctx),
                "service": service_name(ctx), "volume": self.volume(ctx), "image": self.image(ctx),
                "database": self.database(ctx), "role": self.role(ctx),
                "fragment": f"compose/{ctx.resource.name}.yaml",
                "env": {f"{env_prefix(ctx)}_ADMIN_PASSWORD": refs["adminPasswordRef"]}}

    def _require(self, ctx: HookContext) -> None:
        if not ctx.state.get("urlRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned (no state)")

    # -- psql, over the app's network, the URL in the environment ------------------------------
    def psql(self, ctx: HookContext, s: Session, url: str, sql: str, *, env: dict | None = None,
             single_tx: bool = False, what: str = "psql", timeout: float = 90) -> str:
        flags = "-X -q -At -v ON_ERROR_STOP=1" + (" --single-transaction" if single_tx else "")
        proc = s.probe(self.image(ctx), ["-c", f'exec psql {flags} -d "$BOSS_PG_URL" -f -'],
                       env={"BOSS_PG_URL": url, **(env or {})}, stdin=sql.encode(), entrypoint="sh",
                       timeout=timeout, what=what)
        return s.text(proc).strip()

    def wait_ready(self, ctx: HookContext, s: Session, url: str, seconds: float = 60) -> None:
        deadline, last = time.monotonic() + seconds, ""
        while time.monotonic() < deadline:
            try:
                self.psql(ctx, s, url, "SELECT 1;", what="waiting for postgres", timeout=20)
                return
            except HookFailure as exc:
                last = exc.message
                time.sleep(1.5)
        raise HookFailure("not_ready", f"postgres did not accept connections: {last}", retryable=True)

    # -- provision ----------------------------------------------------------------------------
    def fragment(self, ctx: HookContext) -> dict:
        svc, vol, lab = service_name(ctx), self.volume(ctx), labels(ctx, self.plugin_id)
        return {
            "services": {svc: {
                "image": self.image(ctx),
                "restart": "unless-stopped",
                "environment": {"POSTGRES_USER": "postgres", "POSTGRES_DB": self.database(ctx),
                                "POSTGRES_PASSWORD": f"${{{env_prefix(ctx)}_ADMIN_PASSWORD}}"},
                "volumes": [f"{vol}:/var/lib/postgresql/data"],
                "healthcheck": {"test": ["CMD-SHELL", f"pg_isready -U postgres -d {self.database(ctx)}"],
                                "interval": "2s", "timeout": "3s", "retries": 60},
                "labels": lab,
            }},
            "volumes": {vol: {"labels": lab}},
        }

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        sec = self._secrets(ctx, s)
        svc = service_name(ctx)
        before = s.container_id(svc)
        s.write_fragment(self.fragment(ctx))
        s.compose_up(svc, {f"{env_prefix(ctx)}_ADMIN_PASSWORD": sec["admin"]})
        state = self._state(ctx, sec["refs"])
        ctx.state = state
        admin_url = s.get(state["adminUrlRef"])
        self.wait_ready(ctx, s, admin_url)
        role, db = self.role(ctx), self.database(ctx)
        # Idempotent: the role is created once; its password is (re)set to the vault's value.
        self.psql(ctx, s, admin_url, f"""
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
""", env={"BOSS_APP_PW": sec["app"]}, what="creating the app role")
        after = s.container_id(svc)
        s.log(f"{svc} {'already running' if before and before == after else 'started'}; {role} owns {db}")
        return ctx.result(bindings=self.bindings(ctx, {"DATABASE_URL": state["urlRef"]}), state=state,
                          artifacts=[state["fragment"]],
                          evidence=[evidence("container", True, "healthcheck", f"{svc} healthy"),
                                    evidence("app_role", True, "config", f"{role} owns {db}; not a superuser")])

    # -- configure: bindings + migrations -----------------------------------------------------
    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        ev = [evidence("binding", True, "config", "DATABASE_URL is a vault reference")]
        state = dict(ctx.state)
        if mig := ctx.resource.config.get("migrations"):
            applied, total = self.migrate(ctx, s, mig.get("dir", "migrations"))
            state["migrations"] = total
            ev.append(evidence("migrations", True, "migration",
                               f"{len(applied)} applied ({', '.join(applied) or 'none pending'}); {len(total)} total"))
        return ctx.result(bindings=self.bindings(ctx, {"DATABASE_URL": state["urlRef"]}), state=state, evidence=ev)

    def migrate(self, ctx: HookContext, s: Session, directory: str) -> tuple[list[str], list[str]]:
        base = (Path(ctx.workdir) / directory).resolve()
        if not str(base).startswith(str(Path(ctx.workdir).resolve())):
            raise HookFailure("invalid_request", "config.migrations.dir must stay inside the app's workdir")
        if not base.is_dir():
            raise HookFailure("invalid_request", f"migrations directory {directory!r} does not exist")
        files = sorted(p for p in base.iterdir() if p.is_file() and p.suffix == ".sql")
        bad = [p.name for p in files if not _MIGRATION.fullmatch(p.name)]
        if bad:
            raise HookFailure("invalid_request", f"migration file names must be [0-9A-Za-z_.-]+.sql: {', '.join(bad)}")
        url = s.get(ctx.state["urlRef"])
        done = self.psql(ctx, s, url, """
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
            self.psql(ctx, s, url, body + f"\nINSERT INTO boss_schema_migrations (version, checksum) "
                      f"VALUES ('{p.name}', '{digest}');\n", single_tx=True, what=f"migration {p.name}")
            applied.append(p.name)
            s.log(f"applied migration {p.name}")
        return applied, [p.name for p in files]

    # -- verify / observe ---------------------------------------------------------------------
    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        url = s.get(ctx.state["urlRef"])
        nonce = new_password(6)
        out = self.psql(ctx, s, url, f"""
SELECT current_user || '|' || current_database() || '|' || current_setting('server_version') || '|' ||
       (SELECT CASE WHEN rolsuper THEN 'super' ELSE 'plain' END FROM pg_roles WHERE rolname = current_user);
BEGIN;
CREATE TEMP TABLE boss_verify (id int, note text) ON COMMIT DROP;
INSERT INTO boss_verify VALUES (1, '{nonce}');
SELECT note FROM boss_verify WHERE id = 1;
ROLLBACK;
""", what="verifying postgres")
        lines = out.splitlines()
        who = lines[0].split("|") if lines else []
        ok_rw = len(lines) >= 2 and lines[1] == nonce
        ev = [evidence("connect_as_app", len(who) == 4, "connection",
                       f"user {who[0]} on {who[1]}, PostgreSQL {who[2]}" if len(who) == 4 else "no answer"),
              evidence("write_read_row", ok_rw, "connection", "wrote and read a row; rolled back"),
              evidence("app_not_superuser", len(who) == 4 and who[3] == "plain", "config", "the app role is not a superuser"),
              self.no_public_port(ctx, s)]
        return ctx.result(evidence=ev, state=ctx.state) if all(e["passed"] for e in ev) else \
            ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "verify_failed",
                                                                     "message": "postgres did not pass verify"}])

    def no_public_port(self, ctx: HookContext, s: Session) -> dict:
        cid = s.container_id(service_name(ctx))
        if not cid:
            return evidence("no_public_port", False, "config", "container not found")
        proc = s.run(["docker", "inspect", "-f", "{{json .NetworkSettings.Ports}}", cid], what="inspecting ports")
        ports = json.loads(proc.stdout.decode() or "{}") or {}
        published = [k for k, v in ports.items() if v]
        return evidence("no_public_port", not published, "config",
                        "no host port published" if not published else f"published: {', '.join(published)}")

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        url = s.get(ctx.state["urlRef"])
        out = self.psql(ctx, s, url, """
SELECT numbackends || '|' || pg_database_size(datname) || '|' || xact_commit || '|' || xact_rollback
FROM pg_stat_database WHERE datname = current_database();
""", what="observing postgres")
        n = (out.split("|") + ["0"] * 4)[:4]
        observed = {"healthy": True, "connections": int(n[0]), "sizeBytes": int(n[1]),
                    "commits": int(n[2]), "rollbacks": int(n[3])}
        return ctx.result(observed=observed, evidence=[evidence("stats", True, "observe", "pg_stat_database read")])

    # -- backup with a tested restore ---------------------------------------------------------
    def fingerprint(self, ctx: HookContext, s: Session, url: str) -> dict:
        return _fingerprint(self.psql(ctx, s, url, FINGERPRINT_SQL, what="fingerprinting tables"))

    def do_backup(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        admin_url = s.get(ctx.state["adminUrlRef"])
        before = self.fingerprint(ctx, s, admin_url)
        proc = s.probe(self.image(ctx), ["-c", 'exec pg_dump --format=custom --no-password -d "$BOSS_PG_URL"'],
                       env={"BOSS_PG_URL": admin_url}, entrypoint="sh", timeout=600, what="pg_dump")
        after = self.fingerprint(ctx, s, admin_url)
        dump = proc.stdout
        if not dump.startswith(b"PGDMP"):
            raise HookFailure("backup_failed", "pg_dump did not produce a custom-format archive")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        rel = f"backups/{ctx.resource.name}-{stamp}.dump"
        path = Path(ctx.workdir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(dump)
        sha = hashlib.sha256(dump).hexdigest()
        quiet = before == after
        restored = self.test_restore(ctx, s, path)
        if quiet:
            match = restored == before
            detail = (f"restored {len(restored)} tables, {sum(t['rows'] for t in restored.values())} rows; "
                      "every row count and content checksum matches the source" if match else
                      f"restored copy differs from the source: {self._diff(before, restored)}")
        else:  # writes landed during the dump: the snapshot is consistent, the comparison is not
            match = set(restored) == set(before)
            detail = (f"source changed during the dump; restored {len(restored)} tables "
                      "(table set matches; row checksums not comparable)")
        manifest = {"file": rel, "sha256": sha, "bytes": len(dump), "image": self.image(ctx),
                    "database": self.database(ctx), "created": stamp, "tables": restored if match else before}
        (path.with_suffix(".json")).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        ev = [evidence("pg_dump", True, "backup", f"{len(dump)} bytes, sha256 {sha[:16]}"),
              evidence("tested_restore", match, "restore", detail)]
        state = {**ctx.state, "lastBackup": rel}
        if not match:
            return ctx.result(OperationStatus.FAILED, evidence=ev, artifacts=[rel, rel[:-5] + ".json"], state=state,
                              errors=[{"code": "restore_test_failed", "message": detail[:1000]}])
        s.log(f"backup {rel}: {detail}")
        return ctx.result(evidence=ev, artifacts=[rel, rel[:-5] + ".json"], state=state)

    @staticmethod
    def _diff(a: dict, b: dict) -> str:
        out = []
        for t in sorted(set(a) | set(b)):
            if a.get(t) != b.get(t):
                out.append(f"{t}: source {a.get(t, {}).get('rows', 'absent')} rows, "
                           f"restored {b.get(t, {}).get('rows', 'absent')}")
        return "; ".join(out)[:400]

    def test_restore(self, ctx: HookContext, s: Session, dump: Path) -> dict:
        """Restore into a DISPOSABLE server (no network, removed after) and fingerprint it."""
        lab = {**labels(ctx, self.plugin_id), "boss.disposable": "restore-test"}
        cmd = ["docker", "run", "-d", "--network", "none", "-e", "POSTGRES_HOST_AUTH_METHOD=trust"]
        for k, v in lab.items():
            cmd += ["--label", f"{k}={v}"]
        cid = s.run(cmd + [self.image(ctx)], what="starting a disposable postgres").stdout.decode().strip()
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                # pg_isready answers during initdb's temporary server; ask for the final one.
                ready = s.run(["docker", "exec", cid, "sh", "-c",
                               "pg_isready -U postgres -h 127.0.0.1 && psql -U postgres -h 127.0.0.1 -XAtc 'select 1'"],
                              check=False)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise HookFailure("restore_test_failed", "the disposable postgres never became ready", retryable=True)
            s.run(["docker", "exec", "-i", cid, "pg_restore", "-U", "postgres", "-h", "127.0.0.1", "--create",
                   "--no-owner", "--no-privileges", "--exit-on-error", "-d", "postgres"],
                  stdin=dump.read_bytes(), timeout=600, what="pg_restore into the disposable server")
            out = s.run(["docker", "exec", "-i", cid, "psql", "-U", "postgres", "-h", "127.0.0.1", "-X", "-q", "-At",
                         "-v", "ON_ERROR_STOP=1", "-d", self.database(ctx), "-f", "-"],
                        stdin=FINGERPRINT_SQL.encode(), what="fingerprinting the restored copy")
            return _fingerprint(s.text(out))
        finally:
            s.run(["docker", "rm", "-f", "-v", cid], check=False)

    # -- restore into the resource ------------------------------------------------------------
    def do_restore(self, ctx: HookContext, s: Session) -> OperationResult:
        self._require(ctx)
        rel = ctx.resource.config.get("restoreFrom") or ctx.state.get("lastBackup")
        if not rel:
            raise HookFailure("invalid_request", "nothing to restore: no config.restoreFrom and no backup in state")
        path = (Path(ctx.workdir) / rel).resolve()
        if not str(path).startswith(str(Path(ctx.workdir).resolve())) or not path.is_file():
            raise HookFailure("invalid_request", f"backup {rel!r} is not a file in the app's workdir")
        manifest_path = path.with_suffix(".json")
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        data = path.read_bytes()
        if manifest.get("sha256") and hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            raise HookFailure("backup_corrupt", f"{rel} does not match the checksum recorded when it was taken")
        admin_url = s.get(ctx.state["adminUrlRef"])
        s.probe(self.image(ctx), ["-c", 'exec pg_restore --clean --if-exists --exit-on-error -d "$BOSS_PG_URL"'],
                env={"BOSS_PG_URL": admin_url}, stdin=data, entrypoint="sh", timeout=600, what="pg_restore")
        now = self.fingerprint(ctx, s, admin_url)
        want = manifest.get("tables")
        match = want is None or now == want
        detail = (f"{len(now)} tables, {sum(t['rows'] for t in now.values())} rows match the backup" if match
                  else f"restored data differs from the backup: {self._diff(want, now)}")
        ev = [evidence("pg_restore", True, "restore", f"restored {rel}"),
              evidence("rows_match_backup", match, "restore", detail)]
        if not match:
            return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "restore_mismatch", "message": detail}])
        return ctx.result(evidence=ev, state=ctx.state)

    # -- destroy ------------------------------------------------------------------------------
    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        removed = s.compose_destroy(service_name(ctx), [self.volume(ctx)])
        for key in ("urlRef", "adminUrlRef", "appPasswordRef", "adminPasswordRef"):
            if ref := ctx.state.get(key):
                ctx.secrets.delete(ref)
                removed.append(f"secret:{key}")
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed) or "nothing left")],
                          state={})


Plugin = PostgresPlugin
