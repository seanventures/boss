"""The built-in detectors (BOSSAPP-911; design §14.1). Each one is a bounded read of a store BOSS
already writes, plus arithmetic. No model, no writes. Every finding names the rows it rests on.

Where each reads (the shapes are the writers'; see the notes at each detector):

  hotspot          kg_change_links (PR → symbol), kg_nodes file/symbol, kg_edges fan-in,
                   incident_evidence rca_hypothesis.suspect_file
  incident_cause   incident_evidence rca_hypothesis (cause_key | exception | suspect_file)
  missing_tests    kg_change_links churn × kg_nodes file paths (rule_scan.is_test_path)
  review_repeat    code_task_runs.finalize_ctx.review_comments (deduped by comment id per run)
                   and review_finding learnings (attrs.seen_count)
  design_drift     code_task_runs.state.consistency_verdict = 'drift' and
                   code_task.design_from_prd_declined events
  cost_spike       run_scorecards cost_usd per workflow / task kind: last 7 days vs the 8-60
                   days before
  flaky_test       code_task_runs.state.baseline (environment / pre_existing failures) and
                   test_failure learnings
  slow_stage       code_task.build_profile events (stages.*.wall_min) and
                   finalize_ctx.stage_timings
  user_friction    person-made steer / cancel / stop / retry / waive / reject events
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .learning import Finding, clamp, detector, ev, log_scale, plural, table_exists

ROW_CAP = 5000


def _dt(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _path_of_symbol(key: str) -> str:
    from ..knowledge.code_index import symbol_location

    return symbol_location(key)[1]


def _is_test(path: str) -> bool:
    from ..knowledge.rule_scan import is_test_path

    return is_test_path(path)


def _churn(connection: Connection, project_id: str, since: datetime) -> dict[str, dict]:
    """path -> {prs: {source_id}, last: datetime}: how many distinct PRs touched each file."""
    rows = connection.execute(text(
        "SELECT source_id, symbol_key, recorded_at FROM kg_change_links WHERE project_id = :p "
        "AND source_kind = 'pull_request' AND recorded_at > :s ORDER BY recorded_at DESC "
        "LIMIT :n"), {"p": project_id, "s": since, "n": ROW_CAP}).all()
    out: dict[str, dict] = {}
    for source_id, key, at in rows:
        path = _path_of_symbol(key)
        if not path:
            continue
        entry = out.setdefault(path, {"prs": set(), "last": at})
        entry["prs"].add(source_id)
        entry["last"] = max(entry["last"], at)
    return out


def _file_nodes(connection: Connection, project_id: str, paths: list[str]) -> dict[str, dict]:
    if not paths:
        return {}
    rows = connection.execute(text(
        "SELECT DISTINCT ON (path) id, path, COALESCE((attrs->>'lines')::int, 0) AS lines "
        "FROM kg_nodes WHERE project_id = :p AND kind = 'file' AND valid_to IS NULL "
        "AND path = ANY(:paths) ORDER BY path, last_seen DESC"),
        {"p": project_id, "paths": paths}).mappings().all()
    return {r["path"]: dict(r) for r in rows}


def _fan_in(connection: Connection, project_id: str, path: str) -> int:
    return int(connection.execute(text(
        "SELECT count(*) FROM kg_edges e JOIN kg_nodes s ON s.id = e.dst_id WHERE "
        "e.project_id = :p AND e.kind IN ('calls', 'references') AND e.valid_to IS NULL "
        "AND s.kind = 'symbol' AND s.path = :path AND s.valid_to IS NULL"),
        {"p": project_id, "path": path}).scalar() or 0)


def _pr_nodes(connection: Connection, project_id: str, source_ids: list[str]) -> list[str]:
    return list(connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'pull_request' "
        "AND node_key = ANY(:k) LIMIT 5"),
        {"p": project_id, "k": [f"pull_request:{s}" for s in source_ids]}).scalars())


def _incidents_by_file(connection: Connection, project_id: str,
                       since: datetime) -> dict[str, list[dict]]:
    rows = connection.execute(text(
        "SELECT i.id, i.severity, i.first_seen, e.payload->>'suspect_file' AS path "
        "FROM incident_evidence e JOIN incidents i ON i.id = e.incident_id "
        "WHERE i.project_id = :p AND e.kind = 'rca_hypothesis' AND i.first_seen > :s "
        "AND COALESCE(e.payload->>'suspect_file', '') <> '' LIMIT :n"),
        {"p": project_id, "s": since, "n": ROW_CAP}).mappings().all()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if all(x["id"] != r["id"] for x in out[r["path"]]):
            out[r["path"]].append(dict(r))
    return out


# ── 1. code hotspots ────────────────────────────────────────────────────────────────────────

@detector("hotspot", title="Hotspot finder", kind="refactor", shape="ticket", source="kg",
          evidence_source="code", squad=("software_engineer", "chief_architect"))
def hotspot(connection: Connection, project_id: str, *, since: datetime, now: datetime,
            scale: float) -> list[Finding]:
    """churn × size × incidents: a file many PRs keep changing, that is big or that incidents
    point at, is where the next incident comes from."""
    churn = _churn(connection, project_id, since)
    incidents = _incidents_by_file(connection, project_id, since)
    nodes = _file_nodes(connection, project_id, list(churn))
    out: list[Finding] = []
    for path, c in churn.items():
        if _is_test(path):
            continue
        n_prs = len(c["prs"])
        lines = int((nodes.get(path) or {}).get("lines") or 0)
        hits = incidents.get(path, [])
        if n_prs < 5 * scale or not (lines >= 300 * scale or hits):
            continue
        fan_in = _fan_in(connection, project_id, path)
        impact = clamp(25 + 30 * log_scale(n_prs, 40) + 12 * min(len(hits), 3)
                       + 10 * log_scale(lines, 3000))
        refs = ([ev("kg_node", nodes[path]["id"], f"{lines} lines")] if path in nodes else [])
        refs += [ev("kg_node", n, "a PR that changed it") for n in
                 _pr_nodes(connection, project_id, sorted(c["prs"]))[:3]]
        refs += [ev("incident", h["id"], "points at this file", h["first_seen"]) for h in hits]
        if not refs:
            refs = [ev("query", f"kg_change_links path={path}", f"{n_prs} PRs")]
        newest = max([c["last"]] + [h["first_seen"] for h in hits])
        why = (f"{plural(n_prs, 'PR')} changed {path} in 90 days"
               + (f" and {plural(len(hits), 'incident')} pointed at it" if hits else "")
               + f"; it has {lines} lines and {fan_in} callers.")
        out.append(Finding("hotspot", (path,), f"file:{path}",
                           f"Split up hotspot {path.rsplit('/', 1)[-1]}", why, impact,
                           5 if lines > 1500 else 3, refs,
                           {"prs": n_prs, "lines": lines, "incidents": len(hits),
                            "fan_in": fan_in}, newest,
                           severity="high" if hits else "medium", blast_radius=fan_in))
    return sorted(out, key=lambda f: -f.impact)[:20]


# ── 2. repeated incident causes ─────────────────────────────────────────────────────────────

_NOISE = re.compile(r"(0x[0-9a-f]+|[0-9a-f-]{36}|[0-9a-f]{8,}|\d+)", re.I)


def _cause_key(payload: dict) -> tuple[str, str]:
    """(key, words) of a root cause: the RCA's own cause_key, else the exception's shape, else
    the suspect file, else the basis. Ids and numbers are folded so repeats collide."""
    for field in ("cause_key", "exception", "suspect_file", "root_cause"):
        raw = str(payload.get(field) or "").strip()
        if raw:
            first = raw.splitlines()[0].lower()
            # A cause_key / file path is already a stable identity; free text (an exception,
            # a root-cause sentence) has ids and numbers folded so repeats collide.
            words = (first if field in ("cause_key", "suspect_file")
                     else _NOISE.sub("#", first))[:120]
            said = str(payload.get("root_cause") or raw).strip().splitlines()[0]
            return f"{field}:{words}", said[:120]
    return "", ""


@detector("incident_cause", title="Incident patterns", kind="hardening", shape="ticket",
          source="kg", evidence_source="incident", squad=("sre", "software_engineer"))
def incident_cause(connection: Connection, project_id: str, *, since: datetime,
                   now: datetime, scale: float) -> list[Finding]:
    """The same root cause behind two or more incidents: fix the cause, not the next page."""
    rows = connection.execute(text(
        "SELECT i.id, i.severity, i.service, i.first_seen, e.payload FROM incident_evidence e "
        "JOIN incidents i ON i.id = e.incident_id WHERE i.project_id = :p AND "
        "e.kind = 'rca_hypothesis' AND i.first_seen > :s AND i.status <> 'dismissed' "
        "ORDER BY i.first_seen DESC LIMIT :n"),
        {"p": project_id, "s": since, "n": ROW_CAP}).mappings().all()
    by_cause: dict[str, dict] = {}
    for r in rows:
        payload = dict(r["payload"] or {})
        key, words = _cause_key(payload)
        if not key:
            continue
        entry = by_cause.setdefault(key, {"words": words, "incidents": {}, "file": "",
                                          "service": r["service"]})
        entry["incidents"].setdefault(r["id"], r)
        entry["file"] = entry["file"] or str(payload.get("suspect_file") or "")
    out: list[Finding] = []
    for key, entry in by_cause.items():
        incs = list(entry["incidents"].values())
        if len(incs) < max(2, round(2 * scale)):
            continue
        critical = sum(1 for i in incs if i["severity"] == "critical")
        impact = clamp(40 + 12 * len(incs) + 10 * critical)
        newest = max(i["first_seen"] for i in incs)
        where = entry["file"] or entry["service"] or "the service"
        out.append(Finding(
            "incident_cause", (key,), f"file:{entry['file']}" if entry["file"]
            else f"cause:{key}",
            f"Stop repeat incidents: {entry['words'][:60]}",
            f"{plural(len(incs), 'incident')} in 90 days share one root cause: "
            f"{entry['words']}.", impact, 3,
            [ev("incident", i["id"], i["severity"], i["first_seen"]) for i in incs[:10]],
            {"incidents": len(incs), "critical": critical, "cause": entry["words"]}, newest,
            severity="high" if critical else "medium"))
    return sorted(out, key=lambda f: -f.impact)[:20]


def short(path: str) -> str:
    return path.rsplit("/", 1)[-1] if "/" in path else path


# ── 3. missing tests for frequently changed code ────────────────────────────────────────────

def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.split(".", 1)[0].lower()


@detector("missing_tests", title="Test gap finder", kind="test_gap", shape="ticket",
          source="kg", evidence_source="code", squad=("qa", "software_engineer"))
def missing_tests(connection: Connection, project_id: str, *, since: datetime,
                  now: datetime, scale: float) -> list[Finding]:
    """A file changed by several PRs that no test file in the code graph is named for. (No
    indexer writes `tests` edges yet, so coverage is judged by test-file naming.)"""
    churn = _churn(connection, project_id, since)
    if not churn:
        return []
    files = [p for (p,) in connection.execute(text(
        "SELECT DISTINCT path FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
        "AND valid_to IS NULL AND path IS NOT NULL LIMIT 50000"), {"p": project_id}).all()]
    if not files:
        return []                      # no code graph: coverage cannot be judged
    test_names = " ".join(p.rsplit("/", 1)[-1].lower() for p in files if _is_test(p))
    nodes = _file_nodes(connection, project_id, list(churn))
    out: list[Finding] = []
    for path, c in churn.items():
        if _is_test(path) or path not in nodes:
            continue
        n_prs = len(c["prs"])
        stem = _stem(path)
        if n_prs < 3 * scale or len(stem) < 3 or stem in ("__init__", "index", "main"):
            continue
        if stem in test_names:
            continue
        impact = clamp(30 + 35 * log_scale(n_prs, 30))
        out.append(Finding(
            "missing_tests", (path,), f"file:{path}", f"Add tests for {short(path)}",
            f"{plural(n_prs, 'PR')} changed {path} in 90 days and no test file is named for "
            "it.", impact, 2,
            [ev("kg_node", nodes[path]["id"], "no matching test file")]
            + [ev("kg_node", n, "a PR that changed it")
               for n in _pr_nodes(connection, project_id, sorted(c["prs"]))[:3]],
            {"prs": n_prs, "stem": stem}, c["last"], severity="low"))
    return sorted(out, key=lambda f: -f.impact)[:20]


# ── 4. review findings that recur across builds ─────────────────────────────────────────────

_PATHISH = re.compile(r"[\w./-]+\.(py|ts|tsx|js|go|java|rb|rs|sql|kt|cs)(:\d+)?", re.I)


def finding_shape(body: str) -> str:
    """The reusable part of a review comment: paths, numbers and quotes folded away, first
    twelve words — 'missing null check on user' recurs; 'line 41 of a.py' does not."""
    words = _PATHISH.sub("<file>", str(body or "").lower())
    words = re.sub(r"[`'\"].*?[`'\"]", "<x>", words)
    words = _NOISE.sub("#", words)
    return " ".join(re.findall(r"[a-z<>#]+", words)[:12])


def _head(body: str, words: int = 8) -> str:
    """The first words of a finding, for a title: 'Payment retry has no idempotency key…'."""
    plain = re.sub(r"\s+", " ", _PATHISH.sub("", str(body or ""))).strip()
    first = re.split(r"[.;:](?:\s|$)|\n", plain, maxsplit=1)[0].split()
    return " ".join(first[:words]) + ("…" if len(first) > words else "")


@detector("review_repeat", title="Review patterns", kind="process", shape="ticket",
          source="kg", evidence_source="review", squad=("qa", "adversarial_reviewer"))
def review_repeat(connection: Connection, project_id: str, *, since: datetime,
                  now: datetime, scale: float) -> list[Finding]:
    """The same review finding in three or more builds: make it a lint rule or a skill, so no
    reviewer has to say it again."""
    rows = connection.execute(text(
        "SELECT id, created_at, COALESCE(finalize_ctx->>'root_run_id', id) AS root, "
        "finalize_ctx->'review_comments' AS comments FROM code_task_runs WHERE "
        "project_id = :p AND created_at > :s AND jsonb_typeof(finalize_ctx->'review_comments') "
        "= 'array' ORDER BY created_at DESC LIMIT 2000"),
        {"p": project_id, "s": since}).mappings().all()
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        seen_ids: set[str] = set()
        for c in r["comments"] or []:
            if not isinstance(c, dict) or c.get("author_kind") == "human":
                continue
            cid = str(c.get("id") or "")
            if cid and cid in seen_ids:
                continue
            seen_ids.add(cid)
            shape = finding_shape(c.get("body"))
            if len(shape.split()) < 3:
                continue
            key = (str(c.get("stage") or "review_code"), shape)
            g = groups.setdefault(key, {"roots": {}, "severity": set(), "sample": c.get("body"),
                                        "last": r["created_at"]})
            g["roots"].setdefault(r["root"], r["id"])
            g["severity"].add(str(c.get("severity") or "minor"))
            g["last"] = max(g["last"], r["created_at"])
    out: list[Finding] = []
    for (stage, shape), g in groups.items():
        builds = len(g["roots"])
        if builds < max(3, round(3 * scale)):
            continue
        heavy = bool(g["severity"] & {"major", "blocker"})
        impact = clamp(25 + 30 * log_scale(builds, 30) + (15 if heavy else 0))
        out.append(Finding(
            "review_repeat", (stage, shape), f"review:{stage}:{shape}",
            f"Make a rule: “{_head(g['sample'] or shape)}”",
            f"Reviewers flagged “{shape}” in {plural(builds, 'build')}; a lint rule or a skill "
            "would stop it before review.", impact, 2,
            [ev("code_task", run_id, f"{stage}") for run_id in list(g["roots"].values())[:8]],
            {"builds": builds, "stage": stage, "severities": sorted(g["severity"]),
             "example": str(g["sample"] or "")[:200]}, g["last"],
            severity="medium" if heavy else "low"))
    learned = connection.execute(text(
        "SELECT id, attrs, last_seen FROM kg_nodes WHERE project_id = :p AND kind = "
        "'review_finding' AND valid_to IS NULL AND COALESCE((attrs->>'seen_count')::int, 0) "
        ">= :n AND last_seen > :s ORDER BY last_seen DESC LIMIT 50"),
        {"p": project_id, "n": max(3, round(3 * scale)), "s": since}).mappings().all()
    for r in learned:
        attrs = dict(r["attrs"] or {})
        statement = str(attrs.get("statement") or "")[:160]
        shape = finding_shape(statement)
        if not shape or any(f.key[1] == shape for f in out):
            continue
        seen = int(attrs.get("seen_count") or 0)
        out.append(Finding(
            "review_repeat", ("learned", shape), f"review:learned:{shape}",
            f"Make a rule: “{_head(statement)}”",
            f"A learned review finding came back {seen} times: {statement}.",
            clamp(25 + 30 * log_scale(seen, 30)), 2, [ev("kg_node", r["id"], "learned")],
            {"seen_count": seen, "category": (attrs.get("detail") or {}).get("category")},
            r["last_seen"], severity="low"))
    return sorted(out, key=lambda f: -f.impact)[:20]


# ── 5. design drift: code vs PRD/ADR decisions ──────────────────────────────────────────────

@detector("design_drift", title="Design drift", kind="design_fix", shape="prd", source="kg",
          evidence_source="design", squad=("chief_architect", "pm"))
def design_drift(connection: Connection, project_id: str, *, since: datetime,
                 now: datetime, scale: float) -> list[Finding]:
    """Builds whose consistency check found the code no longer matches the approved design, and
    designs BOSS declined because the repository contradicts the PRD: the design needs a call."""
    groups: dict[str, dict] = {}
    for r in connection.execute(text(
            "SELECT id, decision_id, created_at, state->>'consistency_summary' AS summary "
            "FROM code_task_runs WHERE project_id = :p AND created_at > :s AND "
            "state->>'consistency_verdict' = 'drift' ORDER BY created_at DESC LIMIT 500"),
            {"p": project_id, "s": since}).mappings():
        key = r["decision_id"] or "project"
        g = groups.setdefault(key, {"runs": [], "paths": set(), "last": r["created_at"],
                                    "decision": r["decision_id"], "summary": r["summary"]})
        g["runs"].append(r["id"])
        g["last"] = max(g["last"], r["created_at"])
    for r in connection.execute(text(
            "SELECT entity_id, data, created_at FROM events WHERE project_id = :p AND "
            "event_type = 'code_task.design_from_prd_declined' AND created_at > :s "
            "ORDER BY created_at DESC LIMIT 500"), {"p": project_id, "s": since}).mappings():
        data = dict(r["data"] or {})
        key = str(data.get("decision_id") or "project")
        g = groups.setdefault(key, {"runs": [], "paths": set(), "last": r["created_at"],
                                    "decision": data.get("decision_id"),
                                    "summary": data.get("why")})
        if r["entity_id"] and r["entity_id"] not in g["runs"]:
            g["runs"].append(r["entity_id"])
        if data.get("path"):
            g["paths"].add(str(data["path"]))
        g["summary"] = g["summary"] or data.get("why")
        g["last"] = max(g["last"], r["created_at"])
    out: list[Finding] = []
    for key, g in groups.items():
        if len(g["runs"]) < max(2, round(2 * scale)) and not g["paths"]:
            continue
        refs = ([ev("decision", g["decision"], "the PRD decision")] if g["decision"] else [])
        refs += [ev("code_task", run, "drift") for run in g["runs"][:8]]
        path = sorted(g["paths"])[0] if g["paths"] else ""
        out.append(Finding(
            "design_drift", (key,), f"file:{path}" if path else f"design:{key}",
            "Reconcile code with its PRD" if g["decision"] else "Reconcile code with design",
            f"{plural(len(g['runs']), 'build')} found the code drifting from the approved "
            f"design. {str(g['summary'] or '').strip()[:200]}".strip(),
            clamp(45 + 10 * len(g["runs"])), 5, refs,
            {"builds": len(g["runs"]), "paths": sorted(g["paths"])[:10]}, g["last"]))
    return sorted(out, key=lambda f: -f.impact)[:10]


# ── 6. cost spikes per workflow / task class ────────────────────────────────────────────────

@detector("cost_spike", title="Cost watcher", kind="cost", shape="ticket", source="cost",
          evidence_source="cost", squad=("finops_sentinel", "model_spend_analyst"))
def cost_spike(connection: Connection, project_id: str, *, since: datetime, now: datetime,
               scale: float) -> list[Finding]:
    """A workflow or task kind whose median cost in the last 7 days is ≥ 1.5× its median over
    the 8-60 days before, on enough runs for the jump to be real and worth ≥ $5 a week."""
    if not table_exists(connection, "run_scorecards"):
        return []
    rows = connection.execute(text(
        "SELECT unit_kind, unit_id, COALESCE(workflow_key, task_kind, unit_kind) AS what, "
        "cost_usd, COALESCE(ended_at, computed_at) AS at FROM run_scorecards WHERE "
        "project_id = :p AND unit_kind IN ('build', 'workflow_run') AND cost_usd > 0 "
        "AND COALESCE(ended_at, computed_at) > :s ORDER BY 5 DESC LIMIT :n"),
        {"p": project_id, "s": now - timedelta(days=60), "n": ROW_CAP}).mappings().all()
    recent_cut = now - timedelta(days=7)
    by: dict[str, dict] = defaultdict(lambda: {"recent": [], "before": []})
    for r in rows:
        (by[r["what"]]["recent"] if r["at"] > recent_cut else by[r["what"]]["before"]).append(r)
    out: list[Finding] = []
    for what, g in by.items():
        if len(g["recent"]) < 3 or len(g["before"]) < 3:
            continue
        now_med = median(float(r["cost_usd"]) for r in g["recent"])
        was_med = median(float(r["cost_usd"]) for r in g["before"])
        ratio = now_med / was_med if was_med else 0
        extra_week = (now_med - was_med) * len(g["recent"])
        if ratio < 1.5 * scale or extra_week < 5 * scale:
            continue
        top = sorted(g["recent"], key=lambda r: -float(r["cost_usd"]))[:6]
        refs = [ev("code_task" if r["unit_kind"] == "build" else "workflow_run", r["unit_id"],
                   f"${float(r['cost_usd']):.2f}", r["at"]) for r in top]
        out.append(Finding(
            "cost_spike", (what,), f"cost:{what}", f"Bring {what} cost back down",
            f"{what} runs cost ${now_med:.2f} at the median this week, {ratio:.1f}× the "
            f"${was_med:.2f} before; that is about ${extra_week:.0f} extra a week.",
            clamp(30 + 40 * log_scale(extra_week * 4, 2000)), 2, refs,
            {"median_now_usd": round(now_med, 2), "median_before_usd": round(was_med, 2),
             "ratio": round(ratio, 2), "extra_per_week_usd": round(extra_week, 2),
             "runs": len(g["recent"])}, max(r["at"] for r in g["recent"]),
            severity="high" if ratio >= 3 else "medium"))
    return sorted(out, key=lambda f: -f.impact)[:10]


# ── 7. flaky tests ──────────────────────────────────────────────────────────────────────────

@detector("flaky_test", title="Flaky test finder", kind="test_gap", shape="ticket",
          source="ci", evidence_source="test", squad=("qa",))
def flaky_test(connection: Connection, project_id: str, *, since: datetime, now: datetime,
               scale: float) -> list[Finding]:
    """A test that builds keep classifying as failing for reasons the change did not cause
    (environment / pre-existing, baseline_verify), or a learned test failure that keeps coming
    back: it costs every build a repair round."""
    tests: dict[str, dict] = {}
    for r in connection.execute(text(
            "SELECT id, created_at, state->'baseline' AS baseline FROM code_task_runs WHERE "
            "project_id = :p AND created_at > :s AND jsonb_typeof(state->'baseline') = 'object' "
            "ORDER BY created_at DESC LIMIT 2000"), {"p": project_id, "s": since}).mappings():
        for bucket in ("environment", "pre_existing"):
            for t in (r["baseline"] or {}).get(bucket) or []:
                test_id = str((t or {}).get("test_id") or "").strip()
                if not test_id:
                    continue
                g = tests.setdefault(test_id, {"runs": [], "last": r["created_at"],
                                               "reasons": set()})
                if r["id"] not in g["runs"]:
                    g["runs"].append(r["id"])
                g["reasons"].add(bucket)
                g["last"] = max(g["last"], r["created_at"])
    out: list[Finding] = []
    for test_id, g in tests.items():
        if len(g["runs"]) < max(3, round(3 * scale)):
            continue
        path = test_id.split("::", 1)[0]
        out.append(Finding(
            "flaky_test", (test_id,), f"file:{path}" if "/" in path or "." in path
            else f"test:{test_id}", f"Fix or quarantine {short(test_id)[:60]}",
            f"{plural(len(g['runs']), 'build')} hit {test_id} failing for reasons the change "
            "did not cause.", clamp(30 + 35 * log_scale(len(g["runs"]), 30)), 1,
            [ev("code_task", run, "failed outside the change") for run in g["runs"][:8]],
            {"builds": len(g["runs"]), "reasons": sorted(g["reasons"])}, g["last"],
            severity="medium"))
    for r in connection.execute(text(
            "SELECT id, attrs, last_seen FROM kg_nodes WHERE project_id = :p AND kind = "
            "'test_failure' AND valid_to IS NULL AND COALESCE((attrs->>'seen_count')::int, 0) "
            ">= :n AND last_seen > :s ORDER BY last_seen DESC LIMIT 50"),
            {"p": project_id, "n": max(3, round(3 * scale)), "s": since}).mappings():
        detail = dict((r["attrs"] or {}).get("detail") or {})
        test_id = str(detail.get("test") or "")
        if not test_id or test_id in tests:
            continue
        seen = int((r["attrs"] or {}).get("seen_count") or 0)
        out.append(Finding(
            "flaky_test", (test_id,), f"test:{test_id}", f"Fix or quarantine {short(test_id)[:60]}",
            f"The same failure of {test_id} came back {seen} times.",
            clamp(30 + 35 * log_scale(seen, 30)), 1, [ev("kg_node", r["id"], "learned")],
            {"seen_count": seen, "error": str(detail.get("error_signature") or "")[:120]},
            r["last_seen"], severity="low"))
    return sorted(out, key=lambda f: -f.impact)[:15]


# ── 8. slow stages ──────────────────────────────────────────────────────────────────────────

#: What a stage should take (minutes, wall clock). A stage whose median over ≥ 5 builds is
#: 1.5× its target is slow (design 9.5 min and repair review 17 min were real, 2026-11-05).
STAGE_TARGET_MIN = {"design": 5.0, "spec": 4.0, "plan": 4.0, "review_plan": 4.0,
                    "write_tests": 8.0, "implement": 20.0, "review_code": 6.0,
                    "repair_review": 6.0, "security_check": 4.0, "consistency_check": 3.0,
                    "check_tests": 6.0}
DEFAULT_STAGE_TARGET = 8.0


@detector("slow_stage", title="Stage timer", kind="process", shape="ticket", source="build",
          evidence_source="stage", squad=("engineering_captain", "software_engineer"))
def slow_stage(connection: Connection, project_id: str, *, since: datetime, now: datetime,
               scale: float) -> list[Finding]:
    """Per pipeline stage, the median wall time over recent builds against its target."""
    times: dict[str, dict[str, float]] = defaultdict(dict)   # stage -> run -> minutes
    last: dict[str, datetime] = {}
    for r in connection.execute(text(
            "SELECT COALESCE(data->>'run_id', entity_id) AS run, data->'stages' AS stages, "
            "created_at FROM events WHERE project_id = :p AND event_type = "
            "'code_task.build_profile' AND created_at > :s ORDER BY created_at DESC LIMIT 1000"),
            {"p": project_id, "s": now - timedelta(days=30)}).mappings():
        for stage, body in (r["stages"] or {}).items():
            minutes = (body or {}).get("wall_min")
            if isinstance(minutes, (int, float)) and minutes > 0:
                times[stage].setdefault(r["run"], float(minutes))
                last[stage] = max(last.get(stage, r["created_at"]), r["created_at"])
    for r in connection.execute(text(
            "SELECT id, created_at, finalize_ctx->'stage_timings' AS t FROM code_task_runs "
            "WHERE project_id = :p AND created_at > :s AND jsonb_typeof(finalize_ctx->"
            "'stage_timings') = 'array' ORDER BY created_at DESC LIMIT 1000"),
            {"p": project_id, "s": now - timedelta(days=30)}).mappings():
        per: dict[str, float] = defaultdict(float)
        for t in r["t"] or []:
            if isinstance(t, dict) and t.get("stage") and isinstance(t.get("total_ms"),
                                                                     (int, float)):
                per[str(t["stage"])] += float(t["total_ms"]) / 60000
        for stage, minutes in per.items():
            if minutes > 0 and r["id"] not in times[stage]:
                times[stage][r["id"]] = minutes
                last[stage] = max(last.get(stage, r["created_at"]), r["created_at"])
    out: list[Finding] = []
    for stage, runs in times.items():
        if len(runs) < 5:
            continue
        med = median(runs.values())
        target = STAGE_TARGET_MIN.get(stage, DEFAULT_STAGE_TARGET)
        if med < target * 1.5 * scale:
            continue
        hours_week = (med - target) * len(runs) / 60 * 7 / 30
        slowest = sorted(runs.items(), key=lambda kv: -kv[1])[:6]
        out.append(Finding(
            "slow_stage", (stage,), f"stage:{stage}", f"Speed up the {stage.replace('_', ' ')} "
            "stage", f"The {stage.replace('_', ' ')} stage takes {med:.1f} min at the median "
            f"over {plural(len(runs), 'build')}, against a {target:.0f} min target.",
            clamp(30 + 40 * log_scale(hours_week, 40)), 3,
            [ev("code_task", run, f"{m:.1f} min") for run, m in slowest],
            {"median_min": round(med, 1), "target_min": target, "builds": len(runs),
             "hours_per_week": round(hours_week, 1)}, last.get(stage),
            severity="medium"))
    return sorted(out, key=lambda f: -f.impact)[:10]


# ── 9. user friction ────────────────────────────────────────────────────────────────────────

FRICTION_EVENTS = {
    "code_task.steered": "steer", "code_task.stop_requested": "stop",
    "code_task.cancelled": "cancel", "code_task.gate_overridden": "waive",
    "quality_gate.waived": "waive", "quality_gate.overridden": "waive",
    "workflow.step_retried": "retry", "workflow.step_skipped": "skip",
    "workflow.run_cancelled": "cancel", "workflow.rerun_started": "retry",
    "decision.rejected": "reject", "decision.revision_requested": "revise",
    "ambient.decision_overridden": "override",
}
_NOT_A_PERSON = ("boss", "system", "worker", "scheduler", "shepherd", "agent:", "policy:",
                 "squad:")


@detector("user_friction", title="Friction finder", kind="process", shape="ticket",
          source="person", evidence_source="friction", squad=("pm", "engineering_captain"))
def user_friction(connection: Connection, project_id: str, *, since: datetime,
                  now: datetime, scale: float) -> list[Finding]:
    """Where people intervene most: the same kind of steer/retry/waive/override at the same stage
    again and again is a step BOSS should do better (or a policy it should learn)."""
    rows = connection.execute(text(
        "SELECT event_type, actor, entity_type, entity_id, data, created_at FROM events "
        "WHERE project_id = :p AND event_type = ANY(:t) AND created_at > :s "
        "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "t": list(FRICTION_EVENTS), "s": now - timedelta(days=30),
         "n": ROW_CAP}).mappings().all()
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        actor = str(r["actor"] or "").lower()
        if not actor or actor.startswith(_NOT_A_PERSON) or actor in _NOT_A_PERSON:
            continue
        data = dict(r["data"] or {})
        gates = data.get("waived_gates")
        where = str(data.get("stage") or data.get("step_key") or data.get("gate")
                    or (gates[0] if isinstance(gates, list) and gates else "")
                    or r["entity_type"] or "run")
        key = (FRICTION_EVENTS[r["event_type"]], where)
        g = groups.setdefault(key, {"n": 0, "entities": [], "people": set(),
                                    "last": r["created_at"], "kind": r["entity_type"]})
        g["n"] += 1
        g["people"].add(actor)
        if r["entity_id"] and r["entity_id"] not in [e for e, _ in g["entities"]]:
            g["entities"].append((r["entity_id"], r["entity_type"]))
        g["last"] = max(g["last"], r["created_at"])
    out: list[Finding] = []
    kinds = {"code_task": "code_task", "workflow_run": "workflow_run", "decision": "decision",
             "step_run": "step_run", "agent_act": "act"}
    for (what, where), g in groups.items():
        if g["n"] < 5 * scale or len(g["entities"]) < 3:
            continue
        refs = [ev(kinds[t], e, what) for e, t in g["entities"][:8] if t in kinds]
        if not refs:
            refs = [ev("query", f"events {what} at {where}", f"{g['n']} times")]
        verb = {"steer": "steered", "stop": "stopped", "cancel": "cancelled",
                "waive": "waived gates on", "retry": "retried", "skip": "skipped",
                "reject": "rejected", "revise": "sent back", "override": "overrode"}[what]
        out.append(Finding(
            "user_friction", (what, where), f"friction:{what}:{where}",
            f"Fewer {what}s at {where.replace('_', ' ')}"[:120],
            f"People {verb} work at {where.replace('_', ' ')} {g['n']} times in 30 days "
            f"across {plural(len(g['entities']), 'run')}.",
            clamp(25 + 40 * log_scale(g["n"], 60) + 5 * min(len(g["people"]), 3)), 3, refs,
            {"count": g["n"], "runs": len(g["entities"]), "people": len(g["people"]),
             "action": what, "where": where}, g["last"], severity="medium"))
    return sorted(out, key=lambda f: -f.impact)[:10]
