"""Token efficiency / consumption control — practical, measurable, no fake ML.

Read-only analytics over the canonical ``llm_calls`` ledger plus project/stage
token-budget policies. No external model calls. Extends the deterministic
cost-insights rules with scorecards, packing estimates, and profile-based
model-routing suggestions.

Surfaced at ``/v1/token-efficiency/*``.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .core import make_id, now_utc
from .cost_insights import build_insights
from .pipeline_profiles import DEFAULT_PROFILE, PROFILES, model_for_step, resolve_profile

# Heuristic used across BOSS for packing (toolchain_registry, knowledge pack).
# chars/4 ≈ token count without tiktoken; intentional and documented.
_CHARS_PER_TOKEN = 4

# Recovery DDL — IF NOT EXISTS, no invented rows. Matches db/schema.sql.
_TOKEN_BUDGET_POLICIES_DDL = """
CREATE TABLE IF NOT EXISTS token_budget_policies (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT '*',
  max_prompt_tokens INTEGER,
  max_completion_tokens INTEGER,
  max_total_tokens_per_call INTEGER,
  max_tokens_per_day BIGINT,
  max_cost_usd_micros_per_day BIGINT,
  alert_at_pct NUMERIC NOT NULL DEFAULT 0.80,
  preferred_model_class TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  version INTEGER NOT NULL DEFAULT 1,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (project_id, stage)
)
"""
_TOKEN_BUDGET_POLICIES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_token_budget_policies_project
  ON token_budget_policies (project_id, stage)
"""


def is_missing_relation(exc: BaseException) -> bool:
    """True for UndefinedTable / missing-relation SQL errors."""
    orig = getattr(exc, "orig", None)
    name = type(orig).__name__ if orig is not None else type(exc).__name__
    blob = str(orig or exc).lower()
    return name == "UndefinedTable" or "does not exist" in blob


@contextmanager
def _savepoint(connection: Connection) -> Iterator[None]:
    """Isolate a statement so a missing table does not abort the outer txn."""
    begin_nested = getattr(connection, "begin_nested", None)
    nested = None
    if callable(begin_nested):
        try:
            nested = begin_nested()
        except Exception:
            nested = None
    try:
        yield
        if nested is not None:
            nested.commit()
    except Exception:
        if nested is not None:
            try:
                nested.rollback()
            except Exception:
                pass
        raise


def ensure_tables(connection: Connection) -> bool:
    """Create token_budget_policies if missing. False when DDL cannot run."""
    try:
        with _savepoint(connection):
            connection.execute(text(_TOKEN_BUDGET_POLICIES_DDL))
            connection.execute(text(_TOKEN_BUDGET_POLICIES_INDEX))
        return True
    except Exception as exc:
        blob = str(exc).lower()
        if is_missing_relation(exc) or "permission" in blob or "read-only" in blob:
            return False
        raise


def unconfigured_policies(project_id: str) -> dict[str, Any]:
    """Documented defaults only — no stored rows, no invented consumption."""
    pid = (project_id or "").strip()
    stages = [PROJECT_DEFAULT_STAGE] + [
        s for s in KNOWN_STAGES if s in _DEFAULT_STAGE_CAPS
    ]
    policies = [_default_policy_row(pid, stage) for stage in stages]
    return {
        "project_id": pid,
        "count": len(policies),
        "policies": policies,
        "default_stage_key": PROJECT_DEFAULT_STAGE,
        "schema_ready": False,
        "configured": False,
    }


# Project-wide stage key in token_budget_policies.stage
PROJECT_DEFAULT_STAGE = "*"

# Stages known to the governed pipeline (aligned with llm_telemetry.STAGE_ROLES).
KNOWN_STAGES: tuple[str, ...] = (
    "standards",
    "spec",
    "plan",
    "review_plan",
    "design",
    "write_tests",
    "implement",
    "repair",
    "review_code",
    "consistency_check",
    "security_check",
    "check_tests",
    "run_all_tests",
    "guest",
    "solo",
)

# Stages the default profiles route to low_cost — used for recommendations.
_CHEAP_STAGES = frozenset({"standards", "spec", "plan", "review_plan", "design"})
_STRONG_STAGES = frozenset(
    {"review_code", "security_check", "consistency_check", "check_tests"}
)

