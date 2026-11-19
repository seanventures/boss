"""The project page always shows the app (operator, 2026-11-19: "for the Recipe why there are no
previews showing up now? … did we remove the Preview feature").

recipe-box's previews were all "Stopped by BOSS: nobody opened it for 20 min"; the Overview had no
app card, the preview link answered plain-text 409, and Needs you told a person to restart a
worker that was healthy. These pin, on the real test database:

  * Your app (project_experience/your_app.py): main once a change merged, else the latest ready
    change; a preview BOSS stopped is `asleep`; on the Overview, the projects summary (list and
    Command Center) alike.
  * Wake on visit (preview_wake.py through the gateway): a page load of a BOSS-stopped preview
    starts its build again and answers "Waking up your app…" (503, refreshing); one start per
    build; the old link follows its replacement; a person's Stop stays stopped; an asset request
    never starts anything.
  * A "no worker" card is shown only while the queue is really starved, and the sweep withdraws
    it otherwise (cleared_causes.LIVE_CAUSES).
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from apps.api.tests import _factory_support as fx

P = fx.PROJECT
PAGE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "accept": "text/html"}


def _run(engine, *, pr: int | None, merged: bool = False, decision_status: str = "pending",
         minutes_ago: int = 10, status: str = "completed", kind: str | None = None) -> str:
    run_id = f"crun_{uuid4().hex[:16]}"
    decision_id = f"dec_{uuid4().hex[:12]}" if pr else None
    fctx = {"merge": {"sha": "a" * 40, "at": "2026-11-18T20:00:00+00:00", "source": "boss"}} \
        if merged else {}
    state = {"app_kind": {"kind": kind, "why": "test"}} if kind else {}
    with engine.begin() as c:
        if decision_id:
            c.execute(text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status) "
                "VALUES (:i, :p, 'Approve code change', '{}'::jsonb, :s)"),
                {"i": decision_id, "p": P, "s": decision_status})
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, state, "
            "finalize_ctx, pull_request_url, pull_request_number, decision_id, created_at) VALUES "
            "(:i, :p, 'Recipe box', :st, 'done', '{}'::jsonb, CAST(:state AS jsonb), "
            "CAST(:f AS jsonb), :u, :n, :d, now() - make_interval(mins => :m))"),
            {"i": run_id, "p": P, "st": status, "state": json.dumps(state), "f": json.dumps(fctx),
             "u": f"https://github.com/acme/recipes/pull/{pr}" if pr else None, "n": pr,
             "d": decision_id, "m": minutes_ago})
    return run_id


def _preview(engine, run_id: str, *, status: str, error: str | None = None,
             minutes_ago: int = 5, url: str | None = None) -> str:
    preview_id = f"prev_{uuid4().hex[:20]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO app_previews (id, project_id, run_id, status, error, mode, url, created_at) "
            "VALUES (:i, :p, :r, :s, :e, 'process', :u, now() - make_interval(mins => :m))"),
            {"i": preview_id, "p": P, "r": run_id, "s": status, "e": error, "u": url,
             "m": minutes_ago})
    return preview_id


IDLE = "Stopped by BOSS: nobody opened it for 20 min."


def _card(engine):
    from boss_engine.services.project_experience import your_app

    with engine.begin() as c:
        return your_app.card(c, P)


# ── Your app ────────────────────────────────────────────────────────────────────────────────

def test_no_previewable_build_means_no_card(engine):
    _run(engine, pr=None)                       # a build with no PR
    _run(engine, pr=9, kind="library")          # a library has no live app
    _run(engine, pr=8, status="failed")
    assert _card(engine) is None


def test_main_once_a_change_merged_and_an_idle_preview_is_asleep(engine):
    merged = _run(engine, pr=2, merged=True, decision_status="executed", minutes_ago=60)
    _run(engine, pr=4, minutes_ago=5)           # newer, open: main still wins
    old = _preview(engine, merged, status="stopped", error=IDLE)
    card = _card(engine)
    assert card["run_id"] == merged and card["version"] == "main"
    assert card["version_label"] == "Main" and "change #2" in card["version_words"]
    assert card["state"] == "asleep" and card["preview_id"] == old
    assert card["href"] == f"/v1/previews/{old}/web/"
    assert "wakes" in card["words"].lower()


def test_else_the_latest_ready_change_and_a_closed_pr_is_skipped(engine):
    _run(engine, pr=3, decision_status="rejected", minutes_ago=1)   # closed
    ready = _run(engine, pr=4, minutes_ago=5)
    card = _card(engine)
    assert card["run_id"] == ready and card["version"] == "change"
    assert card["version_label"] == "Change #4" and card["state"] == "none" and card["href"] is None


def test_a_persons_stop_is_stopped_and_a_running_preview_runs(engine):
    run = _run(engine, pr=4)
    _preview(engine, run, status="stopped", error=None, minutes_ago=9)
    assert _card(engine)["state"] == "stopped"
    live = _preview(engine, run, status="running", url="http://127.0.0.1:1")
    card = _card(engine)
    assert card["state"] == "running" and card["preview_id"] == live


def test_the_overview_and_the_projects_summary_carry_it(client, engine):
    run = _run(engine, pr=2, merged=True, decision_status="executed")
    _preview(engine, run, status="expired")
    ov = client.get(f"/v1/projects/{P}/overview").json()
    assert ov["app"]["run_id"] == run and ov["app"]["state"] == "asleep", ov.get("errors")
    assert "app" not in (ov.get("errors") or {})
    items = client.get("/v1/projects/summary").json()["items"]
    mine = next(i for i in items if i["id"] == P)
    assert mine["app"]["run_id"] == run and mine["app"]["version"] == "main"


# ── wake on visit ───────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def starts(engine, monkeypatch):
    """start_preview_async without a build: records the call and leaves a `starting` row."""
    from boss_engine.services import preview_service

    calls: list[tuple[str, str]] = []

    def fake_start(engine_, settings, run_id, *, ttl_minutes=60, mode="auto", actor="operator"):
        calls.append((run_id, actor))
        new_id = _preview(engine, run_id, status="starting", minutes_ago=0)
        return {"id": new_id, "status": "starting", "run_id": run_id}

    monkeypatch.setattr(preview_service, "start_preview_async", fake_start)
    monkeypatch.setattr(preview_service, "_respawn_allowed", lambda *a, **k: True)
    return calls


def _latest(engine, run_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text(
            "SELECT id, status FROM app_previews WHERE run_id = :r ORDER BY created_at DESC "
            "LIMIT 1"), {"r": run_id}).mappings().first())


def test_a_page_load_wakes_an_idle_preview_once(client, engine, starts):
    run = _run(engine, pr=2)
    old = _preview(engine, run, status="stopped", error=IDLE)
    r = client.get(f"/v1/previews/{old}/web/", headers=PAGE, follow_redirects=False)
    assert r.status_code == 503, r.text
    assert "Waking up your app" in r.text and r.headers.get("refresh")
    assert "sandbox" in r.headers.get("content-security-policy", "")
    assert starts == [(run, "boss:wake-on-visit")]
    new = _latest(engine, run)["id"]
    assert new != old
    # A second tab (or the page's own refresh) waits; it never starts a second preview.
    again = client.get(f"/v1/previews/{old}/web/", headers=PAGE, follow_redirects=False)
    assert again.status_code == 503 and len(starts) == 1
    with engine.begin() as c:
        woke = c.execute(text("SELECT count(*) FROM events WHERE event_type = 'app_preview.woken' "
                              "AND entity_id = :i"), {"i": new}).scalar()
    assert woke == 1
    # Once the replacement serves, the old link goes to it.
    with engine.begin() as c:
        c.execute(text("UPDATE app_previews SET status = 'running', url = 'http://127.0.0.1:1' "
                       "WHERE id = :i"), {"i": new})
    moved = client.get(f"/v1/previews/{old}/web/recipes?x=1", headers=PAGE, follow_redirects=False)
    assert moved.status_code == 307
    assert moved.headers["location"] == f"/v1/previews/{new}/web/recipes?x=1"


def test_a_persons_stop_and_a_failed_start_are_not_woken(client, engine, starts):
    run = _run(engine, pr=2)
    stopped = _preview(engine, run, status="stopped", error=None)
    r = client.get(f"/v1/previews/{stopped}/web/", headers=PAGE)
    assert r.status_code == 409 and "Open the app again from BOSS" in r.text
    run2 = _run(engine, pr=3)
    failed = _preview(engine, run2, status="failed", error="port_timeout")
    assert client.get(f"/v1/previews/{failed}/web/", headers=PAGE).status_code == 409
    assert starts == []


def test_an_asset_request_never_starts_a_preview(client, engine, starts):
    run = _run(engine, pr=2)
    old = _preview(engine, run, status="stopped", error=IDLE)
    r = client.get(f"/v1/previews/{old}/web/app.js",
                   headers={"sec-fetch-mode": "no-cors", "accept": "*/*"})
    assert r.status_code == 503 and r.headers.get("retry-after")
    assert starts == []


def test_an_old_link_is_not_woken(client, engine, starts):
    run = _run(engine, pr=2)
    old = _preview(engine, run, status="stopped", error=IDLE, minutes_ago=60 * 24 * 20)
    r = client.get(f"/v1/previews/{old}/web/", headers=PAGE)
    assert r.status_code == 409 and "too old" in r.text and starts == []


def test_on_the_preview_origin_the_replacement_is_its_own_host():
    from boss_engine.services.preview_wake import sibling_location

    assert sibling_location("prev-aaa.localhost:3001", "http", "prev_aaa", "prev_bbb", "x", "",
                            on_preview_origin=True) == \
        "http://prev-bbb.localhost:3001/v1/previews/prev_bbb/web/x"
    assert sibling_location("100.64.0.20:8000", "http", "prev_aaa", "prev_bbb", "", "q=1",
                            on_preview_origin=False) == "/v1/previews/prev_bbb/web/?q=1"


# ── Needs you never says "restart the worker" when BOSS can see it is fine ─────────────────

def _no_worker_card(engine) -> str:
    decision_id = f"dec_{uuid4().hex[:12]}"
    rec = {"action": "stuck_work_needs_person", "cause_code": "no_worker", "reason": "no_worker",
           "reason_code": "no_worker", "cause": "No worker is taking work from the queue",
           "next": "Restart BOSS's worker (boss restart worker); queued builds then start.",
           "run_ids": []}
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status) VALUES "
            "(:i, :p, 'A build: No worker is taking work from the queue. Restart BOSS''s worker.', "
            "CAST(:r AS jsonb), 'pending')"), {"i": decision_id, "p": P, "r": json.dumps(rec)})
    return decision_id


def _starve(engine) -> None:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO jobs (id, project_id, kind, status, payload, run_after, created_at, "
            "updated_at) VALUES (:i, :p, 'code.task', 'queued', '{}'::jsonb, "
            "now() - interval '40 minutes', now() - interval '40 minutes', "
            "now() - interval '40 minutes')"), {"i": f"job_{uuid4().hex[:12]}", "p": P})


def _waiting(engine) -> set[str]:
    from boss_engine.services.decision_actions import waiting_on_operator

    with engine.begin() as c:
        return {str(d["id"]) for d in waiting_on_operator(c, P)}


def test_a_no_worker_card_is_hidden_and_withdrawn_when_the_queue_is_fine(engine):
    from boss_engine.services.cleared_causes import withdraw_cleared

    card = _no_worker_card(engine)
    assert card not in _waiting(engine)       # healthy: empty queue
    with engine.begin() as c:
        assert card in withdraw_cleared(c, P)
        status = c.execute(text("SELECT status FROM decisions WHERE id = :i"), {"i": card}).scalar()
    assert status == "withdrawn"


def test_a_no_worker_card_stays_while_the_queue_is_really_starved(engine):
    from boss_engine.services.cleared_causes import withdraw_cleared
    from boss_engine.services.stuck_work import queue_starved

    _starve(engine)
    with engine.begin() as c:
        assert queue_starved(c) is not None
    card = _no_worker_card(engine)
    assert card in _waiting(engine)
    with engine.begin() as c:
        assert card not in withdraw_cleared(c, P)


def test_boss_never_raises_a_no_worker_card_when_a_worker_is_taking_work(engine):
    from boss_engine.services import stuck_work

    with engine.begin() as c:
        made = stuck_work._raise_card(  # noqa: SLF001 — the one place a stuck card is raised
            c, "crun_x", {"run": {"project_id": P, "title": "A build", "status": "queued"}},
            {"cause_code": "no_worker", "cause": "No worker is taking work from the queue"},
            "Restart BOSS's worker", "headline", chain=False)
    assert made is None
