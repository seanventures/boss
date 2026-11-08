"""WATCH THIS APP over HTTP (agentic-factory 2-watch-an-app.html; engine services/watch_app.py).

One input is read into the app it means, how it talks, what matters and the watches, with a
7-day backtest, and nothing is written. Start watching writes through Observability's own
functions: the app's alert rules (triaged; answering alone below prod), the outside channel
(sent its code, the one gate), each watch's routing and quiet hours, and the landing. A
refusal writes nothing; a later edit keeps its why; production waits for a person."""
from __future__ import annotations

import re
from uuid import uuid4

from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import PROJECT_ID, deployed_target, sample
from boss_engine.services import watch_app

HOOK = "https://hooks.slack.example/T1/B2/apitest"
WORDS = "Wake me if replies stop sending or the inbox gets slow. Tell #support-eng on Slack."


def _apps(engine) -> tuple[str, str, str]:
    name = f"HelpDesk {uuid4().hex[:4]}"
    stg, prod = f"t_wa_{uuid4().hex[:8]}", f"t_wa_{uuid4().hex[:8]}"
    with begin(engine) as c:
        deployed_target(c, PROJECT_ID, stg, name, "staging")
        deployed_target(c, PROJECT_ID, prod, name, "prod")
        for i in range(6):
            sample(c, PROJECT_ID, stg, ago_s=600 * (i + 1))
    return name, stg, prod


def _read(client, words: str, target_id: str | None = None, kind: str | None = None) -> dict:
    got = client.post(f"/v1/projects/{PROJECT_ID}/watch/read", json={"text": words, "target_id": target_id, "kind": kind})
    assert got.status_code == 200, got.text
    return got.json()


def _spec(read: dict, **over) -> dict:
    body = {"project_id": PROJECT_ID, "actor": "ceo", "target_id": read["target"]["target_id"], "words": WORDS,
            "night": "wake", "alone": False,
            "watches": [{"kind": w["kind"], "params": w["params"], "for_s": w["for_s"], "tone": w["tone"], "enabled": True,
                         "words": w["words"]} for w in read["watches"]],
            "tell": {"kind": "slack_webhook", "name": "#support-eng", "url": HOOK}}
    body.update(over)
    return body


def _rules(engine, tid: str) -> dict[str, dict]:
    with begin(engine) as c:
        rows = c.execute(text("SELECT * FROM app_alert_rules WHERE target_id = :t AND kind <> 'custom'"),
                         {"t": tid}).mappings().all()
    return {r["kind"]: dict(r) for r in rows}


def test_the_words_are_read_into_watches_and_nothing_is_written(client, engine):
    name, stg, prod = _apps(engine)
    r = _read(client, f"{name} staging.\n{WORDS}", stg)
    assert r["kind"] == "project" and r["target"]["target_id"] == stg and not r["target"]["prod"]
    kinds = [w["kind"] for w in r["watches"]]
    assert kinds[:4] == ["error_rate", "latency_p95", "health_check", "deploy_health"]
    er = r["watches"][0]
    assert er["params"]["threshold"] == 2.0 and er["words"] == "replies stop sending" and er["tone"] == "wake"
    assert er["title"] == "Errors > 2% for 5 min" and "100 * sum(increase(" in er["expr"]
    assert re.search(r"would have fired \d+× in 7 d", er["sub"]) and "168 h" in er["backtest"]
    assert r["watches"][1]["words"] == "the inbox gets slow" and r["watches"][1]["tone"] == "tell"
    assert r["tell"]["channel"] == {"kind": "slack_webhook", "name": "#support-eng"} and r["tell"]["needs_url"]
    assert r["question"]["answer"] == "wake" and [o["key"] for o in r["question"]["options"]] == ["wake", "wait", "alone"]
    assert r["on_call"]["alone"] and r["boundary"]["waits"][0]["key"] == "prod_deploy"
    assert r["signals"]["probes"] == 6 and r["collector"]["kind"] == "boss"
    with begin(engine) as c:
        assert not c.execute(text("SELECT 1 FROM app_watches WHERE target_id = :t"), {"t": stg}).first()
        assert not c.execute(text("SELECT 1 FROM contact_points WHERE name = '#support-eng' AND project_id = :p"),
                             {"p": PROJECT_ID}).first()
    p = _read(client, f"{name} prod. Wake me if it goes down.", prod)
    assert p["target"]["prod"] and not p["on_call"]["alone"] and p["boundary"]["waits"][0]["key"] == "fix"
    assert _read(client, "our alertmanager pages us already")["kind"] == "elsewhere"


