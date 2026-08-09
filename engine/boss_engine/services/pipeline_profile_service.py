"""Pipeline profiles as a managed resource — presets (in code) + custom profiles (in the DB),
a per-project default, and edition gating. Lets customers pick the cost/rigor dial and, on
enterprise, define their own profiles for different kinds of work.
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from .core import emit_event, make_id
from .pipeline_profiles import DEFAULT_PROFILE, PROFILES

_COLS = ("name", "label", "tier_floor", "tier_cap", "model_routing",
         "caching", "parallel_checks", "is_default", "advisory_gate", "panel_checks")


def _preset_field(name: str, field: str, default):
    """Fall back to the PRESET's value for a field the DB row leaves NULL.

    Every column added here is a semantic a preset defines. A row that predates the column
    must not silently redefine `lean` as blocking just because its cell is empty — that is
    exactly how `implement` ended up on opus at $6.10 after being routed to the cheap lane:
    the Python constant was right and the stale DB row won.
    """
    return PROFILES.get(name, {}).get(field, default)


def _panel_checks(r):
    """Which adversarial seats run. NULL row cell -> the preset's list; absent -> None,
    which the runtime reads as 'the full panel'."""
    v = r["panel_checks"]
    if v is None:
        return _preset_field(r["name"], "panel_checks", None)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return None
    return list(v) if isinstance(v, list) else None


def _row_to_profile(r) -> dict:
    routing = r["model_routing"]
    if isinstance(routing, str):
        try:
            routing = json.loads(routing)
        except Exception:
            routing = {}
    return {
        "name": r["name"], "label": r["label"],
        "tier_floor": r["tier_floor"], "tier_cap": r["tier_cap"],
        "model": routing or {}, "caching": r["caching"],
        "parallel_checks": r["parallel_checks"], "is_default": bool(r["is_default"]),
        "advisory_gate": bool(r["advisory_gate"]) if r["advisory_gate"] is not None
        else bool(_preset_field(r["name"], "advisory_gate", False)),
        "panel_checks": _panel_checks(r),
        "custom": r["name"] not in PROFILES,
    }


def _preset(name: str, *, is_default: bool = False) -> dict:
    p = PROFILES[name]
    return {
        "name": name, "label": p["label"], "tier_floor": p["tier_floor"],
        "tier_cap": p["tier_cap"], "model": p.get("model", {}),
        "caching": p.get("caching", True), "parallel_checks": p.get("parallel_checks", True),
        "advisory_gate": bool(p.get("advisory_gate", False)),
        "panel_checks": p.get("panel_checks"),
        "is_default": is_default, "custom": False,
    }


def _custom_rows(connection, project_id: str) -> dict[str, dict]:
    rows = connection.execute(
        text(f"SELECT {', '.join(_COLS)} FROM pipeline_profiles WHERE project_id=:p"),
        {"p": project_id},
    ).mappings().all()
    return {r["name"]: _row_to_profile(r) for r in rows}


def list_profiles(connection, project_id: str) -> list[dict]:
    """All profiles for a project: built-in presets plus any custom ones. A DB row for a preset
    name overrides the preset (e.g. to mark it default)."""
    custom = _custom_rows(connection, project_id)
    out = [custom.pop(name) if name in custom else _preset(name) for name in PROFILES]
    out.extend(custom.values())  # purely custom profiles
    return out


def default_profile_name(connection, project_id: str, settings: Settings) -> str:
    row = connection.execute(
        text("SELECT name FROM pipeline_profiles WHERE project_id=:p AND is_default=true LIMIT 1"),
        {"p": project_id},
    ).mappings().first()
    if row:
        return row["name"]
    # No project default chosen: community-edition trials default to the lowest-cost profile so
    # a first trial never looks expensive; paid editions use the configured default.
    from .commercial import CommercialService

    if CommercialService(settings).normalize_edition() == "community":
        return "lean"
    return settings.pipeline_profile or DEFAULT_PROFILE


def resolve_for_run(
    connection, project_id: str, override: Optional[str], settings: Settings
) -> dict:
    """Per-task override > project default > edition default. Returns a profile dict the runtime
    consumes (name/tier_floor/tier_cap/model/caching/parallel_checks)."""
    name = (override or "").strip() or default_profile_name(connection, project_id, settings)
    row = connection.execute(
        text(f"SELECT {', '.join(_COLS)} FROM pipeline_profiles WHERE project_id=:p AND name=:n"),
        {"p": project_id, "n": name},
    ).mappings().first()
    if row:
        return _row_to_profile(row)
    if name in PROFILES:
        return _preset(name)
    return _preset(DEFAULT_PROFILE)


def create_custom_profile(
    connection, *, project_id: str, name: str, label: Optional[str],
    tier_floor: int, tier_cap: int, model_routing: dict, caching: bool,
    parallel_checks: bool, actor: str, settings: Settings,
) -> dict:
    from .commercial import CommercialService

    if CommercialService(settings).normalize_edition() == "community":
        raise PermissionError(
            "Custom pipeline profiles require BOSS Enterprise; community edition uses the "
            "lean / balanced / strict presets."
        )
    if name in PROFILES:
        raise ValueError(f"'{name}' is a built-in profile name; choose a different name.")
    name = name.strip().lower()
    if not name:
        raise ValueError("profile name is required")
    tier_floor = max(0, min(3, int(tier_floor)))
    tier_cap = max(tier_floor, min(3, int(tier_cap)))
    pid = make_id("pprof")
    connection.execute(
        text("""
            INSERT INTO pipeline_profiles
                (id, project_id, name, label, tier_floor, tier_cap, model_routing,
                 caching, parallel_checks, created_by)
            VALUES (:id, :p, :n, :l, :tf, :tc, CAST(:mr AS JSONB), :ca, :pc, :by)
            ON CONFLICT (project_id, name) DO UPDATE SET
                label=EXCLUDED.label, tier_floor=EXCLUDED.tier_floor, tier_cap=EXCLUDED.tier_cap,
                model_routing=EXCLUDED.model_routing, caching=EXCLUDED.caching,
                parallel_checks=EXCLUDED.parallel_checks
        """),
        {"id": pid, "p": project_id, "n": name, "l": label or name,
         "tf": tier_floor, "tc": tier_cap, "mr": json.dumps(model_routing or {}),
         "ca": bool(caching), "pc": bool(parallel_checks), "by": actor},
    )
    emit_event(connection, project_id, "pipeline_profile.created", actor,
               "pipeline_profile", pid, {"name": name})
    return {"id": pid, "name": name}


def set_default_profile(connection, project_id: str, name: str, actor: str) -> None:
    name = name.strip().lower()
    is_preset = name in PROFILES
    if not is_preset:
        exists = connection.execute(
            text("SELECT 1 FROM pipeline_profiles WHERE project_id=:p AND name=:n"),
            {"p": project_id, "n": name},
        ).first()
        if not exists:
            raise ValueError(f"Unknown profile '{name}'.")
    connection.execute(
        text("UPDATE pipeline_profiles SET is_default=false WHERE project_id=:p"),
        {"p": project_id},
    )
    if is_preset:
        p = PROFILES[name]
        connection.execute(
            text("""
                INSERT INTO pipeline_profiles
                    (id, project_id, name, label, tier_floor, tier_cap, model_routing,
                     caching, parallel_checks, is_default, created_by)
                VALUES (:id, :p, :n, :l, :tf, :tc, CAST(:mr AS JSONB), :ca, :pc, true, :by)
                ON CONFLICT (project_id, name) DO UPDATE SET is_default=true
            """),
            {"id": make_id("pprof"), "p": project_id, "n": name, "l": p["label"],
             "tf": p["tier_floor"], "tc": p["tier_cap"], "mr": json.dumps(p.get("model", {})),
             "ca": p.get("caching", True), "pc": p.get("parallel_checks", True), "by": actor},
        )
    else:
        connection.execute(
            text("UPDATE pipeline_profiles SET is_default=true WHERE project_id=:p AND name=:n"),
            {"p": project_id, "n": name},
        )
    emit_event(connection, project_id, "pipeline_profile.default_set", actor,
               "pipeline_profile", name, {"name": name})


# --------------------------------------------------------------------------- #
# Temporarily withdrawing a profile
# --------------------------------------------------------------------------- #
FALLBACK_PROFILE = "balanced"


def usable_profile(requested: Optional[str], settings=None) -> tuple[str, Optional[str]]:
    """The profile that will actually run, and why it differs from what was asked for.

    An operator needs to be able to say "strict is not fit to run yet" once, rather than
    hunting every hardcoded `profile="strict"` in the codebase and remembering to put them all
    back. `BOSS_DISABLED_PROFILES` is that switch; clearing it restores the profile with no
    code change.

    Downgrades rather than refuses. A benchmark or a relaunch that errors because a profile is
    withdrawn is a dead end for whoever pressed the button; running it at `balanced` and
    saying so keeps the work moving and keeps the substitution visible.
    """
    name = (requested or "").strip().lower()
    if not name:
        return DEFAULT_PROFILE, None
    disabled = {
        p.strip().lower()
        for p in str(getattr(settings, "disabled_profiles", "") or "").split(",")
        if p.strip()
    }
    if name in disabled:
        return FALLBACK_PROFILE, (
            f"the `{name}` profile is disabled on this platform "
            f"(BOSS_DISABLED_PROFILES); running `{FALLBACK_PROFILE}` instead")
    return name, None
