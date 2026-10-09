"""SCANS AND FINDINGS (spec §4.3, §4.4): request, run (worker job `security.scan`), store, triage.

`request_scan` checks scope (scope.py), records a queued scan and queues the job. `execute_scan`
(the API runs it when the worker hands it the job: the scanners and the sandbox helper live in the
api image) checks out the app, runs each tool as the run's sandbox uid, parses, fingerprints,
redacts and stores. `triage` is the only way a finding's status changes by hand: a reason and an
actor, recorded as an event; findings are never deleted (S4.5).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import bindparam, text

from ..services import owned_process, sandbox_identity
from ..services.core import emit_event, make_id
from . import runners, scope
from .findings import SEVERITIES, TRIAGE_STATUSES, Finding, counts
from .runners import ToolResult

log = logging.getLogger("boss.security_scan")

JOB_KIND = "security.scan"
OWNER_KIND = "sandbox"              # a workload kind: everything spawned in it runs as the run's uid
MAX_REASON = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scan_row(conn: Any, scan_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(text("SELECT * FROM security_scans WHERE id = :i"), {"i": scan_id}).mappings().first()
    return dict(row) if row else None


def public_scan(row: dict[str, Any]) -> dict[str, Any]:
    target = dict(row.get("target") or {})
    target.pop("inventory", None)          # internal: what the report compares against
    return {"id": row["id"], "project_id": row["project_id"], "app_key": row["app_key"],
            "commit_sha": row.get("commit_sha"), "workflow_run_id": row.get("workflow_run_id"),
            "status": row["status"], "target": target, "tools": row.get("tools") or [],
            "counts": row.get("counts") or {}, "triggered_by": row.get("triggered_by"),
            "started_at": row["started_at"].isoformat() if row.get("started_at") else None,
            "ended_at": row["ended_at"].isoformat() if row.get("ended_at") else None}


# ---- request -----------------------------------------------------------------------------------

def request_scan(conn: Any, *, project_id: str, app_key: str, ref: Optional[str] = None,
                 tools: Any = None, zap: Optional[dict] = None, triggered_by: str,
                 workflow_run_id: Optional[str] = None, settings: Any = None,
                 enqueue: bool = True) -> dict[str, Any]:
    src = scope.resolve_app(conn, project_id, app_key, ref, settings)
    req = scope.resolve_request(conn, project_id, app_key, tools=tools, zap=zap)
    if not runners.installed():
        raise scope.ScopeRefused("scanners_not_installed", runners.missing_message(), status=409)
    scan_id = make_id("scan")
    target = {"source": {"branch": src.branch, "commit": src.commit, "run_id": src.run_id},
              "tools": list(req.tools),
              "zap": {"mode": req.zap_mode, **req.target} if "zap" in req.tools else None}
    conn.execute(text(
        "INSERT INTO security_scans (id, project_id, app_key, commit_sha, workflow_run_id, target, "
        "status, triggered_by) VALUES (:i, :p, :a, :c, :w, CAST(:t AS JSONB), 'queued', :by)"),
        {"i": scan_id, "p": project_id, "a": app_key, "c": src.commit, "w": workflow_run_id,
         "t": json.dumps(target), "by": triggered_by})
    if enqueue:
        from ..services.jobs import enqueue_job

        enqueue_job(conn, project_id, JOB_KIND, {"scan_id": scan_id}, dedupe_key=f"{JOB_KIND}:{scan_id}",
                    max_attempts=1, workload_class="general")
    emit_event(conn, project_id, "security_scan.requested", triggered_by, "security_scan", scan_id,
               {"app_key": app_key, "tools": list(req.tools), "zap_mode": req.zap_mode,
                "target": req.target.get("kind")})
    return public_scan(_scan_row(conn, scan_id))


# ---- run ---------------------------------------------------------------------------------------

def _open_tree(root: Path) -> None:
    """BOSS cloned it; the run's uid must be able to write in it (npm install, build output)."""
    for dirpath, dirnames, filenames in os.walk(root):
        with contextlib.suppress(OSError):
            os.chmod(dirpath, 0o2777)
        for name in filenames:
            p = Path(dirpath) / name
            if not p.is_symlink():
                with contextlib.suppress(OSError):
                    os.chmod(p, 0o666 | (p.stat().st_mode & 0o111))


