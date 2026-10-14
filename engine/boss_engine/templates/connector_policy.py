"""A CONNECTOR IS PRE-TESTED: the one policy for every build that adds one (operator, 2026-10-13).

Login, Stripe and PayPal are each fully tested in their own test tier (the template trial, and the
reference suite run with `gate.evaluate(..., as_connector=False)`). A build that adds one, by any
entry path (the composer's New app or Add a feature, POST /v1/templates/{id}/apply, the
apply_template tool, a retry of any of them), therefore:

  * never copies the connector's test files into the app, never adds its `test:*` scripts and
    never writes tests for it (BOSS does not place them either: place_template_tests);
  * is judged on acceptance criteria that name the WIRING, never "the template's tests";
  * is reviewed by seats told that the absence of those files and scripts is correct;
  * is gated by one wiring smoke check (gate.connector_smoke): the app starts and the
    connector's routes answer.

Before this module the brief said "do NOT copy its test files" while the template's own brief,
the reference package.json and the acceptance criteria ("the template's tests pass: npm run
test:login-oauth") still demanded them, and the reviewer, the consistency seat and the PR all
called the missing suite a BLOCKER (crun_2d097414, PR paramg/boss-sandbox#227). Every sentence
about it now comes from here.
"""
from __future__ import annotations

from typing import Any, Iterable

#: The words every connector build carries, in its brief and in every seat's context. It names
#: the template's own material as overridden, because an imported template's brief may still
#: say "copy the tests" and the agents read it.
POLICY = (
    "THE CONNECTOR IS PRE-TESTED (BOSS policy; it overrides anything the template's brief, guide "
    "or reference files say about tests). Its own test suite runs in its own test tier, never in "
    "this build. Do NOT copy its test files into the app, do NOT add its test scripts (such as "
    "`test:<connector>` / `pretest:<connector>` from the reference package.json) to the app. "
    "Do NOT write tests for the connector: the app's tests cover only what the operator asked the "
    "app to do. BOSS checks the connector's wiring once: the app starts and the connector's "
    "routes answer. Reviewers: the absence of the connector's test files or test scripts is "
    "correct and is never a finding.")

#: The marker a stored brief is recognised by (a retry of a build started before this policy).
_MARKER = "THE CONNECTOR IS PRE-TESTED"

#: Criteria that require the connector's own suite (what apply.plan wrote before this policy).
_SUITE_PHRASES = ("the template's tests", "template's test", "template tests")


def is_connector(manifest_or_kind: Any) -> bool:
    kind = manifest_or_kind if isinstance(manifest_or_kind, str) else \
        getattr(manifest_or_kind, "item_kind", None)
    return kind == "connector"


def acceptance_criteria(routes: Iterable[str]) -> list[str]:
    """A connector build's acceptance criteria: its wiring, never its suite."""
    out = [f"{route} is mounted in the app's own router and answers on the started app"
           for route in routes]
    out.append("the app starts with the connector wired in (BOSS's wiring smoke check)")
    return out


def requires_suite(criterion: Any) -> bool:
    text = str(criterion or "").lower()
    return any(p in text for p in _SUITE_PHRASES)


def normalize(instructions: str, criteria: Iterable[Any], routes: Iterable[str]) -> tuple[str, list]:
    """The brief and criteria a connector build runs with, whatever its stored spec says.

    A retry re-runs the spec it was started with, so a build started before this policy (or by
    an imported workflow that wrote its own criteria) still carries "the template's tests pass".
    Those criteria are replaced by the wiring ones, and the policy is put at the top of the brief
    when it is not there. Idempotent."""
    crit = list(criteria or [])
    kept = [c for c in crit if not requires_suite(c)]
    if len(kept) != len(crit):
        kept += [c for c in acceptance_criteria(routes) if c not in kept]
    text = str(instructions or "")
    if _MARKER not in text:
        text = POLICY + ("\n\n" + text if text else "")
    return text, kept
