"""RETENTION RULES AND THRESHOLDS (Layer 1 policy the Infra Agent enforces; LLD §4.1, §4.3, §5.1).

A rule is `retention_rules(kind, host_id NULL = every host, rule JSONB, enabled, source)`. The
defaults below ship enabled so the cleanup is boring and predictable from day one — except
`backups`, which the LLD leaves to the first incident ("ask once, automatic after": the card
carries the rule proposal; approve → row with source 'learned'). Thresholds are a rule of kind
`threshold`, editable on the Storage card; the proposed 20/15/25/10 are the defaults.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from .core import make_id

KINDS = ("backups", "scheduled_backups", "images", "build_cache", "logs", "telemetry", "scratch_db", "kg_derived",
         "docker_trim", "threshold")

#: Thresholds as proposed (LLD §4.1 table) — settings, not constants.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "warn_free_pct": 20.0, "critical_free_pct": 15.0, "target_free_pct": 25.0,
    "floor_free_pct": 10.0, "floor_free_gb": 5.0,
    "warn_days": 7.0, "critical_days": 3.0,
    "pg_temp_critical_gb": 5.0, "docker_unreclaimed_gb": 40.0,
    # Exit bands (hysteresis): warn clears at warn_exit_free_pct AND warn_exit_days.
    "warn_exit_free_pct": 24.0, "warn_exit_days": 10.0, "pg_temp_exit_gb": 1.0,
}

#: What ships enabled. `backups` is proposed by the first incident, not pre-approved (LLD §12).
DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "images": {"keep_versions": 2},             # current + prev-<tag> (upgrade.sh)
    "build_cache": {"cap_gb": 10},              # docker builder prune --keep-storage
    "logs": {"max_size_mb": 50, "max_file": 3}, # compose logging caps (Layer 1) + truncate
    "telemetry": {"keep_days": 30, "tables": {}},  # per-table days: telemetry_retention.POLICIES
    "scratch_db": {"ttl_hours": 24},            # boss_scratch_* databases
    "kg_derived": {"unused_days": 14},
    "docker_trim": {"enabled": True},
}
#: The rule the first backups incident proposes (plan §2: keep 10 / 7 days / 20 GB).
PROPOSED_BACKUP_RULE: dict[str, Any] = {"keep_newest": 10, "keep_days": 7, "cap_gb": 20}

LABELS = {
    "backups": "Backups", "scheduled_backups": "Scheduled backups", "images": "Images", "build_cache": "Build cache", "logs": "Logs",
    "telemetry": "Telemetry", "scratch_db": "Scratch DBs", "kg_derived": "KG derived",
    "docker_trim": "Docker trim", "threshold": "Thresholds",
}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def rule_label(kind: str, rule: Mapping[str, Any]) -> str:
    """The chip on the Storage card: ≤ 4 words."""
    r = dict(rule or {})
    if kind == "scheduled_backups":
        return f"keep {r.get('keep_newest', '?')} · {r.get('cap_gb', '?')} GB"
    if kind == "backups":
        return f"keep {r.get('keep_newest', '?')} · {r.get('keep_days', '?')} d · {r.get('cap_gb', '?')} GB"
    if kind == "images":
        return f"keep {r.get('keep_versions', 2)} versions"
    if kind == "build_cache":
        return f"cap {r.get('cap_gb', 10)} GB"
    if kind == "logs":
        return f"{r.get('max_size_mb', 50)} MB × {r.get('max_file', 3)}"
    if kind == "telemetry":
        # Per-table days since ST-8 (telemetry_retention.POLICIES; overrides in `tables`).
        return "7–90 days per table"
    if kind == "scratch_db":
        return f"drop after {r.get('ttl_hours', 24)} h"
    if kind == "kg_derived":
        return f"unused {r.get('unused_days', 14)} d"
    if kind == "docker_trim":
        return "trim on" if r.get("enabled", True) else "trim off"
    if kind == "threshold":
        return (f"warn {r.get('warn_free_pct', 20):g} · crit {r.get('critical_free_pct', 15):g}"
                f" · target {r.get('target_free_pct', 25):g}")
    return kind


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    rule = _json(row.get("rule"))
    return {"id": row["id"], "host_id": row.get("host_id"), "kind": row["kind"], "rule": rule,
            "enabled": bool(row.get("enabled", True)), "source": row.get("source") or "default",
            "decision_id": row.get("decision_id"), "created_by": row.get("created_by"),
            "created_at": _iso(row.get("created_at")), "updated_at": _iso(row.get("updated_at")),
            "label": rule_label(str(row["kind"]), rule)}


def ensure_defaults(connection: Any) -> int:
    """Seed the shipped rules once (install-wide, source 'default'). Returns rows added."""
    added = 0
    for kind, rule in {**DEFAULT_RULES, "threshold": DEFAULT_THRESHOLDS}.items():
        got = connection.execute(text("""
            INSERT INTO retention_rules (id, host_id, kind, rule, enabled, source, created_by)
            VALUES (:id, NULL, :k, CAST(:r AS JSONB), true, 'default', 'boss')
            ON CONFLICT (kind, COALESCE(host_id, '')) DO NOTHING RETURNING id"""),
            {"id": make_id("rule"), "k": kind, "r": json.dumps(rule)}).scalar()
        added += 1 if got else 0
    return added


def list_rules(connection: Any, host_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Every rule that applies to `host_id` (its own rows win over install-wide ones)."""
    rows = connection.execute(text(
        "SELECT * FROM retention_rules WHERE host_id IS NULL OR host_id = :h "
        "ORDER BY kind, host_id NULLS LAST"), {"h": host_id or ""}).mappings().all()
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        kind = str(r["kind"])
        if kind not in seen or r.get("host_id"):
            seen[kind] = _public(r)
    return sorted(seen.values(), key=lambda x: KINDS.index(x["kind"]) if x["kind"] in KINDS else 99)


