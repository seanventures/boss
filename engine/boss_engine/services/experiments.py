"""Experiments: an A/B on one setting of a role or a workflow (docs/product/RUN_SCORECARDS.md).

"software_engineer: Claude Code vs Codex, 50/50, 20 builds" — the arm is chosen once, when the
build starts (`code_task_service.start_code_task` is the one door every build goes through),
stamped on the build's spec and on its scorecard, and compared from scorecards.

GUARDRAILS, each enforced here and tested:
  * off by default: nothing is enrolled unless the operator started an experiment;
  * never changes what needs approval: an arm can only set the keys in ARM_KNOBS, none of which
    is risk tier, profile, an approval rule or a gate (test_experiments_never_touch_approval);
  * a cost cap per arm: a capped arm takes no new units; both capped stops the experiment;
  * stop early when one arm fails gates noticeably more often than the other.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping, Optional

from sqlalchemy import text

#: The only settings an arm may vary, how each one lands on a build's spec, and the project
#: setting Promote writes. Approval, risk and gate knobs are deliberately absent.
ARM_KNOBS: dict[str, dict[str, Any]] = {
    "harness": {"label": "Harness", "setting": "code_agent_engine", "kind": "harness"},
    "review_rounds": {"label": "Review rounds", "setting": "code_review_inrun_max_rounds",
                      "kind": "int", "min": 0, "max": 5},
}
#: Spec keys an arm writes. Asserted disjoint from APPROVAL_KEYS by a test.
ARM_SPEC_KEYS = frozenset({"model_config", "review_rounds", "experiment"})
APPROVAL_KEYS = frozenset({"risk_tier", "profile", "open_pr", "auto_merge", "auto_approve",
                           "approval", "approvals", "gates", "waived_gates", "data_boundary"})
#: Promote needs this many finished units in EACH arm and a verdict that is not "too close to
#: call" — unless the operator ticks "Promote anyway" (defect 3).
PROMOTE_MIN_UNITS = 5
STOP_GATE_GAP = 0.3
STOP_MIN_UNITS = 4
CALL_MIN_UNITS = 5


class ExperimentError(ValueError):
    pass


def _harnesses() -> list[str]:
    try:
        from .project_settings import _harness_names

        return list(_harness_names())
    except Exception:  # noqa: BLE001
        return ["claude_code", "codex"]


def _coerce(knob: str, value: Any) -> Any:
    spec = ARM_KNOBS.get(knob)
    if spec is None:
        raise ExperimentError(f"An experiment can vary: {', '.join(ARM_KNOBS)}.")
    if spec["kind"] == "harness":
        name = str(value or "").strip().lower()
        if name not in _harnesses():
            raise ExperimentError(f"'{name}' is not a harness BOSS can drive.")
        return name
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ExperimentError(f"{spec['label']} has to be a whole number.") from error
    if not spec["min"] <= number <= spec["max"]:
        raise ExperimentError(f"{spec['label']} has to be {spec['min']}–{spec['max']}.")
    return number


def _arm_word(knob: str, value: Any) -> str:
    from .scorecards import HARNESS_NAMES

    return HARNESS_NAMES.get(str(value), str(value)) if knob == "harness" else f"{value} rounds"


def _default_name(connection, project_id: str, kind: str, key: str, knob: str, a: Any, b: Any) -> str:
    from .scorecards import names_for

    who = names_for(connection, project_id, "role" if kind == "role" else "workflow", [key]).get(key, key)
    return f"{who}: {_arm_word(knob, a)} vs {_arm_word(knob, b)}"


def create(connection, *, project_id: str, actor: str, name: str, target_kind: str,
           target_key: str, knob: str, arm_a: Any, arm_b: Any, split_b: float = 0.5,
           target_units: int = 20, arm_cost_cap_usd: float = 25.0) -> dict[str, Any]:
    from .core import emit_event, make_id

    if target_kind not in ("role", "workflow"):
        raise ExperimentError("Target is a role or a workflow.")
    if not str(target_key or "").strip():
        raise ExperimentError("Name the role or workflow.")
    table, col = (("roles", "role_key") if target_kind == "role"
                  else ("workflow_definitions", "workflow_key"))
    if not connection.execute(text(f"SELECT 1 FROM {table} WHERE project_id = :p AND {col} = :k"),
                              {"p": project_id, "k": target_key}).scalar():
        raise ExperimentError(f"No {target_kind} '{target_key}' in this project.")
    a, b = _coerce(knob, arm_a), _coerce(knob, arm_b)
    if a == b:
        raise ExperimentError("The two arms have to differ.")
    if not 0.05 <= float(split_b) <= 0.95:
        raise ExperimentError("Split has to be between 5% and 95%.")
    if not 2 <= int(target_units) <= 1000:
        raise ExperimentError("Units has to be 2–1000.")
    if not 0 < float(arm_cost_cap_usd) <= 10_000:
        raise ExperimentError("Cost cap has to be above $0.")
    clash = connection.execute(text(
        "SELECT id FROM experiments WHERE project_id = :p AND status = 'running' "
        "AND target_kind = :tk AND target_key = :t AND knob = :k"),
        {"p": project_id, "tk": target_kind, "t": target_key, "k": knob}).scalar()
    if clash:
        raise ExperimentError("One experiment on this already runs.")
    eid = make_id("exp")
    connection.execute(text("""
        INSERT INTO experiments (id, project_id, name, target_kind, target_key, knob, arm_a, arm_b,
                                 split_b, target_units, arm_cost_cap_usd, created_by)
        VALUES (:id, :p, :n, :tk, :t, :k, CAST(:a AS JSONB), CAST(:b AS JSONB), :s, :u, :c, :by)"""),
        {"id": eid, "p": project_id, "n": (name or _default_name(connection, project_id, target_kind,
                                                                  target_key, knob, a, b))[:120],
         "tk": target_kind, "t": target_key, "k": knob, "a": json.dumps(a), "b": json.dumps(b),
         "s": float(split_b), "u": int(target_units), "c": float(arm_cost_cap_usd), "by": actor})
    emit_event(connection, project_id, "experiment.started", actor, "experiment", eid,
               {"knob": knob, "target_kind": target_kind})
    return get(connection, project_id, eid)


def _row(connection, project_id: str, eid: str) -> Mapping[str, Any]:
    row = connection.execute(text("SELECT * FROM experiments WHERE id = :id AND project_id = :p"),
                             {"id": eid, "p": project_id}).mappings().first()
    if not row:
        raise LookupError("No such experiment.")
    return row


def _arm_for(eid: str, unit_id: str, split_b: float, capped: list[str]) -> Optional[str]:
    """Deterministic: the same unit always lands in the same arm (a rerun of start is safe)."""
    open_arms = [a for a in ("a", "b") if a not in capped]
    if not open_arms:
        return None
    if len(open_arms) == 1:
        return open_arms[0]
    bucket = int(hashlib.sha256(f"{eid}:{unit_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "b" if bucket < float(split_b) else "a"


def apply_arm(connection, *, project_id: str, run_id: str, spec: dict[str, Any],
              finalize_ctx: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """At a build's start: if a running experiment targets it, pick the arm and write it onto
    the spec. Returns {experiment_id, arm} or None. An explicit pin on the build wins."""
    role_key = spec.get("role_key") or "software_engineer"
    wf_key = None
    wf = finalize_ctx.get("workflow") if isinstance(finalize_ctx.get("workflow"), dict) else {}
    wf_key = wf.get("workflow_key") or wf.get("key")
    if not wf_key and finalize_ctx.get("workflow_run_id"):
        wf_key = connection.execute(text(
            "SELECT d.workflow_key FROM workflow_runs r JOIN workflow_definitions d "
            "ON d.id = r.workflow_definition_id WHERE r.id = :id"),
            {"id": finalize_ctx["workflow_run_id"]}).scalar()
    rows = connection.execute(text("""
        SELECT * FROM experiments WHERE project_id = :p AND status = 'running'
          AND ((target_kind = 'role' AND target_key = :r) OR (target_kind = 'workflow' AND target_key = :w))
        ORDER BY created_at"""), {"p": project_id, "r": role_key, "w": wf_key or ""}).mappings().all()
    for exp in rows:
        knob = exp["knob"]
        if knob == "harness" and spec.get("model_config"):
            continue  # pinned by the caller: an explicit choice is never overruled
        if knob == "review_rounds" and spec.get("review_rounds") is not None:
            continue
        capped = list(exp["capped_arms"] or [])
        arm = _arm_for(exp["id"], run_id, float(exp["split_b"]), capped)
        if arm is None:
            continue
        value = exp["arm_a"] if arm == "a" else exp["arm_b"]
        if knob == "harness":
            spec["model_config"] = {"default": {"engine": str(value)}}
        elif knob == "review_rounds":
            spec["review_rounds"] = int(value)
        spec["experiment"] = {"id": exp["id"], "arm": arm}
        connection.execute(text(
            "INSERT INTO experiment_assignments (experiment_id, unit_kind, unit_id, arm) "
            "VALUES (:e, 'build', :u, :a) ON CONFLICT (unit_kind, unit_id) DO NOTHING"),
            {"e": exp["id"], "u": run_id, "a": arm})
        return {"experiment_id": exp["id"], "arm": arm}
    return None


def _arms(connection, eid: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(text("""
        SELECT a.arm, COUNT(s.unit_id) AS n, AVG(s.score) AS mean, STDDEV_SAMP(s.score) AS sd,
               COALESCE(SUM(s.cost_usd), 0) AS cost,
               AVG(CASE WHEN COALESCE(s.gates_failed, 0) > 0 OR s.terminal_state <> 'completed'
                        THEN 1 ELSE 0 END) FILTER (WHERE s.unit_id IS NOT NULL) AS gate_fail,
               COUNT(a.unit_id) AS assigned
        FROM experiment_assignments a
        LEFT JOIN run_scorecards s ON s.unit_kind IN ('build', 'quick_change')
             AND s.unit_id = a.unit_id
        WHERE a.experiment_id = :e GROUP BY a.arm"""), {"e": eid}).mappings().all()
    out = {arm: {"n": 0, "mean": None, "sd": None, "cost_usd": 0.0, "gate_fail": None,
                 "assigned": 0} for arm in ("a", "b")}
    for r in rows:
        out[r["arm"]] = {"n": int(r["n"] or 0),
                         "mean": None if r["mean"] is None else round(float(r["mean"]), 3),
                         "sd": None if r["sd"] is None else round(float(r["sd"]), 3),
                         "cost_usd": round(float(r["cost"] or 0), 2),
                         "gate_fail": None if r["gate_fail"] is None else round(float(r["gate_fail"]), 3),
                         "assigned": int(r["assigned"] or 0)}
    return out


def compare(arms: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """B − A with a 95% interval (Welch). 'Too close to call' while it crosses 0 or n < 5."""
    a, b = arms["a"], arms["b"]
    if a["n"] < CALL_MIN_UNITS or b["n"] < CALL_MIN_UNITS or a["mean"] is None or b["mean"] is None:
        return {"verdict": "too_close", "diff": None, "low": None, "high": None,
                "label": "Too close to call", "why": f"needs {CALL_MIN_UNITS}+ per arm"}
    diff = b["mean"] - a["mean"]
    se = math.sqrt((a["sd"] or 0) ** 2 / a["n"] + (b["sd"] or 0) ** 2 / b["n"])
    low, high = diff - 1.96 * se, diff + 1.96 * se
    if low > 0:
        verdict, label = "b", "B wins"
    elif high < 0:
        verdict, label = "a", "A wins"
    else:
        verdict, label = "too_close", "Too close to call"
    return {"verdict": verdict, "diff": round(diff, 3), "low": round(low, 3),
            "high": round(high, 3), "label": label, "why": "95% interval of B − A"}


def after_scorecard(connection, eid: str) -> None:
    """A scorecard of this experiment landed: apply the caps, the early stop and the finish."""
    exp = connection.execute(text("SELECT * FROM experiments WHERE id = :e"),
                             {"e": eid}).mappings().first()
    if not exp or exp["status"] != "running":
        return
    arms = _arms(connection, eid)
    capped = sorted({arm for arm in ("a", "b")
                     if arms[arm]["cost_usd"] >= float(exp["arm_cost_cap_usd"])})
    stop_reason = None
    fa, fb = arms["a"]["gate_fail"], arms["b"]["gate_fail"]
    if (arms["a"]["n"] >= STOP_MIN_UNITS and arms["b"]["n"] >= STOP_MIN_UNITS
            and fa is not None and fb is not None and abs(fa - fb) >= STOP_GATE_GAP):
        worse = "a" if fa > fb else "b"
        stop_reason = f"arm_{worse}_fails_gates"
    elif len(capped) == 2:
        stop_reason = "both_arms_capped"
    status = "stopped" if stop_reason else "running"
    if not stop_reason and arms["a"]["n"] + arms["b"]["n"] >= int(exp["target_units"]):
        status, stop_reason = "finished", "reached_units"
    connection.execute(text("""
        UPDATE experiments SET capped_arms = CAST(:c AS JSONB), status = :s,
               stop_reason = COALESCE(:r, stop_reason),
               ended_at = CASE WHEN :s <> 'running' THEN now() ELSE ended_at END,
               ended_by = CASE WHEN :s <> 'running' THEN 'boss' ELSE ended_by END
        WHERE id = :e"""), {"c": json.dumps(capped), "s": status, "r": stop_reason, "e": eid})


def get(connection, project_id: str, eid: str) -> dict[str, Any]:
    exp = dict(_row(connection, project_id, eid))
    arms = _arms(connection, eid)
    return {
        "id": exp["id"], "name": exp["name"], "target_kind": exp["target_kind"],
        "target_key": exp["target_key"], "knob": exp["knob"],
        "knob_label": ARM_KNOBS.get(exp["knob"], {}).get("label", exp["knob"]),
        "arm_a": exp["arm_a"], "arm_b": exp["arm_b"], "split_b": float(exp["split_b"]),
        "arm_a_label": _arm_word(exp["knob"], exp["arm_a"]),
        "arm_b_label": _arm_word(exp["knob"], exp["arm_b"]),
        "target_units": int(exp["target_units"]),
        "arm_cost_cap_usd": float(exp["arm_cost_cap_usd"]), "status": exp["status"],
        "stop_reason": exp["stop_reason"], "capped_arms": list(exp["capped_arms"] or []),
        "promoted_arm": exp["promoted_arm"], "created_by": exp["created_by"],
        "created_at": exp["created_at"], "ended_at": exp["ended_at"],
        "arms": arms, "compare": compare(arms),
        "promote": {a: promote_plan(connection, project_id, exp, a) for a in ("a", "b")}
        if exp["status"] != "promoted" else None,
    }


def list_all(connection, project_id: str) -> dict[str, Any]:
    ids = connection.execute(text(
        "SELECT id FROM experiments WHERE project_id = :p ORDER BY (status = 'running') DESC, "
        "created_at DESC LIMIT 30"), {"p": project_id}).scalars().all()
    roles = connection.execute(text(
        "SELECT role_key, COALESCE(NULLIF(display_name, ''), role_key) FROM roles "
        "WHERE project_id = :p ORDER BY 2"), {"p": project_id}).all()
    flows = connection.execute(text(
        "SELECT workflow_key, name FROM workflow_definitions WHERE project_id = :p "
        "AND namespace = 'product' AND status = 'active' ORDER BY name"), {"p": project_id}).all()
    return {"items": [get(connection, project_id, i) for i in ids],
            "targets": {"role": [{"key": r[0], "name": r[1]} for r in roles],
                        "workflow": [{"key": r[0], "name": r[1]} for r in flows]},
            "knobs": [{"key": k, "label": v["label"], "kind": v["kind"],
                       "choices": _harnesses() if v["kind"] == "harness" else None,
                       "min": v.get("min"), "max": v.get("max")} for k, v in ARM_KNOBS.items()]}


def stop(connection, *, project_id: str, eid: str, actor: str) -> dict[str, Any]:
    exp = _row(connection, project_id, eid)
    if exp["status"] == "running":
        connection.execute(text(
            "UPDATE experiments SET status = 'stopped', stop_reason = 'stopped_by_operator', "
            "ended_at = now(), ended_by = :a WHERE id = :e"), {"a": actor, "e": eid})
    return get(connection, project_id, eid)


def promote_plan(connection, project_id: str, exp: Mapping[str, Any], arm: str) -> dict[str, Any]:
    """What Promote would change, in words, and whether the evidence is enough."""
    from ..config import get_settings
    from .project_settings import effective

    knob = ARM_KNOBS[exp["knob"]]
    value = exp["arm_a"] if arm == "a" else exp["arm_b"]
    current = effective(connection, project_id, get_settings(), knob["setting"])
    arms = _arms(connection, exp["id"])
    verdict = compare(arms)
    short = [a for a in ("a", "b") if arms[a]["n"] < PROMOTE_MIN_UNITS]
    reasons = []
    if short:
        reasons.append(f"fewer than {PROMOTE_MIN_UNITS} finished in arm "
                       + " and ".join(a.upper() for a in short))
    if verdict["verdict"] == "too_close":
        reasons.append("too close to call")
    elif verdict["verdict"] != arm:
        reasons.append(f"arm {verdict['verdict'].upper()} scores higher")
    return {"setting": knob["setting"], "label": knob["label"],
            "from": _arm_word(exp["knob"], current) if current not in (None, "") else "default",
            "to": _arm_word(exp["knob"], value), "value": value,
            "enough": not reasons, "why_not": "; ".join(reasons)}


def promote(connection, *, project_id: str, eid: str, arm: str, actor: str,
            anyway: bool = False) -> dict[str, Any]:
    """Make the arm's value this project's setting — through project_settings.set_one, so the
    change is in Govern's log like any other — and end the experiment. Refused without enough
    evidence unless `anyway` (the operator's own tick)."""
    from .project_settings import set_one

    if arm not in ("a", "b"):
        raise ExperimentError("Promote arm a or b.")
    exp = _row(connection, project_id, eid)
    if exp["status"] == "promoted":
        raise ExperimentError("Already promoted.")
    plan = promote_plan(connection, project_id, exp, arm)
    if not plan["enough"] and not anyway:
        raise NotEnoughEvidence(f"Not enough evidence: {plan['why_not']}.")
    set_one(connection, project_id, plan["setting"], plan["value"], actor=actor)
    connection.execute(text(
        "UPDATE experiments SET status = 'promoted', promoted_arm = :arm, "
        "ended_at = COALESCE(ended_at, now()), ended_by = :a WHERE id = :e"),
        {"arm": arm, "a": actor, "e": eid})
    return get(connection, project_id, eid)


class NotEnoughEvidence(ExperimentError):
    """Promote was pressed before the arms had enough units or a clear winner (409)."""
