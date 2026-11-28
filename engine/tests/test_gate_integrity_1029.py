"""BOSSAPP-1029: Gate integrity — the last gate run must execute on the exact commit that lands,
the receipt carries the SHA, and any repair edit after the last test run invalidates the prior
pass until re-verified on the exact landing commit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.schemas import FileChange
from boss_engine.services import gate_integrity, pr_description
from boss_engine.services.decisions import DecisionService


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "textkit"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "boss@example.com")
    _git(repo, "config", "user.name", "BOSS")
    (repo / "words.py").write_text(
        "def first_words(text, n):\n    return ' '.join(text.split()[:n])\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_repair_edit_after_last_test_run_invalidates_prior_pass(tmp_path):
    """BOSSAPP-1029 enforcing test: editing a file after the last test run invalidates the
    prior gate pass until verification is re-run on the exact landing tree."""
    repo = _init_repo(tmp_path)
    tracker = gate_integrity.GateVerificationTracker()

    # 1) Authoritative verification passes on the clean tree.
    changes_v1 = [("words.py", (repo / "words.py").read_text())]
    tracker.record_verification(
        root=repo, changes=changes_v1, passed=True,
        command="python3 -m unittest discover", exit_code=0,
    )
    assert tracker.verified_passed is True
    assert tracker.verified_tree_sha
    assert tracker.check_integrity(root=repo, changes=changes_v1)["ok"] is True

    # 2) A late repair edit breaks first_words AFTER the last test run.
    broken = "def first_words(text, n):\n    return text[:n]\n"
    (repo / "words.py").write_text(broken)
    changes_v2 = [("words.py", broken)]

    check = tracker.check_integrity(root=repo, changes=changes_v2)
    assert check["ok"] is False
    assert check["invalidated"] is True
    assert tracker.invalidated is True
    assert tracker.verified_passed is False
    assert "working tree changed" in (check["reason"] or "")

    # 3) Landing boundary refuses the mutated tree without a re-run.
    with pytest.raises(gate_integrity.GateIntegrityError):
        gate_integrity.verify_landing_integrity(
            expected_changes_digest=tracker.verified_changes_digest,
            landing_changes=changes_v2,
            gate_passed=True,
            invalidated=tracker.invalidated,
            invalidation_reason=tracker.invalidation_reason,
        )

    # 4) Re-running verification on the exact fixed tree restores integrity with the new SHA.
    fixed = "def first_words(text, n):\n    return ' '.join(text.split()[:n])\n"
    (repo / "words.py").write_text(fixed)
    changes_v3 = [("words.py", fixed)]
    tracker.record_verification(
        root=repo, changes=changes_v3, passed=True,
        command="python3 -m unittest discover", exit_code=0,
    )
    assert tracker.invalidated is False
    assert tracker.verified_passed is True
    assert tracker.check_integrity(root=repo, changes=changes_v3)["ok"] is True


def test_untracked_or_deleted_file_after_verify_also_invalidates_pass(tmp_path):
    repo = _init_repo(tmp_path)
    tracker = gate_integrity.GateVerificationTracker()
    tracker.record_verification(root=repo, changes=[], passed=True)
    assert tracker.check_integrity(root=repo, changes=[])["ok"] is True

    # Untracked file added after tests ran.
    (repo / "stray.py").write_text("BROKEN = (\n")
    res = tracker.check_integrity(root=repo, changes=[("stray.py", "BROKEN = (\n")])
    assert res["ok"] is False and res["invalidated"] is True


def test_factory_refuses_repair_edit_after_last_test_run_and_records_sha_receipt_when_clean(monkeypatch):
    """Proves at the SoftwareFactoryService boundary:
    (a) a post-verify edit cannot reach the branch;
    (b) an intact verified tree lands with a `quality_gate.verify` receipt carrying the exact SHA.
    """
    from .test_pr_test_evidence_1011 import _FakeGitHubHost, _make_factory

    verified_files = [FileChange(path="textkit/words.py", content="def first_words(s, n): return ' '.join(s.split()[:n])\n")]
    verified_digest = gate_integrity.compute_changes_digest([(f.path, f.content) for f in verified_files])
    ev = {
        "tests": [{"command": "python3 -m unittest discover", "ok": True, "exit_code": 0}],
        "tests_passed": True,
        "last_verify_output": "Ran 3 tests in 0.001s\n\nOK\n",
    }
    summary = pr_description.render(
        title="Word count", evidence=ev, gates={"written_tests_pass": True},
        paths=["textkit/words.py"], host_kind="github",
    )

    # (a) Repair edit mutates words.py after the last test run -> REFUSED before commit/PR!
    host_bad = _FakeGitHubHost(kind="github")
    sf_bad, _ = _make_factory(monkeypatch, host_bad)
    mutated_files = [FileChange(path="textkit/words.py", content="def first_words(s, n): return s[:n]\n")]
    bad_res = sf_bad.propose_pull_request(
        object(), "proj_1", "task_1", reservation_id="res_1",
        dedupe_key="code:task:crun_bad", title="Word count",
        branch_name="boss/code/crun_bad", summary=summary,
        files=mutated_files, evidence=ev, quality_gate_id="qg_1", run_id="crun_bad",
        require_test_evidence=True, verified_changes_digest=verified_digest,
        verified_tree_sha="a" * 40,
    )
    assert bad_res["status"] == "refused"
    assert bad_res["error"] == "gate_integrity_violation"
    assert host_bad.files == {}, "mutated file must never be committed to branch"
    assert host_bad.prs == [], "mutated tree must never open a PR"
    assert any(c.args[2] == "software_factory.gate_integrity_refused"
               for c in sf_bad.events.emit.call_args_list)

    # (b) Exact verified tree lands -> `quality_gate.verify` receipt carries exact landed commit SHA!
    host_ok = _FakeGitHubHost(kind="github")
    host_ok.head_sha = "723533757b" + "0" * 30
    sf_ok, _ = _make_factory(monkeypatch, host_ok)
    ok_res = sf_ok.propose_pull_request(
        object(), "proj_1", "task_1", reservation_id="res_2",
        dedupe_key="code:task:crun_ok", title="Word count",
        branch_name="boss/code/crun_ok", summary=summary,
        files=verified_files, evidence=ev, quality_gate_id="qg_2", run_id="crun_ok",
        require_test_evidence=True, verified_changes_digest=verified_digest,
        verified_tree_sha="b" * 40,
    )
    assert ok_res["status"] == "created"
    assert ok_res["verified_commit_sha"] == host_ok.head_sha
    assert ok_res["gate_receipt_id"]
    gate_receipts = [r for r in sf_ok.receipts.by_id.values() if r["effect_kind"] == "quality_gate.verify"]
    assert len(gate_receipts) == 1
    assert gate_receipts[0]["status"] == "succeeded"
    assert gate_receipts[0]["provider_ref"] == f"commit:{host_ok.head_sha}"
    assert gate_receipts[0]["result"]["commit_sha"] == host_ok.head_sha
    assert gate_receipts[0]["result"]["verified_tree_sha"] == "b" * 40
    assert gate_receipts[0]["result"]["verified_changes_digest"] == verified_digest


def test_decisions_merge_gate_blocks_when_verified_sha_mismatches_landing_head():
    """BOSSAPP-1029: DecisionService._pull_request_ci_gate blocks merge if the landing head SHA
    diverges from the quality gate's verified_commit_sha or the test report's head_sha."""
    url = os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("BOSS_TEST_DATABASE_URL not configured")
    eng = create_engine(url, future=True)
    with eng.connect() as conn:
        tx = conn.begin()
        try:
            project_id = "proj_gate_1029"
            org_id = "org_gate_1029"
            ws_id = "ws_gate_1029"
            run_id = "crun_gate_1029"
            qg_id = "qg_gate_1029"
            art_id = "art_gate_1029"
            verified_sha = "a" * 40
            drifted_sha = "b" * 40

            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'Test Org') ON CONFLICT DO NOTHING"), {"id": org_id})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'WS') ON CONFLICT DO NOTHING"),
                         {"id": ws_id, "o": org_id})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :w, 'P', 'p-1029') ON CONFLICT DO NOTHING"),
                         {"id": project_id, "w": ws_id})
            conn.execute(text(
                "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, finalize_ctx) "
                "VALUES (:id, :p, 'completed', 'done', 'Word count', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"
            ), {"id": run_id, "p": project_id})
            conn.execute(text(
                "INSERT INTO quality_gates (id, project_id, code_task_run_id, risk_tier, "
                "test_author_independent, reviewer_independent, tests_passed, adversarial_verdict, "
                "status, fail_closed, gates_passed, gates_failed, evidence) "
                "VALUES (:id, :p, :r, 2, true, true, true, 'survived', 'pass', true, "
                "CAST(:gp AS JSONB), CAST(:gf AS JSONB), CAST(:ev AS JSONB))"
            ), {
                "id": qg_id, "p": project_id, "r": run_id,
                "gp": json.dumps(["written_tests_pass", "existing_tests_intact"]),
                "gf": json.dumps([]),
                "ev": json.dumps({"verified_commit_sha": verified_sha, "gate_integrity_invalidated": False}),
            })
            conn.execute(text(
                "INSERT INTO artifacts (id, project_id, artifact_type, title, uri, content, content_hash) "
                "VALUES (:id, :p, 'test_report', 'Test report', 'artifact://t', CAST(:c AS JSONB), 'h1')"
            ), {
                "id": art_id, "p": project_id,
                "c": json.dumps({"status": "passed", "head_sha": verified_sha, "verified_commit_sha": verified_sha}),
            })

            svc = DecisionService(Settings())
            row_matching = {
                "id": "dec_ok",
                "project_id": project_id,
                "recommendation": {
                    "action": "review_pull_request",
                    "branch_name": f"boss/code/{run_id}",
                    "head_sha": verified_sha,
                    "test_report_artifact_id": art_id,
                },
            }
            assert svc._pull_request_ci_gate(conn, row_matching) is None

            # A post-gate commit moved HEAD to drifted_sha without re-running the gate!
            row_drifted = {
                "id": "dec_drift",
                "project_id": project_id,
                "recommendation": {
                    "action": "review_pull_request",
                    "branch_name": f"boss/code/{run_id}",
                    "head_sha": drifted_sha,
                    "test_report_artifact_id": art_id,
                },
            }
            blocked = svc._pull_request_ci_gate(conn, row_drifted)
            assert blocked is not None
            assert blocked["status"] == "blocked_gate_sha_mismatch"
            assert blocked["event_reason"] == "gate_sha_mismatch"
            assert verified_sha[:12] in blocked["reason"]
            assert drifted_sha[:12] in blocked["reason"]
        finally:
            tx.rollback()
    eng.dispose()
