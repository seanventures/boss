"""official.pgvector: the `vector` extension on a database official.postgres provisioned.

Not a vector database: PostgreSQL with one extension, sharing the database's backups, role and
network. `config.database` names that resource (also listed in `dependsOn`); the plugin finds its
container on the host by label, so it needs no other resource's secrets or state.

provision  CREATE EXTENSION IF NOT EXISTS vector, as the superuser over the container's local
           socket (pgvector is not a trusted extension).
verify     AS THE DATABASE'S APP ROLE: an HNSW index and nearest-neighbour queries by L2 and
           cosine distance on a temporary table, in a transaction that is rolled back.
destroy    DROP EXTENSION vector RESTRICT: refused while any column still uses the type.
"""
from __future__ import annotations

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import HookFailure, OfficialPlugin, Session, evidence, resource_id_of

VERIFY_SQL = """
BEGIN;
CREATE TEMP TABLE boss_vec (id text, v vector(3)) ON COMMIT DROP;
INSERT INTO boss_vec VALUES ('a', '[1,0,0]'), ('b', '[0,1,0]'), ('c', '[0,0,1]'), ('d', '[0.9,0.1,0]');
CREATE INDEX ON boss_vec USING hnsw (v vector_l2_ops);
SET LOCAL enable_seqscan = off;
SELECT string_agg(id, ',' ORDER BY d) FROM (SELECT id, v <-> '[1,0.05,0]' AS d FROM boss_vec) x;
SELECT id FROM boss_vec ORDER BY v <=> '[0,0.2,1]' LIMIT 1;
SELECT extversion FROM pg_extension WHERE extname = 'vector';
ROLLBACK;
"""
PSQL = ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]


class PgvectorPlugin(OfficialPlugin):
    directory = "pgvector"

    def _validate(self, ctx: HookContext) -> list[str]:
        db = ctx.resource.config.get("database")
        if db and db not in ctx.resource.dependsOn:
            return [f"config.database names {db!r}; list it in dependsOn too, so it is provisioned first"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"extension:vector@{ctx.resource.config['database']}",
                              detail="CREATE EXTENSION IF NOT EXISTS vector")]

    def database(self, ctx: HookContext, s: Session) -> tuple[str, str, str]:
        """(container id, database, app role) of the postgres resource named in config.database."""
        name = ctx.resource.config["database"]
        cid = s.container_id("", rid=resource_id_of(ctx, name))
        if not cid:
            raise HookFailure("not_provisioned", f"database {name!r} has not been provisioned; pgvector needs it first")
        fmt = '{{.Config.Image}}|{{index .Config.Labels "boss.pg.database"}}|{{index .Config.Labels "boss.pg.role"}}'
        image, db, role = s.run(["docker", "inspect", "-f", fmt, cid], what="reading the database").stdout.strip().split("|")
        if "pgvector" not in image:
            raise HookFailure("invalid_request", f"database {name!r} runs an image without pgvector; add "
                              "`features: [pgvector]` to it and provision it again")
        return cid, db, role

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        cid, db, _role = self.database(ctx, s)
        out = s.exec(cid, PSQL + ["-U", "postgres", "-d", db, "-f", "-"], what="creating the extension",
                     stdin="CREATE EXTENSION IF NOT EXISTS vector;\nSELECT extversion FROM pg_extension WHERE extname = 'vector';")
        version = s.text(out).strip()
        return ctx.result(state={**ctx.state, "extension": "vector", "version": version, "database": db},
                          evidence=[evidence("extension", True, "config", f"vector {version} in {db}")])

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        cid, db, role = self.database(ctx, s)
        out = s.text(s.exec(cid, PSQL + ["-U", role, "-d", db, "-f", "-"], stdin=VERIFY_SQL,
                            what="verifying pgvector")).strip().splitlines()
        l2_ok = len(out) >= 1 and out[0] == "a,d,b,c"
        cos_ok = len(out) >= 2 and out[1] == "c"
        ev = [evidence("l2_nearest_neighbours", l2_ok, "connection",
                       f"as {role}: <-> through an HNSW index gave {out[0] if out else 'nothing'} (want a,d,b,c)"),
              evidence("cosine_nearest", cos_ok, "connection", f"<=> nearest {out[1] if len(out) > 1 else '-'} (want c)"),
              evidence("extension_version", len(out) >= 3, "config", f"vector {out[2] if len(out) > 2 else '?'}")]
        if l2_ok and cos_ok:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": "vector similarity queries gave wrong answers"}])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        try:
            cid, db, _role = self.database(ctx, s)
        except HookFailure as exc:
            if exc.code == "not_provisioned":  # the database is already gone, and the extension with it
                return ctx.result(evidence=[evidence("removed", True, "cleanup", "database already removed")], state={})
            raise
        out = s.exec(cid, PSQL + ["-U", "postgres", "-d", db, "-c", "DROP EXTENSION IF EXISTS vector RESTRICT"],
                     check=False, what="dropping the extension")
        if out.returncode != 0:
            raise HookFailure("has_dependents", "the vector extension is still used: " + s.scrub(out.stderr)[-300:],
                              status=OperationStatus.REFUSED)
        return ctx.result(evidence=[evidence("removed", True, "cleanup", "extension:vector")], state={})
