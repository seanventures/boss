"""Recorded CVEs and the packages a scan named, for one Security screen (BOSSAPP-1257).

Reads rows the existing scanners already stored. App scans come from
``security_scan.service`` (the findings ``security_scan.parsers`` wrote). Project
runs come from ``services.security_scan``. This module does not start a scanner
and does not fill in a package, a CVE, or a count the row did not carry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from ..security_scan import service as scans
from . import security_scan as oss

_SEVERITY_RANK = {name: i for i, name in enumerate(("critical", "high", "medium", "low", "info"))}
_APP_SQL = (
    "SELECT DISTINCT app_key FROM app_template_applications WHERE project_id = :p",
    "SELECT DISTINCT app_key FROM app_components WHERE project_id = :p",
    "SELECT DISTINCT app_key FROM project_agents WHERE project_id = :p AND app_key <> '*'",
)


def _text(value: Any, limit: int = 200) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _when(row: dict[str, Any]) -> datetime:
    value = row.get("ended_at") or row.get("started_at")
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _cves(row: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        item = _text(value, 40)
        if item.startswith("CVE-") and item not in found:
            found.append(item)

    add(row.get("cve"))
    add(row.get("rule_id"))
    for key in ("ids", "aliases"):
        vals = evidence.get(key)
        if isinstance(vals, str):
            vals = [vals]
        if isinstance(vals, list):
            for item in vals:
                add(item)
    return found


def _package(row: dict[str, Any], loc: dict[str, Any]) -> str:
    named = _text(loc.get("package") if isinstance(loc.get("package"), str) else "", 200)
    if named and named.lower() != "unknown":
        return named
    for key in ("package", "pkg_name", "PkgName"):
        extra = _text(row.get(key), 200)
        if extra and extra.lower() != "unknown":
            return extra
    return ""


def _lockfile(row: dict[str, Any], loc: dict[str, Any]) -> str:
    lock = _text(loc.get("lockfile") if isinstance(loc.get("lockfile"), str) else "", 300)
    if lock:
        return lock
    path = _text(row.get("path"), 300).rstrip("/")
    return path.split("/")[-1] if path else ""


def _shape(row: dict[str, Any], *, app_key: str, finding_id: str) -> Optional[dict[str, Any]]:
    loc = _obj(row.get("location"))
    evidence = _obj(row.get("evidence"))
    package = _package(row, loc)
    severity = _text(row.get("severity"), 40).lower()
    if not package or not severity:
        return None
    cves = _cves(row, evidence)
    advisory = _text(row.get("rule_id"), 200)
    return {
        "id": finding_id,
        "app_key": app_key,
        "package": package,
        "version": _text(loc.get("version") if isinstance(loc.get("version"), str) else "", 80),
        "ecosystem": _text(
            loc.get("ecosystem") if isinstance(loc.get("ecosystem"), str) else "", 40
        ),
        "lockfile": _lockfile(row, loc),
        "severity": severity,
        "cve": cves[0] if cves else "",
        "advisory": advisory,
        "title": _text(row.get("title") or row.get("message"), 300),
        "status": _text(row.get("status"), 40) or "open",
    }


def _sbom(findings: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[dict[str, str]] = []
    for row in findings:
        key = (row["app_key"], row["ecosystem"], row["package"], row["version"], row["lockfile"])
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "app_key": row["app_key"],
                "name": row["package"],
                "version": row["version"],
                "ecosystem": row["ecosystem"],
                "lockfile": row["lockfile"],
            }
        )
    return out


def _names(conn: Any, project_id: str) -> dict[str, str]:
    try:
        with conn.begin_nested():
            rows = (
                conn.execute(
                    text(
                        "SELECT app_key, app_name FROM project_agents "
                        "WHERE project_id = :p AND app_key <> '*'"
                    ),
                    {"p": project_id},
                )
                .mappings()
                .all()
            )
    except Exception:  # noqa: BLE001 — a name is optional; the scan rows are the record
        return {}
    named: dict[str, str] = {}
    for row in rows:
        key, name = _text(row.get("app_key"), 80), _text(row.get("app_name"), 120)
        if key and name:
            named[key] = name
    return named


def _extra_apps(conn: Any, project_id: str) -> list[str]:
    found: list[str] = []
    for sql in _APP_SQL:
        try:
            with conn.begin_nested():
                rows = conn.execute(text(sql), {"p": project_id}).scalars().all()
        except Exception:  # noqa: BLE001 — that app table is not on every install
            continue
        found.extend(_text(row, 80) for row in rows)
    return [key for key in found if key and key != "*"]


def _scan_rows(conn: Any, project_id: str) -> list[dict[str, Any]]:
    rows = (
        conn.execute(text("SELECT * FROM security_scans WHERE project_id = :p"), {"p": project_id})
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _app_from_scans(
    conn: Any, project_id: str, app_key: str, group: list[dict[str, Any]], names: dict[str, str]
) -> dict[str, Any]:
    done = [row for row in group if row.get("status") == "done"]
    latest = max(done or group, key=lambda row: (_when(row), str(row.get("id") or "")))
    findings: list[dict[str, Any]] = []
    recorded = bool(done)
    if done:
        chosen = max(done, key=lambda row: (_when(row), str(row.get("id") or "")))
        for row in scans.list_findings(conn, str(chosen["id"])):
            shaped = _shape(row, app_key=app_key, finding_id=_text(row.get("id"), 80))
            if shaped is not None:
                findings.append(shaped)
        latest = chosen
    findings.sort(
        key=lambda row: (
            _SEVERITY_RANK.get(row["severity"], 9),
            row["package"],
            row["cve"],
            row["id"],
        )
    )
    return {
        "app_key": app_key,
        "app_name": names.get(app_key) or app_key,
        "recorded": recorded,
        "status": str(latest.get("status") or "none"),
        "scan_id": str(latest.get("id") or ""),
        "findings": findings,
    }


def _oss_apps(project_id: str, apps: dict[str, dict[str, Any]]) -> None:
    """Non-simulated runs only. A finding with no recorded package is not a row."""
    for run in oss.list_runs(project_id, limit=50):
        if run.get("simulated"):
            continue
        run_id = _text(run.get("id"), 80)
        raw = run.get("findings")
        if not isinstance(raw, list):
            continue
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            app_key = _text(item.get("app_key"), 80)
            if not app_key:
                continue
            cve = _text(item.get("cve"), 40)
            package = _package(item, _obj(item.get("location")))
            finding_id = _text(item.get("id"), 80) or f"{run_id}:{package}:{cve or index}"
            shaped = _shape(item, app_key=app_key, finding_id=finding_id)
            if shaped is None:
                continue
            app = apps.get(app_key)
            if app is None:
                app = apps[app_key] = {
                    "app_key": app_key,
                    "app_name": app_key,
                    "recorded": True,
                    "status": "done",
                    "scan_id": run_id,
                    "findings": [],
                }
            else:
                app["recorded"] = True
            if any(row["id"] == shaped["id"] for row in app["findings"]):
                continue
            app["findings"].append(shaped)
            app["findings"].sort(
                key=lambda row: (
                    _SEVERITY_RANK.get(row["severity"], 9),
                    row["package"],
                    row["cve"],
                    row["id"],
                )
            )


def read_screen(conn: Any, project_id: str) -> dict[str, Any]:
    """Every app's latest recorded scan, the packages it named, and nothing invented."""
    project_id = _text(project_id, 80)
    names = _names(conn, project_id) if project_id else {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    if project_id:
        for row in _scan_rows(conn, project_id):
            key = _text(row.get("app_key"), 80)
            if key:
                grouped.setdefault(key, []).append(row)
    apps = {
        key: _app_from_scans(conn, project_id, key, group, names) for key, group in grouped.items()
    }
    if project_id:
        _oss_apps(project_id, apps)
        for key in _extra_apps(conn, project_id):
            apps.setdefault(
                key,
                {
                    "app_key": key,
                    "app_name": names.get(key) or key,
                    "recorded": False,
                    "status": "none",
                    "scan_id": "",
                    "findings": [],
                },
            )
    ordered = [apps[key] for key in sorted(apps)]
    findings = [row for app in ordered for row in app["findings"]]
    return {
        "project_id": project_id,
        "recorded": any(app["recorded"] for app in ordered),
        "apps": ordered,
        "sbom": _sbom(findings),
    }
