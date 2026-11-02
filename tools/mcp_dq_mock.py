"""The DQ case study's mock MCP connectors — the DQ Monitor and the Profile Service — over NorthPeak Bank's synthetic loan DQ world in tools/dq_sample. Sample tools, like
the Service Desk and AML mocks: zero config, in-process, deterministic, no network, served by the
`mcp-tools` service (tools/mcp_tools_mux.py) on one port, one path per connector:

    /dq-monitor/mcp        list_exception_groups, get_exception_group, get_rule_run, rerun_check
    /profile-service/mcp   get_exception_profile, list_recent_changes

The Service Desk (incidents, teams; create / attach / update) is the shipped `service-desk` mock,
which carries the bank's DQ incidents too (tools/mcp_service_desk_mock.py).

Read-only. The DQ Monitor's `rerun_check` re-grades a rule on a COB "now": the mock world has no live
data, so a re-run after a recorded change (`change_ref`) measures the platform's baseline for the
rule (the Profile Service's 30-day rate) — the data is back to normal once the cause is fixed — and without
one it measures what the DQ Monitor measured that COB. Run standalone:

    .venv/bin/python -m uvicorn tools.mcp_dq_mock:app --port 8774

Implements: initialize, notifications/initialized, tools/list, tools/call.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Callable

from fastapi import FastAPI, Request, Response

from tools.dq_sample import data as D

app = FastAPI(title="boss-dq-mock")

_STR = {"type": "string"}


def _ds() -> D.Dataset:
    return D.generate()


def _summary(group: D.ExceptionGroup) -> dict[str, Any]:
    return {"group_id": group.group_id, "monitor_rule_id": group.monitor_rule_id,
            "rule_id": group.rule_id, "rule_version": group.rule_version, "cob": group.cob,
            "platform": group.platform, "column": group.column, "grade": group.monitor_grade,
            "failed": group.failed, "population": group.population,
            "emitted_at": group.emitted_at}


# ── the DQ Monitor ─────────────────────────────────────────────────────────────────────────────────────

def _list_exception_groups(args: dict) -> dict:
    """The DQ Monitor's exception groups for a close of business (every COB when none is named)."""
    ds = _ds()
    cob = str(args.get("cob") or "").strip()
    grade = str(args.get("grade") or "").strip().capitalize()
    groups = [g for g in ds.exception_groups
              if (not cob or g.cob == cob) and (not grade or g.monitor_grade == grade)]
    groups.sort(key=lambda g: (g.cob, g.emitted_at, g.group_id))
    return {"cob": cob or None, "groups": [_summary(g) for g in groups],
            "cobs": sorted({g.cob for g in ds.exception_groups})}


def _get_exception_group(args: dict) -> dict:
    group_id = str(args.get("group_id") or "").strip()
    ds = _ds()
    try:
        group = ds.exception_group(group_id)
    except KeyError:
        return {"error": f"no exception group '{group_id}'",
                "known_groups": [g.group_id for g in ds.exception_groups]}
    payload = ds.monitor_payload(group_id)
    payload["note"] = group.note
    return payload


def _baseline(ds: D.Dataset, rule: D.DqRule, platform: str) -> float | None:
    try:
        profile = ds.profile(platform)
    except KeyError:
        return None
    stats = profile.rules.get(rule.rule_id)
    return float(stats["baseline_rate_30d"]) if stats else None


def _run_of(ds: D.Dataset, rule: D.DqRule, cob: str, *, measured: float | None = None
            ) -> dict[str, Any]:
    group = next((g for g in ds.exception_groups if g.rule_id == rule.rule_id and g.cob == cob),
                 None)
    platform = group.platform if group else rule.system_id
    thresholds = dict(group.monitor_thresholds) if group else {"warning": rule.warning.value,
                                                             "error": rule.error.value}
    if measured is None:
        if group is not None:
            measured = group.measured
        else:
            base = _baseline(ds, rule, platform)
            measured = base if base is not None else 0.0
    return {"rule_id": rule.rule_id, "monitor_rule_id": rule.monitor_id, "rule_version": rule.version,
            "cob": cob, "platform": platform, "metric": rule.warning.metric,
            "measured": measured, "thresholds": thresholds,
            "grade": D.grade(rule, measured, thresholds),
            "exception_group": group.group_id if group else None}