def test_start_watching_writes_rules_channel_routes_and_the_one_gate(client, engine):
    name, stg, _prod = _apps(engine)
    read = _read(client, f"{name} staging. {WORDS}", stg)
    got = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read))
    assert got.status_code == 200, got.text
    land = got.json()
    assert land["gate"]["name"] == "#support-eng" and [s["key"] for s in land["steps"]] == [
        "read", "collector", "data", "watches", "verify", "oncall"]
    assert land["steps"][4]["state"] == "you" and "waits for the code" in land["now"]["text"]
    rules = _rules(engine, stg)
    assert all(rules[k]["enabled"] and rules[k]["triage"] for k in ("error_rate", "latency_p95", "health_check", "deploy_health"))
    assert float(rules["error_rate"]["params"]["threshold"]) == 2.0
    assert rules["error_rate"]["urgency"] == "high" and rules["latency_p95"]["urgency"] == "low"
    with begin(engine) as c:
        cp = c.execute(text("SELECT id, verified_at, secret_ref FROM contact_points WHERE project_id = :p AND name = '#support-eng'"),
                       {"p": PROJECT_ID}).mappings().first()
        assert cp and cp["verified_at"] is None and cp["secret_ref"]
        routes = c.execute(text("SELECT rule_id, contact_point_ids FROM notification_policies WHERE project_id = :p "
                                "AND rule_id = ANY(:r)"), {"p": PROJECT_ID, "r": [x["id"] for x in rules.values()]}).all()
        assert len(routes) == 4 and all(cp["id"] in r[1] for r in routes)
        body = c.execute(text("SELECT body FROM notifications WHERE contact_point_id = :c AND kind = 'test' "
                              "ORDER BY created_at DESC LIMIT 1"), {"c": cp["id"]}).scalar()
        why = c.execute(text("SELECT reason FROM monitoring_changes WHERE object_kind = 'app_watch' AND object_id = :t"),
                        {"t": stg}).scalar()
    assert why and "replies stop sending" in why
    code = re.search(r"\b(\d{6})\b", body).group(1)
    assert HOOK not in got.text, "a webhook URL is never sent back"
    bad = client.post(f"/v1/projects/{PROJECT_ID}/watch/{stg}/verify", json={"project_id": PROJECT_ID, "actor": "ceo", "code": "12"})
    assert bad.status_code == 422 and "6 digits" in bad.json()["message"]
    wrong = client.post(f"/v1/projects/{PROJECT_ID}/watch/{stg}/verify",
                        json={"project_id": PROJECT_ID, "actor": "ceo", "code": "000000" if code != "000000" else "111111"})
    assert wrong.status_code == 422 and "not the code" in wrong.json()["message"]
    ok = client.post(f"/v1/projects/{PROJECT_ID}/watch/{stg}/verify", json={"project_id": PROJECT_ID, "actor": "ceo", "code": code})
    assert ok.status_code == 200, ok.text
    assert ok.json()["gate"] is None and ok.json()["steps"][4]["state"] == "done"
    health = client.get(f"/v1/projects/{PROJECT_ID}/health").json()
    assert any(w["target_id"] == stg for w in health["watched"]), "the landing rides the Health read"
    again = client.get(f"/v1/projects/{PROJECT_ID}/watch/{stg}")
    assert again.status_code == 200 and again.json()["tell"]["verified"]


def test_a_refused_start_writes_nothing(client, engine):
    name, stg, _prod = _apps(engine)
    read = _read(client, f"{name} staging. {WORDS}", stg)
    fresh = f"#ops-{uuid4().hex[:6]}"  # a channel BOSS has no webhook for yet
    no_url = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, tell={"kind": "slack_webhook", "name": fresh}))
    assert no_url.status_code == 422 and "incoming webhook URL" in no_url.json()["message"]
    bad_url = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, tell={"kind": "slack_webhook", "name": fresh, "url": "nope"}))
    assert bad_url.status_code == 422 and "full URL" in bad_url.json()["message"]
    none = _spec(read)
    for w in none["watches"]:
        w["enabled"] = False
    empty = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=none)
    assert empty.status_code == 422 and "at least one watch" in empty.json()["message"]
    odd = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, night="later"))
    assert odd.status_code == 422 and "Wake me" in odd.json()["message"]
    rules = _rules(engine, stg)
    assert all(r["source"] != "console" for r in rules.values()), "the rules a refused start touched rolled back"
    with begin(engine) as c:
        assert not c.execute(text("SELECT 1 FROM app_watches WHERE target_id = :t"), {"t": stg}).first()
    other = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, target_id="t_nope"))
    assert other.status_code == 404


