"""The Context Graph and the Domain layer in the Graph area (design §4.2, §4.3, §4.7.3).

Two graphs, one store. The Knowledge Graph is the reference world (code, rules, regulations and
their clauses); the Context Graph is what agents did and decided while they ran: a case, the
runs that worked on it, each step's decision, the precedent a run produced or reused and the
remediation it proposed. Both are ``kg_nodes`` rows; this module is how the canvas reads the
second one.

KINDS, LAYERS, LABELS AND COLOURS COME FROM THE ONTOLOGY PACKS. The core kinds (the agent trace
the engine writes itself: ``agent_decision``, ``coverage_gap``) are declared here; every other
Domain or Context kind is whatever a pack under ``packages/specpack/ontology/`` declares, with
its label and colour — a new pack's kinds appear on the chips, in the legend and in the tree
without a change here.

THE TRACE IS LINKED BY WHAT THE ENGINE RECORDED, NOT BY EDGES IT NEVER WROTE. A step's decision
carries its run and step (``attrs.run_id``/``step_id``); a pack's own context node is keyed by
its run (``remediation:<run>:<step>``); a workflow run's context names the cases it opened
(``compliance_case_ids``, any ``*_case_ids``); a decision's statement names the nodes it cites by
id. Those are read here into edges the walk draws ("worked on", "decided", "remediation",
"concerns", "cites"), never written back: the trace stays the single source (§4.2).

A run with no ``run`` node of its own is drawn as a run all the same (``ctxrun:<run id>``),
named by its workflow and start time — never by its id.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)

#: The Context kinds the engine itself writes (context_graph.py), in every project.
CORE_CONTEXT_KINDS = frozenset({"agent_decision", "coverage_gap", "resolved_pattern"})
#: The Domain kinds BOSS knows without a pack (scopes.DOMAIN_KINDS).
CORE_DOMAIN_KINDS = frozenset({"regulation", "clause", "document"})
#: What a Context focus draws beside the trace itself: the runs, precedents and incidents the
#: trace hangs on (their own layer is Learning), so "Context" alone still shows the story.
CONTEXT_ANCHORS = frozenset({"run", "precedent", "incident"})

CORE_KIND_META: dict[str, dict[str, Any]] = {
    "agent_decision": {"layer": "context", "label": "Agent decision", "plural": "Agent decisions", "color": "#38bdf8"},
    "coverage_gap": {"layer": "context", "label": "Coverage gap", "plural": "Coverage gaps", "color": "#fbbf24"},
    "resolved_pattern": {"layer": "context", "label": "Resolved pattern", "plural": "Resolved patterns", "color": "#a78bfa"},
    "regulation": {"layer": "domain", "label": "Regulation", "plural": "Regulations"},
    "clause": {"layer": "domain", "label": "Clause", "plural": "Clauses"},
    "document": {"layer": "domain", "label": "Document", "plural": "Documents"},
}

#: The links the trace implies (load_trace), never written as kg_edges rows.
TRACE_EDGES = frozenset({"decided", "remedied_by", "worked_on", "concerns", "cites", "precedent"})

#: Virtual run ids: a run the trace names that has no run node of its own.
VIRTUAL_RUN = "ctxrun:"
_NODE_ID = re.compile(r"\bkgn_[0-9a-f]{20}\b")
_RUN_ID = re.compile(r"^(wfr|ctr|ctask|run|athr)_[0-9a-z]+$")


# ── the packs ────────────────────────────────────────────────────────────────────────────────

_PACK_KINDS: Optional[dict[str, dict[str, Any]]] = None


def _plural(label: str) -> str:
    if label.endswith("y") and not label.endswith(("ay", "ey", "oy")):
        return label[:-1] + "ies"
    return label if label.endswith("s") else label + "s"


def pack_kinds() -> dict[str, dict[str, Any]]:
    """Every kind every shipped pack declares — {kind: {layer, label, plural, color, pack}} —
    read once from the YAML on disk. A pack that cannot be read is logged and skipped: the
    canvas still draws everything else."""
    global _PACK_KINDS
    if _PACK_KINDS is not None:
        return _PACK_KINDS
    out: dict[str, dict[str, Any]] = {}
    try:
        from .ontology_packs import SHIPPED_PACK_IDS, _ontology_root
        from ..yaml_files import load_mapping

        # In shipping order, so the older pack keeps its words for a kind a newer one shares
        # (banking-dq also declares banking-regulatory's `system`, `owner`, ...).
        order = {pid: n for n, pid in enumerate(SHIPPED_PACK_IDS)}
        paths = sorted(_ontology_root().glob("*.yaml"),
                       key=lambda p: (order.get(p.stem, len(order)), p.stem))
    except Exception:  # noqa: BLE001 - no specpack: core kinds only
        log.warning("ontology packs could not be listed", exc_info=True)
        paths = []
    for path in paths:
        try:
            document = load_mapping(path)
            pack_id = str(document.get("id") or path.stem)
            for kind in document.get("kinds") or []:
                kid = str(kind.get("id") or "").strip()
                if not kid or kid in out:
                    continue
                label = str(kind.get("label") or kid.replace("_", " ").capitalize())
                out[kid] = {"layer": kind.get("layer"), "label": label,
                            "plural": str(kind.get("plural") or _plural(label)),
                            "color": kind.get("color"), "pack": pack_id}
        except Exception:  # noqa: BLE001 - one bad pack never hides the others
            log.warning("ontology pack %s could not be read", path.name, exc_info=True)
    _PACK_KINDS = out
    return out


def reset_cache() -> None:
    global _PACK_KINDS
    _PACK_KINDS = None


def kind_meta(kind: str) -> Optional[dict[str, Any]]:
    """A kind's label, plural, colour and layer: the pack's word when a pack declares it."""
    pack = pack_kinds().get(kind)
    core = CORE_KIND_META.get(kind)
    if pack and core:
        return {**core, **{k: v for k, v in pack.items() if v}}
    return pack or core


def layer_of(kind: str) -> Optional[str]:
    """The product layer a pack (or the core) puts a kind in: domain or context; None when
    neither says (the caller's own mapping decides)."""
    meta = kind_meta(kind)
    layer = (meta or {}).get("layer")
    return str(layer) if layer in ("domain", "context") else None


def kinds_in(layer: str) -> frozenset[str]:
    core = CORE_CONTEXT_KINDS if layer == "context" else CORE_DOMAIN_KINDS if layer == "domain" else frozenset()
    return core | frozenset(k for k, m in pack_kinds().items() if m.get("layer") == layer)


def legend(connection: Connection, project_id: str) -> dict[str, dict[str, Any]]:
    """The Domain and Context kinds the canvas names and colours: the core kinds, then every
    pack's, the project's own pack first (its words win where two packs share a kind)."""
    out: dict[str, dict[str, Any]] = {}
    own: dict[str, dict[str, Any]] = {}
    try:
        from .ontology_packs import get_project_pack

        with connection.begin_nested():
            pack = get_project_pack(connection, project_id=project_id)
        if pack is not None:
            for kid, meta in pack.kind_meta.items():
                label = str(meta.get("label") or kid.replace("_", " ").capitalize())
                own[kid] = {"layer": meta.get("layer"), "label": label,
                            "plural": str(meta.get("plural") or _plural(label)),
                            "color": meta.get("color"), "pack": pack.pack_id}
    except Exception:  # noqa: BLE001 - the shipped packs still name the kinds
        log.warning("project %s ontology pack could not be read", project_id, exc_info=True)
    for kid in sorted(set(CORE_KIND_META) | set(pack_kinds()) | set(own)):
        meta = {**(kind_meta(kid) or {}), **{k: v for k, v in own.get(kid, {}).items() if v}}
        if meta.get("layer") not in ("domain", "context"):
            continue
        out[kid] = {k: meta.get(k) for k in ("layer", "label", "plural", "color")}
    return out


# ── words ────────────────────────────────────────────────────────────────────────────────────

def words(name: str) -> str:
    """``HistoricalPatternMatched`` → "Historical pattern matched"; ``lane_confirmation`` →
    "Lane confirmation"."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name or "")).replace("_", " ").strip()
    return s[:1].upper() + s[1:].lower() if s else s


def short_fingerprint(value: Any) -> Optional[str]:
    v = str(value or "").strip()
    if not v:
        return None
    if v == "deterministic":
        return "deterministic"
    v = v.split(":", 1)[-1]
    return v[:12]


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def run_of(row: Mapping[str, Any]) -> Optional[str]:
    """The run a context node belongs to: ``attrs.run_id``, else the run in its node_key
    (``remediation:<run>:<step>``, ``precedent:<run>:<step>``, ``context:<kind>:<run>:<step>``)."""
    attrs = row.get("attrs") or {}
    rid = str(attrs.get("run_id") or "").strip()
    if rid and not rid.startswith("adhoc:"):
        return rid
    for part in str(row.get("node_key") or "").split(":"):
        if _RUN_ID.match(part):
            return part
    return None


# ── the trace index ──────────────────────────────────────────────────────────────────────────

@dataclass
class Trace:
    """The project's Context Graph, read once per request: its nodes, the runs they belong to,
    and the edges the trace implies (src, dst, kind, weight)."""
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)     # run id -> {node_id, title, at, ...}
    edges: list[tuple[str, str, str, int]] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)               # node id -> kind (incl. virtual runs)

    def touching(self, ids: Iterable[str]) -> list[tuple[str, str, str, int]]:
        ids = set(ids)
        return [e for e in self.edges if e[0] in ids or e[1] in ids]

    def run_node(self, run_id: str) -> Optional[str]:
        r = self.runs.get(run_id)
        return r["node_id"] if r else None


_CASE_KEYS = re.compile(r".*case_ids?$")


def load_trace(connection: Connection, project_id: str, withheld: Iterable[str] = ()) -> Trace:
    """Read the project's Context Graph and the links it implies. Small (a project's trace is
    hundreds of rows, not the code graph's tens of thousands) and read only when asked."""
    withheld = frozenset(withheld)
    trace = Trace()
    ctx_kinds = sorted(kinds_in("context") - withheld)
    if not ctx_kinds:
        return trace
    rows = connection.execute(text(
        "SELECT id, kind, node_key, name, attrs, recorded_at FROM kg_nodes WHERE project_id = :p "
        "AND kind = ANY(:k) AND valid_to IS NULL"), {"p": project_id, "k": ctx_kinds}).mappings().all()
    if not rows:
        return trace
    for r in rows:
        trace.nodes[r["id"]] = dict(r)
        trace.kinds[r["id"]] = r["kind"]
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in trace.nodes.values():
        rid = run_of(r)
        if rid:
            by_run[rid].append(r)
    run_ids = sorted(by_run)
    # The runs: their own run node when there is one, else a virtual run named by its workflow.
    found = {}
    if run_ids:
        for nid, key in connection.execute(text(
                "SELECT id, node_key FROM kg_nodes WHERE project_id = :p AND kind = 'run' "
                "AND node_key = ANY(:k) AND valid_to IS NULL"),
                {"p": project_id, "k": [f"run:{r}" for r in run_ids]}).all():
            found[str(key)[4:]] = nid
    meta: dict[str, dict[str, Any]] = {}
    wf_ids = [r for r in run_ids if r.startswith("wfr_")]
    if wf_ids:
        try:
            with connection.begin_nested():
                for row in connection.execute(text(
                        "SELECT r.id, coalesce(nullif(r.input->>'title', ''), d.name) AS title, r.created_at, "
                        "r.status, r.context, v.version FROM workflow_runs r "
                        "LEFT JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
                        "LEFT JOIN workflow_versions v ON v.id = r.workflow_version_id "
                        "WHERE r.project_id = :p AND r.id = ANY(:r)"), {"p": project_id, "r": wf_ids}).mappings().all():
                    meta[row["id"]] = dict(row)
        except Exception:  # noqa: BLE001 - a run without its record is still drawn, by its time
            log.warning("workflow runs of project %s could not be read", project_id, exc_info=True)
    for rid in run_ids:
        m = meta.get(rid, {})
        first = min((r["recorded_at"] for r in by_run[rid] if r.get("recorded_at")), default=None)
        at = m.get("created_at") or first
        node_id = found.get(rid) or f"{VIRTUAL_RUN}{rid}"
        trace.runs[rid] = {"node_id": node_id, "title": str(m.get("title") or "A workflow run"),
                           "at": at, "status": m.get("status"), "version": m.get("version"),
                           "context": m.get("context") or {}, "virtual": rid not in found}
        trace.kinds[node_id] = "run"

    # Cases: a context kind whose node_key is "case:<subject>" or named *_case.
    cases = {nid: r for nid, r in trace.nodes.items()
             if r["kind"].endswith("_case") or str(r.get("node_key") or "").startswith("case:")}
    case_runs: dict[str, list[str]] = defaultdict(list)
    for rid, run in trace.runs.items():
        ctx = run["context"] if isinstance(run["context"], Mapping) else {}
        named: set[str] = set()
        for key, value in ctx.items():
            if _CASE_KEYS.match(str(key)):
                for v in (value if isinstance(value, list) else [value]):
                    if str(v) in cases:
                        named.add(str(v))
        if not named:
            # An older run's record without the ids: its intake decision names the case's subject.
            for r in by_run[rid]:
                said = str((r.get("attrs") or {}).get("statement") or "")
                for cid, case in cases.items():
                    subject = _subject_of(case)
                    if subject and subject in said:
                        named.add(cid)
        for cid in named:
            case_runs[cid].append(rid)

    def at_of(rid: str) -> str:
        return _iso(trace.runs[rid]["at"]) or ""

    edges: dict[tuple[str, str, str], int] = {}

    def add(src: str, dst: str, kind: str, weight: int = 1) -> None:
        if src and dst and src != dst:
            edges[(src, dst, kind)] = max(weight, edges.get((src, dst, kind), 0))

    # A context node of a run hangs on that run: "produced" (a decision, a remediation package).
    for rid, items in by_run.items():
        run_node = trace.runs[rid]["node_id"]
        for r in items:
            if r["id"] in cases:
                continue
            add(run_node, r["id"], "produced", 2)
    # A case: the subject it concerns, the runs that worked on it and, from the latest of them,
    # its decisions and its remediation — the case file at one hop.
    for cid, case in cases.items():
        subject = _subject_of(case)
        if subject:
            add(cid, subject, "concerns", 3)
        rids = sorted(set(case_runs.get(cid, [])), key=at_of)
        for rid in rids:
            add(trace.runs[rid]["node_id"], cid, "worked_on", 1)
        if rids:
            latest = rids[-1]
            for r in by_run[latest]:
                if r["id"] == cid or r["id"] in cases:
                    continue
                add(cid, r["id"], "remedied_by" if "remediation" in r["kind"] else "decided", 3)
            add(trace.runs[latest]["node_id"], cid, "worked_on", 2)
    # What a decision names by id (the rule it confirmed, the code it read): "cites".
    named_ids: dict[str, set[str]] = defaultdict(set)
    for nid, r in trace.nodes.items():
        said = " ".join(str((r.get("attrs") or {}).get(k) or "") for k in ("statement", "change_brief"))
        for ref in set(_NODE_ID.findall(said)):
            named_ids[nid].add(ref)
    refs = {ref for s in named_ids.values() for ref in s}
    if refs:
        live = set(connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND id = ANY(:i) AND valid_to IS NULL"),
            {"p": project_id, "i": sorted(refs)}).scalars().all())
        for nid, ids in named_ids.items():
            for ref in ids & live:
                if ref != nid and not (nid in cases and ref == _subject_of(cases[nid])):
                    add(nid, ref, "cites", 1)
    # A precedent a run produced (keyed by the run) hangs on it, like its decisions.
    if run_ids:
        for pid, pkey, pattrs in connection.execute(text(
                "SELECT id, node_key, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'precedent' "
                "AND valid_to IS NULL AND (attrs->>'run_id' = ANY(:r) OR split_part(node_key, ':', 2) = ANY(:r))"),
                {"p": project_id, "r": run_ids}).all():
            rid = run_of({"attrs": pattrs or {}, "node_key": pkey})
            if rid in trace.runs:
                add(trace.runs[rid]["node_id"], pid, "produced", 2)
                trace.kinds.setdefault(pid, "precedent")
                for cid, rids in case_runs.items():
                    if rid in rids:
                        add(cid, pid, "precedent", 3)
        # A precedent a case's run reused (the run node's own edge): on the case file too.
        node_of = {trace.runs[rid]["node_id"]: rid for rid in run_ids}
        for src, pid, _kind in connection.execute(text(
                "SELECT e.src_id, e.dst_id, e.kind FROM kg_edges e JOIN kg_nodes o ON o.id = e.dst_id "
                "AND o.kind = 'precedent' AND o.valid_to IS NULL WHERE e.project_id = :p AND e.valid_to IS NULL "
                "AND e.kind = 'reused' AND e.src_id = ANY(:r)"),
                {"p": project_id, "r": sorted(n for n in node_of if not n.startswith(VIRTUAL_RUN))}).all():
            trace.kinds.setdefault(pid, "precedent")
            for cid, rids in case_runs.items():
                if node_of[src] in rids:
                    add(cid, pid, "reused", 2)
    trace.edges = [(s, d, k, w) for (s, d, k), w in edges.items()]
    return trace


