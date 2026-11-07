"""A mode switch never floods Approvals (2026-11-07; services/ambient/chief_of_staff.py).

Over HTTP, the way the owner did it on the laptop: turn Govern › Autonomy › Always-on agents to
"Act within levels", then the Chief of Staff's turn runs. The routine hand-offs (tests for hot
files, docs) go straight to their owners; Approvals gains ONE card — the nudge, the only thing
whose level needs a person — worded in plain words. Turning the agents off again withdraws it on
the next tick, with a note, and Approvals is back where it was.
"""
from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import text

HOME = "boss-dogfood"
RAW = re.compile(r"module:|repo:|subject:|task:|run:|crun_|wfr_|host:")


def _pending(client):
    got = client.get("/v1/decisions/inbox", params={"project_id": HOME})
    assert got.status_code == 200, got.text
    return {d["id"]: d for d in got.json()["items"] if d["status"] == "pending"}


def test_act_within_levels_hands_work_over_and_asks_once_then_off_withdraws(client, engine):
    from boss_engine.config import get_settings
    from boss_engine.services.ambient import chief_of_staff, signals

    repo = f"acme/svc-{uuid4().hex[:6]}"
    with engine.begin() as c:
        for path in ("web/src/pages/TicketDetailPage.jsx", "server/auth/routes.mjs",
                     "server/incidents/service.mjs"):
            signals.record_signal(c, project_id=HOME, source="scan", kind="scan.untested_hotspot",
                                  title=f"Hot file without tests: {path}", subject_kind="module",
                                  subject_id=f"{repo}:{path}", severity="medium")
        signals.record_signal(c, project_id=HOME, source="scan", kind="scan.no_docs",
                              title=f"{repo} has no README", subject_kind="repo",
                              subject_id=repo, severity="low")
        signals.record_signal(c, project_id=HOME, source="shepherd", kind="shepherd.escalation",
                              title="Finish the visual refresh the last change promised",
                              subject_kind="task", subject_id=f"crun_{uuid4().hex[:12]}",
                              severity="medium")
    before = _pending(client)
    try:
        put = client.put("/v1/ambient/mode", json={"mode": "route", "actor": "ceo"})
        assert put.status_code == 200, put.text
        assert put.json()["mode"]["mode"] == "route"
        ran = client.post("/v1/ambient/chief-of-staff/run",
                          json={"project_id": HOME, "actor": "ceo", "sweep": False})
        assert ran.status_code == 200, ran.text
        assert ran.json()["projects"][HOME]["counts"] == {"routed": 4, "proposed": 1}
        new = {k: v for k, v in _pending(client).items() if k not in before}
        assert len(new) == 1, [d["question"] for d in new.values()]
        [card] = new.values()
        assert card["question"].startswith("Nudge: “Finish the visual refresh")
        assert not RAW.search(card["question"])
        handed = client.get("/v1/ambient/work-items",
                            params={"project_id": HOME, "filter": "all"}).json()["items"]
        assert len([i for i in handed if repo.split("/")[1] in i["title"]
                    or "Hot file without tests" in i["title"]]) >= 4

        off = client.put("/v1/ambient/mode", json={"mode": "off", "actor": "ceo"})
        assert off.status_code == 200, off.text
        tick = chief_of_staff.tick(engine, get_settings())
        assert HOME in tick["reconsidered"]
        assert {k for k in _pending(client) if k not in before} == set()
        withdrawn = client.get(f"/v1/decisions/{card['id']}").json()
        assert withdrawn["status"] == "withdrawn"
        assert "always-on agents are off" in withdrawn["resolution_note"]
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM system_state WHERE key = 'ambient_mode'"))
