"""THE FINDINGS LEDGER (AA-B, BOSSAPP-1216, epic BOSSAPP-1213).

Northgate (2026-11-15): the fast lane recorded the reviewers' BLOCKERs and MAJORs as advisory
JSONB on the run, the PR opened, and the follow-up carried two MINOR findings while fifteen
must-haves stayed missing. Now every acceptance gap and every reviewer finding — any severity,
advisory or not — is a ledger row tied to a work item; a row is fixed only when a later check
PROVES it, reopens on a regression, and follow-ups are built from the open rows and the frozen
contract.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import acceptance_gate as ag
from boss_engine.services import governed_code_task_service as governed
from boss_engine.services.acceptance import checks as acc_checks
from boss_engine.services.acceptance import contract as ac
from boss_engine.services.acceptance import ledger as al
from boss_engine.services.review_comments import parse_findings

from ._acceptance_fixtures import load_northgate_v1
from .test_a_resumed_build_runs_as_the_person import _Jobs
from .test_workflow_capability_runner import eng, project  # noqa: F401 - fixtures used by name


@pytest.fixture(autouse=True)
def strict_transaction_guard(monkeypatch):
    monkeypatch.setenv(ac.STRICT_GUARD_ENV, "1")


def _run(c, project_id, *, spec=None, fctx=None, status="completed", pr=None, rid=None):
    rid = rid or f"crun_{uuid.uuid4().hex[:12]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, title, spec, finalize_ctx, "
        "pull_request_url, created_at) VALUES (:i, :p, :s, 'Gateway', CAST(:sp AS JSONB), "
        "CAST(:f AS JSONB), :u, clock_timestamp())"),
        {"i": rid, "p": project_id, "s": status, "sp": json.dumps(spec or {}),
         "f": json.dumps(fctx or {}), "u": pr})
    return rid


@pytest.fixture()
def ledger_project(eng, project):  # noqa: F811
    yield project
    with eng.begin() as c:
        for table in ("finding_observations WHERE finding_id IN (SELECT id FROM findings "
                      "WHERE project_id = :p)", "findings WHERE project_id = :p",
                      "acceptance_checks WHERE project_id = :p",
                      "acceptance_contracts WHERE project_id = :p",
                      "jobs WHERE project_id = :p",
                      "work_item_links WHERE project_id = :p",
                      "work_items WHERE project_id = :p",
                      "code_task_runs WHERE project_id = :p"):
            c.execute(text(f"DELETE FROM {table}"), {"p": project})


SPEC = {"instructions": "Must-haves:\n- Unauthenticated requests get 401\n"
                        "- make run-staging starts the gateway\n- Rate limit returns 429\n"}


def _check(c, project_id, root, statuses: dict, *, evidence=None, layer="build_gate"):
    """Judge the lineage's contract items with the given statuses and record + observe it."""
    spec, source = ag.items_spec(c, run_id=root, fctx={}, own_spec=SPEC)
    items = ag.acceptance_items(spec)
    lines = []
    for i in items:
        st = statuses.get(i["n"], "MET")
        why = (evidence or {}).get(i["n"]) or ("app.py" if st == "MET" else "absent")
        lines.append(f"ITEM {i['n']}: {st} — {why}")
    rec = ag.judge(items, "\n".join(lines) + "\nACCEPTANCE: "
                   + ("MET" if all(s == "MET" for s in statuses.values()) else "NOT MET"),
                   {}, root=None, source=source)
    # root=None: no tree, so a MET reads as cited; mark it verified as the gate would.
    for i in rec["items"]:
        if statuses.get(i["n"], "MET") == "MET":
            i["status"] = "met"
    check = acc_checks.record_check(c, project_id=project_id, lineage_root_run_id=root,
                                    layer=layer, mode="static", record=rec,
                                    contract_id=source["contract"]["id"], code_task_run_id=root)
    delta = al.observe_check(c, project_id=project_id, lineage=root, check=check, record=rec,
                             run_id=root, pr_url=al.lineage_of(c, root)["pr_url"])
    return check, delta


def _rows(c, project_id):
    return {r["fingerprint"]: r for r in al.open_for(c, project_id=project_id)}


