"""Run scorecards: for every finished unit, what was tried and how it turned out.

docs/product/RUN_SCORECARDS.md ("Implementation"). One row per unit in `run_scorecards`, keyed by
(unit_kind, unit_id), written by `record_scorecard` when the unit reaches a terminal state and
again when a late outcome arrives. Idempotent: a rerun rewrites the same row from the same facts.

NAMES, NUMBERS AND VERDICTS ONLY. Nothing a person or a model wrote is copied here: no title,
brief, prompt, diff, path or repo. The readers below select only the columns they turn into
numbers; content stays in its own tables and their own redaction rules.

Every function here is best-effort from the caller's side: `on_event` and `settle` swallow and
log, so a scorecard can never undo or fail the transition it records.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import text

log = logging.getLogger(__name__)

UNIT_KINDS = ("workflow_run", "build", "step", "panel", "research", "quick_change")
CODE_KINDS = ("build", "quick_change")

#: Score v1's weights. Shown and editable in Govern › Sharing › Score; an edit makes "1.<n>".
DEFAULT_WEIGHTS: dict[str, float] = {
    "tests": 0.5, "gates": 0.2, "review": 0.2, "intact": 0.1, "approvals": 0.2,
    "prd_approved": 0.6, "panel_agreed": 0.4,
    "completed_unverified": 0.6,
    "merged_bonus": 0.15, "revert_penalty": 0.4, "rollback_penalty": 0.3,
    "unhealthy_24h_penalty": 0.2,
    "cost_penalty": 0.04, "turns_penalty": 0.03, "steers_penalty": 0.03,
}
WEIGHT_LABELS: dict[str, str] = {
    "tests": "Tests pass", "gates": "Gates clear", "review": "Review approved",
    "intact": "Old tests intact", "approvals": "Approvals",
    "prd_approved": "PRD approved", "panel_agreed": "Panel agreed",
    "completed_unverified": "Done, no evidence",
    "merged_bonus": "Merged bonus", "revert_penalty": "Reverted", "rollback_penalty": "Rolled back",
    "unhealthy_24h_penalty": "Unhealthy at 24h",
    "cost_penalty": "Cost over median", "turns_penalty": "Turns over median",
    "steers_penalty": "Steers over median",
}
MEDIAN_WINDOW_DAYS = 90
#: "What's costing you" lists a unit for its score only below this (defect 11).
LOW_SCORE = 0.5
#: What the console calls each harness (never its key on screen).
HARNESS_NAMES = {"claude_code": "Claude Code", "codex": "Codex", "native": "BOSS native",
                 "openai": "OpenAI API", "azure": "Azure OpenAI", "openrouter": "OpenRouter",
                 "groq": "Groq", "deepseek": "DeepSeek", "ollama": "Ollama", "vllm": "vLLM",
                 "grok": "Grok", "cursor": "Cursor", "gemini": "Gemini", "echo": "Echo"}


def names_for(connection, project_id: str, group: str, keys: list[str]) -> dict[str, str]:
    """What the console shows for a group's keys: an agent's or workflow's name, a harness's."""
    keys = [k for k in keys if k and k != "—"]
    if not keys:
        return {}
    if group == "harness":
        return {k: HARNESS_NAMES.get(k, k.replace("_", " ").title()) for k in keys}
    if group == "role":
        rows = connection.execute(text(
            "SELECT role_key, display_name FROM roles WHERE project_id = :p AND role_key = ANY(:k)"),
            {"p": project_id, "k": keys}).all()
        got = {r[0]: r[1] for r in rows if r[1]}
        return {k: got.get(k) or k.replace("_", " ").title() for k in keys}
    if group == "workflow":
        rows = connection.execute(text(
            "SELECT workflow_key, name FROM workflow_definitions WHERE project_id = :p "
            "AND workflow_key = ANY(:k)"), {"p": project_id, "k": keys}).all()
        got = {r[0]: r[1] for r in rows if r[1]}
        return {k: got.get(k) or k.replace("_", " ").title() for k in keys}
    return {k: k for k in keys}
MEDIAN_MIN_ROWS = 5
SWEEP_BATCH = 300
RESCORE_BATCH = 2000


def _j(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _int(value: Any) -> Optional[int]:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


def _secs(a: Any, b: Any) -> Optional[int]:
    if not a or not b:
        return None
    try:
        return max(0, int((b - a).total_seconds()))
    except Exception:  # noqa: BLE001
        return None


def _word(value: Any) -> Optional[str]:
    """A BOSS-generated class word (failure_class, reason_code); anything else is dropped."""
    if not value:
        return None
    text_ = str(value).strip().lower().replace("-", "_").replace(" ", "_")[:40]
    return text_ if text_.replace("_", "").isalnum() else None


# ------------------------------------------------------------------------------------------
# Weights and the score
# ------------------------------------------------------------------------------------------


def weights(connection) -> tuple[dict[str, float], str]:
    """(weights, score_version). Defaults are version "1"; each saved edit is "1.<n>"."""
    row = connection.execute(text(
        "SELECT value FROM system_state WHERE key = 'score_weights'")).scalar()
    value = _j(row, {}) or {}
    merged = dict(DEFAULT_WEIGHTS)
    for key, number in (value.get("weights") or {}).items():
        if key in merged and isinstance(number, (int, float)):
            merged[key] = float(number)
    n = int(value.get("n") or 0)
    return merged, ("1" if n <= 0 else f"1.{n}")


def set_weights(connection, new: Mapping[str, Any], actor: str, project_id: str) -> dict:
    """Save edited weights (each 0..1), bump the version, record the change in Govern's log."""
    current, version = weights(connection)
    clean: dict[str, float] = {}
    for key, number in (new or {}).items():
        if key not in DEFAULT_WEIGHTS:
            raise ValueError(f"'{key}' is not a score weight.")
        try:
            value = float(number)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{WEIGHT_LABELS[key]} has to be a number.") from error
        if not 0 <= value <= 1:
            raise ValueError(f"{WEIGHT_LABELS[key]} has to be between 0 and 1.")
        clean[key] = round(value, 3)
    merged = {**current, **clean}
    if merged == current:
        return {"weights": current, "score_version": version}
    row = connection.execute(text(
        "SELECT value FROM system_state WHERE key = 'score_weights'")).scalar()
    n = int((_j(row, {}) or {}).get("n") or 0) + 1
    if merged == DEFAULT_WEIGHTS:
        n = 0
    connection.execute(text(
        "INSERT INTO system_state (key, value, updated_at) VALUES ('score_weights', "
        "CAST(:v AS JSONB), now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "updated_at = now()"),
        {"v": json.dumps({"weights": {k: v for k, v in merged.items()
                                      if v != DEFAULT_WEIGHTS[k]}, "n": n,
                          "by": actor, "at": datetime.now(timezone.utc).isoformat()})})
    _log_change(connection, project_id, actor, "score_weights", "Score weights",
                version, "1" if n <= 0 else f"1.{n}")
    # Rescored NOW, every row, so the Scorecards view never shows an empty version (defect 4).
    rescore(connection, limit=None)
    return {"weights": merged, "score_version": "1" if n <= 0 else f"1.{n}"}


def _log_change(connection, project_id: str, actor: str, key: str, label: str,
                before: Any, after: Any) -> None:
    """A Govern change line (`settings.changed`), best effort, in a savepoint."""
    if not project_id:
        return
    from .core import emit_event

    try:
        with connection.begin_nested():
            emit_event(connection, project_id, "settings.changed", actor or "operator",
                       "install_setting", key,
                       {"key": key, "label": label, "from": before, "to": after,
                        "source_before": "install", "source_after": "install"})
    except Exception:  # noqa: BLE001 — the record of a change is not the change
        log.exception("could not record settings.changed for %s", key)


def score_of(card: Mapping[str, Any], w: Mapping[str, float]) -> float:
    """Score v1 (docs: "Score v1"). Raw signals in, 0..1 out, 3 decimals."""
    state = card.get("terminal_state")
    if state in ("failed", "cancelled", "rejected"):
        quality = 0.0
    else:
        kind = card.get("unit_kind")
        parts: list[tuple[float, float]] = []
        if kind == "panel":
            if card.get("prd_approved") is not None:
                parts.append((w["prd_approved"], 1.0 if card["prd_approved"] else 0.0))
            if card.get("panel_agreed") is not None:
                parts.append((w["panel_agreed"], 1.0 if card["panel_agreed"] else 0.0))
        elif kind == "research":
            conf = card.get("confidence")
            if conf is not None:
                parts.append((1.0, max(0.0, min(1.0, float(conf)))))
        else:
            total = card.get("tests_total")
            if total:
                parts.append((w["tests"], float(card.get("tests_passed") or 0) / float(total)))
            if card.get("gates_failed") is not None and (
                    card.get("gates_failed") or card.get("gates_passed")):
                parts.append((w["gates"], 1.0 if not card["gates_failed"] else 0.0))
            if card.get("review_approved") is not None:
                parts.append((w["review"], 1.0 if card["review_approved"] else 0.0))
            if card.get("existing_tests_intact") is not None:
                parts.append((w["intact"], 1.0 if card["existing_tests_intact"] else 0.0))
            if kind == "workflow_run":
                decided = int(card.get("approvals") or 0) + int(card.get("rejections") or 0)
                if decided:
                    parts.append((w["approvals"], float(card.get("approvals") or 0) / decided))
        weight = sum(p[0] for p in parts)
        quality = (sum(p[0] * p[1] for p in parts) / weight) if weight > 0 else float(
            w["completed_unverified"])
    bonus = 0.0
    if card.get("pr_merged"):
        bonus += w["merged_bonus"]
    if card.get("pr_reverted"):
        bonus -= w["revert_penalty"]
    if card.get("rolled_back") or state == "rolled_back":
        bonus -= w["rollback_penalty"]
    if card.get("deploy_healthy_24h") is False:
        bonus -= w["unhealthy_24h_penalty"]
    over = card.get("over_median") or {}
    penalty = (w["cost_penalty"] * bool(over.get("cost"))
               + w["turns_penalty"] * bool(over.get("turns"))
               + w["steers_penalty"] * bool(over.get("steers")))
    return round(max(0.0, min(1.0, quality + bonus - penalty)), 3)


# ------------------------------------------------------------------------------------------
# Readers: one per unit kind. Each returns the scorecard's columns, or None when the unit is
# missing or not terminal. Only names, numbers and verdicts are selected.
# ------------------------------------------------------------------------------------------


def _provider(harness: Optional[str], model: Optional[str]) -> Optional[str]:
    h, m = (harness or "").lower(), (model or "").lower()
    if h == "claude_code" or m.startswith(("claude", "sonnet", "opus", "haiku", "fable")):
        return "anthropic"
    if h in ("codex", "openai", "native") or m.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai"
    if h in ("ollama", "vllm"):
        return "local"
    if h in ("azure", "openrouter", "groq", "deepseek"):
        return h
    if m.startswith("gemini"):
        return "google"
    if m.startswith("grok"):
        return "xai"
    return None if not (h or m) else "other"


def _llm(connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(text("""
        SELECT COALESCE(SUM(cost_usd_micros) FILTER (WHERE NOT included_in_subscription), 0) AS cost,
               COALESCE(SUM(prompt_tokens), 0) AS tin, COALESCE(SUM(completion_tokens), 0) AS tout,
               COUNT(*) AS calls, COALESCE(SUM(turns), 0) AS turns,
               COALESCE(SUM(retries), 0) AS retries
        FROM llm_calls WHERE code_task_run_id = :id"""), {"id": run_id}).mappings().first() or {}
    top = connection.execute(text("""
        SELECT engine, model FROM llm_calls WHERE code_task_run_id = :id AND stage = 'implement'
        GROUP BY engine, model ORDER BY SUM(total_tokens) DESC LIMIT 1"""),
        {"id": run_id}).mappings().first()
    return {**dict(row), "engine": (top or {}).get("engine"), "model": (top or {}).get("model")}


def _decisions(connection, project_id: str, ids: Iterable[str], key: str,
               value: str) -> dict[str, Any]:
    """Approvals, rejections and time to decide for the decisions that belong to a unit."""
    ids = [i for i in ids if i]
    rows = connection.execute(text(f"""
        SELECT status, created_at, approved_at, updated_at FROM decisions
        WHERE project_id = :p AND (id = ANY(:ids) OR recommendation->>'{key}' = :v)"""),
        {"p": project_id, "ids": ids or [""], "v": value}).mappings().all()
    approvals = sum(1 for r in rows if r["status"] in ("approved", "executed", "merged"))
    rejections = sum(1 for r in rows if r["status"] in ("rejected", "revision_requested"))
    waits = [_secs(r["created_at"], r["approved_at"] or (
        r["updated_at"] if r["status"] not in ("pending",) else None)) for r in rows]
    waits = [w for w in waits if w is not None]
    return {"approvals": approvals, "rejections": rejections,
            "decide_wait_s": int(sum(waits) / len(waits)) if waits else None,
            "wait_on_person_s": int(sum(waits)) if waits else None}


def _role_shape(connection, project_id: str, role_key: Optional[str]) -> dict[str, Any]:
    if not role_key:
        return {"role_builtin": None, "prompt_version": None, "skills": [], "mcp_tools": []}
    from .improve_allowlist import builtin_keys

    fp = connection.execute(text(
        "SELECT fingerprint FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role_key}).scalar()
    skills = connection.execute(text(
        "SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r "
        "ORDER BY skill_id"), {"p": project_id, "r": role_key}).scalars().all()
    mcp = connection.execute(text(
        "SELECT mcp_server_id FROM role_mcp_servers WHERE project_id = :p AND role_key = :r "
        "ORDER BY mcp_server_id"), {"p": project_id, "r": role_key}).scalars().all()
    return {"role_builtin": role_key in builtin_keys()["roles"],
            "prompt_version": (str(fp)[:16] if fp else None),
            "skills": list(skills), "mcp_tools": list(mcp)}


def _effective_knob(connection, project_id: str, key: str) -> Optional[int]:
    from ..config import get_settings
    from .project_settings import effective

    try:
        return _int(effective(connection, project_id, get_settings(), key))
    except Exception:  # noqa: BLE001
        return None


def _is_quick_change(fc: Mapping[str, Any]) -> bool:
    """workflow_costs._changes' predicate: feedback-driven work outside any workflow run."""
    if fc.get("workflow_run_id"):
        return False
    if str(fc.get("is_followup") or "").lower() == "true" or fc.get("is_followup") is True:
        return True
    history = fc.get("feedback_history") or []
    if (_int(fc.get("attempt")) or 1) > 1 and history:
        note = str((history[-1] or {}).get("fix_note") or "")
        return bool(note) and not note.startswith(("auto", "Auto"))
    return False


def _read_code_task(connection, run_id: str) -> Optional[dict[str, Any]]:
    r = connection.execute(text("""
        SELECT r.id, r.project_id, r.status, r.phase, r.spec, r.state, r.usage, r.finalize_ctx,
               r.pull_request_number, r.decision_id, r.created_at, r.updated_at,
               qg.adversarial_verdict, qg.status AS qg_status, qg.gates_passed, qg.gates_failed
        FROM code_task_runs r
        LEFT JOIN quality_gates qg ON qg.code_task_run_id = r.id AND qg.project_id = r.project_id
        WHERE r.id = :id"""), {"id": run_id}).mappings().first()
    if not r:
        return None
    spec, state = _j(r["spec"], {}), _j(r["state"], {})
    usage, fc = _j(r["usage"], {}), _j(r["finalize_ctx"], {})
    if spec.get("simulated") or fc.get("simulated"):
        return None
    status = str(r["status"])
    terminal = {"completed": "completed", "failed": "failed", "cancelled": "cancelled"}.get(status)
    if not terminal:
        return None
    project_id = str(r["project_id"])
    kind = "quick_change" if _is_quick_change(fc) else "build"
    llm = _llm(connection, run_id)
    pinned = (spec.get("model_config") or {}).get("default")
    pinned = (pinned.get("engine") if isinstance(pinned, dict)
              else str(pinned).split(":", 1)[0] if pinned else None)
    harness = state.get("build_engine") or state.get("engine") or pinned or llm.get("engine")
    model = state.get("model") or usage.get("model") or llm.get("model")
    tests = state.get("tests") if isinstance(state.get("tests"), list) else []
    if tests:
        tests_total = len(tests)
        tests_passed = sum(1 for t in tests if isinstance(t, dict) and t.get("ok"))
    elif isinstance(state.get("tests_passed"), bool):
        tests_total, tests_passed = 1, int(state["tests_passed"])
    else:
        tests_total = tests_passed = None
    gp, gf = _j(r["gates_passed"], None), _j(r["gates_failed"], None)
    verdict = r["adversarial_verdict"]
    # WHO BUILT IT, for every build (defect 8): the spec's agent, else the run's act, else the
    # agent that took the implement stage — builds a workflow step made carry no spec role.
    role_key = spec.get("role_key") or connection.execute(text(
        "SELECT role_key FROM agent_acts WHERE code_task_run_id = :id AND kind = 'run' "
        "AND role_key IS NOT NULL ORDER BY started_at LIMIT 1"), {"id": run_id}).scalar() \
        or connection.execute(text(
            "SELECT role_key FROM agent_turns WHERE code_task_run_id = :id AND role_key IS NOT NULL "
            "AND stage = 'implement' LIMIT 1"), {"id": run_id}).scalar() \
        or connection.execute(text(
            "SELECT role FROM llm_calls WHERE code_task_run_id = :id AND role IS NOT NULL "
            "AND stage = 'implement' LIMIT 1"), {"id": run_id}).scalar()
    # AND WHICH WORKFLOW it was a step of: the run's key, version and whether it is ours.
    wf = {}
    wf_run = fc.get("workflow_run_id")
    if wf_run:
        got = connection.execute(text(
            "SELECT d.workflow_key, d.is_catalogue, v.version FROM workflow_runs r "
            "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
            "JOIN workflow_versions v ON v.id = r.workflow_version_id WHERE r.id = :id"),
            {"id": wf_run}).mappings().first()
        if got:
            from .improve_allowlist import builtin_keys

            wf = {"workflow_key": got["workflow_key"], "workflow_version": _int(got["version"]),
                  "workflow_builtin": bool(got["is_catalogue"])
                  or got["workflow_key"] in builtin_keys()["workflows"]}
    elif isinstance(fc.get("workflow"), dict) and fc["workflow"].get("workflow_key"):
        wf = {"workflow_key": fc["workflow"]["workflow_key"]}
    rounds = connection.execute(text(
        "SELECT COUNT(*) FROM events WHERE entity_type = 'code_task_run' AND entity_id = :id "
        "AND event_type IN ('code_task.inrun_convergence_round', 'code_task.review_repair_round')"),
        {"id": run_id}).scalar() or 0
    shepherd = connection.execute(text(
        "SELECT COUNT(*) FROM events WHERE entity_type = 'code_task_run' AND entity_id = :id "
        "AND event_type LIKE 'shepherd.%'"), {"id": run_id}).scalar() or 0
    tool_calls = connection.execute(text(
        "SELECT COUNT(*) FROM agent_turns WHERE code_task_run_id = :id "
        "AND direction = 'tool_call'"), {"id": run_id}).scalar() or 0
    pr_state = None
    if r["decision_id"]:
        pr_state = connection.execute(text("SELECT status FROM decisions WHERE id = :d"),
                                      {"d": r["decision_id"]}).scalar()
    pr_opened = r["pull_request_number"] is not None
    # MERGED ONLY WHEN IT WAS (pr_merge): an approved merge question is not a merged PR.
    from .pr_merge import MERGED, merged_shas, state as merge_state

    pr_merged = (merge_state(
        {"pull_request_number": r["pull_request_number"], "finalize_ctx": fc},
        {"status": pr_state,
         "merged_sha": merged_shas(connection, r["project_id"], [r["decision_id"]]).get(
             str(r["decision_id"]), "") if r["decision_id"] else ""}) == MERGED) \
        if pr_opened else None
    pr_reverted = None
    if pr_merged:
        pr_reverted = bool(connection.execute(text(
            "SELECT 1 FROM code_task_runs WHERE project_id = :p AND id <> :id "
            "AND status = 'completed' AND title ILIKE 'revert%' AND title LIKE :num LIMIT 1"),
            {"p": project_id, "id": run_id, "num": f"%#{r['pull_request_number']}%"}).scalar())
    deploy = _deploy_facts(connection, "code_task_run_id", run_id)
    dec = _decisions(connection, project_id, [r["decision_id"]], "code_task_run_id", run_id)
    origin = fc.get("origin") or {}
    context = sorted({name for name, present in (
        ("research", origin.get("discovery_run_id") or fc.get("discovery_run_id")),
        ("prd", origin.get("meeting_run_id") or origin.get("prd_artifact_id")),
        ("prior_attempts", fc.get("feedback_history")),
        ("plan", fc.get("plan") or state.get("plan_ok") is not None),
    ) if present})
    followup = bool(fc.get("is_followup") or fc.get("parent_run_id") or (
        _int(fc.get("attempt")) or 1) > 1)
    task_kind = ("fix" if fc.get("feedback_history") or fc.get("failure_class") else
                 "add_feature") if followup else "new_app"
    files = _int(state.get("files_changed"))
    task_size = None if files is None else ("small" if files <= 3 else
                                            "medium" if files <= 15 else "large")
    steers = len(fc.get("applied_guidance") or []) + len(fc.get("pending_guidance") or [])
    return {
        "unit_kind": kind, "project_id": project_id,
        "run_id": fc.get("workflow_run_id"), **wf,
        "step_key": None, "role_key": role_key,
        **_role_shape(connection, project_id, role_key),
        "harness": harness, "model": model, "provider": _provider(harness, model),
        "review_lanes": _int(spec.get("review_panel_size") or state.get("review_panel_size")),
        "review_rounds": _int(spec.get("review_rounds")) if spec.get("review_rounds") is not None
        else _effective_knob(connection, project_id, "code_review_inrun_max_rounds"),
        "turn_budget": _effective_knob(connection, project_id, "code_agent_max_turns"),
        "retry_max": _int(state.get("repair_max") or spec.get("repair_retries")),
        "context_policy": context, "task_kind": task_kind, "task_size": task_size,
        "terminal_state": terminal,
        "tests_passed": tests_passed, "tests_total": tests_total,
        "existing_tests_intact": state.get("existing_tests_intact")
        if isinstance(state.get("existing_tests_intact"), bool) else None,
        "gates_passed": len(gp) if isinstance(gp, list) else None,
        "gates_failed": len(gf) if isinstance(gf, list) else None,
        "failed_gates": [str(g)[:60] for g in gf] if isinstance(gf, list) else [],
        "review_approved": {"survived": True, "refuted": False}.get(str(verdict or "")),
        "review_rounds_used": int(rounds),
        **{k: dec[k] for k in ("approvals", "rejections", "decide_wait_s", "wait_on_person_s")},
        "steers": steers, "pr_opened": pr_opened, "pr_merged": pr_merged,
        "pr_reverted": pr_reverted, **deploy,
        "cost_usd": round(float(llm.get("cost") or 0) / 1_000_000, 4),
        "tokens_in": int(llm.get("tin") or 0), "tokens_out": int(llm.get("tout") or 0),
        "model_calls": int(llm.get("calls") or 0), "tool_calls": int(tool_calls),
        "turns": _int(state.get("turns")) or int(llm.get("turns") or 0),
        "wall_time_s": _secs(r["created_at"], r["updated_at"]),
        "retries": max(0, (_int(fc.get("attempt")) or 1) - 1),
        "shepherd_interventions": int(shepherd),
        "error_class": _word(fc.get("failure_class") or fc.get("reason_code")) if terminal != "completed" else None,
        "started_at": r["created_at"], "ended_at": r["updated_at"],
    }


def _deploy_facts(connection, column: str, value: str) -> dict[str, Any]:
    """First check, 24h verdict and rollback for the newest deploy of a build or run."""
    d = connection.execute(text(f"""
        SELECT id, target_id, status, finished_at, updated_at FROM stack_deployments
        WHERE {column} = :v AND status IN ('healthy', 'failed')
        ORDER BY created_at DESC LIMIT 1"""), {"v": value}).mappings().first()
    if not d:
        return {"deploy_healthy": None, "deploy_healthy_24h": None, "rolled_back": None}
    healthy = d["status"] == "healthy"
    rolled = bool(connection.execute(text(
        "SELECT 1 FROM stack_deployments WHERE rollback_of = :id LIMIT 1"), {"id": d["id"]}).scalar())
    at = d["finished_at"] or d["updated_at"]
    h24 = None
    if healthy and at is not None and at < datetime.now(timezone.utc) - timedelta(hours=25):
        got = connection.execute(text("""
            SELECT bool_and(ok) AS all_ok, COUNT(*) AS n FROM deployment_health_samples
            WHERE deployment_target_id = :t AND checked_at BETWEEN :a AND :b"""),
            {"t": d["target_id"], "a": at + timedelta(hours=23),
             "b": at + timedelta(hours=25)}).mappings().first()
        if got and got["n"]:
            h24 = bool(got["all_ok"])
    return {"deploy_healthy": healthy, "deploy_healthy_24h": h24, "rolled_back": rolled}


_WF_TERMINAL = {"completed": "completed", "failed": "failed", "deadlettered": "failed",
                "cancelled": "cancelled", "compensated": "rolled_back"}


def _read_workflow_run(connection, run_id: str) -> Optional[dict[str, Any]]:
    r = connection.execute(text("""
        SELECT wr.id, wr.project_id, wr.status, wr.created_at, wr.updated_at, wr.completed_at,
               wr.mode, wd.workflow_key, wd.namespace, wd.is_catalogue, wv.version
        FROM workflow_runs wr
        JOIN workflow_definitions wd ON wd.id = wr.workflow_definition_id
        JOIN workflow_versions wv ON wv.id = wr.workflow_version_id
        WHERE wr.id = :id"""), {"id": run_id}).mappings().first()
    # A TEST RUN (workflow_test_runs) is not production work: it is never scored.
    if not r or r["namespace"] != "product" or r.get("mode") == "test":
        return None
    terminal = _WF_TERMINAL.get(str(r["status"]))
    if not terminal:
        return None
    from .improve_allowlist import builtin_keys

    project_id = str(r["project_id"])
    builds = connection.execute(text("""
        SELECT COUNT(*) AS n, SUM(tests_passed) AS tp, SUM(tests_total) AS tt,
               SUM(gates_passed) AS gp, SUM(gates_failed) AS gf,
               bool_and(review_approved) AS review, bool_or(pr_opened) AS pr_opened,
               bool_or(pr_merged) AS merged, bool_or(pr_reverted) AS reverted,
               SUM(steers) AS steers, SUM(retries) AS retries,
               mode() WITHIN GROUP (ORDER BY harness) AS harness,
               mode() WITHIN GROUP (ORDER BY model) AS model
        FROM run_scorecards WHERE run_id = :id AND unit_kind IN ('build', 'quick_change')"""),
        {"id": run_id}).mappings().first() or {}
    acts = connection.execute(text("""
        SELECT COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(tokens_in), 0) AS tin,
               COALESCE(SUM(tokens_out), 0) AS tout, COUNT(*) AS n
        FROM agent_acts WHERE workflow_run_id = :id AND kind IN ('step', 'stage', 'seat', 'run')"""),
        {"id": run_id}).mappings().first() or {}
    dec = _decisions(connection, project_id, [], "workflow_run_id", run_id)
    has_builds = int(builds.get("n") or 0) > 0
    return {
        "unit_kind": "workflow_run", "project_id": project_id, "run_id": run_id,
        "workflow_key": r["workflow_key"], "workflow_version": _int(r["version"]),
        "workflow_builtin": bool(r["is_catalogue"]) or r["workflow_key"] in builtin_keys()["workflows"],
        "harness": builds.get("harness"), "model": builds.get("model"),
        "provider": _provider(builds.get("harness"), builds.get("model")),
        "terminal_state": terminal,
        "tests_passed": _int(builds.get("tp")) if has_builds else None,
        "tests_total": _int(builds.get("tt")) if has_builds else None,
        "gates_passed": _int(builds.get("gp")) if has_builds else None,
        "gates_failed": _int(builds.get("gf")) if has_builds else None,
        "review_approved": builds.get("review") if has_builds else None,
        "pr_opened": builds.get("pr_opened") if has_builds else None,
        "pr_merged": builds.get("merged") if has_builds else None,
        "pr_reverted": builds.get("reverted") if has_builds else None,
        **_deploy_facts(connection, "workflow_run_id", run_id),
        **{k: dec[k] for k in ("approvals", "rejections", "decide_wait_s", "wait_on_person_s")},
        "steers": int(builds.get("steers") or 0), "retries": int(builds.get("retries") or 0),
        "cost_usd": round(float(acts.get("cost") or 0), 4),
        "tokens_in": int(acts.get("tin") or 0), "tokens_out": int(acts.get("tout") or 0),
        "model_calls": int(acts.get("n") or 0),
        "wall_time_s": _secs(r["created_at"], r["completed_at"] or r["updated_at"]),
        "started_at": r["created_at"], "ended_at": r["completed_at"] or r["updated_at"],
    }


_STEP_TERMINAL = {"succeeded": "completed", "failed": "failed", "deadlettered": "failed",
                  "cancelled": "cancelled"}


def _read_step(connection, step_run_id: str) -> Optional[dict[str, Any]]:
    s = connection.execute(text("""
        SELECT s.id, s.project_id, s.workflow_run_id, s.step_id, s.step_kind, s.status, s.attempt,
               s.max_attempts, s.output, s.started_at, s.completed_at, s.created_at, s.updated_at,
               wr.mode, wd.workflow_key, wd.namespace, wd.is_catalogue, wv.version
        FROM workflow_step_runs s
        JOIN workflow_runs wr ON wr.id = s.workflow_run_id
        JOIN workflow_definitions wd ON wd.id = wr.workflow_definition_id
        JOIN workflow_versions wv ON wv.id = wr.workflow_version_id
        WHERE s.id = :id"""), {"id": step_run_id}).mappings().first()
    if not s or s["namespace"] != "product" or s.get("mode") == "test":
        return None
    terminal = _STEP_TERMINAL.get(str(s["status"]))
    if not terminal:
        return None
    # ONE RULE, live and backfill (defect 7): a step cancelled before it started did nothing to
    # score — it gets no scorecard at all.
    if s["status"] == "cancelled" and s["started_at"] is None:
        return None
    from .improve_allowlist import builtin_keys

    project_id = str(s["project_id"])
    output = _j(s["output"], {}) or {}
    ran_as = output.get("ran_as") if isinstance(output.get("ran_as"), dict) else {}
    act = connection.execute(text("""
        SELECT role_key, COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(tokens_in), 0) AS tin,
               COALESCE(SUM(tokens_out), 0) AS tout, COUNT(*) AS n
        FROM agent_acts WHERE workflow_run_id = :r AND step_id = :s
        GROUP BY role_key ORDER BY COUNT(*) DESC LIMIT 1"""),
        {"r": s["workflow_run_id"], "s": s["step_id"]}).mappings().first() or {}
    role_key = ran_as.get("agent") or act.get("role_key")
    child = output.get("code_task_run_id") or (output.get("run_id")
                                                if str(output.get("run_id") or "").startswith("crun") else None)
    build = {}
    if child:
        build = dict(connection.execute(text(
            "SELECT tests_passed, tests_total, gates_passed, gates_failed, review_approved, "
            "pr_opened, pr_merged, harness, model, steers FROM run_scorecards "
            "WHERE unit_kind IN ('build', 'quick_change') AND unit_id = :id"),
            {"id": str(child)}).mappings().first() or {})
    return {
        "unit_kind": "step", "project_id": project_id, "run_id": s["workflow_run_id"],
        "workflow_key": s["workflow_key"], "workflow_version": _int(s["version"]),
        "workflow_builtin": bool(s["is_catalogue"]) or s["workflow_key"] in builtin_keys()["workflows"],
        "step_key": s["step_id"], "role_key": role_key,
        **_role_shape(connection, project_id, role_key),
        "retry_max": _int(s["max_attempts"]),
        "terminal_state": terminal, **{k: v for k, v in build.items() if k != "steers"},
        "provider": _provider(build.get("harness"), build.get("model")),
        "steers": int(build.get("steers") or 0),
        "retries": max(0, (_int(s["attempt"]) or 1) - 1),
        "cost_usd": round(float(act.get("cost") or 0), 4),
        "tokens_in": int(act.get("tin") or 0), "tokens_out": int(act.get("tout") or 0),
        "model_calls": int(act.get("n") or 0),
        "wall_time_s": _secs(s["started_at"] or s["created_at"], s["completed_at"] or s["updated_at"]),
        "started_at": s["started_at"] or s["created_at"],
        "ended_at": s["completed_at"] or s["updated_at"],
    }


def _read_panel(connection, meeting_run_id: str) -> Optional[dict[str, Any]]:
    m = connection.execute(text("""
        SELECT id, project_id, status, state, usage, decision_id, created_at, updated_at
        FROM meeting_runs WHERE id = :id"""), {"id": meeting_run_id}).mappings().first()
    if not m:
        return None
    terminal = {"completed": "completed", "failed": "failed",
                "cancelled": "cancelled"}.get(str(m["status"]))
    if not terminal:
        return None
    state, usage = _j(m["state"], {}) or {}, _j(m["usage"], {}) or {}
    prd = None
    if m["decision_id"]:
        st = connection.execute(text("SELECT status FROM decisions WHERE id = :d"),
                                {"d": m["decision_id"]}).scalar()
        prd = True if st in ("approved", "executed") else False if st in (
            "rejected", "revision_requested") else None
    dec = _decisions(connection, str(m["project_id"]), [m["decision_id"]], "meeting_run_id",
                     meeting_run_id)
    if prd is False and terminal == "completed":
        terminal = "rejected"
    return {
        "unit_kind": "panel", "project_id": str(m["project_id"]), "role_key": None,
        "terminal_state": terminal, "prd_approved": prd,
        "panel_agreed": bool(state.get("synthesis")) if terminal != "failed" else False,
        "model": usage.get("model"), "provider": _provider(None, usage.get("model")),
        "cost_usd": round(float(usage.get("cost_usd_micros") or 0) / 1_000_000, 4),
        **{k: dec[k] for k in ("approvals", "rejections", "decide_wait_s", "wait_on_person_s")},
        "wall_time_s": _secs(m["created_at"], m["updated_at"]),
        "started_at": m["created_at"], "ended_at": m["updated_at"],
    }


def _read_research(connection, discovery_run_id: str) -> Optional[dict[str, Any]]:
    d = connection.execute(text("""
        SELECT id, project_id, status, state, usage, created_at, updated_at
        FROM discovery_runs WHERE id = :id"""), {"id": discovery_run_id}).mappings().first()
    if not d:
        return None
    terminal = {"completed": "completed", "failed": "failed"}.get(str(d["status"]))
    if not terminal:
        return None
    gate = ((_j(d["state"], {}) or {}).get("gate") or {})
    usage = _j(d["usage"], {}) or {}
    conf = gate.get("confidence")
    try:
        conf = float(conf)
        conf = conf / 100.0 if conf > 1 else conf
    except (TypeError, ValueError):
        conf = None
    verdict = _word(gate.get("verdict"))
    return {
        "unit_kind": "research", "project_id": str(d["project_id"]),
        "terminal_state": terminal, "research_verdict": verdict, "confidence": conf,
        "model": usage.get("model"), "provider": _provider(None, usage.get("model")),
        "cost_usd": round(float(usage.get("cost_usd_micros") or 0) / 1_000_000, 4),
        "wall_time_s": _secs(d["created_at"], d["updated_at"]),
        "started_at": d["created_at"], "ended_at": d["updated_at"],
    }


_READERS = {
    "workflow_run": _read_workflow_run, "step": _read_step, "panel": _read_panel,
    "research": _read_research, "build": _read_code_task, "quick_change": _read_code_task,
}

_COLUMNS = (
    "project_id", "run_id", "workflow_key", "workflow_version", "workflow_builtin", "step_key",
    "role_key", "role_builtin", "prompt_version", "skills", "mcp_tools", "harness", "model",
    "provider", "review_lanes", "review_rounds", "turn_budget", "retry_max", "context_policy",
    "experiment_id", "experiment_arm", "task_kind", "task_size", "terminal_state",
    "tests_passed", "tests_total", "existing_tests_intact", "gates_passed", "gates_failed",
    "failed_gates", "review_approved", "review_rounds_used", "approvals", "rejections",
    "decide_wait_s", "steers", "pr_opened", "pr_merged", "pr_reverted", "deploy_healthy",
    "deploy_healthy_24h", "rolled_back", "research_verdict", "confidence", "panel_agreed",
    "prd_approved", "cost_usd", "tokens_in", "tokens_out", "model_calls", "tool_calls", "turns",
    "wall_time_s", "wait_on_person_s", "retries", "shepherd_interventions", "error_class",
    "rating", "over_median", "score", "score_version", "started_at", "ended_at", "source",
)
_JSON_COLUMNS = {"skills", "mcp_tools", "context_policy", "failed_gates", "over_median"}
_DEFAULTS = {"skills": [], "mcp_tools": [], "context_policy": [], "failed_gates": [],
             "approvals": 0, "rejections": 0, "steers": 0, "cost_usd": 0, "tokens_in": 0,
             "tokens_out": 0, "model_calls": 0, "tool_calls": 0, "turns": 0, "retries": 0,
             "shepherd_interventions": 0, "over_median": {}}

#: Which person_feedback subject rates which unit.
_RATED_AS = {"build": "build", "quick_change": "build", "workflow_run": "build",
             "research": "research", "panel": "panel"}


def _over_median(connection, card: Mapping[str, Any], unit_id: str) -> dict[str, bool]:
    row = connection.execute(text(f"""
        SELECT COUNT(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY cost_usd) AS cost,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY turns) AS turns,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY steers) AS steers
        FROM run_scorecards
        WHERE project_id = :p AND unit_kind = :k AND role_key IS NOT DISTINCT FROM :r
          AND unit_id <> :id AND terminal_state = 'completed'
          AND ended_at > now() - interval '{MEDIAN_WINDOW_DAYS} days'"""),
        {"p": card["project_id"], "k": card["unit_kind"], "r": card.get("role_key"),
         "id": unit_id}).mappings().first()
    if not row or int(row["n"] or 0) < MEDIAN_MIN_ROWS:
        return {}
    return {"cost": float(card.get("cost_usd") or 0) > float(row["cost"] or 0),
            "turns": float(card.get("turns") or 0) > float(row["turns"] or 0),
            "steers": float(card.get("steers") or 0) > float(row["steers"] or 0)}


def record_scorecard(connection, unit_kind: str, unit_id: str, *, source: str = "live",
                     late: bool = False) -> Optional[dict[str, Any]]:
    """Write this unit's scorecard from its own rows. None when it is not (yet) terminal.

    `unit_kind` "build" and "quick_change" both read the code task and the reader decides which
    it is, so one run never holds two rows. Idempotent: the same facts write the same row.
    """
    reader = _READERS.get(unit_kind)
    if reader is None or not unit_id:
        return None
    card = reader(connection, str(unit_id))
    if card is None:
        return None
    kind = card["unit_kind"]
    card = {**_DEFAULTS, **card}
    assigned = connection.execute(text(
        "SELECT experiment_id, arm FROM experiment_assignments "
        "WHERE unit_kind IN ('build', 'quick_change') AND unit_id = :id"),
        {"id": unit_id}).mappings().first() if kind in CODE_KINDS else None
    card["experiment_id"] = (assigned or {}).get("experiment_id")
    card["experiment_arm"] = (assigned or {}).get("arm")
    subject = _RATED_AS.get(kind)
    card["rating"] = connection.execute(text(
        "SELECT rating FROM person_feedback WHERE subject_kind = :s AND subject_id = :id "
        "AND rating IS NOT NULL ORDER BY updated_at DESC LIMIT 1"),
        {"s": subject, "id": unit_id}).scalar() if subject else None
    card["over_median"] = _over_median(connection, card, unit_id)
    w, version = weights(connection)
    card["unit_kind"] = kind
    card["score"] = score_of(card, w)
    card["score_version"] = version
    card["source"] = source
    params = {c: (json.dumps(card.get(c)) if c in _JSON_COLUMNS else card.get(c))
              for c in _COLUMNS}
    params.update({"unit_kind": kind, "unit_id": str(unit_id), "late": late})
    cols = ", ".join(_COLUMNS)
    vals = ", ".join(f"CAST(:{c} AS JSONB)" if c in _JSON_COLUMNS else f":{c}" for c in _COLUMNS)
    # A live record never demotes a backfilled row to 'backfill' or the reverse: the first
    # writer's source stands. Late outcomes stamp late_outcomes_at.
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c != "source")
    connection.execute(text(f"""
        INSERT INTO run_scorecards (unit_kind, unit_id, {cols}, computed_at, late_outcomes_at)
        VALUES (:unit_kind, :unit_id, {vals}, now(), CASE WHEN :late THEN now() END)
        ON CONFLICT (unit_kind, unit_id) DO UPDATE SET {updates}, computed_at = now(),
          late_outcomes_at = CASE WHEN :late THEN now() ELSE run_scorecards.late_outcomes_at END
    """), params)
    card["routing"] = stamp_routing(connection, kind, str(unit_id))
    if card["experiment_id"]:
        from .experiments import after_scorecard

        after_scorecard(connection, card["experiment_id"])
    return card


def routing_summary(routes: list[dict[str, Any]]) -> dict[str, Any]:
    """What the model router did for one unit (docs/product/MODEL_ROUTER.md, "(e) Learning"):
    the mode, the tier mix, escalations, work moved off a subscription and the classifier."""
    if not routes:
        return {}
    tiers: dict[str, int] = {}
    classifiers: dict[str, int] = {}
    for r in routes:
        tiers[str(r.get("tier") or "?")] = tiers.get(str(r.get("tier") or "?"), 0) + 1
        if r.get("classifier"):
            classifiers[str(r["classifier"])] = classifiers.get(str(r["classifier"]), 0) + 1
    primary = next((r for r in routes if r.get("step") in ("implement", "solo")), None) \
        or max(routes, key=lambda r: tiers.get(str(r.get("tier")), 0))
    return {
        "mode": routes[-1].get("mode"),
        "tier": primary.get("tier"),
        "tiers": tiers,
        "routes": len(routes),
        "escalations": sum(1 for r in routes if r.get("escalation")),
        "moved_by_headroom": sum(1 for r in routes if r.get("moved_by_headroom")),
        "classifier": max(classifiers, key=classifiers.get) if classifiers else None,
    }


def stamp_routing(connection, kind: str, unit_id: str) -> dict[str, Any]:
    """Copy the unit's routes onto its scorecard (route_mode, route_tier, routing). Best
    effort, in a savepoint: a store without the router's tables scores exactly as before."""
    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT step, tier, mode, classifier, escalation, moved_by_headroom "
                "FROM model_routes WHERE unit_id = :u ORDER BY created_at"),
                {"u": unit_id}).mappings().all()
            summary = routing_summary([dict(r) for r in rows])
            if summary:
                connection.execute(text(
                    "UPDATE run_scorecards SET route_mode = :m, route_tier = :t, "
                    "routing = CAST(:r AS JSONB) WHERE unit_kind = :k AND unit_id = :u"),
                    {"m": summary["mode"], "t": summary["tier"], "r": json.dumps(summary),
                     "k": kind, "u": unit_id})
            return summary
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------------------------------
# Hooks
# ------------------------------------------------------------------------------------------

#: Terminal events → the unit they finish. (event_type → (unit_kind, how to find its id))
_EVENT_UNITS: dict[str, tuple[str, str]] = {
    "workflow.run_completed": ("workflow_run", "entity"),
    "workflow.run_stopped": ("workflow_run", "entity"),
    "workflow.run_cancelled": ("workflow_run", "entity"),
    "workflow.compensation_completed": ("workflow_run", "entity"),
    "workflow.step_succeeded": ("step", "data:workflow_step_run_id"),
    "workflow.step_failed": ("step", "data:workflow_step_run_id"),
    "workflow.step_deadlettered": ("step", "data:workflow_step_run_id"),
    "code_task.failed": ("build", "entity"),
    "code_task.cancelled": ("build", "entity"),
    "code_task.pr_opened": ("build", "entity"),
    "code_task.completed": ("build", "entity"),
    "meeting.completed": ("panel", "meeting"),
    "discovery.completed": ("research", "entity"),
    "discovery.failed": ("research", "entity"),
}
_DECISION_EVENTS = ("decision.approved", "decision.rejected", "decision.executed",
                    "decision.revision_requested")


def on_event(connection, event_type: str, entity_type: str, entity_id: str,
             data: Mapping[str, Any]) -> None:
    """core.emit_event's hook: record the unit a terminal event finished. Never raises."""
    try:
        if event_type in _DECISION_EVENTS:
            _on_decision(connection, entity_id)
            return
        hit = _EVENT_UNITS.get(event_type)
        if hit is None:
            return
        kind, how = hit
        if how == "entity":
            unit_id = entity_id
        elif how == "meeting":
            unit_id = connection.execute(text(
                "SELECT id FROM meeting_runs WHERE meeting_id = :m ORDER BY created_at DESC LIMIT 1"),
                {"m": entity_id}).scalar()
        else:
            unit_id = (data or {}).get(how.split(":", 1)[1])
        if unit_id:
            with connection.begin_nested():
                record_scorecard(connection, kind, str(unit_id))
    except Exception:  # noqa: BLE001 — a scorecard never undoes what it records
        log.warning("scorecard: could not record after %s", event_type, exc_info=True)


def _on_decision(connection, decision_id: str) -> None:
    """A decision changed: re-record the finished units it belongs to (PRD, merge, plan)."""
    d = connection.execute(text("SELECT recommendation FROM decisions WHERE id = :d"),
                           {"d": decision_id}).scalar()
    rec = _j(d, {}) or {}
    targets: list[tuple[str, str]] = []
    for key, kind in (("code_task_run_id", "build"), ("workflow_run_id", "workflow_run"),
                      ("meeting_run_id", "panel")):
        if rec.get(key):
            targets.append((kind, str(rec[key])))
    for kind, table in (("build", "code_task_runs"), ("panel", "meeting_runs")):
        for uid in connection.execute(text(f"SELECT id FROM {table} WHERE decision_id = :d"),
                                      {"d": decision_id}).scalars().all():
            targets.append((kind, str(uid)))
    for kind, uid in dict.fromkeys(targets):
        exists = connection.execute(text(
            "SELECT 1 FROM run_scorecards WHERE unit_id = :id LIMIT 1"), {"id": uid}).scalar()
        if exists:
            with connection.begin_nested():
                record_scorecard(connection, kind, uid, late=True)


def settle(engine, code_task_run_id: str) -> None:
    """For a build's own `finally` (agent_acts.settle_code_task): its final numbers."""
    try:
        from ..db import begin

        with begin(engine) as c:
            record_scorecard(c, "build", code_task_run_id)
    except Exception:  # noqa: BLE001
        log.warning("scorecard: could not settle %s", code_task_run_id, exc_info=True)


# ------------------------------------------------------------------------------------------
# Sweep (= backfill), late outcomes, rescore — run by the daily tick's maintenance
# ------------------------------------------------------------------------------------------

_MISSING = {
    "build": ("SELECT r.id FROM code_task_runs r WHERE r.status IN ('completed','failed','cancelled') "
              "AND NOT (r.spec ? 'simulated') AND NOT EXISTS (SELECT 1 FROM run_scorecards s "
              "WHERE s.unit_kind IN ('build','quick_change') AND s.unit_id = r.id) "
              "ORDER BY r.updated_at DESC LIMIT :n"),
    "workflow_run": ("SELECT r.id FROM workflow_runs r JOIN workflow_definitions d "
                     "ON d.id = r.workflow_definition_id WHERE d.namespace = 'product' AND "
                     "r.mode <> 'test' AND r.status IN ('completed','failed','deadlettered','cancelled','compensated') "
                     "AND NOT EXISTS (SELECT 1 FROM run_scorecards s WHERE s.unit_kind = "
                     "'workflow_run' AND s.unit_id = r.id) ORDER BY r.updated_at DESC LIMIT :n"),
    "step": ("SELECT s.id FROM workflow_step_runs s JOIN workflow_runs r ON r.id = s.workflow_run_id "
             "JOIN workflow_definitions d ON d.id = r.workflow_definition_id WHERE d.namespace = "
             "'product' AND r.mode <> 'test' "
             "AND s.status IN ('succeeded','failed','deadlettered','cancelled') "
             "AND NOT (s.status = 'cancelled' AND s.started_at IS NULL) AND NOT "
             "EXISTS (SELECT 1 FROM run_scorecards c WHERE c.unit_kind = 'step' AND c.unit_id = "
             "s.id) ORDER BY s.updated_at DESC LIMIT :n"),
    "panel": ("SELECT m.id FROM meeting_runs m WHERE m.status IN ('completed','failed','cancelled') "
              "AND NOT EXISTS (SELECT 1 FROM run_scorecards s WHERE s.unit_kind = 'panel' AND "
              "s.unit_id = m.id) ORDER BY m.updated_at DESC LIMIT :n"),
    "research": ("SELECT d.id FROM discovery_runs d WHERE d.status IN ('completed','failed') AND "
                 "NOT EXISTS (SELECT 1 FROM run_scorecards s WHERE s.unit_kind = 'research' AND "
                 "s.unit_id = d.id) ORDER BY d.updated_at DESC LIMIT :n"),
}


def sweep(connection, *, limit: int = SWEEP_BATCH) -> dict[str, int]:
    """Record every terminal unit that has no scorecard yet: the safety net behind the hooks AND
    the backfill (history is just units nobody recorded). Builds first: runs read theirs."""
    done: dict[str, int] = {}
    for kind in ("build", "research", "panel", "step", "workflow_run"):
        ids = connection.execute(text(_MISSING[kind]), {"n": int(limit)}).scalars().all()
        n = 0
        for uid in ids:
            try:
                with connection.begin_nested():
                    if record_scorecard(connection, kind, str(uid), source="backfill"):
                        n += 1
            except Exception:  # noqa: BLE001
                log.warning("scorecard sweep: %s %s", kind, uid, exc_info=True)
        done[kind] = n
    return done


def late_outcomes(connection, *, limit: int = 200) -> int:
    """Re-record units 25h+ after they ended, once: merged/reverted, 24h health, rollback."""
    rows = connection.execute(text("""
        SELECT unit_kind, unit_id FROM run_scorecards
        WHERE late_outcomes_at IS NULL AND unit_kind IN ('build','quick_change','workflow_run','panel')
          AND ended_at < now() - interval '25 hours' AND ended_at > now() - interval '30 days'
        ORDER BY ended_at LIMIT :n"""), {"n": int(limit)}).mappings().all()
    n = 0
    for r in rows:
        try:
            with connection.begin_nested():
                if record_scorecard(connection, r["unit_kind"], r["unit_id"], late=True) is None:
                    connection.execute(text(
                        "UPDATE run_scorecards SET late_outcomes_at = now() "
                        "WHERE unit_kind = :k AND unit_id = :id"),
                        {"k": r["unit_kind"], "id": r["unit_id"]})
                n += 1
        except Exception:  # noqa: BLE001
            log.warning("scorecard late outcome: %s", r["unit_id"], exc_info=True)
    return n


_SCORE_INPUTS = ("unit_kind", "unit_id", "terminal_state", "tests_passed", "tests_total",
                 "gates_passed", "gates_failed", "review_approved", "existing_tests_intact",
                 "approvals", "rejections", "prd_approved", "panel_agreed", "confidence",
                 "pr_merged", "pr_reverted", "rolled_back", "deploy_healthy_24h", "over_median")


def rescore(connection, *, limit: Optional[int] = RESCORE_BATCH) -> int:
    """Rows scored under other weights get this version's score, from their stored signals.
    Reads only the score's inputs and writes back in one statement per 2,000 rows."""
    w, version = weights(connection)
    total = 0
    while True:
        rows = connection.execute(text(
            f"SELECT {', '.join(_SCORE_INPUTS)} FROM run_scorecards "
            "WHERE score_version IS DISTINCT FROM :v LIMIT :n"),
            {"v": version, "n": RESCORE_BATCH}).mappings().all()
        if not rows:
            break
        values = []
        for r in rows:
            card = dict(r)
            card["over_median"] = _j(card.get("over_median"), {})
            values.append({"k": r["unit_kind"], "id": r["unit_id"], "s": score_of(card, w)})
        connection.execute(text("""
            UPDATE run_scorecards AS t SET score = v.s, score_version = :v
            FROM (SELECT x->>'k' AS k, x->>'id' AS id, (x->>'s')::numeric AS s
                  FROM jsonb_array_elements(CAST(:rows AS JSONB)) AS x) AS v
            WHERE t.unit_kind = v.k AND t.unit_id = v.id"""),
            {"v": version, "rows": json.dumps(values)})
        total += len(rows)
        if limit is not None and total >= limit:
            break
    return total


def maintain(connection) -> dict[str, Any]:
    """The tick's scorecard work, bounded per call."""
    # Rows an earlier rule wrote for steps cancelled before they started go (defect 7).
    connection.execute(text("""
        DELETE FROM run_scorecards s USING workflow_step_runs w
        WHERE s.unit_kind = 'step' AND s.unit_id = w.id AND w.status = 'cancelled'
          AND w.started_at IS NULL"""))
    return {"swept": sweep(connection), "late": late_outcomes(connection),
            "rescored": rescore(connection)}


# ------------------------------------------------------------------------------------------
# The Observability › Scorecards view
# ------------------------------------------------------------------------------------------

GROUPS = {"role": "role_key", "workflow": "workflow_key", "harness": "harness", "model": "model",
          "routing": "route_mode", "tier": "route_tier"}
WINDOWS = {"7d": 7, "30d": 30, "90d": 90}


def _href(project_id: str, kind: str, unit_id: str, run_id: Optional[str]) -> str:
    """The console's own address for the unit (the same links the Factory, Workflows, Panels
    and Research screens use)."""
    from ..app_links.console import build_console_link

    if kind in CODE_KINDS:
        return build_console_link("code_tasks", project_id=project_id, build=unit_id)
    if kind == "workflow_run":
        return build_console_link("delivery_workflows", project_id=project_id, wfr=unit_id)
    if kind == "step":
        return (build_console_link("delivery_workflows", project_id=project_id, wfr=run_id)
                if run_id else build_console_link("delivery_workflows", project_id=project_id))
    if kind == "panel":
        return build_console_link("meetings", project_id=project_id, meeting=unit_id)
    return build_console_link("discovery", project_id=project_id, run=unit_id)


def _title(connection, kind: str, unit_id: str) -> str:
    """What the console calls the unit (local only: this never leaves the install)."""
    if kind in ("workflow_run", "step"):
        got = connection.execute(text(
            "SELECT d.name, s.step_id FROM workflow_runs r "
            "JOIN workflow_definitions d ON d.id = r.workflow_definition_id "
            "LEFT JOIN workflow_step_runs s ON s.id = :id "
            "WHERE r.id = COALESCE((SELECT workflow_run_id FROM workflow_step_runs WHERE id = :id), :id)"),
            {"id": unit_id}).mappings().first()
        if got:
            return (f"{got['name']} · {got['step_id']}" if got["step_id"] else str(got["name"]))[:80]
    table = {"build": "code_task_runs", "quick_change": "code_task_runs",
             "panel": "meeting_runs", "research": "discovery_runs"}.get(kind)
    if table:
        col = "COALESCE(title, directive)" if table == "meeting_runs" else "title"
        got = connection.execute(text(f"SELECT {col} FROM {table} WHERE id = :id"),
                                 {"id": unit_id}).scalar()
        if got:
            return str(got)[:80]
    return unit_id


def view(connection, project_id: str, *, group: str = "role", win: str = "30d",
         kind: str = "build") -> dict[str, Any]:
    column = GROUPS.get(group)
    days = WINDOWS.get(win)
    if column is None or days is None:
        raise ValueError("group must be role|workflow|harness|model and win 7d|30d|90d")
    kinds = list(CODE_KINDS) if kind == "build" else [kind] if kind in UNIT_KINDS else None
    if kinds is None:
        raise ValueError(f"kind must be one of {', '.join(UNIT_KINDS)}")
    w, version = weights(connection)
    # NEVER A BLANK VIEW (defect 4): if no row carries the current version yet, show the newest
    # version that exists, and say which.
    if not connection.execute(text(
            "SELECT 1 FROM run_scorecards WHERE project_id = :p AND score_version = :v LIMIT 1"),
            {"p": project_id, "v": version}).scalar():
        newest = connection.execute(text(
            "SELECT score_version FROM run_scorecards WHERE project_id = :p "
            "ORDER BY computed_at DESC LIMIT 1"), {"p": project_id}).scalar()
        version = newest or version
    base = ("FROM run_scorecards WHERE project_id = :p AND unit_kind = ANY(:k) "
            "AND score_version = :v AND ended_at > now() - make_interval(days => :d)")
    params = {"p": project_id, "k": kinds, "v": version, "d": days}
    total = connection.execute(text(f"""
        SELECT COUNT(*) AS n, AVG(score) AS score, SUM(cost_usd) AS cost,
               AVG((terminal_state = 'completed')::int) AS done,
               AVG(pr_merged::int) FILTER (WHERE pr_opened) AS merged,
               COUNT(*) FILTER (WHERE source = 'backfill') AS backfilled {base}"""),
        params).mappings().first() or {}
    rows = connection.execute(text(f"""
        SELECT COALESCE({column}, '—') AS key, COUNT(*) AS n, AVG(score) AS score,
               STDDEV_SAMP(score) AS sd, AVG((terminal_state = 'completed')::int) AS done,
               AVG(pr_merged::int) FILTER (WHERE pr_opened) AS merged,
               SUM(cost_usd) AS cost, AVG(turns) AS turns, AVG(steers) AS steers,
               AVG(review_rounds_used) AS rounds
        {base} GROUP BY 1 ORDER BY COUNT(*) DESC, 1 LIMIT 50"""), params).mappings().all()
    worst = connection.execute(text(f"""
        SELECT unit_kind, unit_id, run_id, score, steers, review_rounds_used, cost_usd, role_key,
               terminal_state, error_class, ended_at
        {base} AND score < :low ORDER BY score ASC, cost_usd DESC LIMIT 6"""),
        {**params, "low": LOW_SCORE}).mappings().all()
    steered = connection.execute(text(f"""
        SELECT unit_kind, unit_id, run_id, score, steers, review_rounds_used, cost_usd, role_key,
               terminal_state, error_class, ended_at
        {base} AND steers > 0 ORDER BY steers DESC, score ASC LIMIT 4"""), params).mappings().all()
    rounds = connection.execute(text(f"""
        SELECT unit_kind, unit_id, run_id, score, steers, review_rounds_used, cost_usd, role_key,
               terminal_state, error_class, ended_at
        {base} AND review_rounds_used > 1 ORDER BY review_rounds_used DESC, score ASC LIMIT 4"""),
        params).mappings().all()

    role_names = names_for(connection, project_id, "role",
                           [r["role_key"] for r in list(worst) + list(steered) + list(rounds)])
    row_names = names_for(connection, project_id, group, [r["key"] for r in rows])

    def item(r: Mapping[str, Any], why: str) -> dict[str, Any]:
        return {"unit_kind": r["unit_kind"], "unit_id": r["unit_id"], "why": why,
                "title": _title(connection, r["unit_kind"], r["unit_id"]),
                "score": float(r["score"]) if r["score"] is not None else None,
                "steers": int(r["steers"] or 0), "rounds": int(r["review_rounds_used"] or 0),
                "cost_usd": float(r["cost_usd"] or 0), "role_key": r["role_key"],
                "role_name": role_names.get(r["role_key"]) if r["role_key"] else None,
                "state": r["terminal_state"], "error_class": r["error_class"],
                "ended_at": r["ended_at"],
                "href": _href(project_id, r["unit_kind"], r["unit_id"], r["run_id"])}

    costing: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r, why in ([(r, "low") for r in worst] + [(r, "steers") for r in steered]
                   + [(r, "rounds") for r in rounds]):
        if r["unit_id"] in seen:
            continue
        seen.add(r["unit_id"])
        costing.append(item(r, why))

    def num(v: Any, nd: int = 3) -> Optional[float]:
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)

    return {
        "group": group, "win": win, "kind": kind, "score_version": version,
        "weights": [{"key": k, "label": WEIGHT_LABELS[k], "value": w[k],
                     "default": DEFAULT_WEIGHTS[k]} for k in DEFAULT_WEIGHTS],
        "total": {"n": int(total.get("n") or 0), "score": num(total.get("score")),
                  "cost_usd": num(total.get("cost"), 2), "done": num(total.get("done")),
                  "merged": num(total.get("merged")),
                  "backfilled": int(total.get("backfilled") or 0)},
        "rows": [{"key": r["key"], "label": row_names.get(r["key"], r["key"]), "n": int(r["n"]), "score": num(r["score"]), "sd": num(r["sd"]),
                  "done": num(r["done"]), "merged": num(r["merged"]), "cost_usd": num(r["cost"], 2),
                  "turns": num(r["turns"], 1), "steers": num(r["steers"], 2),
                  "rounds": num(r["rounds"], 2)} for r in rows],
        "costing": costing,
    }
