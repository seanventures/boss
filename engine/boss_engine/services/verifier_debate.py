"""VAG #108 — bounded generator↔verifier debate on contested cases.

The refute panel (#104) returns a verdict *and a confidence*. When confidence is decisive we act on
it. But a **contested** case — the panel split near its quorum — is exactly where a single fail-closed
default is most likely to be *wrong*: reject it and you've false-failed good code; pass it and you've
let a real bug through. Instead of guessing, resolve it the way humans do — a short, **bounded**
argument: the generator defends its code, the verifier presses its objection, for a capped number of
rounds, ending the moment one side concedes (Ng's multi-agent-debate pattern, quorum-cheap).

Resolution rule:
  - generator **concedes** → the bug is real → ``refuted`` (high confidence).
  - verifier **withdraws** → the objection didn't hold → ``survived`` (high confidence).
  - neither, after ``max_rounds`` → a **judge** decides if provided, else **fail-closed** (``refuted``,
    low confidence) — an unresolved dispute is not a pass.

The orchestration + resolution here are pure (tested with injected debaters); ``agent_debaters``
wires the roles to real models (opt-in, paid). "Bounded" is the whole point: max_rounds caps the cost.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# defend_fn(claim, transcript)    -> {"argument": str, "concede": bool}
# challenge_fn(claim, transcript) -> {"argument": str, "withdraw": bool}
# judge_fn(claim, transcript)     -> {"verdict": "survived"|"refuted", "rationale": str}
DebateFn = Callable[[str, list[dict[str, Any]]], dict[str, Any]]

_SURVIVED, _REFUTED = "survived", "refuted"


def is_contested(confidence: Optional[float], *, low: float = 0.34, high: float = 0.66) -> bool:
    """A verdict is contested when the panel's confidence sits in the mushy middle — neither a clear
    survive nor a clear refute. Outside the band, trust the panel and skip the (costly) debate."""
    return confidence is not None and low < confidence < high


def _resolve(verdict: str, reason: str, confidence: float, rounds: int,
             transcript: list[dict[str, Any]]) -> dict[str, Any]:
    return {"verdict": verdict, "reason": reason, "confidence": round(confidence, 3),
            "rounds_used": rounds, "transcript": transcript}


def run_debate(
    claim: str, defend_fn: DebateFn, challenge_fn: DebateFn,
    judge_fn: Optional[DebateFn] = None, *, max_rounds: int = 2,
) -> dict[str, Any]:
    """Run a bounded debate over a contested ``claim`` (the verifier's objection). Alternates
    generator-defense and verifier-challenge, stopping the instant one side yields. Deterministic
    given deterministic debaters."""
    transcript: list[dict[str, Any]] = []
    for r in range(1, max_rounds + 1):
        defense = defend_fn(claim, transcript) or {}
        transcript.append({"role": "generator", "round": r, **defense})
        if defense.get("concede"):
            return _resolve(_REFUTED, "generator_conceded", 0.95, r, transcript)

        challenge = challenge_fn(claim, transcript) or {}
        transcript.append({"role": "verifier", "round": r, **challenge})
        if challenge.get("withdraw"):
            return _resolve(_SURVIVED, "verifier_withdrew", 0.9, r, transcript)

    # Unresolved after the bound: a judge breaks the tie, else fail closed (a live dispute ≠ a pass).
    if judge_fn is not None:
        j = judge_fn(claim, transcript) or {}
        verdict = _SURVIVED if j.get("verdict") == _SURVIVED else _REFUTED
        transcript.append({"role": "judge", **j})
        return _resolve(verdict, "judge", 0.6, max_rounds, transcript)
    return _resolve(_REFUTED, "unresolved_fail_closed", 0.5, max_rounds, transcript)


def resolve_contested(
    panel_verdict: str, panel_confidence: Optional[float],
    debate: Callable[[], dict[str, Any]], *, low: float = 0.34, high: float = 0.66,
) -> dict[str, Any]:
    """Panel → maybe-debate glue: if the panel verdict is contested, run ``debate()`` and take its
    result; otherwise keep the panel verdict untouched. ``debate`` is a thunk so the (paid) debate is
    only constructed when actually needed."""
    if not is_contested(panel_confidence, low=low, high=high):
        return {"verdict": panel_verdict, "confidence": panel_confidence,
                "reason": "panel_decisive", "debated": False}
    result = debate()
    return {**result, "debated": True, "panel_verdict": panel_verdict,
            "panel_confidence": panel_confidence}


def agent_debaters(
    settings, builder_engine: Optional[str] = None, verifier_engine: Optional[str] = None,
    *, model: Optional[str] = None,
) -> tuple[DebateFn, DebateFn, DebateFn]:
    """Wire the three debate roles to real agents (opt-in, paid). The generator agent defends, a
    DIFFERENT-engine verifier agent challenges (independence, see #110), and the verifier engine also
    judges. Returns (defend_fn, challenge_fn, judge_fn); each turn is one read-only model call.
    Prompts ask for a strict last line so the concede/withdraw/verdict signal is machine-readable.
    """
    from .code_agent import make_code_agent

    b_engine = builder_engine or settings.code_agent_engine
    v_engine = verifier_engine or settings.code_review_engine or settings.code_agent_engine
    gen = make_code_agent(settings, engine=b_engine)
    ver = make_code_agent(settings, engine=v_engine)

    def _transcript_text(transcript: list[dict[str, Any]]) -> str:
        return "\n".join(f"{t.get('role')}: {t.get('argument', '')}" for t in transcript)

    def defend(claim: str, transcript: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (f"You wrote code the reviewer objects to:\n{claim}\n\nDebate so far:\n"
                  f"{_transcript_text(transcript)}\n\nDefend your code OR concede if the objection is "
                  f"correct. End with exactly 'CONCEDE: YES' or 'CONCEDE: NO'.")
        out = gen.run_role(prompt, ".", read_only=True, model=model).get("summary", "")
        return {"argument": out, "concede": "concede: yes" in out.lower()}

    def challenge(claim: str, transcript: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (f"You objected to code:\n{claim}\n\nDebate so far:\n{_transcript_text(transcript)}\n\n"
                  f"Press your objection with a concrete failing case OR withdraw if the defense holds. "
                  f"End with exactly 'WITHDRAW: YES' or 'WITHDRAW: NO'.")
        out = ver.run_role(prompt, ".", read_only=True, model=model).get("summary", "")
        return {"argument": out, "withdraw": "withdraw: yes" in out.lower()}

    def judge(claim: str, transcript: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (f"Judge this unresolved debate about:\n{claim}\n\n{_transcript_text(transcript)}\n\n"
                  f"Who is right? End with exactly 'VERDICT: SURVIVED' or 'VERDICT: REFUTED'.")
        out = ver.run_role(prompt, ".", read_only=True, model=model).get("summary", "")
        return {"verdict": _SURVIVED if "verdict: survived" in out.lower() else _REFUTED,
                "rationale": out}

    return defend, challenge, judge
