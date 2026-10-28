"""Route a code-task request to the fix lane or the full pipeline.

Deterministic rules decide first (the brief: "no model call for anything a deterministic
step can do"); a fast model is consulted only when the deterministic signals are genuinely
ambiguous. See the fix-lane spec's acceptance criterion #1:

  * a bug/regression WITH a repro or evidence, an estimated diff <= 3 files / <= 150 lines,
    and no touch on a protected area (auth/identity/sandbox/egress/budget-gates/self-fix-policy)
    routes to the FIX lane;
  * a feature request, a request with no repro, an un-estimable diff, or a protected-path
    touch routes to the FULL pipeline (fail toward the safer, fuller lane);
  * a ``forced_lane`` (operator override, or a Shepherd escalation that forces ``fix``) always
    short-circuits and is honoured without re-evaluation.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)

LANE_FIX = "fix"
LANE_FULL = "full"

MAX_FILES = 3
MAX_LINES = 150

#: Substrings that mark a protected area a fix-lane change may not touch. These mirror the
#: self-fix policy's intent; the policy's own globs are consulted too (see
#: ``_touches_protected_path``) so the two never drift, but these keep triage correct even when
#: the policy file cannot be read.
PROTECTED_PATH_MARKERS = (
    "identity/",
    "/auth",
    "auth/",
    "sandbox",
    "egress",
    "budget",
    "gates",
    "self_fix_policy",
    "self_fix",
)

#: Framings that signal a bug/regression rather than a feature request.
BUG_KINDS = frozenset({"bug", "regression", "defect", "fix", "hotfix"})

#: Free-text keys a chat report's message may arrive under (the "this link fails on first
#: click" entry point has no ``kind`` at all — the message itself is the only signal).
CHAT_REPORT_TEXT_KEYS = ("description", "report", "message", "text")

#: Phrases in a kind-less chat report's text that mark it as a failure report rather than a
#: feature ask. Deliberately narrow: a feature request that happens to carry unrelated
#: "evidence" (e.g. a screenshot) must not be swept into the fix lane by that key's mere
#: presence (see acceptance criterion #1's "fail toward the safer, fuller lane").
BUG_REPORT_PHRASES = (
    "fail", "broken", "break", "bug", "error", "crash", "hang", "regression",
    "doesn't work", "stopped working", "drops", "loses", "wrong", "incorrect",
)


@dataclass(frozen=True)
class TriageVerdict:
    """Which lane a request takes, why, and the signals the decision rested on."""

    lane: str
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)


def _is_bug(spec: dict) -> bool:
    """True when the request is framed as a bug/regression (not a feature).

    An explicit ``kind`` always decides. A chat report has no ``kind`` at all (the
    "this link fails on first click" entry point) — there, the message text itself is the
    only framing signal, so it is read for failure phrasing. A kind-less request is NOT
    inferred to be a bug merely because it carries a ``repro``/``evidence`` key: a feature
    request can carry unrelated evidence (e.g. a screenshot) and must still fail toward the
    full lane.
    """
    kind = str(spec.get("kind") or "").strip().lower()
    if kind:
        return kind in BUG_KINDS
    text = " ".join(
        str(spec.get(key) or "") for key in CHAT_REPORT_TEXT_KEYS
    ).lower()
    return any(phrase in text for phrase in BUG_REPORT_PHRASES)


def _has_repro(spec: dict) -> bool:
    """True when the request carries a reproduction or evidence the red test can key off."""
    for key in ("repro", "reproduction", "evidence", "failing_test", "carried_test", *CHAT_REPORT_TEXT_KEYS):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if value:  # a non-empty structured carried test / evidence object
            return True
    return False


def _policy_protected_globs() -> tuple[str, ...]:
    """The self-fix policy's protected globs, or an empty tuple when it cannot be read.

    Reuses the existing ``self_fix_policy`` machinery so triage and the self-fix gate agree on
    what "protected" means; a missing/malformed policy never breaks triage (the static markers
    still apply, and an un-decidable request already fails toward the full lane)."""
    try:
        from . import self_fix_policy as sp

        policy = sp.load()
        return tuple(rule.glob for rule in (*policy.never, *policy.tier3) if rule.glob)
    except Exception:  # noqa: BLE001 — policy unavailable: fall back to the static markers
        log.debug("fix_lane_triage: self-fix policy unavailable; using static markers", exc_info=True)
        return ()


def _touches_protected_path(touched_paths: list[str]) -> Optional[str]:
    """The first touched path that falls in a protected area, or ``None``."""
    globs = _policy_protected_globs()
    for path in touched_paths or ():
        norm = str(path).replace("\\", "/")
        if any(marker in norm for marker in PROTECTED_PATH_MARKERS):
            return path
        try:
            from . import self_fix_policy as sp

            if any(sp.matches(glob, norm) for glob in globs):
                return path
        except Exception:  # noqa: BLE001 — static markers already checked above
            pass
    return None


def _field(diff_estimate: Any, name: str) -> Any:
    """Read ``name`` from ``diff_estimate`` whether it is an object or a mapping.

    The contract allows ``diff_estimate`` to be an object (attributes) or any mapping (keys,
    including non-``dict`` mappings like ``UserDict``); a plain ``getattr`` silently reads
    ``None`` for every key of a mapping and so routes an oversized mapping-shaped estimate to
    the fix lane by mistake.
    """
    if isinstance(diff_estimate, Mapping):
        return diff_estimate.get(name)
    return getattr(diff_estimate, name, None)


def _model_classify(spec: dict, signals: dict) -> Optional[str]:
    """A fast-model tie-breaker for genuinely ambiguous requests.

    Deterministic rules decide every case the spec enumerates, so there is nothing to call the
    model for here yet; the hook exists so an ambiguous request can be routed by a fast model
    without changing ``classify``'s contract. Returns a lane, or ``None`` to defer to the
    deterministic default."""
    return None


def classify(
    spec: dict,
    *,
    diff_estimate: Optional[Any] = None,
    forced_lane: Optional[str] = None,
) -> TriageVerdict:
    """Decide the lane for ``spec``.

    ``diff_estimate`` carries ``files``, ``lines`` and ``touched_paths`` (an object or mapping);
    ``None`` means it could not be computed, which fails toward the full lane. ``forced_lane``
    (``"fix"`` or ``"full"``) always wins and is never re-evaluated.
    """
    if forced_lane is not None:
        return TriageVerdict(lane=forced_lane, reason="forced_lane",
                             signals={"forced": True, "forced_lane": forced_lane})

    signals: dict[str, Any] = {}

    if not _is_bug(spec):
        return TriageVerdict(lane=LANE_FULL, reason="not_a_bug_report",
                             signals={"kind": spec.get("kind")})

    if not _has_repro(spec):
        return TriageVerdict(lane=LANE_FULL, reason="no_repro", signals={"repro": False})

    if diff_estimate is None:
        return TriageVerdict(lane=LANE_FULL, reason="no_diff_estimate",
                             signals={"diff_estimate": None})

    files = _field(diff_estimate, "files")
    lines = _field(diff_estimate, "lines")
    touched_paths = list(_field(diff_estimate, "touched_paths") or [])
    signals.update({"files": files, "lines": lines})

    # A diff estimate that cannot report both its file and line counts is not an estimate we
    # can size against the fix-lane caps, so it fails toward the full lane (same posture as a
    # wholly-missing estimate). Without this, a mapping/object that omits ``files`` or ``lines``
    # would read ``None`` for the missing field, skip the size check, and route to ``fix``.
    if files is None or lines is None:
        return TriageVerdict(lane=LANE_FULL, reason="incomplete_diff_estimate", signals=signals)

    if files > MAX_FILES or lines > MAX_LINES:
        return TriageVerdict(lane=LANE_FULL, reason="diff_too_large", signals=signals)

    protected = _touches_protected_path(touched_paths)
    if protected is not None:
        return TriageVerdict(lane=LANE_FULL, reason="protected_path_touch",
                             signals={**signals, "protected_path": protected})

    forced_by_model = _model_classify(spec, signals)
    if forced_by_model is not None:
        return TriageVerdict(lane=forced_by_model, reason="model_assisted",
                             signals={**signals, "model_assisted": True})

    return TriageVerdict(lane=LANE_FIX, reason="small_evidenced_bug", signals=signals)