def test_ledger_dedupes_a_live_fingerprint_and_counts_each_sighting(eng, ledger_project):
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        check1, d1 = _check(c, p, root, {1: "MISSING", 2: "PARTIAL"})
        assert len(d1.opened) == 2
        _, d2 = _check(c, p, root, {1: "MISSING", 2: "PARTIAL"})
        assert d2.opened == [] and len(d2.seen) == 2
        rows = _rows(c, p)
        gap = rows["ac:AC-1"]
        assert gap["kind"] == "acceptance_gap" and gap["contract_item_id"] == "AC-1"
        assert gap["seen_count"] == 2 and gap["first_seen_check_id"] == check1["id"]
        assert "Unauthenticated requests get 401" in gap["body"]
        # Re-recording the SAME observation is not a second sighting.
        al.observe_check(c, project_id=p, lineage=root, check=check1,
                         record={"items": [{"n": 1, "text": "Unauthenticated requests get 401",
                                            "status": "missing", "evidence": "absent"}]},
                         run_id=root)
        assert _rows(c, p)["ac:AC-1"]["seen_count"] == 2
        assert c.execute(text("SELECT count(*) FROM acceptance_checks WHERE project_id = :p "
                              "AND layer = 'build_gate'"), {"p": p}).scalar() == 2


def test_a_check_that_proves_it_fixed_closes_the_row_and_a_regression_reopens_it(
        eng, ledger_project):
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        _check(c, p, root, {1: "MISSING"})
        fid = _rows(c, p)["ac:AC-1"]["id"]
        fixed_check, d = _check(c, p, root, {})
        assert d.fixed == [fid]
        row = al.get(c, fid)
        assert row["status"] == "fixed" and row["resolved_check_id"] == fixed_check["id"]
        assert fixed_check["diff"]["newly_met"] == ["AC-1"]     # by contract id (PR #161 C2)
        _, d = _check(c, p, root, {1: "MISSING"})
        assert d.reopened == [fid] and d.opened == []
        row = al.get(c, fid)
        assert row["status"] == "open" and row["reopened_count"] == 1
        assert row["resolved_check_id"] is None
        seen = [o["status_seen"] for o in al.observations(c, fid)]
        assert seen == ["open", "fixed", "open"]


def test_an_unanswered_item_neither_opens_nor_fixes_a_row(eng, ledger_project):
    """A check that did not run (no answer) is not evidence either way."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        _check(c, p, root, {1: "MISSING"})
        rec = {"items": [{"n": 1, "text": "Unauthenticated requests get 401",
                          "status": "unjudged"}], "verdict": "not_run"}
        d = al.observe_check(c, project_id=p, lineage=root, check={"id": None}, record=rec)
        assert d.opened == d.fixed == [] and al.open_for(c, project_id=p)[0]["status"] == "open"


def test_review_findings_dedupe_by_path_and_title_across_rounds(eng, ledger_project):
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p)
        r1 = parse_findings("FINDING: MAJOR src/auth.go:14 — JWT audience is never checked\n"
                            "FINDING: NIT README.md:3 — typo in heading", stage="review_code")
        d1 = al.observe_review(c, project_id=p, lineage=root, run_id=root, comments=r1)
        # Round 2: the same defect, other words after the dash, another line, higher severity.
        r2 = parse_findings("FINDING: BLOCKER src/auth.go:20 — JWT audience is never checked "
                            "— still", stage="review_code")
        d2 = al.observe_review(c, project_id=p, lineage=root, run_id=f"{root}-r2", comments=r2)
        assert len(d1.opened) == 2 and d2.opened == [] and len(d2.seen) == 1
        row = al.get(c, d2.seen[0])
        assert row["seen_count"] == 2 and row["severity"] == "blocker"
        assert row["kind"] == "review" and row["path"] == "src/auth.go"


def test_an_agent_declared_duplicate_must_name_a_live_row(eng, ledger_project):
    """The reviewer's "no Makefile" BLOCKER and the gap AC-2 are one problem: the agent says
    so ("same as F-n"), BOSS checks F-n is live in this lineage, and only the gap stays."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        _check(c, p, root, {2: "MISSING"})
        gap = _rows(c, p)["ac:AC-2"]
        reviewer = al.observe_review(c, project_id=p, lineage=root, run_id=root,
                                     comments=parse_findings(
                                         "FINDING: BLOCKER Makefile:1 — no Makefile target",
                                         stage="review_code"))
        own = reviewer.opened[0]
        merged = al.observe_review(c, project_id=p, lineage=root, run_id=root,
                                   comments=parse_findings(
                                       f"FINDING: BLOCKER Makefile:1 — no Makefile target "
                                       f"[same as {gap['key']}]", stage="acceptance_step"))
        assert merged.merged == [own]
        assert al.get(c, own)["status"] == "duplicate"
        assert al.get(c, own)["duplicate_of"] == gap["id"]
        assert al.get(c, gap["id"])["severity"] == "blocker"
        # A declaration naming a row that is not live (or does not exist) is ignored.
        ghost = al.observe_review(c, project_id=p, lineage=root, run_id=root,
                                  comments=parse_findings(
                                      "FINDING: MAJOR x.go:2 — leaks a goroutine [same as F-9999]",
                                      stage="acceptance_step"))
        assert len(ghost.opened) == 1 and not ghost.merged