def test_wait_till_eight_holds_messages_overnight(client, engine):
    name, stg, _prod = _apps(engine)
    read = _read(client, f"{name} staging. Tell me if the inbox gets slow.", stg)
    got = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, night="wait", tell={}, timezone="Europe/London"))
    assert got.status_code == 200, got.text
    assert got.json()["gate"] is None
    rules = _rules(engine, stg)
    with begin(engine) as c:
        quiet = c.execute(text("SELECT quiet_hours FROM notification_policies WHERE rule_id = ANY(:r)"),
                          {"r": [x["id"] for x in rules.values()]}).scalars().all()
    assert quiet and all(q == {"start": "22:00", "end": "08:00", "timezone": "Europe/London"} for q in quiet)
    assert all(r["urgency"] == "low" for r in rules.values() if r["enabled"])


def test_production_triages_and_waits(client, engine):
    name, _stg, prod = _apps(engine)
    read = _read(client, f"{name} prod. Wake me if it goes down.", prod)
    got = client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, alone=True, tell={}))
    assert got.status_code == 200, got.text
    assert not got.json()["alone"]
    rules = _rules(engine, prod)
    assert all(r["triage"] and not r["auto_respond"] for r in rules.values() if r["enabled"])


def test_a_later_edit_keeps_its_why(client, engine):
    name, stg, _prod = _apps(engine)
    read = _read(client, f"{name} staging. Wake me if replies stop sending.", stg)
    assert client.post(f"/v1/projects/{PROJECT_ID}/watch", json=_spec(read, tell={})).status_code == 200
    rid = _rules(engine, stg)["latency_p95"]["id"]
    url = f"/v1/projects/{PROJECT_ID}/watch/{stg}/change"
    no_why = client.post(url, json={"project_id": PROJECT_ID, "actor": "ceo", "rule_id": rid, "params": {"threshold": 900}})
    assert no_why.status_code == 422 and "why" in no_why.json()["message"].lower()
    out_of_bounds = client.post(url, json={"project_id": PROJECT_ID, "actor": "ceo", "rule_id": rid, "reason": "try",
                                           "params": {"threshold": 10 ** 9}})
    assert out_of_bounds.status_code == 422 and "between" in out_of_bounds.json()["message"]
    ok = client.post(url, json={"project_id": PROJECT_ID, "actor": "ceo", "rule_id": rid, "reason": "Monday imports",
                                "params": {"threshold": 900}, "tone": "wake"})
    assert ok.status_code == 200, ok.text
    r = _rules(engine, stg)["latency_p95"]
    assert float(r["params"]["threshold"]) == 900 and r["urgency"] == "high"
    with begin(engine) as c:
        why = c.execute(text("SELECT reason FROM monitoring_changes WHERE object_id = :r ORDER BY changed_at DESC LIMIT 1"),
                        {"r": rid}).scalar()
    assert why == "Monday imports"
    gone = client.post(url, json={"project_id": PROJECT_ID, "actor": "ceo", "rule_id": rid, "reason": "not needed", "remove": True})
    assert gone.status_code == 200 and not _rules(engine, stg)["latency_p95"]["enabled"]
    assert all(w["kind"] != "latency_p95" for w in gone.json()["watches"])


def test_reading_words_finds_the_app_the_channel_and_the_night():
    w = watch_app.read_words("HelpDesk staging.\nWake me if replies stop sending or the inbox gets slow.\nTell #support-eng on Slack.")
    assert [m["kind"] for m in w["matters"]] == ["error_rate", "latency_p95"]
    assert w["channel"] == {"kind": "slack_webhook", "name": "#support-eng"} and w["night"] == "wake" and w["env"] == "staging"
    assert watch_app.read_words("Fix it alone if checkout is down; email ops@acme.io")["night"] == "alone"
    assert watch_app.read_words("email ops@acme.io")["channel"]["kind"] == "email"
    assert watch_app.read_words("wait until the morning")["night"] == "wait"
    assert watch_app.read_words("prod, https://helpdesk.example/x")["url"] == "https://helpdesk.example/x"
    assert watch_app.read_words("")["matters"] == []
