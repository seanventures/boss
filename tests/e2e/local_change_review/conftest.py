"""LOCAL CHANGE REVIEW, DRIVEN LIKE A PERSON (BOSSAPP-948; wireframe
docs/product/wireframes/agentic-factory/5-local-change-review.html).

The local-first harness as it is (tests/e2e/local_first/conftest.py: a real Chromium at 1440 and
390, the guard after every test — no console error, no unexpected /v1 >= 400, no sideways scroll,
every popover whole in view), and the same dev stack:

  BOSS_E2E_CONSOLE / BOSS_E2E_API      the console and the dev API (signed in as an owner)
  BOSS_E2E_DEV_SEAMS=1                 /__dev/lf/stage (a build's change set, through the
                                       pipeline's own functions) and /__dev/lf/answer (the fix
                                       round's builder answering, through _record_comment_responses
                                       and local_review.on_change_proposed)
  BOSS_E2E_REPO_ROOT / _IMPORT_ROOT / _FAKES   as local_first needs them

Run:  scripts/e2e/local-change-review.sh   (starts the whole disposable stack, runs both suites, stops it)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_LF = Path(__file__).resolve().parents[1] / "local_first" / "conftest.py"
_spec = importlib.util.spec_from_file_location("local_first_harness", _LF)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)  # type: ignore[union-attr]

browser = lf.browser
person = lf.person
pytest_runtest_makereport = lf.pytest_runtest_makereport
Person, api, seam, git, bare_of, fresh_project, unique = (lf.Person, lf.api, lf.seam, lf.git, lf.bare_of,
                                                           lf.fresh_project, lf.unique)
CONSOLE = lf.ap.CONSOLE


@pytest.fixture(scope="session")
def review_project() -> str:
    """One local project for the suite: each test opens its own change on it."""
    return fresh_project("Review local")
