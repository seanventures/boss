"""Procedural priors v1 (BOSSAPP-1031): the scorekeeper for first moves.

Frontier models invent search discipline on the spot; weak models flail (measured: llama3.1
greping absurdities, 0/24 on the bench). The harness watches which tools strong runs reached
for per KIND of situation, ranks them by how often they led to progress, and hands the ranking
to cheaper models — content-blind. You don't build the use cases, you build the scorekeeper.

Three parts, all deterministic in v1:

  * ``fingerprint`` — a TOTAL function naming the situation: pipeline stage (or ``solo``),
    the repository's dominant language, and whether the agent is still exploring, already
    building, or standing in front of a failing test/command. v2 may add task-kind and
    vectors; v1 stays deterministic on purpose, so a prior can always be replayed and audited.
  * ``build_priors`` — the offline, replay-first fold of ``agent_turns`` into
    ``kg_action_priors`` upserts (one row per org/fingerprint/canonical tool). Idempotent:
    a high-water mark per org in ``system_state`` keeps a re-run from double-counting.
  * the two injection forms behind ``BOSS_PROCEDURAL_PRIORS`` (off | advisory | ordered),
    rendered here and applied by ``NativeCodeAgent._run_tool_loop`` — the native lane only.
    With the knob off, prompts stay BYTE-IDENTICAL to today's.

The stored ``example_excerpt`` is ONE short scrubbed ARGS-SHAPE (key names and value sizes),
never the arguments themselves: the scorekeeper never memorises content.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..engine_contract import BASH, WRITE_TOOLS, canonical_tool_name
from .code_parse import EXTENSIONS
from .learning import days_between, decay, usefulness
from .learning_writeback import error_signature
from .scrub import scrub

LOG = logging.getLogger(__name__)

FINGERPRINT_VERSION = "v1"
#: The knob (config.procedural_priors_mode / BOSS_PROCEDURAL_PRIORS). Anything else reads as off.
MODES = ("off", "advisory", "ordered")
EXPLORATION, BUILDING, POST_TEST_FAILURE = "exploration", "building", "post_test_failure"
SOLO_STAGE = "solo"
UNKNOWN_LANGUAGE = "unknown"
#: Evidence grade ladder (BOSSAPP-1031 v1.1): a judge NEVER outranks ground truth. Mirrors
#: learning_writeback's confidence ladder (log .6 / test .9; Jev slots between at .75).
EVIDENCE_GROUND_TRUTH = "ground_truth"
EVIDENCE_JEV = "jev"
EVIDENCE_CORRELATION = "correlation"
EVIDENCE_GRADE_WEIGHTS: dict[str, float] = {
    EVIDENCE_GROUND_TRUTH: 0.90,
    EVIDENCE_JEV: 0.75,
    EVIDENCE_CORRELATION: 0.60,
}
_EVIDENCE_ORDER = (EVIDENCE_CORRELATION, EVIDENCE_JEV, EVIDENCE_GROUND_TRUTH)
#: Hard timeout + negative cooldown for optional Jev calls (mirrors model_router/classifier.py).
DEFAULT_JEV_TIMEOUT_MS = 1_500
JEV_COOLDOWN_S = 60.0
_JEV_COOL_UNTIL = [0.0]
#: Fixed labels Jev may choose from when labelling an ambiguous fingerprint.
AMBIGUOUS_FINGERPRINT_LABELS: dict[str, str] = {
    "code_exploration": "Inspecting an unfamiliar repository or locating definitions before editing.",
    "bug_diagnosis": "Investigating a bug, error output, or failing behaviour without a clear error class.",
    "test_repair": "Fixing failing tests or build verification commands.",
    "investigation": "Open-ended incident, log, database, or system investigation with no code test signal.",
    "configuration": "Inspecting or updating configuration, environment, or deployment settings.",
    "ops_triage": "Operational, support, or non-engineering triage and status checks.",
    "unclear": "Insufficient evidence to assign a specific situation label.",
}
#: Non-engineering / non-gated stages whose episodes carry no unit-test or build-gate signal.
_NON_GATED_STAGES = frozenset({"investigation", "chat", "triage", "discovery", "ops", "sales", "support"})
#: An action is advised only once it has been SEEN enough to mean something and scored above
#: a smoothed coin toss ((s+1)/(s+f+2) with learning.py's 180-day half-life).
MIN_OBSERVATIONS = 5
MIN_USEFULNESS = 0.45
TOP_ACTIONS = 5
#: How many tool results ahead (same stage) progress is looked for when crediting an action.
PROGRESS_WINDOW = 6
#: The advisory block's hard ceiling (~300 tokens at the usual ~4 chars/token).
ADVISORY_MAX_CHARS = 1_200
MAX_EXCERPT_CHARS = 200
#: system_state key prefix for the per-org high-water mark (idempotent re-runs).
_HWM_KEY = "kg_action_priors_high_water"

#: The native lane's spelling of each canonical tool, for rendering advice the model can act on.
_NATIVE_SPELLING = {"Read": "read_file", "Write": "write_file", "Edit": "edit_file",
                    "Bash": "bash", "Grep": "grep", "Glob": "glob", "LS": "list_dir"}

_CLASS = re.compile(r"[A-Za-z_][\w.]*(?:Error|Exception)\b")
_NOT_TOKEN = re.compile(r"[^a-z0-9_]+")
_PART = re.compile(r"[|\s]+")
#: What the native toolset prints for a command (`$ cmd\n(exit N, Xms)`), plus the test-runner
#: phrases a CLI lane's transcript shows instead.
_EXIT_NONZERO = re.compile(r"\(exit [1-9]\d*[,)]")
_EXIT_ZERO = re.compile(r"\(exit 0[,)]")
_FAILING_TEXT = re.compile(r"\bFAILED\b|\bTraceback \(most recent call last\)|\b\d+ failed\b")
_PASSING_TEXT = re.compile(r"\b\d+ (?:tests? )?passed\b|\ball tests passed\b", re.IGNORECASE)

_SKIP_DIRS = frozenset({"node_modules", ".git", ".hg", "venv", ".venv", "dist", "build",
                        "__pycache__", "vendor", "target", ".next", "coverage"})
_LANGUAGE_SCAN_CAP = 4_000


def mode(settings: Any) -> str:
    """The configured injection mode; an unknown value is OFF, never a surprise."""
    raw = str(getattr(settings, "procedural_priors_mode", "") or "").strip().lower()
    return raw if raw in MODES else "off"


# ── The fingerprint: a total, deterministic name for a situation ─────────────────────────────

def _part(value: Any, fallback: str) -> str:
    cleaned = _PART.sub("_", str(value or "").strip().lower()).strip("_")
    return cleaned[:64] or fallback


def _error_class(output: Any) -> str:
    """A failing output's class token, via the EXISTING learning_writeback.error_signature
    regexes, normalised to a lowercase identifier (``assertionerror``, ``typeerror``)."""
    signature = error_signature(str(output or ""))
    if not signature:
        return "unknown"
    match = _CLASS.search(signature)
    token = match.group(0).rsplit(".", 1)[-1] if match else signature.split()[0]
    token = _NOT_TOKEN.sub("_", token.lower()).strip("_")
    return token[:48] or "unknown"


def situation(last_error: Any = None, *, wrote: bool = False) -> str:
    """``post_test_failure:<class>`` when the previous tool result was a failing test/command,
    else ``exploration`` (no write yet in this stage) or ``building`` (a write happened)."""
    if str(last_error or "").strip():
        return f"{POST_TEST_FAILURE}:{_error_class(last_error)}"
    return BUILDING if wrote else EXPLORATION


def fingerprint(stage: Any, repo_language: Any, last_error: Any = None, *,
                wrote: bool = False) -> str:
    """The situation's deterministic key: ``v1|<stage>|<language>|<situation>``.

    A TOTAL function: any input — None, empty, unreadable — still names a situation, so a
    prior can never fail the work it advises. v2 may add task-kind and vectors; v1 stays
    deterministic so every key can be replayed and audited.
    """
    return "|".join((FINGERPRINT_VERSION,
                     _part(stage, SOLO_STAGE),
                     _part(repo_language, UNKNOWN_LANGUAGE),
                     situation(last_error, wrote=wrote)))


def repo_language_of(root: Any) -> str:
    """The workspace's dominant language by extension count — the cheap fallback the design
    allows, over the same extensions the code graph parses (code_parse.EXTENSIONS)."""
    counts: dict[str, int] = {}
    try:
        seen = 0
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                seen += 1
                if seen > _LANGUAGE_SCAN_CAP:
                    raise StopIteration
                lang = EXTENSIONS.get(os.path.splitext(name)[1].lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    except StopIteration:
        pass
    except Exception:  # noqa: BLE001 — a prior never fails the work it advises
        return UNKNOWN_LANGUAGE
    if not counts:
        return UNKNOWN_LANGUAGE
    return min(counts, key=lambda lang: (-counts[lang], lang))


# ── What a tool result says about progress ───────────────────────────────────────────────────

def looks_failed(ok: Any, content: Any) -> bool:
    """A failing test/command: the result errored, or its text shows a non-zero exit or a
    test runner's failure line (the native `bash` returns failing tests as ok results)."""
    if ok is False:
        return True
    text_ = str(content or "")
    return bool(_EXIT_NONZERO.search(text_) or _FAILING_TEXT.search(text_))


