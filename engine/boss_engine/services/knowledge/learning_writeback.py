"""Learning while executing (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.3, §4.1, §4.3, §4.4).

Learning is written AT THE MOMENT THE AGENT LEARNS IT, inside the one agent pipeline — never
reconstructed afterwards from the repository or a run summary. Two write paths share
``record()``, so dedupe, scrub, provenance and the live event behave the same for both:

1. the agent says it: the ``record_learning`` tool (``knowledge.propose`` bundle) calls
   ``record()``;
2. the pipeline sees it: stage-end hooks (build stage boundary and repair checkpoint, the engine
   turn stream, workflow step completion, ``AgentService.decide``) call ``write_back()`` with
   what they observed, with no extra model call.

A learned item is a ``kg_nodes`` row whose ``kind`` is its learning kind (``test_failure``,
``investigation``…), ``trust_tier='inferred'`` (store-enforced: quarantined, forgettable by a
person), ``node_key='learning:<kind>:<dedupe_key>'``, with the §2.3 envelope in ``attrs``. Its
edges: ``produced`` (the run that learned it, attrs step/at), ``proposed_by`` (the agent),
``about`` (the code and rules it is about), ``ruled_out`` (what an investigation rejected),
``fixed_by`` (a test failure → the fix that made it pass) and ``supersedes`` (a newer learning
that contradicts it).

What later runs USE of it is ``kg_usage`` (``log_use``): ``consulted`` (a tool returned it),
``cited`` (an output named it), ``reused`` (a cited precedent or fix). Their ``consulted`` /
``reused`` edges are derived from those rows (``sync_usage_edges``) when the row is written and
again when ingest captures the run, so usage written before the run node exists is never lost
and re-ingest is idempotent (the edge key is (project, src, dst, kind)).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .scrub import contains_secret_marker, scrub
from .store import PostgresKnowledgeStore
from .titles import word_cut

LOG = logging.getLogger(__name__)

LEARNING_KINDS = frozenset({
    "investigation", "test_failure", "fix", "workaround", "review_finding",
    "decision", "root_cause", "precedent", "fact",
})

# The kind-specific ``detail`` fields (§2.3). A trailing ``?`` marks an optional field.
DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "investigation": ("tried", "ruled_out", "found"),
    "test_failure": ("test", "error_signature", "cause"),
    "fix": ("change_summary", "files", "pr_url?"),
    "workaround": ("problem", "workaround", "permanent_fix?"),
    "decision": ("question", "chosen", "reasons", "alternatives"),
    "precedent": ("symptom", "cause_summary", "fix_summary", "applies_to", "reuse_count"),
}

# The hooks that call write_back() (§4.1, table "The pipeline sees it").
HOOKS = frozenset({"build_stage", "repair_checkpoint", "engine_turn", "step_complete", "agent_decide"})

# Confidence is capped by evidence (§4.3): none refuses, a log excerpt alone caps at 0.6, a
# passing test at 0.9; below 0.4 is stored but never injected.
CAP_LOG_ONLY = 0.6
CAP_PASSING_TEST = 0.9
INJECT_BELOW = 0.4
#: Evidence stronger than a log (a diff, a file, a metric, a test that did not pass) but no
#: passing test: between the two caps the spec names.
CAP_OTHER = 0.75

EVIDENCE_TYPES = ("log", "test", "diff", "metric", "file")
#: Fields a kind cannot do without: a learning with none of them says nothing.
REQUIRED_ANY: dict[str, tuple[str, ...]] = {
    "investigation": ("ruled_out", "found"),
    "test_failure": ("test", "error_signature"),
    "fix": ("change_summary",),
    "workaround": ("workaround",),
    "decision": ("chosen",),
    "precedent": ("symptom",),
}
#: The ``detail`` field that names WHAT a learning is about, for the contradiction check
#: (archivist's same-predicate rule): a newer decision on the same question, precedent for the
#: same symptom or workaround for the same problem, on the same code, closes the older one.
SUBJECT_FIELDS: dict[str, str] = {"decision": "question", "precedent": "symptom", "workaround": "problem"}
#: Kinds where a new item on the same subject ADDS to what is known rather than contradicting it
#: (two incidents with one symptom are two precedents). The same lesson seen again is
#: `reinforce`d; a different one is linked (`correlates_with`), never superseded.
CUMULATIVE_KINDS = frozenset({"precedent"})
LIST_FIELDS = frozenset({"tried", "ruled_out", "files", "reasons", "alternatives", "applies_to"})

MAX_TITLE = 200
MAX_STATEMENT = 2000
MAX_FIELD = 2000
MAX_EXCERPT = 1200
MAX_EVIDENCE = 10
MAX_KEPT_EVIDENCE = 20
MAX_ABOUT = 30
MAX_LIST = 20

#: Citations in any output an agent writes: [L:<id>] for a learning, [R:<id>] for a rule (the
#: pack quotes them that way), [D:<id>] for a document's clause, a data-quality rule or another
#: node a rulebook put in the graph, or a bare kg node id.
CITATION = re.compile(r"\[(?P<tag>[LRD]):(?P<id>kgn_[A-Za-z0-9_]+)\]")
#: What a rulebook or regulation put into the graph and an answer may cite (rulebook source G6):
#: clauses, documents, data-quality rules, their thresholds and the CDEs they protect. A cited one
#: is logged like a rule or a learning — before, it was dropped without a word.
CITABLE_DOCUMENT_KINDS = frozenset({"clause", "document", "data_quality_rule", "dq_threshold", "cde"})
_NODE_ID = re.compile(r"\bkgn_[A-Za-z0-9]{6,}\b")

_STORE = PostgresKnowledgeStore()
_SPACES = re.compile(r"\s+")


class LearningRefused(ValueError):
    """A learning refused in words (an ``error`` from the tool; a hook skips it, logged)."""


# --- small helpers --------------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _attrs(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            out = json.loads(value)
            return out if isinstance(out, dict) else {}
        except ValueError:
            return {}
    return {}


def _secret(value: Any) -> bool:
    if value is None:
        return False
    raw = value if isinstance(value, str) else json.dumps(value, default=str)
    return contains_secret_marker(raw)


def _clean(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    raw = _SPACES.sub(" ", str(value if value is not None else "")).strip()
    if required and not raw:
        raise LearningRefused(f"{field} is required.")
    if _secret(raw):
        raise LearningRefused(f"{field} holds something shaped like a secret; nothing was stored.")
    return scrub(raw[:limit])[0]


def _context() -> dict[str, Any]:
    try:
        from ..llm_telemetry import get_llm_context
    except Exception:  # noqa: BLE001
        return {}
    return get_llm_context() or {}


def normalise(value: str) -> str:
    """An error signature or statement with what varies between occurrences taken out: ids,
    numbers, hex, quoted values, line numbers and paths' line suffixes, case and spacing."""
    out = str(value or "").lower()
    out = re.sub(r"\b(?:kgn|crun|wfr|wsr|arun|run|inc|dec)_[a-z0-9]+\b", "<id>", out)
    out = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{12,}\b", "<hex>", out)
    out = re.sub(r":\d+(?::\d+)?\b", "", out)
    out = re.sub(r"\d+(?:\.\d+)?", "<n>", out)
    out = re.sub(r"(['\"`]).{0,80}?\1", "<v>", out)
    return _SPACES.sub(" ", out).strip(" .")


