"""RED IS NOT NORMAL (BOSSAPP-1429).

PRs routinely said "19-23 failures, same as main"; a security guard stayed red because its red was
accepted; the worker suite never ran anywhere (two of its files did not even import). Now:
  * each suite has ONE known-failures baseline (tests/known-failures/<suite>.txt), every line with
    a ticket, an owner, a date and a reason; a gate fails on a failure not in it, and on a listed
    test that now passes (the list only shrinks);
  * a red security guard (tests/tiers.yaml `security:`) fails every gate, and can never be listed;
  * the worker suite collects and runs in CI.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import known_failures as kf  # noqa: E402


def _entry(test: str, line: int = 1) -> kf.Entry:
    return kf.Entry(test, "BOSSAPP-1", "engine", "2026-11-23", "it fails today", line)


def _baselines(*tests: str) -> dict[str, dict[str, kf.Entry]]:
    out: dict[str, dict[str, kf.Entry]] = {}
    for n, t in enumerate(tests, 1):
        out.setdefault(kf.suite_of(t) or "engine", {})[t] = _entry(t, n)
    return out


# ── the baselines as checked in ───────────────────────────────────────────────────────────────

def test_every_baseline_is_well_formed_and_holds_no_security_guard():
    loaded = kf.load_all()
    assert set(loaded) == set(kf.SUITES)
    security = kf.security_files()
    for suite, entries in loaded.items():
        for e in entries.values():
            assert e.file not in security, e.id
            assert kf.suite_of(e.id) == suite


def test_every_security_guard_exists():
    security = kf.security_files()
    assert len(security) >= 10, "the security guards are listed in tests/tiers.yaml `security:`"
    for f in security:
        assert (ROOT / f).is_file(), f"tests/tiers.yaml security: {f} does not exist"


def test_there_is_one_list_per_suite_and_no_old_second_list():
    assert not (ROOT / "tests" / "known-failures.txt").exists()
    assert not (ROOT / "tests" / "web-quarantine.txt").exists()
    tier = (ROOT / "scripts" / "test-tier.sh").read_text(encoding="utf-8")
    assert "scripts/known_failures.py judge" in tier
    assert "known-failures.txt" not in tier


def test_ci_runs_the_worker_suite_and_checks_the_baselines():
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    p0 = ci["jobs"]["p0"]
    suites = p0["env"]["TEST_TIER_SUITES"].split(",")
    assert {"engine", "api", "worker", "scripts"} <= set(suites)
    runs = " ".join(str(step.get("run") or "") for step in p0["steps"])
    assert "scripts/known_failures.py check" in runs
    assert "scripts/test-tier.sh P0" in runs


def test_the_worker_suite_collects():
    """Two worker files imported scripts that are not in this tree; the whole directory then
    failed to collect, so nothing in it could run."""
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "apps/worker/tests"], cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={"PYTHONPATH": f"{ROOT}:{ROOT / 'engine'}", "PATH": "/usr/bin:/bin"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


# ── the judge ─────────────────────────────────────────────────────────────────────────────────

A = "engine/tests/test_a.py"
B = "engine/tests/test_b.py"
G = "engine/tests/test_guard.py"


def test_a_failure_not_listed_is_new():
    problems, known = kf.judge(ran=[A], failed=[f"{A}::test_x"], baselines={}, security=set())
    assert problems == [f"NEW FAILURE: {A}::test_x (not in tests/known-failures/)"]
    assert known == []


def test_a_listed_failure_is_reported_not_counted():
    problems, known = kf.judge(ran=[A], failed=[f"{A}::test_x[case-1]"],
                               baselines=_baselines(f"{A}::test_x"), security=set())
    assert problems == [] and len(known) == 1


def test_a_whole_file_entry_covers_its_tests_and_its_collection_error():
    base = _baselines(A)
    for failed in (f"{A}::test_x", A):
        assert kf.judge(ran=[A], failed=[failed], baselines=base, security=set())[0] == []


def test_a_listed_test_that_passes_now_is_stale():
    problems, _ = kf.judge(ran=[A], failed=[], passed=[f"{A}::test_x"],
                           baselines=_baselines(f"{A}::test_x"),
                           security=set())
    assert len(problems) == 1 and problems[0].startswith(f"STALE: {A}::test_x passes now")
    assert "only shrinks" in problems[0]


def test_a_listed_test_that_did_not_run_or_did_not_finish_is_not_judged():
    base = _baselines(f"{A}::test_x")
    assert kf.judge(ran=[B], failed=[], baselines=base, security=set())[0] == []
    assert kf.judge(ran=[A], failed=["engine: the pytest run did not finish"], unfinished=[A],
                    baselines=base, security=set())[0] == [
        "NEW FAILURE: engine: the pytest run did not finish (not in tests/known-failures/)"]


def test_a_red_security_guard_fails_even_when_listed():
    problems, known = kf.judge(ran=[G], failed=[f"{G}::test_no_leak"],
                               baselines=_baselines(G), security={G})
    assert problems and problems[0].startswith(f"SECURITY GUARD RED: {G}::test_no_leak")
    assert known == []


# ── loading refuses a dishonest line ──────────────────────────────────────────────────────────

def _tree(tmp_path: Path, line: str, security: str = "[]") -> Path:
    (tmp_path / "tests" / "known-failures").mkdir(parents=True)
    (tmp_path / "engine" / "tests").mkdir(parents=True)
    (tmp_path / "engine" / "tests" / "test_a.py").write_text("")
    (tmp_path / "tests" / "tiers.yaml").write_text(f"security: {security}\n")
    (tmp_path / "tests" / "known-failures" / "engine.txt").write_text(line + "\n")
    return tmp_path


@pytest.mark.parametrize("line, words", [
    (f"{A} | BOSSAPP-1 | engine | 2026-11-23", "expected"),
    (f"{A} | soon | engine | 2026-11-23 | it fails today", "ticket"),
    (f"{A} | BOSSAPP-1 | engine | yesterday | it fails today", "since"),
    (f"{A} | BOSSAPP-1 | engine | 2026-11-23 | flaky", "why it fails"),
    (f"engine/tests/test_gone.py | BOSSAPP-1 | engine | 2026-11-23 | it fails today",
     "does not exist"),
    (f"apps/api/tests/test_a.py | BOSSAPP-1 | engine | 2026-11-23 | it fails today",
     "not under engine/tests/"),
])
def test_a_malformed_line_is_refused(tmp_path, line, words):
    with pytest.raises(ValueError, match=words):
        kf.load("engine", _tree(tmp_path, line))


def test_a_security_guard_can_never_be_listed(tmp_path):
    root = _tree(tmp_path, f"{A} | BOSSAPP-1 | engine | 2026-11-23 | it fails today",
                 security=f"[{A}]")
    with pytest.raises(ValueError, match="security guard"):
        kf.load("engine", root)


def test_a_line_listed_twice_is_refused(tmp_path):
    line = f"{A} | BOSSAPP-1 | engine | 2026-11-23 | it fails today"
    with pytest.raises(ValueError, match="twice"):
        kf.load("engine", _tree(tmp_path, f"{line}\n{line}"))


def test_a_listed_test_that_was_skipped_here_is_not_stale():
    """A Go test skipped on a machine without Go is not evidence that it is fixed."""
    base = _baselines(f"{A}::test_go")
    assert kf.judge(ran=[A], failed=[], passed=[f"{A}::test_other"], baselines=base,
                    security=set())[0] == []
    problems, _ = kf.judge(ran=[A], failed=[], passed=[f"{A}::test_go"], baselines=base,
                           security=set())
    assert problems and problems[0].startswith("STALE")
