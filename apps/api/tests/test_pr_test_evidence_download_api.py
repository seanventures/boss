"""BOSSAPP-1011 API tests: downloadable full per-case test results on /v1/artifacts/{id}/download,
change_review test_evidence payload, and deliverable_review download link.
"""
from __future__ import annotations

import json
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.app.services.core import make_id
from boss_engine.services import change_review, deliverable_review, test_evidence


def test_artifact_download_endpoint_and_change_review_test_evidence(client, engine):
    project_id = "boss-dogfood"
    run_id = make_id("crun")
    art_id = make_id("art")
    dec_id = make_id("dec")
    pr_url = f"http://boss-local/local_{project_id}/demo/pull/91"
    head_sha = "c" * 40

    raw_out = (
        "tests/test_calc.py::test_add PASSED\n"
        "tests/test_calc.py::test_sub PASSED\n"
        "========================= 2 passed in 0.02s =========================\n"
    )
    te_rec = test_evidence.build_evidence_record(
        evidence={"tests": [{"command": "pytest -v", "ok": True, "exit_code": 0}],
                  "tests_passed": True, "last_verify_output": raw_out, "substrate": "local"},
        host_kind="local",
        head_sha=head_sha,
        artifact_id=art_id,
    )
    content = {
        "title": "Test report for Calculator",
        "status": "passed",
        "head_sha": head_sha,
        "verified_commit_sha": head_sha,
        "tests_added_or_run": ["pytest -v"],
        "test_evidence": te_rec,
        "full_report": test_evidence.render_full_report_text(
            {"title": "Test report for Calculator", "status": "passed",
             "head_sha": head_sha, "test_evidence": te_rec}
        ),
    }

    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, "
            "finalize_ctx, pull_request_url, pull_request_number, decision_id) "
            "VALUES (:id, :p, 'completed', 'done', 'Calculator', '{}'::jsonb, '{}'::jsonb, "
            "'{}'::jsonb, :u, 91, :d)"
        ), {"id": run_id, "p": project_id, "u": pr_url, "d": dec_id})
        c.execute(text(
            "INSERT INTO artifacts (id, project_id, artifact_type, title, uri, content, content_hash) "
            "VALUES (:id, :p, 'test_report', 'Test report for Calculator', 'artifact://t', "
            "CAST(:c AS JSONB), 'hash1011')"
        ), {"id": art_id, "p": project_id, "c": json.dumps(content)})
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, recommendation) "
            "VALUES (:d, :p, 'Approve Calculator', 'pending', CAST(:rec AS JSONB))"
        ), {
            "d": dec_id, "p": project_id,
            "rec": json.dumps({
                "action": "review_pull_request",
                "run_id": run_id,
                "head_sha": head_sha,
                "pull_request_url": pr_url,
                "test_report_artifact_id": art_id,
            }),
        })

    # 1) Plain-text attachment download carries full per-case results.
    resp = client.get(f"/v1/artifacts/{art_id}/download")
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "tests/test_calc.py::test_add" in resp.text
    assert "tests/test_calc.py::test_sub" in resp.text
    assert head_sha in resp.text

    # 2) JSON format download returns structured artifact payload.
    resp_json = client.get(f"/v1/artifacts/{art_id}/download?format=json")
    assert resp_json.status_code == 200, resp_json.text
    parsed = resp_json.json()
    assert parsed["test_evidence"]["counts"]["cases_passed"] == 2

    # 3) change_review helper exposes download_url and suppresses check_run_url on local host.
    with begin(engine) as c:
        local_te = change_review._test_evidence_for_change(c, project_id, run_id, pr_url, "local")
        assert local_te is not None
        assert local_te["artifact_id"] == art_id
        assert local_te["download_url"] == f"/v1/artifacts/{art_id}/download"
        assert local_te["counts"]["cases_passed"] == 2
        assert local_te["check_run_url"] is None

        # Deliverable review snapshot also surfaces download_url.
        snap = deliverable_review.snapshot(c, project_id, run_id)["snapshot"]
        assert snap["checks"]["download_url"] == f"/v1/artifacts/{art_id}/download"
        assert snap["checks"]["counts"]["cases_passed"] == 2
