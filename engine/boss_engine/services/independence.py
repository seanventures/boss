"""VAG #110 — generator/verifier independence contract.

Everything the VAG verifier work assumes — the refute panel (#104), the meta-eval (#105), the
gate's `reviewer_independent` flag — is only meaningful if the verifier is actually **independent**
of the generator. A model reviewing its own output shares its blind spots: the same training gives
the same wrong intuitions, so it rubber-stamps the very bugs it would have written. Independence is
currently a *convention* (`BOSS_CODE_REVIEW_ENGINE` defaults to a different engine); this makes it a
**contract** — measured, and enforced fail-closed.

Two axes:
  - **Diverse family** — the reviewer must not be the same underlying model *family* as the builder
    (claude≠gpt≠gemini≠deepseek≠…), because correlated errors come from shared training, not just a
    shared vendor. Separation is ranked: same-model < same-family < same-provider < independent.
  - **No shared trace** — the reviewer must judge the *code*, not the builder's reasoning. If the
    builder's chain-of-thought leaks into the review context, the reviewer inherits its rationalizations.

`assess_independence` scores a builder against its verifiers; `independence_gate` blocks a run whose
verifiers don't meet the required separation.
"""
from __future__ import annotations

from typing import Any, Optional

from .model_rate_limiter import provider_of

# Ordered family keywords — first match wins (checked against "<engine> <model>"). Families capture
# shared training lineage, which is what makes errors correlate.
_FAMILY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("claude", ("claude", "anthropic")),
    ("gemini", ("gemini", "google")),
    ("deepseek", ("deepseek",)),
    ("qwen", ("qwen",)),
    ("llama", ("llama",)),
    ("mistral", ("mistral", "mixtral")),
    ("grok", ("grok", "xai")),
    ("gpt", ("gpt", "openai", "o1-", "o3-", "o4-", "davinci")),
]

# separation rank (higher = more independent)
_SAME_MODEL, _SAME_FAMILY, _SAME_PROVIDER, _INDEPENDENT = 0, 1, 2, 3
_LEVEL_NAME = {0: "same_model", 1: "same_family", 2: "same_provider", 3: "independent"}
# required-level -> minimum acceptable rank
_REQUIRED_RANK = {"off": 0, "model": 1, "family": 2, "provider": 3}


def family_of(engine: Optional[str], model: Optional[str] = None) -> str:
    """The model family (lineage) for an (engine, model). Derived from the model string first, then
    the engine, falling back to the provider lane when nothing else is recognizable."""
    hay = f"{engine or ''} {model or ''}".lower()
    for fam, kws in _FAMILY_KEYWORDS:
        if any(k in hay for k in kws):
            return fam
    prov = provider_of(engine)
    if prov == "anthropic":
        return "claude"
    if prov in ("openai", "azure"):
        return "gpt"
    return prov  # unknown lineage -> use the provider lane as a coarse family


def _descriptor(engine: Optional[str], model: Optional[str]) -> dict[str, str]:
    return {"engine": (engine or ""), "model": (model or ""),
            "provider": provider_of(engine), "family": family_of(engine, model)}


def separation(builder: dict[str, str], verifier: dict[str, str]) -> int:
    """Rank how separated a verifier is from the builder (higher = more independent)."""
    if builder["family"] != verifier["family"]:
        return _INDEPENDENT if builder["provider"] != verifier["provider"] else _SAME_PROVIDER
    # same family:
    same_model = bool(builder["model"]) and builder["model"].lower() == verifier["model"].lower()
    # empty models on the same family/engine are treated as the same model (can't prove otherwise)
    if same_model or (not builder["model"] and builder["engine"] == verifier["engine"]):
        return _SAME_MODEL
    return _SAME_FAMILY


def assess_independence(
    builder: tuple[Optional[str], Optional[str]],
    verifiers: list[tuple[Optional[str], Optional[str]]],
) -> dict[str, Any]:
    """Assess a builder (engine, model) against its verifier (engine, model) list. The contract is
    only as strong as its weakest verifier, so the overall rank is the minimum across verifiers."""
    b = _descriptor(*builder)
    per = []
    for v_engine, v_model in verifiers:
        v = _descriptor(v_engine, v_model)
        rank = separation(b, v)
        per.append({**v, "rank": rank, "level": _LEVEL_NAME[rank]})
    min_rank = min((p["rank"] for p in per), default=_INDEPENDENT)
    return {"builder": b, "verifiers": per,
            "min_rank": min_rank, "min_level": _LEVEL_NAME[min_rank],
            "n_verifiers": len(per)}


