"""Repo & knowledge tab (contract §4, screen 06): the repositories with their access strip,
the dossier Discovery wrote (BOSSAPP-909), a knowledge-graph snippet centred on the project's
hottest entity, the docs and decisions, and what agents LEARNED on the job.

Reads project_dossiers (ambient/dossier.py), kg_repo_index (how far the code is mapped),
kg_nodes / kg_edges (the snippet; `rules` and learned counts), meeting_runs (PRDs), decided
plan/architecture approvals, the newest build's test run (code_task_runs.state) and the learning
layer: kg_nodes written by executed tasks (`source = 'learning'`: fixes, workarounds, known test
failures), the run that `produced` each, who `proposed_by` it, and kg_usage (was it reused by a
later run). Everything the screen shows has its source here; nothing is invented.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

import re

from .common import age, agents, as_json, face, href, iso, pr_number, project_href, repos, short_day, words

GRAPH_NODES = 12


def _stack(facts: dict[str, Any]) -> list[str]:
    stack = facts.get("stack") or {}
    out = [w for w in (stack.get("language"), stack.get("framework"), stack.get("build")) if w]
    for lang in facts.get("languages") or []:
        name = lang.get("language") if isinstance(lang, dict) else None
        if name and name not in out:
            out.append(name)
    return out[:6]


def commands(facts: dict[str, Any], stack: Optional[dict[str, Any]] = None) -> dict[str, Optional[str]]:
    """{build, test, run}: the first command the repository declares for each (the dossier has
    no run command; the stack's usual one is said only when the build tool names it)."""
    cmds = facts.get("commands") or {}
    build = (cmds.get("build") or [None])[0]
    test = (cmds.get("test") or [None])[0]
    run = None
    tool = str(((stack or facts.get("stack") or {}).get("build")) or "")
    if tool in ("npm", "pnpm", "Yarn"):
        run = {"npm": "npm start", "pnpm": "pnpm start", "Yarn": "yarn start"}[tool]
    elif tool == "Maven":
        run = "mvn spring-boot:run" if (facts.get("stack") or {}).get("framework") == "Spring" else None
    elif tool == "Gradle":
        run = "./gradlew bootRun" if (facts.get("stack") or {}).get("framework") == "Spring" else None
    install = _install(tool, facts)
    # What the repository itself says (its manifests, its README) wins over the tool's usual.
    install = (cmds.get("install") or [None])[0] or install
    run = (cmds.get("run") or [None])[0] or run
    return {"install": install, "test": test, "build": build, "run": run,
            "lint": (cmds.get("lint") or [None])[0]}


def _install(tool: str, facts: dict[str, Any]) -> Optional[str]:
    """How to install the dependencies, from the build tool the repository uses."""
    if tool in ("npm", "pnpm", "Yarn"):
        return {"npm": "npm install", "pnpm": "pnpm install", "Yarn": "yarn install"}[tool]
    if tool == "Maven":
        return "mvn install -DskipTests"
    if tool == "Gradle":
        return "./gradlew assemble"
    lang = str(((facts.get("stack") or {}).get("language")) or "")
    if lang == "Python":
        return "pip install -r requirements.txt"
    if lang == "Go":
        return "go mod download"
    if lang == "Rust":
        return "cargo fetch"
    return None


def _hotspots(facts: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for h in (facts.get("hotspots") or [])[:6]:
        path = str(h.get("file") or "")
        if not path:
            continue
        stem = PurePosixPath(path).stem.replace("_", " ").replace("-", " ")
        why = f"{h.get('churn', 0)} lines changed in 180 days, {h.get('lines', 0)} lines"
        if not h.get("has_tests"):
            why += ", no tests"
        out.append({"path": path, "label": words(stem.title()), "why": why,
                    "heat": int(h.get("churn") or 0) or None,
                    "has_tests": bool(h.get("has_tests"))})
    return out



def _code_map(facts: dict[str, Any]) -> dict[str, Any]:
    """The code map bar: the top-level directories by files (4 + the rest), and the total."""
    dirs = [d for d in facts.get("structure") or [] if isinstance(d, dict) and d.get("dir")]
    total = int(facts.get("files") or sum(int(d.get("files") or 0) for d in dirs))
    top = [{"dir": str(d["dir"]) if d["dir"] != "." else "root", "files": int(d.get("files") or 0)}
           for d in dirs[:4]]
    rest = total - sum(d["files"] for d in top)
    if rest > 0 and len(dirs) > 4:
        top.append({"dir": "other", "files": rest})
    return {"parts": top, "files": total}


def parse_test_counts(output: Any) -> Optional[dict[str, int]]:
    """{passed, total} from a test run's output (test_runs.parse_counts)."""
    from .test_runs import parse_counts

    return parse_counts(output)


def dossier(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    from ..ambient import dossier as dossier_service
    from . import captain as captain_service

    rows = dossier_service.get(connection, project_id)
    if not rows:
        return None
    row = rows[0]
    facts = as_json(row.get("facts"), {}) or {}
    owners = facts.get("owners") or {}
    cap = captain_service.view(connection, project_id, with_last_said=False) or {}
    built = row.get("built_at")
    try:
        from datetime import datetime as _dt

        built_dt = _dt.fromisoformat(str(built)) if isinstance(built, str) else built
    except ValueError:
        built_dt = None
    from .test_runs import last_full_run

    run = last_full_run(connection, project_id)
    return {"repo": row.get("repo"), "stack": _stack(facts), "commands": commands(facts),
            # THE TEST BASELINE says where its numbers came from ("the last full run, a blocked
            # build, 9h ago") and, when one setup error explains the failures, which one.
            "tests": {"files": int((facts.get("tests") or {}).get("files") or 0),
                      **({"passed": run["passed"], "total": run["total"],
                          "failing": run["failing"], "from": run["from"],
                          "source": run["label"], "ago": run["ago"],
                          "cause": run.get("cause"),
                          "href": run["href"]} if run else {})},
            "files": facts.get("files"), "ci": facts.get("ci") or [],
            "code_map": _code_map(facts),
            "hotspots": _hotspots(facts),
            "todo_clusters": [{"dir": c.get("dir"), "count": c.get("count"),
                               "label": words(f"{c.get('count')} TODOs {c.get('dir')}")}
                              for c in (facts.get("todos") or {}).get("clusters") or []][:5],
            "owners": ([{"name": p.get("name"), "commits": p.get("commits")}
                        for p in owners.get("top_committers") or []][:5]),
            "codeowners": owners.get("codeowners"),
            "highlights": row.get("highlights") or [], "read_from": row.get("read_from"),
            "by": cap.get("name") or "Helm",
            "built_at": built, "built_ago": (f"{age(built_dt)} ago" if built_dt else None),
            "version": row.get("version"),
            "href": project_href(project_id, tab="knowledge", drawer="dossier")}


# ── the knowledge-graph snippet ─────────────────────────────────────────────────────────────

#: What a node is drawn as: a business rule, something agents learned, code, or data.
LEARNED_SOURCE = "learning"
_GROUP_ORDER = ("rule", "learned", "table", "endpoint", "file", "module", "config_key", "symbol")
NEIGHBOURS = 7


def _group(kind: str, layer: Optional[str], source: Optional[str]) -> str:
    if layer == "rules" or kind == "rule":
        return "rule"
    if source == LEARNED_SOURCE and kind in LEARNED_KINDS:
        return "learned"
    if kind in ("table", "database_table", "endpoint"):
        return "data" if kind != "endpoint" else "code"
    return "code" if layer == "code" else "entity"


def _short(label: str, limit: int = 18) -> str:
    label = " ".join(str(label or "").split())
    return label if len(label) <= limit else label[: limit - 1].rstrip() + "…"


def node_label(kind: str, name: Any, path: Any, attrs: Any = None) -> str:
    """A node's few words on the diagram: a file's name, a table, an endpoint, a rule's title."""
    from .titles import shorten

    name_ = str(name or "")
    if kind in ("file", "module"):
        base = str(path or name_).rstrip("/").rsplit("/", 1)[-1] or name_
        if len(base) > 18 and "." in base:
            base = base.rsplit(".", 1)[0]  # TicketDetailPage.jsx → TicketDetailPage
        return _short(base)
    if kind == "symbol" and "." in name_:
        return _short(name_.rsplit(".", 1)[-1])  # PgUserStore.signIn → signIn
    if kind in LEARNED_KINDS:
        return _short(learned_label(kind, name_, as_json(attrs, {}) or {}), 22)
    if kind == "rule":
        return _short(shorten(name_, 3) or name_)
    return _short(name_)


def _hub(connection: Connection, project_id: str, hot_path: Optional[str]) -> Optional[dict[str, Any]]:
    """THE HOTTEST ENTITY: the dossier's most-changed file when the graph has it, else the file
    BOSS's own pull requests changed most (`changed` edges), else the most connected table,
    endpoint or rule."""
    if not hot_path:
        hot_path = connection.execute(text(
            "SELECT d.path FROM kg_edges e JOIN kg_nodes d ON d.id = e.dst_id "
            "WHERE e.project_id = :p AND e.kind = 'changed' AND e.valid_to IS NULL "
            "AND d.path IS NOT NULL GROUP BY d.path ORDER BY count(*) DESC, d.path LIMIT 1"),
            {"p": project_id}).scalar()
    if hot_path:
        row = connection.execute(text(
            "SELECT id, kind, name, path, knowledge_layer, source, attrs FROM kg_nodes "
            "WHERE project_id = :p AND kind = 'file' AND path = :path AND valid_to IS NULL "
            "LIMIT 1"), {"p": project_id, "path": hot_path}).mappings().first()
        if row:
            return dict(row)
    cands = [dict(r) for r in connection.execute(text(
        "(SELECT id, kind, name, path, knowledge_layer, source, attrs FROM kg_nodes "
        " WHERE project_id = :p AND kind = 'table' AND valid_to IS NULL LIMIT 30) UNION ALL "
        "(SELECT id, kind, name, path, knowledge_layer, source, attrs FROM kg_nodes "
        " WHERE project_id = :p AND kind = 'rule' AND valid_to IS NULL LIMIT 30) UNION ALL "
        "(SELECT id, kind, name, path, knowledge_layer, source, attrs FROM kg_nodes "
        " WHERE project_id = :p AND kind = 'endpoint' AND valid_to IS NULL LIMIT 30)"),
        {"p": project_id}).mappings().all()]
    if not cands:
        return None
    ids = [c["id"] for c in cands]
    degree = {str(r[0]): int(r[1]) for r in connection.execute(text(
        "SELECT id, count(*) FROM (SELECT src_id AS id FROM kg_edges WHERE src_id = ANY(:ids) "
        "AND valid_to IS NULL UNION ALL SELECT dst_id FROM kg_edges WHERE dst_id = ANY(:ids) "
        "AND valid_to IS NULL) x GROUP BY id"), {"ids": ids}).all()}
    return max(cands, key=lambda c: (degree.get(str(c["id"]), 0), c["kind"] == "rule"))


def graph(connection: Connection, project_id: str, limit: int = NEIGHBOURS,
          hot_path: Optional[str] = None) -> dict[str, Any]:
    """A snippet of the project's graph CENTRED on its hottest entity (screen 06): the hub, up
    to `limit` neighbours of mixed kinds (rules and learned facts first), the edges among them,
    and the counts the chips show (nodes, rules, learned)."""
    hub = _hub(connection, project_id, hot_path)
    nodes: list[dict[str, Any]] = []
    if hub:
        near = [dict(r) for r in connection.execute(text(
            "SELECT n.id, n.kind, n.name, n.path, n.knowledge_layer, n.source, n.attrs "
            "FROM kg_edges e JOIN kg_nodes n ON n.id = CASE WHEN e.src_id = :h THEN e.dst_id "
            "ELSE e.src_id END WHERE (e.src_id = :h OR e.dst_id = :h) AND e.valid_to IS NULL "
            "AND n.valid_to IS NULL AND n.id <> :h LIMIT 300"), {"h": hub["id"]}).mappings().all()]
        buckets: dict[str, list[dict[str, Any]]] = {}
        seen: set[str] = set()
        for n in near:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            if n["kind"] in ("run", "agent", "pull_request", "person", "decision"):
                continue  # the lifecycle (who ran what) is the graph's own view, not the snippet
            key = "learned" if n["source"] == LEARNED_SOURCE else (
                "rule" if n["knowledge_layer"] == "rules" else n["kind"])
            buckets.setdefault(key, []).append(n)
        order = [k for k in _GROUP_ORDER if k in buckets] + sorted(
            k for k in buckets if k not in _GROUP_ORDER)
        picked: list[dict[str, Any]] = []
        # Round-robin across kinds so the snippet tells a story (a rule, a lesson, data, code);
        # symbols only fill what is left.
        rich = [k for k in order if k != "symbol"]

        def fill(kinds: list[str]) -> None:
            while len(picked) < limit and any(buckets.get(k) for k in kinds):
                for k in kinds:
                    if buckets.get(k) and len(picked) < limit:
                        picked.append(buckets[k].pop(0))

        fill(rich)
        if len(picked) < limit and picked:
            # A sparse hub: one more hop, from its neighbours (Customer → Org on screen 06).
            have = {hub["id"], *(n["id"] for n in picked), *(n["id"] for b in buckets.values()
                                                              for n in b)}
            hop = [dict(r) for r in connection.execute(text(
                "SELECT DISTINCT ON (n.id) n.id, n.kind, n.name, n.path, n.knowledge_layer, "
                "n.source, n.attrs FROM kg_edges e JOIN kg_nodes n ON n.id = CASE WHEN "
                "e.src_id = ANY(:ids) THEN e.dst_id ELSE e.src_id END WHERE (e.src_id = ANY(:ids) "
                "OR e.dst_id = ANY(:ids)) AND e.valid_to IS NULL AND n.valid_to IS NULL "
                "AND n.kind NOT IN ('run', 'agent', 'pull_request', 'person', 'decision', 'symbol') "
                "LIMIT 60"), {"ids": [n["id"] for n in picked]}).mappings().all()]
            # Rules and lessons first: they are what the snippet is for.
            hop.sort(key=lambda n: (0 if n["knowledge_layer"] == "rules" else
                                    1 if n["source"] == LEARNED_SOURCE else
                                    2 if n["kind"] in ("table", "endpoint") else 3,
                                    str(n["name"])))
            for n in hop:
                if len(picked) >= limit:
                    break
                if n["id"] not in have:
                    have.add(n["id"])
                    picked.append(n)
        fill(["symbol"])
        nodes = [hub, *picked]
    ids = [n["id"] for n in nodes]
    edges = [dict(r) for r in connection.execute(text(
        "SELECT src_id, dst_id, kind FROM kg_edges WHERE project_id = :p AND valid_to IS NULL "
        "AND src_id = ANY(:ids) AND dst_id = ANY(:ids) LIMIT 60"),
        {"p": project_id, "ids": ids}).mappings().all()] if ids else []
    total = int(connection.execute(text(
        "SELECT COALESCE(sum(nodes), 0) FROM kg_repo_index WHERE project_id = :p"),
        {"p": project_id}).scalar() or 0)
    counted = connection.execute(text(
        "SELECT count(*) FILTER (WHERE knowledge_layer = 'rules' OR kind = 'rule') AS rules, "
        "count(*) FILTER (WHERE source = :learning AND kind = ANY(:kinds)) AS learned, "
        "count(*) AS nodes FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL"),
        {"p": project_id, "learning": LEARNED_SOURCE,
         "kinds": list(LEARNED_KINDS)}).mappings().first()
    rules_ids = {n["id"] for n in nodes if _group(n["kind"], n["knowledge_layer"],
                                                   n["source"]) == "rule"}
    return {"nodes": [{"id": n["id"], "kind": n["kind"],
                       "group": "hub" if i == 0 else _group(n["kind"], n["knowledge_layer"],
                                                            n["source"]),
                       "label": node_label(n["kind"], n["name"], n["path"], n.get("attrs")),
                       "detail": n["path"] or n["name"]} for i, n in enumerate(nodes)],
            # A RULE EDGE (rule → rule: "breach → escalate") carries its words and is drawn
            # apart; every other edge is a plain line.
            "edges": [{"from": e["src_id"], "to": e["dst_id"], "kind": e["kind"],
                       "label": (str(e["kind"]).replace("_", " ")
                                 if e["src_id"] in rules_ids and e["dst_id"] in rules_ids
                                 else "")} for e in edges],
            "hub": nodes[0]["id"] if nodes else None,
            "total_nodes": total or int((counted or {}).get("nodes") or 0),
            "counts": {"nodes": total or int((counted or {}).get("nodes") or 0),
                       "rules": int((counted or {}).get("rules") or 0),
                       "learned": int((counted or {}).get("learned") or 0)},
            "href": href(project_id, area="knowledge")}


def mapping(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """How far each repository's code is mapped, from its index row (not a count of the graph)."""
    from ..start_project import mapping as mapped

    rows = connection.execute(text(
        "SELECT b.full_name, r.status, r.files, r.parsed, r.nodes, r.error, r.indexed_at "
        "FROM github_repository_bindings b LEFT JOIN kg_repo_index r ON r.binding_id = b.id "
        "AND r.project_id = b.project_id WHERE b.project_id = :p AND b.status = 'active' "
        "ORDER BY b.full_name"), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        item = None if r["status"] is None else {
            "status": r["status"], "files": r["files"], "parsed": r["parsed"],
            "graph": {"nodes": r["nodes"] or 0}, "error": r["error"]}
        out.append({"repo": r["full_name"], **mapped(item), "files": r["files"],
                    "parsed": r["parsed"], "indexed_at": iso(r["indexed_at"])})
    return out




# ── learned on the job ──────────────────────────────────────────────────────────────────────

#: The learning layer's kinds (memory/learning writes them while a task runs).
LEARNED_KINDS = ("fix", "workaround", "test_failure", "fact", "precedent", "resolved_pattern")
_REUSE = ("cited", "reused", "consulted")


_SHELL = re.compile(r"^`?(npm|npx|pnpm|yarn|node|python3?|pytest|go|cargo|mvn|gradle|\./gradlew|make|"
                    r"bash|sh|cat|cd|docker)\b")


def _cmd_head(command: Any, last: bool = False) -> str:
    """A shell command in two words: `npm install … && npm test 2>&1 | tail` → "npm test";
    `cat server/seedDemoTickets.mjs` → "cat seedDemoTickets.mjs"."""
    raw = str(command or "").strip().strip("`")
    parts = [p.strip() for p in raw.split("&&") if p.strip()]
    if not parts:
        return ""
    part = (parts[-1] if last else parts[0]).split("|", 1)[0]
    toks = []
    for tok in part.split():
        if tok.startswith(("-", "2>", ">", "'", '"')) or tok in ("2>&1",):
            if toks and toks[-1] in ("node", "python", "python3") and tok in ("-e", "-c"):
                toks.append(tok)
                break
            continue
        toks.append(tok.rsplit("/", 1)[-1] if "/" in tok else tok)
        if len(toks) >= 2 and toks[0] not in ("npm", "pnpm", "yarn", "make", "go", "cargo", "mvn"):
            break
        if len(toks) >= 3:
            break
    return " ".join(toks)


def learned_label(kind: str, name: str, attrs: dict[str, Any]) -> str:
    """A learned fact in ≤ 5 words, from its structured detail, never its raw first line."""
    from .titles import shorten

    detail = attrs.get("detail") if isinstance(attrs.get("detail"), dict) else {}
    if kind == "test_failure":
        test = str(detail.get("test") or "")
        if test and _SHELL.match(test):
            # The "test" is a command: the lesson is why it failed.
            why = str(detail.get("error_signature") or "")
            return shorten(why, 6) if why else f"{_cmd_head(test, last=True)} fails"
        if test:
            return shorten(re.sub(r"^P\d+:\s*", "", test), 5)
        msg = name.split(" fails:", 1)[1] if " fails:" in name else name
        return shorten(msg, 6) or "A failing test"
    if kind == "fix":
        m = re.match(r"What made (.+?) pass", name)
        cmd = _cmd_head(m.group(1) if m else detail.get("command"), last=True)
        return f"Make {cmd} pass" if cmd else (shorten(name, 5) or "A fix")
    if kind == "workaround":
        problem = str(detail.get("problem") or name)
        m = re.match(r"`([^`]+)`", problem)
        cmd = _cmd_head(m.group(1) if m else problem.split(" failed", 1)[0], last=True)
        return f"Workaround: {cmd}" if cmd else "A workaround"
    return shorten(name, 5) or "Learned"


def learned(connection: Connection, project_id: str, limit: int = 6) -> list[dict[str, Any]]:
    """WHAT AGENTS LEARNED on this project while running its tasks, newest first: each with the
    agent who proposed it, the run that taught it (its PR, else its short title), how many later
    runs reused it (kg_usage cited/reused/consulted, and `consulted` edges) and `used` / `new`."""
    from .titles import short_title

    rows = [dict(r) for r in connection.execute(text(
        "SELECT id, kind, name, attrs, created_by, first_seen FROM kg_nodes WHERE project_id = :p "
        "AND source = :learning AND valid_to IS NULL AND kind = ANY(:kinds) "
        "ORDER BY first_seen DESC LIMIT :n"),
        {"p": project_id, "learning": LEARNED_SOURCE, "kinds": list(LEARNED_KINDS),
         "n": limit}).mappings().all()]
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    producers: dict[str, dict[str, Any]] = {}
    for r in connection.execute(text(
            "SELECT e.dst_id AS fact, n.attrs->>'source_id' AS run_id, n.name FROM kg_edges e "
            "JOIN kg_nodes n ON n.id = e.src_id WHERE e.kind = 'produced' AND e.dst_id = ANY(:ids) "
            "AND n.kind = 'run' ORDER BY n.first_seen"), {"ids": ids}).mappings():
        producers.setdefault(str(r["fact"]), {"run_id": r["run_id"], "name": r["name"]})
    proposers = {str(r["fact"]): str(r["name"]) for r in connection.execute(text(
        "SELECT e.src_id AS fact, n.name FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id "
        "WHERE e.kind = 'proposed_by' AND e.src_id = ANY(:ids) AND n.kind = 'agent'"),
        {"ids": ids}).mappings()}
    reuse: dict[str, set[str]] = {}
    for r in connection.execute(text(
            "SELECT fact_id, COALESCE(code_task_run_id, workflow_run_id, run_id) AS run "
            "FROM kg_usage WHERE fact_id = ANY(:ids) AND use_kind = ANY(:kinds)"),
            {"ids": ids, "kinds": list(_REUSE)}).mappings():
        if r["run"]:
            reuse.setdefault(str(r["fact_id"]), set()).add(str(r["run"]))
    for r in connection.execute(text(
            "SELECT e.dst_id AS fact, n.attrs->>'source_id' AS run FROM kg_edges e "
            "JOIN kg_nodes n ON n.id = e.src_id WHERE e.kind = 'consulted' AND e.dst_id = ANY(:ids)"),
            {"ids": ids}).mappings():
        if r["run"]:
            reuse.setdefault(str(r["fact"]), set()).add(str(r["run"]))
    runs = [p["run_id"] for p in producers.values() if p.get("run_id")]
    builds = {str(r["id"]): dict(r) for r in connection.execute(text(
        "SELECT id, title, pull_request_number, pull_request_url FROM code_task_runs "
        "WHERE id = ANY(:ids)"), {"ids": runs}).mappings()} if runs else {}
    roster = agents(connection, project_id)
    by_name = {str(v["name"]).lower(): k for k, v in roster.items()}
    out = []
    for r in rows:
        attrs = as_json(r["attrs"], {}) or {}
        prod = producers.get(str(r["id"])) or {}
        build = builds.get(str(prod.get("run_id") or "")) or {}
        n = build.get("pull_request_number") or pr_number(build.get("pull_request_url"))
        taught = (f"PR #{n} build" if n else short_title(build.get("title") or prod.get("name"),
                                                          limit=4)) or "a run"
        again = {x for x in reuse.get(str(r["id"]), set()) if x != prod.get("run_id")}
        role = by_name.get(proposers.get(str(r["id"]), "").lower()) or r["created_by"]
        who = face(roster, role) if role else None
        out.append({"id": r["id"], "kind": r["kind"],
                    "label": learned_label(str(r["kind"]), str(r["name"] or ""), attrs),
                    "detail": str(r["name"] or ""),
                    "sub": f"{taught} · reused {len(again)}×" if again else taught,
                    "reused": len(again), "state": "used" if again else "new",
                    "agent": who, "at": iso(r["first_seen"]),
                    "href": href(project_id, area="knowledge", node=r["id"])})
    return out

def docs(connection: Connection, project_id: str, facts_docs: dict[str, Any],
         repo: Optional[str]) -> list[dict[str, Any]]:
    out = []
    base = f"https://github.com/{repo}/blob/HEAD/" if repo else None
    roster = agents(connection, project_id)
    for r in connection.execute(text(
            "SELECT id, COALESCE(title, directive) AS title, updated_at, role_keys, "
            "left(state->>'prd_markdown', 1500) AS prd FROM meeting_runs "
            "WHERE project_id = :p AND status = 'completed' AND state ? 'prd_markdown' "
            "ORDER BY updated_at DESC LIMIT 6"), {"p": project_id}).mappings().all():
        keys = as_json(r["role_keys"], []) or []
        lead = "product_manager" if "product_manager" in keys else (keys[0] if keys else None)
        pm = face(roster, lead) if lead else None
        approved = _prd_approved(connection, project_id, str(r["id"]))
        when = short_day(approved or r["updated_at"])
        sub = " · ".join(x for x in ((pm or {}).get("name"),
                                     f"approved {when}" if approved else when) if x)
        out.append({"label": f"PRD · {prd_title(r['prd'], r['title'])}",
                    "detail": _excerpt(r["prd"]) or r["title"],
                    "kind": "prd", "sub": sub, "agent": pm,
                    "at": iso(r["updated_at"]), "href": href(project_id, area="meetings",
                                                             meeting=r["id"])})
    if facts_docs.get("readme"):
        out.append({"label": "README", "kind": "readme", "detail": facts_docs["readme"],
                    "sub": "from the repo",
                    "external_url": (base + facts_docs["readme"]) if base else None,
                    "href": project_href(project_id, tab="knowledge", drawer="doc:readme")})
    for path in facts_docs.get("runbooks") or []:
        out.append({"label": doc_label(path),
                    "kind": "adr" if re.search(r"(^|/)(adr|decisions?)[-_/]", path, re.I)
                    else "runbook", "detail": path, "sub": f"from repo /{PurePosixPath(path).parent}",
                    "external_url": (base + path) if base else None,
                    "href": project_href(project_id, tab="knowledge", drawer=f"doc:{path}")})
    return out[:12]


def doc_label(path: str) -> str:
    """A repo doc's name: `docs/adr-001-postgres-queue.md` → "ADR-001 · Postgres queue"."""
    stem = PurePosixPath(path).stem
    m = re.match(r"(?i)^(adr|rfc)[-_ ]?(\d+)[-_ ]+(.+)$", stem)
    if m:
        rest = m.group(3).replace("-", " ").replace("_", " ").strip()
        return f"{m.group(1).upper()}-{m.group(2)} · {rest[:1].upper()}{rest[1:]}"
    words_ = stem.replace("-", " ").replace("_", " ").strip()
    return words(words_[:1].upper() + words_[1:], 4)


def _excerpt(markdown: Any, limit: int = 900) -> str:
    """A PRD's opening in plain words for its drawer: no heading marks, no bold."""
    lines = [re.sub(r"[*_`]{1,3}", "", ln).lstrip("# ").rstrip()
             for ln in str(markdown or "").splitlines()]
    text_ = "\n".join(ln for ln in lines if ln.strip())
    return text_[:limit].rstrip() + ("…" if len(text_) > limit else "")


def prd_title(markdown: Any, fallback: Any) -> str:
    """A PRD's own name: its first heading without "PRD:", cut to a short title by rule."""
    from .titles import shorten, short_title

    first = next((ln.strip("# ").strip() for ln in str(markdown or "").splitlines()
                  if ln.startswith("#")), "")
    first = re.sub(r"^PRD\s*[:·-]\s*", "", first, flags=re.I)
    got = (shorten(first, 4) if first else short_title(fallback, limit=4)) or "PRD"
    return re.sub(r"[\s+&/,;:—–-]+$", "", got) or "PRD"


def _prd_approved(connection: Connection, project_id: str, meeting_id: str) -> Any:
    """When a person approved the spec of the workflow run whose panel wrote this PRD."""
    return connection.execute(text(
        "SELECT d.approved_at FROM workflow_step_runs m JOIN workflow_step_runs s "
        "ON s.workflow_run_id = m.workflow_run_id AND s.step_id = 'spec_review' "
        "JOIN decisions d ON d.id = s.decision_id AND d.status IN ('approved', 'executed') "
        "WHERE m.project_id = :p AND m.step_id = 'meeting' AND m.output::text LIKE :m LIMIT 1"),
        {"p": project_id, "m": f"%{meeting_id}%"}).scalar()


def decisions(connection: Connection, project_id: str, limit: int = 3) -> list[dict[str, Any]]:
    """What people decided for this project, newest first: each environment's deploy policy
    ("Staging deploys on merge · Policy · you, Nov 2") and the answered plan / design approvals,
    named by the work they were about ("Plan · Visual refresh"), never "Approve the PRD?"."""
    from ..people_names import who
    from .health import env_label
    from .titles import short_title

    out = []
    for r in connection.execute(text(
            "SELECT id, environment, requires_approval, created_by, metadata, updated_at "
            "FROM deployment_environment_policies WHERE project_id = :p AND status = 'active' "
            "ORDER BY updated_at DESC LIMIT 3"), {"p": project_id}).mappings():
        meta = as_json(r["metadata"], {}) or {}
        by = meta.get("approval_set_by") or r["created_by"]
        at = meta.get("approval_set_at") or r["updated_at"]
        env = env_label(r["environment"])
        out.append({"label": f"{env} deploys {'need you' if r['requires_approval'] else 'on merge'}",
                    "detail": meta.get("note") or "", "kind": "policy", "at": iso(at),
                    "sub": "Policy · " + ", ".join(x for x in (who(by, connection) if by else None,
                                                                short_day(at)) if x),
                    "href": href(project_id, area="budget", tab="autonomy")})
    rows = connection.execute(text(
        "SELECT d.id, d.question, d.status, d.approved_by, d.approved_at, d.updated_at, "
        "d.recommendation->>'capability' AS cap, r.input->>'title' AS title, "
        "r.input->>'directive' AS directive FROM decisions d LEFT JOIN workflow_runs r "
        "ON r.id = d.recommendation->>'workflow_run_id' WHERE d.project_id = :p "
        "AND d.status IN ('approved', 'executed') AND "
        "(d.recommendation->>'capability' IN ('spec_review', 'architecture', 'meeting') OR "
        " d.recommendation->>'action' = 'review_architecture') "
        "ORDER BY COALESCE(d.approved_at, d.updated_at) DESC LIMIT :n"),
        {"p": project_id, "n": limit}).mappings().all()
    for r in rows:
        kind = "Plan" if r["cap"] in ("spec_review", "meeting") else "Design"
        name = short_title(r["title"], r["directive"], limit=4) if (r["title"] or r["directive"]) \
            else short_title(r["question"], limit=5)
        by = who(r["approved_by"], connection) if r["approved_by"] else None
        when = r["approved_at"] or r["updated_at"]
        out.append({"label": f"{kind} · {name}" if name else f"{kind} approved",
                    "detail": r["question"], "kind": "decision", "at": iso(when),
                    "sub": "Approved · " + ", ".join(x for x in (by, short_day(when)) if x),
                    "href": href(project_id, area="decisions", decision=r["id"])})
    return out[:limit]

def view(connection: Connection, project_id: str) -> dict[str, Any]:
    d = dossier(connection, project_id)
    facts_docs: dict[str, Any] = {}
    if d is not None:
        from ..ambient import dossier as dossier_service

        row = dossier_service.get(connection, project_id)[0]
        facts_docs = (as_json(row.get("facts"), {}) or {}).get("docs") or {}
    the_repos = repo_rows(connection, project_id)
    primary = the_repos[0]["full_name"] if the_repos else None
    hot = ((d or {}).get("hotspots") or [{}])[0].get("path") if d else None
    out = {"repos": the_repos, "mapping": mapping(connection, project_id), "dossier": d,
           "graph": graph(connection, project_id, hot_path=hot),
           "docs": docs(connection, project_id, facts_docs, primary),
           "decisions": decisions(connection, project_id),
           "learned": learned(connection, project_id),
           "build": build_action(connection, project_id)}
    if not the_repos:
        out["empty"] = {"label": "No repository yet",
                        "detail": "Add the project's repository and BOSS reads it: stack, "
                                  "commands, tests, owners and hot spots.",
                        "href": href(project_id, area="project", tab="settings",
                                     drawer="section:repos")}
    elif d is None:
        running = out["build"]["state"] == "running"
        out["empty"] = {"label": "Helm is reading the repository" if running else "No dossier yet",
                        "detail": ("Mapping the code, the commands and the tests; the dossier "
                                   "appears here when it's done." if running else
                                   "Helm reads the repository once: stack, commands, tests, "
                                   "hotspots. It takes about two minutes."),
                        "href": project_href(project_id, tab="onboarding")}
    return out


def build_action(connection: Connection, project_id: str) -> dict[str, Any]:
    """"Build dossier now": the onboarding pass (idempotent: a moving run is returned, never a
    second one), and how far the current one is."""
    from . import onboarding as onboarding_service

    ob = onboarding_service.view(connection, project_id)
    discover = next((s_ for s_ in ob.get("stages") or [] if s_.get("key") == "discover"), {})
    return {"label": "Build dossier now", "method": "POST",
            "href": f"/v1/projects/{project_id}/onboarding/start",
            "state": "running" if ob.get("state") == "running" else ob.get("state") or "not_started",
            "progress": discover.get("sub") or "",
            "nodes": (ob.get("discover") or {}).get("nodes") or []}


def _check_reason(check: Any) -> Optional[str]:
    """A failed access check's own reason (`error` / `message` / `reason`), in one line."""
    if not isinstance(check, dict) or check.get("ok") is not False:
        return None
    said = check.get("error") or check.get("message") or check.get("reason") or ""
    return " ".join(str(said).split())[:140] or "The last check failed"


def repo_rows(connection: Connection, project_id: str) -> list[dict[str, Any]]:
    """The repositories (§2 shape) plus what screen 06 says under each: the last push BOSS
    recorded (its merges), how BOSS reaches it (the GitHub connection), and whether it ships to
    a deploy target (the access strip's Staging node)."""
    out = repos(connection, project_id)
    if not out:
        return out
    via = {str(r["full_name"]).lower(): r for r in connection.execute(text(
        "SELECT b.full_name, c.connection_label, c.auth_method, c.account_login FROM "
        "github_repository_bindings b LEFT JOIN github_connections c ON c.id = b.connection_id "
        "WHERE b.project_id = :p AND b.status = 'active'"), {"p": project_id}).mappings()}
    pushed = connection.execute(text(
        "SELECT max(CAST(finalize_ctx->'merge'->>'at' AS timestamptz)) FROM code_task_runs "
        "WHERE project_id = :p AND COALESCE(finalize_ctx->'merge'->>'at', '') <> ''"),
        {"p": project_id}).scalar()
    staging = connection.execute(text(
        "SELECT environment FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        "ORDER BY (lower(environment) IN ('staging', 'stage')) DESC LIMIT 1"),
        {"p": project_id}).scalar()
    # THE STAGING NODE IS A REAL CHECK (06-04): the newest deploy of that environment — live
    # (done), failed (red, with its reason), still going, or none yet — never "a target exists".
    last = connection.execute(text(
        "SELECT s.status, s.failure_reason, s.repository FROM stack_deployments s "
        "JOIN deployment_targets t ON t.id = s.target_id WHERE t.project_id = :p "
        "AND t.status = 'active' AND lower(t.environment) = lower(:e) "
        "AND s.status IN ('healthy', 'failed', 'running', 'deploying', 'applying') "
        "ORDER BY COALESCE(s.finished_at, s.created_at) DESC LIMIT 1"),
        {"p": project_id, "e": staging or ""}).mappings().first() if staging else None
    checks_by = {str(r["full_name"]).lower(): as_json(r["checks"], {}) or {} for r in connection.execute(text(
        "SELECT b.full_name, COALESCE(NULLIF(b.checks, '{}'::jsonb), g.checks) AS checks "
        "FROM github_repository_bindings b LEFT JOIN LATERAL (SELECT checks FROM github_repos x "
        "WHERE lower(x.full_name) = lower(b.full_name) ORDER BY x.checked_at DESC NULLS LAST "
        "LIMIT 1) g ON true WHERE b.project_id = :p AND b.status = 'active'"),
        {"p": project_id}).mappings()}
    for r in out:
        checks = checks_by.get(r["full_name"].lower(), {})
        # Why a check failed, in its own words (a red node's tooltip and its line).
        r["access_reasons"] = {
            k: _check_reason(checks.get(src) or (checks.get("pr") if k == "pr" else None))
            for k, src in (("read", "read"), ("write", "write"), ("pr", "pull_requests"))}
        if staging:
            word = str(staging).title()
            if last is None:
                r["deploy_check"] = {"label": word, "state": "todo",
                                     "reason": f"Nothing deployed to {word.lower()} yet"}
            elif last["status"] == "healthy":
                r["deploy_check"] = {"label": word, "state": "done",
                                     "reason": f"The last deploy to {word.lower()} is live"}
            elif last["status"] == "failed":
                r["deploy_check"] = {"label": word, "state": "failed",
                                     "reason": " ".join(str(last["failure_reason"] or
                                                            "The last deploy failed").split())[:140]}
            else:
                r["deploy_check"] = {"label": word, "state": "now",
                                     "reason": f"Deploying to {word.lower()} now"}
        c = via.get(r["full_name"].lower()) or {}
        method = str(c.get("auth_method") or "")
        how = "GitHub App" if method.startswith("github_app") or c.get("connection_label") == \
            "GitHub App" else ("a token" if method else None)
        r["via"] = " · ".join(x for x in (how, c.get("account_login")) if x) or None
        r["last_push"] = f"{age(pushed)} ago" if pushed else None
        r["deploys_to"] = (str(staging).title() if staging else None)
    return out


__all__ = ["build_action", "commands", "decisions", "docs", "dossier", "graph", "learned",
           "learned_label", "mapping", "node_label", "parse_test_counts", "prd_title", "repo_rows",
           "view"]
