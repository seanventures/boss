"""NO OUTSIDE PRODUCT'S NAME IS EVER IN BOSS.

The operator's rule: another product the team works on was a reference only. Its names, domains,
emails and links never appear in BOSS, not even as a sample account in a test or a placeholder in
a form. Samples use a neutral org (`example-org`), placeholders use `your-org`.

The pattern is built from pieces so this file does not match itself.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Any spelling with doubled letters: an exact match once missed a variant with an extra letter.
FORBIDDEN = "ka" + "a+p+u"


def test_no_tracked_file_names_the_outside_product():
    found = subprocess.run(["git", "grep", "-n", "-i", "-E", FORBIDDEN], cwd=ROOT,
                           capture_output=True, text=True)
    assert found.returncode in (0, 1), found.stderr
    hits = found.stdout.splitlines()
    assert hits == [], f"use example-org / your-org instead: {hits[:10]}"


def test_no_tracked_path_names_the_outside_product():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.splitlines()
    bad = [p for p in tracked if re.search(FORBIDDEN, p, re.I)]
    assert bad == [], f"tracked paths name the outside product: {bad[:10]}"
