"""(a) The classifier — task → required tier, always with a confidence, always an answer.

A cascade, cheapest first (docs/product/MODEL_ROUTER.md, "(a) The classifier"):

  cache ▸ Jev (when set up, within its timeout) ▸ rules ▸ cheap model (opt-in, only when the
  rules are unsure, within its timeout)

The rules are deterministic and never skipped: they are the answer when Jev is absent (most
installs) or slow, and the floor under the cheap model. They read the
task through the SAME boilerplate-stripping, whole-word matching `infer_risk_tier` uses, so
routing and rigor cannot disagree about what a task is.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from .policy import ClassifierConfig
from .tiers import BALANCED, FAST, FRONTIER, TIERS

#: Judgment words: work where a wrong call is expensive even when the change is small.
_JUDGMENT = (
    "architecture", "architect", "concurrency", "race condition", "deadlock", "distributed",
    "consensus", "threat model", "cryptograph", "performance-critical", "cross-cutting",
    "security audit", "security review", "data loss", "rewrite the", "redesign the",
)
_BIG_BUILD = ("full-stack", "full stack", "end to end", "end-to-end", "multi-service",
              "frontend and backend", "front end and back end", "microservice")
_QUESTION = re.compile(r"^\s*(what|why|how|where|when|which|who|is|are|can|does|do|should)\b",
                       re.IGNORECASE)
_THINK = ("plan", "strategy", "compare", "trade-off", "tradeoff", "analy", "design", "evaluate",
          "recommend", "review", "prioriti")


@dataclass(frozen=True)
class TaskSignal:
    """What the classifier may read about one piece of work. Names, sizes and the task's own
    words — never secrets (the request is built by the caller from the brief/message)."""

    kind: str                    # build_stage | workflow_step | chat | decision | panel | research | agent
    step: str
    text: str = ""
    task_kind: Optional[str] = None
    repo_files: Optional[int] = None
    #: An explicit rigor tier from the task (0..3), when the caller has one.
    risk_tier: Optional[int] = None
    #: A build's spec, so the rules read it exactly as infer_risk_tier does.
    spec: Optional[dict] = None

    @property
    def group(self) -> str:
        """Steps that share one task share one classification (a build classifies once)."""
        return "build" if self.kind == "build_stage" else self.kind

    def fingerprint(self) -> str:
        source = self.text or ""
        if self.spec:
            from ..pipeline_profiles import _task_haystack

            source = _task_haystack(self.spec)
        words = re.sub(r"\s+", " ", source[:4000].strip().lower())
        size = min(5, len(self.text or "") // 400)
        repo = "?" if self.repo_files is None else str(min(4, (self.repo_files or 0) // 500))
        step = self.step if self.group != "build" else ""
        raw = f"{self.group}|{step}|{self.task_kind or ''}|{self.risk_tier}|{size}|{repo}|{words}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Classification:
    tier: str
    confidence: float
    classifier: str              # rules | jev | model | cache:<inner>
    reason: str
    #: The rigor tier the same judgement implies (infer_risk_tier's 0..3), for builds.
    risk_tier: Optional[int] = None
    sensitive: bool = False
    cached: bool = False
    latency_ms: float = 0.0
    #: Why a better classifier did not answer ("Jev paused", "Jev timed out"…), if one didn't.
    fallback_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "confidence": round(self.confidence, 3),
                "classifier": self.classifier, "reason": self.reason,
                "risk_tier": self.risk_tier, "sensitive": self.sensitive,
                "cached": self.cached, "fallback_note": self.fallback_note}


# ── Rules ──────────────────────────────────────────────────────────────────────────────────────

def _has(words: tuple[str, ...], haystack: str) -> list[str]:
    return [w for w in words if re.search(r"\b" + re.escape(w), haystack)]


#: Words that make work sensitive for ROUTING: rigor's own list (pipeline_profiles) plus the
#: ones a model choice must not get wrong. Kept apart from rigor's list so Fixed mode's pipeline
#: shape is unchanged.
ROUTER_SENSITIVE = ("oauth", "sso", "saml", "2fa", "mfa", "prod database", "production database",
                    "prod db", "security", "pii", "gdpr", "private key", "ssh key", "rbac")


def sensitive_in(signal: "TaskSignal") -> list[str]:
    """The sensitive words in the task's own words (BOSS boilerplate stripped, whole words)."""
    from ..pipeline_profiles import SENSITIVE_MARKERS, _matches, _task_haystack

    spec = dict(signal.spec or {}) or {"title": "", "instructions": signal.text or ""}
    haystack = _task_haystack(spec)
    return [m for m in (*SENSITIVE_MARKERS, *ROUTER_SENSITIVE) if _matches(m, haystack)]


