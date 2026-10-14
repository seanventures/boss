"""WHEN THE ENGINE ITSELF CANNOT RUN, STOP — DO NOT LET THE AGENT WORK BLIND.

WHY THIS EXISTS
---------------
Build crun_f103065efc52457fb60c ran on `claude_agent_sdk`, whose Claude CLI sandbox could not
start. 19 of its 206 agent turns came back

    Sandbox is required but failed to initialize: Sandbox dependencies not available:
    socat not installed. Restart to retry.

in place of the command the agent asked for. So the agent never ran a shell command — no
install, no typecheck — and wrote code blind through design, implement and repair for fifteen
minutes, until the build ended `blocked` on the app's own typecheck. The operator read "the
app's checks failed"; the truth was "BOSS could not run its engine".

A failing test is the agent's work. A sandbox that will not start, a runtime binary that is not
installed, a login or account the provider refuses, a CLI that crashed: those are the HARNESS,
and nothing the agent does can fix them. This module is the one place that tells them apart:

  * `classify(text, engine=…, where=…)` — the generic words every engine's harness uses, plus
    the words each engine DECLARED for itself (harness_registry.FailurePattern, registered with
    the engine, the same idiom as its RuntimeBinary).
  * `check_turn` — every engine's turn stream goes through it: a tool result that is the harness
    answering in the command's place stops the session at its FIRST occurrence (HarnessFailure).
  * `note_engine_error` — an engine's own final error (its stderr, its session error) is read
    the same way before the engine raises it.
  * the latch — inside a governed build (`run_scope`), a harness failure is remembered for the
    run: every later agent session of that run refuses at once (`raise_if_tripped`, no spend),
    and the pipeline's stage boundary ends the build with the harness verdict
    (`BOSS could not run <engine>: <message>; fix: <fix>.`), never a repair round on blind code.
"""
from __future__ import annotations

import contextlib
import contextvars
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Optional

from .harness_registry import FailurePattern

#: Where the words were seen. A tool result can be the agent's own command printing them (a grep
#: of BOSS's source finds this very file), so it is believed only for patterns that may appear
#: there and only as the result's first line. `stream`: the engine CLI's own output, live.
#: `engine`: the engine's final error — every pattern, the refused login and account included.
WHERE = ("tool_result", "stream", "engine")

#: Every engine's harness, in the words harnesses use. An engine adds its own through the
#: registry (`register_harness(failure_patterns=…)`).
GENERIC_PATTERNS: tuple[FailurePattern, ...] = (
    FailurePattern("sandbox_unavailable",
                   r"failed to (?:initiali[sz]e|start|create) (?:the |its )?sandbox"
                   r"|sandbox (?:failed to (?:initiali[sz]e|start)|initiali[sz]ation failed)",
                   "install what {engine}'s sandbox needs where BOSS runs it, then retry"),
    FailurePattern("missing_runtime",
                   r"\bcli not found\b|package is not installed on this server"
                   r"|\bspawn \S+ enoent\b",
                   "install {engine} where BOSS runs it, then retry",
                   in_tool_results=False),
    FailurePattern("credit_refused",
                   r"credit balance is too low|insufficient[_ ]credits?|insufficient_quota"
                   r"|out of credits",
                   "add credit to the account {engine} bills, or connect another source in "
                   "Repo and models, then retry",
                   in_tool_results=False),
    FailurePattern("cli_crashed",
                   r"segmentation fault|core dumped|javascript heap out of memory"
                   r"|\bexited -(?:4|6|7|8|11)\b|thread '[^']*' panicked at",
                   "update {engine} (or give its host more memory) and retry; the engine's "
                   "own log has the crash",
                   in_tool_results=False),
)

#: "Sandbox dependencies not available: bubblewrap (bwrap) not installed / socat not installed"
_DEPENDENCIES = re.compile(r"dependencies (?:are )?not available:?\s*(?P<what>[^\n]*)", re.I)
_NOT_INSTALLED = re.compile(r"([\w.+-]+)(?:\s*\(([\w.+-]+)\))?\s+(?:is )?not installed", re.I)

#: How much of a tool result is read: the harness answers on the first line, in its own words.
_FIRST_LINE_CHARS = 400
#: A tool result that did NOT report an error must open with the words (an "Error: " prefix at
#: most) — a grep or cat that merely contains them starts with its own path.
_LEADING_SLACK = 12