def test_every_finding_gets_a_work_item_under_the_lineages(eng, ledger_project):
    from boss_engine.services import work_items

    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC, pr="https://github.com/acme/gw/pull/7")
        parent = work_items.link_run(c, kind="code_task", run_id=root, project_id=p,
                                     title="Gateway")
        _check(c, p, root, {1: "MISSING"})
        row = _rows(c, p)["ac:AC-1"]
        item = c.execute(text("SELECT * FROM work_items WHERE id = :w"),
                         {"w": row["work_item_id"]}).mappings().one()
        assert item["subject_type"] == "finding" and item["subject_id"] == row["id"]
        assert item["parent_id"] == parent and item["title"].startswith(row["key"])
        links = {(r["work_item_id"], r["kind"], r["ref"]) for r in c.execute(text(
            "SELECT * FROM work_item_links WHERE project_id = :p"), {"p": p}).mappings()}
        assert (parent, "finding", row["id"]) in links
        assert (row["work_item_id"], "pull_request", "https://github.com/acme/gw/pull/7") in links
        _check(c, p, root, {})
        assert c.execute(text("SELECT status FROM work_items WHERE id = :w"),
                         {"w": row["work_item_id"]}).scalar() == "done"


def test_advisory_findings_are_never_lost_under_the_fast_lane(eng, ledger_project):
    """Lean: the MINOR/NIT review findings are advisory and a follow-up never starts from a
    follow-up — yet every one, and the acceptance seat's taste item answered PARTIAL, is a row
    the moment the seat produced it."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        follow = _run(c, p, fctx={"is_followup": True, "root_run_id": root},
                      spec={"instructions": "Fix the two MINOR findings"})
    evidence = {"profile": "lean",
                "review_summary": "FINDING: MINOR src/a.go:3 — error is swallowed\n"
                                  "FINDING: NIT src/b.go:9 — stray debug print\nVERDICT: SURVIVED",
                "security_summary": "FINDING: MAJOR src/c.go:1 — token logged\nVERDICT: CLEAN"}
    governed._record_review_comments(eng, follow, p, evidence)
    rec = {"verdict": "met", "summary_line": "MET", "source": {"root": True},
           "items": [{"n": 1, "text": "The dashboard looks polished", "status": "partial",
                      "judgement": True, "evidence": "spacing is uneven"}],
           "stack": {}, "counts": {}}
    check_id = governed._ledger_acceptance_check(eng, follow, p, rec)
    with eng.begin() as c:
        rows = {r["title"]: r for r in al.open_for(c, project_id=p)}
        assert check_id and set(r["lineage_root_run_id"] for r in rows.values()) == {root}
    assert {"error is swallowed", "stray debug print", "token logged"} <= set(rows)
    assert {rows["stray debug print"]["severity"], rows["token logged"]["kind"]} == {
        "nit", "security"}
    taste = next(r for r in rows.values() if r["kind"] == "acceptance_gap")
    assert taste["severity"] == "minor" and "polished" in taste["body"]


def _spawn(eng, p, parent, root, findings, **kw):
    return governed._spawn_deferred_findings_task(
        eng, get_settings(), _Jobs(), project_id=p, task_id=None,
        spec={"owner": "acme", "repo": "gw"}, base_branch="boss/gw", findings=findings,
        parent_run_id=parent, parent_title="Gateway", parent_root=root, **kw)


def test_a_follow_up_is_built_from_open_ledger_rows_and_the_contract(eng, ledger_project):
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC, pr="https://github.com/acme/gw/pull/7")
        _check(c, p, root, {1: "MISSING", 3: "PARTIAL"})
        al.observe_review(c, project_id=p, lineage=root, run_id=root, comments=parse_findings(
            "FINDING: NIT src/b.go:9 — stray debug print", stage="review_code"))
    # The run's own free-text findings: one is new, one repeats the NIT already in the ledger.
    new = _spawn(eng, p, root, root, [
        {"severity": "major", "text": "(review_code) src/c.go:4 — retries never back off"},
        {"severity": "nit", "text": "stray debug print", "path": "src/b.go", "line": 9}])
    assert new
    with eng.begin() as c:
        run = c.execute(text("SELECT spec, finalize_ctx FROM code_task_runs WHERE id = :i"),
                        {"i": new}).mappings().one()
        rows = al.open_for(c, project_id=p)
    instructions = run["spec"]["instructions"]
    keys = [r["key"] for r in rows]
    assert len(rows) == 4 and all(r["status"] == "in_progress" for r in rows)
    assert all(r["followup_run_id"] == new for r in rows)
    assert sorted(run["finalize_ctx"]["ledger_ids"]) == sorted(r["id"] for r in rows)
    for k in keys:
        assert k in instructions
    # Gaps (major) first, then the major review finding, the NIT last.
    order = [instructions.index(k) for k in keys]
    assert order == sorted(order)
    # THE CONTRACT rides along word for word, its open items marked.
    assert "THE ACCEPTANCE CONTRACT (v1" in instructions
    assert "AC-2: make run-staging starts the gateway" in instructions
    assert "AC-1 [OPEN F-" in instructions and "AC-3 [OPEN F-" in instructions
    assert instructions.count("stray debug print") == 1         # one row, not two
    nit = next(r for r in rows if r["title"] == "stray debug print")
    assert nit["seen_count"] == 1     # handed to the follow-up by the run that found it: once


def test_the_follow_up_guard_allows_new_rows_never_the_same_rows_twice(eng, ledger_project):
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        _check(c, p, root, {1: "MISSING"})
    # As checks_failing hands it over: the acceptance seat's line is the gap already in the
    # ledger (the check observed it), never a second row.
    first = _spawn(eng, p, root, root, [
        {"severity": "major", "text": "(acceptance_check) Must-have 1 MISSING: Unauthenticated "
                                      "requests get 401"}])
    assert first
    with eng.begin() as c:
        second_parent = _run(c, p, fctx={"root_run_id": root})
    # The same rows again (another path, a redelivered job): no second run for them.
    assert _spawn(eng, p, second_parent, root, []) is None
    # A NEW row appears: a follow-up carries it, and only it.
    newer = _spawn(eng, p, second_parent, root,
                   [{"severity": "major", "text": "(review_code) src/d.go:1 — races on close"}])
    assert newer and newer != first
    with eng.begin() as c:
        held = {r["title"]: r["followup_run_id"] for r in al.open_for(c, project_id=p)}
        assert held["races on close"] == newer
        assert [v for k, v in held.items() if k != "races on close"] == [first]
        # The first follow-up FAILS: its rows go back to open and the next one may carry them.
        c.execute(text("UPDATE code_task_runs SET status = 'failed' WHERE id = :i"),
                  {"i": first})
        released = al.release_dead_holds(c, project_id=p, lineage=root)
        assert len(released) == 1 and al.get(c, released[0])["status"] == "open"


def test_the_northgate_follow_up_spec_carries_every_contract_item_verbatim(
        eng, ledger_project):
    ng = load_northgate_v1()
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=ng.spec(), fctx={"ticket": ng.ticket()})
        spec, source = ag.items_spec(c, run_id=root, fctx={"ticket": ng.ticket()},
                                     own_spec=ng.spec())
        contract = ac.latest(c, root_run_id=root)
        items = ag.acceptance_items(spec)
        reply = "\n".join(f"ITEM {i['n']}: MISSING — not in the tree" for i in items[:15])
        rec = ag.judge(items, reply + "\nACCEPTANCE: NOT MET", {}, root=None, source=source)
        check = acc_checks.record_check(c, project_id=p, lineage_root_run_id=root,
                                        layer="build_gate", mode="static", record=rec,
                                        contract_id=contract["id"])
        delta = al.observe_check(c, project_id=p, lineage=root, check=check, record=rec)
        plan = al.followup_spec(c, project_id=p, lineage=root)
    assert len(delta.opened) == 15
    block = plan["contract_block"]
    for item in contract["items"]:
        assert f"{item['id']}" in block and item["text"] in block      # every word, no cap
    assert len(plan["acceptance_criteria"]) == 15
    assert len(plan["findings"]) == 15 and all(f["key"].startswith("F-")
                                               for f in plan["findings"])


def test_every_in_build_acceptance_check_is_recorded_in_the_ledger():
    """The class: the seat's three call sites (first check, repair re-check, escalation) all
    call the one wrapper, which records the check and observes it; nothing judges around it."""
    import inspect

    source = inspect.getsource(governed)
    assert source.count("_judge_acceptance_check(**kwargs)") == 1
    assert "_ledger_acceptance_check(engine, run_id, project_id, rec, root=review_root)" in source
    calls = re.findall(r"_judge_acceptance_check\(", source)
    assert len(calls) == 2              # its definition and the wrapper's one call
    assert len(re.findall(r"\b_run_acceptance_check\(", source)) >= 4   # def + 3 call sites
    review = inspect.getsource(governed._record_review_comments)
    assert "_ledger_review(engine, run_id, project_id, fresh, seats=_seats" in review


# ── PR #151 review: L1-L4 ───────────────────────────────────────────────────────────────────

def test_a_blocked_follow_up_releases_its_rows_and_an_old_hold_expires(eng, ledger_project):
    """L1: only a queued or running follow-up holds rows; BLOCKED (or any other status) ends
    the hold, and a hold older than the TTL is released whatever the run says."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        _check(c, p, root, {1: "MISSING", 2: "MISSING"})
        a, b = (r["id"] for r in al.open_for(c, project_id=p))
        blocked = _run(c, p, status="blocked", fctx={"root_run_id": root})
        running = _run(c, p, status="running", fctx={"root_run_id": root})
        al.hold(c, [a], followup_run_id=blocked)
        al.hold(c, [b], followup_run_id=running)
        assert al.held_by_live_followup(c, [a, b]) == [b]
        assert al.release_dead_holds(c, project_id=p, lineage=root) == [a]
        c.execute(text("UPDATE findings SET held_at = now() - interval '3 days' WHERE id = :i"),
                  {"i": b})
        assert al.held_by_live_followup(c, [b]) == []
        assert al.release_dead_holds(c, project_id=p, lineage=root) == [b]
        assert {al.get(c, x)["status"] for x in (a, b)} == {"open"}


