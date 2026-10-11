"""App ▸ Security, the backend half of the tester's findings (2026-10-09).

  * the X-Powered-By finding was listed once per route (5 rows for one missing header): the
    findings response now carries `groups`, one per rule and route class, with where;
  * Accept / False positive with an empty reason gave no message: the reason is checked by ONE
    rule on the server (service.check_reason), said in words, for the finding's buttons and the
    review's choice alike;
  * a scan that could not run showed a raw traceback with internal paths: it says why, plainly.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from apps.api.tests.test_security_scan_routes import (  # noqa: F401
    _app, _client, _person, _post_scan, _run, scanners_installed, world,
)
from apps.api.app import main
from boss_engine.security_scan import service


def test_one_rule_on_one_route_class_is_one_group(world, engine):  # noqa: F811
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    scan = _run(world, engine, app_key, "before")
    op = _person(world, "operator")
    with world.acting_as(world.ctx(op)):
        body = _client().get(f"/v1/security/scans/{scan['id']}/findings").json()
    findings, groups = body["findings"], body["groups"]
    powered = [f for f in findings if f["tool"] == "zap" and f["rule_id"] == "10037"]
    assert len(powered) > 1, "the fixture's X-Powered-By finding is on several routes"
    pages = [g for g in groups if g["rule_id"] == "10037"]
    # one row per route class, never one per route
    assert len(pages) <= 3 and sum(g["count"] for g in pages) == len(powered)
    assert all(len(g["where"]) == g["count"] for g in pages)
    assert {f["group_key"] for f in powered} == {g["group_key"] for g in pages}
    # every finding is in exactly one group
    assert sorted(i for g in groups for i in g["finding_ids"]) == sorted(f["id"] for f in findings)


def test_triage_a_whole_group_at_once(world, engine):  # noqa: F811
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    scan = _run(world, engine, app_key, "before")
    op = _person(world, "operator")
    client = _client()
    with world.acting_as(world.ctx(op)):
        groups = client.get(f"/v1/security/scans/{scan['id']}/findings").json()["groups"]
        g = max((g for g in groups if g["tool"] == "zap"), key=lambda g: g["count"])
        assert g["count"] > 1
        r = client.post(f"/v1/security/findings/{g['finding_ids'][0]}/triage",
                        json={"status": "accepted", "reason": "the header is stripped by the edge proxy",
                              "whole_group": True})
        assert r.status_code == 200, r.text
        assert r.json()["group_triaged"] == g["count"]
        after = client.get(f"/v1/security/scans/{scan['id']}/findings").json()["groups"]
    same = next(x for x in after if x["group_key"] == g["group_key"])
    assert same["statuses"] == {"accepted": g["count"]}
    with engine.begin() as c:
        n = c.execute(text("SELECT count(*) FROM events WHERE event_type = 'security_finding.triaged' "
                           "AND entity_id = ANY(:ids)"), {"ids": g["finding_ids"]}).scalar()
    assert n == g["count"]                      # one event per finding: each keeps its own history


def test_a_triage_without_a_real_reason_is_refused_in_words(world, engine):  # noqa: F811
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    scan = _run(world, engine, app_key, "before")
    fid = service.list_findings(engine.connect(), scan["id"])[0]["id"]
    op = _person(world, "operator")
    client = _client()
    with world.acting_as(world.ctx(op)):
        for reason, code in (("", "reason_required"), ("   ", "reason_required"), (None, "reason_required"),
                             ("ok", "reason_too_short"), ("........", "reason_too_short")):
            body = {"status": "false_positive"} if reason is None else {"status": "false_positive", "reason": reason}
            r = client.post(f"/v1/security/findings/{fid}/triage", json=body)
            assert r.status_code == 422, (reason, r.text)
            out = r.json()
            assert out["error"] == code and "false positive" in out["message"], out
    with engine.begin() as c:
        assert c.execute(text("SELECT status FROM security_findings WHERE id = :i"), {"i": fid}).scalar() == "open"


def test_a_scan_that_cannot_run_says_why_without_internals(world, engine):  # noqa: F811
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    op = _person(world, "operator")

    def queue() -> str:
        with world.acting_as(world.ctx(op)):
            r = _post_scan(_client(), world, app_key)
        assert r.status_code == 202, r.text
        return r.json()["scan_id"]

    def denied(*a):
        raise PermissionError(13, "Permission denied", "/var/lib/boss/runs/secscan_x/scan-1/src/.git")

    out = service.execute_scan(engine, main.settings, queue(), do_checkout=denied, run_tools=lambda *a: ([], []))
    err = out["scan"]["counts"]["error"]
    assert out["scan"]["status"] == "failed"
    assert "/var/lib" not in err and "Traceback" not in err and "Errno" not in err
    assert "working copy" in err

    def no_clone(*a):
        raise service.ScanCouldNotRun("could not fetch the app's code (acme/shop, branch b) from GitHub: "
                                      "repository not found. Check that BOSS's GitHub connection can read it")

    out = service.execute_scan(engine, main.settings, queue(), do_checkout=no_clone, run_tools=lambda *a: ([], []))
    assert out["scan"]["counts"]["error"].startswith("could not fetch the app's code (acme/shop")


def test_git_text_is_cleaned_of_internal_addresses_and_paths():
    from boss_engine.security_scan.scope import AppSource

    src = AppSource("p", "a", "r", "acme", "shop", "boss/code/r")
    said = service._git_reason(  # noqa: SLF001
        "Cloning into '/var/lib/boss/runs/x/scan-1/src'...\n"
        "fatal: could not read Username for 'http://egress:8090': terminal prompts disabled", src)
    assert "egress" not in said and "/var/lib" not in said and "fatal" not in said
    assert said.startswith("could not read Username")
