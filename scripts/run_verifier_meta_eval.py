#!/usr/bin/env python3
"""VAG #105 driver — run the verifier meta-eval and print/save the report.

    python3 scripts/run_verifier_meta_eval.py                 # offline dry-run (stub verdicts, $0)
    python3 scripts/run_verifier_meta_eval.py --real          # REAL reviewer (one paid call/item)
    python3 scripts/run_verifier_meta_eval.py --real --engine native --model gpt-4o
    python3 scripts/run_verifier_meta_eval.py --save          # also write evals/verifier_meta/results/

Dry-run is the default so an accidental invocation never spends model budget. --real runs the
production adversarial reviewer over each fixed implementation and reports the true TP/FP numbers.
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "apps" / "api"))

from app.config import get_settings  # noqa: E402
from app.services import verifier_meta_eval as vme  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="run the real (paid) reviewer")
    ap.add_argument("--engine", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--corpus", default=None, help="path to a corpus.json (default: shipped)")
    ap.add_argument("--min-recall", type=float, default=None)
    ap.add_argument("--max-fp-rate", type=float, default=None)
    ap.add_argument("--save", action="store_true", help="write report.json + report.md")
    args = ap.parse_args()

    settings = get_settings()
    corpus = vme.load_corpus(args.corpus)
    min_recall = args.min_recall if args.min_recall is not None else settings.verifier_meta_min_recall
    max_fp_rate = args.max_fp_rate if args.max_fp_rate is not None else settings.verifier_meta_max_fp_rate

    if args.real:
        verify_fn = vme.review_verify_fn(settings, engine=args.engine, model=args.model)
        mode = "REAL reviewer"
    else:
        verify_fn = vme.stub_verify_fn
        mode = "dry-run (stub verdicts, $0)"

    print(f"# meta-eval: {mode} — {len(corpus.get('items', []))} items\n", file=sys.stderr)
    report = vme.run_meta_eval(corpus, verify_fn, min_recall=min_recall, max_fp_rate=max_fp_rate)
    print(vme.render_markdown(report))

    if args.save:
        out = REPO / "evals" / "verifier_meta" / "results"
        out.mkdir(parents=True, exist_ok=True)
        tag = "real" if args.real else "dryrun"
        (out / f"report_{tag}.json").write_text(json.dumps(report, indent=2))
        (out / f"report_{tag}.md").write_text(vme.render_markdown(report))
        print(f"\nsaved -> {out}/report_{tag}.{{json,md}}", file=sys.stderr)

    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