def checkout(engine: Any, settings: Any, src: scope.AppSource, workdir: Path) -> str:
    """The app's code at the ref, read-only to the outside world: no credential stays in it (the
    clone's credential rides in git's environment only, identity.runs.repo_access). Returns the
    commit sha."""
    from ..services import preview_service

    local = None if src.pull_request_url else preview_service.find_local_run_workspace(src.run_id)
    if local is not None:
        shutil.copytree(local, workdir, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("node_modules", ".npm-cache"))
    else:
        from ..identity import runs as identity_runs
        from ..services.github_connections import github_token_for

        if not (src.owner and src.repo):
            raise RuntimeError("the app has no repository to scan")

        def _token() -> str:
            with engine.begin() as c:
                return github_token_for(c, settings, src.project_id) or ""

        with identity_runs.repo_access(engine, host=getattr(settings, "target_repo_host", "github") or "github",
                                       owner=src.owner, repo=src.repo, project_id=src.project_id,
                                       token=_token) as access:
            proc = owned_process.boss_run(
                ["git", "clone", "--depth", "200", "--branch", src.branch, access.url, str(workdir)],
                capture_output=True, text=True, timeout=300, env=access.environ())
        if proc.returncode != 0:
            from .redact import redact_text
            raise RuntimeError(f"git clone of the app's branch failed: {redact_text((proc.stderr or '')[-300:])}")
    if src.commit and (workdir / ".git").exists():
        proc = owned_process.boss_run(["git", "-C", str(workdir), "checkout", "--detach", src.commit],
                                      capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"commit {src.commit} is not on the app's branch {src.branch}")
    sha = ""
    if (workdir / ".git").exists():
        proc = owned_process.boss_run(["git", "-C", str(workdir), "rev-parse", "HEAD"],
                                      capture_output=True, text=True, timeout=30)
        sha = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    _open_tree(workdir)
    return sha or (src.commit or "")


def _run_tools(src_dir: Path, out_dir: Path, plan: dict[str, Any], home: Path) -> tuple[list[ToolResult], list[str]]:
    env = runners.tool_env(home)
    results: list[ToolResult] = []
    tools = plan.get("tools") or []
    inventory = runners.code_inventory(src_dir)
    if "semgrep" in tools:
        results.append(runners.run_semgrep(src_dir, out_dir, env=env))
    if "osv" in tools:
        results.append(runners.run_osv(src_dir, out_dir, env=env))
    if "zap" in tools:
        zap = plan.get("zap") or {}
        app = None
        try:
            if zap.get("kind") == "preview":
                target = str(zap["url"])
            else:
                app, target = runners.start_app(src_dir, out_dir / "app.log")
            results.append(runners.run_zap(target, src_dir, out_dir, mode=zap.get("mode") or "baseline", env=env))
        except runners.AppDidNotStart as exc:
            results.append(ToolResult("zap", status="failed", version=runners.installed().get("zap", ""),
                                      error=f"the app could not be started for the scan: {exc}"))
        finally:
            if app is not None:
                app.end(reason="security_scan_done", grace=3.0)
    return results, inventory


def execute_scan(engine: Any, settings: Any, scan_id: str, *,
                 run_tools: Any = None, do_checkout: Any = None) -> dict[str, Any]:
    """Run one queued scan to its end. Idempotent: a scan already done or failed is returned as is.
    `run_tools`/`do_checkout` are seams for tests (the real ones need the security toolchain)."""
    with engine.begin() as conn:
        row = _scan_row(conn, scan_id)
        if row is None:
            return {"ok": False, "error": f"no such scan: {scan_id}"}
        if row["status"] in ("done", "failed"):
            return {"ok": True, "scan": public_scan(row)}
        conn.execute(text("UPDATE security_scans SET status = 'running', started_at = now() WHERE id = :i"),
                     {"i": scan_id})
        try:
            src = scope.resolve_app(conn, row["project_id"], row["app_key"], row.get("commit_sha"), settings)
        except scope.ScopeRefused as exc:
            src, refused = None, str(exc)
    if src is None:
        return _finish(engine, row, [], [], "", status="failed", error=refused)
    plan = row.get("target") or {}
    owner_id = f"secscan_{scan_id}"
    results: list[ToolResult] = []
    inventory: list[str] = []
    sha = ""
    error = None
    try:
        with owned_process.owner_scope(OWNER_KIND, owner_id, project_id=row["project_id"], fresh=True):
            with sandbox_identity.bind(owner_id, project_id=row["project_id"]) as ident:
                base = ident.workspace("scan-") if ident is not None else Path(tempfile.mkdtemp(prefix="boss-scan-"))
                try:
                    src_dir, out_dir = base / "src", base / "out"
                    out_dir.mkdir()
                    os.chmod(out_dir, 0o2777)
                    sha = (do_checkout or checkout)(engine, settings, src, src_dir)
                    results, inventory = (run_tools or _run_tools)(
                        src_dir, out_dir, plan, ident.home if ident is not None else base)
                finally:
                    if ident is None:
                        shutil.rmtree(base, ignore_errors=True)
    except sandbox_identity.SandboxUnavailable as exc:
        error = f"the build sandbox is unavailable: {exc}"
    except Exception as exc:  # noqa: BLE001 — a scan that cannot run is a FAILED scan, with why
        from .redact import redact_text
        log.exception("security scan %s failed", scan_id)
        error = redact_text(str(exc))[:500]
    status = "failed" if error or not results or all(r.status == "failed" for r in results) else "done"
    return _finish(engine, row, results, inventory, sha, status=status, error=error)


