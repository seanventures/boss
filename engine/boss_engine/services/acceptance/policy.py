"""ACCEPTANCE POLICY (AA-G, BOSSAPP-1222; boss-acceptance-agent-lld.md §6.3):
profile -> mode, budget, auto-followup rounds, and action when not accepted.

  lean     — labels & files tickets, NEVER waits (`on_not_met='label'`, `auto_rounds=0`)
  balanced — one automatic follow-up round, then parks on a decision card (`on_not_met='decide'`, `auto_rounds=1`)
  strict   — blocks until fixed or waived (`on_not_met='block'`, `auto_rounds=1`)
  vag      — balanced defaults with strong (frontier) verification model

FLOORS:
  - Budget exhaustion NEVER passes: unanswered items become `unjudged`, verdict is `not_met`
    with `why='budget'`.
  - Downstream promote/deploy never auto-proceeds past `not_met` without a person (I5).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from . import verdict as _verdict

MODES = ("auto", "static", "exercise", "environment")
ON_NOT_MET = ("profile", "label", "decide", "block")
FILE_MODES = ("auto", "on", "off", "ledger_only")

#: Per-profile acceptance defaults (LLD §6.3).
DEFAULTS: dict[str, dict[str, Any]] = {
    "lean": {
        "mode": "auto",
        "usd": 0.60,
        "minutes": 10,
        "max_turns": 24,
        "model": "strong",
        "on_not_met": "label",
        "auto_rounds": 0,
        "file": "auto",
        "max_new_tickets": 10,
    },
    "balanced": {
        "mode": "auto",
        "usd": 1.50,
        "minutes": 20,
        "max_turns": 40,
        "model": "strong",
        "on_not_met": "decide",
        "auto_rounds": 1,
        "file": "auto",
        "max_new_tickets": 10,
    },
    "strict": {
        "mode": "auto",
        "usd": 3.00,
        "minutes": 35,
        "max_turns": 60,
        "model": "strong",
        "on_not_met": "block",
        "auto_rounds": 1,
        "file": "auto",
        "max_new_tickets": 10,
    },
    "vag": {
        "mode": "auto",
        "usd": 1.50,
        "minutes": 20,
        "max_turns": 40,
        "model": "strong",
        "on_not_met": "decide",
        "auto_rounds": 1,
        "file": "auto",
        "max_new_tickets": 10,
    },
}


@dataclass(frozen=True)
class AcceptancePolicy:
    """Resolved acceptance policy for one step / check run."""

    profile: str = "balanced"
    mode: str = "auto"
    usd: float = 1.50
    minutes: int = 20
    max_turns: int = 40
    model: str = "strong"
    on_not_met: str = "decide"
    auto_rounds: int = 1
    file: str = "auto"
    max_new_tickets: int = 10

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]], *,
                     profile: str = "balanced") -> "AcceptancePolicy":
        base = dict(DEFAULTS.get(str(profile or "balanced").lower(), DEFAULTS["balanced"]))
        data = dict(raw or {})
        prof = str(data.get("profile") or profile or "balanced").strip().lower()
        if prof in DEFAULTS:
            base = dict(DEFAULTS[prof])
        mode = str(data.get("mode") or base["mode"]).strip().lower()
        if mode not in MODES:
            mode = base["mode"]
        on_not_met = str(data.get("on_not_met") or base["on_not_met"]).strip().lower()
        if on_not_met not in ("label", "decide", "block"):
            on_not_met = base["on_not_met"]
        file_mode = str(data.get("file") or base["file"]).strip().lower()
        if file_mode not in FILE_MODES:
            file_mode = base["file"]
        usd_val = data.get("usd", data.get("budget_usd", base["usd"]))
        min_val = data.get("minutes", data.get("budget_minutes", base["minutes"]))
        turns_val = data.get("max_turns", base["max_turns"])
        rounds_val = data.get("auto_rounds", base["auto_rounds"])
        cap_val = data.get("max_new_tickets", base["max_new_tickets"])
        return cls(
            profile=prof,
            mode=mode,
            usd=max(0.05, float(usd_val if usd_val is not None else base["usd"])),
            minutes=max(1, int(min_val if min_val is not None else base["minutes"])),
            max_turns=max(1, int(turns_val if turns_val is not None else base["max_turns"])),
            model=str(data.get("model") or base["model"]).strip(),
            on_not_met=on_not_met,
            auto_rounds=max(0, int(rounds_val if rounds_val is not None else base["auto_rounds"])),
            file=file_mode,
            max_new_tickets=max(1, int(cap_val if cap_val is not None else base["max_new_tickets"])),
        )


def for_run(
    connection: Any,
    project_id: str,
    *,
    settings: Any = None,
    profile_override: Optional[str] = None,
    step_inputs: Optional[Mapping[str, Any]] = None,
) -> AcceptancePolicy:
    """Resolve the acceptance policy for a project/run via pipeline_profile_service."""
    from .. import pipeline_profile_service as pps
    from ...config import get_settings

    resolved_name = str(profile_override or "").strip().lower() or "balanced"
    custom_acc: dict[str, Any] = {}
    if connection is not None and project_id:
        try:
            cfg = settings if settings is not None else get_settings()
            prof = pps.resolve_for_run(connection, project_id, profile_override, cfg)
            resolved_name = str(prof.get("name") or resolved_name).strip().lower()
            model_cfg = prof.get("model")
            if isinstance(prof.get("acceptance"), Mapping):
                custom_acc = dict(prof["acceptance"])
            elif isinstance(model_cfg, Mapping) and isinstance(model_cfg.get("acceptance"), Mapping):
                custom_acc = dict(model_cfg["acceptance"])
        except Exception:  # noqa: BLE001 — fall back to preset defaults
            pass
    base = dict(DEFAULTS.get(resolved_name, DEFAULTS["balanced"]))
    merged = {**base, **custom_acc, "profile": resolved_name}

    inputs = dict(step_inputs or {})
    if inputs.get("mode") in MODES:
        merged["mode"] = str(inputs["mode"]).strip().lower()
    if inputs.get("budget_usd") is not None:
        try:
            merged["usd"] = float(inputs["budget_usd"])
        except (TypeError, ValueError):
            pass
    if inputs.get("budget_minutes") is not None:
        try:
            merged["minutes"] = int(inputs["budget_minutes"])
        except (TypeError, ValueError):
            pass
    override_action = str(inputs.get("on_not_met") or "").strip().lower()
    if override_action in ("label", "decide", "block"):
        merged["on_not_met"] = override_action
    return AcceptancePolicy.from_mapping(merged, profile=resolved_name)


def resolve_mode(
    requested_mode: Optional[str] = "auto",
    *,
    deployed_url: Optional[str] = None,
    deployment_id: Optional[str] = None,
    root: Any = None,
) -> Literal["static", "exercise", "environment"]:
    """Resolve `auto` -> `environment` | `exercise` | `static` (LLD §6.1)."""
    req = str(requested_mode or "auto").strip().lower()
    if req in ("static", "exercise", "environment"):
        return req  # type: ignore[return-value]
    if str(deployed_url or "").strip() or str(deployment_id or "").strip():
        return "environment"
    if root is not None:
        path = Path(root)
        if path.is_dir():
            from ..app_root import APP_MANIFESTS

            for name in (*APP_MANIFESTS, "Makefile", "docker-compose.yml", "compose.yaml"):
                if (path / name).exists():
                    return "exercise"
            # Also check 1-level subdirectories for monorepos.
            for child in path.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    if any((child / m).exists() for m in APP_MANIFESTS):
                        return "exercise"
            return "static"
    return "exercise"


Action = Literal["succeed", "label", "auto_followup", "decide", "block", "retry"]


def action(
    verdict: str,
    policy: AcceptancePolicy | Mapping[str, Any],
    step_override: Optional[str] = None,
    *,
    auto_rounds_done: int = 0,
    human_open: int = 0,
) -> Action:
    """Decide how the workflow step settles (LLD §6.2 & §6.3).

    - `not_run` -> `retry` (infra failure; retryable, not a coder failure)
    - `no_items` -> `succeed`
    - `met`:
        * if `human_open > 0`: `label` on lean, else `decide` (person confirms HUMAN items)
        * else: `succeed`
    - `not_met`:
        * effective `label` (lean) -> `label` (never waits)
        * effective `decide` (balanced) or `block` (strict):
            - while `auto_rounds_done < policy.auto_rounds` -> `auto_followup`
            - else -> `decide` or `block`
    """
    pol = (policy if isinstance(policy, AcceptancePolicy)
           else AcceptancePolicy.from_mapping(policy))
    v = str(verdict or "not_run").strip().lower()
    override = str(step_override or "").strip().lower()
    effective = override if override in ("label", "decide", "block") else pol.on_not_met

    if v == "not_run":
        return "retry"
    if v == "no_items":
        return "succeed"
    if v == "met":
        if int(human_open or 0) > 0:
            return "label" if effective == "label" else "decide"
        return "succeed"
    # not_met (or anything unknown -> fail closed)
    if effective == "label":
        return "label"
    if int(auto_rounds_done or 0) < pol.auto_rounds:
        return "auto_followup"
    return "block" if effective == "block" else "decide"


def enforce_budget(
    record: Mapping[str, Any],
    *,
    spent_usd: float,
    budget_usd: float,
    elapsed_seconds: Optional[float] = None,
    budget_minutes: Optional[int] = None,
    exhausted: bool = False,
) -> dict[str, Any]:
    """Enforce the budget floor (LLD §6.3): budget exhaustion NEVER passes.

    Unanswered items stay `unjudged`, verdict becomes `not_met` with `why='budget'`, and the
    record carries a clear budget summary + card note.
    """
    out = dict(record)
    over_usd = budget_usd > 0 and float(spent_usd or 0.0) > float(budget_usd)
    over_time = (
        budget_minutes is not None
        and elapsed_seconds is not None
        and budget_minutes > 0
        and float(elapsed_seconds) > float(budget_minutes) * 60.0
    )
    hit = bool(exhausted or over_usd or over_time)
    elapsed_min = round(float(elapsed_seconds or 0.0) / 60.0, 2)
    out["budget"] = {
        "usd": round(float(spent_usd or 0.0), 4),
        "limit_usd": round(float(budget_usd or 0.0), 4),
        "minutes": elapsed_min,
        "limit_minutes": int(budget_minutes or 0),
        "exhausted": hit,
    }
    if not hit:
        return out
    items = [dict(i) for i in (out.get("items") or [])]
    unjudged_count = sum(1 for i in items if i.get("status") == _verdict.UNJUDGED)
    if out.get("verdict") == "met" and unjudged_count == 0 and not exhausted:
        # Everything was already verified met before the ceiling tripped.
        return out
    counts = dict(out.get("counts") or {})
    counts[_verdict.UNJUDGED] = unjudged_count
    out["verdict"] = "not_met"
    out["counts"] = counts
    out["why"] = "budget"
    note = (
        f"Check stopped at budget: {unjudged_count} item{'s' if unjudged_count != 1 else ''} "
        f"not judged"
        if unjudged_count
        else f"Check stopped at budget (${float(spent_usd or 0.0):.2f} / ${budget_usd:.2f})"
    )
    out["budget_note"] = note
    return out


def decision_options(settled_action: str, *, human_only: bool = False) -> dict[str, Any]:
    """What choices a person sees on an acceptance decision card (LLD §6.2)."""
    if human_only:
        return {
            "kind": "acceptance_human_confirm",
            "options": ["confirm_human", "send_back", "stop"],
            "requires_reason_on_accept": False,
            "requires_waiver": False,
        }
    if settled_action == "block":
        return {
            "kind": "acceptance_blocked",
            "options": ["send_back", "stop"],
            "requires_reason_on_accept": True,
            "requires_waiver": True,
        }
    return {
        "kind": "acceptance_decide",
        "options": ["send_back", "accept_with_tickets", "stop"],
        "requires_reason_on_accept": True,
        "requires_waiver": False,
    }


__all__ = [
    "AcceptancePolicy",
    "DEFAULTS",
    "FILE_MODES",
    "MODES",
    "ON_NOT_MET",
    "action",
    "decision_options",
    "enforce_budget",
    "for_run",
    "resolve_mode",
]
