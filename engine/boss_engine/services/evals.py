"""THE EVALUATION LOOP'S DATA: feedback on runs, datasets, examples, experiments (AP-P4).

docs/design/AGENT_PLATFORM.md §4.9. LangSmith's data model, on BOSS tables (db/schema.sql,
"TRACES AND THE EVALUATION LOOP"):

  feedback               every verdict on a run / span / act / thread: a person's, a rule's, the
                         decision model's (Jev), an opt-in LLM judge's — with what it cost
  datasets               versioned sets of examples, linked to an agent (`agent_key`)
  dataset_examples       input, reference (what good looks like), observed (what happened), where
                         it came from; PROPOSED until a person confirms it
  dataset_experiments    an agent version (or the battle runner's target) run over a dataset's
    + _results           confirmed examples, every result checked and kept with its run id

MISSES BECOME EXAMPLES (a pipeline, not a policy). A gate a person waived, a delivery a person
sent back (a failed acceptance), and later a step-recovery action, a supervisor steer or a
rejected decision call `propose_miss`: the example lands PROPOSED in the project's `misses`
dataset — the ANNOTATION INBOX (`inbox`). Nothing counts until a person confirms it
(`review_example`); a confirmed miss is part of every battle run after (`run_battle`) and fails it
until the agent answers it the way the reference says.

A CHECK IS DATA, NOT A KEYWORD LIST. `check_example` reads the example's `reference`:
  expected_verdict  the answer's `verdict` must be this (a gate / acceptance miss)
  equals            the whole answer must equal this
  any_of / never_execute / never_offer / no_writes / mentions_any / recovery_in / recovery_not_in
                    the battle-test checks (tests/agent-battle/battle.yaml), same meaning
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from sqlalchemy import text

from .core import emit_event, make_id

log = logging.getLogger(__name__)

FEEDBACK_SOURCES = ("human", "rule", "judge", "decision_model", "pairwise")
DATASET_KINDS = ("custom", "misses", "battle", "bench", "verifier")
EXAMPLE_SOURCES = ("span", "gate_waiver", "failed_acceptance", "battle_yaml", "bench", "manual",
                   "step_recovery", "rejected_decision", "negative_feedback")
SPLITS = ("train", "dev", "test")
MISSES = "misses"
BATTLE = "agent-battle"
#: The largest file body a captured miss keeps (a gate's preserved diff can be a whole app).
MAX_FILE_CHARS = 20_000
MAX_FILES = 40


class EvalError(ValueError):
    """Something the caller can fix, phrased for them. `status` is the HTTP answer."""

    def __init__(self, message: str, *, status: int = 422):
        super().__init__(message)
        self.status = status


def _j(value: Any) -> str:
    return json.dumps(value if value is not None else {}, default=str)


def _row(row: Any) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for key, value in list(out.items()):
        if hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


def _secrets_only(value: Any) -> Any:
    """Credentials out, everything else as it was: code and ids must survive (PII masking would
    rewrite a test's phone-number fixture into a different test)."""
    from .redaction import redact_secrets

    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {str(k): _secrets_only(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_secrets_only(v) for v in value]
    return value


# ── Feedback ───────────────────────────────────────────────────────────────────────────────────

def record_feedback(c: Any, project_id: str, *, key: str, source: str, created_by: str,
                    run_id: Optional[str] = None, span_id: Optional[str] = None,
                    act_id: Optional[str] = None, thread_id: Optional[str] = None,
                    score: Optional[float] = None, value: Any = None,
                    comment: Optional[str] = None, evaluator: Optional[str] = None,
                    evaluator_version: Optional[str] = None, agent_key: Optional[str] = None,
                    spec_version: Optional[int] = None, cost_usd_micros: int = 0) -> dict:
    if source not in FEEDBACK_SOURCES:
        raise EvalError(f"source must be one of {', '.join(FEEDBACK_SOURCES)}.")
    if not (run_id or span_id or act_id or thread_id):
        raise EvalError("feedback is ON something: give a run_id, span_id, act_id or thread_id.")
    if not str(key or "").strip():
        raise EvalError("feedback needs a key (what is being judged).")
    if span_id and not run_id:
        run_id = c.execute(text("SELECT run_id FROM trace_spans WHERE span_id = :s"),
                           {"s": span_id}).scalar()
    fid = make_id("fb")
    c.execute(text("""
        INSERT INTO feedback (id, project_id, run_id, span_id, act_id, thread_id, key, score,
            value, comment, source, evaluator, evaluator_version, agent_key, spec_version,
            cost_usd_micros, created_by)
        VALUES (:id, :p, :r, :s, :a, :t, :k, :score, CAST(:v AS jsonb), :comment, :src, :ev,
            :evv, :agent, :ver, :cost, :by)
    """), {"id": fid, "p": project_id, "r": run_id, "s": span_id, "a": act_id, "t": thread_id,
           "k": str(key)[:200], "score": score, "v": json.dumps(value, default=str),
           "comment": (comment or "")[:4000] or None, "src": source, "ev": evaluator,
           "evv": evaluator_version, "agent": agent_key, "ver": spec_version,
           "cost": int(cost_usd_micros or 0), "by": created_by})
    if run_id:
        from .trace_payloads import extend_retention

        extend_retention(c, run_id)
    return get_feedback(c, project_id, fid)


def get_feedback(c: Any, project_id: str, feedback_id: str) -> Optional[dict]:
    return _row(c.execute(text("SELECT * FROM feedback WHERE project_id = :p AND id = :i"),
                          {"p": project_id, "i": feedback_id}).mappings().first())


def list_feedback(c: Any, project_id: str, *, run_id: Optional[str] = None,
                  agent_key: Optional[str] = None, limit: int = 200) -> list:
    where, params = ["project_id = :p"], {"p": project_id, "n": max(1, min(int(limit), 1000))}
    if run_id:
        where.append("run_id = :r")
        params["r"] = run_id
    if agent_key:
        where.append("agent_key = :a")
        params["a"] = agent_key
    return [_row(r) for r in c.execute(text(
        f"SELECT * FROM feedback WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT :n"),
        params).mappings()]


# ── Datasets ───────────────────────────────────────────────────────────────────────────────────

def create_dataset(c: Any, project_id: str, *, name: str, created_by: str, kind: str = "custom",
                   description: str = "", agent_key: Optional[str] = None) -> dict:
    name = str(name or "").strip()
    if not name or len(name) > 120:
        raise EvalError("a dataset needs a name of at most 120 characters.")
    if kind not in DATASET_KINDS:
        raise EvalError(f"kind must be one of {', '.join(DATASET_KINDS)}.")
    found = dataset_by_name(c, project_id, name)
    if found is not None:
        raise EvalError(f"a dataset named {name!r} already exists.", status=409)
    did = make_id("ds")
    c.execute(text("""
        INSERT INTO datasets (id, project_id, name, description, kind, agent_key, created_by)
        VALUES (:id, :p, :n, :d, :k, :a, :by)
    """), {"id": did, "p": project_id, "n": name, "d": description or "", "k": kind,
           "a": agent_key, "by": created_by})
    return get_dataset(c, project_id, did)


def ensure_dataset(c: Any, project_id: str, name: str, *, kind: str, created_by: str,
                   description: str = "", agent_key: Optional[str] = None) -> dict:
    """The dataset by that name, made when there is none (concurrent makers meet on the
    unique name)."""
    c.execute(text("""
        INSERT INTO datasets (id, project_id, name, description, kind, agent_key, created_by)
        VALUES (:id, :p, :n, :d, :k, :a, :by) ON CONFLICT (project_id, name) DO NOTHING
    """), {"id": make_id("ds"), "p": project_id, "n": name, "d": description, "k": kind,
           "a": agent_key, "by": created_by})
    return dataset_by_name(c, project_id, name)


def dataset_by_name(c: Any, project_id: str, name: str) -> Optional[dict]:
    did = c.execute(text("SELECT id FROM datasets WHERE project_id = :p AND name = :n"),
                    {"p": project_id, "n": name}).scalar()
    return get_dataset(c, project_id, did) if did else None


def get_dataset(c: Any, project_id: str, dataset_id: str) -> Optional[dict]:
    row = _row(c.execute(text("SELECT * FROM datasets WHERE project_id = :p AND id = :i"),
                         {"p": project_id, "i": dataset_id}).mappings().first())
    if row is None:
        return None
    counts = {r[0]: int(r[1]) for r in c.execute(text(
        "SELECT status, count(*) FROM dataset_examples WHERE dataset_id = :d GROUP BY status"),
        {"d": dataset_id})}
    row["examples"] = {s: counts.get(s, 0) for s in ("proposed", "confirmed", "rejected",
                                                     "retired")}
    return row


def list_datasets(c: Any, project_id: str) -> list:
    ids = [r[0] for r in c.execute(text(
        "SELECT id FROM datasets WHERE project_id = :p ORDER BY updated_at DESC"),
        {"p": project_id})]
    return [get_dataset(c, project_id, i) for i in ids]


def _bump(c: Any, dataset_id: str) -> None:
    c.execute(text("UPDATE datasets SET version = version + 1, updated_at = now() WHERE id = :d"),
              {"d": dataset_id})


# ── Examples ───────────────────────────────────────────────────────────────────────────────────

def add_example(c: Any, project_id: str, dataset_id: str, *, source: str, proposed_by: str,
                input: Any = None, reference: Any = None, observed: Any = None,
                metadata: Any = None, status: str = "confirmed", split: str = "test",
                source_ref: Optional[str] = None, run_id: Optional[str] = None,
                span_id: Optional[str] = None, agent_key: Optional[str] = None,
                spec_version: Optional[int] = None,
                dedupe_key: Optional[str] = None) -> dict:
    """One example. A `dedupe_key` already in the dataset returns that example
    (`duplicate: true`): a re-fired capture never makes a second one."""
    if source not in EXAMPLE_SOURCES:
        raise EvalError(f"source must be one of {', '.join(EXAMPLE_SOURCES)}.")
    if status not in ("proposed", "confirmed"):
        raise EvalError("an example starts proposed or confirmed.")
    if split not in SPLITS:
        raise EvalError(f"split must be one of {', '.join(SPLITS)}.")
    if get_dataset(c, project_id, dataset_id) is None:
        raise EvalError("no such dataset in this project.", status=404)
    eid = make_id("dex")
    inserted = c.execute(text("""
        INSERT INTO dataset_examples (id, project_id, dataset_id, split, status, input,
            reference, observed, metadata, source, source_ref, run_id, span_id, agent_key,
            spec_version, dedupe_key, proposed_by, reviewed_by, reviewed_at)
        VALUES (:id, :p, :d, :split, :status, CAST(:i AS jsonb), CAST(:ref AS jsonb),
            CAST(:obs AS jsonb), CAST(:meta AS jsonb), :src, :sref, :r, :s, :agent, :ver,
            :dk, :by, CASE WHEN :status = 'confirmed' THEN :by END,
            CASE WHEN :status = 'confirmed' THEN now() END)
        ON CONFLICT (dataset_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING
        RETURNING id
    """), {"id": eid, "p": project_id, "d": dataset_id, "split": split, "status": status,
           "i": _j(input), "ref": _j(reference), "obs": _j(observed), "meta": _j(metadata),
           "src": source, "sref": source_ref, "r": run_id, "s": span_id, "agent": agent_key,
           "ver": spec_version, "dk": dedupe_key, "by": proposed_by}).scalar()
    if inserted is None:
        prior = c.execute(text("SELECT id FROM dataset_examples WHERE dataset_id = :d "
                               "AND dedupe_key = :k"), {"d": dataset_id, "k": dedupe_key}).scalar()
        return {**(get_example(c, project_id, prior) or {}), "duplicate": True}
    if status == "confirmed":
        _bump(c, dataset_id)
    if run_id:
        from .trace_payloads import extend_retention

        extend_retention(c, run_id)
    return {**(get_example(c, project_id, eid) or {}), "duplicate": False}


def get_example(c: Any, project_id: str, example_id: str) -> Optional[dict]:
    return _row(c.execute(text(
        "SELECT * FROM dataset_examples WHERE project_id = :p AND id = :i"),
        {"p": project_id, "i": example_id}).mappings().first())


def list_examples(c: Any, project_id: str, dataset_id: str, *, status: Optional[str] = None,
                  limit: int = 500) -> list:
    where, params = ["project_id = :p", "dataset_id = :d"], {
        "p": project_id, "d": dataset_id, "n": max(1, min(int(limit), 5000))}
    if status:
        where.append("status = :s")
        params["s"] = status
    return [_row(r) for r in c.execute(text(
        f"SELECT * FROM dataset_examples WHERE {' AND '.join(where)} "
        "ORDER BY created_at, id LIMIT :n"), params).mappings()]


def add_from_span(c: Any, project_id: str, span_id: str, *, actor: str,
                  dataset_id: Optional[str] = None, dataset_name: Optional[str] = None,
                  reference: Any = None, split: str = "test") -> dict:
    """"Add to eval set" on a span (console A6): the span's redacted input becomes the
    example's input, its output what was observed; a person did it, so it is confirmed."""
    from .traces import span_detail

    span = span_detail(c, project_id, span_id)
    if span is None:
        raise EvalError("no such span in this project.", status=404)
    if dataset_id is None:
        name = str(dataset_name or "").strip()
        if not name:
            raise EvalError("name the dataset (dataset_id or dataset_name).")
        dataset_id = ensure_dataset(c, project_id, name, kind="custom", created_by=actor,
                                    agent_key=span.get("agent_key"))["id"]
    return add_example(
        c, project_id, dataset_id, source="span", proposed_by=actor, status="confirmed",
        split=split, input=(span.get("input") or {}).get("body"),
        observed=(span.get("output") or {}).get("body"), reference=reference or {},
        metadata={"span": {"kind": span["kind"], "name": span["name"], "status": span["status"],
                           "trace_id": span["trace_id"]}},
        source_ref=span_id, run_id=span["run_id"], span_id=span_id,
        agent_key=span.get("agent_key"), spec_version=span.get("spec_version"),
        dedupe_key=f"span:{span_id}")


# ── Misses → the annotation inbox ──────────────────────────────────────────────────────────────

def propose_miss(c: Any, project_id: str, *, source: str, run_id: Optional[str],
                 input: Any, observed: Any, reference: Any, source_ref: Optional[str] = None,
                 agent_key: Optional[str] = None, proposed_by: str = "boss",
                 note: str = "") -> dict:
    """A miss as a PROPOSED example in the project's `misses` dataset (the annotation inbox).
    Idempotent per (source, source_ref or run). Never confirms anything."""
    ds = ensure_dataset(c, project_id, MISSES, kind="misses", created_by="boss",
                        description="Misses BOSS captured (gate waivers, failed acceptances, "
                                    "…): confirm each one to make it part of every battle run.")
    got = add_example(c, project_id, ds["id"], source=source, proposed_by=proposed_by,
                      status="proposed", input=_secrets_only(input),
                      observed=_secrets_only(observed), reference=_secrets_only(reference),
                      metadata={"note": note[:1000]} if note else {}, source_ref=source_ref,
                      run_id=run_id, agent_key=agent_key,
                      dedupe_key=f"{source}:{source_ref or run_id}")
    if not got.get("duplicate"):
        emit_event(c, project_id, "eval.example_proposed", proposed_by, "dataset_example",
                   got["id"], {"source": source, "run_id": run_id, "dataset_id": ds["id"]})
    return got


def _files(files: Any) -> list:
    out = []
    for item in list(files or [])[:MAX_FILES]:
        if isinstance(item, Mapping) and "path" in item:
            content = str(item.get("content") or "")
            out.append({"path": str(item["path"]),
                        "content": content[:MAX_FILE_CHARS],
                        **({"clipped": True} if len(content) > MAX_FILE_CHARS else {})})
    return out


def capture_gate_waiver(c: Any, project_id: str, run: Mapping[str, Any], *, waived: list,
                        failing: list, actor: str, reason: str = "",
                        quality_gate_id: Optional[str] = None, files: Any = None) -> Optional[dict]:
    """A person waived a failing gate: the gate said FAIL where a person says the work is fine
    — a miss to learn from. Best-effort: never raises into the override."""
    try:
        spec = run.get("spec") if isinstance(run.get("spec"), Mapping) else {}
        with c.begin_nested():
            return propose_miss(
                c, project_id, source="gate_waiver", run_id=str(run.get("id") or ""),
                source_ref=quality_gate_id or str(run.get("id") or ""),
                input={"kind": "gate_verdict", "run_id": run.get("id"),
                       "title": run.get("title"),
                       "acceptance_criteria": list((spec or {}).get("acceptance_criteria") or []),
                       "failing_gates": list(failing or []), "files": _files(files)},
                observed={"verdict": "fail", "gates_failed": list(failing or [])},
                reference={"expected_verdict": "pass", "waived_gates": list(waived or []),
                           "reason": reason, "by": actor},
                note=f"{actor} waived {', '.join(waived or []) or 'the gate'}: {reason}")
    except Exception as exc:  # noqa: BLE001 — capture never blocks the override
        log.warning("evals: gate waiver not captured for %s (non-fatal): %s", run.get("id"), exc)
        return None


def capture_failed_acceptance(c: Any, project_id: str, *, run_id: str, review_id: str,
                              actor: str, note: str, snapshot: Mapping[str, Any]) -> Optional[dict]:
    """A person sent a delivery back (changes_requested): the run said DONE where a person
    says it is not — a miss. Best-effort."""
    try:
        snap = dict(snapshot or {})
        with c.begin_nested():
            return propose_miss(
                c, project_id, source="failed_acceptance", run_id=run_id, source_ref=review_id,
                input={"kind": "delivery", "run_id": run_id,
                       "criteria": snap.get("criteria") or snap.get("acceptance_criteria"),
                       "delivered_head": snap.get("delivered_head"),
                       "title": snap.get("title")},
                observed={"verdict": "accepted", "delivered_head": snap.get("delivered_head")},
                reference={"expected_verdict": "changes_requested", "by": actor,
                           "change_requested": note},
                note=f"{actor} sent it back: {note}")
    except Exception as exc:  # noqa: BLE001
        log.warning("evals: failed acceptance not captured for %s (non-fatal): %s", run_id, exc)
        return None


def inbox(c: Any, project_id: str, *, status: str = "proposed", limit: int = 100) -> list:
    """The annotation inbox: proposed examples, newest first."""
    return [_row(r) for r in c.execute(text("""
        SELECT e.*, d.name AS dataset_name FROM dataset_examples e
          JOIN datasets d ON d.id = e.dataset_id
         WHERE e.project_id = :p AND e.status = :s
         ORDER BY e.created_at DESC LIMIT :n
    """), {"p": project_id, "s": status, "n": max(1, min(int(limit), 500))}).mappings()]


def review_example(c: Any, project_id: str, example_id: str, *, verdict: str, actor: str,
                   note: str = "", reference: Any = None) -> dict:
    """Confirm or reject a proposed example. A confirmation may correct the reference."""
    if verdict not in ("confirm", "reject"):
        raise EvalError("verdict is confirm or reject.")
    row = c.execute(text("SELECT status, dataset_id, run_id FROM dataset_examples "
                         "WHERE project_id = :p AND id = :i FOR UPDATE"),
                    {"p": project_id, "i": example_id}).mappings().first()
    if row is None:
        raise EvalError("no such example in this project.", status=404)
    if row["status"] != "proposed":
        raise EvalError(f"this example is already {row['status']}.", status=409)
    status = "confirmed" if verdict == "confirm" else "rejected"
    c.execute(text("""
        UPDATE dataset_examples SET status = :s, reviewed_by = :a, reviewed_at = now(),
               review_note = :n, updated_at = now(),
               reference = COALESCE(CAST(:ref AS jsonb), reference)
         WHERE id = :i
    """), {"s": status, "a": actor, "n": note[:2000] or None, "i": example_id,
           "ref": json.dumps(reference) if reference is not None else None})
    if status == "confirmed":
        _bump(c, row["dataset_id"])
    emit_event(c, project_id, f"eval.example_{status}", actor, "dataset_example", example_id,
               {"dataset_id": row["dataset_id"], "run_id": row["run_id"]})
    return get_example(c, project_id, example_id)


# ── The battle set as a dataset ────────────────────────────────────────────────────────────────

def battle_cases(scenarios: Iterable) -> list:
    """Every case of the battle set, numbered as scripts/agent_battle.py numbers them
    (`scenario#n`: phrasings or variants 0.., controls 100..)."""
    out = []
    for sc in scenarios:
        if not isinstance(sc, Mapping) or not sc.get("id"):
            continue
        positives = sc.get("variants") if sc.get("surface") == "step_recovery" \
            else sc.get("phrasings")
        for i, item in enumerate(positives or []):
            text_ = item.get("input") if isinstance(item, Mapping) and "input" in item else item
            out.append({"key": f"{sc['id']}#{i}", "scenario": sc["id"], "kind": "positive",
                        "surface": sc.get("surface"), "input": text_,
                        "reference": {**dict(sc.get("checks") or {}),
                                      "expected": sc.get("expected"),
                                      "must_not": sc.get("must_not") or []},
                        "covers": sc.get("covers") or []})
        for j, neg in enumerate(sc.get("negatives") or []):
            if not isinstance(neg, Mapping):
                continue
            out.append({"key": f"{sc['id']}#{100 + j}", "scenario": sc["id"],
                        "kind": "negative", "surface": sc.get("surface"),
                        "input": neg.get("input", neg.get("variant")),
                        "reference": {**dict(neg.get("checks") or {}),
                                      "expected": neg.get("expected"),
                                      "must_not": neg.get("must_not") or []},
                        "covers": sc.get("covers") or []})
    return out


def import_battle_yaml(c: Any, project_id: str, *, actor: str,
                       path: Optional[Path] = None) -> dict:
    """tests/agent-battle/battle.yaml as the `agent-battle` dataset: one confirmed example per
    case (`scenario#n`), its checks and expectations the reference. Idempotent."""
    import yaml

    path = path or Path(__file__).resolve().parents[3] / "tests" / "agent-battle" / "battle.yaml"
    if not path.is_file():
        raise EvalError(f"the battle set is not in this checkout ({path.name}).", status=404)
    ds = ensure_dataset(c, project_id, BATTLE, kind="battle", created_by=actor,
                        description="The agent battle-test set (tests/agent-battle/battle.yaml).")
    added = 0
    for case in battle_cases(yaml.safe_load(path.read_text()) or []):
        got = add_example(c, project_id, ds["id"], source="battle_yaml", proposed_by=actor,
                          status="confirmed",
                          input={"scenario": case["scenario"], "surface": case["surface"],
                                 "kind": case["kind"], "input": case["input"]},
                          reference=case["reference"], metadata={"covers": case["covers"]},
                          source_ref=case["key"], dedupe_key=f"battle:{case['key']}")
        added += 0 if got.get("duplicate") else 1
    return {**get_dataset(c, project_id, ds["id"]), "added": added}


# ── Checks ─────────────────────────────────────────────────────────────────────────────────────

def _names(items: Any) -> set:
    return {str(x.get("tool") if isinstance(x, Mapping) else x) for x in (items or [])}


def check_example(example: Mapping[str, Any], output: Any) -> list:
    """Why `output` fails the example's reference (an empty list: it passes)."""
    ref = example.get("reference") if isinstance(example.get("reference"), Mapping) else {}
    out = output if isinstance(output, Mapping) else {"value": output}
    fails: list = []
    if "expected_verdict" in ref:
        got = str(out.get("verdict") or "").strip().lower()
        if got != str(ref["expected_verdict"]).strip().lower():
            fails.append(f"verdict {got or '(none)'!r}, expected {ref['expected_verdict']!r}")
    if "equals" in ref and output != ref["equals"]:
        fails.append("the answer differs from the reference")
    executed, offered = _names(out.get("executed")), _names(out.get("offered"))
    if ref.get("any_of") and not (set(ref["any_of"]) & (executed | offered)):
        fails.append(f"none of {sorted(ref['any_of'])} ran or was offered")
    for tool in ref.get("never_execute") or []:
        if tool in executed:
            fails.append(f"{tool} ran")
    for tool in ref.get("never_offer") or []:
        if tool in offered:
            fails.append(f"{tool} was offered")
    if ref.get("no_writes") and out.get("writes"):
        fails.append("a write ran")
    reply = str(out.get("text") or "").lower()
    if ref.get("mentions_any") and not any(str(w).lower() in reply
                                           for w in ref["mentions_any"]):
        fails.append(f"the reply mentions none of {ref['mentions_any']}")
    recovery = out.get("recovery")
    if ref.get("recovery_in") and recovery not in ref["recovery_in"]:
        fails.append(f"recovery {recovery!r} not in {ref['recovery_in']}")
    if ref.get("recovery_not_in") and recovery in ref["recovery_not_in"]:
        fails.append(f"recovery {recovery!r} is one it must not take")
    return fails


# ── Experiments ────────────────────────────────────────────────────────────────────────────────

def start_experiment(c: Any, project_id: str, dataset_id: str, *, name: str, created_by: str,
                     source: str = "api", agent_key: Optional[str] = None,
                     spec_version: Optional[int] = None, engine: str = "scripted") -> dict:
    ds = get_dataset(c, project_id, dataset_id)
    if ds is None:
        raise EvalError("no such dataset in this project.", status=404)
    if agent_key and spec_version is None:
        spec_version = c.execute(text(
            "SELECT version FROM agent_spec_versions WHERE project_id = :p AND kind = 'agent' "
            "AND agent_key = :k AND active"), {"p": project_id, "k": agent_key}).scalar()
    xid = make_id("dxp")
    c.execute(text("""
        INSERT INTO dataset_experiments (id, project_id, dataset_id, dataset_version, name,
            source, agent_key, spec_version, engine, created_by)
        VALUES (:id, :p, :d, :v, :n, :src, :a, :ver, :e, :by)
    """), {"id": xid, "p": project_id, "d": dataset_id, "v": int(ds["version"]),
           "n": name[:200], "src": source, "a": agent_key, "ver": spec_version, "e": engine,
           "by": created_by})
    return get_experiment(c, project_id, xid)


def record_result(c: Any, project_id: str, experiment_id: str, example_id: str, *,
                  passed: bool, failures: Iterable = (), output: Any = None,
                  run_id: Optional[str] = None, score: Optional[float] = None,
                  cost_usd_micros: int = 0, latency_ms: int = 0) -> None:
    c.execute(text("""
        INSERT INTO dataset_experiment_results (id, project_id, experiment_id, example_id,
            run_id, passed, score, failures, output, cost_usd_micros, latency_ms)
        VALUES (:id, :p, :x, :e, :r, :ok, :score, CAST(:f AS jsonb), CAST(:o AS jsonb), :cost,
            :lat)
        ON CONFLICT (experiment_id, example_id) DO UPDATE SET passed = EXCLUDED.passed,
            failures = EXCLUDED.failures, output = EXCLUDED.output, run_id = EXCLUDED.run_id
    """), {"id": make_id("dxr"), "p": project_id, "x": experiment_id, "e": example_id,
           "r": run_id, "ok": bool(passed), "score": score if score is not None else (
               1.0 if passed else 0.0), "f": json.dumps(list(failures)),
           "o": json.dumps(output, default=str), "cost": int(cost_usd_micros or 0),
           "lat": int(latency_ms or 0)})


def finish_experiment(c: Any, project_id: str, experiment_id: str, *,
                      status: str = "completed", extra: Optional[Mapping] = None) -> dict:
    rows = c.execute(text(
        "SELECT example_id, passed, cost_usd_micros, run_id FROM dataset_experiment_results "
        "WHERE experiment_id = :x"), {"x": experiment_id}).mappings().all()
    total, passed = len(rows), sum(1 for r in rows if r["passed"])
    summary = {"total": total, "passed": passed, "failed": total - passed,
               "pass_rate": round(passed / total, 4) if total else None,
               "cost_usd_micros": sum(int(r["cost_usd_micros"] or 0) for r in rows),
               "failing_examples": [r["example_id"] for r in rows if not r["passed"]],
               **dict(extra or {})}
    c.execute(text("UPDATE dataset_experiments SET status = :s, summary = CAST(:m AS jsonb), "
                   "finished_at = now() WHERE id = :x"),
              {"s": status, "m": json.dumps(summary, default=str), "x": experiment_id})
    return get_experiment(c, project_id, experiment_id)


def get_experiment(c: Any, project_id: str, experiment_id: str, *,
                   results: bool = False) -> Optional[dict]:
    row = _row(c.execute(text(
        "SELECT * FROM dataset_experiments WHERE project_id = :p AND id = :i"),
        {"p": project_id, "i": experiment_id}).mappings().first())
    if row is not None and results:
        row["results"] = [_row(r) for r in c.execute(text(
            "SELECT * FROM dataset_experiment_results WHERE experiment_id = :x "
            "ORDER BY created_at, example_id"), {"x": experiment_id}).mappings()]
    return row


def list_experiments(c: Any, project_id: str, *, dataset_id: Optional[str] = None,
                     agent_key: Optional[str] = None, limit: int = 50) -> list:
    where, params = ["project_id = :p"], {"p": project_id, "n": max(1, min(int(limit), 500))}
    if dataset_id:
        where.append("dataset_id = :d")
        params["d"] = dataset_id
    if agent_key:
        where.append("agent_key = :a")
        params["a"] = agent_key
    return [_row(r) for r in c.execute(text(
        f"SELECT * FROM dataset_experiments WHERE {' AND '.join(where)} "
        "ORDER BY started_at DESC LIMIT :n"), params).mappings()]


Target = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def run_experiment(engine: Any, project_id: str, dataset_id: str, *, target: Target,
                   name: str, created_by: str, source: str = "api",
                   agent_key: Optional[str] = None, spec_version: Optional[int] = None,
                   engine_name: str = "scripted", split: Optional[str] = None) -> dict:
    """Run `target` over the dataset's CONFIRMED examples and keep every result. The target
    returns {output, run_id?, cost_usd_micros?, latency_ms?}; a target that raises is that
    example's failure, never the experiment's."""
    with engine.begin() as c:
        xp = start_experiment(c, project_id, dataset_id, name=name, created_by=created_by,
                              source=source, agent_key=agent_key, spec_version=spec_version,
                              engine=engine_name)
        examples = [e for e in list_examples(c, project_id, dataset_id, status="confirmed",
                                             limit=5000) if split is None or e["split"] == split]
    for example in examples:
        try:
            got = dict(target(example) or {})
            output = got.get("output")
            fails = check_example(example, output)
        except Exception as exc:  # noqa: BLE001 — one example's error is its result
            got, output = {}, None
            fails = [f"the target failed: {type(exc).__name__}: {exc}"[:500]]
        with engine.begin() as c:
            record_result(c, project_id, xp["id"], example["id"], passed=not fails,
                          failures=fails, output=output, run_id=got.get("run_id"),
                          cost_usd_micros=int(got.get("cost_usd_micros") or 0),
                          latency_ms=int(got.get("latency_ms") or 0))
    with engine.begin() as c:
        return finish_experiment(c, project_id, xp["id"])


# ── The battle runner as a callable (a scheduled bot in P3) ────────────────────────────────────

def scripted_target(engine: Any, project_id: str, *, fixes: Optional[Mapping] = None,
                    actor: str = "agent:battle") -> Target:
    """THE SCRIPTED ENGINE AS THE AGENT (real-money battle runs are frozen). Each example is
    answered by a real kernel run — governance, ledger, spans, llm_calls — whose model is a
    ScriptedEngine that says, as JSON, what the agent said when the miss was captured
    (`observed`), unless `fixes` holds a corrected answer for that example (the agent version
    that fixed it). So a confirmed miss FAILS every battle run until it is fixed."""
    from .. import kernel
    from ..kernel import presets
    from ..kernel.ledger import DbSink
    from ..kernel.scripted import Say, ScriptedEngine

    def run(example: Mapping[str, Any]) -> dict:
        answer = (fixes or {}).get(example["id"], example.get("observed") or {})
        result = kernel.run(kernel.RunSpec(
            engine=ScriptedEngine([Say(json.dumps(answer, default=str))], name="scripted"),
            tools=kernel.ReadOnlyToolSet([], lambda name, args: {}),
            policies=presets.agent_loop(2),
            project_id=project_id, actor=actor, stage="battle",
            system="Answer the case with your verdict as a JSON object.",
            messages=[{"role": "user", "content": json.dumps(example.get("input") or {},
                                                             default=str)[:20_000]}],
            sink=DbSink(engine)))
        try:
            output = json.loads(result.text or "null")
        except ValueError:
            output = {"text": result.text}
        return {"output": output, "run_id": result.run_id,
                "cost_usd_micros": int(result.usage.cost_usd_micros or 0)}

    return run


def run_battle(engine: Any, project_id: str, *, dataset: str = MISSES, mode: str = "scripted",
               fixes: Optional[Mapping] = None, target: Optional[Target] = None,
               agent_key: Optional[str] = None, spec_version: Optional[int] = None,
               created_by: str = "battle", source: str = "battle") -> dict:
    """ONE BATTLE RUN over a dataset (default: the confirmed misses), written as an experiment.
    `mode` scripted uses `scripted_target`; any other mode needs the caller's `target` (a real
    engine: frozen while real-money battle spend is). The P3 scheduler calls this."""
    with engine.begin() as c:
        ds = dataset_by_name(c, project_id, dataset) or get_dataset(c, project_id, dataset)
    if ds is None:
        raise EvalError(f"no dataset {dataset!r} in this project.", status=404)
    if target is None:
        if mode != "scripted":
            raise EvalError("a real-engine battle run needs its target (the agent to run).")
        target = scripted_target(engine, project_id, fixes=fixes)
    return run_experiment(engine, project_id, ds["id"], target=target,
                          name=f"battle · {ds['name']} v{ds['version']} · {mode}",
                          created_by=created_by, source=source, agent_key=agent_key,
                          spec_version=spec_version, engine_name=mode)


def record_battle_results(engine: Any, project_id: str, results: list, *, label: str,
                          engine_name: str, created_by: str = "battle") -> dict:
    """The battle-test script's per-case results (scripts/agent_battle.py) as an experiment over
    the `agent-battle` dataset, instead of only a gitignored JSON file."""
    with engine.begin() as c:
        ds = import_battle_yaml(c, project_id, actor=created_by)
        examples = {e["source_ref"]: e for e in list_examples(c, project_id, ds["id"],
                                                               status="confirmed", limit=5000)}
        xp = start_experiment(c, project_id, ds["id"], name=f"battle · {label}",
                              created_by=created_by, source="battle", engine=engine_name)
        for r in results:
            example = examples.get(f"{r.get('scenario')}#{r.get('n', 0)}")
            if example is None:
                continue
            fails = list(r.get("deterministic_failures") or [])
            if (r.get("judge") or {}).get("verdict") not in (None, "pass", "skipped"):
                fails.append(f"judge: {(r.get('judge') or {}).get('why') or 'failed'}")
            record_result(c, project_id, xp["id"], example["id"], passed=bool(r.get("passed")),
                          failures=fails, output={"reply": r.get("reply"),
                                                  "calls": r.get("calls")},
                          cost_usd_micros=int(r.get("cost_usd_micros") or 0),
                          latency_ms=int(float(r.get("seconds") or 0) * 1000))
        return finish_experiment(c, project_id, xp["id"], extra={"label": label})


__all__ = ["BATTLE", "EvalError", "MISSES", "add_example", "add_from_span", "battle_cases",
           "capture_failed_acceptance",
           "capture_gate_waiver", "check_example", "create_dataset", "dataset_by_name",
           "ensure_dataset", "finish_experiment", "get_dataset", "get_example", "get_experiment",
           "get_feedback", "import_battle_yaml", "inbox", "list_datasets", "list_examples",
           "list_experiments", "list_feedback", "propose_miss", "record_battle_results",
           "record_feedback", "record_result", "review_example", "run_battle", "run_experiment",
           "scripted_target", "start_experiment"]