def minimum_tier(signal: "TaskSignal", markers: Optional[list[str]] = None) -> Optional[str]:
    """The lowest tier sensitive work may get, whoever classifies it: Frontier for work that
    changes things, Balanced for a chat answer. Jev or a model may raise it, never lower it."""
    markers = sensitive_in(signal) if markers is None else markers
    if not markers:
        return None
    return BALANCED if signal.kind == "chat" else FRONTIER


def rules_classify(signal: TaskSignal) -> Classification:
    """The deterministic answer. Pure, a few microseconds, and the floor under everything."""
    text = signal.text or ""
    lowered = text.lower()
    kind, step = signal.kind, signal.step

    if step == "panel_facilitator":
        return Classification(FRONTIER, 0.9, "rules", "facilitation is judgment")
    if step == "panel_member":
        return Classification(BALANCED, 0.75, "rules", "a panel seat argues one view")
    if step == "research_synthesis" or kind == "research":
        return Classification(BALANCED, 0.75, "rules", "research synthesis")
    if kind == "decision":
        return Classification(FAST, 0.7, "rules", "a decision from fixed options")
    if kind == "chat":
        markers = sensitive_in(signal)
        judgment = _has(_JUDGMENT, lowered)
        if judgment:
            return Classification(FRONTIER, 0.6, "rules", f"judgment: {judgment[0]}", None,
                                  bool(markers))
        if markers:
            return Classification(BALANCED, 0.7, "rules", f"sensitive: {markers[0]}", None, True)
        if len(text) > 1500:
            return Classification(BALANCED, 0.7, "rules", "a long request")
        think = _has(_THINK, lowered)
        if think:
            return Classification(BALANCED, 0.65, "rules", f"asks for {think[0]}")
        if len(text) < 400:
            confidence = 0.85 if _QUESTION.search(text) else 0.7
            return Classification(FAST, confidence, "rules", "a short question")
        return Classification(BALANCED, 0.55, "rules", "a medium request")

    # Build stages, agent and workflow steps: the task's own words, read as rigor reads them.
    from ..pipeline_profiles import (
        _is_incremental_change, _is_trivial_change, _task_haystack, sensitive_markers_in,
    )

    spec = dict(signal.spec or {})
    if not spec:
        spec = {"title": "", "instructions": text}
    haystack = _task_haystack(spec)
    markers = sensitive_markers_in(spec) or sensitive_in(signal)
    explicit = signal.risk_tier if signal.risk_tier is not None else spec.get("risk_tier")
    if markers:
        # SENSITIVE WORDS WIN OVER A STATED RISK LEVEL: `risk_tier: 0` on an OAuth change is not
        # a reason to give it Haiku.
        conf = 0.8 if len(markers) > 1 or len(haystack) > 200 else 0.65
        return Classification(FRONTIER, conf, "rules", f"sensitive: {markers[0]}", 3, True)
    if isinstance(explicit, int) and 0 <= explicit <= 3:
        tier = {0: FAST, 1: BALANCED, 2: BALANCED, 3: FRONTIER}[explicit]
        return Classification(tier, 0.9, "rules", f"the task set its risk level ({explicit} of 3)",
                              explicit, explicit >= 3)
    if len(haystack.strip()) < 12:
        return Classification(BALANCED, 0.4, "rules", "too little to judge", 2)
    judgment = _has(_JUDGMENT, haystack)
    if _is_trivial_change(haystack, spec):
        tier, conf, why, rigor = FAST, (0.9 if len(haystack) < 240 else 0.8), \
            "a small presentation change", 0
    elif judgment:
        return Classification(FRONTIER, 0.6, "rules", f"judgment: {judgment[0]}", 2)
    elif _is_incremental_change(haystack, spec):
        tier, conf, why, rigor = BALANCED, 0.7, "a bounded change to existing code", 1
        if len(haystack) < 160 and _has(("typo", "rename", "copy", "wording", "label"),
                                        haystack):
            tier, conf, why = FAST, 0.7, "a one-line change"
    else:
        big = _has(_BIG_BUILD, haystack)
        tier, conf, rigor = BALANCED, (0.8 if big else 0.7), 2
        why = "a new full-stack build" if big else "new behaviour"
    if tier == FAST and signal.repo_files and signal.repo_files > 2000:
        tier, conf, why = BALANCED, 0.6, why + " in a large repo"
    if kind in ("agent", "workflow_step") and step in ("docs", "writing") and tier == BALANCED \
            and len(haystack) < 1200:
        tier, conf, why = FAST, 0.65, "routine writing"
    return Classification(tier, conf, "rules", why, rigor, False)


