"""NEEDS YOU: ONE CARD PER PULL REQUEST, AND A DEPLOY NAMED BY WHAT IT SHIPS.

Replays helpdesk-ai-v3 (2026-11-07) on the Project page's real overview route. Two runs both
claimed PR #2 and each raised "Approve code change … (PR #2)": Needs you showed "Merge PR #2"
twice. "Deploy again" planned 99b569f — PR #2's open head, not PR #1's merge fa64985 — and the
card read "Deploy PR #1 to Staging" because the workflow's first PR was #1; its drawer then had
no commit list to approve (release_scope refused every press).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests.test_the_project_page_speaks_in_words import (  # noqa: F401 - fixtures
    _get, client, helpdesk, world)


def _decide(engine, project_id, rec, *, question="?", minutes_ago=1):
    did = f"dec_{uuid.uuid4().hex[:12]}"
    at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
            "created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending', 2, :at)"),
            {"i": did, "p": project_id, "q": question, "r": json.dumps(rec), "at": at})
    return did


def _close(engine, *ids):
    with engine.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'withdrawn' WHERE id = ANY(:i)"),
                  {"i": list(ids)})


def test_two_questions_about_one_pull_request_are_one_card(world, client, engine, helpdesk):
    url = "https://github.com/paramg/helpdesk-ai-v3/pull/92"
    older = _decide(engine, helpdesk["id"], {"action": "review_pull_request",
                                             "pull_request_url": url, "run_id": "crun_retry"},
                    question="Approve code change: Route new tickets to a queue · retry 1 (PR #92)",
                    minutes_ago=4)
    newer = _decide(engine, helpdesk["id"], {"action": "review_pull_request",
                                             "pull_request_url": url, "run_id": "crun_followup"},
                    question="Approve code change: Follow-up: deferred review findings (PR #92)")
    try:
        o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
        merges = [n for n in o["needs_you"] if n["label"] == "Merge PR #92"]
        assert [n["id"] for n in merges] == [newer], o["needs_you"]
        # Every surface reads the same one list (decision_actions.waiting_on_operator).
        from boss_engine.services.decision_actions import waiting_on_operator

        with engine.begin() as c:
            ids = {d["id"] for d in waiting_on_operator(c, helpdesk["id"])}
        assert newer in ids and older not in ids
    finally:
        _close(engine, older, newer)


def test_a_deploy_of_an_open_pull_requests_head_is_never_named_for_another_pr(
        world, client, engine, helpdesk):
    head = "99b569f71330ec710ec51267c801086b60c18bba"
    card_hash = "sha256:" + "b" * 64
    did = _decide(engine, helpdesk["id"], {
        "action": "deploy_stack", "deployment_id": f"sdep_{uuid.uuid4().hex[:12]}",
        "target_id": helpdesk["target_id"], "commit": head, "ref": "pull/2",
        "workflow_run_id": helpdesk["merged"][0]["run_id"],
        "release_commits": {"known": True, "needs_reason": True, "content_hash": card_hash,
                            "sentence": "Ships 1 commit beyond the serving commit fa64985.",
                            "commits": [{"sha": head, "short": "99b569f"}]},
        "action_preview": {"kind": "fields", "release": True, "content_hash": card_hash,
                           "fields": [{"label": "What ships",
                                       "value": "Ships 1 commit beyond the serving commit fa64985."},
                                      {"label": "99b569f · no BOSS run · unknown, NOT reviewed",
                                       "value": "More tests"}]}},
        question="Deploy paramg/helpdesk-ai-v3@99b569f71330 to 2.29.2.202?")
    try:
        o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
        card = next(n for n in o["needs_you"] if n["id"] == did)
        assert card["label"] == "Deploy PR #2 (not merged) to Staging", card["label"]
        assert card["mini"][0] == {"label": "PR #2 not merged", "state": "failed"}
        # The drawer draws the card's commits and approves with its hash and a reason.
        assert card["release"]["content_hash"] == card_hash
        assert card["release"]["needs_reason"] is True
        assert card["release"]["preview"]["fields"][1]["label"].startswith("99b569f")
    finally:
        _close(engine, did)


def test_a_bare_commit_is_named_by_its_sha_and_a_merge_by_its_pr(engine, helpdesk):
    from boss_engine.services.project_experience.needs import shipped_words

    with engine.begin() as c:
        merged = c.execute(text(
            "SELECT finalize_ctx->'merge'->>'sha', pull_request_number FROM code_task_runs "
            "WHERE project_id = :p AND COALESCE(finalize_ctx->'merge'->>'sha', '') <> '' "
            "AND pull_request_number IS NOT NULL LIMIT 1"), {"p": helpdesk["id"]}).first()
        assert shipped_words(c, helpdesk["id"], {"commit": "c" * 40}, 1)[0] == "ccccccc"
        if merged:
            words, first = shipped_words(c, helpdesk["id"], {"commit": merged[0],
                                                             "ref": "pull/99"}, 1)
            assert words == f"PR #{merged[1]}" and first["state"] == "done"


def test_deploy_again_is_withdrawn_once_a_newer_deploy_goes_live(world, client, engine, helpdesk):
    """The live case: dec_8eaeba2f asked to ship 99b569f; at 13:14 a person's deploy of c6d4d3a
    went live on the same target and the question was withdrawn (cleared_causes). Replayed."""
    from apps.api.tests.test_the_project_page_speaks_in_words import REPO, _ask_to_ship
    from boss_engine.services.cleared_causes import on_deploy_healthy

    sid, did = _ask_to_ship(engine, helpdesk["id"], helpdesk["target_id"],
                            "99b569f71330ec710ec51267c801086b60c18bba",
                            run_id=helpdesk["merged"][0]["run_id"], minutes_ago=4)
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
            "manifest, status, created_by, created_at) VALUES (:i, :p, :t, :r, 'main', :c, "
            "'{}'::jsonb, 'healthy', 'ceo', now())"),
            {"i": f"sdep_{uuid.uuid4().hex[:12]}", "p": helpdesk["id"], "t": helpdesk["target_id"],
             "r": REPO, "c": "c6d4d3a59f8676d973d9bcb562b56cdb4fc32a0e"})
        assert did in on_deploy_healthy(c, helpdesk["id"])
    o = _get(world, client, f"/v1/projects/{helpdesk['id']}/overview")
    assert did not in {n["id"] for n in o["needs_you"]}