def test_an_acceptance_finding_is_kept_when_its_checks_ledger_write_failed(eng, ledger_project,
                                                                          monkeypatch):
    """L2: the in-build check's own ledger write failed (swallowed), so no `ac:` row exists:
    the follow-up's acceptance lines become the rows — the follow-up still starts."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        ag.items_spec(c, run_id=root, fctx={}, own_spec=SPEC)        # the contract exists
    monkeypatch.setattr(al, "observe_check", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("ledger down")))
    rec = {"verdict": "not_met", "source": {"root": True}, "stack": {}, "counts": {},
           "items": [{"n": 1, "text": "Unauthenticated requests get 401", "status": "missing",
                      "evidence": "absent"},
                     {"n": 3, "text": "Rate limit returns 429", "status": "unjudged"}]}
    assert governed._ledger_acceptance_check(eng, root, p, rec) is None     # swallowed
    monkeypatch.undo()
    with eng.begin() as c:
        assert al.open_for(c, project_id=p) == []
    new = _spawn(eng, p, root, root, [
        {"severity": "major", "text": "(acceptance_check) Must-have 1 MISSING: Unauthenticated "
                                      "requests get 401 — absent"},
        {"severity": "major", "text": "(acceptance_check) Must-have 3 not judged: Rate limit "
                                      "returns 429"}])
    assert new
    with eng.begin() as c:
        rows = {r["fingerprint"]: r for r in al.open_for(c, project_id=p)}
    assert set(rows) == {"ac:AC-1", "ac:AC-3"}
    assert all(r["followup_run_id"] == new for r in rows.values())
    # Seen again by a later check, it is the same row (the check's own fingerprint).
    with eng.begin() as c:
        _, d = _check(c, p, root, {1: "MISSING"})
        assert rows["ac:AC-1"]["id"] in d.seen and not d.opened


def test_same_as_cannot_bury_a_finding_in_an_unrelated_row(eng, ledger_project):
    """L3: a BLOCKER on JWT declared "same as" a NIT about a README typo stands on its own;
    a plausible merge keeps the merged finding's words on the row and in the follow-up."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p)
        nit = al.observe_review(c, project_id=p, lineage=root, run_id=root,
                                comments=parse_findings("FINDING: NIT README.md:3 — typo in "
                                                        "heading", stage="review_code")).opened[0]
        key = al.get(c, nit)["key"]
        jwt = al.observe_review(c, project_id=p, lineage=root, run_id=root,
                                comments=parse_findings(
                                    f"FINDING: BLOCKER src/auth.go:14 — JWT audience is never "
                                    f"checked [same as {key}]", stage="acceptance_step"))
        assert len(jwt.opened) == 1 and not jwt.merged and nit not in jwt.seen
        row = al.get(c, jwt.opened[0])
        assert row["severity"] == "blocker" and row["path"] == "src/auth.go"
        assert al.get(c, nit)["severity"] == "nit" and "JWT" not in al.get(c, nit)["body"]
        # A close one (same file, one level apart) merges — and keeps its words.
        al.observe_review(c, project_id=p, lineage=root, run_id=f"{root}-2",
                          comments=parse_findings(f"FINDING: MINOR README.md:9 — heading "
                                                  f"spelled wrong twice [same as {key}]",
                                                  stage="acceptance_step"))
        merged = al.get(c, nit)
        assert "ALSO REPORTED (acceptance_step, MINOR) README.md:9:" in merged["body"]
        assert "heading spelled wrong twice" in merged["body"]
        assert "heading spelled wrong twice" in al.followup_line(merged)


