"""AA-J (BOSSAPP-1225): Ticket filing, status sync (round-2 closes 3), and comment on change.

Covers:
  - BLOCKER / MAJOR / acceptance_gap auto-filed as child of source ticket with PR link at TOP
    and labels `["boss-acceptance", severity]`, guarded by Receipts (idempotent on replay)
  - MINOR batched into ONE `"Polish: <title>"` ticket updated in place; NIT stays ledger-only
  - Per-check cap (`max_new_tickets`) overflows into ONE digest ticket
  - Per-project switch (`auto_file_acceptance_findings=False` / `policy.file='ledger_only'`) and
    no-tracker projects fall back to BOSS work items only
  - Round-2 replay closes exact 3 tickets (comment + transition `done`), regression reopens (`todo`),
    waived comments reason without closing, duplicate comments `"Merged into F-n"`
  - `ticket_sync.on_event("acceptance.checked", ...)` posts verdict comment ONLY on change
"""
from __future__ import annotations

import base64
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault(
    "BOSS_MASTER_KEY",
    base64.b64encode(b"aaj-test-master-key-32-bytes!!!!").decode(),
)

from boss_engine.connectors.tickets import http as ticket_http  # noqa: E402
from boss_engine.identity import vault  # noqa: E402
from boss_engine.services import ticket_sync  # noqa: E402
from boss_engine.services.acceptance import checks as acc_checks  # noqa: E402
from boss_engine.services.acceptance import contract as acc_contract  # noqa: E402
from boss_engine.services.acceptance import filing as acc_filing  # noqa: E402
from boss_engine.services.acceptance import ledger as acc_ledger  # noqa: E402
from boss_engine.services.acceptance import policy as acc_policy  # noqa: E402
from boss_engine.services.acceptance import report as acc_report  # noqa: E402

from .ticket_connectors import dataset as D  # noqa: E402
from .ticket_connectors.fake_jira import CLIENT_ID, CLIENT_SECRET, EMAIL, TOKEN, FakeJira  # noqa: E402

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"),
)
SOURCE_KEY = f"KAN-{D.STORY}"


class _Settings:
    boss_edition = "community"


SETTINGS = _Settings()


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM work_tracker_bindings LIMIT 1"))
            c.execute(text("SELECT 1 FROM findings LIMIT 1"))
            c.execute(text("SELECT status FROM acceptance_checks LIMIT 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {type(exc).__name__}")
    vault.reset_master_key_cache()
    return engine


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    monkeypatch.setenv("BOSS_TICKET_SYNC", "off")


@pytest.fixture()
def fake(monkeypatch):
    server = FakeJira()
    real = ticket_http._urllib_transport

    def route(method, url, headers, data, timeout, context):
        for host in ("https://auth.atlassian.com", "https://api.atlassian.com"):
            if url.startswith(host):
                url = server.base + url[len(host):]
        return real(method, url, headers, data, timeout, context)

    monkeypatch.setattr(ticket_http, "TRANSPORT", route)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    yield server
    server.close()


def _jira_cfg(fake, **settings):
    return {
        "base_url": fake.base, "email": EMAIL, "project_key": "KAN", "api_token": TOKEN,
        "oauth_client_id": CLIENT_ID, "oauth_client_secret": CLIENT_SECRET,
        "settings": settings, "ticket_system": True,
    }


@pytest.fixture()
def world(eng, fake):
    pid = f"p_aaj_{uuid.uuid4().hex[:8]}"
    conn_id = f"trk_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if not wid:
            pytest.skip("no workspace in test DB")
        c.execute(
            text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:i, :w, :i, :i)"),
            {"i": pid, "w": wid},
        )
        c.execute(
            text(
                "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
                "status, is_default, configuration, created_by) VALUES (:id, :p, 'jira', 'Jira', "
                "'active', true, CAST(:c AS JSONB), 'boss')"
            ),
            {"id": conn_id, "p": pid, "c": json.dumps(_jira_cfg(fake))},
        )
    yield {"pid": pid, "conn_id": conn_id, "fake": fake}
    with eng.begin() as c:
        for tbl in ("events", "receipts", "decisions", "artifacts", "jobs",
                    "work_tracker_bindings", "work_items"):
            c.execute(text(f"DELETE FROM {tbl} WHERE project_id = :p"), {"p": pid})
        c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})