def shows_success(content: Any) -> bool:
    text_ = str(content or "")
    return bool(_EXIT_ZERO.search(text_) or _PASSING_TEXT.search(text_))


class SituationTracker:
    """The situation state machine, shared by the offline fold and the live loop.

    ``wrote`` flips on the first ok Write/Edit-class result of the stage; ``last_error`` holds
    the PREVIOUS tool result's text only when it was a failing test/command, so the situation
    is ``post_test_failure`` exactly while the agent stands in front of that failure.
    """

    def __init__(self, *, stage: Any = None, repo_language: Any = None) -> None:
        self.stage = stage
        self.repo_language = repo_language
        self.wrote = False
        self.last_error: Optional[str] = None
        self.failed_in_stage = False

    def observe_result(self, tool_name: Any, content: Any, *, ok: Any = True) -> None:
        canonical = canonical_tool_name(tool_name)
        if canonical in WRITE_TOOLS and ok is not False:
            self.wrote = True
        if canonical == BASH and looks_failed(ok, content):
            self.last_error = str(content or "")[:600]
            self.failed_in_stage = True
        else:
            self.last_error = None

    @property
    def key(self) -> str:
        return fingerprint(self.stage, self.repo_language, self.last_error, wrote=self.wrote)


def args_shape(args: Any) -> str:
    """The SHAPE of a call's arguments, never the arguments: sorted key names with each
    value's type and size. Scrubbed anyway (scrub.py), belt and braces."""
    if not isinstance(args, Mapping):
        shape = f"<{type(args).__name__}>"
    else:
        parts = []
        for key in sorted(str(k) for k in args)[:8]:
            value = args.get(key)
            if isinstance(value, str):
                parts.append(f"{key}=<str {len(value)}>")
            elif isinstance(value, bool):
                parts.append(f"{key}=<bool>")
            elif isinstance(value, (int, float)):
                parts.append(f"{key}=<{type(value).__name__}>")
            elif isinstance(value, Mapping):
                parts.append(f"{key}=<dict {len(value)}>")
            elif isinstance(value, (list, tuple)):
                parts.append(f"{key}=<list {len(value)}>")
            else:
                parts.append(f"{key}=<{type(value).__name__}>")
        shape = ", ".join(parts)
    cleaned, _counts = scrub(shape)
    return cleaned[:MAX_EXCERPT_CHARS]


# ── The offline fold: agent_turns -> kg_action_priors ────────────────────────────────────────

@contextmanager
def _tx(engine_or_session: Any):
    """A transaction on an Engine, or the caller's own Connection/session untouched."""
    if isinstance(engine_or_session, Connection) or not hasattr(engine_or_session, "begin"):
        yield engine_or_session
        return
    if hasattr(engine_or_session, "execute"):  # an already-open session with begin()
        yield engine_or_session
        return
    with engine_or_session.begin() as connection:
        yield connection


def _high_water(connection: Any, org_id: str) -> Optional[datetime]:
    raw = connection.execute(
        text("SELECT value->>'high_water' FROM system_state WHERE key = :k"),
        {"k": f"{_HWM_KEY}:{org_id}"}).scalar()
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _set_high_water(connection: Any, org_id: str, at: datetime) -> None:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    connection.execute(text(
        """
        INSERT INTO system_state (key, value, updated_at)
        VALUES (:k, CAST(:v AS JSONB), now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """),
        {"k": f"{_HWM_KEY}:{org_id}",
         "v": json.dumps({"high_water": at.astimezone(timezone.utc).isoformat()})})


