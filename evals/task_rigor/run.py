"""Task-rigor eval: today's keyword tiering against model-judged tiering, on labelled tasks.

    PYTHONPATH=engine python3 evals/task_rigor/run.py --models haiku,sonnet,opus
    PYTHONPATH=engine python3 evals/task_rigor/run.py --models typesafe,typesafe+opus

"typesafe" is TypeSafe's Jev alone (needs TYPESAFE_API_KEY and a funded account);
"typesafe+opus" re-asks Opus whenever Jev's confidence is below --below.

The tier decides how much of the pipeline runs (tier 0 one session, tier 3 the full design chain
plus a human plan-approval gate), so the two errors cost different things and are reported
apart: UNDER-tiering skips rigor the work needed; OVER-tiering pays for rigor it did not.
Tiers are compared RAW (profile clamp 0..3), because the clamp is policy, not judgement.

Every model call goes through the install's Claude subscription. cost_usd is the CLI's own
API-equivalent figure, not money billed to a subscription.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from boss_engine.config import get_settings  # noqa: E402
from boss_engine.services.pipeline_profiles import infer_risk_tier, model_risk_tier  # noqa: E402
from boss_engine.services.typed_decisions import (  # noqa: E402
    ClaudeDecider, SecondOpinionDecider, TypeSafeDecider,
)

HERE = pathlib.Path(__file__).resolve().parent
OPEN = {"tier_floor": 0, "tier_cap": 3}


def _spec(item: dict) -> dict:
    return {k: item[k] for k in ("title", "instructions", "acceptance_criteria",
                                 "extended_existing_app") if k in item}


def _keyword(item: dict) -> dict:
    started = time.monotonic()
    tier = infer_risk_tier(_spec(item), OPEN)
    return {"tier": tier, "sensitive": tier == 3, "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000), "cost_usd": 0.0}


def _decider(settings, arm: str, below: float):
    if arm == "typesafe":
        return TypeSafeDecider(settings)
    if arm.startswith("typesafe+"):
        return SecondOpinionDecider(TypeSafeDecider(settings),
                                    ClaudeDecider(settings, model=arm.split("+", 1)[1]), below)
    return ClaudeDecider(settings, model=arm)


def _model(decider, item: dict) -> dict:
    try:
        r = model_risk_tier(_spec(item), OPEN, decider)
    except Exception as exc:  # noqa: BLE001 — a failed call is a result, not a crash
        return {"tier": None, "sensitive": None, "ok": False, "error": str(exc)[:200]}
    b = r["batch"]
    tier_d = b.decisions.get("tier")
    return {"tier": r["raw_tier"] if r["ok"] else None, "sensitive": r["sensitive"],
            "ok": r["ok"], "confidence": tier_d.confidence if tier_d else None,
            "reason": tier_d.reason if tier_d else "",
            "latency_ms": b.latency_ms, "cost_usd": (b.cost_usd_micros or 0) / 1e6}


def _score(items: list[dict], preds: list[dict]) -> dict:
    rows = [(i, p) for i, p in zip(items, preds)]
    ok = [(i, p) for i, p in rows if p["ok"] and p["tier"] is not None]
    n = len(ok)

    def rate(xs):
        return round(100 * len(xs) / n, 1) if n else None

    sens_true = [(i, p) for i, p in ok if i["sensitive"]]
    sens_false = [(i, p) for i, p in ok if not i["sensitive"]]
    lat = [p["latency_ms"] for _, p in ok]
    return {
        "answered": f"{n}/{len(rows)}",
        "tier_exact_%": rate([1 for i, p in ok if p["tier"] == i["tier"]]),
        "under_tier_%": rate([1 for i, p in ok if p["tier"] < i["tier"]]),
        "over_tier_%": rate([1 for i, p in ok if p["tier"] > i["tier"]]),
        "sensitive_missed": f"{sum(1 for i, p in sens_true if not p['sensitive'])}/{len(sens_true)}",
        "sensitive_false_alarm": f"{sum(1 for i, p in sens_false if p['sensitive'])}/{len(sens_false)}",
        "latency_p50_ms": int(statistics.median(lat)) if lat else None,
        "cost_usd_total": round(sum(p.get("cost_usd", 0) for _, p in ok), 4),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="haiku,sonnet")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--below", type=float, default=0.7)
    args = ap.parse_args()

    items = json.loads((HERE / "tasks.json").read_text())
    if args.limit:
        items = items[: args.limit]
    settings = get_settings()
    arms = {"keyword": [_keyword(i) for i in items]}
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        decider = _decider(settings, model, args.below)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            arms[model] = list(pool.map(lambda i: _model(decider, i), items))
        print(f"{model}: done", file=sys.stderr)

    clear = [k for k, i in enumerate(items) if not i.get("ambiguous")]
    report = {
        "n_items": len(items), "n_unambiguous": len(clear),
        "all": {arm: _score(items, p) for arm, p in arms.items()},
        "unambiguous": {arm: _score([items[k] for k in clear], [p[k] for k in clear])
                        for arm, p in arms.items()},
    }
    per_item = [
        {"id": i["id"], "title": i["title"][:90], "label_tier": i["tier"],
         "label_sensitive": i["sensitive"], "ambiguous": i.get("ambiguous", False),
         **{arm: {"tier": p[k]["tier"], "sensitive": p[k]["sensitive"],
                  **({"reason": p[k].get("reason", "")} if arm != "keyword" else {})}
            for arm, p in arms.items()}}
        for k, i in enumerate(items)
    ]
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps({"report": report, "items": per_item}, indent=1))
    print(json.dumps(report, indent=1))
    print(f"per-item results: {out / f'{stamp}.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