# Default per-stage token budgets (tokens). Operators override via PUT /policies.
_DEFAULT_STAGE_CAPS: dict[str, dict[str, Any]] = {
    PROJECT_DEFAULT_STAGE: {
        "max_prompt_tokens": 100_000,
        "max_completion_tokens": 16_384,
        "max_total_tokens_per_call": 128_000,
        "max_tokens_per_day": 5_000_000,
        "max_cost_usd_micros_per_day": 50_000_000,  # $50
        "alert_at_pct": 0.80,
        "preferred_model_class": None,
    },
    "standards": {
        "max_total_tokens_per_call": 32_000,
        "preferred_model_class": "low_cost",
    },
    "spec": {
        "max_total_tokens_per_call": 48_000,
        "preferred_model_class": "low_cost",
    },
    "plan": {
        "max_total_tokens_per_call": 48_000,
        "preferred_model_class": "low_cost",
    },
    "review_plan": {
        "max_total_tokens_per_call": 32_000,
        "preferred_model_class": "low_cost",
    },
    "write_tests": {
        "max_total_tokens_per_call": 64_000,
        "preferred_model_class": "low_cost",
    },
    "implement": {
        "max_total_tokens_per_call": 96_000,
        "preferred_model_class": "low_cost",
    },
    "repair": {
        "max_total_tokens_per_call": 96_000,
        "preferred_model_class": "low_cost",
    },
    "review_code": {
        "max_total_tokens_per_call": 64_000,
        "preferred_model_class": "strong",
    },
    "security_check": {
        "max_total_tokens_per_call": 64_000,
        "preferred_model_class": "strong",
    },
    "consistency_check": {
        "max_total_tokens_per_call": 48_000,
        "preferred_model_class": "strong",
    },
}

# Scorecard thresholds (deterministic, high-signal).
_CACHE_RATIO_TARGET = 0.40
_FAILED_BURN_RATIO_WARN = 0.05
_AVG_TOKENS_WARN = 40_000
_COMPRESS_LONG_CHARS = 6_000  # ≈ 1.5k tokens
_COMPRESS_SYSTEM_DUP_MIN = 2


# ── Token estimate (tiktoken-free) ────────────────────────────────────────────


