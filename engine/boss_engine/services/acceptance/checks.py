"""ACCEPTANCE CHECKS (epic BOSSAPP-1213; boss-acceptance-agent-lld.md §2): every acceptance check
BOSS ran, kept as a row — the in-build seat (`layer='build_gate'`, static) and the Acceptance
Agent (`layer='step'`, exercise/environment).

A check is what the findings ledger observes (ledger.observe_check: `first_seen_check_id`,
`resolved_check_id`), and what a re-check replays: its `commands` are the session's command
record (session.CommandRecord — every command it ran, with exit code, trimmed output and
duration) and its `probes` the replayable probes distilled from them (probes.py), so the next
check of the lineage proves unchanged items again WITHOUT a model call (AA-E).
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from .verdict import clip

LAYERS = ("build_gate", "step")
MODES = ("static", "exercise", "environment")
VERDICTS = ("met", "not_met", "not_run", "no_items")

#: What a check row keeps of each item (the judge's record has more: tags, sources).
_ITEM_KEYS = ("id", "n", "text", "source", "status", "evidence", "evidence_refs", "judgement",
              "replayed", "unverified", "flaky", "pinned")


def _j(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default if value is None else value


def _items(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for i in record.get("items") or []:
        row = {k: i.get(k) for k in _ITEM_KEYS if i.get(k) is not None}
        if isinstance(row.get("text"), str):
            row["text"] = row["text"][:2000]
        if isinstance(row.get("evidence"), str):
            # Never cut mid-sentence; BOSS's appended reason kept whole (verdict.clip).
            row["evidence"] = clip(row["evidence"], 1200)
        out.append(row)
    return out


def _row(r: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for k, d in (("counts", {}), ("items", []), ("probes", []), ("commands", []),
                 ("diff", {}), ("budget", {})):
        out[k] = _j(out.get(k), d)
    return out


def get(connection: Any, check_id: str) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    row = connection.execute(text("SELECT * FROM acceptance_checks WHERE id = :i"),
                             {"i": check_id}).mappings().first()
    return _row(row) if row else None


def latest(connection: Any, *, project_id: str, lineage_root_run_id: Optional[str] = None,
           pr_url: Optional[str] = None, layer: Optional[str] = None,
           head_sha: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The newest check of a lineage (or a pull request), optionally of one layer / head."""
    from sqlalchemy import text

    row = connection.execute(text(
        "SELECT * FROM acceptance_checks WHERE project_id = :p "
        "AND (CAST(:r AS TEXT) IS NULL OR lineage_root_run_id = :r) "
        "AND (CAST(:u AS TEXT) IS NULL OR pr_url = :u) "
        "AND (CAST(:l AS TEXT) IS NULL OR layer = :l) "
        "AND (CAST(:h AS TEXT) IS NULL OR head_sha = :h) "
        "ORDER BY created_at DESC, id DESC LIMIT 1"),
        {"p": project_id, "r": lineage_root_run_id, "u": pr_url, "l": layer,
         "h": head_sha}).mappings().first()
    return _row(row) if row else None


def record_check(connection: Any, *, project_id: str, lineage_root_run_id: str, layer: str,
                 mode: str, record: Mapping[str, Any], contract_id: Optional[str] = None,
                 code_task_run_id: Optional[str] = None, workflow_run_id: Optional[str] = None,
                 step_id: Optional[str] = None, head_sha: Optional[str] = None,
                 pr_url: Optional[str] = None, commands: Iterable[Any] = (),
                 probes: Iterable[Mapping[str, Any]] = (),
                 previous_check_id: Optional[str] = "_latest", engine: Optional[str] = None,
                 model: Optional[str] = None, cost_usd_micros: int = 0, duration_ms: int = 0,
                 budget: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Insert one check from the judge's record (verdict.judge) and return the row. The diff
    (newly met / regressed / still open) is against `previous_check_id` — by default the
    lineage's newest check of the same layer."""
    from sqlalchemy import text

    from ..core import make_id
    from .verdict import diff as _diff

    if layer not in LAYERS or mode not in MODES:
        raise ValueError(f"layer must be one of {LAYERS} and mode one of {MODES}")
    verdict = str(record.get("verdict") or "not_run")
    if verdict not in VERDICTS:
        verdict = "not_run"
    prev = None
    if previous_check_id == "_latest":
        prev = latest(connection, project_id=project_id, lineage_root_run_id=lineage_root_run_id,
                      layer=layer)
        previous_check_id = prev["id"] if prev else None
    elif previous_check_id:
        prev = get(connection, previous_check_id)
    items = _items(record)
    cmds = [c.to_dict() if hasattr(c, "to_dict") else dict(c) for c in commands or []]
    check_id = make_id("acc")
    connection.execute(text(
        "INSERT INTO acceptance_checks (id, project_id, contract_id, lineage_root_run_id, "
        "code_task_run_id, workflow_run_id, step_id, layer, mode, head_sha, pr_url, verdict, why, "
        "counts, items, probes, commands, previous_check_id, diff, engine, model, cost_usd_micros, "
        "duration_ms, budget) VALUES (:i, :p, :c, :r, :run, :w, :s, :l, :m, :h, :u, :v, :why, "
        "CAST(:counts AS JSONB), CAST(:items AS JSONB), CAST(:probes AS JSONB), "
        "CAST(:cmds AS JSONB), :prev, CAST(:diff AS JSONB), :e, :model, :cost, :dur, "
        "CAST(:budget AS JSONB))"),
        {"i": check_id, "p": project_id, "c": contract_id, "r": lineage_root_run_id,
         "run": code_task_run_id, "w": workflow_run_id, "s": step_id, "l": layer, "m": mode,
         "h": head_sha, "u": pr_url, "v": verdict, "why": str(record.get("why") or "")[:2000],
         "counts": json.dumps(record.get("counts") or {}), "items": json.dumps(items, default=str),
         "probes": json.dumps(list(probes or []), default=str),
         "cmds": json.dumps(cmds, default=str), "prev": previous_check_id,
         "diff": json.dumps(_diff(prev, {"items": items}) if prev else {}),
         "e": engine, "model": model or record.get("model"),
         "cost": int(cost_usd_micros or 0), "dur": int(duration_ms or 0),
         "budget": json.dumps(dict(budget or {}))})
    return get(connection, check_id) or {"id": check_id}


__all__ = ["LAYERS", "MODES", "VERDICTS", "get", "latest", "record_check"]