def _subject_of(case: Mapping[str, Any]) -> Optional[str]:
    attrs = case.get("attrs") or {}
    for key in ("clause_id", "subject_id", "alert_id", "incident_id"):
        v = str(attrs.get(key) or "")
        if _NODE_ID.fullmatch(v):
            return v
    key = str(case.get("node_key") or "")
    if key.startswith("case:") and _NODE_ID.fullmatch(key[5:]):
        return key[5:]
    return None


# ── virtual runs ─────────────────────────────────────────────────────────────────────────────

def run_label(run: Mapping[str, Any]) -> str:
    """ "<workflow> · Nov 01, 03:34" (UTC, the engine's naming; the console re-says it in the
    viewer's zone)."""
    at = run.get("at")
    when = f" · {at:%b %d, %H:%M}" if isinstance(at, datetime) else ""
    return f"{run.get('title') or 'A workflow run'}{when}"


def virtual_run_payload(trace: Trace, node_id: str) -> Optional[dict[str, Any]]:
    if not node_id.startswith(VIRTUAL_RUN):
        return None
    rid = node_id[len(VIRTUAL_RUN):]
    run = trace.runs.get(rid)
    if not run:
        return None
    return {"id": node_id, "key": f"run:{rid}", "kind": "run", "layer": "learning",
            "label": run_label(run), "sub": "Workflow run", "trust_tier": "observed"}


