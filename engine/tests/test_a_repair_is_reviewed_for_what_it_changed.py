"""A repair attempt's reviewers judge what the repair changed, unless a guardrail says otherwise.

HelpDesk AI, "Add SLA timers to tickets" (2026-11-06). Attempt 1 (crun_c8e2a5f8) was reviewed in
full and failed: a DST test missing, /api/triage/queue untested, web/tests/sla.test.mjs never run
by the root `npm test`. Its repair (crun_b32b4a26) changed the root package.json and added three
test files — and its first panel re-read the whole 21-file change: review_code and
consistency_check ~7 min each, part of a 28-min repair. The repair's seats now get the delta
against what the parent's seat reviewed, its own findings to confirm, and the instruction to look
for regressions. A big repair, a repair touching files nobody named, or a parent with no
recorded review still gets the FULL review. security_check scopes under the same guardrails
(BOSSAPP-1000): it used to re-read the FULL change on every repair round.
"""
from __future__ import annotations

import contextlib

from boss_engine.services import governed_code_task_service as g
from boss_engine.services import repair_review_scope as scope
from boss_engine.services import review_input


def _lines(n: int, tag: str = "x") -> str:
    return "".join(f"const {tag}{i} = {i};\n" for i in range(n))


#: Attempt 1's change as its seats reviewed it (trimmed to the files that matter).
REVIEWED = {
    "package.json": '{"scripts": {"test": "node --test tests/*.test.mjs"}}\n',
    "server/sla.mjs": _lines(40, "sla"),
    "server/routes/tickets.mjs": _lines(60, "route"),
    "tests/sla_lib.test.mjs": _lines(20, "t"),
    "tests/sla_tickets_route.test.mjs": _lines(20, "r"),
    "web/tests/sla.test.mjs": _lines(10, "w"),
    "web/src/pages/DashboardPage.jsx": _lines(30, "d"),
}

#: The seats' words on crun_c8e2a5f8 (trimmed).
REVIEW_SUMMARY = (
    "REVIEWED: package.json, server/app.mjs, server/sla.mjs, server/routes/tickets.mjs, "
    "web/src/pages/DashboardPage.jsx, web/tests/sla.test.mjs\n"
    "root `package.json` only runs `tests/*.test.mjs`, so web/tests is never executed.\n"
    "FINDING: MAJOR web/tests/sla.test.mjs:1 — never executed by the root npm test.\n"
    "FINDING: MINOR server/sla.mjs:23 — the 25% amber/green boundary is untested.\n"
    "VERDICT: REFUTED")
CONSISTENCY_SUMMARY = (
    "REVIEWED: server/routes/tickets.mjs, tests/sla_tickets_route.test.mjs\n"
    "5 ❌ — GET /api/triage/queue carries `sla` (server/routes/tickets.mjs:352) but no test "
    "asserts it.\nCONSISTENT: NO")


def _change(files: dict) -> review_input.ChangeUnderReview:
    return review_input.build(files)


def _rounds(*, reviewed=True, hollow=None):
    entry = {"round": 0, "seats": {
        "review_code": {"verdict": "refuted", "summary": REVIEW_SUMMARY, "hollow": hollow},
        "security_check": {"verdict": "clean", "summary": "REVIEWED: server/sla.mjs\nVERDICT: CLEAN",
                           "hollow": None},
        "consistency_check": {"verdict": "drift", "summary": CONSISTENCY_SUMMARY, "hollow": None}}}
    if reviewed:
        entry["reviewed"] = scope.snapshot(_change(REVIEWED))
    return [entry]


def _the_real_repair() -> dict:
    files = dict(REVIEWED)
    files["package.json"] = ('{"scripts": {"test": "node --test tests/*.test.mjs && '
                             'cd web && npm test"}}\n')
    files["tests/sla_dst.test.mjs"] = _lines(25, "dst")
    files["tests/sla_threshold.test.mjs"] = _lines(25, "th")
    files["tests/sla_triage_queue.test.mjs"] = _lines(30, "tq")
    return files


def _plan(seat, files, rounds=None, named_text=""):
    return scope.plan(seat, parent_run_id="crun_c8e2a5f8ed5246308016",
                      parent_rounds=_rounds() if rounds is None else rounds,
                      current=_change(files), baseline=dict(REVIEWED),
                      named_text=named_text, is_test=g._is_test_path)