def _finish(engine: Any, row: dict[str, Any], results: list[ToolResult], inventory: list[str],
            sha: str, *, status: str, error: Optional[str]) -> dict[str, Any]:
    scan_id, project_id, app_key = row["id"], row["project_id"], row["app_key"]
    findings: list[Finding] = [f for r in results if r.status == "done" for f in r.findings]
    routes = sorted({p for r in results for p in r.routes})
    c = counts(findings)
    c["failed_tools"] = [r.name for r in results if r.status == "failed"]
    if error:
        c["error"] = error
    with engine.begin() as conn:
        prior = _prior_state(conn, project_id, app_key, [f.fingerprint for f in findings], scan_id)
        for f in findings:
            first_seen, status_, by, reason = prior.get(f.fingerprint, (scan_id, "open", None, None))
            conn.execute(text(
                "INSERT INTO security_findings (id, scan_id, fingerprint, tool, rule_id, title, severity, "
                "confidence, cwe, owasp, location, evidence, status, triaged_by, triage_reason, "
                "first_seen_scan_id) VALUES (:id, :s, :fp, :tool, :rule, :title, :sev, :conf, :cwe, "
                ":owasp, CAST(:loc AS JSONB), CAST(:ev AS JSONB), :st, :by, :reason, :first) "
                "ON CONFLICT (scan_id, fingerprint) DO NOTHING"),
                {"id": make_id("sfind"), "s": scan_id, "fp": f.fingerprint, "tool": f.tool,
                 "rule": f.rule_id[:300], "title": f.title[:300], "sev": f.severity,
                 "conf": f.confidence, "cwe": f.cwe, "owasp": f.owasp,
                 "loc": json.dumps(f.location), "ev": json.dumps(f.evidence),
                 "st": status_, "by": by, "reason": reason, "first": first_seen})
        target = dict(row.get("target") or {})
        target["inventory"] = {"files": inventory, "routes": routes}
        conn.execute(text(
            "UPDATE security_scans SET status = :st, tools = CAST(:tools AS JSONB), counts = CAST(:c AS JSONB), "
            "target = CAST(:t AS JSONB), commit_sha = COALESCE(NULLIF(:sha, ''), commit_sha), "
            "started_at = COALESCE(started_at, now()), ended_at = now() WHERE id = :i"),
            {"st": status, "tools": json.dumps([r.record() for r in results]), "c": json.dumps(c),
             "t": json.dumps(target), "sha": sha, "i": scan_id})
        emit_event(conn, project_id, f"security_scan.{status}", "system", "security_scan", scan_id,
                   {"app_key": app_key, "commit_sha": sha, "total": c["total"],
                    "by_severity": c["by_severity"], "failed_tools": c["failed_tools"],
                    **({"error": error} if error else {})})
        return {"ok": status == "done", "scan": public_scan(_scan_row(conn, scan_id))}


