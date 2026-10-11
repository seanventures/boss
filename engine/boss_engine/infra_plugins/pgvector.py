"""official.pgvector: the `vector` extension on a database official.postgres provisioned.

Not a vector database: it is PostgreSQL with one extension, so it shares the database's backups,
role and network. The request names its database with `config.database` (and `dependsOn`); BOSS hands
the plugin that resource's state as `ctx.state["upstream"]`.

provision  CREATE EXTENSION IF NOT EXISTS vector (as the database owner's superuser).
verify     AS THE APP: an HNSW index and nearest-neighbour queries by L2 and cosine distance on a
           temporary table, in a transaction that is rolled back.
destroy    DROP EXTENSION vector RESTRICT: refused while any column still uses the type.
"""
from __future__ import annotations

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import HookFailure, OfficialPlugin, Session, evidence

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


class PgvectorPlugin(OfficialPlugin):
    directory = "pgvector"

    def _validate(self, ctx: HookContext) -> list[str]:
        on = ctx.resource.config.get("database")
        if on and on not in ctx.resource.dependsOn:
            return [f"config.database names {on!r}; list it in dependsOn too, so it is provisioned first"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"extension:vector@{ctx.resource.config['database']}",
                              detail="CREATE EXTENSION IF NOT EXISTS vector")]

    def upstream(self, ctx: HookContext) -> dict:
        up = ctx.state.get("upstream") or {}
        if not up.get("adminUrlRef"):
            raise HookFailure("not_provisioned", f"database {ctx.resource.config['database']!r} has not been provisioned; "
                              "pgvector needs it first")
        if "pgvector" not in str(up.get("image", "")):
            raise HookFailure("invalid_request", f"database {ctx.resource.config['database']!r} runs an image without "
                              "pgvector; add `features: [pgvector]` to it and provision it again")
        return up

    def psql(self, s: Session, up: dict, url: str, sql: str, what: str) -> str:
        proc = s.probe(up["image"], ["-c", 'exec psql -X -q -At -v ON_ERROR_STOP=1 -d "$BOSS_PG_URL" -f -'],
                       env={"BOSS_PG_URL": url}, stdin=sql.encode(), network=up["network"], entrypoint="sh", what=what)
        return s.text(proc).strip()

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        up = self.upstream(ctx)
        version = self.psql(s, up, s.get(up["adminUrlRef"]), "CREATE EXTENSION IF NOT EXISTS vector;\n"
                            "SELECT extversion FROM pg_extension WHERE extname = 'vector';", "creating the extension")
        state = {"upstream": up, "extension": "vector", "version": version}
        return ctx.result(state=state, evidence=[evidence("extension", True, "config", f"vector {version}")])

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        up = self.upstream(ctx)
        out = self.psql(s, up, s.get(up["urlRef"]), VERIFY_SQL, "verifying pgvector").splitlines()
        l2_ok = len(out) >= 1 and out[0] == "a,d,b,c"
        cos_ok = len(out) >= 2 and out[1] == "c"
        ev = [evidence("l2_nearest_neighbours", l2_ok, "connection",
                       f"order by <-> gave {out[0] if out else 'nothing'} (want a,d,b,c), HNSW index"),
              evidence("cosine_nearest", cos_ok, "connection", f"<=> nearest {out[1] if len(out) > 1 else '-'} (want c)"),
              evidence("extension_version", len(out) >= 3, "config", f"vector {out[2] if len(out) > 2 else '?'}")]
        if l2_ok and cos_ok:
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev,
                          errors=[{"code": "verify_failed", "message": "vector similarity queries gave wrong answers"}])

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        up = self.upstream(ctx)
        proc = s.probe(up["image"], ["-c", 'exec psql -X -q -At -v ON_ERROR_STOP=1 -d "$BOSS_PG_URL" -f -'],
                       env={"BOSS_PG_URL": s.get(up["adminUrlRef"])}, stdin=b"DROP EXTENSION IF EXISTS vector RESTRICT;",
                       network=up["network"], entrypoint="sh", check=False, what="dropping the extension")
        if proc.returncode != 0:
            raise HookFailure("has_dependents", "the vector extension is still used by a column: "
                              + s.scrub(proc.stderr.decode(errors="replace"))[-300:], status=OperationStatus.REFUSED)
        return ctx.result(evidence=[evidence("removed", True, "cleanup", "extension:vector")], state={})


Plugin = PgvectorPlugin
