"""VAG #105 — verifier meta-eval: scoring (confusion matrix), metrics, the fail-closed
verifier-quality gate, and the run harness with an injected verifier. All hermetic — no DB,
no model calls (the real reviewer path is exercised with a fake agent)."""
import json
from pathlib import Path

from apps.api.app.services import verifier_meta_eval as vme

CORPUS_PATH = Path(__file__).resolve().parents[3] / "evals" / "verifier_meta" / "corpus.json"


# --- verdict normalization ---------------------------------------------------
def test_normalize_verdict_pass_and_fail_closed():
    for ok in ("survived", "PASS", "clean", "Approved", "ok", "consistent"):
        assert vme.normalize_verdict(ok) == vme.PASS
    for flag in ("refuted", "REJECTED", "issues", "drift", "error", "", "weird", None):
        assert vme.normalize_verdict(flag) == vme.FLAG   # unknown/empty -> flag (fail-closed)


# --- confusion matrix --------------------------------------------------------
def test_confusion_counts_all_four_cells():
    results = [
        ("bugged", "refuted"),   # tp — caught
        ("bugged", "survived"),  # fn — false pass (fooled)
        ("clean", "survived"),   # tn — correctly allowed
        ("clean", "refuted"),    # fp — false fail (over-strict)
        ("bug", "refuted"),      # tp (alias label)
    ]
    assert vme.confusion(results) == {"tp": 2, "fn": 1, "tn": 1, "fp": 1}


def test_metrics_rates():
    m = vme.metrics({"tp": 3, "fn": 1, "tn": 4, "fp": 2})
    assert m["bugged"] == 4 and m["clean"] == 6 and m["total"] == 10
    assert m["recall"] == 0.75          # 3/4 bugs caught
    assert m["false_pass_rate"] == 0.25  # 1/4 slipped through
    assert m["precision"] == 0.6         # 3/(3+2)
    assert m["fp_rate"] == round(2 / 6, 3)  # false-fail on clean
    assert m["accuracy"] == 0.7


def test_metrics_handles_empty_class():
    m = vme.metrics({"tp": 0, "fn": 0, "tn": 3, "fp": 0})
    assert m["recall"] is None and m["false_pass_rate"] is None   # no bugged items
    assert m["fp_rate"] == 0.0


# --- the gate ----------------------------------------------------------------
def _m(tp, fn, tn, fp):
    return vme.metrics({"tp": tp, "fn": fn, "tn": tn, "fp": fp})


def test_gate_passes_a_strong_verifier():
    g = vme.meta_eval_gate(_m(4, 0, 4, 0), min_recall=0.8, max_fp_rate=0.2)
    assert g["passed"] and g["reasons"] == []


def test_gate_fails_when_verifier_misses_bugs():
    g = vme.meta_eval_gate(_m(2, 2, 4, 0), min_recall=0.8, max_fp_rate=0.2)  # recall 0.5
    assert not g["passed"] and any("recall" in r for r in g["reasons"])


def test_gate_fails_when_verifier_over_flags_clean_code():
    g = vme.meta_eval_gate(_m(4, 0, 1, 3), min_recall=0.8, max_fp_rate=0.2)  # fp_rate 0.75
    assert not g["passed"] and any("fp_rate" in r for r in g["reasons"])


def test_gate_fails_on_single_class_corpus():
    g = vme.meta_eval_gate(_m(4, 0, 0, 0), min_recall=0.8, max_fp_rate=0.2)  # no clean items
    assert not g["passed"] and any("BOTH" in r for r in g["reasons"])


# --- run harness with injected verifiers -------------------------------------
CORPUS = {"items": [
    {"key": "b1", "label": "bugged"}, {"key": "b2", "label": "bugged"},
    {"key": "c1", "label": "clean"}, {"key": "c2", "label": "clean"},
]}


def test_run_with_perfect_verifier():
    def perfect(item):
        return "refuted" if item["label"] == "bugged" else "survived"
    rep = vme.run_meta_eval(CORPUS, perfect, min_recall=0.8, max_fp_rate=0.2)
    assert rep["metrics"]["recall"] == 1.0 and rep["metrics"]["fp_rate"] == 0.0
    assert rep["gate"]["passed"]
    assert all(i["correct"] for i in rep["items"])