@dataclass(frozen=True)
class HarnessVerdict:
    engine: str
    kind: str
    message: str
    fix: str
    where: str

    def sentence(self) -> str:
        return f"BOSS could not run {self.engine}: {self.message}; fix: {self.fix}."

    def as_dict(self) -> dict[str, str]:
        return {"engine": self.engine, "kind": self.kind, "message": self.message,
                "fix": self.fix, "where": self.where, "sentence": self.sentence()}


class HarnessFailure(RuntimeError):
    """The engine could not run — not the agent's work failing. Its words are the verdict."""

    def __init__(self, verdict: HarnessVerdict):
        super().__init__(verdict.sentence())
        self.verdict = verdict


#: The sentence, read back from a recorded error (a run's `error`, a step's last_error).
SENTENCE = re.compile(r"BOSS could not run (?P<engine>[\w.-]+): (?P<message>.+?); "
                      r"fix: (?P<fix>.+?)\.(?:\s|$)", re.S)


def parse_sentence(text: object) -> Optional[dict[str, str]]:
    found = SENTENCE.search(str(text or ""))
    return found.groupdict() if found else None


# ── Classifying ──────────────────────────────────────────────────────────────────────────────

def patterns_for(engine: Optional[str]) -> tuple[FailurePattern, ...]:
    """The engine's own declared words first (they are the more specific), then the generic."""
    from .harness_registry import failure_patterns

    try:
        own = failure_patterns(engine) if engine else ()
    except Exception:  # noqa: BLE001 — a registry that cannot load still has the generic words
        own = ()
    return tuple(own) + GENERIC_PATTERNS


def _clean(text: str) -> str:
    from ..identity.redaction import redact_text

    one = re.sub(r"\s+", " ", redact_text(str(text or ""))).strip()
    return one[:300]


def _missing_fix(text: str, engine: str) -> Optional[str]:
    """The fix for "dependencies not available: X not installed", naming how to install each
    binary the engine declared (harness_registry.RuntimeBinary)."""
    found = _DEPENDENCIES.search(text)
    names = _NOT_INSTALLED.findall(found.group("what")) if found else []
    if not names:
        return None
    from .harness_registry import runtime_requirements

    try:
        declared = runtime_requirements(engine)
    except Exception:  # noqa: BLE001
        declared = ()
    hints = []
    for name, alias in names:
        known = next((r for r in declared if r.binary in (name, alias) or r.package == name),
                     None)
        hints.append(f"{name} (`{known.install_hint()}`)" if known else name)
    return f"install {', '.join(hints)} where BOSS runs {engine}, then retry"


def _message(text: str, at: int) -> str:
    """The line the words are on, without the CLI's "Restart to retry." (BOSS retries)."""
    start = text.rfind("\n", 0, at) + 1
    end = text.find("\n", at)
    line = text[start:end if end >= 0 else len(text)]
    return _clean(re.split(r"\.?\s*restart to retry", line, flags=re.I)[0])


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, (list, tuple)):
        return "\n".join(_tool_result_text(c) for c in content)
    return str(content or "")


def classify(text: object, *, engine: Optional[str] = None, where: str = "engine",
             ok: Optional[bool] = None) -> Optional[HarnessVerdict]:
    """The harness failure `text` reports, or None when it is ordinary output (a failing test,
    a compiler error, a 404 — the agent's work, which the agent can fix).

    `where` ∈ WHERE. `ok`: for a tool result, whether the tool reported success."""
    raw = str(text or "")
    if not raw.strip():
        return None
    name = str(engine or "the engine")
    if where == "tool_result":
        line = next((ln for ln in raw.strip().splitlines() if ln.strip()), "")[:_FIRST_LINE_CHARS]
        scan = line
    else:
        scan = raw
    lowered = scan.lower()
    for pattern in patterns_for(engine):
        if where != "engine" and not pattern.in_tool_results:
            continue
        hit = re.search(pattern.pattern, scan, re.I)
        if hit is None:
            continue
        if where == "tool_result" and ok is not False:
            body = re.sub(r"^error:?\s*", "", lowered.lstrip())
            if body.find(hit.group(0).lower()) not in range(0, _LEADING_SLACK + 1):
                continue
        fix = _missing_fix(scan, name)
        return HarnessVerdict(name, "missing_runtime" if fix else pattern.kind,
                              _message(scan, hit.start()), fix or pattern.fix.format(engine=name),
                              where)
    # The binaries themselves, said without any of the words above.
    fix = _missing_fix(scan, name)
    if fix and (where != "tool_result" or ok is False):
        at = _DEPENDENCIES.search(scan).start()
        return HarnessVerdict(name, "missing_runtime", _message(scan, at), fix, where)
    if where == "engine":
        from .model_failures import is_login_rejected, provider_name

        if is_login_rejected(raw):
            who = provider_name(engine)
            whose = f"the {who}" if who != "the model" else name
            return HarnessVerdict(name, "auth_refused", _clean(raw),
                                  f"reconnect {whose} in Repo and models, then retry", where)
    return None