def _seed_run(c, pid: str, conn_id: str, fake: FakeJira) -> str:
    rid = f"crun_aaj_{uuid.uuid4().hex[:8]}"
    ticket = {
        "provider": "jira", "connection_id": conn_id, "key": SOURCE_KEY,
        "id": str(D.STORY), "url": f"{fake.base}/browse/{SOURCE_KEY}",
        "title": "Northgate gateway",
    }
    c.execute(
        text(
            "INSERT INTO code_task_runs (id, project_id, title, status, "
            "pull_request_url, spec, finalize_ctx, state) VALUES (:id, :p, 'Northgate gateway', "
            "'completed', 'https://github.com/acme/northgate/pull/99', "
            "'{}'::jsonb, CAST(:fc AS JSONB), '{}'::jsonb)"
        ),
        {"id": rid, "p": pid, "fc": json.dumps({"root_run_id": rid, "branch": "feat/northgate", "ticket": ticket})},
    )
    return rid


def test_gaps_filed_with_pr_at_top_idempotent_receipts_minors_batched_nits_skipped_cap_digest(
    eng, world,
):
    pid, conn_id, fake = world["pid"], world["conn_id"], world["fake"]
    pr_url = "https://github.com/acme/northgate/pull/99"
    with eng.begin() as c:
        lineage = _seed_run(c, pid, conn_id, fake)
        # Create 4 BLOCKER/MAJOR gaps + 2 MINOR reviews + 1 NIT review.
        for n in range(1, 5):
            acc_ledger.upsert(
                c, project_id=pid, lineage=lineage, kind="acceptance_gap",
                severity="blocker" if n == 1 else "major",
                fingerprint=f"ac:AC-{n}", title=f"Must-have AC-{n} missing",
                body=f"Deliver requirement AC-{n} completely",
                evidence=[{"kind": "file", "ref": f"src/mod_{n}.go", "why": "absent"}],
                verify_with=[{"action": {"argv": ["go", "test", "./..."]}, "expect": {"exit": 0}}],
                contract_item_id=f"AC-{n}", stage="acceptance_step", pr_url=pr_url,
            )
        for m in range(1, 3):
            acc_ledger.upsert(
                c, project_id=pid, lineage=lineage, kind="review", severity="minor",
                fingerprint=f"rv:minor-{m}", title=f"Minor polish item {m}",
                path=f"src/p{m}.go", line=10 + m, stage="review_code", pr_url=pr_url,
            )
        acc_ledger.upsert(
            c, project_id=pid, lineage=lineage, kind="review", severity="nit",
            fingerprint="rv:nit-1", title="Nit formatting",
            path="src/fmt.go", line=3, stage="review_code", pr_url=pr_url,
        )

        # Cap at 3 individual tickets -> 3 individual + 1 digest (overflow of 1) + 1 Polish batch!
        pol = acc_policy.AcceptancePolicy.from_mapping({"max_new_tickets": 3}, profile="balanced")
        src_ticket = {"provider": "jira", "key": SOURCE_KEY, "url": f"{fake.base}/browse/{SOURCE_KEY}"}
        res1 = acc_filing.file_and_sync(
            c, SETTINGS, project_id=pid, lineage=lineage, policy=pol,
            pr_url=pr_url, branch="feat/northgate", run_url="https://boss.local/runs/1",
            source_ticket=src_ticket, lineage_title="Northgate gateway",
        )
        assert res1.mode == "synced"
        assert len(res1.filed) == 3
        assert res1.digest_ticket is not None and res1.digest_ticket["count"] == 1
        assert len(res1.batched_minors) == 2
        assert res1.batched_minors[0]["batch_ticket_key"] == res1.batched_minors[1]["batch_ticket_key"]
        assert any(s["reason"] == "nit is ledger-only" for s in res1.skipped)

        # Check PR link at the TOP of the filed ticket body (tickets-must-link-the-work rule).
        first_key = res1.filed[0]["key"]
        issue_obj = fake.issues[first_key]
        body_doc = issue_obj.get("description") or issue_obj.get("body") or ""
        first_block = body_doc["content"][0] if isinstance(body_doc, dict) else str(body_doc).splitlines()[0]
        assert pr_url in str(first_block)
        assert "Done when" in str(body_doc)
        assert "go test ./..." in str(body_doc)
        assert "boss-acceptance" in (issue_obj.get("labels") or [])

        total_issues_after_1 = len(fake.issues)

        # Idempotent replay: calling file_and_sync again creates ZERO new tickets.
        res2 = acc_filing.file_and_sync(
            c, SETTINGS, project_id=pid, lineage=lineage, policy=pol,
            pr_url=pr_url, branch="feat/northgate", source_ticket=src_ticket,
            lineage_title="Northgate gateway",
        )
        assert len(res2.filed) == 0
        assert len(fake.issues) == total_issues_after_1


