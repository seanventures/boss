"""VAG #105 — meta-eval the verifier.

VAG's whole premise is that *verification* is the binding constraint: cheap models can
generate, but a weak verifier that waves bad code through (or cries wolf on good code)
collapses the pipeline's pass-rate. The refute panel (#104) added a *confidence number*
to the verifier — but a number isn't evidence the verifier actually works. This module
supplies the evidence: feed the review/verify stage a **labeled corpus** of known-bugged
and known-clean implementations and measure its discriminative power, then **gate on it**.

Relationship to the bake-off (`bakeoff.py`): the bake-off runs the FULL generate pipeline
per probe and counts one number — `false_pass` (a known-bad task whose gate passed). That
couples verifier quality to generator variance and only sees the false-*pass* axis. This
harness is the decoupled sibling: it runs the verifier on **fixed** implementations (no
generation, no repair loop — one review call per item) and computes the full confusion
matrix, adding the false-*fail* axis (good code wrongly rejected) the bake-off never sees.

The scoring/gate here is pure and model-free (unit-tested with injected verdicts); the real
verifier invocation (`review_verify_fn`) is opt-in and paid, exactly like the bake-off probes.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

# Module-level (patchable in tests) — the real reviewer reuses the exact prompt + parser the
# governed pipeline uses, so the meta-eval measures the production verifier, not a proxy.
from .code_agent import make_code_agent
from .governed_code_task_service import _parse_verdict, _review_prompt

logger = logging.getLogger(__name__)

# Prediction classes. Positive class = "bugged": a good verifier FLAGS it.
FLAG = "flag"   # verifier rejected/refuted the implementation -> predicts bugged
PASS = "pass"   # verifier passed/survived the implementation  -> predicts clean

# Raw verifier verdicts that mean "this looks fine". Anything else (refuted, rejected,
# issues, drift, error, unknown) is treated as a FLAG — fail-closed, matching the gate.
_PASS_VERDICTS = {"survived", "pass", "passed", "clean", "ok", "approve", "approved", "consistent"}
# Corpus labels that mean the implementation genuinely contains a defect.
_BUGGED_LABELS = {"bugged", "bug", "bad", "buggy", "fail", "defective"}


def normalize_verdict(raw: str) -> str:
    """Map a raw verifier verdict string to :data:`FLAG` / :data:`PASS`. Fail-closed: an
    empty/unknown verdict counts as a FLAG (the verifier expressing doubt is not a pass)."""
    return PASS if (raw or "").strip().lower() in _PASS_VERDICTS else FLAG


def _is_bugged(label: str) -> bool:
    return (label or "").strip().lower() in _BUGGED_LABELS


def confusion(results: list[tuple[str, str]]) -> dict[str, int]:
    """Build the confusion matrix from ``(label, raw_verdict)`` pairs. Positive = bugged.

    - tp: bugged implementation the verifier flagged   (caught the bug)
    - fn: bugged implementation the verifier passed     (**false pass** — verifier fooled)
    - tn: clean implementation the verifier passed      (correctly allowed)
    - fp: clean implementation the verifier flagged      (**false fail** — verifier over-strict)
    """
    cm = {"tp": 0, "fn": 0, "tn": 0, "fp": 0}
    for label, verdict in results:
        pred = normalize_verdict(verdict)
        if _is_bugged(label):
            cm["tp" if pred == FLAG else "fn"] += 1
        else:
            cm["fp" if pred == FLAG else "tn"] += 1
    return cm


def _ratio(num: int, den: int) -> Optional[float]:
    return round(num / den, 3) if den else None


def metrics(cm: dict[str, int]) -> dict[str, Any]:
    """Derive the rates a verifier's quality turns on from a confusion matrix."""
    tp, fn, tn, fp = cm["tp"], cm["fn"], cm["tn"], cm["fp"]
    bugged, clean = tp + fn, tn + fp
    total = bugged + clean
    return {
        **cm,
        "bugged": bugged, "clean": clean, "total": total,
        # recall = bug-detection rate: of all bugged items, how many were flagged.
        "recall": _ratio(tp, bugged),
        # precision = of everything flagged, how much was genuinely bugged.
        "precision": _ratio(tp, tp + fp),
        # false-fail rate on clean code: the axis the bake-off can't see.
        "fp_rate": _ratio(fp, clean),
        # false-pass rate: the #1 VAG guardrail (verifier fooled by a real bug).
        "false_pass_rate": _ratio(fn, bugged),
        "accuracy": _ratio(tp + tn, total),
    }


def meta_eval_gate(m: dict[str, Any], *, min_recall: float, max_fp_rate: float) -> dict[str, Any]:
    """Fail-closed gate on the verifier itself: it must catch enough bugs (recall floor)
    without over-flagging clean code (false-fail ceiling), and the corpus must exercise both
    classes or the numbers are meaningless."""
    reasons: list[str] = []
    if m["bugged"] == 0 or m["clean"] == 0:
        reasons.append("corpus must contain BOTH bugged and clean items to be meaningful")
    if m["recall"] is not None and m["recall"] < min_recall:
        reasons.append(
            f"recall {m['recall']} < min {min_recall} — verifier misses bugs "
            f"(false-pass rate {m['false_pass_rate']})"
        )
    if m["fp_rate"] is not None and m["fp_rate"] > max_fp_rate:
        reasons.append(
            f"fp_rate {m['fp_rate']} > max {max_fp_rate} — verifier over-flags clean code"
        )
    return {"passed": not reasons, "reasons": reasons,
            "thresholds": {"min_recall": min_recall, "max_fp_rate": max_fp_rate}}


