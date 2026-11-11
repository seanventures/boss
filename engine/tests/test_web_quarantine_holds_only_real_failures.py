"""The web unit suite's quarantine (tests/web-quarantine.txt) hides nothing and only shrinks.

CI's web job (scripts/web-unit-ci.py) fails on any failing file not listed and on any listed file
that passes. Here: every line is well formed, names a test file that exists, and carries a ticket
and a reason; and, where the web dependencies are installed, every listed file really fails today.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "web-unit-ci.py"


def _module():
    spec = importlib.util.spec_from_file_location("web_unit_ci", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_line_names_an_existing_test_with_a_ticket_and_a_reason():
    mod = _module()
    quarantine = mod.read_quarantine()
    assert quarantine, "an empty quarantine is fine, but then delete this file's caveats"
    for file, q in quarantine.items():
        assert (mod.WEB / file).is_file(), f"{file} does not exist: delete its line"
        assert re.fullmatch(r"tests/[\w./-]+\.test\.tsx?", file), file
        assert re.fullmatch(r"BOSSAPP-\d+", q["ticket"]), f"{file}: a ticket, e.g. BOSSAPP-944"
        assert len(q["reason"].split()) >= 4, f"{file}: say why it fails"


def test_a_malformed_line_is_refused(tmp_path):
    mod = _module()
    bad = tmp_path / "q.txt"
    bad.write_text("tests/a.test.ts | BOSSAPP-1\n")
    with pytest.raises(ValueError, match="expected"):
        mod.read_quarantine(bad)


def test_the_judge_fails_new_failures_and_fixed_or_missing_quarantined_files(tmp_path):
    mod = _module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "old.test.ts").write_text("")
    quarantine = {"tests/old.test.ts": {"ticket": "BOSSAPP-1", "reason": "r"}}
    real_web = mod.WEB
    try:
        mod.WEB = tmp_path
        assert mod.judge({"tests/old.test.ts": "failed", "tests/ok.test.ts": "passed"},
                         quarantine) == []
        problems = mod.judge({"tests/new.test.ts": "failed", "tests/old.test.ts": "failed"},
                             quarantine)
        assert problems == ["NEW FAILURE: tests/new.test.ts (not in tests/web-quarantine.txt)"]
        assert any("NOW PASSES" in p for p in mod.judge({"tests/old.test.ts": "passed"}, quarantine))
        assert any("DID NOT RUN" in p for p in mod.judge({"tests/x.test.ts": "passed"}, quarantine))
        (tmp_path / "tests" / "old.test.ts").unlink()
        assert any("IS GONE" in p for p in mod.judge({}, quarantine))
    finally:
        mod.WEB = real_web


@pytest.mark.skipif(not (ROOT / "apps" / "web" / "node_modules" / ".bin" / "vitest").exists(),
                    reason="the web dependencies are not installed (pnpm install)")
def test_every_quarantined_file_really_fails():
    done = subprocess.run([sys.executable, str(SCRIPT), "--only-quarantined"], cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