def test_round_2_replay_closes_three_tickets_and_regression_reopens(eng, world):
    """AA-J done-when gate: round 1 opens 5 gaps -> files 5 tickets; round 2 fixes 3 ->
    closes exact 3 tickets with check comment; round 3 regresses 1 -> reopens it to todo."""
    pid, conn_id, fake = world["pid"], world["conn_id"], world["fake"]
    pr_url = "https://github.com/acme/northgate/pull/100"
    with eng.begin() as c:
        lineage = _seed_run(c, pid, conn_id, fake)
        items_spec = [
            {"id": f"AC-{n}", "n": n, "text": f"Requirement {n}", "source": "must_have"}
            for n in range(1, 6)
        ]
        contract_row = acc_contract.freeze(
            c, project_id=pid, root_run_id=lineage,
            sources=acc_contract.gather_sources({
                "instructions": "Must-haves:\n" + "\n".join(f"- Requirement {n}" for n in range(1, 6)),
            }),
        )
        # Round 1: all 5 items missing.
        rec1 = {
            "verdict": "not_met", "why": "5 missing",
            "counts": {"met": 0, "partial": 0, "missing": 5},
            "items": [{**it, "status": "missing", "evidence": "absent"} for it in items_spec],
        }
        chk1 = acc_checks.record_check(
            c, project_id=pid, lineage_root_run_id=lineage, contract_id=contract_row["id"],
            code_task_run_id=lineage, layer="step", mode="exercise", head_sha="aaa1111",
            pr_url=pr_url, record=rec1,
        )
        delta1 = acc_ledger.observe_check(
            c, project_id=pid, lineage=lineage, check=chk1, record=rec1,
            contract=contract_row, run_id=lineage, stage="acceptance_step", pr_url=pr_url,
        )
        assert len(delta1.opened) == 5

        pol = acc_policy.AcceptancePolicy.from_mapping({}, profile="balanced")
        src_ticket = {"provider": "jira", "key": SOURCE_KEY, "url": f"{fake.base}/browse/{SOURCE_KEY}"}
        f1 = acc_filing.file_and_sync(
            c, SETTINGS, project_id=pid, lineage=lineage, delta=delta1, check=chk1,
            policy=pol, pr_url=pr_url, source_ticket=src_ticket, lineage_title="Northgate",
        )
        assert len(f1.filed) == 5
        key_by_finding = {x["finding_id"]: x["key"] for x in f1.filed}

        # Round 2: AC-1, AC-2, AC-3 are now MET with verified evidence; AC-4, AC-5 still missing.
        r2_items = [
            {**it, "status": "met" if it["n"] <= 3 else "missing",
             "evidence": f"E{it['n']} exited 0" if it["n"] <= 3 else "still absent"}
            for it in items_spec
        ]
        rec2 = {"verdict": "not_met", "why": "2 missing",
                "counts": {"met": 3, "partial": 0, "missing": 2}, "items": r2_items}
        chk2 = acc_checks.record_check(
            c, project_id=pid, lineage_root_run_id=lineage, contract_id=contract_row["id"],
            code_task_run_id=lineage, layer="step", mode="exercise", head_sha="bbb2222",
            pr_url=pr_url, record=rec2,
        )
        assert len(chk2["diff"]["newly_met"]) == 3
        delta2 = acc_ledger.observe_check(
            c, project_id=pid, lineage=lineage, check=chk2, record=rec2,
            contract=contract_row, run_id=lineage, stage="acceptance_step", pr_url=pr_url,
        )
        assert len(delta2.fixed) == 3

        f2 = acc_filing.file_and_sync(
            c, SETTINGS, project_id=pid, lineage=lineage, delta=delta2, check=chk2,
            policy=pol, pr_url=pr_url, source_ticket=src_ticket, lineage_title="Northgate",
        )
        assert len(f2.closed) == 3
        closed_keys = {x["key"] for x in f2.closed}
        for key in closed_keys:
            assert fake.issues[key]["status"] == "Done"
            assert any(chk2["id"] in str(cm) for cm in fake.comments[key])

        # The other 2 tickets remain open (To Do).
        still_open_keys = set(key_by_finding.values()) - closed_keys
        assert len(still_open_keys) == 2
        for key in still_open_keys:
            assert fake.issues[key]["status"] == "To Do"

        # Round 3: AC-1 regresses to missing -> reopened in ledger and transitioned back to To Do!
        r3_items = [
            {**it, "status": "met" if it["n"] in (2, 3) else "missing",
             "evidence": "regressed" if it["n"] == 1 else ("ok" if it["n"] in (2, 3) else "absent")}
            for it in items_spec
        ]
        rec3 = {"verdict": "not_met", "why": "regression",
                "counts": {"met": 2, "partial": 0, "missing": 3}, "items": r3_items}
        chk3 = acc_checks.record_check(
            c, project_id=pid, lineage_root_run_id=lineage, contract_id=contract_row["id"],
            code_task_run_id=lineage, layer="step", mode="exercise", head_sha="ccc3333",
            pr_url=pr_url, record=rec3,
        )
        delta3 = acc_ledger.observe_check(
            c, project_id=pid, lineage=lineage, check=chk3, record=rec3,
            contract=contract_row, run_id=lineage, stage="acceptance_step", pr_url=pr_url,
        )
        assert len(delta3.reopened) == 1
        f3 = acc_filing.file_and_sync(
            c, SETTINGS, project_id=pid, lineage=lineage, delta=delta3, check=chk3,
            policy=pol, pr_url=pr_url, source_ticket=src_ticket, lineage_title="Northgate",
        )
        assert len(f3.reopened) == 1
        reopened_key = f3.reopened[0]["key"]
        assert fake.issues[reopened_key]["status"] == "To Do"
        assert any("Reopened" in str(cm) for cm in fake.comments[reopened_key])