def test_run_with_fooled_verifier_fails_gate():
    rep = vme.run_meta_eval(CORPUS, lambda i: "survived", min_recall=0.8, max_fp_rate=0.2)
    assert rep["metrics"]["recall"] == 0.0            # every bug slipped through
    assert not rep["gate"]["passed"]


def test_run_with_overstrict_verifier_fails_gate():
    rep = vme.run_meta_eval(CORPUS, lambda i: "refuted", min_recall=0.8, max_fp_rate=0.2)
    assert rep["metrics"]["fp_rate"] == 1.0           # every clean item flagged
    assert not rep["gate"]["passed"]


def test_run_records_verifier_error_as_flag():
    def boom(item):
        if item["key"] == "c1":
            raise RuntimeError("model exploded")
        return "refuted" if item["label"] == "bugged" else "survived"
    rep = vme.run_meta_eval(CORPUS, boom, min_recall=0.8, max_fp_rate=0.2)
    # c1 errored -> counted as a flag on a clean item -> one false-fail, eval still completes
    c1 = next(i for i in rep["items"] if i["key"] == "c1")
    assert c1["verdict"] == "error" and c1["prediction"] == vme.FLAG
    assert rep["metrics"]["fp"] == 1


# --- shipped corpus + dry-run stub -------------------------------------------
def test_shipped_corpus_is_balanced_and_dry_runs_clean():
    corpus = json.loads(CORPUS_PATH.read_text())
    labels = [i["label"] for i in corpus["items"]]
    assert labels.count("bugged") >= 1 and labels.count("clean") >= 1
    # every item must carry a stub_verdict so the offline dry-run is deterministic
    assert all("stub_verdict" in i for i in corpus["items"])
    rep = vme.run_meta_eval(corpus, vme.stub_verify_fn, min_recall=0.8, max_fp_rate=0.2)
    # the stub encodes a PERFECT reviewer -> the harness + gate wire up green end-to-end
    assert rep["gate"]["passed"]
    assert rep["metrics"]["recall"] == 1.0 and rep["metrics"]["fp_rate"] == 0.0


# --- real reviewer adapter (fake agent, no models) ---------------------------
def test_review_verify_fn_materializes_files_and_parses_verdict(monkeypatch):
    seen = {}

    class _FakeAgent:
        engine = "claude_code"

        def run_role(self, prompt, root, read_only=False, model=None):
            # the reviewer must receive the implementation on disk, read-only
            seen["read_only"] = read_only
            seen["solution"] = (Path(root) / "solution.py").read_text()
            return {
                "summary": (
                    "solution.py returns 1 for every input, violating criterion c "
                    "and producing an incorrect result.\nVERDICT: REFUTED"
                ),
                "cost_usd_micros": 0,
            }

    monkeypatch.setattr(vme, "make_code_agent", lambda settings, engine=None: _FakeAgent(),
                        raising=False)
    from types import SimpleNamespace
    settings = SimpleNamespace(code_review_engine="", code_agent_engine="claude_code")
    verify = vme.review_verify_fn(settings)
    verdict = verify({"title": "t", "acceptance_criteria": ["c"],
                      "implementation": {"solution.py": "def f(): return 1\n"}})
    assert verdict == "refuted"
    assert seen["read_only"] is True and "def f()" in seen["solution"]


def test_review_verify_fn_no_implementation_is_error(monkeypatch):
    monkeypatch.setattr(vme, "make_code_agent", lambda settings, engine=None: object(),
                        raising=False)
    from types import SimpleNamespace
    verify = vme.review_verify_fn(SimpleNamespace(code_review_engine="", code_agent_engine="x"))
    assert verify({"implementation": {}}) == "error"


# --- endpoint (dry-run, no models) -------------------------------------------
def test_endpoint_dry_run_returns_report(client):
    resp = client.post("/v1/verifier-meta-eval", json={"dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["gate"]["passed"] is True          # shipped corpus + perfect stub
    assert body["metrics"]["recall"] == 1.0
    assert "markdown" in body and "Verifier meta-eval" in body["markdown"]