# ── The latch: one governed run ──────────────────────────────────────────────────────────────

_RUN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "boss_harness_failure_run", default=None)
_TRIPPED: dict[str, HarnessVerdict] = {}
_LOCK = threading.Lock()
_KEEP = 500


@contextlib.contextmanager
def run_scope(run_id: str) -> Iterator[None]:
    """Everything inside belongs to governed run `run_id`: a harness failure seen by any of its
    sessions (a review seat on a copied context included) stops the run, not just the session.
    A new execution of the run starts with the latch clear."""
    reset(run_id)
    token = _RUN.set(str(run_id))
    try:
        yield
    finally:
        _RUN.reset(token)


def current_run() -> Optional[str]:
    return _RUN.get()


def reset(run_id: Optional[str]) -> None:
    with _LOCK:
        _TRIPPED.pop(str(run_id or ""), None)


def trip(verdict: HarnessVerdict) -> HarnessFailure:
    """Remember the verdict for the current run (its first one stands) and return the error to
    raise. Outside a governed run nothing is remembered."""
    run_id = _RUN.get()
    if run_id:
        with _LOCK:
            _TRIPPED.setdefault(run_id, verdict)
            while len(_TRIPPED) > _KEEP:
                _TRIPPED.pop(next(iter(_TRIPPED)))
    return HarnessFailure(verdict)


def tripped(run_id: Optional[str] = None) -> Optional[HarnessVerdict]:
    key = str(run_id or _RUN.get() or "")
    if not key:
        return None
    with _LOCK:
        return _TRIPPED.get(key)


def raise_if_tripped() -> None:
    """At the start of every agent session: a run whose engine already could not run starts no
    new session (no spend, no blind work)."""
    verdict = tripped()
    if verdict is not None:
        raise HarnessFailure(verdict)


def failure_of(exc: Optional[BaseException], run_id: Optional[str] = None
               ) -> Optional[HarnessVerdict]:
    """The harness verdict behind a run's failure: the exception itself, or the run's latch (a
    stage that swallowed the HarnessFailure still failed because of it)."""
    seen = 0
    current = exc
    while isinstance(current, BaseException) and seen < 5:
        if isinstance(current, HarnessFailure):
            return current.verdict
        current = current.__cause__ or current.__context__
        seen += 1
    return tripped(run_id)


# ── What the engines call ────────────────────────────────────────────────────────────────────

def check_turn(turn: Mapping[str, Any], *, engine: Optional[str]) -> None:
    """Raise HarnessFailure when a captured turn is the harness answering in a tool's place."""
    if not isinstance(turn, Mapping) or turn.get("direction") != "tool_result":
        return
    ok = turn.get("ok")
    verdict = classify(_tool_result_text(turn.get("content")), engine=engine,
                       where="tool_result", ok=(None if ok is None else bool(ok)))
    if verdict is not None:
        raise trip(verdict)


def check_stream(text: object, *, engine: Optional[str]) -> None:
    """Raise HarnessFailure when the engine CLI's own output (stderr, live) says it cannot run."""
    verdict = classify(text, engine=engine, where="stream")
    if verdict is not None:
        raise trip(verdict)


def note_engine_error(text: object, *, engine: Optional[str]) -> Optional[HarnessVerdict]:
    """Read an engine's final error before it raises it: a harness failure trips the run's latch
    (so the build ends with the harness verdict, whichever stage swallowed the error). The
    engine raises its own error unchanged; never raises."""
    try:
        verdict = classify(text, engine=engine, where="engine")
    except Exception:  # noqa: BLE001 — a reading of an error never replaces the error
        return None
    if verdict is not None:
        trip(verdict)
    return verdict


__all__ = ["GENERIC_PATTERNS", "HarnessFailure", "HarnessVerdict", "check_stream", "check_turn",
           "classify", "current_run", "failure_of", "note_engine_error", "parse_sentence",
           "patterns_for", "raise_if_tripped", "reset", "run_scope", "trip", "tripped"]