# ── Model-backed classifiers (Jev, a cheap model) ──────────────────────────────────────────────

def tier_question(config: ClassifierConfig):
    from ..typed_decisions import Question

    return Question(key="route_tier",
                    instructions="Which model tier does this piece of work need?",
                    options={t: config.criteria.get(t, "") for t in TIERS})


def _from_batch(batch: Any, signal: TaskSignal, name: str) -> Optional[Classification]:
    decision = (getattr(batch, "decisions", None) or {}).get("route_tier")
    if decision is None or decision.answer not in TIERS:
        return None
    rigor, sensitive = None, False
    tier_d = batch.decisions.get("tier")
    sens_d = batch.decisions.get("sensitive")
    if tier_d is not None and tier_d.answer is not None:
        sensitive = bool(sens_d and sens_d.answer == "yes")
        rigor = 3 if sensitive else min(int(tier_d.answer), 2)
    return Classification(decision.answer, float(decision.confidence or 0.5), name,
                          (decision.reason or f"{name} judged it")[:200], rigor, sensitive)


def _bounded(fn: Callable[[], Any], timeout_s: float) -> Any:
    """`fn()` within `timeout_s`, or FutureTimeout. A thread of its own per call: a hung
    classifier must never queue the next route behind it (a shared pool of four did)."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as error:  # noqa: BLE001 — handed to the caller
            box["error"] = error
        finally:
            done.set()

    threading.Thread(target=run, name="route-classify", daemon=True).start()
    if not done.wait(timeout_s):
        raise FutureTimeout()
    if "error" in box:
        raise box["error"]
    return box["value"]


#: After Jev times out, it is skipped for this long: one slow minute must not make every stage
#: of every build wait the full timeout again (a negative cache on the backend itself).
JEV_COOLDOWN_S = 60.0
#: A classification made because a better classifier timed out is kept this long, not an hour.
FALLBACK_TTL_S = 120.0
_JEV_COOL_UNTIL = [0.0]
#: The same for the cheap-model classifier: after it times out or fails, the rules answer for
#: this long without asking it — a down Fast source must not cost every route its timeout.
MODEL_COOLDOWN_S = 60.0
_MODEL_COOL_UNTIL = [0.0]


# ── The cascade ────────────────────────────────────────────────────────────────────────────────

class Classifier:
    """The cascade plus its fingerprint cache. One per process (`CLASSIFIER`); thread-safe."""

    def __init__(self, size: int = 4096, ttl_s: float = 3600.0):
        self._cache: "OrderedDict[str, tuple[float, Classification]]" = OrderedDict()
        self._lock = threading.Lock()
        self._size, self._ttl = size, ttl_s

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            _JEV_COOL_UNTIL[0] = 0.0
            _MODEL_COOL_UNTIL[0] = 0.0

    def _get(self, key: str) -> Optional[Classification]:
        with self._lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            if time.monotonic() > hit[0]:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return hit[1]

    def _put(self, key: str, value: Classification, ttl: Optional[float] = None) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic() + (ttl or self._ttl), value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)

    def classify(self, signal: TaskSignal, config: ClassifierConfig, *,
                 jev: Optional[Any] = None, jev_state: str = "absent",
                 model: Optional[Callable[[], Any]] = None) -> Classification:
        """`jev` is a Decider (or None); `jev_state` is ready | absent. `model` builds
        the cheap-model Decider on demand, so nothing is constructed when it is not asked."""
        started = time.perf_counter()
        key = f"{config.backend}|{signal.fingerprint()}"
        hit = self._get(key)
        if hit is not None:
            return replace(hit, cached=True,
                           latency_ms=(time.perf_counter() - started) * 1000)
        result: Optional[Classification] = None
        note = ""
        short = False                       # cache briefly: a better answer may come soon
        questions = None
        if config.backend in ("auto", "jev"):
            if jev_state == "ready" and jev is not None \
                    and time.monotonic() < _JEV_COOL_UNTIL[0]:
                note, short = "Jev slow — rules for a minute", True
            elif jev_state == "ready" and jev is not None:
                questions = self._questions(signal, config)
                try:
                    batch = _bounded(lambda: jev.decide(self._context(signal), questions),
                                     config.jev_timeout_ms / 1000.0)
                    result = _from_batch(batch, signal, "jev")
                    if result is None:
                        note = "Jev gave no usable answer"
                except FutureTimeout:
                    note, short = "Jev timed out", True
                    _JEV_COOL_UNTIL[0] = time.monotonic() + JEV_COOLDOWN_S
                except Exception:  # noqa: BLE001 — the fast backend is optional
                    note, short = "Jev failed", True
            elif config.backend == "jev":
                note = "Jev not set up"
        if result is None:
            rules = rules_classify(signal)
            result = replace(rules, classifier="rules", fallback_note=note)
            wants_model = config.backend == "model" or (
                config.backend == "auto" and config.model_fallback)
            cooling = time.monotonic() < _MODEL_COOL_UNTIL[0]
            if wants_model and model is not None and rules.confidence < config.model_below \
                    and cooling:
                result = replace(result, fallback_note=(note + "; " if note else "")
                                 + "cheap model slow or failing — rules for a minute")
                short = True
            elif wants_model and model is not None and rules.confidence < config.model_below:
                questions = questions or self._questions(signal, config)
                try:
                    decider = model()
                    batch = _bounded(lambda: decider.decide(self._context(signal), questions),
                                     config.model_timeout_ms / 1000.0)
                    judged = _from_batch(batch, signal, "model")
                    if judged is not None:
                        result = replace(judged, risk_tier=judged.risk_tier
                                         if judged.risk_tier is not None else rules.risk_tier,
                                         fallback_note=note)
                except FutureTimeout:
                    result = replace(result, fallback_note=(note + "; " if note else "")
                                     + "cheap model timed out")
                    short = True
                    _MODEL_COOL_UNTIL[0] = time.monotonic() + MODEL_COOLDOWN_S
                except Exception:  # noqa: BLE001 — the rules' answer stands
                    result = replace(result, fallback_note=(note + "; " if note else "")
                                     + "cheap model failed")
                    short = True
                    _MODEL_COOL_UNTIL[0] = time.monotonic() + MODEL_COOLDOWN_S
        # SENSITIVE WORK HAS A MINIMUM no classifier may lower (Decision 17).
        markers = sensitive_in(signal)
        floor = minimum_tier(signal, markers)
        if floor is not None and TIERS.index(result.tier) < TIERS.index(floor):
            result = replace(result, tier=floor, sensitive=True,
                             reason=f"{result.reason}; sensitive ({markers[0]}) keeps it "
                                    f"≥ {floor.capitalize()}")
        elif markers:
            result = replace(result, sensitive=True)
        result = replace(result, latency_ms=(time.perf_counter() - started) * 1000)
        self._put(key, result, ttl=FALLBACK_TTL_S if short else None)
        return result

    @staticmethod
    def _questions(signal: TaskSignal, config: ClassifierConfig) -> list:
        questions = [tier_question(config)]
        if signal.group == "build":
            # One call answers routing AND rigor: model_risk_tier's own questions ride along.
            from ..pipeline_profiles import _rigor_questions

            questions.extend(_rigor_questions())
        return questions

    @staticmethod
    def _context(signal: TaskSignal) -> str:
        from ..pipeline_profiles import _task_haystack

        words = _task_haystack(signal.spec) if signal.spec else (signal.text or "")
        where = f"(Step: {signal.step}; kind: {signal.kind}"
        if signal.task_kind:
            where += f"; task kind: {signal.task_kind}"
        if signal.repo_files is not None:
            where += f"; repo files: {signal.repo_files}"
        return f"{words[:8000]}\n\n{where})"


CLASSIFIER = Classifier()


def jev_state(settings: Any) -> str:
    """ready | absent. Ready when TypeSafe is the chosen decision backend and a key is held
    (typed_decisions.typesafe_decides — the environment variable or one stored through Setup's
    vault), and every call goes through the governed typed-decision path
    (store.GovernedJevDecider), never a bare client. BOSSAPP-579 lifted the leftover router
    hold: there is no "held" state any more."""
    try:
        from .. import typed_decisions

        return "ready" if typed_decisions.typesafe_decides(settings) else "absent"
    except Exception:  # noqa: BLE001
        return "absent"
