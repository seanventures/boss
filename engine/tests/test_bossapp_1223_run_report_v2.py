"""AA-H (BOSSAPP-1223): One merged run report v2, merge-card payload, I4 & refresh_run_report.

Covers:
  - v2 report order: Verdict banner -> Must-haves (missing -> partial -> human -> met) ->
    Open gaps & tickets -> Details divider -> What it is / What shipped / How it was checked /
    What to do next / Problems during the run / Run facts
  - v1 fallback renders legacy runs without acceptance data cleanly
  - Invariant I4: no surface (report banner, card payload, card_words, ticket_comment) says
    "passed" or "Accepted" while open >= MAJOR ledger rows or open contract items exist
  - `acceptance_gate.merge_card_checks` prefers `layer='step'` over `layer='build_gate'`
  - `acceptance.report.refresh_run_report` updates the single `workflow_report` artifact IN PLACE
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import acceptance_gate as ag
from boss_engine.services import run_retrospective as retro
from boss_engine.services.acceptance import checks as acc_checks
from boss_engine.services.acceptance import ledger as acc_ledger
from boss_engine.services.acceptance import report as acc_report

from .test_a_run_reports_on_itself import REPORT as BASE_V1_REPORT
from .test_workflow_capability_runner import _publish, _start

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT status FROM acceptance_checks LIMIT 1"))
            c.execute(text("SELECT 1 FROM findings LIMIT 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {type(exc).__name__}")
    return engine


@pytest.fixture()
def proj(eng):
    pid = f"p_aah_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if not wid:
            pytest.skip("no workspace in test DB")
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :i, :i)"),
            {"i": pid, "w": wid},
        )
    yield pid
    with eng.begin() as c:
        for tbl in ("events", "receipts", "decisions", "artifacts", "jobs",
                    "work_tracker_bindings", "work_items", "workflow_step_runs",
                    "workflow_runs", "workflow_versions", "workflow_definitions"):
            c.execute(text(f"DELETE FROM {tbl} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


class _Settings:
    boss_edition = "community"

    def __init__(self, artifact_root):
        self.artifact_root = artifact_root


def _sample_check(verdict: str = "not_met") -> dict:
    return {
        "id": "acc_demo001",
        "layer": "step",
        "mode": "exercise",
        "head_sha": "92f7785abcdef",
        "verdict": verdict,
        "counts": {"met": 1, "partial": 1, "missing": 1, "human": 1, "unjudged": 0},
        "diff": {"newly_met": ["AC-4"], "regressed": ["AC-2"], "still_open": ["AC-1"]},
        "contract": {"id": "acn_1", "version": 1, "items_count": 4,
                     "sources_label": "PROJ-142 + PRD v1"},
        "commands": [
            {"id": "E1", "kind": "exec", "exit": 0, "cmd": "make test"},
            {"id": "E2", "kind": "http", "status": 401, "cmd": "HTTP GET http://127.0.0.1:8081/pay"},
        ],
        "budget": {"usd": 0.42, "limit_usd": 0.60, "minutes": 3.1, "exhausted": False},
        "items": [
            {"n": 4, "id": "AC-4", "text": "Health endpoint returns 200", "status": "met",
             "evidence": "E1 passed", "replayed": True},
            {"n": 3, "id": "AC-3", "text": "Visual brand review", "status": "human",
             "evidence": "needs a person"},
            {"n": 2, "id": "AC-2", "text": "Hot reload rejects bad config", "status": "partial",
             "evidence": "reloads without lint"},
            {"n": 1, "id": "AC-1", "text": "Built as Go binary", "status": "missing",
             "evidence": "no go.mod"},
        ],
    }


def _sample_ledger() -> list[dict]:
    return [
        {
            "id": "fnd_1", "number": 1, "key": "F-1", "kind": "acceptance_gap",
            "severity": "blocker", "status": "open", "contract_item_id": "AC-1",
            "fingerprint": "ac:AC-1", "title": "Built as Go binary missing",
            "followup_run_id": "crun_fu1",
            "ticket": {"key": "KAN-101", "url": "https://tickets.test/browse/KAN-101"},
        },
        {
            "id": "fnd_2", "number": 2, "key": "F-2", "kind": "review",
            "severity": "major", "status": "open",
            "fingerprint": "rv:abc", "title": "Nil pointer on empty config",
            "ticket": {"key": "KAN-102", "url": "https://tickets.test/browse/KAN-102"},
        },
    ]


def test_run_report_v2_leads_with_verdict_must_haves_gaps_then_details():
    check = _sample_check("not_met")
    ledger_rows = _sample_ledger()
    report = {
        **BASE_V1_REPORT,
        "report_version": 2,
        "acceptance": check,
        "ledger": ledger_rows,
        "next_steps": retro.next_steps(BASE_V1_REPORT["product"], [], ledger_rows=ledger_rows),
    }
    md = retro.workflow_markdown(report)

    # Exact v2 section order: Verdict -> Must-haves -> Open gaps -> Details -> What it is -> How it was checked.
    i_verdict = md.index("> VERDICT: ✗ Not accepted")
    i_checked = md.index("Checked exercise at `92f7785` against PROJ-142 + PRD v1")
    i_mh = md.index("## Must-haves")
    i_gaps = md.index("## Open gaps & tickets")
    i_details = md.index("*Details: what was done*")
    i_what = md.index("## What it is")
    i_how = md.index("## How it was checked")
    i_next = md.index("## What to do next")
    i_facts = md.index("## Run facts")

    assert i_verdict < i_checked < i_mh < i_gaps < i_details < i_what < i_how < i_next < i_facts
    assert "since last check: +1 met, 1 regressed" in md
    assert "KAN-101" in md and "crun_fu1" in md
    assert "`E1` (exit 0) `make test`" in md
    assert "`E2` (HTTP 401)" in md
    assert "1 item(s) replayed" in md
    # v2 replaces v1's "Does it work" and unverified criteria warning.
    assert "## Does it work" not in md
    assert "Not verified against the build" not in md

    # Must-haves rows ordered missing -> partial -> human -> met.
    mh_block = md[i_mh:i_gaps]
    assert mh_block.index("| AC-1 |") < mh_block.index("| AC-2 |")
    assert mh_block.index("| AC-2 |") < mh_block.index("| AC-3 |")
    assert mh_block.index("| AC-3 |") < mh_block.index("| AC-4 |")


def test_run_report_v1_fallback_still_renders_old_runs():
    legacy = {**BASE_V1_REPORT, "next_steps": retro.next_steps(BASE_V1_REPORT["product"], [])}
    md = retro.workflow_markdown(legacy)
    assert "## Does it work" in md
    assert "## Acceptance criteria" in md
    assert "Not verified against the build" in md
    assert "VERDICT:" not in md


def test_no_surface_says_passed_or_accepted_with_open_major_rows_i4():
    """Invariant I4: even if the check's own verdict were `met`, open >= MAJOR ledger rows
    prevent banner, card payload, card_words and ticket_comment from saying passed/accepted."""
    met_check = {
        "id": "acc_met1", "layer": "step", "mode": "exercise", "head_sha": "1112223",
        "verdict": "met",
        "counts": {"met": 2, "partial": 0, "missing": 0, "human": 0, "unjudged": 0},
        "items": [
            {"n": 1, "id": "AC-1", "text": "Item 1", "status": "met", "evidence": "E1"},
            {"n": 2, "id": "AC-2", "text": "Item 2", "status": "met", "evidence": "E2"},
        ],
    }
    open_major = [
        {"id": "fnd_m", "number": 7, "key": "F-7", "kind": "review",
         "severity": "major", "status": "open", "title": "SQL injection in search"},
    ]

    banner = acc_report.verdict_banner(met_check, open_rows=open_major)
    assert banner["accepted"] is False
    assert "Not accepted" in banner["headline"]
    assert "✓ Accepted" not in banner["headline"]

    payload = acc_report.card_payload(met_check, open_major)
    assert payload["accepted"] is False
    assert payload["verdict"] == "not_met"

    words = ag.card_words(None, payload, "passed")
    assert "not accepted" in words.lower()
    assert not words.lower().startswith("automated checks: passed")

    comment = acc_report.ticket_comment(met_check, open_major)
    assert "Not accepted" in comment
    assert "✓ Accepted" not in comment

    # When zero open blocking rows exist, all surfaces cleanly report accepted.
    clean_banner = acc_report.verdict_banner(met_check, open_rows=[])
    assert clean_banner["accepted"] is True
    assert "✓ Accepted" in clean_banner["headline"]


def test_merge_card_prefers_step_layer_over_build_gate_on_same_head(eng, proj):
    build_id = f"crun_mc_{uuid.uuid4().hex[:8]}"
    pr_url = "https://github.com/acme/app/pull/42"
    sha = "deadbeef1234567"
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, title, status, pull_request_url, "
                "finalize_ctx, state) VALUES (:id, :p, 'Demo', 'completed', :pr, "
                "CAST(:fc AS JSONB), '{}'::jsonb)"
            ),
            {"id": build_id, "p": proj, "pr": pr_url,
             "fc": json.dumps({"root_run_id": build_id})},
        )
        # Inner static build_gate check says met.
        acc_checks.record_check(
            c, project_id=proj, lineage_root_run_id=build_id, code_task_run_id=build_id,
            layer="build_gate", mode="static", head_sha=sha, pr_url=pr_url,
            record={"verdict": "met", "counts": {"met": 1, "partial": 0, "missing": 0},
                    "items": [{"id": "AC-1", "n": 1, "text": "Works at runtime", "status": "met",
                               "evidence": "app.py"}]},
        )
        # Outer Acceptance Agent step check exercised it and found it partial.
        step_row = acc_checks.record_check(
            c, project_id=proj, lineage_root_run_id=build_id, code_task_run_id=build_id,
            layer="step", mode="exercise", head_sha=sha, pr_url=pr_url,
            record={"verdict": "not_met", "why": "runtime 500",
                    "counts": {"met": 0, "partial": 1, "missing": 0},
                    "items": [{"id": "AC-1", "n": 1, "text": "Works at runtime", "status": "partial",
                               "evidence": "E1 returned HTTP 500"}]},
        )
        _, acc = ag.merge_card_checks(
            c, run_id=build_id, evidence={}, checks_failing=None, pr_url=pr_url, head_sha=sha,
        )
        assert acc is not None
        assert acc["layer"] == "step"
        assert acc["mode"] == "exercise"
        assert acc["check_id"] == step_row["id"]
        assert acc["verdict"] == "not_met"


def test_refresh_run_report_updates_same_artifact_in_place(eng, proj, tmp_path):
    build_id = f"crun_ref_{uuid.uuid4().hex[:8]}"
    cfg = _Settings(tmp_path)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, title, status, finalize_ctx, state) "
                "VALUES (:id, :p, 'Widget app', 'completed', CAST(:fc AS JSONB), '{}'::jsonb)"
            ),
            {"id": build_id, "p": proj, "fc": json.dumps({"root_run_id": build_id})},
        )
        _, ver = _publish(
            c, proj, f"rep_wf_{uuid.uuid4().hex[:6]}",
            [{"id": "rep", "kind": "capability",
              "binding": {"capability": "report", "inputs": {}}, "next": []}],
            "rep",
        )
        wf = _start(c, proj, ver["id"], {"directive": "Build widget app"})

        # First publish (no acceptance check yet -> v1).
        first = acc_report.refresh_run_report(c, proj, wf["id"], settings=cfg)
        assert first["ok"] is True and first["created"] is True
        assert first["report_version"] == 1
        art_id = first["artifact_id"]

        # Now an acceptance check lands AFTER the report step -> refresh updates SAME artifact!
        acc_checks.record_check(
            c, project_id=proj, lineage_root_run_id=build_id,
            workflow_run_id=wf["id"], code_task_run_id=build_id,
            layer="step", mode="exercise", head_sha="aaabbb1",
            record={"verdict": "not_met", "why": "missing export",
                    "counts": {"met": 1, "partial": 0, "missing": 1},
                    "items": [
                        {"id": "AC-1", "n": 1, "text": "Create widget", "status": "met", "evidence": "E1"},
                        {"id": "AC-2", "n": 2, "text": "Export CSV", "status": "missing", "evidence": "absent"},
                    ]},
        )
        acc_ledger.upsert(
            c, project_id=proj, lineage=build_id, kind="acceptance_gap", severity="major",
            fingerprint="ac:AC-2", title="Export CSV missing", contract_item_id="AC-2",
            stage="acceptance_step",
        )

        second = acc_report.refresh_run_report(c, proj, wf["id"], settings=cfg)
        assert second["ok"] is True
        assert second["created"] is False
        assert second["artifact_id"] == art_id
        assert second["report_version"] == 2

        count = c.execute(
            text("SELECT count(*) FROM artifacts WHERE project_id = :p "
                 "AND artifact_type = 'workflow_report' AND content->>'run_id' = :r"),
            {"p": proj, "r": wf["id"]},
        ).scalar()
        assert count == 1
        content = c.execute(text("SELECT content FROM artifacts WHERE id = :a"), {"a": art_id}).scalar()
        assert isinstance(content, dict)
        assert "> VERDICT: ✗ Not accepted" in content["markdown"]
        assert "## Must-haves" in content["markdown"]
        assert "## Open gaps & tickets" in content["markdown"]
