"""A FEATURE ADDED TO AN EXISTING APP IS HELD TO ITS OWN MUST-HAVES (BOSSAPP-1322, AR-28).

Adding a feature to an app that already exists is THE main use case. Continue, New work, Change
it and extend_code_task start a follow-up on the app's lineage, and the acceptance gate held that
follow-up only to the lineage's frozen contract: the feature's own must-haves were never added,
so "Add CSV export" passed without the export.

Now a follow-up carrying a PERSON'S NEW REQUEST (start_followup_task marks it `change_request`)
adds the request's must-haves to the lineage's contract as a new version, read the way a first
build's are (or the request itself, word for word). Earlier items still count unless the request
explicitly replaces or removes one, which the version records in the request's words. A
checks-failing / findings follow-up adds nothing. The report lists the new items under the
request that added them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services.acceptance import contract as ac

from .test_a_resumed_build_runs_as_the_person import _Jobs
from .test_acceptance_contract import _run
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name

SPEC = {"owner": "acme", "repo": "helpdesk",
        "instructions": "Must-haves:\n- Tickets can be created\n- Tickets can be closed\n"
                        "- Agents see a queue of open tickets\n"}
OLD = ["Tickets can be created", "Tickets can be closed", "Agents see a queue of open tickets"]


@pytest.fixture(autouse=True)
def strict_transaction_guard(monkeypatch):
    monkeypatch.setenv(ac.STRICT_GUARD_ENV, "1")


@pytest.fixture()
def app(eng, project):  # noqa: F811
    """An app that exists: its first build, its contract frozen at its first check."""
    with eng.begin() as c:
        root = _run(c, project, spec=SPEC, fctx={})
        ag.items_spec(c, run_id=root, fctx={}, own_spec=SPEC)
    yield root
    with eng.begin() as c:
        for t in ("acceptance_contracts", "jobs", "code_task_runs"):
            c.execute(text(f"DELETE FROM {t} WHERE project_id = :p"), {"p": project})


def _child(eng, root, instructions, **kw):
    with eng.begin() as c:
        started = cts.start_followup_task(c, parent_run_id=root, instructions=instructions,
                                          jobs=_Jobs(), **kw)
        row = c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id = :i"),
                        {"i": started["run_id"]}).mappings().one()
    return started["run_id"], dict(row["spec"]), dict(row["finalize_ctx"])


def _held(eng, run_id, fctx, spec):
    with eng.begin() as c:
        held, source = ag.items_spec(c, run_id=run_id, fctx=fctx, own_spec=spec)
    return ag.acceptance_items(held), source


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def create(): ...\ndef close(): ...\ndef queue(): ...\n")
    return tmp_path


def test_a_feature_request_adds_its_must_have_and_a_build_without_it_fails(eng, app, tree):
    run, spec, fctx = _child(eng, app, "Add CSV export of the ticket list.")
    assert fctx["change_request"]["text"] == "Add CSV export of the ticket list."
    items, source = _held(eng, run, fctx, spec)
    texts = [i["text"] for i in items]
    assert texts[:3] == OLD                                   # earlier items still count
    assert "Add CSV export of the ticket list." in texts      # the feature itself
    assert source["contract"]["version"] == 2
    new = next(i for i in items if i["text"].startswith("Add CSV export"))
    assert new["source"] == "request" and new["request"]
    # The build kept the app working but never built the export: NOT met.
    reply = "\n".join(f"ITEM {i['n']}: MET — app.py" for i in items if i is not new)
    reply += f"\nITEM {new['n']}: MISSING — no export route or button anywhere\nACCEPTANCE: NOT MET"
    rec = ag.judge(items, reply, {}, root=tree, source=source)
    assert rec["verdict"] == "not_met" and rec["missing"] == [new["n"]]
    # The report lists the new item under the request that added them.
    table = ag.must_have_table(rec)
    head = table.index("**Added by the change")
    assert table.index("Add CSV export") > head > table.index("Tickets can be closed")


def test_a_structured_request_adds_each_must_have_and_its_criteria(eng, app):
    run, spec, fctx = _child(eng, app, "Add CSV export.\n\nMust-haves:\n- An Export button on "
                             "the queue\n- The file has one row per ticket\n",
                             acceptance_criteria=["Exported dates are ISO 8601"])
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert texts[:3] == OLD
    assert {"An Export button on the queue", "The file has one row per ticket",
            "Exported dates are ISO 8601"} <= set(texts)


def test_the_request_is_added_once_and_a_retry_keeps_it(eng, app, project):  # noqa: F811
    run, spec, fctx = _child(eng, app, "Add CSV export of the ticket list.")
    _held(eng, run, fctx, spec)
    _held(eng, run, fctx, spec)
    with eng.begin() as c:
        assert [v["version"] for v in ac.versions(c, root_run_id=app)] == [1, 2]
    import inspect

    assert '"change_request": fctx_prev["change_request"]' in inspect.getsource(cts)


def test_a_findings_follow_up_adds_nothing(eng, app, project):  # noqa: F811
    with eng.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status = 'completed', "
                       "finalize_ctx = finalize_ctx || '{\"root_run_id\": null}'::jsonb "
                       "WHERE id = :i"), {"i": app})
    new = governed._spawn_deferred_findings_task(
        eng, get_settings(), _Jobs(), project_id=project, task_id=None, spec=SPEC,
        base_branch="boss/helpdesk", findings=[{"severity": "major", "text": "a finding"}],
        parent_run_id=app, parent_title="HelpDesk", parent_root=app)
    review_fix, rspec, rfctx = _child(eng, app, "Fix these review comments: x",
                                      finalize_extra={"review_comments": [{"id": "rc1"}]})
    with eng.begin() as c:
        fspec, ffctx = c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs "
                                      "WHERE id = :i"), {"i": new}).one()
    for rid, s, f in ((new, fspec, ffctx), (review_fix, rspec, rfctx)):
        assert "change_request" not in f
        texts = [i["text"] for i in _held(eng, rid, f, s)[0]]
        assert texts[:3] == OLD
    with eng.begin() as c:
        assert [v["version"] for v in ac.versions(c, root_run_id=app)] == [1]


def test_an_explicit_replace_is_recorded_never_silent(eng, app):
    run, spec, fctx = _child(eng, app, "Closing changes.\n\nReplace item 2: tickets are "
                             "archived instead of closed.\n\nMust-haves:\n- Tickets can be "
                             "archived\n")
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert "Tickets can be closed" not in texts and "Tickets can be archived" in texts
    assert "Tickets can be created" in texts and "Agents see a queue of open tickets" in texts
    with eng.begin() as c:
        v2 = ac.latest(c, root_run_id=app)
    assert v2["version"] == 2
    assert "AC-2 replaced as the request says" in v2["reason"]
    assert "Replace item 2: tickets are archived instead of closed." in v2["reason"]
    req = v2["sources"][-1]
    assert req["kind"] == "request" and req["changed"][0]["id"] == "AC-2"


def test_a_number_the_contract_does_not_hold_changes_nothing(eng, app):
    run, spec, fctx = _child(eng, app, "Remove item 99 and add dark mode.")
    texts = [i["text"] for i in _held(eng, run, fctx, spec)[0]]
    assert texts[:3] == OLD


def test_item_changes_reads_only_explicit_numbered_changes():
    items = [{"id": f"AC-{n}"} for n in range(1, 5)]
    got = ac.item_changes("We will remove AC-3. Also drop must-have 4 as it is obsolete.\n"
                          "Keep item 1 as is.", items)
    assert [(g["id"], g["verb"]) for g in got] == [("AC-3", "remove"), ("AC-4", "drop")]
    assert ac.item_changes("Replace the logo with a new one.", items) == []