def independence_gate(verdict: dict[str, Any], *, required_level: str = "family") -> dict[str, Any]:
    """Fail-closed contract: every verifier must be separated from the builder by at least
    ``required_level`` (off | model | family | provider). Names each offending verifier."""
    req = _REQUIRED_RANK.get((required_level or "family").lower())
    if req is None:
        req = _REQUIRED_RANK["family"]
        required_level = "family"
    reasons: list[str] = []
    if not verdict["verifiers"]:
        reasons.append("no verifiers configured — the builder is judging its own work")
    for v in verdict["verifiers"]:
        if v["rank"] < req:
            reasons.append(
                f"verifier {v['engine'] or v['family']} is {v['level']} vs builder "
                f"{verdict['builder']['family']} — need ≥ {required_level}")
    return {"passed": not reasons, "reasons": reasons,
            "required_level": required_level, "min_level": verdict["min_level"]}


def shares_trace(review_context: str, builder_trace: str, *, min_run: int = 40) -> bool:
    """Detect whether the builder's reasoning leaked into the reviewer's context (a shared-trace
    violation). True if a substantial contiguous slice of the builder trace appears verbatim in the
    review context — the reviewer should see the CODE, never the builder's rationalizations."""
    trace = " ".join((builder_trace or "").split())
    ctx = " ".join((review_context or "").split())
    if len(trace) < min_run or not ctx:
        return False
    # scan windows of the trace for a verbatim run in the review context
    step = max(1, min_run // 2)
    for i in range(0, len(trace) - min_run + 1, step):
        if trace[i:i + min_run] in ctx:
            return True
    return False


def independent_reviewer(connection: Any, settings: Any, project_id: str,
                         build_engine: str, build_model: Optional[str] = None
                         ) -> Optional[str]:
    """A CONNECTED engine in a different model family than the builder, or None (BOSSAPP-960).

    `BOSS_CODE_REVIEW_ENGINE` empty has always meant "the builder reviews itself", and nothing
    ever called this module's gate on a real build — the docstring above says it plainly:
    independence was a convention. This is the default's other half: when the operator has NOT
    pinned a reviewer and the install actually holds a credential for an engine in another
    family, the review lane uses that engine. One engine connected (the common install) changes
    nothing: there is no independent reviewer to pick, and the builder keeps the seat.

    Only engines this project can actually run (setup_service.connected_engines) and that the
    harness registry can drive are considered, `independence_required_level: off` switches the
    behaviour off, and any failure answers None — review must happen even when this lookup
    cannot."""
    try:
        level = str(getattr(settings, "independence_required_level", "family") or "").lower()
        if level == "off":
            return None
        from .harness_registry import UnknownHarness, get_harness
        from .setup_service import connected_engines

        build_family = family_of(build_engine, build_model)
        connected = connected_engines(connection, settings, project_id)
        for engine in sorted(connected):
            if engine == (build_engine or "").lower():
                continue
            if family_of(engine) == build_family:
                continue
            try:
                if get_harness(engine).kind == "conformance":
                    continue
            except UnknownHarness:
                continue  # connected for chat, not a coding harness
            return engine
        return None
    except Exception:  # noqa: BLE001 — a failed lookup must not take the review seat with it
        return None


def configured_contract(settings, *, required_level: Optional[str] = None) -> dict[str, Any]:
    """Audit BOSS's CURRENT configuration: is the reviewer independent of the builder? Reads the
    builder vs review engines/models from settings so an operator can check their setup at a glance."""
    # BOTH SIDES USED TO READ THE SAME KNOB. builder and verifier were both
    # (engine, code_agent_strong_model), so this audit could never report model diversity even
    # when an operator had deliberately arranged it — the two model values were the same string
    # by construction, and the only difference it could ever see was the harness.
    builder = (settings.code_agent_engine,
               (getattr(settings, "code_agent_model", "")
                or getattr(settings, "code_agent_strong_model", "")) or None)
    review_engine = (getattr(settings, "code_review_engine", "") or settings.code_agent_engine)
    verifier = (review_engine,
                (getattr(settings, "code_review_model", "")
                 or getattr(settings, "code_agent_strong_model", "")) or None)
    verdict = assess_independence(builder, [verifier])
    level = required_level or getattr(settings, "independence_required_level", "family")
    return {**verdict, "gate": independence_gate(verdict, required_level=level)}