def _run_language(rows: list[Mapping[str, Any]]) -> str:
    """A replayed run's dominant language, from the file paths its tool calls touched —
    the only workspace evidence a transcript carries."""
    counts: dict[str, int] = {}
    for row in rows:
        if row["direction"] != "tool_call":
            continue
        try:
            args = json.loads(row["content"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(args, Mapping):
            continue
        for key in ("file_path", "path", "filename", "file", "target_file"):
            value = args.get(key)
            if isinstance(value, str):
                lang = EXTENSIONS.get(os.path.splitext(value)[1].lower())
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return UNKNOWN_LANGUAGE
    return min(counts, key=lambda lang: (-counts[lang], lang))


def evidence_grade_weight(grade: Optional[str]) -> float:
    """Numeric weight for an evidence grade (`ground_truth` 0.90 > `jev` 0.75 > `correlation` 0.60)."""
    return EVIDENCE_GRADE_WEIGHTS.get(str(grade or "").strip().lower(),
                                      EVIDENCE_GRADE_WEIGHTS[EVIDENCE_CORRELATION])


def stronger_grade(a: Optional[str], b: Optional[str]) -> str:
    """Higher of two evidence grades — deterministic ground truth always outranks a judge."""
    ga = str(a or EVIDENCE_CORRELATION).strip().lower()
    gb = str(b or EVIDENCE_CORRELATION).strip().lower()
    ia = _EVIDENCE_ORDER.index(ga) if ga in _EVIDENCE_ORDER else 0
    ib = _EVIDENCE_ORDER.index(gb) if gb in _EVIDENCE_ORDER else 0
    return _EVIDENCE_ORDER[max(ia, ib)]


def reset_jev_cooldown() -> None:
    """Clear the Jev timeout negative-cache cooldown (for tests)."""
    _JEV_COOL_UNTIL[0] = 0.0


def optional_jev_decider(settings: Any) -> Optional[Any]:
    """Return a TypeSafeDecider when Jev is configured and enabled, else None. Never raises."""
    try:
        from .. import typed_decisions

        if not typed_decisions.typesafe_decides(settings):
            return None
        return typed_decisions.TypeSafeDecider(settings, attempts=1)
    except Exception:  # noqa: BLE001 — optional backend; absence falls back deterministically
        return None


def _bounded_jev(fn: Callable[[], Any], timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS) -> Any:
    """Run `fn()` inside a dedicated daemon thread bounded by `timeout_ms` (classifier.py pattern)."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def _run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — surfaced to caller
            box["error"] = exc
        finally:
            done.set()

    from ...db import spawn_thread

    spawn_thread(_run, name="priors-jev", daemon=True)
    if not done.wait(max(0.01, float(timeout_ms) / 1000.0)):
        raise FutureTimeout()
    if "error" in box:
        raise box["error"]
    return box["value"]


@dataclass(frozen=True)
class ProgressVerdict:
    credited: bool
    verdict: str             # yes | no | unclear
    evidence_grade: str      # ground_truth | jev | correlation
    judge: str               # deterministic | jev | fallback | correlation
    reason: str
    confidence: Optional[float] = None


@dataclass(frozen=True)
class FingerprintLabel:
    fingerprint_key: str
    label: str
    ambiguous: bool
    evidence_grade: str      # ground_truth | jev | correlation
    judge: str               # deterministic | jev | fallback
    reason: str
    confidence: Optional[float] = None


@dataclass(frozen=True)
class SanityVerdict:
    approved: bool
    verdict: str             # yes | no
    reason: str
    judge: str               # deterministic | jev | fallback
    confidence: Optional[float] = None
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_ambiguous_fingerprint(fingerprint_key: str) -> bool:
    """True when deterministic features leave the situation underspecified."""
    parts = (str(fingerprint_key or "").split("|") + ["", "", "", ""])[:4]
    _ver, stage, language, sit = parts
    if language in ("", UNKNOWN_LANGUAGE):
        return True
    if sit in ("", f"{POST_TEST_FAILURE}:unknown"):
        return True
    if stage in ("", SOLO_STAGE) and sit == EXPLORATION and language == UNKNOWN_LANGUAGE:
        return True
    return False


def label_fingerprint(
    fingerprint_key: str,
    *,
    context: str = "",
    jev: Optional[Any] = None,
    timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS,
    min_confidence: float = 0.5,
) -> FingerprintLabel:
    """Label a fingerprint: deterministic first -> Jev (with hard timeout) -> safe fallback.

    Unambiguous fingerprints never call Jev. Ambiguous ones ask Jev over a fixed label set
    within `timeout_ms` and fall back to `_human_words` on absence, timeout, error, or unclear.
    """
    fallback_label = _human_words(fingerprint_key)
    if not is_ambiguous_fingerprint(fingerprint_key):
        return FingerprintLabel(
            fingerprint_key=fingerprint_key,
            label=fallback_label,
            ambiguous=False,
            evidence_grade=EVIDENCE_GROUND_TRUTH,
            judge="deterministic",
            reason="deterministic fingerprint features are unambiguous",
            confidence=1.0,
        )
    if jev is None:
        return FingerprintLabel(
            fingerprint_key=fingerprint_key,
            label=fallback_label,
            ambiguous=True,
            evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback",
            reason="Jev unavailable; kept deterministic label",
        )
    if time.monotonic() < _JEV_COOL_UNTIL[0]:
        return FingerprintLabel(
            fingerprint_key=fingerprint_key,
            label=fallback_label,
            ambiguous=True,
            evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback",
            reason="Jev cooling down after timeout; kept deterministic label",
        )
    from ..typed_decisions import Question

    cleaned_ctx, _ = scrub(str(context or "")[:1_000])
    question = Question(
        key="fingerprint_label",
        instructions="Which situation label best describes this ambiguous agent context?",
        options=dict(AMBIGUOUS_FINGERPRINT_LABELS),
    )
    prompt = f"Fingerprint: {fingerprint_key}\nSummary: {fallback_label}\nContext:\n{cleaned_ctx}"
    try:
        batch = _bounded_jev(lambda: jev.decide(prompt, [question]), timeout_ms)
        decision = (getattr(batch, "decisions", None) or {}).get("fingerprint_label")
        conf = float(decision.confidence) if (decision and decision.confidence is not None) else 0.0
        if (
            decision is not None
            and decision.answer in AMBIGUOUS_FINGERPRINT_LABELS
            and decision.answer != "unclear"
            and conf >= min_confidence
        ):
            return FingerprintLabel(
                fingerprint_key=fingerprint_key,
                label=str(decision.answer),
                ambiguous=True,
                evidence_grade=EVIDENCE_JEV,
                judge="jev",
                reason=(decision.reason or f"Jev labelled ambiguous fingerprint as {decision.answer}")[:200],
                confidence=conf,
            )
        return FingerprintLabel(
            fingerprint_key=fingerprint_key,
            label=fallback_label,
            ambiguous=True,
            evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback",
            reason="Jev returned unclear or low confidence; kept deterministic label",
            confidence=conf if decision is not None else None,
        )
    except FutureTimeout:
        _JEV_COOL_UNTIL[0] = time.monotonic() + JEV_COOLDOWN_S
        return FingerprintLabel(
            fingerprint_key=fingerprint_key,
            label=fallback_label,
            ambiguous=True,
            evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback",
            reason="Jev timed out; kept deterministic label",
        )
    except Exception as exc:  # noqa: BLE001 — never fail priors on optional judge
        return FingerprintLabel(
            fingerprint_key=fingerprint_key,
            label=fallback_label,
            ambiguous=True,
            evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback",
            reason=f"Jev failed ({type(exc).__name__}); kept deterministic label",
        )


def _deterministic_progress(
    rows: list[Mapping[str, Any]], index: int, *, failed_before: bool
) -> Optional[ProgressVerdict]:
    """Deterministic ground-truth check. Returns None only when no test/write/gate signal exists."""
    stage = rows[index].get("stage")
    failed = failed_before
    results_seen = 0
    saw_test_signal = False
    for row in rows[index + 1:]:
        if row.get("stage") != stage:
            break
        if row.get("direction") != "tool_result":
            continue
        results_seen += 1
        if results_seen > PROGRESS_WINDOW:
            break
        canonical = canonical_tool_name(row.get("tool_name"))
        ok = row.get("ok") is not False
        if canonical in WRITE_TOOLS and ok:
            return ProgressVerdict(
                credited=True, verdict="yes", evidence_grade=EVIDENCE_GROUND_TRUTH,
                judge="deterministic", reason="accepted write/edit in progress window",
                confidence=1.0,
            )
        if canonical == BASH:
            if failed and shows_success(row.get("content")):
                return ProgressVerdict(
                    credited=True, verdict="yes", evidence_grade=EVIDENCE_GROUND_TRUTH,
                    judge="deterministic", reason="tests/command passed after prior failure",
                    confidence=1.0,
                )
            if looks_failed(row.get("ok"), row.get("content")):
                failed = True
                saw_test_signal = True
            elif shows_success(row.get("content")):
                saw_test_signal = True

    stage_name = str(stage or "").strip().lower()
    explicit_no_gate = rows[index].get("has_gate_signal") is False or stage_name in _NON_GATED_STAGES
    run_status = str(rows[index].get("run_status") or "").strip().lower()
    if not explicit_no_gate:
        if results_seen <= PROGRESS_WINDOW and run_status == "completed":
            return ProgressVerdict(
                credited=True, verdict="yes", evidence_grade=EVIDENCE_GROUND_TRUTH,
                judge="deterministic", reason="run completed (gates passed)",
                confidence=1.0,
            )
        if run_status == "failed" or saw_test_signal:
            return ProgressVerdict(
                credited=False, verdict="no", evidence_grade=EVIDENCE_GROUND_TRUTH,
                judge="deterministic",
                reason="run failed gates" if run_status == "failed" else "no progress in window",
                confidence=1.0,
            )
    return None


def judge_step_progress(
    rows: list[Mapping[str, Any]],
    index: int,
    *,
    failed_before: bool = False,
    jev: Optional[Any] = None,
    timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS,
    min_confidence: float = 0.5,
) -> ProgressVerdict:
    """v1.1 success judge (BOSSAPP-1031): deterministic ground truth -> Jev (timeout) -> fallback.

    A judge NEVER outranks ground truth: when a deterministic write, test pass/fail, or run gate
    verdict exists, Jev is not consulted. Only episodes with no test/gate signal (investigation,
    chat, ops, sales, or un-gated runs) cascade to Jev.
    """
    ground_truth = _deterministic_progress(rows, index, failed_before=failed_before)
    if ground_truth is not None:
        return ground_truth
    if jev is None:
        return ProgressVerdict(
            credited=False, verdict="unclear", evidence_grade=EVIDENCE_CORRELATION,
            judge="correlation", reason="no deterministic signal and no Jev judge configured",
        )
    if time.monotonic() < _JEV_COOL_UNTIL[0]:
        return ProgressVerdict(
            credited=False, verdict="unclear", evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback", reason="Jev cooling down after timeout; fell back to correlation",
        )
    from ..typed_decisions import Question

    step_row = rows[index]
    action = canonical_tool_name(step_row.get("tool_name")) or str(step_row.get("tool_name") or "")
    window_summary: list[str] = []
    for nxt in rows[index + 1:index + 1 + PROGRESS_WINDOW * 2]:
        if nxt.get("stage") != step_row.get("stage"):
            break
        snippet, _ = scrub(str(nxt.get("content") or "")[:160])
        window_summary.append(f"{nxt.get('direction')}:{nxt.get('tool_name')} ok={nxt.get('ok')} -> {snippet}")
    prompt = (
        f"Stage: {step_row.get('stage') or SOLO_STAGE}\n"
        f"Step tool: {action}\n"
        f"Following observations:\n" + ("\n".join(window_summary) or "(none)")
    )
    question = Question(
        key="step_progress",
        instructions="Did this tool step advance the goal?",
        options={
            "yes": "The step clearly produced useful evidence or advanced toward the goal.",
            "no": "The step wasted a turn, errored unproductively, or made no progress.",
            "unclear": "Cannot tell from the available observations.",
        },
    )
    try:
        batch = _bounded_jev(lambda: jev.decide(prompt, [question]), timeout_ms)
        decision = (getattr(batch, "decisions", None) or {}).get("step_progress")
        conf = float(decision.confidence) if (decision and decision.confidence is not None) else 0.0
        if decision is not None and decision.answer == "yes" and conf >= min_confidence:
            return ProgressVerdict(
                credited=True, verdict="yes", evidence_grade=EVIDENCE_JEV,
                judge="jev", reason=(decision.reason or "Jev judged step advanced goal")[:200],
                confidence=conf,
            )
        if decision is not None and decision.answer == "no" and conf >= min_confidence:
            return ProgressVerdict(
                credited=False, verdict="no", evidence_grade=EVIDENCE_JEV,
                judge="jev", reason=(decision.reason or "Jev judged step did not advance goal")[:200],
                confidence=conf,
            )
        return ProgressVerdict(
            credited=False, verdict="unclear", evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback", reason=(getattr(decision, "reason", "") or "Jev verdict unclear")[:200],
            confidence=conf if decision is not None else None,
        )
    except FutureTimeout:
        _JEV_COOL_UNTIL[0] = time.monotonic() + JEV_COOLDOWN_S
        return ProgressVerdict(
            credited=False, verdict="unclear", evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback", reason="Jev timed out; fell back to correlation",
        )
    except Exception as exc:  # noqa: BLE001 — optional judge never breaks fold
        return ProgressVerdict(
            credited=False, verdict="unclear", evidence_grade=EVIDENCE_CORRELATION,
            judge="fallback", reason=f"Jev failed ({type(exc).__name__}); fell back to correlation",
        )


def sanity_gate_prior(
    fingerprint_key: str,
    action: str,
    *,
    observations: int,
    successes: int,
    usefulness_score: float,
    evidence_grade: str = EVIDENCE_GROUND_TRUTH,
    example_excerpt: str = "",
    jev: Optional[Any] = None,
    timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS,
    min_confidence: float = 0.6,
    allow_deterministic_graduation: bool = False,
    connection: Any = None,
    org_id: Optional[str] = None,
) -> SanityVerdict:
    """Sanity-gate a prior before it graduates from advisory card to ENFORCED tool ordering.

    Typed yes/no + reasons, recorded on `kg_action_priors` when `connection` and `org_id` are
    supplied. Fails closed (`approved=False`, stays advisory) on timeout, error, or missing Jev.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    if observations < MIN_OBSERVATIONS or usefulness_score < MIN_USEFULNESS:
        verdict = SanityVerdict(
            approved=False, verdict="no",
            reason=f"below threshold (observations={observations}, usefulness={usefulness_score:.2f})",
            judge="deterministic", confidence=1.0, checked_at=now_iso,
        )
    elif jev is None:
        if (
            allow_deterministic_graduation
            and evidence_grade == EVIDENCE_GROUND_TRUTH
            and usefulness_score >= 0.65
        ):
            verdict = SanityVerdict(
                approved=True, verdict="yes",
                reason="deterministic ground-truth graduation approved",
                judge="deterministic", confidence=usefulness_score, checked_at=now_iso,
            )
        else:
            verdict = SanityVerdict(
                approved=False, verdict="no",
                reason="Jev sanity-gate unavailable; prior stays advisory",
                judge="fallback", confidence=None, checked_at=now_iso,
            )
    elif time.monotonic() < _JEV_COOL_UNTIL[0]:
        verdict = SanityVerdict(
            approved=False, verdict="no",
            reason="Jev cooling down after timeout; prior stays advisory",
            judge="fallback", confidence=None, checked_at=now_iso,
        )
    else:
        from ..typed_decisions import Question

        question = Question(
            key="approve_ordering",
            instructions=(
                "Should this procedural prior graduate from an advisory note to enforced "
                "tool ordering for this situation?"
            ),
            options={
                "yes": "Promoting this tool first in this situation is sensible and safe.",
                "no": "Keep advisory only (risky, noisy, or misleading as a default first move).",
            },
        )
        cleaned_excerpt, _ = scrub(str(example_excerpt or "")[:MAX_EXCERPT_CHARS])
        prompt = (
            f"Situation: {fingerprint_key} ({_human_words(fingerprint_key)})\n"
            f"Candidate tool: {action} (native: {native_tool_name(action)})\n"
            f"Stats: {successes}/{observations} successes, usefulness={usefulness_score:.2f}, "
            f"evidence_grade={evidence_grade}\n"
            f"Args shape: {cleaned_excerpt or '(none)'}"
        )
        try:
            batch = _bounded_jev(lambda: jev.decide(prompt, [question]), timeout_ms)
            decision = (getattr(batch, "decisions", None) or {}).get("approve_ordering")
            conf = float(decision.confidence) if (decision and decision.confidence is not None) else 0.0
            if decision is not None and decision.answer == "yes" and conf >= min_confidence:
                verdict = SanityVerdict(
                    approved=True, verdict="yes",
                    reason=(decision.reason or "Jev approved graduation to enforced ordering")[:240],
                    judge="jev", confidence=conf, checked_at=now_iso,
                )
            else:
                reason = (
                    (decision.reason if decision and decision.reason else "")
                    or (f"Jev declined graduation (answer={getattr(decision, 'answer', None)}, conf={conf:.2f})")
                )
                verdict = SanityVerdict(
                    approved=False, verdict="no", reason=reason[:240],
                    judge="jev" if decision is not None and decision.answer in ("yes", "no") else "fallback",
                    confidence=conf if decision is not None else None, checked_at=now_iso,
                )
        except FutureTimeout:
            _JEV_COOL_UNTIL[0] = time.monotonic() + JEV_COOLDOWN_S
            verdict = SanityVerdict(
                approved=False, verdict="no",
                reason="Jev sanity-gate timed out; prior stays advisory",
                judge="fallback", confidence=None, checked_at=now_iso,
            )
        except Exception as exc:  # noqa: BLE001 — fail closed on error
            verdict = SanityVerdict(
                approved=False, verdict="no",
                reason=f"Jev sanity-gate failed ({type(exc).__name__}); prior stays advisory",
                judge="fallback", confidence=None, checked_at=now_iso,
            )

    if connection is not None and org_id:
        connection.execute(text(
            """
            UPDATE kg_action_priors
               SET ordering_approved = :approved,
                   sanity_verdict = CAST(:verdict AS JSONB)
             WHERE org_id = :org AND fingerprint_key = :fp AND action = :action
            """),
            {"approved": verdict.approved, "verdict": json.dumps(verdict.to_dict()),
             "org": org_id, "fp": fingerprint_key, "action": action})
    return verdict


def _credit(rows: list[Mapping[str, Any]], index: int, *, failed_before: bool) -> bool:
    """v1 deterministic progress credit helper (delegates to deterministic-first `judge_step_progress`)."""
    return judge_step_progress(rows, index, failed_before=failed_before, jev=None).credited


def build_priors(
    engine_or_session: Any,
    *,
    org_id: str,
    since: Optional[datetime] = None,
    jev: Optional[Any] = None,
    jev_timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS,
    sanity_gate: bool = False,
) -> dict:
    """Fold ``agent_turns`` (with their run context) into ``kg_action_priors`` for one org.

    Offline and replay-first: walks each run's turns in order, runs the situation state machine,
    and credits each tool_call using `judge_step_progress` (deterministic ground truth first,
    cascading to optional Jev with hard timeout for episodes with no test/gate signal).
    Optionally labels ambiguous fingerprints and runs Jev sanity-gating before enforced ordering.

    Idempotent: without ``since`` it resumes from the org's high-water mark (a system_state
    row) and advances it to the newest turn seen, so a re-run never double-counts.
    """
    org = str(org_id or "").strip()
    if not org:
        raise ValueError("org_id is required")
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    fingerprint_labels: dict[str, str] = {}
    grade_counts: dict[str, int] = {
        EVIDENCE_GROUND_TRUTH: 0, EVIDENCE_JEV: 0, EVIDENCE_CORRELATION: 0,
    }
    observations = successes = 0
    newest: Optional[datetime] = None
    sanity_gated = 0
    with _tx(engine_or_session) as connection:
        effective_since = since if since is not None else _high_water(connection, org)
        rows = [dict(r) for r in connection.execute(text(
            """
            SELECT t.code_task_run_id AS run_id, t.stage, t.direction, t.tool_name,
                   t.content, t.ok, t.created_at, t.turn_index, r.status AS run_status
            FROM agent_turns t
            JOIN code_task_runs r ON r.id = t.code_task_run_id
            JOIN projects p ON p.id = t.project_id
            JOIN workspaces w ON w.id = p.workspace_id
            WHERE w.org_id = :org
              AND (CAST(:since AS TIMESTAMPTZ) IS NULL OR t.created_at > :since)
              AND t.direction IN ('tool_call', 'tool_result')
            ORDER BY t.code_task_run_id, t.created_at, t.turn_index, t.id
            """), {"org": org, "since": effective_since}).mappings().all()]
        runs: dict[str, list[dict]] = {}
        for row in rows:
            runs.setdefault(str(row["run_id"]), []).append(row)
            if newest is None or row["created_at"] > newest:
                newest = row["created_at"]
        for run_rows in runs.values():
            language = _run_language(run_rows)
            tracker = SituationTracker(repo_language=language)
            current_stage: Any = object()
            for index, row in enumerate(run_rows):
                if row["stage"] != current_stage:  # a new stage starts a fresh situation
                    current_stage = row["stage"]
                    tracker = SituationTracker(stage=row["stage"] or SOLO_STAGE,
                                               repo_language=language)
                if row["direction"] == "tool_result":
                    tracker.observe_result(row["tool_name"], row["content"], ok=row["ok"])
                    continue
                action = canonical_tool_name(row["tool_name"])
                if not action:
                    continue
                fp_key = tracker.key
                if jev is not None and fp_key not in fingerprint_labels and is_ambiguous_fingerprint(fp_key):
                    lbl = label_fingerprint(
                        fp_key, context=str(row.get("content") or "")[:400],
                        jev=jev, timeout_ms=jev_timeout_ms,
                    )
                    if lbl.judge == "jev":
                        fingerprint_labels[fp_key] = lbl.label
                verdict = judge_step_progress(
                    run_rows, index, failed_before=tracker.failed_in_stage,
                    jev=jev, timeout_ms=jev_timeout_ms,
                )
                grade_counts[verdict.evidence_grade] = grade_counts.get(verdict.evidence_grade, 0) + 1
                key = (fp_key, action)
                stat = stats.setdefault(key, {
                    "successes": 0, "failures": 0, "last_seen": row["created_at"],
                    "example_run_id": None, "example_excerpt": None,
                    "evidence_grade": verdict.evidence_grade,
                    "fingerprint_label": fingerprint_labels.get(fp_key),
                })
                stat["evidence_grade"] = stronger_grade(stat["evidence_grade"], verdict.evidence_grade)
                if not stat["fingerprint_label"] and fp_key in fingerprint_labels:
                    stat["fingerprint_label"] = fingerprint_labels[fp_key]
                stat["successes" if verdict.credited else "failures"] += 1
                observations += 1
                successes += int(verdict.credited)
                if row["created_at"] > stat["last_seen"]:
                    stat["last_seen"] = row["created_at"]
                if stat["example_excerpt"] is None:
                    try:
                        args = json.loads(row["content"] or "{}")
                    except (TypeError, ValueError):
                        args = None
                    if args is not None:
                        stat["example_run_id"] = str(row["run_id"])
                        stat["example_excerpt"] = args_shape(args)
        for (fingerprint_key, action), stat in stats.items():
            connection.execute(text(
                """
                INSERT INTO kg_action_priors
                    (id, org_id, fingerprint_key, action, success_count, failure_count,
                     last_seen_at, example_run_id, example_excerpt, evidence_grade, fingerprint_label)
                VALUES (:id, :org, :fp, :action, :s, :f, :seen, :run, :excerpt, :grade, :label)
                ON CONFLICT (org_id, fingerprint_key, action) DO UPDATE SET
                    success_count = kg_action_priors.success_count + EXCLUDED.success_count,
                    failure_count = kg_action_priors.failure_count + EXCLUDED.failure_count,
                    last_seen_at = GREATEST(kg_action_priors.last_seen_at, EXCLUDED.last_seen_at),
                    example_run_id = COALESCE(kg_action_priors.example_run_id, EXCLUDED.example_run_id),
                    example_excerpt = COALESCE(kg_action_priors.example_excerpt, EXCLUDED.example_excerpt),
                    evidence_grade = CASE
                        WHEN kg_action_priors.evidence_grade = 'ground_truth'
                             OR EXCLUDED.evidence_grade = 'ground_truth' THEN 'ground_truth'
                        WHEN kg_action_priors.evidence_grade = 'jev'
                             OR EXCLUDED.evidence_grade = 'jev' THEN 'jev'
                        ELSE 'correlation'
                    END,
                    fingerprint_label = COALESCE(kg_action_priors.fingerprint_label, EXCLUDED.fingerprint_label)
                """),
                {"id": uuid.uuid4().hex, "org": org, "fp": fingerprint_key, "action": action,
                 "s": stat["successes"], "f": stat["failures"], "seen": stat["last_seen"],
                 "run": stat["example_run_id"], "excerpt": stat["example_excerpt"],
                 "grade": stat["evidence_grade"], "label": stat["fingerprint_label"]})
        if sanity_gate and stats:
            for fingerprint_key, action in sorted(stats):
                row = connection.execute(text(
                    """
                    SELECT success_count, failure_count, evidence_grade, example_excerpt
                      FROM kg_action_priors
                     WHERE org_id = :org AND fingerprint_key = :fp AND action = :action
                    """), {"org": org, "fp": fingerprint_key, "action": action}).mappings().first()
                if row is None:
                    continue
                s, f = int(row["success_count"] or 0), int(row["failure_count"] or 0)
                if s + f < MIN_OBSERVATIONS:
                    continue
                score = usefulness(s, f)
                if score < MIN_USEFULNESS:
                    continue
                sanity_gate_prior(
                    fingerprint_key, action,
                    observations=s + f, successes=s, usefulness_score=score,
                    evidence_grade=str(row["evidence_grade"] or EVIDENCE_GROUND_TRUTH),
                    example_excerpt=str(row["example_excerpt"] or ""),
                    jev=jev, timeout_ms=jev_timeout_ms,
                    connection=connection, org_id=org,
                )
                sanity_gated += 1
        if newest is not None:
            _set_high_water(connection, org, newest)
    return {
        "org_id": org,
        "fingerprints": len({fp for fp, _ in stats}),
        "actions": len(stats),
        "observations": observations,
        "successes": successes,
        "failures": observations - successes,
        "evidence_grades": grade_counts,
        "labelled_fingerprints": len(fingerprint_labels),
        "sanity_gated": sanity_gated,
        "high_water": newest.isoformat() if newest is not None else None,
    }


# ── Ranking and the two injection forms ──────────────────────────────────────────────────────

def ranked_actions(connection: Any, *, org_id: str, fingerprint_key: str,
                   now: Optional[datetime] = None) -> list[dict]:
    """The situation's advisable actions, best first: learning.py's usefulness
    ((s+1)/(s+f+2)) decayed by the same 180-day half-life since the prior was last seen,
    thresholded (MIN_OBSERVATIONS, MIN_USEFULNESS), ranked by evidence-grade-weighted score
    (ground_truth > jev > correlation), top TOP_ACTIONS, alphabetical tiebreak."""
    at = now or datetime.now(timezone.utc)
    out = []
    for row in connection.execute(text(
            """
            SELECT action, success_count, failure_count, last_seen_at,
                   COALESCE(evidence_grade, 'ground_truth') AS evidence_grade,
                   fingerprint_label, ordering_approved
            FROM kg_action_priors WHERE org_id = :org AND fingerprint_key = :fp
            """), {"org": org_id, "fp": fingerprint_key}).mappings().all():
        s, f = int(row["success_count"] or 0), int(row["failure_count"] or 0)
        if s + f < MIN_OBSERVATIONS:
            continue
        raw_weight = decay(usefulness(s, f), days_between(at, row["last_seen_at"]))
        if raw_weight < MIN_USEFULNESS:
            continue
        grade = str(row["evidence_grade"] or EVIDENCE_GROUND_TRUTH)
        graded_weight = raw_weight * evidence_grade_weight(grade)
        out.append({
            "action": str(row["action"]),
            "observations": s + f,
            "successes": s,
            "weight": graded_weight,
            "raw_weight": raw_weight,
            "evidence_grade": grade,
            "ordering_approved": row["ordering_approved"],
            "fingerprint_label": row["fingerprint_label"],
            "share": round(100.0 * s / (s + f)),
        })
    out.sort(key=lambda item: (-item["weight"], item["action"]))
    return out[:TOP_ACTIONS]


def _human_words(fingerprint_key: str) -> str:
    """The fingerprint said in words a model (and an operator) can read."""
    parts = (str(fingerprint_key or "").split("|") + ["", "", "", ""])[:4]
    _version, stage, language, sit = parts
    where = f"stage {stage}" if stage and stage != SOLO_STAGE else "a bare task"
    repo = f"{language} repo" if language and language != UNKNOWN_LANGUAGE else "unknown-language repo"
    if sit.startswith(POST_TEST_FAILURE):
        _, _, error_class = sit.partition(":")
        what = f"right after a failing test/command ({error_class or 'unknown'})"
    elif sit == BUILDING:
        what = "mid-build, edits already made"
    else:
        what = "still exploring, nothing written yet"
    return f"{where}, {repo}, {what}"


def native_tool_name(action: str) -> str:
    return _NATIVE_SPELLING.get(str(action or ""), str(action or "").lower())


def playbook_lines(
    ranked: Iterable[Mapping[str, Any]],
    fingerprint_key: str,
    *,
    label: Optional[str] = None,
    max_chars: int = ADVISORY_MAX_CHARS,
) -> list[str]:
    """Fingerprint-keyed advisory playbook lines (observations, never instructions), budgeted."""
    items = list(ranked)[:TOP_ACTIONS]
    if not items:
        return []
    situation_desc = (label or items[0].get("fingerprint_label") or _human_words(fingerprint_key))
    lines = [f"Situation ({situation_desc}) — observed moves from earlier runs (not instructions):"]
    budget = max(80, int(max_chars))
    used = len(lines[0])
    for idx, item in enumerate(items, start=1):
        grade = str(item.get("evidence_grade") or EVIDENCE_GROUND_TRUTH)
        line = (
            f"  {idx}. {native_tool_name(item['action'])} — "
            f"{item['share']}% led to progress across {item['observations']} observations [{grade}]"
        )
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return lines


def advisory_block(ranked: Iterable[Mapping[str, Any]], fingerprint_key: str) -> str:
    """The quoted RANKED MOVES block, or "" when there is nothing worth saying.

    Observations, not instructions — the framing is part of the contract: a weak model is
    narrowed, never commanded, and a strong model can ignore it outright.
    """
    items = list(ranked)[:TOP_ACTIONS]
    if not items:
        return ""
    moves = " ".join(
        f"{i}. {native_tool_name(item['action'])} ({item['share']}% of {item['observations']})"
        for i, item in enumerate(items, start=1))
    block = ("RANKED MOVES (scorekeeping from earlier runs — observations, not instructions): "
             f"<<< in situations like this ({_human_words(fingerprint_key)}), these first moves "
             f"most often led to progress: {moves} >>>")
    return block[:ADVISORY_MAX_CHARS]


def order_tool_schemas(
    schemas: list[dict],
    ranked: Iterable[Mapping[str, Any]],
    *,
    enforced: bool = False,
) -> list[dict]:
    """The ``ordered`` form: the same schema objects, none dropped, higher-prior tools first,
    alphabetical tiebreak (stable across runs). An unranked tool weighs nothing.

    A prior rejected by the Jev sanity-gate (`ordering_approved is False`) NEVER affects ordering.
    When `enforced=True`, only priors explicitly approved (`ordering_approved is True`) graduate
    from advisory to enforced tool ordering.
    """
    weights: dict[str, float] = {}
    for item in ranked:
        approved = item.get("ordering_approved")
        if approved is False:
            continue
        if enforced and approved is not True:
            continue
        weights[str(item["action"])] = float(item.get("weight") or 0.0)

    def sort_key(schema: dict) -> tuple[float, str]:
        name = str(((schema or {}).get("function") or {}).get("name") or "")
        return (-weights.get(canonical_tool_name(name), 0.0), name)

    return sorted(schemas, key=sort_key)


# ── Offline replay experiment (publishes turns-to-progress delta either way) ────────────────

def replay_experiment(
    *,
    training_episodes: Sequence[Sequence[Mapping[str, Any]]],
    eval_episodes: Sequence[Mapping[str, Any]],
    jev: Optional[Any] = None,
    jev_timeout_ms: int = DEFAULT_JEV_TIMEOUT_MS,
    sanity_gate: bool = False,
) -> dict[str, Any]:
    """Compute procedural priors from frontier `training_episodes` and measure turns-to-progress
    and pass-rate delta across `eval_episodes` with priors OFF vs ON — publishing the delta
    honestly whether it improves, regresses, or stays unchanged (BOSSAPP-1031).

    Each item in `eval_episodes` carries:
      * `fingerprint_key`: situation key (or derived from `stage`/`repo_language`/`last_error`)
      * `baseline_turns_to_progress`: turns taken with priors OFF
      * `baseline_passed`: bool
      * `baseline_first_move`: canonical tool chosen first with priors OFF
      * optional `guided_turns_to_progress` / `guided_passed` / `guided_first_move` (real
        bench arm measurements); when omitted, deterministic replay estimates whether the top
        offered prior matches the episode's `progress_action`.
    """
    counts: dict[tuple[str, str], dict[str, Any]] = {}
    grade_counts: dict[str, int] = {
        EVIDENCE_GROUND_TRUTH: 0, EVIDENCE_JEV: 0, EVIDENCE_CORRELATION: 0,
    }
    for ep in training_episodes:
        rows = list(ep)
        lang = _run_language(rows)
        tracker = SituationTracker(repo_language=lang)
        current_stage: Any = object()
        for idx, row in enumerate(rows):
            if row.get("stage") != current_stage:
                current_stage = row.get("stage")
                tracker = SituationTracker(stage=row.get("stage") or SOLO_STAGE, repo_language=lang)
            if row.get("direction") == "tool_result":
                tracker.observe_result(row.get("tool_name"), row.get("content"), ok=row.get("ok"))
                continue
            action = canonical_tool_name(row.get("tool_name"))
            if not action:
                continue
            verdict = judge_step_progress(
                rows, idx, failed_before=tracker.failed_in_stage,
                jev=jev, timeout_ms=jev_timeout_ms,
            )
            grade_counts[verdict.evidence_grade] = grade_counts.get(verdict.evidence_grade, 0) + 1
            slot = counts.setdefault(
                (tracker.key, action),
                {"s": 0, "f": 0, "grade": verdict.evidence_grade},
            )
            slot["s" if verdict.credited else "f"] += 1
            slot["grade"] = stronger_grade(slot["grade"], verdict.evidence_grade)

    ranked_by_fp: dict[str, list[dict[str, Any]]] = {}
    sanity_gated_count = 0
    for (fp, action), stat in counts.items():
        s, f = stat["s"], stat["f"]
        if s + f < MIN_OBSERVATIONS:
            continue
        raw_w = usefulness(s, f)
        if raw_w < MIN_USEFULNESS:
            continue
        approved: Optional[bool] = None
        if sanity_gate:
            sv = sanity_gate_prior(
                fp, action, observations=s + f, successes=s, usefulness_score=raw_w,
                evidence_grade=stat["grade"], jev=jev, timeout_ms=jev_timeout_ms,
                allow_deterministic_graduation=(jev is None),
            )
            approved = sv.approved
            sanity_gated_count += 1
        ranked_by_fp.setdefault(fp, []).append({
            "action": action,
            "observations": s + f,
            "successes": s,
            "raw_weight": raw_w,
            "weight": raw_w * evidence_grade_weight(stat["grade"]),
            "evidence_grade": stat["grade"],
            "ordering_approved": approved,
            "share": round(100.0 * s / (s + f)),
        })
    for fp, items in ranked_by_fp.items():
        items.sort(key=lambda it: (-it["weight"], it["action"]))
        ranked_by_fp[fp] = items[:TOP_ACTIONS]

    off_turns: list[float] = []
    on_turns: list[float] = []
    off_pass = on_pass = 0
    off_match = on_match = 0
    priors_used = 0
    for ep in eval_episodes:
        fp = str(
            ep.get("fingerprint_key")
            or fingerprint(ep.get("stage"), ep.get("repo_language"), ep.get("last_error"),
                           wrote=bool(ep.get("wrote", False)))
        )
        ranked = ranked_by_fp.get(fp, [])
        if sanity_gate:
            ranked = [r for r in ranked if r.get("ordering_approved") is True]
        base_t = float(ep.get("baseline_turns_to_progress") or 0.0)
        base_p = bool(ep.get("baseline_passed", False))
        base_move = canonical_tool_name(ep.get("baseline_first_move"))
        progress_move = canonical_tool_name(ep.get("progress_action") or "")

        if "guided_turns_to_progress" in ep:
            guided_t = float(ep["guided_turns_to_progress"])
            guided_p = bool(ep.get("guided_passed", base_p))
            guided_move = canonical_tool_name(ep.get("guided_first_move") or (ranked[0]["action"] if ranked else base_move))
            if ranked:
                priors_used += 1
        elif ranked:
            priors_used += 1
            guided_move = ranked[0]["action"]
            if progress_move and guided_move == progress_move and base_move != progress_move:
                guided_t = max(1.0, base_t - 1.0)
            else:
                guided_t = base_t
            guided_p = base_p
        else:
            guided_move = base_move
            guided_t = base_t
            guided_p = base_p

        off_turns.append(base_t)
        on_turns.append(guided_t)
        off_pass += int(base_p)
        on_pass += int(guided_p)
        if progress_move:
            off_match += int(base_move == progress_move)
            on_match += int(guided_move == progress_move)

    n = len(eval_episodes)
    avg_off = round(sum(off_turns) / n, 3) if n else 0.0
    avg_on = round(sum(on_turns) / n, 3) if n else 0.0
    # Negative turns_to_progress_delta means FEWER turns needed (improvement); positive means regression.
    turns_delta = round(avg_on - avg_off, 3)
    pass_off = round(off_pass / n, 4) if n else 0.0
    pass_on = round(on_pass / n, 4) if n else 0.0
    pass_delta = round(pass_on - pass_off, 4)
    if turns_delta < 0 or (turns_delta == 0 and pass_delta > 0):
        verdict = "improved"
    elif turns_delta > 0 or pass_delta < 0:
        verdict = "regressed"
    else:
        verdict = "unchanged"
    return {
        "episodes_evaluated": n,
        "priors_available": sum(len(v) for v in ranked_by_fp.values()),
        "priors_used": priors_used,
        "sanity_gated_count": sanity_gated_count,
        "evidence_grades": grade_counts,
        "turns_to_progress_off": avg_off,
        "turns_to_progress_on": avg_on,
        "turns_to_progress_delta": turns_delta,
        "pass_rate_off": pass_off,
        "pass_rate_on": pass_on,
        "pass_rate_delta": pass_delta,
        "first_move_match_rate_off": round(off_match / n, 4) if n else 0.0,
        "first_move_match_rate_on": round(on_match / n, 4) if n else 0.0,
        "verdict": verdict,
    }


# ── The live loop's door into all of the above (never raises) ────────────────────────────────

def _org_for(connection: Any, project_id: Any) -> Optional[str]:
    if not str(project_id or "").strip():
        return None
    return connection.execute(text(
        """
        SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id
        WHERE p.id = :project_id
        """), {"project_id": str(project_id)}).scalar()


def runtime_ranked(settings: Any, *, project_id: Any, fingerprint_key: str,
                   db_engine: Any = None) -> list[dict]:
    """``ranked_actions`` for the running agent: resolves the org (the project's, else the
    install's default) and NEVER raises — an empty list is the only failure mode, because a
    prior must never fail the work it advises."""
    try:
        from ...db import begin

        with begin(db_engine) as connection:
            org = (_org_for(connection, project_id)
                   or str(getattr(settings, "default_org_id", "") or ""))
            if not org:
                return []
            return ranked_actions(connection, org_id=org, fingerprint_key=fingerprint_key)
    except Exception:  # noqa: BLE001 — see docstring
        LOG.debug("procedural priors unavailable", exc_info=True)
        return []