# ── labels without ids ───────────────────────────────────────────────────────────────────────

def display(kind: str, name: str, attrs: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    """A context node's label and sub-line in words: a decision by its type ("Historical
    pattern matched") with its step under it; a coverage gap by its step."""
    if kind == "agent_decision":
        step = words(attrs.get("step_id") or "")
        return words(attrs.get("decision_type") or "Decision"), (f"{step} step" if step else None)
    if kind == "coverage_gap":
        step = words(attrs.get("step_id") or "")
        return "Coverage gap", (f"{step} step" if step else None)
    if kind.endswith("_case"):
        status = str(attrs.get("status") or "").strip()
        return name, (f"{status.capitalize()} case" if status else None)
    if attrs.get("owner_team"):
        return name, f"For {attrs['owner_team']}"
    return name, None


def mend_ids(connection: Connection, project_id: str, payload: Any) -> Any:
    """Every ``kgn_…`` id a label, sub-line or statement still carries (a case named by its
    clause's id, a decision that says which node it confirmed) said by that node's name — one
    query for the whole response. No raw ids on screen."""
    found: set[str] = set()

    def scan(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(x, str) and k in _MEND_KEYS:
                    found.update(_NODE_ID.findall(x))
                else:
                    scan(x)
        elif isinstance(v, list):
            for x in v:
                scan(x)

    scan(payload)
    if not found:
        return payload
    names = {r[0]: r[1] for r in connection.execute(text(
        "SELECT id, name FROM kg_nodes WHERE project_id = :p AND id = ANY(:i)"),
        {"p": project_id, "i": sorted(found)}).all()}

    def say(s: str) -> str:
        return _NODE_ID.sub(lambda m: f"“{names[m.group(0)]}”" if m.group(0) in names else "an earlier node", s)

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: (say(x) if isinstance(x, str) and k in _MEND_KEYS else walk(x)) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(payload)


_MEND_KEYS = frozenset({"label", "sub", "statement", "title", "change_brief", "text"})


# ── the panel ────────────────────────────────────────────────────────────────────────────────

def _agent(connection: Connection, project_id: str, run_id: Optional[str], step_id: Optional[str],
           attrs: Mapping[str, Any], created_by: str) -> dict[str, Any]:
    """Which agent made it, in words: the project's role agent by its callsign or name (from the
    run's acts for that step), an outside agent by its name, or BOSS's deterministic step."""
    fingerprint = str(attrs.get("agent_fingerprint") or "")
    row = None
    if run_id and step_id:
        try:
            with connection.begin_nested():
                row = connection.execute(text(
                    "SELECT a.role_key, a.fingerprint, a.kind, r.display_name, r.spec->'persona'->>'callsign' AS callsign, "
                    "r.fingerprint AS role_fingerprint FROM agent_acts a "
                    "LEFT JOIN roles r ON r.project_id = a.project_id AND r.role_key = a.role_key "
                    "WHERE a.project_id = :p AND a.workflow_run_id = :r AND a.step_id = :s "
                    "AND a.role_key IS NOT NULL ORDER BY a.started_at LIMIT 1"),
                    {"p": project_id, "r": run_id, "s": step_id}).mappings().first()
        except Exception:  # noqa: BLE001 - who it was is words, never a reason to refuse
            row = None
    outside = str(attrs.get("agent_name") or attrs.get("external_agent") or "")
    if outside or created_by.startswith("external"):
        name = outside or words(created_by.split(":", 1)[-1])
        return {"name": name, "kind": "outside", "role": None,
                "fingerprint": short_fingerprint(fingerprint)}
    if row:
        role = words(row["role_key"])
        name = row["callsign"] or row["display_name"] or role
        return {"name": str(name), "kind": "agent", "role": str(row["display_name"] or role),
                "fingerprint": short_fingerprint(fingerprint if fingerprint not in ("", "deterministic")
                                                 else row["fingerprint"] or row["role_fingerprint"] or fingerprint)}
    if fingerprint == "deterministic" or not fingerprint:
        return {"name": "BOSS", "kind": "deterministic", "role": "A deterministic step (no model)",
                "fingerprint": short_fingerprint(fingerprint)}
    return {"name": "An agent", "kind": "agent", "role": None, "fingerprint": short_fingerprint(fingerprint)}


def _version_words(connection: Connection, project_id: str, value: Any) -> Optional[str]:
    """A recorded version in words: a workflow version id is said by its number ("v4"), never
    the id; any other recorded value as it is."""
    v = str(value or "").strip()
    if not v:
        return None
    if v.startswith("wfv_"):
        try:
            with connection.begin_nested():
                n = connection.execute(text(
                    "SELECT version FROM workflow_versions WHERE project_id = :p AND id = :i"),
                    {"p": project_id, "i": v}).scalar()
        except Exception:  # noqa: BLE001
            n = None
        return f"v{n}" if n is not None else None
    if re.fullmatch(r"[a-z]+_[0-9a-f]{12,}", v):
        return None     # some other record's id: never shown
    return v if v.lower().startswith("v") or not v.isdigit() else f"v{v}"


def context_card(connection: Connection, project_id: str, row: Mapping[str, Any], payload_of,
                 withheld: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    """What the details panel says about a Context node, a case, or a run of the trace: the
    decision in words, which step of which run, which agent, the versions and fingerprint it
    ran under, when — and its links (the case it belongs to, what it cited, the precedent and
    remediation), each a node the console can focus."""
    trace = load_trace(connection, project_id, withheld)
    nid = str(row["id"])
    kind = str(row["kind"])
    if nid not in trace.kinds and kind not in kinds_in("context"):
        return None
    attrs = row.get("attrs") or {}
    rid = run_of(row) if kind != "run" else (str(row.get("node_key") or "")[4:] or None)
    run = trace.runs.get(rid) if rid else None
    step = attrs.get("step_id")
    ids: dict[str, list[str]] = defaultdict(list)
    for s, d, k, _w in trace.touching([nid]):
        other, out = (d, True) if s == nid else (s, False)
        group = {("produced", False): "run", ("produced", True): "produced", ("worked_on", True): "case",
                 ("worked_on", False): "runs", ("decided", True): "decisions", ("decided", False): "case",
                 ("remedied_by", True): "remediation", ("remedied_by", False): "case",
                 ("concerns", True): "concerns", ("concerns", False): "cases",
                 ("cites", True): "cites", ("cites", False): "cited_by"}.get((k, out), k)
        if other not in ids[group]:
            ids[group].append(other)
    # A precedent this node's run produced or reused (real edges from a run node).
    run_node = run["node_id"] if run else None
    if run_node and not run_node.startswith(VIRTUAL_RUN):
        for other, ekind in connection.execute(text(
                "SELECT e.dst_id, e.kind FROM kg_edges e JOIN kg_nodes o ON o.id = e.dst_id AND o.valid_to IS NULL "
                "WHERE e.project_id = :p AND e.src_id = :r AND e.valid_to IS NULL AND o.kind = 'precedent' "
                "AND e.kind IN ('produced', 'reused', 'consulted')"), {"p": project_id, "r": run_node}).all():
            group = "precedent_reused" if ekind in ("reused", "consulted") else "precedent_produced"
            if other not in ids[group]:
                ids[group].append(other)
    for other in ids.get("produced", []):
        if trace.kinds.get(other) == "precedent" and other not in ids["precedent_produced"]:
            ids["precedent_produced"].append(other)
    # A decision's (or remediation's) run's precedent: what its run set for later runs.
    if run_node and run_node != nid:
        for s, d, k, _w in trace.touching([run_node]):
            if s == run_node and k == "produced" and trace.kinds.get(d) == "precedent" \
                    and d not in ids["precedent_produced"]:
                ids["precedent_produced"].append(d)
    all_ids = [i for group in ids.values() for i in group]
    real = [i for i in all_ids if not i.startswith(VIRTUAL_RUN)]
    found = payload_of(real)
    nodes: dict[str, dict[str, Any]] = {}
    for i in all_ids:
        if i.startswith(VIRTUAL_RUN):
            p = virtual_run_payload(trace, i)
            if p:
                nodes[i] = p
        elif i in found:
            nodes[i] = found[i]
    if run and run_node and run_node not in nodes and run_node != nid:
        p = virtual_run_payload(trace, run_node) or (payload_of([run_node]).get(run_node))
        if p:
            nodes[run_node] = p
            if run_node not in ids["run"]:
                ids["run"].insert(0, run_node)
    labels = [
        ("case", "Case"), ("concerns", "Concerns"), ("run", "Run"), ("runs", "Runs that worked on it"),
        ("decisions", "Decisions"), ("remediation", "Remediation"),
        ("produced", "What it produced"), ("cites", "Cites"), ("cited_by", "Cited by"),
        ("cases", "Cases about it"), ("precedent_produced", "Precedent produced"),
        ("precedent_reused", "Precedent reused"),
    ]
    links = []
    for key, label in labels:
        items = [nodes[i] for i in ids.get(key, []) if i in nodes]
        if key == "produced":
            items = [n for n in items if n["kind"] != "precedent"]
        if items:
            links.append({"key": key, "label": label, "items": items[:40], "total": len(items)})
    out: dict[str, Any] = {"links": links}
    if kind == "agent_decision" or kind == "coverage_gap" or (kind not in ("run",) and rid):
        out["decision_type"] = words(attrs.get("decision_type") or "") or None
        out["step"] = words(step) if step else None
        out["statement"] = attrs.get("statement") or row.get("body") or None
        out["agent"] = _agent(connection, project_id, rid, step, attrs, str(row.get("created_by") or ""))
        out["workflow_version"] = _version_words(connection, project_id, attrs.get("workflow_version")) or (
            f"v{run['version']}" if run and run.get("version") else None)
        out["policy_version"] = _version_words(connection, project_id, attrs.get("policy_version"))
        out["fingerprint"] = out["agent"].get("fingerprint")
    if run:
        out["run"] = {"label": run_label(run), "at": _iso(run["at"]), "status": run.get("status"),
                      "node_id": run["node_id"], "workflow_run_id": rid if rid and rid.startswith("wfr_") else None}
    out["at"] = _iso(row.get("recorded_at") or row.get("first_seen"))
    return out