def _prior_state(conn: Any, project_id: str, app_key: str, fps: list[str], scan_id: str) -> dict[str, tuple]:
    """For each fingerprint seen before in this app: (first_seen_scan_id, carried triage status,
    triaged_by, reason). A person's `accepted`/`false_positive` carries to the next scan; nothing
    else does (a finding seen again is open again)."""
    if not fps:
        return {}
    rows = conn.execute(text(
        "SELECT f.fingerprint, f.first_seen_scan_id, f.status, f.triaged_by, f.triage_reason "
        "FROM security_findings f JOIN security_scans s ON s.id = f.scan_id "
        "WHERE s.project_id = :p AND s.app_key = :a AND s.id <> :me AND f.fingerprint IN :fps "
        "ORDER BY s.started_at DESC NULLS LAST").bindparams(bindparam("fps", expanding=True)),
        {"p": project_id, "a": app_key, "me": scan_id, "fps": fps}).mappings().all()
    out: dict[str, tuple] = {}
    for r in rows:
        if r["fingerprint"] in out:
            continue
        carried = r["status"] if r["status"] in TRIAGE_STATUSES else "open"
        out[r["fingerprint"]] = (r["first_seen_scan_id"] or scan_id, carried,
                                 r["triaged_by"] if carried != "open" else None,
                                 r["triage_reason"] if carried != "open" else None)
    return out


# ---- read --------------------------------------------------------------------------------------

def get_scan(conn: Any, scan_id: str) -> Optional[dict[str, Any]]:
    row = _scan_row(conn, scan_id)
    return public_scan(row) if row else None


def list_scans(conn: Any, project_id: str, app_key: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT * FROM security_scans WHERE project_id = :p AND (CAST(:a AS TEXT) IS NULL OR app_key = :a) "
        "ORDER BY COALESCE(started_at, ended_at) DESC NULLS FIRST, id DESC LIMIT :n"),
        {"p": project_id, "a": app_key, "n": max(1, min(int(limit), 200))}).mappings().all()
    return [public_scan(dict(r)) for r in rows]


def public_finding(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("id", "scan_id", "fingerprint", "tool", "rule_id", "title",
                                    "severity", "confidence", "cwe", "owasp", "location",
                                    "evidence", "status", "triaged_by", "triage_reason",
                                    "first_seen_scan_id")}


def list_findings(conn: Any, scan_id: str, *, tool: Optional[str] = None, severity: Optional[str] = None,
                  status: Optional[str] = None) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT * FROM security_findings WHERE scan_id = :s "
        "AND (CAST(:t AS TEXT) IS NULL OR tool = :t) AND (CAST(:sev AS TEXT) IS NULL OR severity = :sev) "
        "AND (CAST(:st AS TEXT) IS NULL OR status = :st)"),
        {"s": scan_id, "t": tool, "sev": severity, "st": status}).mappings().all()
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted((public_finding(dict(r)) for r in rows),
                  key=lambda f: (rank.get(f["severity"], 9), f["tool"], f["title"]))


# ---- triage ------------------------------------------------------------------------------------

class TriageRefused(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def triage(conn: Any, finding_id: str, *, status: str, reason: str, actor: str) -> dict[str, Any]:
    """accepted | false_positive, with a reason and an actor; an event records it. Never a delete."""
    if status not in TRIAGE_STATUSES:
        raise TriageRefused("invalid_status", "status is accepted or false_positive")
    reason = " ".join(str(reason or "").split())
    if len(reason) < 3:
        raise TriageRefused("reason_required", "a triage needs a reason")
    row = conn.execute(text(
        "SELECT f.*, s.project_id FROM security_findings f JOIN security_scans s ON s.id = f.scan_id "
        "WHERE f.id = :i"), {"i": finding_id}).mappings().first()
    if row is None:
        raise TriageRefused("finding_not_found", "no such finding", status=404)
    before = row["status"]
    conn.execute(text("UPDATE security_findings SET status = :st, triaged_by = :by, triage_reason = :r "
                      "WHERE id = :i"),
                 {"st": status, "by": actor, "r": reason[:MAX_REASON], "i": finding_id})
    emit_event(conn, row["project_id"], "security_finding.triaged", actor, "security_finding", finding_id,
               {"scan_id": row["scan_id"], "fingerprint": row["fingerprint"], "from": before,
                "to": status, "reason": reason[:MAX_REASON]})
    out = dict(row)
    out.update({"status": status, "triaged_by": actor, "triage_reason": reason[:MAX_REASON]})
    return public_finding(out)