def estimate_tokens(text_value: Optional[str]) -> int:
    """Approx token count: max(1, ceil(len/4)) for non-empty; 0 for empty/None."""
    if text_value is None:
        return 0
    s = str(text_value)
    if not s:
        return 0
    return max(1, (len(s) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def estimate_messages(
    messages: Iterable[Mapping[str, Any]],
    *,
    max_context_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """Estimate tokens for a chat-style message list + compress suggestions.

    Heuristic only (chars/4). No model call. Compress suggestions are
    deterministic rules operators can apply before packing.
    """
    items: list[dict[str, Any]] = []
    total = 0
    role_totals: dict[str, int] = {}
    for idx, raw in enumerate(messages or []):
        if not isinstance(raw, Mapping):
            raise ValueError(f"messages[{idx}] must be an object with role/content")
        role = str(raw.get("role") or "user").strip() or "user"
        content = raw.get("content")
        if content is None:
            content = ""
        elif not isinstance(content, str):
            content = str(content)
        tokens = estimate_tokens(content)
        total += tokens
        role_totals[role] = role_totals.get(role, 0) + tokens
        items.append(
            {
                "index": idx,
                "role": role,
                "chars": len(content),
                "estimated_tokens": tokens,
            }
        )

    budget = max_context_tokens
    over_budget = False
    headroom = None
    if budget is not None:
        budget = int(budget)
        if budget < 1:
            raise ValueError("max_context_tokens must be >= 1 when provided")
        over_budget = total > budget
        headroom = budget - total

    suggestions = compress_suggestions(items, messages=list(messages or []), total_tokens=total)
    return {
        "heuristic": "chars/4",
        "chars_per_token": _CHARS_PER_TOKEN,
        "message_count": len(items),
        "total_estimated_tokens": total,
        "by_role": [
            {"role": r, "estimated_tokens": t}
            for r, t in sorted(role_totals.items(), key=lambda x: -x[1])
        ],
        "messages": items,
        "max_context_tokens": budget,
        "over_budget": over_budget,
        "headroom_tokens": headroom,
        "compress_suggestions": suggestions,
    }


def compress_suggestions(
    items: list[dict[str, Any]],
    *,
    messages: list[Mapping[str, Any]],
    total_tokens: int,
) -> list[dict[str, Any]]:
    """Deterministic packing tips — no ML, just structure heuristics."""
    tips: list[dict[str, Any]] = []
    system_idxs = [i for i, m in enumerate(items) if m["role"] == "system"]
    if len(system_idxs) >= _COMPRESS_SYSTEM_DUP_MIN:
        tips.append(
            {
                "kind": "merge_system",
                "severity": "medium",
                "finding": (
                    f"{len(system_idxs)} system messages — each re-pays the prefix. "
                    "Merge into one stable system prefix for cache hits."
                ),
                "action": "Concatenate system messages into a single first message; "
                          "keep variable instructions in the last user turn.",
                "message_indexes": system_idxs,
                "potential_token_saving": sum(
                    items[i]["estimated_tokens"] for i in system_idxs[1:]
                ),
            }
        )

    long_idxs = [
        i for i, m in enumerate(items) if m["chars"] >= _COMPRESS_LONG_CHARS
    ]
    for i in long_idxs:
        chars = items[i]["chars"]
        est = items[i]["estimated_tokens"]
        tips.append(
            {
                "kind": "truncate_long_message",
                "severity": "medium" if est >= 8_000 else "low",
                "finding": (
                    f"Message[{i}] ({items[i]['role']}) is ~{est:,} tokens "
                    f"({chars:,} chars) — large single blob."
                ),
                "action": "Summarize or chunk: keep authoritative facts, drop "
                          "repeated logs/diffs, or retrieve on demand.",
                "message_indexes": [i],
                "potential_token_saving": max(0, est - 2_000),
            }
        )

    # Stable prefix first → better prompt-cache leverage on providers that cache prefixes.
    if items and items[0]["role"] not in ("system", "developer"):
        tips.append(
            {
                "kind": "stable_prefix_first",
                "severity": "low",
                "finding": "First message is not system/developer — cacheable prefix is weak.",
                "action": "Put stable system + repo context first; variable user content last.",
                "message_indexes": [0],
                "potential_token_saving": 0,
            }
        )

    # Drop empty / near-empty messages
    empty = [i for i, m in enumerate(items) if m["estimated_tokens"] <= 1 and m["chars"] == 0]
    if empty:
        tips.append(
            {
                "kind": "drop_empty",
                "severity": "low",
                "finding": f"{len(empty)} empty message(s) add role framing overhead.",
                "action": "Remove empty turns before packing.",
                "message_indexes": empty,
                "potential_token_saving": len(empty),
            }
        )

    # High total — suggest knowledge pack_context budget
    if total_tokens >= 24_000:
        tips.append(
            {
                "kind": "pack_under_budget",
                "severity": "high" if total_tokens >= 64_000 else "medium",
                "finding": (
                    f"Conversation estimates {total_tokens:,} tokens — consider "
                    "authoritative pack_context with an explicit token_budget."
                ),
                "action": "Pack the context with a hard token budget (the Toolchain pack does "
                          "this); drop non-authoritative chunks first.",
                "message_indexes": [],
                "potential_token_saving": max(0, total_tokens - 16_000),
            }
        )

    tips.sort(
        key=lambda t: (
            {"high": 0, "medium": 1, "low": 2}.get(t["severity"], 9),
            -int(t.get("potential_token_saving") or 0),
        )
    )
    return tips


# ── Policies ─────────────────────────────────────────────────────────────────


def _default_policy_row(project_id: str, stage: str) -> dict[str, Any]:
    base = dict(_DEFAULT_STAGE_CAPS.get(PROJECT_DEFAULT_STAGE, {}))
    if stage != PROJECT_DEFAULT_STAGE:
        base.update(_DEFAULT_STAGE_CAPS.get(stage, {}))
    return {
        "id": None,
        "project_id": project_id,
        "stage": stage,
        "max_prompt_tokens": base.get("max_prompt_tokens"),
        "max_completion_tokens": base.get("max_completion_tokens"),
        "max_total_tokens_per_call": base.get("max_total_tokens_per_call"),
        "max_tokens_per_day": base.get("max_tokens_per_day"),
        "max_cost_usd_micros_per_day": base.get("max_cost_usd_micros_per_day"),
        "alert_at_pct": float(base.get("alert_at_pct") if base.get("alert_at_pct") is not None else 0.80),
        "preferred_model_class": base.get("preferred_model_class"),
        "enabled": True,
        "source": "default",
        "version": 0,
    }


def _row_from_db(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "project_id": r["project_id"],
        "stage": r["stage"] or PROJECT_DEFAULT_STAGE,
        "max_prompt_tokens": r.get("max_prompt_tokens"),
        "max_completion_tokens": r.get("max_completion_tokens"),
        "max_total_tokens_per_call": r.get("max_total_tokens_per_call"),
        "max_tokens_per_day": (
            int(r["max_tokens_per_day"]) if r.get("max_tokens_per_day") is not None else None
        ),
        "max_cost_usd_micros_per_day": (
            int(r["max_cost_usd_micros_per_day"])
            if r.get("max_cost_usd_micros_per_day") is not None
            else None
        ),
        "alert_at_pct": float(r["alert_at_pct"] if r.get("alert_at_pct") is not None else 0.80),
        "preferred_model_class": r.get("preferred_model_class"),
        "enabled": bool(r["enabled"]) if r.get("enabled") is not None else True,
        "source": "stored",
        "version": int(r.get("version") or 1),
        "updated_at": (
            r["updated_at"].isoformat()
            if r.get("updated_at") is not None and hasattr(r["updated_at"], "isoformat")
            else (str(r["updated_at"]) if r.get("updated_at") is not None else None)
        ),
        "created_at": (
            r["created_at"].isoformat()
            if r.get("created_at") is not None and hasattr(r["created_at"], "isoformat")
            else (str(r["created_at"]) if r.get("created_at") is not None else None)
        ),
    }


def list_policies(connection: Connection, project_id: str) -> dict[str, Any]:
    """Return effective policies: stored overrides merged over defaults."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    pid = project_id.strip()
    ready = ensure_tables(connection)
    if not ready:
        return unconfigured_policies(pid)
    try:
        with _savepoint(connection):
            rows = connection.execute(
                text(
                    """
                    SELECT id, project_id, stage, max_prompt_tokens, max_completion_tokens,
                           max_total_tokens_per_call, max_tokens_per_day,
                           max_cost_usd_micros_per_day, alert_at_pct, preferred_model_class,
                           enabled, version, updated_at, created_at
                    FROM token_budget_policies
                    WHERE project_id = :p
                    ORDER BY CASE WHEN stage = '*' THEN 0 ELSE 1 END, stage
                    """
                ),
                {"p": pid},
            ).mappings().all()
    except Exception as exc:
        if is_missing_relation(exc):
            return unconfigured_policies(pid)
        raise
    stored = {_row_from_db(r)["stage"]: _row_from_db(r) for r in rows}

    stages = [PROJECT_DEFAULT_STAGE] + [
        s for s in KNOWN_STAGES if s in _DEFAULT_STAGE_CAPS or s in stored
    ]
    # Include any stored custom stages not in known list
    for st in stored:
        if st not in stages:
            stages.append(st)

    policies: list[dict[str, Any]] = []
    for stage in stages:
        if stage in stored:
            policies.append(stored[stage])
        else:
            policies.append(_default_policy_row(pid, stage))

    return {
        "project_id": pid,
        "count": len(policies),
        "policies": policies,
        "default_stage_key": PROJECT_DEFAULT_STAGE,
        "schema_ready": True,
        "configured": any(p.get("source") == "stored" for p in policies),
    }


def upsert_policies(
    connection: Connection,
    *,
    project_id: str,
    policies: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Upsert one or more stage policies for a project. Returns effective set."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    pid = project_id.strip()
    items = list(policies or [])
    if not items:
        raise ValueError("policies must contain at least one entry.")
    if not ensure_tables(connection):
        raise ValueError(
            "token_budget_policies is not applied on this runtime; cannot store policies."
        )

    now = now_utc()
    for idx, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise ValueError(f"policies[{idx}] must be an object")
        stage = str(raw.get("stage") or PROJECT_DEFAULT_STAGE).strip() or PROJECT_DEFAULT_STAGE
        if len(stage) > 64:
            raise ValueError(f"policies[{idx}].stage too long")
        preferred = raw.get("preferred_model_class")
        if preferred is not None:
            preferred = str(preferred).strip().lower() or None
            if preferred not in (None, "low_cost", "strong"):
                raise ValueError(
                    f"policies[{idx}].preferred_model_class must be low_cost|strong|null"
                )
        alert = raw.get("alert_at_pct")
        if alert is None:
            alert = 0.80
        alert_f = float(alert)
        if not 0.0 < alert_f <= 1.0:
            raise ValueError(f"policies[{idx}].alert_at_pct must be in (0, 1]")

        def _opt_int(key: str) -> Optional[int]:
            v = raw.get(key)
            if v is None:
                return None
            iv = int(v)
            if iv < 0:
                raise ValueError(f"policies[{idx}].{key} must be >= 0")
            return iv

        enabled = bool(raw["enabled"]) if "enabled" in raw else True
        existing = connection.execute(
            text(
                "SELECT id, version FROM token_budget_policies "
                "WHERE project_id=:p AND stage=:s"
            ),
            {"p": pid, "s": stage},
        ).mappings().first()

        if existing:
            connection.execute(
                text(
                    """
                    UPDATE token_budget_policies SET
                      max_prompt_tokens = :mpt,
                      max_completion_tokens = :mct,
                      max_total_tokens_per_call = :mtt,
                      max_tokens_per_day = :mtd,
                      max_cost_usd_micros_per_day = :mcd,
                      alert_at_pct = :alert,
                      preferred_model_class = :pmc,
                      enabled = :en,
                      version = version + 1,
                      updated_at = :now
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing["id"],
                    "mpt": _opt_int("max_prompt_tokens"),
                    "mct": _opt_int("max_completion_tokens"),
                    "mtt": _opt_int("max_total_tokens_per_call"),
                    "mtd": _opt_int("max_tokens_per_day"),
                    "mcd": _opt_int("max_cost_usd_micros_per_day"),
                    "alert": alert_f,
                    "pmc": preferred,
                    "en": enabled,
                    "now": now,
                },
            )
        else:
            connection.execute(
                text(
                    """
                    INSERT INTO token_budget_policies (
                      id, project_id, stage, max_prompt_tokens, max_completion_tokens,
                      max_total_tokens_per_call, max_tokens_per_day,
                      max_cost_usd_micros_per_day, alert_at_pct, preferred_model_class,
                      enabled, version, updated_at, created_at)
                    VALUES (
                      :id, :p, :s, :mpt, :mct, :mtt, :mtd, :mcd, :alert, :pmc,
                      :en, 1, :now, :now)
                    """
                ),
                {
                    "id": make_id("tpol"),
                    "p": pid,
                    "s": stage,
                    "mpt": _opt_int("max_prompt_tokens"),
                    "mct": _opt_int("max_completion_tokens"),
                    "mtt": _opt_int("max_total_tokens_per_call"),
                    "mtd": _opt_int("max_tokens_per_day"),
                    "mcd": _opt_int("max_cost_usd_micros_per_day"),
                    "alert": alert_f,
                    "pmc": preferred,
                    "en": enabled,
                    "now": now,
                },
            )

    return list_policies(connection, pid)


# ── Scorecard + consumption (read-only llm_calls) ─────────────────────────────


def _window_clause(window_hours: Optional[int]) -> tuple[str, dict[str, Any]]:
    if window_hours is None:
        return "", {}
    hours = max(1, min(int(window_hours), 24 * 90))
    return " AND created_at >= now() - (:hours || ' hours')::interval", {"hours": str(hours)}


def empty_scorecard(
    project_id: str,
    window_hours: Optional[int] = None,
    *,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Honest zero scorecard. Never invents calls, tokens, or cost."""
    return {
        "project_id": project_id,
        "window_hours": int(window_hours) if window_hours is not None else None,
        "calls": 0,
        "ok_calls": 0,
        "failed_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_ratio": None,
        "failed_burn_usd_micros": 0,
        "failed_burn_ratio": 0.0,
        "avg_tokens_per_call": 0.0,
        "cost_usd_micros": 0,
        "ok_cost_usd_micros": 0,
        "cost_per_ok_call_usd_micros": None,
        "cost_per_call_usd_micros": None,
        "avg_latency_ms": 0.0,
        "grades": {
            "cache_ratio": "n/a",
            "failed_burn": "n/a",
            "avg_tokens": "n/a",
        },
        "targets": {
            "cache_ratio": _CACHE_RATIO_TARGET,
            "failed_burn_ratio": _FAILED_BURN_RATIO_WARN,
            "avg_tokens_per_call": _AVG_TOKENS_WARN,
        },
        "by_stage": [],
        "schema_ready": schema_ready,
    }


def build_scorecard(
    connection: Connection,
    project_id: str,
    *,
    window_hours: Optional[int] = None,
) -> dict[str, Any]:
    """Efficiency scorecard from llm_calls — cache ratio, failed burn, avgs."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    pid = project_id.strip()
    extra, params = _window_clause(window_hours)
    params = {"p": pid, **params}

    try:
        with _savepoint(connection):
            totals = connection.execute(
                text(
                    f"""
                    SELECT
                      count(*) AS calls,
                      COALESCE(SUM(CASE WHEN ok THEN 1 ELSE 0 END), 0) AS ok_calls,
                      COALESCE(SUM(CASE WHEN NOT ok THEN 1 ELSE 0 END), 0) AS failed_calls,
                      COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                      COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                      COALESCE(SUM(cost_usd_micros), 0) AS cost_usd_micros,
                      COALESCE(SUM(CASE WHEN NOT ok THEN cost_usd_micros ELSE 0 END), 0)
                        AS failed_cost_usd_micros,
                      COALESCE(SUM(CASE WHEN ok THEN cost_usd_micros ELSE 0 END), 0)
                        AS ok_cost_usd_micros,
                      COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                    FROM llm_calls
                    WHERE project_id = :p{extra}
                    """
                ),
                params,
            ).mappings().one()
    except Exception as exc:
        if is_missing_relation(exc):
            return empty_scorecard(pid, window_hours, schema_ready=False)
        raise

    calls = int(totals["calls"] or 0)
    ok_calls = int(totals["ok_calls"] or 0)
    failed_calls = int(totals["failed_calls"] or 0)
    prompt = int(totals["prompt_tokens"] or 0)
    cached = int(totals["cache_read_tokens"] or 0)
    completion = int(totals["completion_tokens"] or 0)
    total_tokens = int(totals["total_tokens"] or 0)
    cost = int(totals["cost_usd_micros"] or 0)
    failed_cost = int(totals["failed_cost_usd_micros"] or 0)
    ok_cost = int(totals["ok_cost_usd_micros"] or 0)

    input_tokens = prompt + cached
    cache_ratio = (cached / input_tokens) if input_tokens > 0 else None
    failed_burn_ratio = (failed_cost / cost) if cost > 0 else 0.0
    avg_tokens_per_call = (total_tokens / calls) if calls else 0.0
    cost_per_ok_call = (ok_cost / ok_calls) if ok_calls else None
    cost_per_call = (cost / calls) if calls else None

    # Grades: simple traffic-light from thresholds (no ML).
    grades: dict[str, str] = {}
    if cache_ratio is None:
        grades["cache_ratio"] = "n/a"
    elif cache_ratio >= _CACHE_RATIO_TARGET:
        grades["cache_ratio"] = "good"
    elif cache_ratio >= 0.15:
        grades["cache_ratio"] = "fair"
    else:
        grades["cache_ratio"] = "poor"

    if calls == 0:
        grades["failed_burn"] = "n/a"
    elif failed_burn_ratio <= 0.01:
        grades["failed_burn"] = "good"
    elif failed_burn_ratio < _FAILED_BURN_RATIO_WARN:
        grades["failed_burn"] = "fair"
    else:
        grades["failed_burn"] = "poor"

    if calls == 0:
        grades["avg_tokens"] = "n/a"
    elif avg_tokens_per_call <= 12_000:
        grades["avg_tokens"] = "good"
    elif avg_tokens_per_call <= _AVG_TOKENS_WARN:
        grades["avg_tokens"] = "fair"
    else:
        grades["avg_tokens"] = "poor"

    try:
        with _savepoint(connection):
            by_stage_rows = connection.execute(
                text(
                    f"""
                    SELECT COALESCE(stage, '?') AS stage,
                           count(*) AS calls,
                           COALESCE(SUM(CASE WHEN ok THEN 1 ELSE 0 END), 0) AS ok_calls,
                           COALESCE(SUM(total_tokens), 0) AS total_tokens,
                           COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                           COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                           COALESCE(SUM(cost_usd_micros), 0) AS cost_usd_micros,
                           COALESCE(SUM(CASE WHEN NOT ok THEN cost_usd_micros ELSE 0 END), 0)
                             AS failed_cost_usd_micros
                    FROM llm_calls
                    WHERE project_id = :p{extra}
                    GROUP BY 1
                    ORDER BY cost_usd_micros DESC
                    """
                ),
                params,
            ).mappings().all()
    except Exception as exc:
        if is_missing_relation(exc):
            return empty_scorecard(pid, window_hours, schema_ready=False)
        raise

    by_stage = []
    for r in by_stage_rows:
        st_calls = int(r["calls"] or 0)
        st_prompt = int(r["prompt_tokens"] or 0)
        st_cached = int(r["cache_read_tokens"] or 0)
        st_in = st_prompt + st_cached
        by_stage.append(
            {
                "stage": str(r["stage"]),
                "calls": st_calls,
                "ok_calls": int(r["ok_calls"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "avg_tokens_per_call": (
                    int(r["total_tokens"] or 0) / st_calls if st_calls else 0.0
                ),
                "cache_ratio": (st_cached / st_in) if st_in else None,
                "cost_usd_micros": int(r["cost_usd_micros"] or 0),
                "failed_cost_usd_micros": int(r["failed_cost_usd_micros"] or 0),
            }
        )

    return {
        "project_id": pid,
        "window_hours": int(window_hours) if window_hours is not None else None,
        "calls": calls,
        "ok_calls": ok_calls,
        "failed_calls": failed_calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total_tokens,
        "cache_read_tokens": cached,
        "cache_write_tokens": int(totals["cache_write_tokens"] or 0),
        "cache_ratio": cache_ratio,
        "failed_burn_usd_micros": failed_cost,
        "failed_burn_ratio": failed_burn_ratio,
        "avg_tokens_per_call": avg_tokens_per_call,
        "cost_usd_micros": cost,
        "ok_cost_usd_micros": ok_cost,
        "cost_per_ok_call_usd_micros": cost_per_ok_call,
        "cost_per_call_usd_micros": cost_per_call,
        "avg_latency_ms": float(totals["avg_latency_ms"] or 0),
        "grades": grades,
        "targets": {
            "cache_ratio": _CACHE_RATIO_TARGET,
            "failed_burn_ratio": _FAILED_BURN_RATIO_WARN,
            "avg_tokens_per_call": _AVG_TOKENS_WARN,
        },
        "by_stage": by_stage,
        "schema_ready": True,
    }


def build_consumption(
    connection: Connection,
    project_id: str,
    *,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Token/cost consumption over a rolling window, with policy pressure."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    hours = max(1, min(int(window_hours), 24 * 90))
    pid = project_id.strip()
    score = build_scorecard(connection, pid, window_hours=hours)
    policies = list_policies(connection, pid)
    project_pol = next(
        (p for p in policies["policies"] if p["stage"] == PROJECT_DEFAULT_STAGE),
        _default_policy_row(pid, PROJECT_DEFAULT_STAGE),
    )

    # Scale daily caps to the window for pressure estimates.
    day_frac = hours / 24.0
    max_tokens = project_pol.get("max_tokens_per_day")
    max_cost = project_pol.get("max_cost_usd_micros_per_day")
    token_cap = int(max_tokens * day_frac) if max_tokens else None
    cost_cap = int(max_cost * day_frac) if max_cost else None
    alert_pct = float(project_pol.get("alert_at_pct") or 0.80)

    token_util = None
    cost_util = None
    alerts: list[dict[str, Any]] = []
    have_usage = bool(score.get("schema_ready", True))
    if have_usage and token_cap and token_cap > 0:
        token_util = score["total_tokens"] / token_cap
        if token_util >= alert_pct:
            alerts.append(
                {
                    "kind": "token_budget_pressure",
                    "severity": "high" if token_util >= 1.0 else "medium",
                    "finding": (
                        f"Used {score['total_tokens']:,} tokens in {hours}h "
                        f"({token_util:.0%} of scaled daily cap {token_cap:,})."
                    ),
                    "action": "Lower per-stage max_total_tokens_per_call, enable caching, "
                              "or raise the daily token cap deliberately.",
                }
            )
    if have_usage and cost_cap and cost_cap > 0:
        cost_util = score["cost_usd_micros"] / cost_cap
        if cost_util >= alert_pct:
            alerts.append(
                {
                    "kind": "cost_budget_pressure",
                    "severity": "high" if cost_util >= 1.0 else "medium",
                    "finding": (
                        f"Spent ${score['cost_usd_micros']/1e6:.2f} in {hours}h "
                        f"({cost_util:.0%} of scaled daily cost cap)."
                    ),
                    "action": "Route planning stages to low_cost, cut failed-call burn, "
                              "or adjust max_cost_usd_micros_per_day.",
                }
            )

    # Hourly rollup for charts — empty when llm_calls is missing, never invented.
    series: list[Any] = []
    try:
        with _savepoint(connection):
            series = connection.execute(
                text(
                    """
                    SELECT date_trunc('hour', created_at) AS bucket,
                           count(*) AS calls,
                           COALESCE(SUM(total_tokens), 0) AS total_tokens,
                           COALESCE(SUM(cost_usd_micros), 0) AS cost_usd_micros
                    FROM llm_calls
                    WHERE project_id = :p
                      AND created_at >= now() - (:hours || ' hours')::interval
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {"p": pid, "hours": str(hours)},
            ).mappings().all()
    except Exception as exc:
        if not is_missing_relation(exc):
            raise

    return {
        "project_id": pid,
        "window_hours": hours,
        "calls": score["calls"],
        "total_tokens": score["total_tokens"],
        "prompt_tokens": score["prompt_tokens"],
        "completion_tokens": score["completion_tokens"],
        "cache_read_tokens": score["cache_read_tokens"],
        "cost_usd_micros": score["cost_usd_micros"],
        "failed_burn_usd_micros": score["failed_burn_usd_micros"],
        "cache_ratio": score["cache_ratio"],
        "policy": {
            "stage": PROJECT_DEFAULT_STAGE,
            "max_tokens_per_day": max_tokens,
            "max_cost_usd_micros_per_day": max_cost,
            "alert_at_pct": alert_pct,
            "scaled_token_cap": token_cap,
            "scaled_cost_cap_usd_micros": cost_cap,
            "token_utilization": token_util,
            "cost_utilization": cost_util,
            "enabled": project_pol.get("enabled", True),
        },
        "alerts": alerts,
        "by_stage": score["by_stage"],
        "series": [
            {
                "bucket": (
                    r["bucket"].isoformat()
                    if r["bucket"] is not None and hasattr(r["bucket"], "isoformat")
                    else str(r["bucket"])
                ),
                "calls": int(r["calls"] or 0),
                "total_tokens": int(r["total_tokens"] or 0),
                "cost_usd_micros": int(r["cost_usd_micros"] or 0),
            }
            for r in series
        ],
        "schema_ready": bool(score.get("schema_ready", True))
        and bool(policies.get("schema_ready", True)),
        "configured": bool(policies.get("configured")),
    }


# ── Recommendations (extends cost_insights) ───────────────────────────────────


def build_recommendations(
    connection: Connection,
    project_id: str,
    *,
    profile_name: Optional[str] = None,
) -> dict[str, Any]:
    """Deterministic recommendations: cost_insights + token-efficiency rules."""
    if not (project_id or "").strip():
        raise ValueError("project_id is required.")
    pid = project_id.strip()

    base = build_insights(connection, pid)
    recs: list[dict[str, Any]] = []
    for ins in base.get("insights") or []:
        recs.append(
            {
                "kind": ins["kind"],
                "severity": ins["severity"],
                "finding": ins["finding"],
                "action": ins["action"],
                "potential_saving_usd_micros": int(ins.get("potential_saving_usd_micros") or 0),
                "potential_token_saving": 0,
                "source": "cost_insights",
            }
        )

    score = build_scorecard(connection, pid)
    spend = int(score["cost_usd_micros"] or 0)

    # Token-specific rules
    if score["calls"] > 0 and score["cache_ratio"] is not None:
        if score["cache_ratio"] < _CACHE_RATIO_TARGET:
            fresh = score["prompt_tokens"]
            recs.append(
                {
                    "kind": "raise_cache_ratio",
                    "severity": "high" if score["cache_ratio"] < 0.15 else "medium",
                    "finding": (
                        f"Cache ratio {score['cache_ratio']:.0%} below target "
                        f"{_CACHE_RATIO_TARGET:.0%} — {fresh:,} fresh prompt tokens re-paid."
                    ),
                    "action": "Stabilize system+repo prefixes; enable provider prompt caching; "
                              "put variable content last.",
                    "potential_saving_usd_micros": 0,
                    "potential_token_saving": int(fresh * 0.5),
                    "source": "token_efficiency",
                }
            )

    if score["avg_tokens_per_call"] >= _AVG_TOKENS_WARN and score["calls"] >= 3:
        recs.append(
            {
                "kind": "high_avg_tokens",
                "severity": "medium",
                "finding": (
                    f"Average {score['avg_tokens_per_call']:.0f} tokens/call "
                    f"(target < {_AVG_TOKENS_WARN:,}) across {score['calls']} calls."
                ),
                "action": "Estimate a large pack before sending it; trim long messages; give "
                          "packed context a token budget.",
                "potential_saving_usd_micros": 0,
                "potential_token_saving": int(
                    (score["avg_tokens_per_call"] - 12_000) * score["calls"]
                ),
                "source": "token_efficiency",
            }
        )

    # Per-stage over-cap vs policy
    policies = {p["stage"]: p for p in list_policies(connection, pid)["policies"]}
    for st in score["by_stage"]:
        pol = policies.get(st["stage"]) or policies.get(PROJECT_DEFAULT_STAGE)
        if not pol or not pol.get("enabled", True):
            continue
        cap = pol.get("max_total_tokens_per_call")
        if not cap or st["calls"] == 0:
            continue
        if st["avg_tokens_per_call"] > float(cap):
            recs.append(
                {
                    "kind": "stage_over_token_cap",
                    "severity": "high",
                    "finding": (
                        f"Stage `{st['stage']}` averages "
                        f"{st['avg_tokens_per_call']:.0f} tokens/call vs policy cap {cap:,}."
                    ),
                    "action": f"Compress context for `{st['stage']}`, or raise that stage's "
                              "per-call cap under Token budget policies (Save cap) in Govern.",
                    "potential_saving_usd_micros": 0,
                    "potential_token_saving": int(
                        (st["avg_tokens_per_call"] - cap) * st["calls"]
                    ),
                    "source": "token_efficiency",
                    "stage": st["stage"],
                }
            )

    # Model routing suggestion mismatch vs profile preferred class
    profile = resolve_profile(profile_name)
    routing = suggest_model_routing(profile_name=profile["name"])
    # If material spend on stages that profiles mark low_cost, nudge
    cheap_spend = sum(
        int(s["cost_usd_micros"])
        for s in score["by_stage"]
        if s["stage"] in _CHEAP_STAGES
    )
    if spend and cheap_spend * 10 >= spend:  # ≥10%
        recs.append(
            {
                "kind": "route_planning_low_cost",
                "severity": "medium",
                "finding": (
                    f"${cheap_spend/1e6:.2f} on planning-class stages "
                    f"({', '.join(sorted(_CHEAP_STAGES))}) under profile `{profile['name']}`."
                ),
                "action": "Confirm pipeline profile routes those stages to low_cost "
                          "(lean/balanced/vag already do).",
                "potential_saving_usd_micros": int(cheap_spend * 0.7),
                "potential_token_saving": 0,
                "source": "token_efficiency",
            }
        )

    # Failed burn already in cost_insights when material; still surface ratio grade
    if score["failed_burn_ratio"] >= _FAILED_BURN_RATIO_WARN and score["failed_burn_usd_micros"] > 0:
        if not any(r["kind"] == "failed_call_burn" for r in recs):
            recs.append(
                {
                    "kind": "failed_call_burn",
                    "severity": "high" if score["failed_burn_ratio"] >= 0.15 else "medium",
                    "finding": (
                        f"{score['failed_burn_ratio']:.0%} of spend "
                        f"(${score['failed_burn_usd_micros']/1e6:.2f}) on failed calls."
                    ),
                    "action": "Fix failing stages before scaling concurrency — "
                              "failed calls pay full price.",
                    "potential_saving_usd_micros": int(score["failed_burn_usd_micros"]),
                    "potential_token_saving": 0,
                    "source": "token_efficiency",
                }
            )

    recs.sort(
        key=lambda r: (
            {"high": 0, "medium": 1, "low": 2}.get(r["severity"], 9),
            -int(r.get("potential_saving_usd_micros") or 0),
            -int(r.get("potential_token_saving") or 0),
        )
    )

    return {
        "project_id": pid,
        "total_spend_usd_micros": spend,
        "scorecard_grades": score["grades"],
        "profile": profile["name"],
        "model_routing": routing["stages"],
        "recommendations": recs,
        "count": len(recs),
    }


def suggest_model_routing(
    *,
    profile_name: Optional[str] = None,
) -> dict[str, Any]:
    """Map pipeline stages → preferred model class (low_cost|strong) from profiles.

    Pure function over PROFILES — no DB, no model calls.
    """
    profile = resolve_profile(profile_name)
    stages: list[dict[str, Any]] = []
    # Cover known stages + any explicit overrides in the profile model map
    routing_map = dict(profile.get("model") or {})
    stage_names = list(KNOWN_STAGES)
    for key in routing_map:
        if key not in ("default",) and key not in stage_names:
            stage_names.append(key)

    for stage in stage_names:
        cls = model_for_step(profile, stage)
        stages.append(
            {
                "stage": stage,
                "preferred_class": cls,
                "rationale": (
                    "explicit profile override"
                    if stage in routing_map
                    else f"profile default ({routing_map.get('default', 'strong')})"
                ),
            }
        )

    return {
        "profile": profile["name"],
        "label": profile.get("label"),
        "available_profiles": sorted(PROFILES.keys()),
        "default_profile": DEFAULT_PROFILE,
        "stages": stages,
    }
