"""VAG #106 — mutation gate: deterministic AST mutant generation, scoring, the fail-closed
test-strength gate, and the end-to-end runner. Engine + scoring are pure; the runner shells out
to `python3 -m unittest` in a temp dir (no DB, no models)."""
from apps.api.app.services import mutation_gate as mg

# a small function with exactly the mutable sites we reason about below
LAST_N = (
    "def last_n(xs, n):\n"
    "    if n <= 0:\n"
    "        return []\n"
    "    return xs[-n:]\n"
)


# --- engine ------------------------------------------------------------------
def test_count_and_generate_are_deterministic():
    n = mg.count_sites(LAST_N)
    a = mg.generate_mutants(LAST_N)
    b = mg.generate_mutants(LAST_N)
    assert len(a) == n and a == b            # same source -> identical mutants
    ops = {m["operator"].split(":")[0] for m in a}
    assert {"const", "compare", "return"} <= ops
    for m in a:
        assert m["source"] != LAST_N          # each mutant actually differs
        compile(m["source"], "<m>", "exec")   # and is still valid Python


def test_operators_cover_binop_and_boolop():
    src = "def f(a, b):\n    return a + b and a > 0\n"
    ops = {m["operator"].split(":")[0] for m in mg.generate_mutants(src)}
    assert "binop" in ops and "boolop" in ops and "compare" in ops


def test_max_mutants_caps():
    assert len(mg.generate_mutants(LAST_N, max_mutants=2)) == 2


# --- scoring + gate ----------------------------------------------------------
def test_mutation_score():
    res = [{"status": "killed"}, {"status": "killed"}, {"status": "survived"}, {"status": "skipped"}]
    assert mg.mutation_score(res) == round(2 / 3, 3)     # skipped excluded from the denominator
    assert mg.mutation_score([]) is None


def test_gate_passes_strong_fails_weak_and_broken_baseline():
    assert mg.mutation_gate_decision(0.9, min_score=0.8, baseline_passed=True)["passed"]
    weak = mg.mutation_gate_decision(0.5, min_score=0.8, baseline_passed=True)
    assert not weak["passed"] and any("mutation score" in r for r in weak["reasons"])
    broken = mg.mutation_gate_decision(1.0, min_score=0.8, baseline_passed=False)
    assert not broken["passed"] and any("baseline" in r for r in broken["reasons"])
    none = mg.mutation_gate_decision(None, min_score=0.8, baseline_passed=True)
    assert not none["passed"] and any("no mutants" in r for r in none["reasons"])


def test_property_test_requirement():
    assert mg.has_property_tests("from hypothesis import given\n@given(x=...)")
    assert not mg.has_property_tests("import unittest")
    blocked = mg.mutation_gate_decision(0.9, min_score=0.8, baseline_passed=True,
                                        require_property_tests=True, property_tests_present=False)
    assert not blocked["passed"] and any("property" in r for r in blocked["reasons"])


# --- end-to-end runner (subprocess) ------------------------------------------
_CMD = ["python3", "-m", "unittest", "discover", "-v"]

# complete suite: pins n=1 boundary, n=0, and the normal path -> kills every mutant
_STRONG = (
    "import unittest\n"
    "from solution import last_n\n"
    "class T(unittest.TestCase):\n"
    "    def test_basic(self): self.assertEqual(last_n([1,2,3,4],2),[3,4])\n"
    "    def test_one(self): self.assertEqual(last_n([1,2],1),[2])\n"
    "    def test_zero(self): self.assertEqual(last_n([1,2,3],0),[])\n"
    "    def test_big(self): self.assertEqual(last_n([1,2],5),[1,2])\n"
)
# barely tests anything -> lets most mutants survive
_WEAK = (
    "import unittest\n"
    "from solution import last_n\n"
    "class T(unittest.TestCase):\n"
    "    def test_smoke(self): self.assertIsInstance(last_n([1,2,3],2), list)\n"
)


def test_run_gate_strong_suite_kills_all():
    rep = mg.run_mutation_gate({"solution.py": LAST_N, "test_solution.py": _STRONG},
                               "solution.py", _CMD, min_score=0.8, timeout=20)
    assert rep["baseline_passed"] is True
    assert rep["score"] == 1.0 and rep["gate"]["passed"] is True
    assert rep["survivors"] == []


def test_run_gate_weak_suite_fails_with_survivors():
    rep = mg.run_mutation_gate({"solution.py": LAST_N, "test_solution.py": _WEAK},
                               "solution.py", _CMD, min_score=0.8, timeout=20)
    assert rep["baseline_passed"] is True
    assert rep["score"] < 0.8 and rep["gate"]["passed"] is False
    assert rep["survivors"]                                   # names the uncaught mutants
    assert all("operator" in s and "lineno" in s for s in rep["survivors"])


def test_run_gate_blocks_when_baseline_red():
    broken_test = ("import unittest\nfrom solution import last_n\n"
                   "class T(unittest.TestCase):\n"
                   "    def test_wrong(self): self.assertEqual(last_n([1,2,3],2),[1,2,3])\n")
    rep = mg.run_mutation_gate({"solution.py": LAST_N, "test_solution.py": broken_test},
                               "solution.py", _CMD, min_score=0.8, timeout=20)
    assert rep["baseline_passed"] is False and rep["gate"]["passed"] is False
    assert rep["mutants_run"] == 0                            # never mutation-tests a red baseline


def test_run_gate_unknown_target_file():
    rep = mg.run_mutation_gate({"solution.py": LAST_N}, "missing.py", _CMD, min_score=0.8)
    assert rep["gate"]["passed"] is False and "error" in rep


# --- endpoint ----------------------------------------------------------------
def test_endpoint_runs_and_validates(client):
    resp = client.post("/v1/mutation-gate", json={
        "files": {"solution.py": LAST_N, "test_solution.py": _WEAK},
        "target_file": "solution.py", "min_score": 0.8, "timeout": 20})
    assert resp.status_code == 200
    body = resp.json()
    assert body["gate"]["passed"] is False and body["survivors"]
    # missing files/target -> 422
    assert client.post("/v1/mutation-gate", json={"files": {}}).status_code == 422
