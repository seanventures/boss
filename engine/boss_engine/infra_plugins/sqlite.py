"""official.sqlite: a SQLite file for local and development use.

No server and no container: the file lives under the app's workdir (`config.path`, default
`data/<name>.sqlite3`), in WAL mode. Refused for a production environment: one writer, one host.

verify   opens the existing file read-write, writes and reads a row in a transaction that is rolled
         back, and runs PRAGMA quick_check.
backup   the SQLite online backup API to `backups/`, then the copy is opened, integrity-checked and
         every table's row count and content hash compared with the source.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ..platform.contract import HookContext, OperationResult, OperationStatus, PlannedChange
from ._runtime import HookFailure, OfficialPlugin, Session, evidence, new_password

PRODUCTION = {"production", "prod"}


def _fingerprint(conn: sqlite3.Connection) -> dict:
    out = {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        q = '"' + t.replace('"', '""') + '"'
        rows = sorted(repr(r) for r in conn.execute(f"SELECT * FROM {q}"))
        out[t] = {"rows": len(rows), "md5": hashlib.md5("\n".join(rows).encode()).hexdigest()}
    return out


class SqlitePlugin(OfficialPlugin):
    directory = "sqlite"

    def rel(self, ctx: HookContext) -> str:
        return ctx.resource.config.get("path") or f"data/{ctx.resource.name}.sqlite3"

    def path(self, ctx: HookContext) -> Path:
        base = Path(ctx.workdir).resolve()
        p = (base / self.rel(ctx)).resolve()
        if not str(p).startswith(str(base)):
            raise HookFailure("invalid_request", "config.path must stay inside the app's workdir")
        return p

    def _validate(self, ctx: HookContext) -> list[str]:
        if ctx.resource.environment in PRODUCTION:
            return ["official.sqlite is for local and development use; use official.postgres in production"]
        if ".." in Path(self.rel(ctx)).parts or Path(self.rel(ctx)).is_absolute():
            return ["config.path must be a relative path inside the app's workdir"]
        return []

    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return [PlannedChange(action="create", target=f"file:{self.rel(ctx)}", detail="SQLite database, WAL mode"),
                PlannedChange(action="create", target="secret:DATABASE_URL", detail="vault reference")]

    def connect(self, ctx: HookContext, *, create: bool = False) -> sqlite3.Connection:
        p = self.path(ctx)
        if not create and not p.is_file():
            raise HookFailure("not_provisioned", f"{self.rel(ctx)} does not exist")
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(f"file:{p}?mode={'rwc' if create else 'rw'}", uri=True, timeout=10,
                               isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def do_provision(self, ctx: HookContext, s: Session) -> OperationResult:
        with closing(self.connect(ctx, create=True)) as conn:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        ref = ctx.state.get("urlRef") or s.put("DATABASE_URL", f"sqlite:///{self.path(ctx)}").secretRef
        state = {**ctx.state, "urlRef": ref, "path": self.rel(ctx)}
        return ctx.result(bindings=self.bindings(ctx, {"DATABASE_URL": ref}), state=state,
                          evidence=[evidence("file", True, "config", f"{self.rel(ctx)}, journal_mode {mode}")])

    def do_configure(self, ctx: HookContext, s: Session) -> OperationResult:
        if not ctx.state.get("urlRef"):
            raise HookFailure("not_provisioned", f"{ctx.resource_id} has not been provisioned")
        return ctx.result(bindings=self.bindings(ctx, {"DATABASE_URL": ctx.state["urlRef"]}), state=ctx.state,
                          evidence=[evidence("binding", True, "config", "DATABASE_URL is a vault reference")])

    def do_verify(self, ctx: HookContext, s: Session) -> OperationResult:
        nonce = new_password(4)
        with closing(self.connect(ctx)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE boss_verify (id INTEGER PRIMARY KEY, note TEXT)")
            conn.execute("INSERT INTO boss_verify (id, note) VALUES (1, ?)", (nonce,))
            got = conn.execute("SELECT note FROM boss_verify WHERE id = 1").fetchone()
            conn.execute("ROLLBACK")
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            left = conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'boss_verify'").fetchone()[0]
        ok = bool(got) and got[0] == nonce and left == 0
        ev = [evidence("write_read_row", ok, "connection", f"opened {self.rel(ctx)}; wrote and read a row; rolled back"),
              evidence("quick_check", check == "ok", "healthcheck", f"PRAGMA quick_check: {check}")]
        if ok and check == "ok":
            return ctx.result(evidence=ev, state=ctx.state)
        return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "verify_failed", "message": "sqlite verify failed"}])

    def do_observe(self, ctx: HookContext, s: Session) -> OperationResult:
        p = self.path(ctx)
        wal = p.with_name(p.name + "-wal")
        return ctx.result(observed={"healthy": p.is_file(), "sizeBytes": p.stat().st_size if p.is_file() else 0,
                                    "walBytes": wal.stat().st_size if wal.is_file() else 0},
                          evidence=[evidence("stat", True, "observe", "file size read")])

    def do_backup(self, ctx: HookContext, s: Session) -> OperationResult:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        rel = f"backups/{ctx.resource.name}-{stamp}.sqlite3"
        dest = Path(ctx.workdir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect(ctx)) as src:
            with closing(sqlite3.connect(dest)) as out:
                src.backup(out)
            before = _fingerprint(src)
        with closing(sqlite3.connect(f"file:{dest}?mode=ro", uri=True)) as copy:
            integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
            restored = _fingerprint(copy)
        match = integrity == "ok" and restored == before
        data = dest.read_bytes()
        manifest = {"file": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "tables": before}
        dest.with_suffix(".json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        detail = (f"copy opened, integrity ok, {len(restored)} tables, {sum(t['rows'] for t in restored.values())} rows match"
                  if match else f"copy differs from the source (integrity {integrity})")
        ev = [evidence("backup", True, "backup", f"{len(data)} bytes"), evidence("tested_restore", match, "restore", detail)]
        state = {**ctx.state, "lastBackup": rel}
        arts = [rel, rel.rsplit(".", 1)[0] + ".json"]
        if not match:
            return ctx.result(OperationStatus.FAILED, evidence=ev, artifacts=arts, state=state,
                              errors=[{"code": "restore_test_failed", "message": detail}])
        return ctx.result(evidence=ev, artifacts=arts, state=state)

    def do_restore(self, ctx: HookContext, s: Session) -> OperationResult:
        rel = ctx.resource.config.get("restoreFrom") or ctx.state.get("lastBackup")
        src = (Path(ctx.workdir) / (rel or "")).resolve()
        if not rel or not str(src).startswith(str(Path(ctx.workdir).resolve())) or not src.is_file():
            raise HookFailure("invalid_request", "nothing to restore: no backup file in the app's workdir")
        manifest_path = src.with_suffix(".json")
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        with closing(sqlite3.connect(f"file:{src}?mode=ro", uri=True)) as b:
            with closing(self.connect(ctx)) as live:
                b.backup(live)
                now = _fingerprint(live)
        match = not manifest.get("tables") or now == manifest["tables"]
        ev = [evidence("rows_match_backup", match, "restore", f"{len(now)} tables restored from {rel}")]
        if not match:
            return ctx.result(OperationStatus.FAILED, evidence=ev, errors=[{"code": "restore_mismatch", "message": "differs"}])
        return ctx.result(evidence=ev, state=ctx.state)

    def do_destroy(self, ctx: HookContext, s: Session) -> OperationResult:
        self.guard_destroy(ctx)
        p, removed = self.path(ctx), []
        for f in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
            if f.exists():
                f.unlink()
                removed.append(f"file:{f.name}")
        if ref := ctx.state.get("urlRef"):
            ctx.secrets.delete(ref)
            removed.append("secret:DATABASE_URL")
        return ctx.result(evidence=[evidence("removed", True, "cleanup", ", ".join(removed))], state={})


Plugin = SqlitePlugin