def test_per_project_switch_off_keeps_findings_in_boss_work_items_only(eng, world):
    pid, conn_id, fake = world["pid"], world["conn_id"], world["fake"]
    before_issues = len(fake.issues)
    with eng.begin() as c:
        c.execute(
            text("UPDATE work_tracker_providers SET configuration = CAST(:c AS JSONB) WHERE id = :id"),
            {"id": conn_id, "c": json.dumps(_jira_cfg(fake, auto_file_acceptance_findings=False))},
        )
        lineage = _seed_run(c, pid, conn_id, fake)
        row, _ = acc_ledger.upsert(
            c, project_id=pid, lineage=lineage, kind="acceptance_gap", severity="blocker",
            fingerprint="ac:AC-1", title="Missing auth", stage="acceptance_step",
        )
        res = acc_filing.file_and_sync(
            c, SETTINGS, project_id=pid, lineage=lineage,
            policy=acc_policy.AcceptancePolicy.from_mapping({}, profile="balanced"),
        )
        assert res.mode == "boss_work_items_only"
        assert len(fake.issues) == before_issues
        refreshed = acc_ledger.get(c, row["id"])
        assert refreshed is not None and refreshed["work_item_id"] is not None


def test_ticket_sync_acceptance_comment_posts_only_on_change(eng, world):
    """LLD §7 & AA-J: `acceptance.checked` posts a compact verdict comment on the source ticket,
    and re-running with the SAME verdict + item statuses does NOT post a second comment."""
    pid, conn_id, fake = world["pid"], world["conn_id"], world["fake"]
    with eng.begin() as c:
        lineage = _seed_run(c, pid, conn_id, fake)
    before = len(fake.comments[SOURCE_KEY])

    check_v1 = {
        "id": "acc_sync1", "layer": "step", "mode": "exercise", "head_sha": "aaa1111",
        "verdict": "not_met", "counts": {"met": 1, "partial": 0, "missing": 1},
        "items": [
            {"id": "AC-1", "n": 1, "text": "Item 1", "status": "met", "evidence": "E1"},
            {"id": "AC-2", "n": 2, "text": "Item 2", "status": "missing", "evidence": "absent"},
        ],
    }
    data_v1 = {
        "check_id": "acc_sync1", "code_task_run_id": lineage, "lineage_root_run_id": lineage,
        "verdict": "not_met", "digest": acc_report.comment_digest(check_v1),
        "comment_markdown": acc_report.ticket_comment(check_v1, report_url="https://boss.local/r/1"),
    }
    # First event posts comment.
    out1 = ticket_sync.on_event(
        eng, SETTINGS, "acceptance.checked", "acceptance_check", "acc_sync1", data_v1,
    )
    assert out1["synced"] is True
    assert len(fake.comments[SOURCE_KEY]) == before + 1
    assert "Not accepted" in str(fake.comments[SOURCE_KEY][-1]["body"])

    # Second event with SAME verdict & item statuses (even a new check_id!) -> receipt deduped!
    out2 = ticket_sync.on_event(
        eng, SETTINGS, "acceptance.checked", "acceptance_check", "acc_sync2",
        {**data_v1, "check_id": "acc_sync2"},
    )
    assert out2["effects"][0].get("replayed") is True
    assert len(fake.comments[SOURCE_KEY]) == before + 1

    # Third event with CHANGED status (AC-2 now met) -> posts updated comment!
    check_v2 = {
        "id": "acc_sync3", "layer": "step", "mode": "exercise", "head_sha": "bbb2222",
        "verdict": "met", "counts": {"met": 2, "partial": 0, "missing": 0},
        "items": [
            {"id": "AC-1", "n": 1, "text": "Item 1", "status": "met", "evidence": "E1"},
            {"id": "AC-2", "n": 2, "text": "Item 2", "status": "met", "evidence": "E2"},
        ],
    }
    data_v2 = {
        "check_id": "acc_sync3", "code_task_run_id": lineage, "lineage_root_run_id": lineage,
        "verdict": "met", "digest": acc_report.comment_digest(check_v2),
        "comment_markdown": acc_report.ticket_comment(check_v2, report_url="https://boss.local/r/1"),
    }
    out3 = ticket_sync.on_event(
        eng, SETTINGS, "acceptance.checked", "acceptance_check", "acc_sync3", data_v2,
    )
    assert out3["synced"] is True
    assert len(fake.comments[SOURCE_KEY]) == before + 2
    assert "Accepted" in str(fake.comments[SOURCE_KEY][-1]["body"])