def test_a_re_review_that_no_longer_reports_a_finding_closes_it(eng, ledger_project):
    """L4: the review seat looked again at the files it flagged (this round changed them) and
    said nothing about it: the row is fixed by that re-review. A file it did not touch, or a
    seat that gave no structured answer, closes nothing."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p)
    round1 = {"review_summary": "FINDING: MAJOR src/a.go:3 — error is swallowed\n"
                                "FINDING: MINOR src/b.go:9 — stray debug print\n"
                                "VERDICT: REFUTED",
              "impl_files": ["src/a.go", "src/b.go"]}
    governed._record_review_comments(eng, root, p, round1)
    with eng.begin() as c:
        follow = _run(c, p, fctx={"is_followup": True, "root_run_id": root})
    # A reply with no structure is no signal: nothing closes.
    governed._record_review_comments(eng, follow, p, {"review_summary": "looks fine?",
                                                      "impl_files": ["src/a.go"]})
    with eng.begin() as c:
        assert len(al.open_for(c, project_id=p)) == 2
    governed._record_review_comments(eng, follow, p, {"review_summary": "VERDICT: SURVIVED",
                                                      "impl_files": ["src/a.go"]})
    with eng.begin() as c:
        live = {r["title"]: r for r in al.open_for(c, project_id=p)}
        fixed = c.execute(text("SELECT title, status FROM findings WHERE project_id = :p"),
                          {"p": p}).all()
    assert set(live) == {"stray debug print"}                 # src/b.go was not re-reviewed
    assert ("error is swallowed", "fixed") in fixed


def test_a_re_review_never_closes_a_security_finding_or_a_blocker(eng, ledger_project):
    """N3: silence from a re-review is not proof for those: only a check closes them."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p)
    governed._record_review_comments(eng, root, p, {
        "review_summary": "FINDING: BLOCKER src/a.go:3 — panics on empty body\n"
                          "FINDING: MAJOR src/a.go:9 — retries never back off\nVERDICT: REFUTED",
        "security_summary": "FINDING: MAJOR src/a.go:20 — token logged\nVERDICT: ISSUES",
        "impl_files": ["src/a.go"]})
    governed._record_review_comments(eng, root, p, {
        "review_summary": "VERDICT: SURVIVED", "security_summary": "VERDICT: CLEAN",
        "impl_files": ["src/a.go"]})
    with eng.begin() as c:
        status = dict(c.execute(text("SELECT title, status FROM findings WHERE project_id = :p"),
                                {"p": p}).all())
    assert status == {"panics on empty body": "open", "retries never back off": "fixed",
                      "token logged": "open"}


def test_an_edit_only_item_is_a_minor_human_row_not_a_major_gap(eng, ledger_project):
    """N4: hot reload (Northgate AC-11/AC-12) cannot be proven without editing a file yet."""
    p = ledger_project
    with eng.begin() as c:
        root = _run(c, p, spec=SPEC)
        rec = {"verdict": "not_met", "items": [{"n": 1, "text": "Unauthenticated requests get 401",
                                               "status": "human",
                                               "evidence": "needs a person: edit-then-test"}]}
        al.observe_check(c, project_id=p, lineage=root, check={"id": None}, record=rec)
        row = al.open_for(c, project_id=p)[0]
    assert row["kind"] == "acceptance_gap" and row["severity"] == "minor"
    assert "needs a person" in row["title"]
