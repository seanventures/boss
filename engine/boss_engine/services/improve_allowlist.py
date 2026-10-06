"""What "Improve BOSS" and the daily heartbeat may say, as a typed allow-list.

docs/product/RUN_SCORECARDS.md, "Implementation". ONE schema, read by both sides:

  * the client passes every payload through `sanitize()` before it is stored, previewed or sent;
  * the collector validates what arrives with the same `sanitize()` (so a modified client cannot
    store a field this file does not name).

An ALLOW-list, never a deny-list: a key that is not here is dropped, a string that is not one of
the listed values becomes ``custom``/``other`` (or is dropped), numbers are clamped and rounded.
After the schema, every surviving string passes the existing secret and PII gate
(`redaction.redact`): a string the gate would change is dropped, not masked.

Free text exists in exactly one place: the monthly check-in's one-liner, which a person typed and
pressed send on (`feedback.lines`, ≤ 140 characters, still through the gate).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------------------------
# Value sets
# --------------------------------------------------------------------------------------------

#: Count ranges. Counts of things an operator owns (projects, people, agents) are never exact.
COUNT_RANGES = ("0", "1", "2-5", "6-20", "21-100", "101-500", "500+")
AGE_BUCKETS = ("0-1", "2-7", "8-30", "31-90", "91-180", "181-365", "365+")
EDIT_BUCKETS = ("0-10%", "10-30%", "30-60%", "60-100%")
EDITIONS = ("community", "enterprise", "cloud", "other")
INSTALL_METHODS = ("compose", "source", "e2e", "docker", "other")
OSES = ("linux", "darwin", "windows", "other")
ARCHES = ("amd64", "arm64", "other")
UNIT_KINDS = ("workflow_run", "build", "step", "panel", "research", "quick_change")
TERMINAL_STATES = ("completed", "failed", "cancelled", "rejected", "rolled_back")
HARNESSES = ("claude_code", "codex", "native", "openai", "azure", "openrouter", "groq",
             "deepseek", "ollama", "vllm", "grok", "cursor", "gemini", "echo", "other")
PROVIDERS = ("anthropic", "openai", "azure", "google", "openrouter", "groq", "deepseek",
             "local", "xai", "other")
TASK_KINDS = ("new_app", "add_feature", "fix", "research", "panel", "other")
TASK_SIZES = ("small", "medium", "large")
RESEARCH_VERDICTS = ("pursue", "promising", "needs_more_research", "not_promising", "kill",
                     "not_worth_it", "other")
SUBJECTS = ("build", "research", "panel", "answer", "deploy")
RATINGS: dict[str, tuple[str, ...]] = {
    "build": ("yes", "with_changes", "no"),
    "research": ("useful", "not_useful"),
    "panel": ("right", "wrong"),
    "answer": ("up", "down"),
    "deploy": ("working", "not_working"),
}
BUILD_CHIPS = ("wrong_scope", "poor_quality", "broke_something", "too_slow", "too_expensive",
               "misread_brief")
CHIPS: dict[str, tuple[str, ...]] = {
    "build": BUILD_CHIPS,
    "research": ("shallow", "wrong_market", "great_sources", "changed_my_mind"),
    "panel": ("missed_requirements", "over_scoped", "good_debate"),
    "answer": (),
    "deploy": (),
    "reject": BUILD_CHIPS,
    "steer": BUILD_CHIPS,
}
DECISION_KINDS = ("plan", "spec", "merge", "deploy", "other")
EDIT_DOCS = ("brief", "prd", "plan")
PMF_ANSWERS = ("very", "somewhat", "not")
FUNNEL = ("setup", "first_build", "first_pr", "first_merge", "first_deploy")
OVERRIDES = ("start_anyway", "waived_gates")
PROVIDER_KINDS = ("subscription", "api_key", "self_hosted")
DEPLOY_TARGET_KINDS = ("ssh_host", "docker", "kubernetes", "cloud", "static", "other")
#: Error classes BOSS itself writes (failure_class / reason_code words). Others → `other`.
ERROR_CLASSES = (
    "no_repo_access", "build_failed", "tests_failed", "health_failed", "gate_failed",
    "review_refuted", "budget_exceeded", "budget_cap", "timeout", "model_unavailable",
    "model_error", "rate_limited", "sandbox_failed", "deploy_failed", "merge_conflict",
    "cancelled", "interrupted", "infra", "auth_failed", "no_model", "spec_invalid",
    "plan_rejected", "stalled", "duplicate", "other",
)
SCALE_FIELDS = ("projects", "active_projects", "people", "agents_builtin", "agents_custom",
                "workflows_run", "builds", "prs_merged", "deploys")
AGG_REASONS = ("rows_over_threshold", "size_cap")
MEMORY_RANGES = ("<4", "4-8", "8-16", "16-32", "32-64", "64+")
CPU_RANGES = ("1-2", "3-4", "5-8", "9-16", "17+")
_REF = re.compile(r"^[0-9a-f]{12}$")

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTALL_ID = re.compile(r"^bi_[a-z2-7]{26}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}(?:-(?:rc|beta|alpha|dev)\.?\d{0,3})?$")
_SCORE_VERSION = re.compile(r"^\d{1,2}(?:\.\d{1,4})?$")

# --------------------------------------------------------------------------------------------
# Built-in catalogue keys (named); everything else is `custom`
# --------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def builtin_keys() -> dict[str, frozenset[str]]:
    """Keys BOSS ships, read once from the repo: the public catalogue is the only thing named."""
    roles: set[str] = set()
    skills: set[str] = set()
    workflows: set[str] = set()
    steps: set[str] = set()
    try:
        from ..config import get_settings

        root = Path(get_settings().specpack_root)
    except Exception:  # noqa: BLE001
        root = Path(__file__).resolve().parents[3] / "packages" / "specpack"
    for folder, into in (("agents", roles), ("roles", roles), ("skills", skills),
                         ("workflows", workflows)):
        try:
            into.update(p.stem for p in (root / folder).glob("*.yaml"))
        except OSError:
            pass
    try:
        from .workflow_catalogue import CATALOGUE

        for entry in CATALOGUE:
            workflows.add(str(entry.get("key")))
            for step in entry.get("steps") or []:
                if step.get("id"):
                    steps.add(str(step["id"]))
    except Exception:  # noqa: BLE001
        pass
    return {"roles": frozenset(roles), "skills": frozenset(skills),
            "workflows": frozenset(workflows), "steps": frozenset(steps)}


@lru_cache(maxsize=1)
def builtin_role_names() -> dict[str, str]:
    """{role key: display name} for the roles BOSS ships, for screens (never sent)."""
    import yaml

    try:
        from ..config import get_settings

        root = Path(get_settings().specpack_root)
    except Exception:  # noqa: BLE001
        root = Path(__file__).resolve().parents[3] / "packages" / "specpack"
    out: dict[str, str] = {}
    for folder in ("agents", "roles"):
        for p in (root / folder).glob("*.yaml"):
            try:
                got = yaml.safe_load(p.read_text()) or {}
            except Exception:  # noqa: BLE001
                continue
            name = got.get("display_name") or got.get("name") or got.get("title")
            if isinstance(name, str) and name.strip():
                out.setdefault(p.stem, name.strip())
    return out


def catalogue_name(kind: str, value: Any) -> Optional[str]:
    """A built-in key as itself, anything else as `custom`; None stays None."""
    if value is None or value == "":
        return None
    text = str(value)
    return text if text in builtin_keys().get(kind, frozenset()) else "custom"


#: THE PUBLIC MODEL IDS that may be named (P1, 2026-10-05). Exact ids only — a prefix test let
#: `claude-nightingale-ft`, `gpt-4o-acmecorp` and `qwen-acme-payroll-7b` through, and a fine-tune or
#: a local model is routinely named after the company or the person who made it. A name is
#: normalised (lower case; a provider prefix, a date stamp, `-latest` and a `[1m]` context tag
#: removed) and then must be IN this set; everything else is `other`. Add a model here when it is
#: publicly released, never by pattern.
PUBLIC_MODELS: frozenset[str] = frozenset({
    # Anthropic
    "claude-3-opus", "claude-3-sonnet", "claude-3-haiku", "claude-3-5-sonnet", "claude-3-5-haiku",
    "claude-3-7-sonnet", "claude-sonnet-4", "claude-opus-4", "claude-opus-4-1", "claude-sonnet-4-5",
    "claude-haiku-4-5", "claude-opus-4-5", "claude-sonnet-5", "claude-opus-5", "claude-haiku-5",
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5", "opus", "sonnet", "haiku", "fable",
    # OpenAI
    "gpt-4", "gpt-4-turbo", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    "gpt-4.5", "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-codex", "gpt-5-pro", "gpt-5.1",
    "gpt-5.1-codex", "gpt-5.1-codex-mini", "gpt-5.1-codex-max", "gpt-5.2", "gpt-5.2-codex",
    "gpt-5.2-pro", "gpt-6", "gpt-6-codex", "gpt-6-mini", "o1", "o1-mini", "o1-pro", "o3", "o3-mini",
    "o3-pro", "o4-mini", "codex-mini", "gpt-oss-20b", "gpt-oss-120b",
    # Google
    "gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite",
    "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3-pro", "gemini-3-flash",
    # xAI, DeepSeek, Mistral
    "grok-3", "grok-3-mini", "grok-4", "grok-4-fast", "grok-code-fast-1",
    "deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-r1", "deepseek-coder",
    "mistral-large", "mistral-medium", "mistral-small", "codestral", "devstral", "ministral-8b",
    # Open weights as their public registries tag them (family:size)
    "llama3:8b", "llama3:70b", "llama3.1:8b", "llama3.1:70b", "llama3.1:405b", "llama3.2:1b",
    "llama3.2:3b", "llama3.3:70b", "llama4:scout", "llama4:maverick",
    "qwen2.5:7b", "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:72b", "qwen2.5-coder:7b",
    "qwen2.5-coder:14b", "qwen2.5-coder:32b", "qwen3:8b", "qwen3:14b", "qwen3:32b", "qwen3:235b",
    "qwen3-coder:30b", "qwen3-coder:480b", "gemma2:9b", "gemma2:27b", "gemma3:4b", "gemma3:12b",
    "gemma3:27b", "phi3:mini", "phi4", "phi4:14b", "mistral:7b", "mixtral:8x7b",
    "deepseek-r1:7b", "deepseek-r1:14b", "deepseek-r1:32b", "deepseek-r1:70b",
    "deepseek-coder-v2:16b", "codellama:7b", "codellama:13b", "codellama:34b",
})
_PROVIDER_PREFIX = re.compile(r"^(?:[a-z0-9-]+\.)?(?:anthropic|openai|google|xai|meta|mistralai|"
                              r"deepseek|qwen|ollama|openrouter)[/.]")
_DATE_SUFFIX = re.compile(r"(?:-20\d{6}|-20\d{2}-\d{2}-\d{2}|-latest|-preview|-v\d+:\d+)$")


def public_model(value: Any) -> Optional[str]:
    """A model as it may travel: its public id, or `other`. None stays None."""
    if not value:
        return None
    text = str(value).strip().lower()
    text = text.replace("[1m]", "").strip()
    for _ in range(3):
        text = _PROVIDER_PREFIX.sub("", text)
        text = _DATE_SUFFIX.sub("", text)
    if text.endswith(":latest"):
        text = text[: -len(":latest")]
    return text if text in PUBLIC_MODELS else "other"


#: Kept for callers that still say family; it is the exact check now.
model_family = public_model


def count_range(n: Any) -> str:
    try:
        v = int(n or 0)
    except (TypeError, ValueError):
        v = 0
    if v <= 0:
        return "0"
    if v == 1:
        return "1"
    for top, label in ((5, "2-5"), (20, "6-20"), (100, "21-100"), (500, "101-500")):
        if v <= top:
            return label
    return "500+"


def age_bucket(days: Any) -> str:
    try:
        d = int(days or 0)
    except (TypeError, ValueError):
        d = 0
    for top, label in ((1, "0-1"), (7, "2-7"), (30, "8-30"), (90, "31-90"), (180, "91-180"),
                       (365, "181-365")):
        if d <= top:
            return label
    return "365+"


def edit_bucket(ratio: Any) -> str:
    r = float(ratio or 0)
    if r < 0.1:
        return "0-10%"
    if r < 0.3:
        return "10-30%"
    if r < 0.6:
        return "30-60%"
    return "60-100%"


# --------------------------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------------------------
# A spec is one of:
#   ("enum", values)       a string from `values`; anything else → the fallback or dropped
#   ("int", lo, hi)        an int clamped to [lo, hi]
#   ("num", lo, hi, nd)    a number clamped and rounded to nd decimals
#   ("bool",)
#   ("re", pattern)        a string matching `pattern`, else dropped
#   ("text", max_len)      free text: only feedback.lines
#   ("list", spec, max)    a list of `spec`, at most `max` items
#   ("map", key_spec, value_spec, max)   a dict whose keys pass key_spec
#   dict                   a fixed object: only these keys

Spec = Any


def _enum(values: Iterable[str], fallback: Optional[str] = None) -> Spec:
    return ("enum", tuple(values), fallback)


_COUNT = ("int", 0, 10_000_000)
_RANGE = _enum(COUNT_RANGES)
_COUNTS = lambda keys, cap=64: ("map", _enum(keys), _COUNT, cap)  # noqa: E731

HEARTBEAT: dict[str, Spec] = {
    "install_id": ("re", _INSTALL_ID),
    "version": ("re", _VERSION),
    "edition": _enum(EDITIONS, "other"),
    "install_method": _enum(INSTALL_METHODS, "other"),
    "os": _enum(OSES, "other"),
    "arch": _enum(ARCHES, "other"),
    "age": _enum(AGE_BUCKETS),
    "worked_24h": ("bool",),
}

#: The key set of a scorecard row as sent (the batch's per-run detail).
SCORECARD_ROW: dict[str, Spec] = {
    "ref": ("re", _REF),   # a per-install salted hash of the unit id: lets a later row replace it
    "unit_kind": _enum(UNIT_KINDS),
    "workflow_key": ("catalogue", "workflows"),
    "workflow_version": ("int", 0, 10_000),
    "step_key": ("catalogue", "steps"),
    "role_key": ("catalogue", "roles"),
    "harness": _enum(HARNESSES, "other"),
    "model": ("model",),
    "provider": _enum(PROVIDERS, "other"),
    "review_lanes": ("int", 0, 64),
    "review_rounds": ("int", 0, 16),
    "retry_max": ("int", 0, 32),
    "skills": ("list", ("catalogue_only", "skills"), 32),
    "skills_custom": ("int", 0, 1000),
    "mcp_tools": ("int", 0, 1000),
    "experiment_arm": _enum(("a", "b")),
    "task_kind": _enum(TASK_KINDS, "other"),
    "task_size": _enum(TASK_SIZES),
    "terminal_state": _enum(TERMINAL_STATES),
    "tests_passed": ("int", 0, 100_000),
    "tests_total": ("int", 0, 100_000),
    "existing_tests_intact": ("bool",),
    "gates_passed": ("int", 0, 1000),
    "gates_failed": ("int", 0, 1000),
    "review_approved": ("bool",),
    "review_rounds_used": ("int", 0, 64),
    "approvals": ("int", 0, 1000),
    "rejections": ("int", 0, 1000),
    "decide_wait_s": ("int", 0, 10_000_000),
    "steers": ("int", 0, 1000),
    "pr_opened": ("bool",),
    "pr_merged": ("bool",),
    "pr_reverted": ("bool",),
    "deploy_healthy": ("bool",),
    "deploy_healthy_24h": ("bool",),
    "rolled_back": ("bool",),
    "research_verdict": _enum(RESEARCH_VERDICTS, "other"),
    "confidence": ("num", 0, 1, 2),
    "panel_agreed": ("bool",),
    "prd_approved": ("bool",),
    "cost_usd": ("num", 0, 100_000, 2),
    "tokens_in": ("int", 0, 10_000_000_000),
    "tokens_out": ("int", 0, 10_000_000_000),
    "model_calls": ("int", 0, 1_000_000),
    "tool_calls": ("int", 0, 1_000_000),
    "turns": ("int", 0, 1_000_000),
    "wall_time_s": ("int", 0, 100_000_000),
    "wait_on_person_s": ("int", 0, 100_000_000),
    "retries": ("int", 0, 1000),
    "shepherd_interventions": ("int", 0, 1000),
    "error_class": _enum(ERROR_CLASSES, "other"),
    "rating": _enum(tuple({r for rs in RATINGS.values() for r in rs})),
    "score": ("num", 0, 1, 3),
    "score_version": ("re", _SCORE_VERSION),
}

#: One aggregate group: the same dimensions, counted.
#: The most aggregate groups one batch carries. The client coarsens to fit; it never truncates.
MAX_AGG_GROUPS = 400

SCORECARD_AGG: dict[str, Spec] = {
    "unit_kind": _enum(UNIT_KINDS),
    "harness": _enum(HARNESSES, "other"),
    "model": ("model",),
    "role_key": ("catalogue", "roles"),
    "workflow_key": ("catalogue", "workflows"),
    "terminal_state": _enum(TERMINAL_STATES),
    "experiment_arm": _enum(("a", "b")),
    "n": _COUNT,
    "merged": _COUNT,
    "gates_failed_units": _COUNT,
    "steers": _COUNT,
    "turns": _COUNT,
    "cost_usd": ("num", 0, 10_000_000, 2),
    "score_mean": ("num", 0, 1, 3),
    "score_hist": ("list", ("int", 0, 10_000_000), 10),
    "score_version": ("re", _SCORE_VERSION),
}

BATCH: dict[str, Spec] = {
    "day": ("re", _DAY),
    "since": ("re", _DAY),
    "until": ("re", _DAY),
    "context": {"version": ("re", _VERSION), "edition": _enum(EDITIONS, "other")},
    "scale": {k: _RANGE for k in SCALE_FIELDS},
    "feedback": {
        "ratings": ("map", _enum(SUBJECTS), ("map", _enum(
            tuple({r for rs in RATINGS.values() for r in rs})), _COUNT, 8), 8),
        "chips": ("map", _enum(SUBJECTS), ("map", _enum(
            tuple({c for cs in CHIPS.values() for c in cs})), _COUNT, 16), 8),
        "rejects": ("map", _enum(DECISION_KINDS), _COUNTS(BUILD_CHIPS + ("none",)), 8),
        "steers": _COUNTS(BUILD_CHIPS + ("none",)),
        "edits": ("map", _enum(EDIT_DOCS), _COUNTS(EDIT_BUCKETS), 4),
        "checkin": _COUNTS(PMF_ANSWERS),
        "lines": ("list", ("text", 140), 10),
    },
    "usage": {
        "funnel": ("map", _enum(FUNNEL), _enum(AGE_BUCKETS), 8),
        "workflows": ("map", ("catalogue", "workflows"), _COUNT, 64),
        "doors": ("map", _enum(("new_app", "add_feature", "template", "build_box", "talk",
                                "research", "panel")), _COUNT, 8),
        "overrides": _COUNTS(OVERRIDES),
        "abandoned": _COUNT,
        "custom": {"roles": _RANGE, "skills": _RANGE, "mcp_servers": _RANGE,
                   "workflows": _RANGE},
    },
    "setup": {
        "memory_gb": _enum(MEMORY_RANGES),
        "cpus": _enum(CPU_RANGES),
        "providers": _COUNTS(PROVIDER_KINDS),
        "deploy_targets": _COUNTS(DEPLOY_TARGET_KINDS),
    },
    "health": {
        "errors": ("map", _enum(ERROR_CLASSES, "other"), _COUNT, 32),
        "retries": _COUNT,
        "stuck": _COUNT,
        "budget_cap_hits": _COUNT,
        "doctor": ("map", ("re", re.compile(r"^[a-z][a-z0-9_]{0,31}$")), _COUNT, 32),
    },
    "scorecards": {
        "rows": ("list", SCORECARD_ROW, 200),
        "agg": ("list", SCORECARD_AGG, MAX_AGG_GROUPS),
    },
    "aggregated": ("map", _enum(("scorecards",)), {
        "from_rows": _COUNT, "covered": _COUNT, "groups": _COUNT, "reason": _enum(AGG_REASONS),
        "level": ("int", 1, 4)}, 2),
}

ENVELOPE: dict[str, Spec] = {
    "v": ("int", 1, 1),
    "day": ("re", _DAY),
    "install_id": ("re", _INSTALL_ID),
    "secret_hash": ("re", _HEX64),
    "heartbeat": HEARTBEAT,
    "batches": ("list", BATCH, 7),
    "dropped_batches": ("int", 0, 10_000),
}

# Share this run: content by design, still bounded and through the redaction gate.
SHARE: dict[str, Spec] = {
    "v": ("int", 1, 1),
    "install_id": ("re", _INSTALL_ID),
    "secret_hash": ("re", _HEX64),
    "note": ("text", 2000),
    "bundle": ("text", 400_000),
    "context": {"version": ("re", _VERSION), "edition": _enum(EDITIONS, "other")},
}

# --------------------------------------------------------------------------------------------
# sanitize
# --------------------------------------------------------------------------------------------


def _gate(text: str) -> Optional[str]:
    """The existing secret + PII gate: a string it would change does not travel."""
    from .redaction import redact

    cleaned, _ = redact(text)
    return text if cleaned == text else None


def _scrub_text(text: str, limit: int) -> Optional[str]:
    """Free text (the one-liner, a shared bundle): masked rather than dropped, then bounded."""
    from .redaction import redact

    cleaned, _ = redact(str(text))
    cleaned = cleaned.strip()
    return cleaned[:limit] if cleaned else None


def _value(spec: Spec, value: Any, drop: Callable[[], None]) -> Any:
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return None
        return _object(spec, value, drop)
    kind = spec[0]
    if kind == "enum":
        values, fallback = spec[1], spec[2]
        if isinstance(value, str) and value in values:
            return value
        return fallback
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(min(max(int(value), spec[1]), spec[2]))
    if kind == "num":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(float(min(max(float(value), spec[1]), spec[2])), spec[3])
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind == "re":
        return value if isinstance(value, str) and spec[1].fullmatch(value) else None
    if kind == "text":
        return _scrub_text(value, spec[1]) if isinstance(value, str) else None
    if kind == "catalogue":
        return catalogue_name(spec[1], value) if isinstance(value, str) else None
    if kind == "catalogue_only":
        return value if isinstance(value, str) and value in builtin_keys()[spec[1]] else None
    if kind == "model":
        return public_model(value) if isinstance(value, str) else None
    if kind == "list":
        if not isinstance(value, list):
            return None
        out = []
        for item in value[: spec[2]]:
            got = _value(spec[1], item, drop)
            if got is not None:
                out.append(got)
        if len(value) > spec[2]:
            drop()
        return out
    if kind == "map":
        if not isinstance(value, dict):
            return None
        out: dict[str, Any] = {}
        for key, item in list(value.items())[: spec[3]]:
            k = _value(spec[1], key, drop)
            if k is None:
                drop()
                continue
            v = _value(spec[2], item, drop)
            if v is None:
                continue
            if k in out and isinstance(v, (int, float)) and isinstance(out[k], (int, float)):
                out[k] = out[k] + v  # two custom names fold into one `custom` count
            else:
                out[k] = v
        return out
    raise ValueError(f"unknown spec {kind!r}")


def _object(schema: dict[str, Spec], value: dict, drop: Callable[[], None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        spec = schema.get(key)
        if spec is None:
            drop()
            continue
        got = _value(spec, item, drop)
        if got is None:
            continue
        out[key] = got
    return out


def _strict_id(value: str) -> bool:
    return any(p.fullmatch(value) for p in (_DAY, _INSTALL_ID, _HEX64, _VERSION, _REF,
                                            _SCORE_VERSION))


def _gate_strings(value: Any, drop: Callable[[], None], free_text: bool = False) -> Any:
    """Every string left after the schema passes the redaction gate (free text was masked)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _gate(str(k)) is None:
                drop()
                continue
            got = _gate_strings(v, drop, free_text=k in ("lines", "bundle", "note"))
            if got is not None:
                out[k] = got
        return out
    if isinstance(value, list):
        return [g for g in (_gate_strings(v, drop, free_text) for v in value) if g is not None]
    # Identifiers that already passed a strict pattern (a day, the install id, a hash, a version)
    # are exempt: the PII gate reads their digit runs as phone numbers and would drop a valid id.
    if isinstance(value, str) and not free_text and not _strict_id(value):
        if _gate(value) is None:
            drop()
            return None
    return value


def sanitize(payload: Any, schema: dict[str, Spec] = ENVELOPE) -> tuple[dict[str, Any], int]:
    """(what may travel, how many fields were dropped). Never raises on odd input."""
    dropped = [0]

    def drop() -> None:
        dropped[0] += 1

    if not isinstance(payload, dict):
        return {}, 1
    clean = _object(schema, payload, drop)
    clean = _gate_strings(clean, drop)
    return clean, dropped[0]
