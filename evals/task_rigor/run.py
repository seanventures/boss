"""Score the production keyword rules. Model rows are not measured and no model is called."""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from boss_engine.services.intent_router import asked_only_to_look
from boss_engine.services.pipeline_profiles import (
    PROFILES,
    SENSITIVE_MARKERS,
    _matches,
    _task_haystack,
    infer_risk_tier,
)

COLUMNS = ("accuracy", "false_negatives", "p95_latency_ms", "cost_per_call")
NOT_MEASURED = "not measured"
MODEL_ROWS = ("jev", "jev+claude", "claude")


def _spec(row: dict) -> dict:
    return {
        "title": row.get("title", ""),
        "instructions": row.get("instructions", ""),
        "acceptance_criteria": row.get("acceptance_criteria", []) or [],
        "repo": row.get("repo", ""),
    }


def _open_profile() -> dict:
    matches = [p for p in PROFILES.values() if p.get("tier_floor") == 0 and p.get("tier_cap") == 3]
    if len(matches) != 1:
        raise SystemExit("expected one profile with tier_floor 0 and tier_cap 3")
    return matches[0]


def _timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000.0


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = math.ceil(0.95 * len(ordered)) - 1
    return ordered[min(max(rank, 0), len(ordered) - 1)]


def _keyword_sensitive(spec: dict) -> bool:
    haystack = _task_haystack(spec)
    return any(_matches(marker, haystack) for marker in SENSITIVE_MARKERS)


def _score_sensitivity(rows: list[dict]) -> tuple[int, int, int, float]:
    correct = 0
    false_negatives = 0
    samples: list[float] = []
    for row in rows:
        spec = _spec(row)
        predicted, elapsed = _timed(lambda spec=spec: _keyword_sensitive(spec))
        samples.append(elapsed)
        label = bool(row["label"])
        if label and not predicted:
            false_negatives += 1
        if predicted == label:
            correct += 1
    return correct, false_negatives, len(rows), _p95(samples)


def _score_rigor(rows: list[dict]) -> tuple[int, int, int, float]:
    profile = _open_profile()
    correct = 0
    false_negatives = 0
    samples: list[float] = []
    for row in rows:
        spec = _spec(row)
        predicted, elapsed = _timed(lambda spec=spec: infer_risk_tier(spec, profile))
        samples.append(elapsed)
        label = int(row["label"])
        if label == 3 and predicted < 3:
            false_negatives += 1
        if predicted == label:
            correct += 1
    return correct, false_negatives, len(rows), _p95(samples)


def _score_look(rows: list[dict]) -> tuple[int, int, int, float]:
    correct = 0
    false_negatives = 0
    samples: list[float] = []
    for row in rows:
        text = row["text"]
        predicted_look, elapsed = _timed(lambda text=text: asked_only_to_look(text))
        samples.append(elapsed)
        label_look = row["label"] == "look"
        if label_look and not predicted_look:
            false_negatives += 1
        if predicted_look == label_look:
            correct += 1
    return correct, false_negatives, len(rows), _p95(samples)


def _section(name: str, correct: int, false_negatives: int, total: int, p95_ms: float) -> str:
    accuracy = f"{correct / total:.6f}"
    lines = [
        f"family {name}",
        "method | " + " | ".join(COLUMNS),
        f"keyword | {accuracy} | {false_negatives} | {p95_ms:.6f} | 0",
    ]
    for method in MODEL_ROWS:
        lines.append(f"{method} | " + " | ".join(NOT_MEASURED for _ in COLUMNS))
    return "\n".join(lines)


def main() -> None:
    corpus = json.loads((Path(__file__).parent / "corpus.json").read_text())
    sections = [
        _section("sensitivity", *_score_sensitivity(corpus["sensitivity"])),
        _section("rigor", *_score_rigor(corpus["rigor"])),
        _section("look", *_score_look(corpus["look"])),
    ]
    print("\n".join(sections))


if __name__ == "__main__":
    main()