def _rule(ds: D.Dataset, args: dict) -> D.DqRule | None:
    rule_id = str(args.get("rule_id") or "").strip()
    try:
        return ds.rule(rule_id) if rule_id.startswith("NPB-") else ds.rule_by_monitor_id(rule_id)
    except KeyError:
        return None


def _get_rule_run(args: dict) -> dict:
    """How a rule was graded on a COB: its exception group's measure when the DQ Monitor raised one,
    else the platform's usual rate for the rule (Clean)."""
    ds = _ds()
    rule = _rule(ds, args)
    cob = str(args.get("cob") or "").strip()
    if rule is None:
        return {"error": f"no rule '{args.get('rule_id')}'"}
    if not cob:
        return {"error": "name the close of business (cob, YYYY-MM-DD)"}
    return _run_of(ds, rule, cob)


def _rerun_check(args: dict) -> dict:
    """Re-run a rule's check for a COB now (Retrospective's confirmation)."""
    ds = _ds()
    rule = _rule(ds, args)
    cob = str(args.get("cob") or "").strip()
    if rule is None:
        return {"error": f"no rule '{args.get('rule_id')}'"}
    if not cob:
        return {"error": "name the close of business (cob, YYYY-MM-DD)"}
    change_ref = str(args.get("change_ref") or "").strip()
    first = _run_of(ds, rule, cob)
    if not change_ref:
        return {**first, "rerun": True, "change_ref": None,
                "note": "re-run without a recorded change: the data is as the DQ Monitor measured it"}
    base = _baseline(ds, rule, first["platform"])
    measured = base if base is not None else 0.0
    # After the change the DQ Monitor's configuration is the rulebook's (a threshold entered in the wrong
    # unit is corrected by exactly such a change), and the data is back at its usual rate.
    thresholds = {"warning": rule.warning.value, "error": rule.error.value}
    return {**first, "measured": measured, "thresholds": thresholds,
            "grade": D.grade(rule, measured, thresholds), "rerun": True,
            "change_ref": change_ref, "before": first["measured"],
            "before_grade": first["grade"],
            "note": f"re-run after {change_ref}: measured at the platform's usual rate against "
                    "the rulebook's thresholds"}


# ── the Profile Service ───────────────────────────────────────────────────────────────────────────────────

def _get_exception_profile(args: dict) -> dict:
    """The deterministic exception profile of a platform: its daily volume and, per rule,
    the 30-day baseline failure rate, its spread and the last 7 COBs; with `measured`, how far
    a value sits from that baseline."""
    ds = _ds()
    platform = str(args.get("platform") or "").strip()
    try:
        profile = ds.profile(platform)
    except KeyError:
        return {"error": f"no profile for platform '{platform}'",
                "platforms": [p.platform for p in ds.profiles]}
    rule_id = str(args.get("rule_id") or "").strip()
    out: dict[str, Any] = {"platform": platform, "daily_rows": profile.daily_rows}
    if rule_id:
        stats = profile.rules.get(rule_id)
        if stats is None:
            return {**out, "rule_id": rule_id, "profiled": False,
                    "note": "this rule's metric is not profiled on this platform"}
        out.update({"rule_id": rule_id, "profiled": True, **stats})
        if args.get("measured") not in (None, ""):
            measured = float(args["measured"])
            base, spread = float(stats["baseline_rate_30d"]), float(stats["stdev_30d"]) or 1e-12
            out.update({"measured": measured, "ratio_to_baseline": round(measured / base, 2)
                        if base else None, "z_score": round((measured - base) / spread, 1)})
    else:
        out["rules"] = profile.rules
    return out


