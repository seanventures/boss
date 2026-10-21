"""The lifecycle graph: BOSS's own records as nodes linked to code (KNOWLEDGE_GRAPH_SPEC.md §3.3, §4; Phase 1).

BOSS writes the history other tools try to mine: what was asked, who decided, what changed, what
the reviewers found, what broke and what fixed it. `LifecycleGraph.sync` reads those records for
one project and writes them into the KnowledgeStore, idempotently. Re-running converges.

| Record | Node | Edges |
|---|---|---|
| `code_task_runs`, `kg_change_links` | `run`, `spec` (the run's instructions), `pull_request` | run/PR `implements` spec; PR `evidenced_by` run; PR `changed` file, and symbol (from `kg_change_links`) |
| `decisions` | `decision`, `person` | run/release `decided_by` decision; decision `approved_by` person |
| review findings (run state) | `review_finding` | finding `found_in` file; finding `evidenced_by` run |
| `change_set_prs` | `pull_request` | PR `evidenced_by` run |
| `incidents`, `learning_examples` | `incident`, `service`, root cause | incident `fired_on` service; incident `caused_by` cause; cause `about` file/symbol (from its evidence) |
| `remediations` | | incident `fixed_by` PR and run |
| `app_alert_rules`, `app_alerts` | `alert_rule` | rule `fired_on` incident |
| `sre_runbooks`, `sre_runbook_runs` | `runbook` | incident `fixed_by` runbook |
| release cards (decisions), `stack_deployments` | `release` | release `decided_by` decision; release `shipped` run/PR |

Code nodes are only linked when the code graph already holds them. Their keys are those of
`code_index` (`owner/repo:path`), so a sync after indexing joins the two graphs. Text is excerpted,
never copied whole; the records stay the source of truth.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..core import now_utc
from .code_index import file_key
from .store import PostgresKnowledgeStore, Scope

SOURCE = "lifecycle"
EXCERPT = 400


def _excerpt(value: Any) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str) if value is not None else ""
    return s[:EXCERPT]


def _obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def pr_key(full_name: str, number: Any) -> str:
    return f"pull_request:{full_name}#{number}"


def _findings(state: dict, finalize: dict) -> Iterable[dict]:
    for f in state.get("deferred_findings") or []:
        if isinstance(f, dict):
            yield f
    for rnd in finalize.get("review_rounds") or []:
        for f in (rnd.get("findings") if isinstance(rnd, dict) else None) or []:
            if isinstance(f, dict):
                yield f


class LifecycleGraph:
    def __init__(self, store: PostgresKnowledgeStore | None = None):
        self.store = store or PostgresKnowledgeStore()

    def sync(self, c: Connection, scope: Scope) -> dict[str, int]:
        """Project the project's lifecycle records into the graph. Idempotent."""
        self.store.require_scope(c, scope)
        p = scope.project_id
        nodes: dict[tuple[str, str], dict] = {}
        edges: list[tuple[tuple[str, str], tuple[str, str], str, dict]] = []

        def node(kind: str, key: str, name: str, attrs: dict | None = None) -> tuple[str, str]:
            nodes.setdefault((kind, key), {"kind": kind, "key": key, "name": name[:200] or key, "attrs": attrs or {}})
            return (kind, key)

        def edge(src, dst, kind, attrs=None):
            edges.append((src, dst, kind, attrs or {}))

        existing_code = self._code_nodes(c, scope)
        runs: dict[str, dict] = {}

        # Runs, specs, PRs, changed files, review findings ---------------------------------------
        for r in c.execute(text(
                "SELECT id, task_id, status, title, spec, state, finalize_ctx, pull_request_url, pull_request_number, "
                "decision_id, created_at FROM code_task_runs WHERE project_id = :p"), {"p": p}).mappings():
            spec, state, fin = _obj(r["spec"]), _obj(r["state"]), _obj(r["finalize_ctx"])
            full_name = "/".join(x for x in (spec.get("owner"), spec.get("repo")) if x)
            run = node("run", f"run:{r['id']}", r["title"] or r["id"],
                       {"status": r["status"], "repo": full_name, "created_at": str(r["created_at"])})
            runs[r["id"]] = {"node": run, "repo": full_name, "pr": None}
            if spec.get("instructions"):
                sp = node("spec", f"spec:{r['id']}", r["title"] or "spec",
                          {"instructions": _excerpt(spec.get("instructions")),
                           "acceptance_criteria": (spec.get("acceptance_criteria") or [])[:20]})
                edge(run, sp, "implements")
            else:
                sp = None
            if r["pull_request_number"] and full_name:
                pr = node("pull_request", pr_key(full_name, r["pull_request_number"]),
                          f"{full_name}#{r['pull_request_number']}",
                          {"url": r["pull_request_url"], "repo": full_name})
                runs[r["id"]]["pr"] = pr
                edge(pr, run, "evidenced_by")
                if sp:
                    edge(pr, sp, "implements")
                for path in (state.get("paths") or []):
                    fk = file_key(full_name, path)
                    if fk in existing_code:
                        edge(pr, ("file", fk), "changed")
            if r["decision_id"]:
                edge(run, ("decision", f"decision:{r['decision_id']}"), "decided_by")
            for i, f in enumerate(_findings(state, fin)):
                fnode = node("review_finding", f"review_finding:{r['id']}:{i}", _excerpt(f.get("text") or f.get("message"))[:120],
                             {"severity": f.get("severity"), "path": f.get("path"), "line": f.get("line"),
                              "text": _excerpt(f.get("text") or f.get("message")), "stage": f.get("stage")})
                edge(fnode, run, "evidenced_by")
                if f.get("path") and full_name and file_key(full_name, f["path"]) in existing_code:
                    edge(fnode, ("file", file_key(full_name, f["path"])), "found_in", {"line": f.get("line")})

        # PR -> changed symbols, from their own durable table (never from the versioned run record).
        for r in self._rows(c, "SELECT repo, pr_number, symbol_key FROM kg_change_links WHERE project_id = :p", p):
            if r["symbol_key"] in existing_code:
                pr = node("pull_request", pr_key(r["repo"], r["pr_number"]), f"{r['repo']}#{r['pr_number']}",
                          {"repo": r["repo"]})
                edge(pr, ("symbol", r["symbol_key"]), "changed")

        # PRs from change sets --------------------------------------------------------------------
        for r in self._rows(c, "SELECT cp.full_name, cp.pr_number, cp.pr_url, cp.head_sha, cp.merged_sha, "
                               "cp.code_task_run_id FROM change_set_prs cp JOIN repo_change_sets cs "
                               "ON cs.id = cp.change_set_id WHERE cs.project_id = :p", p):
            if not r["pr_number"]:
                continue
            pr = node("pull_request", pr_key(r["full_name"], r["pr_number"]), f"{r['full_name']}#{r['pr_number']}",
                      {"url": r["pr_url"], "repo": r["full_name"], "head_sha": r["head_sha"], "merged_sha": r["merged_sha"]})
            if r["code_task_run_id"] in runs:
                edge(pr, runs[r["code_task_run_id"]]["node"], "evidenced_by")

        # Decisions and the people who made them --------------------------------------------------
        for r in self._rows(c, "SELECT id, question, status, risk_tier, approved_by, resolution_note, recommendation, "
                               "updated_at FROM decisions WHERE project_id = :p", p):
            rec = _obj(r["recommendation"])
            d = node("decision", f"decision:{r['id']}", _excerpt(r["question"])[:200] or r["id"],
                     {"status": r["status"], "risk_tier": r["risk_tier"], "reason": _excerpt(r["resolution_note"]),
                      "approved_by": r["approved_by"], "at": str(r["updated_at"])})
            if r["approved_by"]:
                edge(d, node("person", f"person:{r['approved_by']}", r["approved_by"]), "approved_by")
            card = rec.get("release_commits")
            if card:
                rel = node("release", f"release:{r['id']}", f"release {str(_obj(card).get('head_sha') or '')[:12]}",
                           {"head_sha": _obj(card).get("head_sha")})
                edge(rel, d, "decided_by")
                run = runs.get(rec.get("run_id") or "")
                if run:
                    edge(rel, run["pr"] or run["node"], "shipped")

        # Incidents, causes, fixes, alerts, runbooks ----------------------------------------------
        incidents = {}
        for r in self._rows(c, "SELECT id, title, service, severity, status, first_seen FROM incidents WHERE project_id = :p", p):
            inc = node("incident", f"incident:{r['id']}", r["title"],
                       {"severity": r["severity"], "status": r["status"], "first_seen": str(r["first_seen"])})
            incidents[r["id"]] = inc
            if r["service"]:
                edge(inc, node("service", r["service"], r["service"]), "fired_on")
        causes = self._causes(c, scope)
        for r in self._rows(c, "SELECT ref, payload FROM learning_examples WHERE project_id = :p "
                               "AND kind = 'incident_feedback' AND label = 'real'", p):
            cause = _obj(r["payload"]).get("cause") or {}
            inc = incidents.get(r["ref"])
            if inc and cause.get("key"):
                ck = (cause.get("kind") or "root_cause", cause["key"])
                if ck in causes:
                    edge(inc, ck, "caused_by")
        for (kind, key), evidence in causes.items():
            for target in self._evidence_targets(evidence, existing_code):
                edge((kind, key), target, "about")
        for r in self._rows(c, "SELECT incident_id, code_task_run_id, pull_request_url FROM remediations WHERE project_id = :p", p):
            inc = incidents.get(r["incident_id"])
            run = runs.get(r["code_task_run_id"] or "")
            if inc and run:
                edge(inc, run["pr"] or run["node"], "fixed_by")
        for r in self._rows(c, "SELECT DISTINCT ar.id, ar.alert, ar.kind, ar.expr, a.incident_id FROM app_alert_rules ar "
                               "LEFT JOIN app_alerts a ON a.rule_id = ar.id WHERE ar.project_id = :p", p):
            rule = node("alert_rule", f"alert_rule:{r['id']}", r["alert"] or r["id"],
                        {"kind": r["kind"], "expr": _excerpt(r["expr"])})
            if r["incident_id"] in incidents:
                edge(rule, incidents[r["incident_id"]], "fired_on")
        for r in self._rows(c, "SELECT rb.id, rb.name, rr.incident_id FROM sre_runbooks rb LEFT JOIN sre_runbook_runs rr "
                               "ON rr.runbook_id = rb.id WHERE rb.project_id = :p", p):
            rb = node("runbook", f"runbook:{r['id']}", r["name"] or r["id"])
            if r["incident_id"] in incidents:
                edge(incidents[r["incident_id"]], rb, "fixed_by")
        for r in self._rows(c, "SELECT id, commit_sha, decision_id FROM stack_deployments WHERE project_id = :p", p):
            rel = node("release", f"release:deploy:{r['id']}", f"deploy {str(r['commit_sha'] or '')[:12]}",
                       {"commit_sha": r["commit_sha"]})
            if r["decision_id"]:
                edge(rel, ("decision", f"decision:{r['decision_id']}"), "decided_by")

        # Write ------------------------------------------------------------------------------------
        seen = now_utc()
        # Services belong to the SRE topology graph: link to them, never overwrite their attributes.
        shared = self._ids(c, scope, [k for k in nodes if k[0] == "service"])
        node_rows = [n for k, n in nodes.items() if k not in existing_code and k not in shared]
        ids = self.store.upsert_nodes_bulk(c, scope, node_rows, seen_at=seen, source=SOURCE, layer="deterministic")
        ids.update(existing_code)
        # Ends written before this sync (root causes, services from the SRE graph, decisions).
        for k, v in self._ids(c, scope, [s for s, *_ in edges] + [d for _, d, *_ in edges]).items():
            ids.setdefault(k, v)
        rows = [{"src": ids[s], "dst": ids[d], "kind": kind, "attrs": attrs} for s, d, kind, attrs in edges
                if s in ids and d in ids]
        written = self.store.upsert_edges_bulk(c, scope, rows, seen_at=seen, source=SOURCE)
        return {"nodes": len(node_rows), "edges": written, "runs": len(runs), "incidents": len(incidents)}

    # helpers ---------------------------------------------------------------------------------------
    @staticmethod
    def _rows(c: Connection, sql: str, project_id: str) -> list[dict]:
        """Records from a table this install may not have yet (older schema): empty, not an error."""
        with c.begin_nested() as sp:
            try:
                return [dict(r) for r in c.execute(text(sql), {"p": project_id}).mappings()]
            except Exception:  # noqa: BLE001 — a missing table means no records of that kind
                sp.rollback()
                return []

    @staticmethod
    def _code_nodes(c: Connection, scope: Scope) -> dict[tuple[str, str], str]:
        rows = c.execute(text("SELECT kind, node_key, id FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL "
                              "AND kind IN ('file', 'symbol')"), {"p": scope.project_id})
        found: dict = {}
        for r in rows:
            found[(r.kind, r.node_key)] = r.id
            found[r.node_key] = r.id
        return found

    @staticmethod
    def _causes(c: Connection, scope: Scope) -> dict[tuple[str, str], dict]:
        rows = c.execute(text("SELECT n.kind, n.node_key, n.attrs FROM kg_nodes n JOIN kg_edges e ON e.dst_id = n.id "
                              "AND e.kind = 'caused_by' WHERE n.project_id = :p"), {"p": scope.project_id})
        return {(r.kind, r.node_key): _obj(r.attrs).get("evidence") or {} for r in rows}

    @staticmethod
    def _evidence_targets(evidence: dict, existing: dict) -> Iterable[tuple[str, str]]:
        repo = evidence.get("repo")
        for key in ("symbol", "symbol_key"):
            if evidence.get(key) in existing:
                yield ("symbol", evidence[key])
        for key in ("path", "file"):
            value = evidence.get(key)
            if value and repo and file_key(repo, value) in existing:
                yield ("file", file_key(repo, value))

    @staticmethod
    def _ids(c: Connection, scope: Scope, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        wanted = sorted({k for _, k in keys})
        if not wanted:
            return {}
        rows = c.execute(text("SELECT kind, node_key, id FROM kg_nodes WHERE project_id = :p AND node_key = ANY(:k) "
                              "AND valid_to IS NULL"), {"p": scope.project_id, "k": wanted})
        return {(r.kind, r.node_key): r.id for r in rows}