# --- scoped: a small repair of the files the findings named -----------------------------------

def test_the_real_sla_repair_is_reviewed_for_what_it_changed():
    p = _plan("review_code", _the_real_repair())
    assert p.mode == scope.SCOPED, p.reason
    assert sorted(p.changed) == ["package.json", "tests/sla_dst.test.mjs",
                                 "tests/sla_threshold.test.mjs",
                                 "tests/sla_triage_queue.test.mjs"]
    # Unchanged, already-judged code is not handed over again.
    assert "server/routes/tickets.mjs" not in p.changed
    block = scope.render(p)
    assert "YOUR PREVIOUS FINDINGS" in block and "web/tests/sla.test.mjs:1" in block
    assert "regressions the repair introduced" in block
    # A file BOSS holds the reviewed text of is shown as a diff, a new one whole.
    assert "=== DIFF: package.json ===" in block and "+{\"scripts\"" in block
    assert "=== FILE: tests/sla_dst.test.mjs ===" in block
    assert "=== FILE: server/sla.mjs ===" not in block


def test_consistency_is_scoped_with_its_own_findings():
    # The panel hands every seat the files ANY seat's findings named (the wiring below): the
    # package.json fix answered the code reviewer, and consistency re-checks it all the same.
    assert _plan("consistency_check", _the_real_repair()).mode == scope.FULL
    p = _plan("consistency_check", _the_real_repair(), named_text=REVIEW_SUMMARY)
    assert p.mode == scope.SCOPED, p.reason
    assert "/api/triage/queue" in scope.render(p)


def test_security_scopes_under_the_same_guardrails():
    # The same guardrails as the other seats: files nobody named send it to the full review …
    assert _plan("security_check", _the_real_repair()).mode == scope.FULL
    # … and a small repair of named files is scoped, with the security seat's OWN findings.
    p = _plan("security_check", _the_real_repair(), named_text=REVIEW_SUMMARY)
    assert p.mode == scope.SCOPED, p.reason
    assert "VERDICT: CLEAN" in scope.render(p)
    # A seat this module does not know is never scoped.
    assert scope.plan("regression", parent_run_id="x", parent_rounds=_rounds(),
                      current=_change(_the_real_repair())).reason == "seat_always_full"


# --- full: the guardrails ----------------------------------------------------------------------

def test_a_large_repair_gets_the_full_review():
    files = dict(REVIEWED)
    files["server/sla.mjs"] = _lines(40, "sla") + _lines(scope.MAX_SCOPED_LINES + 1, "more")
    p = _plan("review_code", files)
    assert p.mode == scope.FULL and p.reason.startswith("too_many_lines")


def test_a_repair_over_many_files_gets_the_full_review():
    files = dict(REVIEWED)
    for i in range(scope.MAX_SCOPED_FILES + 1):
        files[f"tests/sla_case{i}.test.mjs"] = "ok\n"
    p = _plan("review_code", files)
    assert p.mode == scope.FULL and p.reason.startswith("too_many_files")


def test_a_repair_touching_unrelated_files_gets_the_full_review():
    files = _the_real_repair()
    files["server/auth/session.mjs"] = "export const SECRET_TTL = 1;\n"
    p = _plan("review_code", files)
    assert p.mode == scope.FULL
    assert "server/auth/session.mjs" in p.reason


def test_an_untouched_file_named_only_in_the_reviewed_list_does_not_open_the_scope():
    # DashboardPage.jsx is in the REVIEWED line, but no finding names it: changing it is outside.
    files = _the_real_repair()
    files["web/src/pages/DashboardPage.jsx"] = _lines(30, "d") + "export const x = 1;\n"
    p = _plan("review_code", files)
    assert p.mode == scope.FULL and "DashboardPage.jsx" in p.reason


def test_a_test_for_unnamed_code_gets_the_full_review():
    files = _the_real_repair()
    files["tests/billing_export.test.mjs"] = "ok\n"
    assert _plan("review_code", files).mode == scope.FULL


def test_failing_test_output_and_the_operators_note_name_files_too():
    files = _the_real_repair()
    files["server/flags.mjs"] = "export const ENABLE_SLA_TIMERS = true;\n"
    assert _plan("review_code", files).mode == scope.FULL
    out = "not ok 3 - kill switch\n  at server/flags.mjs:4\n"
    assert _plan("review_code", files, named_text=out).mode == scope.SCOPED