def _list_recent_changes(args: dict) -> dict:
    """Changes on a platform (or touching a rule or feed) in a window ending at `until`."""
    ds = _ds()
    platform = str(args.get("platform") or "").strip()
    rule_id = str(args.get("rule_id") or "").strip()
    feed_id = str(args.get("feed_id") or "").strip()
    until = str(args.get("until") or "").strip()
    try:
        days = int(args.get("lookback_days") or 14)
    except (TypeError, ValueError):
        days = 14
    since = str(args.get("since") or "").strip()
    if until and not since:
        since = (date.fromisoformat(until) - timedelta(days=days)).isoformat()
    changes = []
    for c in ds.changes:
        if since and c.day < since:
            continue
        if until and c.day > until:
            continue
        if platform or rule_id or feed_id:
            if not ((platform and c.system_id == platform) or (rule_id and rule_id in c.rule_ids)
                    or (feed_id and feed_id in c.feed_ids)):
                continue
        changes.append({"change_id": c.change_id, "day": c.day, "system_id": c.system_id,
                        "description": c.description, "rule_ids": list(c.rule_ids),
                        "feed_ids": list(c.feed_ids)})
    changes.sort(key=lambda c: (c["day"], c["change_id"]))
    return {"since": since or None, "until": until or None, "changes": changes}


SERVERS: dict[str, dict[str, Any]] = {
    "dq-monitor": {
        "tools": [
            {"name": "list_exception_groups",
             "description": "The DQ Monitor's exception groups for a close of business (cob, "
                            "YYYY-MM-DD), optionally only one grade (Error, Warning).",
             "inputSchema": {"type": "object", "properties": {"cob": _STR, "grade": _STR}}},
            {"name": "get_exception_group",
             "description": "One exception group as the DQ Monitor publishes it: rule and version, COB, "
                            "platform, column, grade, measure, thresholds, sample keys.",
             "inputSchema": {"type": "object", "properties": {"group_id": _STR},
                             "required": ["group_id"]}},
            {"name": "get_rule_run",
             "description": "How a rule was graded on a COB (by rule id or DQ Monitor id).",
             "inputSchema": {"type": "object", "properties": {"rule_id": _STR, "cob": _STR},
                             "required": ["rule_id", "cob"]}},
            {"name": "rerun_check",
             "description": "Re-run a rule's check for a COB now; name the change that fixed "
                            "it (change_ref) to confirm the fix.",
             "inputSchema": {"type": "object", "properties": {
                 "rule_id": _STR, "cob": _STR, "change_ref": _STR},
                 "required": ["rule_id", "cob"]}},
        ],
        "fns": {"list_exception_groups": _list_exception_groups,
                "get_exception_group": _get_exception_group,
                "get_rule_run": _get_rule_run, "rerun_check": _rerun_check},
    },
    "profile-service": {
        "tools": [
            {"name": "get_exception_profile",
             "description": "A data platform's exception profile: daily volume and per-rule "
                            "30-day baseline failure rate, spread and last 7 COBs; with "
                            "measured, its distance from the baseline.",
             "inputSchema": {"type": "object", "properties": {
                 "platform": _STR, "rule_id": _STR, "measured": {"type": "number"}},
                 "required": ["platform"]}},
            {"name": "list_recent_changes",
             "description": "Changes on a platform, or touching a rule or feed, in the "
                            "lookback window before `until`.",
             "inputSchema": {"type": "object", "properties": {
                 "platform": _STR, "rule_id": _STR, "feed_id": _STR, "until": _STR,
                 "since": _STR, "lookback_days": {"type": "integer"}}}},
        ],
        "fns": {"get_exception_profile": _get_exception_profile,
                "list_recent_changes": _list_recent_changes},
    },
}
#: {server: {tool: fn}} for in-process callers and tests.
TOOL_FNS: dict[str, dict[str, Callable[[dict], dict]]] = {
    name: spec["fns"] for name, spec in SERVERS.items()}


def _rpc(server: str, body: dict) -> Any:
    method = body.get("method")
    rid = body.get("id")
    params = body.get("params") or {}
    spec = SERVERS[server]
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                           "serverInfo": {"name": f"boss-dq-{server}-mock", "version": "0.1.0"}}}
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": spec["tools"]}}
    if method == "tools/call":
        fn = spec["fns"].get(params.get("name"))
        if fn is None:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        result = fn(params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}],
                           "isError": bool(result.get("error"))}}
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def _route(server: str):
    async def handler(request: Request):
        return _rpc(server, await request.json())
    return handler


for _name in SERVERS:
    app.add_api_route(f"/{_name}/mcp", _route(_name), methods=["POST"])


@app.get("/health")
def health():
    return {"status": "ok", "server": "boss-dq-mock",
            "connectors": {name: [t["name"] for t in spec["tools"]]
                           for name, spec in SERVERS.items()}}
