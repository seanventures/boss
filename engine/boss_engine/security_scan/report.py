"""THE BEFORE/AFTER REPORT (spec §4.3, §4.4, §4.6; S4.6).

Every finding of the BEFORE scan is classified against the AFTER scan:

* still_open  — its fingerprint is in AFTER;
* moved       — not by fingerprint, but the same tool+rule+snippet is in AFTER elsewhere (a
                refactor moved the code; nothing was fixed);
* removed     — its file (code, lockfile) or route (DAST) no longer exists in AFTER: gone with the
                code, which is not a fix;
* not_rescanned — its tool did not run successfully in AFTER: no claim either way;
* fixed       — none of the above.

`new` is every AFTER finding whose fingerprint was not in BEFORE and that is not the far end of a
move. Before/after run with different tool versions or rule digests are flagged (`rules_differ`),
because then a disappearance may be the rules', not the fix's.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text

from .findings import SEVERITIES
from .service import _scan_row, list_findings, public_scan


class ReportError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _done_scans(conn: Any, project_id: str, app_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(text(
        "SELECT * FROM security_scans WHERE project_id = :p AND app_key = :a AND status = 'done' "
        "ORDER BY ended_at DESC NULLS LAST, id DESC LIMIT 2"), {"p": project_id, "a": app_key}).mappings().all()
    return [dict(r) for r in rows]


def _pick(conn: Any, project_id: str, app_key: str, scan_id: str) -> dict[str, Any]:
    row = _scan_row(conn, scan_id)
    if row is None or row["project_id"] != project_id or row["app_key"] != app_key:
        raise ReportError("scan_not_found", f"no scan {scan_id} of this app", status=404)
    if row["status"] != "done":
        raise ReportError("scan_not_done", f"scan {scan_id} is {row['status']}", status=409)
    return row


def _tool_map(scan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t.get("name"): t for t in (scan.get("tools") or []) if isinstance(t, dict)}


def _gone(f: dict[str, Any], inventory: dict[str, Any]) -> bool:
    loc = f.get("location") or {}
    files = set(inventory.get("files") or [])
    if f["tool"] == "zap":
        routes = set(inventory.get("routes") or [])
        return bool(routes) and loc.get("path") not in routes
    path = loc.get("lockfile") if f["tool"] == "osv" else loc.get("file")
    return bool(files) and bool(path) and path not in files


def _brief(f: dict[str, Any]) -> dict[str, Any]:
    loc = f.get("location") or {}
    where = (f"{loc.get('file')} ({loc.get('function')})" if f["tool"] == "semgrep"
             else f"{loc.get('package')}@{loc.get('version')} in {loc.get('lockfile')}" if f["tool"] == "osv"
             else f"{loc.get('method')} {loc.get('path')}" + (f" [{loc['param']}]" if loc.get("param") else ""))
    return {"id": f["id"], "fingerprint": f["fingerprint"], "tool": f["tool"], "rule_id": f["rule_id"],
            "title": f["title"], "severity": f["severity"], "status": f["status"], "where": where,
            "triage_reason": f.get("triage_reason")}


def build_report(conn: Any, project_id: str, app_key: str, *, before: Optional[str] = None,
                 after: Optional[str] = None) -> dict[str, Any]:
    if before and after:
        a, b = _pick(conn, project_id, app_key, before), _pick(conn, project_id, app_key, after)
    else:
        latest = _done_scans(conn, project_id, app_key)
        if after:
            b = _pick(conn, project_id, app_key, after)
            a = _pick(conn, project_id, app_key, before) if before else next(
                (s for s in latest if s["id"] != b["id"]), None)
        else:
            if len(latest) < (1 if before else 2):
                raise ReportError("not_enough_scans", "a report needs two finished scans of this app", status=409)
            b = latest[0]
            a = _pick(conn, project_id, app_key, before) if before else latest[1]
        if a is None:
            raise ReportError("not_enough_scans", "a report needs two finished scans of this app", status=409)
    if a["id"] == b["id"]:
        raise ReportError("same_scan", "before and after are the same scan")
    fa, fb = list_findings(conn, a["id"]), list_findings(conn, b["id"])
    ta, tb = _tool_map(a), _tool_map(b)
    by_fp_b = {f["fingerprint"]: f for f in fb}
    by_key_b: dict[str, list[dict[str, Any]]] = {}
    for f in fb:
        by_key_b.setdefault((f.get("location") or {}).get("snippet_key") or "", []).append(f)
    fps_a = {f["fingerprint"] for f in fa}
    inventory_b = ((b.get("target") or {}).get("inventory") or {})
    out: dict[str, list] = {"fixed": [], "still_open": [], "moved": [], "removed": [], "not_rescanned": []}
    moved_targets: set[str] = set()
    for f in fa:
        if f["fingerprint"] in by_fp_b:
            g = by_fp_b[f["fingerprint"]]
            out["still_open"].append({**_brief(g), "first_seen_scan_id": g.get("first_seen_scan_id")})
            continue
        if (tb.get(f["tool"]) or {}).get("status") != "done":
            out["not_rescanned"].append(_brief(f))
            continue
        twin = next((g for g in by_key_b.get((f.get("location") or {}).get("snippet_key") or "", [])
                     if g["fingerprint"] not in fps_a and g["fingerprint"] not in moved_targets), None)
        if twin is not None:
            moved_targets.add(twin["fingerprint"])
            out["moved"].append({**_brief(f), "to": _brief(twin)})
            continue
        out["removed" if _gone(f, inventory_b) else "fixed"].append(_brief(f))
    new = [_brief(g) for g in fb if g["fingerprint"] not in fps_a and g["fingerprint"] not in moved_targets]
    differ = []
    for name in sorted(set(ta) | set(tb)):
        x, y = ta.get(name) or {}, tb.get(name) or {}
        for key in ("version", "rules_digest", "image_digest"):
            if x.get(key) != y.get(key):
                differ.append({"tool": name, "field": key, "before": x.get(key), "after": y.get(key)})
    high_new = [f for f in new if f["severity"] in ("critical", "high")]

    def sev_counts(fs: list[dict[str, Any]]) -> dict[str, int]:
        c = {s: 0 for s in SEVERITIES}
        for f in fs:
            c[f["severity"]] += 1
        return c

    return {
        "app_key": app_key,
        "before": public_scan(a), "after": public_scan(b),
        "rules_differ": bool(differ), "differences": differ,
        "severity_before": sev_counts(fa), "severity_after": sev_counts(fb),
        **out, "new": new,
        "summary": {"fixed": len(out["fixed"]), "still_open": len(out["still_open"]),
                    "moved": len(out["moved"]), "removed": len(out["removed"]),
                    "not_rescanned": len(out["not_rescanned"]), "new": len(new),
                    "new_high_or_critical": len(high_new)},
    }


def _sha(scan: dict[str, Any]) -> str:
    return (scan.get("commit_sha") or "")[:12] or scan["id"]


def to_markdown(rep: dict[str, Any]) -> str:
    """The export (spec §4.6). Titles and locations come from scanned code: escaped for a table."""
    def esc(s: Any) -> str:
        return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")[:300]

    s = rep["summary"]
    lines = [f"# Security report: {esc(rep['app_key'])}", "",
             f"**{esc(_sha(rep['before']))} → {esc(_sha(rep['after']))}** — "
             f"{s['fixed']} fixed, {s['still_open']} still open, {s['new']} new "
             f"({s['new_high_or_critical']} high or critical), {s['moved']} moved, {s['removed']} removed.", ""]
    if rep["rules_differ"]:
        lines += ["> The two scans ran with different tools or rules; a finding that disappeared may "
                  "be the rules' doing, not the fix's.", ""]
        lines += [f"> - {esc(d['tool'])} {esc(d['field'])}: {esc(d['before'])} → {esc(d['after'])}"
                  for d in rep["differences"]] + [""]
    lines += ["| Severity | Before | After |", "|---|---|---|"]
    lines += [f"| {sev} | {rep['severity_before'][sev]} | {rep['severity_after'][sev]} |" for sev in SEVERITIES]
    for key, label in (("fixed", "Fixed"), ("new", "New"), ("still_open", "Still open"),
                       ("moved", "Moved"), ("removed", "Removed with the code"),
                       ("not_rescanned", "Not re-scanned (the tool failed)")):
        items = rep[key]
        if not items:
            continue
        lines += ["", f"## {label} ({len(items)})", "", "| Severity | Tool | Finding | Where |", "|---|---|---|---|"]
        lines += [f"| {f['severity']} | {f['tool']} | {esc(f['title'])} (`{esc(f['rule_id'])}`) | {esc(f['where'])} |"
                  for f in items]
    return "\n".join(lines) + "\n"