def test_no_prior_review_gets_the_full_review():
    for rounds in ([], _rounds(hollow="the review names none of the changed files"),
                   _rounds(reviewed=False)):   # a parent from before the snapshot was recorded
        p = _plan("review_code", _the_real_repair(), rounds=rounds)
        assert p.mode == scope.FULL and p.reason == "no_prior_review"


def test_the_latest_completed_review_is_the_baseline():
    later = {"round": 1, "reviewed": scope.snapshot(_change(_the_real_repair())),
             "seats": {"review_code": {"verdict": "survived", "summary": "REVIEWED: x\nok",
                                       "hollow": None}}}
    p = _plan("review_code", _the_real_repair(), rounds=_rounds() + [later])
    assert p.baseline_round == 1 and p.changed == {}
    assert p.mode == scope.SCOPED


# --- wiring: the panel asks once, records the mode, scopes only what the plan allows -----------

class _FakeConn:
    def __init__(self, store):
        self.store = store

    def execute(self, stmt, params=None):
        self.store.setdefault("sql", []).append((str(stmt), params))

        class _R:
            def mappings(self_inner):
                return self_inner

            def first(self_inner):
                return {"finalize_ctx": {}}
        return _R()


def _wire(monkeypatch, parent):
    store: dict = {"events": []}

    @contextlib.contextmanager
    def _tx(_engine):
        yield _FakeConn(store)
    monkeypatch.setattr(g, "_transaction", _tx)
    monkeypatch.setattr(g, "load_code_task_run", lambda _c, rid: parent)
    monkeypatch.setattr(g, "emit_event", lambda _c, _p, kind, *_a: store["events"].append(
        (kind, _a[-1])))
    return store


def test_the_panel_scopes_the_seats_and_records_the_mode(monkeypatch):
    parent = {"id": "crun_c8e2a5f8ed5246308016",
              "finalize_ctx": {"review_rounds": _rounds(),
                               "failing_gates": ["written_tests_pass", "code_review_passed"]},
              "state": {"last_verify_output": "not ok 1 tests/sla_lib.test.mjs"}}
    store = _wire(monkeypatch, parent)
    blocks = g._repair_review_scope_blocks(
        None, run_id="crun_b32b4a26adc74899881d", project_id="p",
        fctx={"parent_run_id": parent["id"]}, change=_change(_the_real_repair()),
        baseline=dict(REVIEWED))
    # Every LLM seat scopes here: the panel hands each one the files ANY seat's findings named,
    # so security (whose own review named only server/sla.mjs) is scoped too (BOSSAPP-1000).
    assert set(blocks) == {"review_code", "security_check", "consistency_check"}
    (kind, data), = store["events"]
    assert kind == "code_task.repair_review_scope"
    assert data["seats"]["review_code"]["mode"] == "scoped"
    assert data["seats"]["security_check"]["mode"] == "scoped"


def test_a_first_attempt_and_the_fix_lane_review_in_full(monkeypatch):
    store = _wire(monkeypatch, None)
    change = _change(_the_real_repair())
    assert g._repair_review_scope_blocks(None, run_id="r", project_id="p", fctx={},
                                         change=change, baseline={}) == {}
    assert g._repair_review_scope_blocks(None, run_id="r", project_id="p",
                                         fctx={"parent_run_id": "x"}, change=change,
                                         baseline={}, enabled=False) == {}
    assert store["events"] == []
    # A parent BOSS cannot read: full review, and the record says so.
    assert g._repair_review_scope_blocks(None, run_id="r", project_id="p",
                                         fctx={"parent_run_id": "gone"}, change=change,
                                         baseline={}) == {}
    (_, data), = store["events"]
    assert {s["mode"] for s in data["seats"].values()} == {"full"}


def test_every_review_round_records_the_change_it_judged(monkeypatch):
    store = _wire(monkeypatch, None)
    snap = scope.snapshot(_change(REVIEWED))
    g._record_review_round(None, "r", "p", round_i=0, seats={
        "review_code": {"verdict": "refuted", "summary": REVIEW_SUMMARY}}, reviewed=snap)
    import json
    written = [p for s, p in store["sql"] if "UPDATE code_task_runs" in s][0]
    assert json.loads(written["r"])[0]["reviewed"] == snap
