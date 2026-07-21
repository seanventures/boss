"""RB-1 — telemetry mining: templating + the statistical detector cascade (spec 12 §2/§8, spec 11 §2).

This is the front-end that makes big-data logs LLM-digestible **without** a model: Drain-style templating
collapses millions of raw lines into a few hundred **event templates + parameters** (a 10³–10⁴× cardinality
drop), and cheap statistical detectors flag the anomalies — spiking/new templates, error surges, metric
z-scores. Only the resulting handful of `AnomalySignal`s ever reach an LLM (for triage/RCA downstream). All
pure and model-free.

`mine()` is the read-only **Insight** output (RB-3): top templates, what's *new*, what's *spiking*, and any
error surge — a "what's happening / what changed" report from a batch of events, source-agnostic.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from .telemetry_source import LogEvent, MetricSeries

# --- templating (Drain-lite: mask variable tokens; the masked string is the template signature) -----
# Order matters — specific patterns before general ones, so an IP isn't half-eaten by the number mask.
_MASKS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<EMAIL>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<HASH>"),
    (re.compile(r"/[^\s\"']{2,}"), "<PATH>"),
    (re.compile(r"\"[^\"]*\""), "<STR>"),
    # number with an optional unit ('7ms', '500MB', '99%') BEFORE the bare-number mask, so the unit
    # doesn't block the word boundary and leave two near-identical templates.
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|us|ns|s|m|h|d|kb|mb|gb|tb|b|%)\b", re.IGNORECASE), "<NUM>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<NUM>"),
]


def template_of(message: str) -> str:
    """Mask the variable tokens of a log line; the result is its template signature. Deterministic, so
    'user 42 from 10.0.0.1' and 'user 7 from 10.0.0.9' collapse to 'user <NUM> from <IP>'."""
    t = message or ""
    for pattern, repl in _MASKS:
        t = pattern.sub(repl, t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class AnomalySignal:
    kind: str                       # "new_template" | "template_spike" | "error_surge" | "metric_spike"
    key: str                        # the template / metric name
    score: float                    # z-score or a ratio, higher = more anomalous
    count: int = 0
    sample: str = ""                # a representative raw line / value
    meta: dict[str, Any] = field(default_factory=dict)


def _zscores(counts: dict[str, int], baseline: dict[str, float]) -> dict[str, float]:
    """z of each template's current count vs its baseline mean/std across the baseline windows."""
    vals = list(baseline.values())
    mean = sum(vals) / len(vals) if vals else 0.0
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) if len(vals) > 1 else 0.0
    out = {}
    for tmpl, c in counts.items():
        base = baseline.get(tmpl, 0.0)
        if std > 0:
            out[tmpl] = (c - base) / std
        else:
            out[tmpl] = float("inf") if c > base else 0.0
    return out


def template_signals(events: list[LogEvent], baseline: Optional[dict[str, float]] = None, *,
                     z_threshold: float = 3.0, min_count: int = 3) -> list[AnomalySignal]:
    """New + spiking templates vs a baseline of per-template counts. With no baseline, every template
    with >= min_count is 'new' (cold start)."""
    counts: Counter = Counter()
    sample: dict[str, str] = {}
    has_error: dict[str, bool] = {}
    for e in events:
        t = template_of(e.message)
        counts[t] += 1
        sample.setdefault(t, e.message)
        has_error[t] = has_error.get(t, False) or e.level in ("error", "fatal", "critical")
    baseline = baseline or {}
    cold = not baseline           # no baseline yet -> "everything is new" is meaningless
    signals: list[AnomalySignal] = []
    zs = _zscores(dict(counts), baseline) if baseline else {}
    for tmpl, c in counts.items():
        if c < min_count:
            continue
        if tmpl not in baseline:
            # With a baseline, a genuinely-new template is a signal at any level. In cold start, only a
            # NOVEL ERROR template is worth surfacing — otherwise benign high-volume lines drown the report.
            if cold and not has_error[tmpl]:
                continue
            signals.append(AnomalySignal("new_template", tmpl, float("inf"), c, sample[tmpl]))
        elif zs.get(tmpl, 0.0) >= z_threshold:
            signals.append(AnomalySignal("template_spike", tmpl, round(zs[tmpl], 2), c, sample[tmpl]))
    signals.sort(key=lambda s: (s.score == float("inf"), s.score, s.count), reverse=True)
    return signals


def error_surge_signal(events: list[LogEvent], *, baseline_error_rate: Optional[float] = None,
                       ratio_threshold: float = 2.0, min_errors: int = 5) -> Optional[AnomalySignal]:
    """Surge in error/fatal-level events vs a baseline error rate (fraction). Cold start: fire if the
    error rate itself is high and there are enough errors."""
    if not events:
        return None
    errs = sum(1 for e in events if e.level in ("error", "fatal", "critical"))
    rate = errs / len(events)
    if errs < min_errors:
        return None
    if baseline_error_rate is None:
        fired = rate >= 0.10
        score = round(rate, 3)
    else:
        fired = baseline_error_rate > 0 and rate >= baseline_error_rate * ratio_threshold
        score = round(rate / baseline_error_rate, 2) if baseline_error_rate > 0 else float("inf")
    if not fired:
        return None
    return AnomalySignal("error_surge", "error_rate", score, errs,
                         meta={"error_rate": round(rate, 4), "baseline": baseline_error_rate})


def metric_signals(series: list[MetricSeries], *, z_threshold: float = 3.0) -> list[AnomalySignal]:
    """z-score anomalies within each metric series (a point far from the series mean)."""
    out: list[AnomalySignal] = []
    for s in series:
        vals = [p.value for p in s.points]
        if len(vals) < 4:
            continue
        mean = sum(vals) / len(vals)
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
        if std == 0:
            continue
        for p in s.points:
            z = (p.value - mean) / std
            if abs(z) >= z_threshold:
                out.append(AnomalySignal("metric_spike", s.name, round(abs(z), 2), meta={
                    "value": p.value, "mean": round(mean, 3), "ts": p.ts}))
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def mine(events: list[LogEvent], *, baseline: Optional[dict[str, float]] = None,
         metrics: Optional[list[MetricSeries]] = None, top_n: int = 10) -> dict[str, Any]:
    """The read-only Insight report (RB-3): distill a batch into top templates + anomaly signals. This is
    the whole 'point BOSS at your logs → what's happening / what changed' output, model-free."""
    counts: Counter = Counter()
    sample: dict[str, str] = {}
    for e in events:
        t = template_of(e.message)
        counts[t] += 1
        sample.setdefault(t, e.message)
    signals = template_signals(events, baseline)
    surge = error_surge_signal(events, baseline_error_rate=None)
    if surge:
        signals.append(surge)
    if metrics:
        signals.extend(metric_signals(metrics))
    return {
        "events": len(events),
        "distinct_templates": len(counts),
        "reduction_ratio": round(len(events) / len(counts), 1) if counts else None,
        "top_templates": [{"template": t, "count": c, "sample": sample[t]}
                          for t, c in counts.most_common(top_n)],
        "signals": [{"kind": s.kind, "key": s.key, "score": (None if s.score == float("inf") else s.score),
                     "count": s.count, "sample": s.sample, **({"meta": s.meta} if s.meta else {})}
                    for s in signals],
        "signal_count": len(signals),
    }