def run_meta_eval(
    corpus: dict[str, Any],
    verify_fn: Callable[[dict[str, Any]], str],
    *,
    min_recall: float,
    max_fp_rate: float,
) -> dict[str, Any]:
    """Run ``verify_fn`` over every corpus item, score the confusion matrix, and gate.

    ``verify_fn(item) -> raw_verdict`` is injectable: a fake in tests, the corpus's
    per-item ``stub_verdict`` for an offline dry-run, or :func:`review_verify_fn` for the
    real (paid) verifier. A verify_fn that raises is recorded as an ``error`` verdict, which
    normalizes to a FLAG (fail-closed) rather than aborting the whole eval.
    """
    items = corpus.get("items", [])
    per_item: list[dict[str, Any]] = []
    results: list[tuple[str, str]] = []
    for item in items:
        try:
            verdict = str(verify_fn(item))
        except Exception as exc:  # noqa: BLE001 — one bad item shouldn't sink the eval
            logger.warning("verifier errored on item %s: %s", item.get("key"), exc)
            verdict = "error"
        label = item.get("label", "")
        pred = normalize_verdict(verdict)
        # "correct" = the verifier's prediction matched the ground-truth label.
        correct = (pred == FLAG) == _is_bugged(label)
        per_item.append({
            "key": item.get("key"), "label": label, "bug_class": item.get("bug_class"),
            "verdict": verdict, "prediction": pred, "correct": correct,
        })
        results.append((label, verdict))
    m = metrics(confusion(results))
    gate = meta_eval_gate(m, min_recall=min_recall, max_fp_rate=max_fp_rate)
    return {"metrics": m, "gate": gate, "items": per_item, "corpus_size": len(items)}


# --- real verifier invocation (opt-in, paid) ---------------------------------
def review_verify_fn(
    settings, *, engine: Optional[str] = None, model: Optional[str] = None,
) -> Callable[[dict[str, Any]], str]:
    """Build a ``verify_fn`` that runs BOSS's *real* adversarial reviewer on a fixed
    implementation and returns its raw verdict — reusing the exact ``_review_prompt`` +
    ``_parse_verdict`` the governed pipeline uses, so the meta-eval measures the production
    verifier, not a proxy. Each call is one read-only ``run_role`` model call (paid); the
    implementation files are materialized into a throwaway temp dir the reviewer reads.

    Returns a closure so the (expensive) agent construction happens once per eval, not per item.
    """
    review_engine = engine or settings.code_review_engine or settings.code_agent_engine
    agent = make_code_agent(settings, engine=review_engine)

    def _verify(item: dict[str, Any]) -> str:
        impl = item.get("implementation") or {}
        if not impl:
            return "error"  # nothing to review -> fail-closed
        with tempfile.TemporaryDirectory(prefix="vmeta_") as root:
            for rel, content in impl.items():
                dest = Path(root) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            rr = agent.run_role(
                _review_prompt(item.get("title", ""), item.get("acceptance_criteria", [])),
                root, read_only=True, model=model,
            )
        return _parse_verdict(rr.get("summary", ""))

    return _verify


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable meta-eval report (the numbers a verifier's quality turns on)."""
    m, g = report["metrics"], report["gate"]
    th = g["thresholds"]
    out = [
        "# Verifier meta-eval (VAG #105)",
        "",
        f"**Gate: {'PASS ✅' if g['passed'] else 'FAIL ❌'}** "
        f"— min_recall={th['min_recall']}, max_fp_rate={th['max_fp_rate']}",
    ]
    if g["reasons"]:
        out += ["", *[f"- {r}" for r in g["reasons"]]]
    out += [
        "",
        "| metric | value | reads as |",
        "|---|---|---|",
        f"| corpus | {m['bugged']} bugged / {m['clean']} clean | |",
        f"| recall | {m['recall']} | fraction of bugs the verifier caught |",
        f"| false-pass rate | {m['false_pass_rate']} | bugs it waved through (the #1 guardrail) |",
        f"| fp rate | {m['fp_rate']} | clean code it wrongly rejected |",
        f"| precision | {m['precision']} | of what it flagged, how much was real |",
        f"| accuracy | {m['accuracy']} | |",
        f"| confusion | tp={m['tp']} fn={m['fn']} tn={m['tn']} fp={m['fp']} | |",
        "",
        "| item | label | bug_class | verdict | correct |",
        "|---|---|---|---|---|",
    ]
    for i in report["items"]:
        out.append(
            f"| {i['key']} | {i['label']} | {i.get('bug_class') or ''} "
            f"| {i['verdict']} | {'✓' if i['correct'] else '✗'} |"
        )
    return "\n".join(out)


def load_corpus(path: Optional[str] = None) -> dict[str, Any]:
    """Load the labeled corpus JSON. Defaults to the repo's evals/verifier_meta/corpus.json."""
    import json
    if path is None:
        # this file: apps/api/app/services/verifier_meta_eval.py -> repo root is parents[4]
        path = str(Path(__file__).resolve().parents[4] / "evals" / "verifier_meta" / "corpus.json")
    return json.loads(Path(path).read_text())


def stub_verify_fn(item: dict[str, Any]) -> str:
    """Offline dry-run verifier: replays each item's declared ``stub_verdict`` (what a
    perfect reviewer would say). Exercises the whole harness — scoring, gate, report — with
    zero model calls, for tests, demos, and CI (which can't run the paid path)."""
    return str(item.get("stub_verdict", "survived"))