def rule_for(connection: Any, kind: str, host_id: Optional[str] = None,
             *, enabled_only: bool = True) -> Optional[dict[str, Any]]:
    for r in list_rules(connection, host_id):
        if r["kind"] == kind and (r["enabled"] or not enabled_only):
            return r
    return None


def thresholds(connection: Any, host_id: Optional[str] = None) -> dict[str, float]:
    found = rule_for(connection, "threshold", host_id, enabled_only=False)
    out = dict(DEFAULT_THRESHOLDS)
    for k, v in ((found or {}).get("rule") or {}).items():
        if k in out:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def upsert(connection: Any, *, kind: str, rule: Mapping[str, Any], host_id: Optional[str] = None,
           enabled: bool = True, source: str = "operator", actor: str = "boss",
           decision_id: Optional[str] = None, rule_id: Optional[str] = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "threshold":
        rule = {**thresholds(connection, host_id), **{k: float(v) for k, v in rule.items()
                                                      if k in DEFAULT_THRESHOLDS}}
    row = connection.execute(text("""
        INSERT INTO retention_rules (id, host_id, kind, rule, enabled, source, decision_id,
                                     created_by, updated_at)
        VALUES (:id, :h, :k, CAST(:r AS JSONB), :e, :s, :d, :by, now())
        ON CONFLICT (kind, COALESCE(host_id, '')) DO UPDATE SET rule = EXCLUDED.rule,
               enabled = EXCLUDED.enabled, source = EXCLUDED.source,
               decision_id = COALESCE(EXCLUDED.decision_id, retention_rules.decision_id),
               updated_at = now()
        RETURNING *"""),
        {"id": rule_id or make_id("rule"), "h": host_id, "k": kind, "r": json.dumps(dict(rule)),
         "e": bool(enabled), "s": source, "d": decision_id, "by": actor}).mappings().one()
    out = _public(row)
    try:
        from .core import emit_event

        pid = connection.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar()
        if pid:
            emit_event(connection, pid, "storage.rule.approved", actor, "retention_rule", out["id"],
                       {"kind": kind, "rule": dict(rule), "host_id": host_id, "source": source,
                        "enabled": bool(enabled)})
    except Exception:  # noqa: BLE001
        pass
    return out


def set_enabled(connection: Any, rule_id: str, enabled: bool) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "UPDATE retention_rules SET enabled = :e, updated_at = now() WHERE id = :id RETURNING *"),
        {"e": bool(enabled), "id": rule_id}).mappings().first()
    return _public(row) if row else None


__all__ = ["DEFAULT_RULES", "DEFAULT_THRESHOLDS", "KINDS", "LABELS", "PROPOSED_BACKUP_RULE",
           "ensure_defaults", "list_rules", "rule_for", "rule_label", "set_enabled", "thresholds",
           "upsert"]
