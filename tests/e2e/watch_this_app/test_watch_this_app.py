"""PROJECT › HEALTH › WATCH THIS APP, click by click (docs/product/wireframes/agentic-factory/
2-watch-an-app.html), at 1440 and 390.

Every control and path of the wireframe: the door on Health, the one input and its read, the kind
pills, the Agent actions card (how it talks, what matters, the open question, the watches with a
7-day backtest), a watch's popover (sparkline, chips, PromQL, the full builder), the six boxes and
their popovers, the alone / waits strip, Wire it myself, Start watching (with the consent the
first one asks), the landing (diagram, Now line, the one gate: an outside channel's code), a
test alert firing into the SRE flow, quiet hours, production waiting for a person, a later edit
that asks why, refusals in words, and the old Observability and Health alert screens still there.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import (APP, HOOK, PROD, PROJECT, STAGING, WORDS, Person, api, code_for, deliver, dev, fire, revoke_grants,
                      rules_of, sql)

ROOT = Path(__file__).resolve().parents[3]


def _start_default(p: Person) -> str:
    p.open_sheet()
    p.type()
    p.slack_url()
    return p.start()


# ── the door, the input and the agent's read ────────────────────────────────────────────────

def test_the_door_on_health_opens_one_input_and_the_agent_reads_the_app(person: Person):
    p = person
    p.go()
    assert "Watch this app" in p.tid("px-h-watch").inner_text()
    p.check("the door")
    p.tid("px-h-watch").click()
    p.tid("wa-sheet").wait_for()
    assert "watch=1" in p.p.url, "the sheet has a URL any page can link to"
    focused = p.p.evaluate("() => document.activeElement && document.activeElement.getAttribute('data-testid')")
    assert focused == "wa-input", "the one input has the focus"
    assert p.tid("wa-agent-actions").count() == 0, "nothing is read before anything is typed"
    p.type()
    # detected: a project app, matched to the staging app
    assert p.tid("wa-kind-project").get_attribute("aria-checked") == "true"
    assert f"{APP} · staging" in p.tid("wa-matched").inner_text()
    card = p.tid("wa-agent-actions")
    head = card.locator("h3").inner_text()
    assert head == "Agent actions", head
    assert "understood" not in card.inner_text().lower()
    # how it talks, what matters (your words first), the open question
    assert "probe" in p.tid("wa-aa-signals").inner_text().lower() and "no traces" in p.tid("wa-aa-signals").inner_text()
    matters = p.tid("wa-aa-matters").inner_text()
    assert "replies stop sending" in matters and "the inbox gets slow" in matters and "your words" in matters
    assert "wake you or wait until 08:00" in p.tid("wa-question").inner_text()
    assert p.tid("wa-night-wake").get_attribute("aria-checked") == "true", "'Wake me' in the words answers it"
    # the watches: from your words, closer than the default, each with a 7-day backtest
    assert p.tid("wa-watch-title-error_rate").inner_text() == "Errors > 2% for 5 min"
    assert p.tid("wa-watch-title-latency_p95").inner_text() == "p95 > 800 ms for 5 min"
    for kind in ("error_rate", "latency_p95", "health_check", "deploy_health"):
        assert re.search(r"would have fired \d+× in 7 d", p.tid(f"wa-watch-{kind}").inner_text()), kind
    assert "wake" in p.tid("wa-watch-error_rate").inner_text() and "tell" in p.tid("wa-watch-latency_p95").inner_text()
    assert "baseline 0 failed probes" in p.tid("wa-watch-health_check").inner_text(), "the probe history is the baseline"
    # the six boxes and the strip
    for b in ("signals", "collector", "watches", "oncall", "tell", "runbooks"):
        assert p.tid(f"wa-box-{b}").is_visible(), b
    assert "4 watches" in p.tid("wa-box-watches").inner_text() and "2 wake · 2 tell" in p.tid("wa-box-watches").inner_text()
    assert "#support-eng · you" in p.tid("wa-box-tell").inner_text() and "verify once" in p.tid("wa-box-tell").inner_text()
    assert p.tid("wa-box-tell").get_attribute("data-amber") is not None
    assert "triage + fix alone" in p.tid("wa-box-oncall").inner_text()
    strip = p.tid("wa-strip").inner_text()
    assert "open a fix PR" in strip and "prod rollback" in strip and "status page" in strip
    # Looks right / Fix it / a hint
    p.tid("wa-looks-right").click()
    assert p.tid("wa-looks-right").get_attribute("aria-pressed") == "true"
    p.tid("wa-fix-it").click()
    p.tid("wa-pop-watches").wait_for()
    p.tid("wa-pop-done").click()
    p.tid("wa-hint").fill("also wake me if no traffic for an hour")
    p.tid("wa-hint").press("Enter")
    assert "no traffic" in p.tid("wa-input").input_value()
    p.p.wait_for_function("() => !!document.querySelector('[data-testid=wa-watch-no_traffic]')", timeout=60_000)
    p.check("the read")
    # Esc closes; nothing was written
    p.p.keyboard.press("Escape")
    p.tid("wa-sheet").wait_for(state="detached")
    assert "watch=" not in p.p.url
    assert not sql("SELECT 1 FROM app_watches WHERE target_id = :t", t=STAGING)


def test_a_watch_popover_keeps_the_builder_as_chips_and_the_promql_visible(person: Person):
    p = person
    p.open_sheet()
    p.type()
    p.tid("wa-watch-latency_p95").click()
    pop = p.tid("wa-pop-latency_p95")
    pop.wait_for()
    assert p.tid("wa-spark-latency_p95").count() == 1
    assert "histogram_quantile(0.95" in p.tid("wa-promql-latency_p95").inner_text()
    assert "168 h" in p.tid("wa-backtest-latency_p95").inner_text(), "the backtest replays 7 days"
    assert "#support-eng" in pop.inner_text()
    p.tid("wa-p-latency_p95-threshold").fill("600")
    p.tid("wa-p-latency_p95-for").fill("10")
    p.tid("wa-tone-latency_p95-wake").click()
    assert p.tid("wa-watch-title-latency_p95").inner_text() == "p95 > 600 ms for 5 min", "the line follows the chips"
    href = p.tid("wa-builder-latency_p95").get_attribute("href")
    assert "area=observability" in href and f"app={STAGING}" in href and "atab=alerts" in href
    p.check("the popover")
    p.tid("wa-done-latency_p95").click()
    assert pop.count() == 0
    assert "wake" in p.tid("wa-watch-latency_p95").inner_text()
    # Remove takes it out (and back)
    p.tid("wa-watch-deploy_health").click()
    p.tid("wa-remove-deploy_health").click()
    assert "removed" in p.tid("wa-watch-deploy_health").inner_text()
    assert "3 watches" in p.tid("wa-box-watches").inner_text()
    p.tid("wa-watch-deploy_health").click()
    p.tid("wa-remove-deploy_health").click()
    assert "4 watches" in p.tid("wa-box-watches").inner_text()
    # what was chosen is what is saved
    p.slack_url()
    target = p.start()
    assert target == STAGING
    r = rules_of(STAGING)
    lat = r["latency_p95"]
    assert float(lat["params"]["threshold"]) == 600 and lat["for_seconds"] == 600
    assert lat["labels"]["severity"] == "critical" and lat["urgency"] == "high", "wake = a page"
    p.check("saved")


def test_every_box_opens_its_popover(person: Person):
    p = person
    p.open_sheet()
    p.type()
    pop = p.box("signals")
    assert "nothing is redeployed" in pop.inner_text()
    p.tid("wa-pop-done").click()
    pop = p.box("collector")
    assert "Sources" in pop.inner_text() and "view=sources" in p.tid("wa-sources-link").get_attribute("href")
    p.tid("wa-pop-done").click()
    p.box("watches")
    p.tid("wa-on-health_check").uncheck()
    assert "3 watches" in p.tid("wa-box-watches").inner_text()
    p.tid("wa-on-health_check").check()
    p.tid("wa-pop-done").click()
    pop = p.box("oncall")
    assert p.tid("wa-alone").is_checked() and p.tid("wa-alone").is_enabled()
    p.tid("wa-alone").uncheck()
    assert "fixes wait for you" in p.tid("wa-box-oncall").inner_text()
    assert "every fix" in p.tid("wa-strip").inner_text()
    p.tid("wa-alone").check()
    assert "autonomy" in p.tid("wa-decide-link").get_attribute("href")
    p.tid("wa-pop-done").click()
    pop = p.box("tell")
    assert p.tid("wa-tell-slack_webhook").get_attribute("aria-checked") == "true", "Slack, from the words"
    assert p.tid("wa-tell-name").input_value() == "#support-eng"
    assert p.tid("wa-tell-url").get_attribute("type") == "password", "a webhook URL is write-only"
    assert "one step that waits for you" in p.tid("wa-tell-gate").inner_text()
    p.tid("wa-tell-email").click()
    p.tid("wa-tell-name").fill("oncall@acme.example")
    assert "oncall@acme.example" in p.tid("wa-box-tell").inner_text()
    p.tid("wa-tell-you").click()
    assert p.tid("wa-box-tell").inner_text().split("\n")[1].strip() == "you"
    assert p.tid("wa-box-tell").get_attribute("data-amber") is None, "telling only you needs no gate"
    p.tid("wa-pop-done").click()
    pop = p.box("runbooks")
    assert "app-serving" in pop.inner_text() and "app-down" in pop.inner_text()
    p.tid("wa-pop-done").click()
    p.check("boxes")


def test_kind_pills_and_wire_it_myself_keep_every_old_path(person: Person):
    p = person
    p.open_sheet()
    p.type("https://status.example.org/health")
    p.tid("wa-kind-url").click()
    p.p.wait_for_function("() => /wired by hand/.test(document.querySelector('[data-testid=wa-matched]')?.textContent || '')", timeout=30_000)
    assert p.tid("wa-agent-actions").count() == 0
    p.tid("wa-submit").click()
    assert "Wire it myself keeps every path" in p.tid("wa-refusal").inner_text()
    p.tid("wa-kind-elsewhere").click()
    p.p.wait_for_timeout(600)
    p.tid("wa-kind-project").click()
    p.tid("wa-wire").click()
    p.tid("obs-connect-sheet").wait_for()
    for path in ("boss", "otel", "collector", "remote_write", "metrics_url", "url", "alerts"):
        assert p.tid(f"obs-connect-{path}").count() == 1, path
    assert "watch=1" in p.tid("obs-connect-watch").get_attribute("href"), "the old sheet points back at the one input"
    p.check("wire it myself")


# ── Start watching: the rules, the channel, the gate ────────────────────────────────────────

def test_start_watching_sets_the_rules_the_channel_and_asks_only_for_the_code(person: Person):
    p = person
    revoke_grants()
    p.open_sheet()
    p.type()
    p.slack_url()
    p.tid("wa-submit").click()
    assert p.consent(), "Respond automatically asks the person's standing grant the first time"
    p.tid("wa-sheet").wait_for(state="detached", timeout=60_000)
    card = p.tid(f"wa-landing-{STAGING}")
    card.wait_for()
    assert "watch=" not in p.p.url
    assert "watched" in p.tid("wa-l-state").inner_text()
    steps = {k: p.tid(f"wa-l-step-{k}").get_attribute("data-state") for k in ("read", "collector", "data", "watches", "verify", "oncall")}
    assert steps == {"read": "done", "collector": "done", "data": "done", "watches": "done", "verify": "you", "oncall": "run"}, steps
    assert re.search(r"\b20\d\d probes", p.tid("wa-l-step-data").inner_text()), "a week of BOSS probes is the first data"
    assert "#support-eng waits for the code" in p.tid("wa-l-now-text").inner_text()
    assert "Verify #support-eng" in p.tid("wa-l-gate").inner_text()
    p.check("landed")
    # what BOSS wrote: the app's own alert rules, triaged, answering alone on staging
    r = rules_of(STAGING)
    for kind in ("error_rate", "latency_p95", "health_check", "deploy_health"):
        assert r[kind]["enabled"] and r[kind]["triage"] and r[kind]["auto_respond"], (kind, r[kind])
    assert float(r["error_rate"]["params"]["threshold"]) == 2.0, "the failure you named is watched closer"
    assert r["error_rate"]["urgency"] == "high" and r["latency_p95"]["urgency"] == "low"
    why = sql("SELECT reason FROM monitoring_changes WHERE project_id = :p AND object_kind = 'app_watch' AND object_id = :t "
              "ORDER BY changed_at DESC LIMIT 1", p=PROJECT, t=STAGING)
    assert why and "replies stop sending" in why[0][0], "your words are the why"
    cp = sql("SELECT id, kind, verified_at, secret_ref IS NOT NULL FROM contact_points WHERE project_id = :p AND name = '#support-eng'", p=PROJECT)
    assert cp and cp[0][1] == "slack_webhook" and cp[0][2] is None and cp[0][3], cp
    routes = sql("SELECT rule_id, contact_point_ids FROM notification_policies WHERE project_id = :p AND rule_id IS NOT NULL", p=PROJECT)
    assert {x[0] for x in routes} == {r[k]["id"] for k in ("error_rate", "latency_p95", "health_check", "deploy_health")}
    assert all(cp[0][0] in x[1] for x in routes), "every watch tells the channel"
    page_text = p.p.content()
    assert HOOK not in page_text, "the webhook URL is never shown back"
    # the one gate: a wrong code is refused in words; the right one opens the channel
    p.tid("wa-l-code").fill("12345")
    assert p.tid("wa-l-verify").is_disabled(), "six digits first"
    p.tid("wa-l-code").fill("000001")
    p.expect(r"422 POST /v1/projects/[^ ]+/verify", r"status of 422")
    p.tid("wa-l-verify").click()
    p.tid("wa-l-said").wait_for()
    assert "not the code" in p.tid("wa-l-said").inner_text()
    p.tid("wa-l-resend").click()
    p.p.wait_for_function("() => /new code/.test(document.querySelector('[data-testid=wa-l-said]')?.textContent || '')")
    p.tid("wa-l-code").fill(code_for("#support-eng"))
    p.tid("wa-l-verify").click()
    p.tid("wa-l-gate").wait_for(state="detached", timeout=30_000)
    assert p.tid("wa-l-step-verify").get_attribute("data-state") == "done"
    assert sql("SELECT verified_at IS NOT NULL FROM contact_points WHERE id = :i", i=cp[0][0])[0][0]
    # the old door on Health now says so
    assert "Watch another app" in p.tid("px-h-watch").inner_text()
    p.check("verified")


def test_a_test_alert_fires_into_the_sre_flow(person: Person):
    p = person
    _start_default(p)
    p.tid("wa-l-code").fill(code_for("#support-eng"))
    p.tid("wa-l-verify").click()
    p.tid("wa-l-gate").wait_for(state="detached", timeout=30_000)
    got = fire(STAGING, "error_rate", 9.0)
    fired = got["results"][0]["fired"] or got["results"][1]["fired"]
    assert fired, got
    p.p.wait_for_function("() => /firing/.test(document.querySelector('[data-testid=wa-l-now-text]')?.textContent || '')", timeout=60_000)
    assert "the SRE agent is on it" in p.tid("wa-l-now-text").inner_text()
    assert "firing" in p.tid("wa-l-watch-error_rate").inner_text()
    flow = p.p.locator("[data-testid^='wa-l-inc-']").first
    flow.wait_for(timeout=30_000)
    text = flow.inner_text()
    assert "Error rate" in text and "SRE triage" in text and "staging · no" in text and "Fix alone" in text, text
    # the real record: the alert, its incident, the agent's triage (no model on the dev stack),
    # the response on the person's grant, and the routed message to the verified channel
    alert = sql("SELECT state, triage_state, incident_id FROM app_alerts WHERE id = :a", a=fired[0])[0]
    assert alert[0] == "firing" and alert[2], alert
    assert alert[1] == "done", alert  # the on-call agent decided (the scripted model: open the incident)
    inc = sql("SELECT status FROM incidents WHERE id = :i", i=alert[2])[0][0]
    assert inc in ("open", "triaged", "diagnosing", "remediating"), inc
    # THE RESPONSE (fix alone) went through the one start door on the person's grant: on a
    # stack with a model and a repo it runs; here the door refuses in words, and says so.
    response = sql("SELECT response_run_id, response_word FROM app_alerts WHERE id = :a", a=fired[0])[0]
    runs = sql("SELECT on_behalf_of FROM workflow_runs WHERE dedupe_key = :k", k=f"alert:{fired[0]}:respond")
    assert (runs and runs[0][0] == "usr_asdev") or (response[1] and response[1].startswith("Not started:")), response
    assert "There is no published" not in (response[1] or ""), "Start watching installs the response workflow"
    shown = p.p.locator("[data-testid^='wa-l-inc-response-']").first
    p.p.wait_for_function("(w) => (document.querySelector('[data-testid^=wa-l-inc-response-]')?.getAttribute('title') || '') === w",
                          arg=response[1] or "", timeout=60_000)
    assert shown.is_visible()
    deliver()  # the worker's notify clock: route what fired
    groups = sql("SELECT count(*) FROM notification_groups WHERE project_id = :p", p=PROJECT)
    assert groups and groups[0][0] >= 1, "the alert is routed to its contact points"
    # the Health page's incident list and the alerts list see the same incident
    assert p.p.locator("[data-testid^='px-h-inc-flow-']").count() >= 1, p.tid("px-h-incidents").inner_text()
    p.check("firing")
    fire(STAGING, "error_rate", clear=True)


def test_wait_till_eight_sets_quiet_hours_and_fix_alone_answers_overnight(person: Person):
    p = person
    p.open_sheet()
    p.type("HelpDesk staging. Tell me if the inbox gets slow.")
    assert p.tid("wa-night-wake").get_attribute("aria-checked") == "true", "unanswered: wake is the default"
    p.tid("wa-night-wait").click()
    assert p.tid("wa-night-wait").get_attribute("aria-checked") == "true"
    assert p.tid("wa-box-tell").get_attribute("data-amber") is None, "no outside channel: no gate"
    p.start()
    assert p.tid("wa-l-gate").count() == 0, "only you: nothing to verify"
    assert p.tid("wa-l-step-verify").count() == 0
    assert "wait till 08:00" in p.tid(f"wa-landing-{STAGING}").inner_text()
    r = rules_of(STAGING)
    assert all(v["urgency"] == "low" for k, v in r.items() if v["enabled"] and k != "no_traffic"), r
    quiet = sql("SELECT quiet_hours FROM notification_policies WHERE project_id = :p AND rule_id IS NOT NULL", p=PROJECT)
    assert quiet and all(q[0] and q[0]["start"] == "22:00" and q[0]["end"] == "08:00" for q in quiet), quiet
    assert sql("SELECT night FROM app_watches WHERE target_id = :t", t=STAGING)[0][0] == "wait"
    p.check("quiet hours")


def test_production_waits_for_a_person(person: Person):
    p = person
    p.open_sheet()
    p.type("HelpDesk prod. Wake me if it goes down.")
    assert "prod" in p.tid("wa-matched").inner_text()
    assert "prod waits for you" in p.tid("wa-box-oncall").inner_text()
    p.box("oncall")
    assert p.tid("wa-alone").is_disabled() and not p.tid("wa-alone").is_checked()
    p.tid("wa-pop-done").click()
    strip = p.tid("wa-strip").inner_text()
    assert "every fix" in strip and "open a fix PR" not in strip
    target = p.start(consent=False)
    assert target == PROD
    r = rules_of(PROD)
    assert all(v["triage"] and not v["auto_respond"] for v in r.values() if v["enabled"]), "on prod the agent triages; a fix waits"
    got = fire(PROD, "health_check", 5)
    assert got["results"][0]["fired"] or got["results"][1]["fired"]
    flow = p.p.locator("[data-testid^='wa-l-inc-']").first
    flow.wait_for(timeout=60_000)
    assert "prod · yes" in flow.inner_text() and "You approve" in flow.inner_text()
    p.check("prod")
    fire(PROD, "health_check", clear=True)


def test_a_later_edit_asks_why_and_keeps_it(person: Person):
    p = person
    p.open_sheet()
    p.type("HelpDesk staging. Wake me if replies stop sending.")
    p.start()
    p.tid("wa-l-watch-latency_p95").click()
    p.tid("wa-pop-latency_p95").wait_for()
    p.tid("wa-p-latency_p95-threshold").fill("900")
    assert p.tid("wa-done-latency_p95").is_disabled(), "a later change needs its why"
    assert p.tid("wa-remove-latency_p95").is_disabled()
    p.tid("wa-why-latency_p95").fill("Monday imports make the inbox slower")
    p.tid("wa-done-latency_p95").click()
    p.tid("wa-pop-latency_p95").wait_for(state="detached")
    assert "900 ms" in p.tid("wa-l-watch-latency_p95").inner_text()
    why = sql("SELECT reason FROM monitoring_changes WHERE project_id = :p AND object_id = :r ORDER BY changed_at DESC LIMIT 1",
              p=PROJECT, r=rules_of(STAGING)["latency_p95"]["id"])
    assert why and why[0][0] == "Monday imports make the inbox slower", why
    p.tid("wa-l-watch-deploy_health").click()
    p.tid("wa-why-deploy_health").fill("deploy health is on the release page")
    p.tid("wa-remove-deploy_health").click()
    p.tid("wa-l-watch-deploy_health").wait_for(state="detached")
    assert not rules_of(STAGING)["deploy_health"]["enabled"]
    # the API refuses a change without a why, in words
    status, body = api("POST", f"/v1/projects/{PROJECT}/watch/{STAGING}/change",
                       {"project_id": PROJECT, "rule_id": rules_of(STAGING)["error_rate"]["id"], "reason": "", "params": {"threshold": 3}})
    assert status == 422 and "why" in body["message"].lower()
    p.check("why kept")


# ── refusals, in words ──────────────────────────────────────────────────────────────────────

def test_refusals_say_why_in_words(person: Person):
    p = person
    p.open_sheet()
    p.tid("wa-submit").click()
    assert "Name the app first" in p.tid("wa-refusal").inner_text()
    p.type()
    p.tid("wa-submit").click()
    assert "incoming webhook URL" in p.tid("wa-refusal").inner_text() and "#support-eng" in p.tid("wa-refusal").inner_text()
    p.box("watches")
    for kind in ("error_rate", "latency_p95", "health_check", "deploy_health"):
        p.tid(f"wa-on-{kind}").uncheck()
    p.tid("wa-pop-done").click()
    p.tid("wa-submit").click()
    assert "Keep at least one watch" in p.tid("wa-refusal").inner_text()
    p.box("watches")
    p.tid("wa-on-error_rate").check()
    p.tid("wa-pop-done").click()
    p.box("tell")
    p.tid("wa-tell-url").fill("not a url")
    p.tid("wa-pop-done").click()
    p.expect(r"422 POST /v1/projects/[^ ]+/watch ", r"status of 422")
    p.tid("wa-submit").click()
    p.consent()
    p.tid("wa-refusal").wait_for()
    p.p.wait_for_function("() => /webhook's full URL/.test(document.querySelector('[data-testid=wa-refusal]')?.textContent || '')", timeout=30_000)
    assert not sql("SELECT 1 FROM app_watches WHERE target_id = :t", t=STAGING), "a refused start writes nothing"
    assert not sql("SELECT 1 FROM app_alert_rules WHERE target_id = :t AND source = 'console'", t=STAGING), "…not even the rules"
    # a declined consent is said in words and writes nothing
    revoke_grants()
    p.box("tell")
    p.tid("wa-tell-you").click()
    p.tid("wa-pop-done").click()
    p.tid("wa-submit").click()
    assert p.consent(grant=False)
    p.p.wait_for_function("() => /consent/i.test(document.querySelector('[data-testid=wa-refusal]')?.textContent || '')", timeout=30_000)
    assert not sql("SELECT 1 FROM app_watches WHERE target_id = :t", t=STAGING)
    p.check("refusals")


def test_a_lapsed_second_factor_is_asked_for_before_consent_is_granted(person: Person):
    """Step-up where required: granting the standing consent asks "Confirm it's you" when the
    second factor has lapsed; a cancel is said in words and writes nothing."""
    p = person
    revoke_grants()
    p.open_sheet()
    p.type("HelpDesk staging. Wake me if it goes down.")
    dev(mfa="lapsed")
    p.tid("wa-submit").click()
    p.tid("consent-sheet").wait_for(timeout=30_000)
    p.expect(r"409 POST", r"status of 409", r"401 POST", r"status of 401")
    p.tid("consent-standing").click()
    p.step_up(confirm=True)
    p.tid("wa-sheet").wait_for(state="detached", timeout=60_000)
    p.tid(f"wa-landing-{STAGING}").wait_for()
    p.check("step-up")


# ── nothing old is lost ─────────────────────────────────────────────────────────────────────

def test_the_old_alert_screens_stay_reachable_and_unified(person: Person):
    p = person
    _start_default(p)
    # Health keeps Observability's alert list and rules (the same components), below the landing
    p.tid("px-h-alerts").wait_for()
    assert "Every app in Observability" in p.tid("px-h-alerts").inner_text()
    rules = p.tid(f"px-h-rules-{STAGING}")
    rules.wait_for()
    p.p.wait_for_function(f"() => /Respond automatically/.test(document.querySelector('[data-testid=px-h-rules-{STAGING}]')?.textContent || '')",
                          timeout=60_000)
    href = p.tid(f"px-h-rules-obs-{STAGING}").get_attribute("href")
    assert f"app={STAGING}" in href
    # the global Observability Alerts view keeps its cross-app list
    p.p.goto(p.p.url.split("?")[0] + f"?org=org_boss&area=observability&project_id={PROJECT}&view=alerts", wait_until="domcontentloaded")
    alerts = p.tid("obs-alerts")
    alerts.wait_for(timeout=120_000)
    assert alerts.get_attribute("data-scope") != "project", "Observability keeps every app"
    p.check("observability")


def test_fonts_use_the_type_scale_only():
    css = (ROOT / "apps/web/app/projects/watch-app.css").read_text()
    sizes = re.findall(r"font-size:\s*([^;]+);", css)
    assert sizes and all(s.strip().startswith("var(--fs-") for s in sizes), sizes
    tsx = (ROOT / "apps/web/app/projects/watch-app.tsx").read_text()
    assert not re.search(r"fontSize:\s*[\"'\d]", tsx), "no inline font sizes"
    assert "Agent actions" in tsx and "Here's what I understood" not in tsx
