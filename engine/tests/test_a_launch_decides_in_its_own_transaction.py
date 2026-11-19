"""A PDP CHECK INSIDE A WRITE RUNS IN THAT WRITE'S TRANSACTION.

THE CASE (2026-11-19, laptop upgraded to 0c4f2f2ee): sign-in hung at "Enter your code · Checking…".
An Easy Mode launch (spec_first_run -> _start_approver) asked the PDP "may this person approve a
PRD here?" through viewer.decide WITHOUT the launch's connection. The PDP read on its own
connection (it could not see the project the launch had just created, so: deny) and audited that
deny in its own transaction, which waited for the org's audit lock the launch transaction already
held. The request thread waited on itself forever, holding the lock; every later audited action in
the org queued behind it — the TOTP check first (its rate-limit row, then the audit write).

Now viewer.decide takes the caller's `db` and hands it to authz.check, and the callers that hold a
transaction pass it.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from boss_engine.identity import authz, lineage, viewer
from boss_engine.services import factory_launch

ENGINE = Path(__file__).resolve().parents[1] / "boss_engine"


def test_decide_hands_the_callers_transaction_to_the_pdp(monkeypatch):
    seen = {}
    monkeypatch.setattr(viewer, "enforced", lambda: True)
    monkeypatch.setattr(lineage, "current_context",
                        lambda: SimpleNamespace(principal=SimpleNamespace(org_id="org_x", id="usr_a")))

    def check(ctx, permission, scope, *, db=None):
        seen["db"] = db
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(authz, "check", check)
    tx = object()
    assert viewer.decide("prj_1", "decision:approve", db=tx) is True
    assert seen["db"] is tx


def test_the_easy_mode_start_asks_in_the_launch_transaction(monkeypatch):
    got = {}

    def decide(project_id, permission, *, db=None):
        got.update(project_id=project_id, permission=permission, db=db)
        return True

    monkeypatch.setattr(viewer, "decide", decide)
    monkeypatch.setattr(lineage, "current_context",
                        lambda: SimpleNamespace(principal=SimpleNamespace(id="usr_a")))
    tx = object()
    assert factory_launch._start_approver(tx, "prj_1", "ceo") == "usr_a"
    assert got == {"project_id": "prj_1", "permission": "decision:approve", "db": tx}


def test_every_decide_in_a_function_holding_a_connection_passes_it():
    """The class, not the instance: a viewer.decide call inside a function that has a
    `connection`/`db` parameter must pass it as db=."""
    wrong = []
    for path in ENGINE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            if not params & {"connection", "db", "conn"}:
                continue
            for call in ast.walk(fn):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "decide"
                        and isinstance(call.func.value, ast.Name)
                        and call.func.value.id in ("viewer", "_viewer")
                        and not any(k.arg == "db" for k in call.keywords)):
                    wrong.append(f"{path.relative_to(ENGINE)}:{call.lineno}")
    assert not wrong, f"viewer.decide without the caller's transaction: {wrong}"