def _hash(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


# --- validation -----------------------------------------------------------------------------------
def _detail(kind: str, detail: Mapping[str, Any] | None) -> dict[str, Any]:
    if detail is not None and not isinstance(detail, Mapping):
        raise LearningRefused("detail is an object of the kind's fields.")
    raw = dict(detail or {})
    if _secret(raw):
        raise LearningRefused("detail holds something shaped like a secret; nothing was stored.")
    fields = [f.rstrip("?") for f in DETAIL_FIELDS.get(kind, ())]
    if not fields:                      # review_finding, root_cause, fact: free-form detail
        return {str(k): _value(str(k), v) for k, v in list(raw.items())[:20]
                if str(k).strip() and v not in (None, "", [], {})}
    out: dict[str, Any] = {}
    for name in fields:
        if name in raw and raw[name] not in (None, "", [], {}):
            out[name] = _value(name, raw[name])
    need = REQUIRED_ANY.get(kind, ())
    if need and not any(out.get(f) not in (None, "", []) for f in need):
        raise LearningRefused(f"A {kind.replace('_', ' ')} needs detail.{' or detail.'.join(need)}.")
    return out


def _value(name: str, value: Any) -> Any:
    if name == "ruled_out":
        items = value if isinstance(value, (list, tuple)) else [value]
        out = []
        for item in list(items)[:MAX_LIST]:
            if isinstance(item, Mapping):
                entry = {"hypothesis": _clean(item.get("hypothesis") or item.get("what"), field="ruled_out",
                                              limit=MAX_FIELD),
                         "reason": _clean(item.get("reason"), field="ruled_out reason", limit=MAX_FIELD)}
                for key in ("symbol", "id"):
                    if item.get(key):
                        entry[key] = str(item[key]).strip()[:500]
                if entry["hypothesis"]:
                    out.append(entry)
            elif str(item or "").strip():
                out.append({"hypothesis": _clean(item, field="ruled_out", limit=MAX_FIELD), "reason": ""})
        return out
    if name in LIST_FIELDS:
        items = value if isinstance(value, (list, tuple)) else [value]
        return [_clean(v, field=name, limit=MAX_FIELD) for v in list(items)[:MAX_LIST]
                if str(v or "").strip()]
    if name == "reuse_count":
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0
    if isinstance(value, (Mapping, list, tuple)):
        raw = json.dumps(value, default=str)
        if len(raw) > MAX_FIELD * 2:
            return _clean(raw, field=name, limit=MAX_FIELD)
        cleaned = scrub(raw)[0]
        try:
            return json.loads(cleaned)
        except ValueError:          # a scrubbed span broke the JSON: keep it as text
            return cleaned[:MAX_FIELD]
    if isinstance(value, (bool, int, float)):
        return value
    return _clean(value, field=name, limit=MAX_FIELD)


def _evidence(evidence: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if not isinstance(evidence, (list, tuple)) or not evidence:
        raise LearningRefused("A learning needs evidence: at least one {type, ref, excerpt} "
                              f"(type is one of {', '.join(EVIDENCE_TYPES)}).")
    out = []
    for item in list(evidence)[:MAX_EVIDENCE]:
        if not isinstance(item, Mapping):
            raise LearningRefused("Each piece of evidence is {type, ref, excerpt}.")
        kind = str(item.get("type") or "").strip().lower()
        if kind not in EVIDENCE_TYPES:
            raise LearningRefused(f"evidence type is one of {', '.join(EVIDENCE_TYPES)}.")
        ref = _clean(item.get("ref"), field="evidence ref", limit=500)
        excerpt = _clean(item.get("excerpt"), field="evidence excerpt", limit=MAX_EXCERPT)
        if not (ref or excerpt):
            raise LearningRefused("Each piece of evidence names where it is (ref) or quotes it (excerpt).")
        entry = {"type": kind, "ref": ref, "excerpt": excerpt or None}
        if kind == "test" and item.get("passed") is not None:
            entry["passed"] = bool(item.get("passed"))
        if item.get("observed"):            # the pipeline ran it itself (a hook), not the agent's word
            entry["observed"] = True
        out.append(entry)
    return out


def confidence_cap(evidence: Sequence[Mapping[str, Any]]) -> float:
    """The most a learning may claim, by its evidence (§4.3)."""
    if not evidence:
        return 0.0
    if any(e.get("type") == "test" and (e.get("passed") or e.get("observed")) for e in evidence):
        return CAP_PASSING_TEST
    if all(e.get("type") == "log" for e in evidence):
        return CAP_LOG_ONLY
    return CAP_OTHER


def _confidence(value: Any, evidence: Sequence[Mapping[str, Any]]) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise LearningRefused("confidence is a number from 0 to 1.") from error
    if not 0.0 <= number <= 1.0:
        raise LearningRefused("confidence is a number from 0 to 1.")
    return round(min(number, confidence_cap(evidence)), 3)


# --- resolving what it is about -------------------------------------------------------------------
def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise LearningRefused("about.symbols, about.rules and about.areas are lists.")
    return list(dict.fromkeys(str(v).strip() for v in value if str(v or "").strip()))[:MAX_ABOUT]


def _resolve_code(connection: Connection, project_id: str, ref: str) -> Optional[dict[str, Any]]:
    """A live code node for a symbol or file reference, or None. Lenient: a learning about code
    the graph has not indexed yet is still a learning."""
    from .rules import RuleRefused, resolve_code

    try:
        return resolve_code(connection, project_id, ref)
    except RuleRefused:
        pass
    # A path, as a build's changed files name them: the file node whose path it is, or ends with.
    row = connection.execute(text(
        "SELECT id, kind, node_key, name, repo, path, attrs FROM kg_nodes WHERE project_id = :p "
        "AND kind = 'file' AND (valid_to IS NULL OR valid_to > now()) AND (path = :r OR path LIKE :tail) "
        "ORDER BY length(path) LIMIT 1"), {"p": project_id, "r": ref, "tail": f"%/{ref.lstrip('/')}"}
    ).mappings().first()
    return dict(row) if row else None


def _about(connection: Connection, project_id: str, about: Mapping[str, Any] | None
           ) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """(about as stored, code node ids, rule ids). Symbols are stored by node key once they
    resolve; one that does not is kept as named. Rules must be rules of this project."""
    if about is not None and not isinstance(about, Mapping):
        raise LearningRefused("about is {symbols[], rules[], areas[]}.")
    about = about or {}
    symbols, code_ids = [], []
    for ref in _list(about.get("symbols")):
        node = _resolve_code(connection, project_id, ref)
        if node is not None:
            symbols.append(node["node_key"])
            code_ids.append(node["id"])
        else:
            symbols.append(ref)
    rule_ids = []
    for ref in _list(about.get("rules")):
        rid = ref[2:] if ref.startswith(("R:", "r:")) else ref
        found = connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = 'rule' AND (id = :r OR node_key = :r) LIMIT 1"),
            {"p": project_id, "r": rid}).scalar()
        if found is None:
            raise LearningRefused(f"'{ref}' is not a rule of this project; name it by its id (kg_rules).")
        rule_ids.append(str(found))
    areas = [a.rsplit(":", 1)[-1] if a.startswith("area:") else a for a in _list(about.get("areas"))]
    return ({"symbols": list(dict.fromkeys(symbols)), "rules": list(dict.fromkeys(rule_ids)),
             "areas": list(dict.fromkeys(areas))}, list(dict.fromkeys(code_ids)), list(dict.fromkeys(rule_ids)))


# --- provenance -----------------------------------------------------------------------------------
def _agent_name(connection: Connection, project_id: str, role: Optional[str]) -> Optional[str]:
    if not role:
        return None
    from .rules import _agent_name as rules_agent_name

    try:
        with connection.begin_nested():
            return rules_agent_name(connection, project_id, role)
    except Exception:  # noqa: BLE001 — a name is a label, never a reason to refuse
        return None


def _agent_id(connection: Connection, project_id: str, role: Optional[str]) -> Optional[str]:
    """The agent's Passport id (roles.agent_id), or None for BOSS itself or an unknown role."""
    if not role:
        return None
    try:
        with connection.begin_nested():
            found = connection.execute(text(
                "SELECT agent_id FROM roles WHERE project_id = :p AND role_key = :r"),
                {"p": project_id, "r": role}).scalar()
    except Exception:  # noqa: BLE001 — an id is a label, never a reason to refuse
        return None
    return str(found) if found else None


def run_refs(connection: Connection, run_id: Optional[str]) -> dict[str, Optional[str]]:
    """What a run id is: ``{run_id, code_task_run_id, workflow_run_id}``. A code task run names
    the workflow run that started it (finalize_ctx.workflow_run_id)."""
    rid = str(run_id or "").strip() or None
    out: dict[str, Optional[str]] = {"run_id": rid, "code_task_run_id": None, "workflow_run_id": None}
    if not rid:
        return out
    if rid.startswith("wfr"):
        out["workflow_run_id"] = rid
        return out
    if rid.startswith(("crun", "ctr")):
        out["code_task_run_id"] = rid
        try:
            with connection.begin_nested():
                out["workflow_run_id"] = connection.execute(text(
                    "SELECT finalize_ctx->>'workflow_run_id' FROM code_task_runs WHERE id = :i"),
                    {"i": rid}).scalar()
        except Exception:  # noqa: BLE001
            pass
    return out


def provenance(connection: Connection, project_id: str,
               observed: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Who learned it, in which run and step, on which model. From the LLM context of the call
    in progress — what the pipeline set, never a tool's arguments — overlaid by what a hook
    observed (the pipeline itself is the witness there)."""
    from ..llm_telemetry import GENERAL_NAME, GENERAL_ROLE

    ctx = _context()
    seen = dict(observed or {})
    run_id = seen.get("run_id") or ctx.get("run_id")
    refs = run_refs(connection, run_id)
    stage = seen.get("step") or ctx.get("step") or ctx.get("stage")
    # The addressed agent of a chat turn (`speaker_role`, AgentService.chat), else the stage's.
    role = seen.get("agent_role") or ctx.get("speaker_role") or ctx.get("role_key")
    if not role and stage:
        try:
            from ..llm_telemetry import canonical_role, role_for_stage

            role = canonical_role(role_for_stage(str(stage).split("-", 1)[0]))
        except Exception:  # noqa: BLE001
            role = None
    name = seen.get("agent_name") or ctx.get("agent_name") or _agent_name(connection, project_id, role)
    if not role and not name:
        # NEVER BLANK: nobody was addressed and no stage names an agent, so BOSS itself said it.
        role, name = GENERAL_ROLE, GENERAL_NAME
    return {
        "agent_role": role or None,
        "agent_name": name or None,
        "agent_id": seen.get("agent_id") or ctx.get("agent_id") or _agent_id(connection, project_id, role),
        "run_id": refs["run_id"],
        "workflow_run_id": seen.get("workflow_run_id") or ctx.get("workflow_run_id") or refs["workflow_run_id"],
        "code_task_run_id": seen.get("code_task_run_id") or ctx.get("code_task_run_id") or refs["code_task_run_id"],
        "step": str(stage) if stage else None,
        "model_id": seen.get("model_id") or ctx.get("model_id") or ctx.get("model"),
        # The conversation it was said in (a chat turn or a press in that thread: AgentService),
        # the anchor when there is no run (learning_conversations).
        "agent_thread_id": seen.get("agent_thread_id") or ctx.get("agent_thread_id"),
        "act_id": seen.get("act_id") or ctx.get("agent_act_id"),
        "at": _now_iso(),
    }


def anchor_ref(prov: Mapping[str, Any]) -> Optional[str]:
    """The run a learning or a use hangs on: the build (code task run) when there is one — the
    run node ingest captures — else the workflow run, else the agent run, else the conversation
    it was said in (learning_conversations)."""
    return (prov.get("code_task_run_id") or prov.get("workflow_run_id") or prov.get("run_id")
            or prov.get("agent_thread_id")) or None


def _anchor(connection: Connection, project_id: str, kind: str, key: str, name: str,
            attrs: Optional[dict] = None) -> str:
    found = _STORE.find_by_key(connection, project_id=project_id, kind=kind, node_key=key)
    if found is not None:
        return found["id"]
    return _STORE.upsert_node(connection, project_id=project_id, kind=kind, node_key=key, name=name,
                              body="", trust_tier="observed", actor_kind="system", source="learning",
                              attrs=attrs or {})


def run_name(connection: Connection, ref: str) -> tuple[Optional[str], Any]:
    """A run's own title (a workflow run's title, else its workflow's name; a build's title) and
    when it started, or (None, None) when there is no such run. Never its id."""
    ref = str(ref or "")
    row = None
    try:
        with connection.begin_nested():
            if ref.startswith("wfr"):
                row = connection.execute(text(
                    "SELECT coalesce(nullif(r.input->>'title', ''), d.name) AS title, r.created_at "
                    "FROM workflow_runs r LEFT JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
                    "WHERE r.id = :r"), {"r": ref}).mappings().first()
            else:
                row = connection.execute(text(
                    "SELECT title, created_at FROM code_task_runs WHERE id = :r"), {"r": ref}).mappings().first()
    except Exception:  # noqa: BLE001 — a name is a label, never a reason to refuse
        row = None
    if not row:
        return None, None
    # Cut at a word within its limit (tester round 6: "…(retry with verif…"), never mid-word.
    title = word_cut(str(row["title"] or "")) or None
    return title, row.get("created_at")


def run_title(connection: Connection, ref: str) -> str:
    """A RUN BY ITS NAME, never its id (tester round 2: the tree, the canvas and the panel said
    "Run wfr_6267…"): a workflow run's title (else its workflow's name), a build's title, then
    when it started — "Business rules scan: loan repayment… · Oct 25, 02:50"."""
    title, started = run_name(connection, ref)
    if not title:
        return "A run" + (f" · {started:%b %d, %H:%M}" if started else "")
    return f"{title} · {started:%b %d, %H:%M}" if started else title


def run_anchor(connection: Connection, project_id: str, ref: str) -> str:
    """The run node a learning or a use hangs on (``run:<id>``, the key ingest captures under),
    named for the run (run_title). One still named by its raw id is renamed. A conversation
    (``athr_…``) is a run node of subtype conversation, named after its question, with its
    addressed agent (learning_conversations)."""
    from .learning_conversations import anchor as conversation_anchor, is_conversation

    if is_conversation(ref):
        return conversation_anchor(connection, project_id, ref)
    node_id = _anchor(connection, project_id, "run", f"run:{ref}", run_title(connection, ref), {"source_id": ref})
    rename_raw_run(connection, project_id, node_id, ref)
    return node_id


def rename_raw_run(connection: Connection, project_id: str, node_id: str, ref: str) -> None:
    connection.execute(text(
        "UPDATE kg_nodes SET name = :n WHERE project_id = :p AND id = :id AND name = :raw"),
        {"n": run_title(connection, ref), "p": project_id, "id": node_id, "raw": f"Run {ref}"})


# --- the write ------------------------------------------------------------------------------------
def record(
    connection: Connection,
    *,
    project_id: str,
    learning_kind: str,
    title: str,
    statement: str,
    detail: Mapping[str, Any],
    about: Mapping[str, Sequence[str]],
    evidence: Sequence[Mapping[str, Any]],
    confidence: float,
    supersedes: Optional[str] = None,
    observed: Optional[Mapping[str, Any]] = None,
    links: Optional[Mapping[str, Sequence[str]]] = None,
) -> dict[str, Any]:
    """Record one learned item; return it as the view payload (``KgLearningItem``).

    The ``record_learning`` tool's arguments, validated against ``DETAIL_FIELDS``. ``about`` is
    ``{symbols[], rules[], areas[]}`` and becomes ``about`` edges; ``evidence`` is at least one
    ``{type: log|test|diff|metric|file, ref, excerpt}``.

    - Provenance (agent role and name, run, workflow run, code task run, step, model) comes from
      ``llm_telemetry.get_llm_context()``, never from the arguments; ``kg_nodes.created_by`` is
      the agent role key, and a ``produced`` edge links the run. ``observed`` is a hook's own
      record of where it saw it (the pipeline's, never a tool's).
    - The node is ``inferred``, quarantined and forgettable, like ``kg_propose_fact``.
    - Dedupe: ``dedupe_key`` = hash(kind, normalised error signature or statement, sorted
      ``about`` keys). A repeat increments ``seen_count``, appends evidence, sets
      ``last_seen_run`` and adds a ``produced`` edge from the new run — no second node.
    - Contradiction: a learning negating an open one on the same ``about`` closes the old one
      (``supersedes``): the same question, symptom or problem (``SUBJECT_FIELDS``) answered
      differently, or the id the caller names in ``supersedes``.
    - Confidence is capped by evidence (``CAP_*``); no evidence refuses.
    - ``scrub.py`` runs on every body; a secret shape refuses the write.
    - Emits ``knowledge.learning_recorded`` ``{node_id, learning_kind, run_id, step}``.
    - ``links``: hook-only extra edges, ``{"fixed_by_from": [test_failure ids]}``.
    """
    kind = str(learning_kind or "").strip().lower()
    if kind not in LEARNING_KINDS:
        raise LearningRefused(f"learning_kind is one of {', '.join(sorted(LEARNING_KINDS))}.")
    title = _clean(title, field="title", limit=MAX_TITLE, required=True)
    statement = _clean(statement, field="statement", limit=MAX_STATEMENT, required=True)
    clean_detail = _detail(kind, detail)
    kept_evidence = _evidence(evidence)
    capped = _confidence(confidence, kept_evidence)
    resolved_about, code_ids, rule_ids = _about(connection, project_id, about)
    _STORE._org_id(connection, project_id, None)
    prov = provenance(connection, project_id, observed)
    from ..llm_telemetry import GENERAL_ROLE

    # BOSS itself (no agent addressed) writes as the system, its provenance still saying so.
    role = prov["agent_role"] if prov["agent_role"] != GENERAL_ROLE else None
    actor_kind = "agent" if role else "system"
    who = role or "system"

    signature = clean_detail.get("error_signature") if kind == "test_failure" else None
    if kind == "test_failure" and signature:
        material = normalise(" ".join(p for p in (str(clean_detail.get("test") or ""), str(signature)) if p))
    elif kind == "decision" and clean_detail.get("question") and clean_detail.get("chosen"):
        # The same choice on the same question is one decision seen again, whatever the words.
        material = normalise(f"{clean_detail['question']} -> {clean_detail['chosen']}")
    else:
        material = normalise(statement)
    about_keys = sorted(resolved_about["symbols"]) + sorted(resolved_about["rules"]) + sorted(resolved_about["areas"])
    dedupe_key = _hash(kind, material, about_keys)
    subject_field = SUBJECT_FIELDS.get(kind)
    subject = normalise(str(clean_detail.get(subject_field) or "")) if subject_field else ""
    subject_key = _hash(kind, subject, about_keys) if subject else None
    node_key = f"learning:{kind}:{dedupe_key}"
    run_ref = anchor_ref(prov)

    existing = connection.execute(text(
        "SELECT id, attrs, valid_to FROM kg_nodes WHERE project_id = :p AND kind = :k AND node_key = :key"),
        {"p": project_id, "k": kind, "key": node_key}).mappings().first()
    repeat = existing is not None
    if repeat:
        node_id = existing["id"]
        old = _attrs(existing["attrs"])
        merged_evidence = list(old.get("evidence") or [])
        for item in kept_evidence:
            if not any(e.get("type") == item["type"] and e.get("ref") == item["ref"]
                       and e.get("excerpt") == item["excerpt"] for e in merged_evidence):
                merged_evidence.append(item)
        merged_evidence = merged_evidence[-MAX_KEPT_EVIDENCE:]
        new_conf = max(float(old.get("confidence") or 0.0), min(capped, confidence_cap(merged_evidence)))
        same_run = run_ref and run_ref == old.get("last_seen_run")
        patch = {
            "evidence": merged_evidence,
            "confidence": round(new_conf, 3),
            "injectable": new_conf >= INJECT_BELOW,
            "seen_count": int(old.get("seen_count") or 1) + (0 if same_run else 1),
            "last_seen_run": run_ref or old.get("last_seen_run"),
            "last_seen_at": prov["at"],
        }
        connection.execute(text(
            "UPDATE kg_nodes SET attrs = attrs || CAST(:patch AS JSONB), last_seen = clock_timestamp(), valid_to = NULL "
            "WHERE id = :id"), {"patch": json.dumps(patch), "id": node_id})
    else:
        attrs = {
            "learning_kind": kind, "statement": statement, "detail": clean_detail,
            "about": resolved_about, "evidence": kept_evidence, "confidence": capped,
            "injectable": capped >= INJECT_BELOW,
            "dedupe_key": dedupe_key, "subject_key": subject_key,
            "seen_count": 1, "last_seen_run": run_ref, "last_seen_at": prov["at"],
            "provenance": prov,
        }
        node_id = _STORE.upsert_node(connection, project_id=project_id, kind=kind, node_key=node_key,
                                     name=title, body=statement, trust_tier="inferred",
                                     actor_kind=actor_kind, actor_ref=who, attrs=attrs, source="learning")

    edge_attrs = {"step": prov["step"], "at": prov["at"], "run_id": run_ref}
    try:
        run_id = run_anchor(connection, project_id, run_ref) if run_ref else None
    except (LookupError, PermissionError):             # a conversation that no longer exists
        run_id = None
    if run_id:
        _STORE.upsert_edge(connection, project_id=project_id, src_id=run_id, dst_id=node_id, kind="produced",
                           actor_kind=actor_kind, actor_ref=who, attrs=edge_attrs)
    if role and not repeat:
        agent_id = _anchor(connection, project_id, "agent", f"agent:{role}", prov["agent_name"] or role,
                           {"role_key": role})
        _STORE.upsert_edge(connection, project_id=project_id, src_id=node_id, dst_id=agent_id, kind="proposed_by",
                           actor_kind=actor_kind, actor_ref=who, attrs=edge_attrs)
    for target in code_ids + rule_ids:
        _STORE.upsert_edge(connection, project_id=project_id, src_id=node_id, dst_id=target, kind="about",
                           actor_kind=actor_kind, actor_ref=who)
    if kind == "investigation":
        for entry in clean_detail.get("ruled_out") or []:
            target = _ruled_out_target(connection, project_id, entry)
            if target:
                _STORE.upsert_edge(connection, project_id=project_id, src_id=node_id, dst_id=target,
                                   kind="ruled_out", actor_kind=actor_kind, actor_ref=who,
                                   attrs={"reason": entry.get("reason") or None,
                                          "hypothesis": entry.get("hypothesis")})
    for src in (links or {}).get("fixed_by_from") or []:
        try:
            _STORE.upsert_edge(connection, project_id=project_id, src_id=str(src), dst_id=node_id,
                               kind="fixed_by", actor_kind=actor_kind, actor_ref=who, attrs=edge_attrs)
        except (LookupError, PermissionError):
            continue
    if not repeat:
        _contradicted(connection, project_id, node_id, kind, subject_key, supersedes, actor_kind)
    # What the learning cites ([L:…] / [R:…] in its words) was used to learn it: `cited` (a cited
    # precedent or fix `reused`), on the same anchor — a run, or the conversation.
    cited = [i for i in citations(title, statement, clean_detail) if i != node_id]
    if cited and run_ref:
        try:
            with connection.begin_nested():
                conversation = prov.get("agent_thread_id") if run_ref == prov.get("agent_thread_id") else None
                log_use(connection, project_id=project_id, ids=cited, use_kind="cited",
                        run_id=None if conversation else run_ref, thread_id=conversation,
                        act_id=prov.get("act_id"), step=prov["step"], agent_role=prov["agent_role"])
        except Exception:  # noqa: BLE001 — a citation is a label, never a reason to refuse
            LOG.info("learning %s: citations not logged", node_id, exc_info=True)
    _STORE.audit(connection, project_id=project_id, action="knowledge.learning.record", entity_id=node_id,
                 actor_kind=actor_kind, actor_ref=who,
                 detail={"learning_kind": kind, "repeat": repeat, "run_id": run_ref, "step": prov["step"]})
    _emit(connection, project_id, who, {"node_id": node_id, "learning_kind": kind, "run_id": run_ref,
                                        "step": prov["step"], "repeat": repeat})
    from .age import after_write

    after_write(connection, project_id)
    return item_payload(connection, project_id, node_id)


def reinforce(
    connection: Connection,
    *,
    project_id: str,
    node_id: str,
    incident_id: str,
    incident_ref: Optional[str] = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    actor_role: Optional[str] = None,
) -> dict[str, Any]:
    """THE SAME LESSON, LEARNED AGAIN (a squad judged today's incident to have the precedent's
    cause): strengthen it instead of writing a second one. ``seen_count`` + 1, the evidence is
    merged, the incident joins ``attrs.incidents``, confidence may rise within its evidence cap,
    and the node stays the one every earlier reuse points at (its reuse count is kept)."""
    row = connection.execute(text(
        "SELECT id, kind, attrs, valid_to FROM kg_nodes WHERE project_id = :p AND id = :i"),
        {"p": project_id, "i": node_id}).mappings().first()
    if row is None or row["kind"] not in CUMULATIVE_KINDS:
        raise LearningRefused(f"'{node_id}' is not a precedent of this project.")
    old = _attrs(row["attrs"])
    kept = _evidence(evidence) if evidence else []
    merged = list(old.get("evidence") or [])
    for item in kept:
        if not any(e.get("type") == item["type"] and e.get("ref") == item["ref"] for e in merged):
            merged.append(item)
    merged = merged[-MAX_KEPT_EVIDENCE:]
    incidents = [dict(i) for i in old.get("incidents") or [] if isinstance(i, Mapping)]
    if old.get("incident_id") and not any(i.get("id") == old["incident_id"] for i in incidents):
        incidents.insert(0, {"id": old["incident_id"], "ref": old.get("incident_ref")})
    fresh = not any(i.get("id") == incident_id for i in incidents)
    if fresh:
        incidents.append({"id": incident_id, "ref": incident_ref})
    conf = max(float(old.get("confidence") or 0.0),
               min(1.0, float(old.get("confidence") or 0.0) + 0.05, confidence_cap(merged)))
    patch = {"evidence": merged, "incidents": incidents[-50:], "confidence": round(conf, 3),
             "injectable": conf >= INJECT_BELOW,
             "seen_count": int(old.get("seen_count") or 1) + (1 if fresh else 0),
             "last_seen_at": _now_iso(), "last_seen_run": incident_id}
    connection.execute(text(
        "UPDATE kg_nodes SET attrs = attrs || CAST(:patch AS JSONB), last_seen = clock_timestamp(), "
        "valid_to = NULL WHERE id = :id"), {"patch": json.dumps(patch), "id": node_id})
    who = actor_role or "system"
    _STORE.audit(connection, project_id=project_id, action="knowledge.learning.reinforce", entity_id=node_id,
                 actor_kind="agent" if actor_role else "system", actor_ref=who,
                 detail={"incident_id": incident_id, "seen_count": patch["seen_count"]})
    _emit(connection, project_id, who, {"node_id": node_id, "learning_kind": row["kind"],
                                        "run_id": incident_id, "step": "postmortem", "repeat": True})
    return item_payload(connection, project_id, node_id)


def log_incident_use(
    connection: Connection,
    *,
    project_id: str,
    incident_id: str,
    ids: Iterable[str],
    use_kind: str,
    agent_role: Optional[str] = None,
    step: str = "incident_squad",
) -> list[str]:
    """An incident used these learnings (``kg_usage``): the incident is the unit of work, as a
    run is for a build — "Learned on the job" counts it as one reuse, and the Graph's usage line
    and edges read the same rows. Only learnings of this project are logged."""
    wanted = [str(i) for i in ids if str(i or "").strip()]
    if not wanted:
        return []
    found = [str(r) for r in connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND id = ANY(:ids) AND kind = ANY(:kinds)"),
        {"p": project_id, "ids": wanted, "kinds": sorted(LEARNING_KINDS)}).scalars()]
    if found:
        _STORE.log_usage(connection, project_id=project_id, run_id=str(incident_id), step=step,
                         fact_ids=found, use_kind=use_kind, agent_role=agent_role)
    return found


def _ruled_out_target(connection: Connection, project_id: str, entry: Mapping[str, Any]) -> Optional[str]:
    ref = str(entry.get("id") or entry.get("symbol") or "").strip()
    if not ref:
        return None
    row = connection.execute(text(
        "SELECT id FROM kg_nodes WHERE project_id = :p AND id = :r AND kind IN ('root_cause', 'symbol', 'file') "
        "LIMIT 1"), {"p": project_id, "r": ref}).scalar()
    if row:
        return str(row)
    node = _resolve_code(connection, project_id, ref)
    return node["id"] if node else None


def _contradicted(connection: Connection, project_id: str, node_id: str, kind: str,
                  subject_key: Optional[str], named: Optional[str], actor_kind: str) -> None:
    """Close what the new learning contradicts (spec §4.3): the open learning it names, and any
    open one of its kind with the same subject on the same code. Both stay in history."""
    targets: list[str] = []
    if named:
        rid = str(named).strip()
        rid = rid[2:] if rid.startswith(("L:", "l:")) else rid
        row = connection.execute(text(
            "SELECT id, trust_tier FROM kg_nodes WHERE project_id = :p AND id = :i AND kind = ANY(:kinds) "
            "AND valid_to IS NULL"), {"p": project_id, "i": rid, "kinds": sorted(LEARNING_KINDS)}).mappings().first()
        if row is None:
            raise LearningRefused(f"'{named}' is not an open learning of this project.")
        targets.append(row["id"])
    if subject_key:
        same_subject = [str(i) for i in connection.execute(text(
            "SELECT id FROM kg_nodes WHERE project_id = :p AND kind = :k AND id <> :i AND valid_to IS NULL "
            "AND trust_tier = 'inferred' AND attrs->>'subject_key' = :s"),
            {"p": project_id, "k": kind, "i": node_id, "s": subject_key}).scalars().all()]
        if kind in CUMULATIVE_KINDS:
            # A precedent is what happened in ONE incident: a second incident with the same
            # symptom and another cause does not make the first wrong. Both stay open, linked,
            # and the first keeps its reuse count (it used to be closed and read "reused 0").
            for other in same_subject:
                _STORE.upsert_edge(connection, project_id=project_id, src_id=node_id, dst_id=other,
                                   kind="correlates_with", actor_kind=actor_kind,
                                   attrs={"why": "same symptom, another cause"})
        else:
            targets += same_subject
    for old in dict.fromkeys(targets):
        _STORE.close_validity(connection, project_id=project_id, valid_to=datetime.now(timezone.utc),
                              actor_kind=actor_kind, node_id=old, superseded_by=node_id)
        connection.execute(text(
            "UPDATE kg_nodes SET attrs = attrs || CAST(:p AS JSONB) WHERE id = :id"),
            {"p": json.dumps({"superseded_by": node_id}), "id": old})


def _emit(connection: Connection, project_id: str, actor: str, data: dict) -> None:
    try:
        from ..core import emit_event

        with connection.begin_nested():
            emit_event(connection, project_id, "knowledge.learning_recorded", actor or "boss",
                       "kg_node", data["node_id"], data)
    except Exception:  # noqa: BLE001 — the live feed reads the node itself; an event is a nudge
        LOG.info("learning event not emitted for %s", data.get("node_id"), exc_info=True)


# --- reading one back -----------------------------------------------------------------------------
def item_payload(connection: Connection, project_id: str, node_id: str) -> dict[str, Any]:
    """A learned item as the view payload (``KgLearningItem``)."""
    row = connection.execute(text(
        "SELECT id, project_id, kind, node_key, name, body, attrs, trust_tier, created_by, first_seen, "
        "recorded_at, last_seen, valid_to FROM kg_nodes WHERE id = :i"), {"i": node_id}).mappings().first()
    if row is None:
        raise LookupError(f"learning not found: {node_id}")
    if row["project_id"] != project_id:
        raise PermissionError(f"learning {node_id} is not in project {project_id}")
    return payload_of(connection, dict(row))


def payload_of(connection: Optional[Connection], row: Mapping[str, Any]) -> dict[str, Any]:
    attrs = _attrs(row.get("attrs"))
    prov = _attrs(attrs.get("provenance"))
    kind = str(attrs.get("learning_kind") or row["kind"])
    who = prov.get("agent_name") or prov.get("agent_role")
    sub = " · ".join(str(p) for p in (who, prov.get("step")) if p) or kind.replace("_", " ")
    created_by = str(row.get("created_by") or "system")
    events = [{"at": prov.get("at") or _iso(row.get("first_seen")), "what": "recorded",
               "by": who or (None if created_by == "system" else created_by)}]
    if int(attrs.get("seen_count") or 1) > 1:
        events.append({"at": attrs.get("last_seen_at") or _iso(row.get("last_seen")), "what": "seen again",
                       "by": attrs.get("last_seen_run")})
    if row.get("valid_to") is not None:
        events.append({"at": _iso(row.get("valid_to")),
                       "what": "superseded" if attrs.get("superseded_by") else "closed",
                       "by": attrs.get("superseded_by")})
    about = _attrs(attrs.get("about"))
    return {
        "node": {"id": row["id"], "key": row.get("node_key") or row["id"], "kind": kind, "layer": "learning",
                 "label": row.get("name") or kind, "sub": sub, "trust_tier": row.get("trust_tier") or "inferred"},
        "learning_kind": kind,
        "title": row.get("name") or "",
        "statement": attrs.get("statement") or row.get("body") or "",
        "detail": _attrs(attrs.get("detail")),
        "about": {"symbols": list(about.get("symbols") or []), "rules": list(about.get("rules") or []),
                  "areas": list(about.get("areas") or [])},
        "evidence": [{"type": e.get("type"), "ref": e.get("ref") or "", "excerpt": e.get("excerpt")}
                     for e in attrs.get("evidence") or [] if isinstance(e, Mapping)],
        "confidence": float(attrs.get("confidence") or 0.0),
        "seen_count": int(attrs.get("seen_count") or 1),
        "last_seen_run": attrs.get("last_seen_run"),
        "provenance": {
            "created_by": created_by,
            "created_by_kind": "agent" if prov.get("agent_role") and created_by == prov.get("agent_role")
            else ("system" if created_by == "system" else "person"),
            "agent_role": prov.get("agent_role"), "agent_name": prov.get("agent_name"),
            "agent_id": prov.get("agent_id"),
            "run_id": prov.get("run_id"), "workflow_run_id": prov.get("workflow_run_id"),
            "code_task_run_id": prov.get("code_task_run_id"), "model_id": prov.get("model_id"),
            "step": prov.get("step"), "at": prov.get("at") or _iso(row.get("first_seen")),
            "approved_by": None, "approved_at": None, "version": None, "previous_id": None,
            "events": events,
        },
        "at": attrs.get("last_seen_at") or prov.get("at") or _iso(row.get("last_seen")) or _now_iso(),
    }


# --- what later runs use --------------------------------------------------------------------------
def citations(*texts: Any, bare: bool = False) -> list[str]:
    """Node ids an output cites: the ``[L:<id>]`` / ``[R:<id>]`` markers the pack quotes, in any
    text or structure an agent wrote (and bare ``kgn_`` ids with ``bare``)."""
    found: list[str] = []
    for value in texts:
        if value is None:
            continue
        raw = value if isinstance(value, str) else json.dumps(value, default=str)
        for match in CITATION.finditer(raw):
            found.append(match.group("id"))
        if bare:
            for match in _NODE_ID.finditer(raw):
                found.append(match.group(0))
    return list(dict.fromkeys(found))


def log_use(
    connection: Connection,
    *,
    project_id: str,
    ids: Iterable[str],
    use_kind: str,
    run_id: Optional[str] = None,
    step: Optional[str] = None,
    agent_role: Optional[str] = None,
    thread_id: Optional[str] = None,
    act_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> list[str]:
    """Record that a run used these items (``kg_usage``, spec §2.5) and draw the run's edges to
    them now. Ids that are not rules, learnings, facts or document nodes (clauses, data-quality
    rules, thresholds, CDEs) of this project are ignored (a cited id
    is the model's word). A cited precedent or fix is also ``reused``. Returns the ids logged.

    With no run, the use is the conversation's (a chat turn, or a press in its thread): the
    thread (``thread_id`` or the LLM context's ``agent_thread_id``) is the anchor, with the turn's
    act and, when known, the message that answered (learning_conversations). With neither, nothing
    is logged: a use nobody can be shown to have made is not recorded."""
    ctx = _context()
    refs = run_refs(connection, run_id or ctx.get("run_id"))
    thread = None
    if not refs["run_id"]:
        thread = str(thread_id or ctx.get("agent_thread_id") or "").strip() or None
        if not thread:
            return []
        refs["run_id"] = thread
    ref = refs["run_id"]
    wanted = [str(i).strip() for i in ids if str(i or "").strip()]
    if not wanted:
        return []
    rows = connection.execute(text(
        "SELECT id, kind FROM kg_nodes WHERE project_id = :p AND id = ANY(:ids) "
        "AND (kind = 'rule' OR kind = ANY(:kinds))"),
        {"p": project_id, "ids": wanted,
         "kinds": sorted(LEARNING_KINDS | CITABLE_DOCUMENT_KINDS)}).mappings().all()
    kinds = {r["id"]: r["kind"] for r in rows}
    found = [i for i in dict.fromkeys(wanted) if i in kinds]
    if not found:
        return []
    stage = str(step or ctx.get("step") or ctx.get("stage") or ("chat" if thread else "agent"))
    # The addressed agent of a chat turn (speaker_role) before the stage's cost role.
    role = agent_role or ctx.get("speaker_role") or ctx.get("role_key")
    common = dict(project_id=project_id, run_id=ref, step=stage,
                  workflow_run_id=None if thread else (refs["workflow_run_id"] or ctx.get("workflow_run_id")),
                  code_task_run_id=refs["code_task_run_id"], agent_role=role,
                  agent_thread_id=thread, act_id=(act_id or ctx.get("agent_act_id")) if thread else None,
                  message_id=message_id if thread else None)
    _STORE.log_usage(connection, fact_ids=found, use_kind=use_kind, **common)
    if use_kind == "cited":
        reused = [i for i in found if kinds[i] in ("precedent", "fix")]
        if reused:
            _STORE.log_usage(connection, fact_ids=reused, use_kind="reused", **common)
    if use_kind != "shown":
        sync_usage_edges(connection, project_id=project_id, run_ref=anchor_ref(refs))
    return found


def sync_usage_edges(connection: Connection, *, project_id: str, run_ref: Optional[str] = None) -> int:
    """Derive the ``consulted`` / ``reused`` edges from ``kg_usage`` (spec §2.5), for one run
    (any of its run, code task run or workflow run ids) or every run of the project. A ``shown``
    row draws nothing. Idempotent: an edge is unique per (project, run, item, kind) and a repeat
    only refreshes it. Returns how many edges were written or refreshed."""
    params: dict[str, Any] = {"p": project_id}
    where = "u.project_id = :p AND u.use_kind IN ('consulted', 'cited', 'reused')"
    if run_ref:
        where += " AND (u.run_id = :r OR u.code_task_run_id = :r OR u.workflow_run_id = :r)"
        params["r"] = run_ref
    rows = connection.execute(text(
        f"""
        SELECT coalesce(u.code_task_run_id, CASE WHEN u.run_id LIKE 'crun%' THEN u.run_id END,
                        u.workflow_run_id, u.run_id) AS ref,
               u.fact_id, u.use_kind, min(u.step) AS step, max(u.agent_role) AS agent_role,
               max(u.created_at) AS at, max(u.agent_thread_id) AS thread_id, max(u.message_id) AS message_id
        FROM kg_usage u WHERE {where}
        GROUP BY 1, 2, 3
        """), params).mappings().all()
    written = 0
    anchors: dict[str, str] = {}
    for row in rows:
        ref = row["ref"]
        if not ref:
            continue
        if ref not in anchors:
            try:
                anchors[ref] = run_anchor(connection, project_id, ref)
            except (LookupError, PermissionError):     # a conversation that no longer exists
                anchors[ref] = None
        if not anchors[ref]:
            continue
        kind = "reused" if row["use_kind"] == "reused" else "consulted"
        how = {"consulted": "tool", "cited": "cited", "reused": "cited"}[row["use_kind"]]
        role = row["agent_role"]
        attrs = {"how": how, "step": row["step"], "run_id": ref, "agent_role": role,
                 "agent_name": _use_name(connection, project_id, role),
                 "at": _iso(row["at"])}
        if row["thread_id"]:
            # Used in a conversation: "used by <agent> in conversation <title> (when)".
            attrs.update({"conversation": True, "thread_id": row["thread_id"], "message_id": row["message_id"]})
        if kind == "consulted":
            # A citation outranks a tool hit: "cited" stays once a run cited it.
            prior = connection.execute(text(
                "SELECT attrs->>'how' FROM kg_edges WHERE project_id = :p AND src_id = :s AND dst_id = :d "
                "AND kind = 'consulted'"), {"p": project_id, "s": anchors[ref], "d": row["fact_id"]}).scalar()
            if prior == "cited":
                attrs["how"] = "cited"
        try:
            _STORE.upsert_edge(connection, project_id=project_id, src_id=anchors[ref], dst_id=row["fact_id"],
                               kind=kind, actor_kind="system", attrs=attrs)
            written += 1
        except (LookupError, PermissionError):
            continue
    return written


def _use_name(connection: Connection, project_id: str, role: Optional[str]) -> Optional[str]:
    if not role:
        return None
    from ..llm_telemetry import GENERAL_NAME, GENERAL_ROLE

    return GENERAL_NAME if role == GENERAL_ROLE else _agent_name(connection, project_id, role)


# --- the hooks ------------------------------------------------------------------------------------
def write_back(
    connection: Connection,
    *,
    project_id: str,
    hook: str,
    observed: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Record what a pipeline hook observed; return the items written (possibly none).

    One call per hook (``HOOKS``), each a single line at its call site:

    - ``build_stage`` / ``repair_checkpoint``: a ``test_failure`` (failing test + error signature
      from the gate output) when a repair round starts; a ``fix`` linked ``fixed_by`` when the
      next gate passes, ``about`` the files and symbols the repair changed.
    - ``engine_turn``: a ``workaround`` when a build/deploy command failed and a later command in
      the same stage succeeded with a changed approach.
    - ``step_complete``: the step's typed outputs — ``investigation`` from investigate steps,
      ``review_finding`` from review steps, ``decision`` from typed decisions,
      ``root_cause``/``fix``/``precedent`` from ``incident_report`` — and the step's ``learned[]``.
    - ``agent_decide``: a ``decision`` with the chosen action's reasons and the reads it relied on.

    Every item goes through ``record()``. A hook never raises into the pipeline: a refused item
    (no evidence, a secret) is skipped with its reason logged.
    """
    if connection is None or hook not in HOOKS or not str(project_id or "").strip():
        return []
    try:
        planner = _PLANNERS[hook]
        with connection.begin_nested():
            plan = planner(connection, project_id, dict(observed or {}))
    except Exception:  # noqa: BLE001 — a hook never fails the work it watches
        LOG.warning("learning hook %s could not read what it observed", hook, exc_info=True)
        return []
    written: list[dict[str, Any]] = []
    for item in plan.get("items") or []:
        try:
            with connection.begin_nested():
                written.append(record(connection, project_id=project_id, **item))
        except LearningRefused as refused:
            LOG.info("learning hook %s skipped an item: %s", hook, refused)
        except Exception:  # noqa: BLE001
            LOG.warning("learning hook %s could not record an item", hook, exc_info=True)
    for use in plan.get("uses") or []:
        try:
            with connection.begin_nested():
                log_use(connection, project_id=project_id, **use)
        except Exception:  # noqa: BLE001
            LOG.info("learning hook %s could not log a use", hook, exc_info=True)
    after = plan.get("after")
    if callable(after):
        try:
            with connection.begin_nested():
                after(connection, written)
        except Exception:  # noqa: BLE001
            LOG.info("learning hook %s could not finish", hook, exc_info=True)
    return written


@contextmanager
def _open(target: Any) -> Iterator[Connection]:
    """A connection for a hook called with an engine (its own transaction) or a connection (a
    savepoint inside the caller's)."""
    if hasattr(target, "begin_nested") and hasattr(target, "execute"):
        yield target
        return
    with target.begin() as connection:
        yield connection


def observe(target: Any, *, project_id: str, hook: str, **observed: Any) -> list[dict[str, Any]]:
    """``write_back`` for a call site that holds an engine, not a connection (the build): its own
    short transaction. Never raises; returns the items written."""
    if hook == "build_stage":
        # The stage boundary closes the engine-turn window of the stage that just ended: a
        # failed command not followed by a working one in that stage is not a workaround.
        _TURNS.close_stage(str(observed.get("run_id") or ""))
        return []
    if target is None:
        return []
    try:
        with _open(target) as connection:
            return write_back(connection, project_id=project_id, hook=hook, observed=observed)
    except Exception:  # noqa: BLE001 — a hook never fails the work it watches
        LOG.warning("learning hook %s failed to open a transaction", hook, exc_info=True)
        return []


# ---- repair checkpoint: a gate that failed, then the fix that made it pass ----------------------
#: What a test run prints when it says what went wrong, most telling first: a raised error
#: (`LinkedAccountRequired: ...`, `TypeError: ...`, `E   AssertionError ...`), a line naming an
#: error, then a failed assertion or test line.
_RAISED = re.compile(r"(?m)^\s*(?:E\s+)?(?:[A-Za-z_][\w.]*\.)?[A-Z][a-z0-9]+[A-Za-z0-9]*:\s+\S.*$")
_ERRORISH = re.compile(r"(?m)^.*\b(?:[A-Za-z_][\w.]*(?:Error|Exception))\b.*$")
_FAILED = re.compile(r"(?m)^.*\b(?:FAIL(?:ED)?|assert(?:ion)?|expected)\b.*$", re.IGNORECASE)


def error_signature(output: str) -> str:
    """The line of a test run's output that says what went wrong: a raised error, else a line
    naming one, else a failed assertion or test, else the last line it printed."""
    text_ = str(output or "")
    for pattern in (_RAISED, _ERRORISH, _FAILED):
        match = pattern.search(text_)
        if match:
            return _SPACES.sub(" ", match.group(0)).strip()[:300]
    line = next((l for l in reversed(text_.splitlines()) if l.strip()), "")
    return _SPACES.sub(" ", line).strip()[:300]


def _open_failures(connection: Connection, project_id: str, run_ref: str) -> list[str]:
    return [str(i) for i in connection.execute(text(
        """
        SELECT n.id FROM kg_nodes n
        JOIN kg_edges p ON p.dst_id = n.id AND p.kind = 'produced'
        JOIN kg_nodes r ON r.id = p.src_id AND r.node_key = :rk
        WHERE n.project_id = :p AND n.kind = 'test_failure' AND n.valid_to IS NULL
          AND NOT EXISTS (SELECT 1 FROM kg_edges f WHERE f.src_id = n.id AND f.kind = 'fixed_by'
                            AND f.attrs->>'run_id' = :ref)
        ORDER BY n.first_seen
        """), {"p": project_id, "rk": f"run:{run_ref}", "ref": run_ref}).scalars().all()]


def _plan_repair(connection: Connection, project_id: str, seen: dict) -> dict:
    run_ref = str(seen.get("run_id") or "").strip()
    if not run_ref:
        return {}
    attempt = int(seen.get("attempt") or 0)
    step = str(seen.get("stage") or ("implement" + (f"-repair{attempt}" if attempt else "")))
    obs = {"run_id": run_ref, "step": step}
    command = str(seen.get("command") or "the gate's tests")[:300]
    files = [str(f) for f in seen.get("files") or [] if str(f or "").strip()][:MAX_LIST]
    uses = []
    cited = citations(seen.get("summary"))
    if cited:
        uses.append({"ids": cited, "use_kind": "cited", "run_id": run_ref, "step": step})
    if not seen.get("passed"):
        if seen.get("tests_passed"):
            # The gate failed for another reason (no implementation, an edited test): no test
            # failed, so there is no test failure to learn from.
            return {"uses": uses}
        output = str(seen.get("output") or "")
        failing = [str(t) for t in seen.get("failing") or [] if str(t or "").strip()]
        signature = error_signature(output)
        if not (failing or signature):
            return {"uses": uses}
        test = failing[0] if failing else command
        item = {
            "learning_kind": "test_failure",
            "title": f"{test} fails" + (f": {signature[:120]}" if signature else ""),
            "statement": (f"{test} failed after attempt {attempt}" if attempt else f"{test} failed on the first attempt")
                         + (f" with {signature}" if signature else "") + ".",
            "detail": {"test": test, "error_signature": signature or test,
                       "cause": str(seen.get("cause") or "")},
            "about": {"symbols": files},
            "evidence": [{"type": "test", "ref": command, "excerpt": output[-MAX_EXCERPT:] or signature,
                          "passed": False, "observed": True}],
            "confidence": CAP_PASSING_TEST,
            "observed": obs,
        }
        return {"items": [item], "uses": uses}
    pending = _open_failures(connection, project_id, run_ref)
    if not pending:
        return {"uses": uses}
    tests = [str(r) for r in connection.execute(text(
        "SELECT coalesce(attrs->'detail'->>'test', name) FROM kg_nodes WHERE id = ANY(:i)"),
        {"i": pending}).scalars().all()]
    summary = str(seen.get("summary") or "").strip()
    change = summary[:600] or f"The repair changed {', '.join(files[:5]) or 'the implementation'}."
    item = {
        "learning_kind": "fix",
        "title": f"What made {tests[0] if tests else 'the failing tests'} pass",
        "statement": f"After attempt {attempt}, {', '.join(tests[:3]) or 'the failing tests'} passed: {change}"[:MAX_STATEMENT],
        "detail": {"change_summary": change, "files": files},
        "about": {"symbols": files},
        "evidence": [{"type": "test", "ref": command, "excerpt": f"passed on attempt {attempt}",
                      "passed": True, "observed": True}],
        "confidence": CAP_PASSING_TEST,
        "observed": obs,
        "links": {"fixed_by_from": pending},
    }
    return {"items": [item], "uses": uses}


# ---- engine turns: a command that failed, then one that worked with a changed approach -----------
def _command_head(command: str) -> str:
    """The program a shell command runs (the last segment of a chain, its first two words,
    without leading variable assignments): `cd app && NODE_ENV=x npm run build` -> `npm run`."""
    segment = re.split(r"&&|\|\||;|\|", str(command or ""))
    words = [w for w in (segment[-1] if segment else "").split() if "=" not in w or w.startswith("-")]
    return " ".join(words[:2]).strip()


class _TurnPairs:
    """Per run, the commands of the current stage that failed and are not yet worked around.
    Bounded and thread-safe: the engines stream turns from their own threads."""

    MAX_RUNS = 64
    MAX_FAILED = 12

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}

    def _state(self, run: str, stage: str) -> dict[str, Any]:
        state = self._runs.get(run)
        if state is None or state["stage"] != stage:
            if len(self._runs) >= self.MAX_RUNS and run not in self._runs:
                self._runs.pop(next(iter(self._runs)), None)
            state = {"stage": stage, "last_call": {}, "failed": []}
            self._runs[run] = state
        return state

    def close_stage(self, run: str) -> None:
        with self._lock:
            self._runs.pop(run, None)

    def feed(self, run: str, stage: str, turn: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        direction = turn.get("direction")
        tool = str(turn.get("tool_name") or "")
        with self._lock:
            state = self._state(run, stage)
            if direction == "tool_call":
                content = turn.get("content")
                command = content.get("command") if isinstance(content, Mapping) else None
                if isinstance(command, str) and command.strip():
                    state["last_call"][tool] = command.strip()
                return None
            if direction != "tool_result":
                return None
            command = state["last_call"].pop(tool, None)
            if not command:
                return None
            head = _command_head(command)
            if not head:
                return None
            if turn.get("ok") is False:
                excerpt = turn.get("content")
                excerpt = excerpt if isinstance(excerpt, str) else json.dumps(excerpt, default=str)
                state["failed"] = (state["failed"] + [{"head": head, "command": command,
                                                       "error": str(excerpt or "")[-MAX_EXCERPT:]}])[-self.MAX_FAILED:]
                return None
            if turn.get("ok") is True:
                for failed in reversed(state["failed"]):
                    if failed["head"] == head and failed["command"] != command:
                        state["failed"] = [f for f in state["failed"] if f["head"] != head]
                        return {"failed": failed, "worked": command}
                state["failed"] = [f for f in state["failed"] if f["head"] != head]
        return None


_TURNS = _TurnPairs()


def observe_turn(turn: Mapping[str, Any]) -> None:
    """The engine turn stream (harness_failures.check_turn, every engine): pair a failed shell
    command with a later one in the same stage that ran the same program differently and worked,
    and record the pair as a ``workaround``. Never raises; costs no model call."""
    try:
        if not isinstance(turn, Mapping) or turn.get("direction") not in ("tool_call", "tool_result"):
            return
        ctx = _context()
        run = str(ctx.get("run_id") or "")
        project_id = ctx.get("project_id")
        if not run or not project_id:
            return
        pair = _TURNS.feed(run, str(ctx.get("stage") or ""), turn)
        if pair is None or ctx.get("db_engine") is None:
            return
        observe(ctx["db_engine"], project_id=str(project_id), hook="engine_turn",
                run_id=run, step=ctx.get("stage"), **pair)
    except Exception:  # noqa: BLE001
        LOG.info("learning: an engine turn could not be read", exc_info=True)


def _plan_workaround(connection: Connection, project_id: str, seen: dict) -> dict:
    failed = seen.get("failed") or {}
    worked = str(seen.get("worked") or "").strip()
    if not failed or not worked:
        return {}
    error = error_signature(str(failed.get("error") or "")) or "it failed"
    problem = f"`{str(failed.get('command'))[:300]}` failed: {error}"
    return {"items": [{
        "learning_kind": "workaround",
        "title": f"{failed.get('head')}: what worked after it failed",
        "statement": f"`{str(failed.get('command'))[:200]}` failed ({error[:200]}); `{worked[:200]}` worked instead.",
        "detail": {"problem": problem, "workaround": f"`{worked[:500]}`"},
        "about": {},
        "evidence": [{"type": "log", "ref": f"{seen.get('run_id')} {seen.get('step') or ''}".strip(),
                      "excerpt": str(failed.get("error") or "")[-MAX_EXCERPT:] or error}],
        "confidence": CAP_LOG_ONLY,
        "observed": {"run_id": seen.get("run_id"), "step": seen.get("step")},
    }]}


# ---- step completion: the step's typed outputs and its learned[] ---------------------------------
def _plan_step(connection: Connection, project_id: str, seen: dict) -> dict:
    step = dict(seen.get("step") or {})
    template = dict(seen.get("template") or {})
    output = step.get("output")
    output = _attrs(output) if not isinstance(output, dict) else output
    context = _attrs(seen.get("context"))
    wfr = str(step.get("workflow_run_id") or "")
    step_id = str(step.get("step_id") or template.get("id") or "step")
    role = str(template.get("captain") or template.get("agent") or "").strip() or None
    binding = template.get("binding") or {}
    capability = str(binding.get("capability") or "")
    obs = {"run_id": wfr or None, "workflow_run_id": wfr or None, "step": step_id, "agent_role": role}
    ref = f"{wfr}/{step_id}" if wfr else step_id
    items: list[dict] = []

    for raw in (output.get("learned") or []) if isinstance(output.get("learned"), list) else []:
        if isinstance(raw, Mapping):
            items.append({**_tool_args(raw), "observed": obs})
    if output.get("kind") == "typed_decision" and output.get("answer"):
        question = str(((binding.get("typed_decision") or {}).get("question")) or template.get("label") or step_id)
        options = (binding.get("typed_decision") or {}).get("options") or {}
        reason = str(output.get("reason") or "").strip()
        if reason:
            items.append({
                "learning_kind": "decision",
                "title": f"{question[:120]}: {output['answer']}",
                "statement": f"Decided {output['answer']} for “{question[:300]}”: {reason}",
                "detail": {"question": question, "chosen": str(output["answer"]), "reasons": [reason],
                           "alternatives": [str(k) for k in options if str(k) != str(output["answer"])]},
                "about": {},
                "evidence": [{"type": "log", "ref": ref, "excerpt": reason}],
                "confidence": float(output.get("confidence") or 0.5),
                "observed": obs,
            })
    if capability == "investigate_incident" and output.get("root_cause"):
        items += _investigation_items(connection, project_id, output, ref, obs)
    if capability == "incident_report":
        items += _report_items(context, output, ref, obs)
    for finding in _findings(output):
        items.append({**finding, "observed": obs, "evidence": finding.get("evidence") or
                      [{"type": "log", "ref": ref, "excerpt": finding["statement"][:MAX_EXCERPT]}]})
    cited = list(dict.fromkeys([str(i) for key in ("cited_fact_ids", "cited_rule_ids", "cited_learning_ids")
                                for i in (output.get(key) or []) if str(i or "").strip()]
                               + citations(output)))
    uses = [{"ids": cited, "use_kind": "cited", "run_id": wfr or None, "step": step_id, "agent_role": role}] \
        if cited and wfr else []
    return {"items": items, "uses": uses}


def _tool_args(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A ``learned[]`` entry has ``record_learning``'s arguments."""
    return {
        "learning_kind": raw.get("learning_kind") or raw.get("kind"),
        "title": raw.get("title"), "statement": raw.get("statement"),
        "detail": raw.get("detail") if isinstance(raw.get("detail"), Mapping) else {},
        "about": raw.get("about") if isinstance(raw.get("about"), Mapping) else {},
        "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), list) else [],
        "confidence": raw.get("confidence") if raw.get("confidence") is not None else 0.5,
        "supersedes": raw.get("supersedes"),
    }


#: What each basis of incident_response.hypothesis says was looked at and rejected.
_RULED_OUT_BY_BASIS = {
    "release_changed_failing_frame": [],
    "release_before_onset": [{"hypothesis": "the failing code is in the release's diff",
                              "reason": "the failing frame is not in a file that release changed"}],
    "no_release_near_onset": [{"hypothesis": "a release BOSS shipped caused it",
                               "reason": "no release went out shortly before the onset"}],
}


def _investigation_items(connection: Connection, project_id: str, output: Mapping[str, Any],
                         ref: str, obs: dict) -> list[dict]:
    hyp = _attrs(output.get("hypothesis"))
    basis = str(hyp.get("basis") or "")
    tried = [f"the app's logs ({output.get('logs_word')})"] * bool(output.get("logs_word")) + \
            [f"the release diff ({output.get('diff_word')})"] * bool(output.get("diff_word"))
    ruled_out = list(_RULED_OUT_BY_BASIS.get(basis, []))
    files = [str(hyp["suspect_file"])] if hyp.get("suspect_file") else []
    evidence = [{"type": "log", "ref": ref, "excerpt": str(output.get("root_cause"))[:MAX_EXCERPT]}]
    if hyp.get("suspect_commit"):
        evidence.append({"type": "diff", "ref": str(hyp["suspect_commit"]), "excerpt": hyp.get("suspect_file")})
    confidence = float(hyp.get("confidence") or 0.5)
    items = [{
        "learning_kind": "investigation",
        "title": f"Investigated: {str(output.get('title') or 'the incident')[:150]}",
        "statement": f"Found: {output['root_cause']}"
                     + ("".join(f" Ruled out {r['hypothesis']} ({r['reason']})." for r in ruled_out)),
        "detail": {"tried": tried or ["the incident's evidence"], "ruled_out": ruled_out,
                   "found": str(output["root_cause"])},
        "about": {"symbols": files},
        "evidence": evidence, "confidence": confidence, "observed": obs,
    }, {
        "learning_kind": "root_cause",
        "title": str(output.get("title") or "Root cause")[:MAX_TITLE],
        "statement": str(output["root_cause"]),
        "detail": {"basis": basis, "exception": hyp.get("exception"), "location": hyp.get("location"),
                   "failing_route": hyp.get("failing_route"), "suspect_commit": hyp.get("suspect_commit")},
        "about": {"symbols": files},
        "evidence": evidence, "confidence": confidence, "observed": obs,
    }]
    return items


def _report_items(context: Mapping[str, Any], output: Mapping[str, Any], ref: str, obs: dict) -> list[dict]:
    symptom = str(context.get("title") or "").strip()
    cause = str(context.get("root_cause") or "").strip()
    if not (symptom and cause):
        return []
    fix = str(context.get("pull_request_url") or context.get("summary") or context.get("build_summary")
              or "the fix build").strip()
    applies = [str(v) for v in (context.get("service"), (context.get("app") or {}).get("name")
                                if isinstance(context.get("app"), Mapping) else None,
                                context.get("target_id")) if v]
    recovered = str(context.get("recovery_summary") or output.get("recovery_summary") or "").strip()
    evidence = [{"type": "metric", "ref": str(context.get("recovery_check_id") or ref),
                 "excerpt": recovered or None}] if (recovered or context.get("recovery_check_id")) else \
        [{"type": "log", "ref": ref, "excerpt": str(output.get("report_markdown") or cause)[:MAX_EXCERPT]}]
    return [{
        "learning_kind": "precedent",
        "title": f"Precedent: {symptom[:150]}",
        "statement": f"When {symptom}: the cause was {cause}; it was fixed by {fix}.",
        "detail": {"symptom": symptom, "cause_summary": cause, "fix_summary": fix,
                   "applies_to": applies, "reuse_count": 0},
        "about": {"symbols": [str(context["suspect_file"])] if context.get("suspect_file") else []},
        "evidence": evidence, "confidence": 0.75, "observed": obs,
    }]


def _findings(output: Mapping[str, Any]) -> list[dict]:
    """Review findings a review step's output lists (``findings``: title/summary/severity)."""
    out = []
    for raw in (output.get("findings") or []) if isinstance(output.get("findings"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        statement = str(raw.get("summary") or raw.get("detail") or raw.get("message") or raw.get("title") or "").strip()
        if not statement:
            continue
        where = [str(raw[k]) for k in ("file", "path", "symbol") if raw.get(k)]
        out.append({
            "learning_kind": "review_finding",
            "title": str(raw.get("title") or statement)[:MAX_TITLE],
            "statement": statement,
            "detail": {k: raw.get(k) for k in ("severity", "category", "file", "line") if raw.get(k) is not None},
            "about": {"symbols": where},
            "evidence": [{"type": "file", "ref": where[0], "excerpt": statement[:MAX_EXCERPT]}] if where else [],
            "confidence": 0.6,
        })
    return out


# ---- a domain agent's structured decision --------------------------------------------------------
def _plan_decide(connection: Connection, project_id: str, seen: dict) -> dict:
    chosen = str(seen.get("chosen") or "").strip()
    reason = str(seen.get("reason") or "").strip()
    relied = [e for e in seen.get("evidence") or [] if str(e or "").strip()]
    if not (chosen and reason and relied):
        # Nothing the agent relied on, nothing to learn from: a decision with no evidence is refused.
        return {}
    question = str(seen.get("question") or "").strip().splitlines()[0][:300] if seen.get("question") else chosen
    ref = str(seen.get("external_ref") or "decision")
    return {"items": [{
        "learning_kind": "decision",
        "title": f"{question[:120]}: {chosen}",
        "statement": f"Chose {chosen} for {ref}: {reason}",
        "detail": {"question": question, "chosen": chosen, "reasons": [reason],
                   "alternatives": [str(a) for a in seen.get("alternatives") or [] if str(a) != chosen]},
        "about": {},
        "evidence": [{"type": "log", "ref": ref, "excerpt": (e if isinstance(e, str) else json.dumps(e, default=str))[:MAX_EXCERPT]}
                     for e in relied[:5]],
        "confidence": float(seen.get("confidence") or 0.5),
        "observed": {"run_id": seen.get("run_id"), "step": seen.get("step") or "decide",
                     "agent_role": seen.get("agent_role")},
    }]}


def _plan_stage(connection: Connection, project_id: str, seen: dict) -> dict:
    _TURNS.close_stage(str(seen.get("run_id") or ""))
    return {}


_PLANNERS = {
    "build_stage": _plan_stage,
    "repair_checkpoint": _plan_repair,
    "engine_turn": _plan_workaround,
    "step_complete": _plan_step,
    "agent_decide": _plan_decide,
}
