"""WHERE PEOPLE DECIDE, CLICK BY CLICK (docs/product/wireframes/agentic-factory/
1-where-people-decide.html; AUDIT.md §1).

Govern › Where people decide is the one list of decisions that wait for a person: the summary
line, the 7 + 1 cards and their popovers, "Change a rule in plain words" → the Rule change card,
and the doors out (What agents did ›, Stop all, More › Per-area autonomy). Every control is
pressed the way a person presses it, and each test asserts what the person sees and what the API
now holds. See conftest.py for the environment and the guard every test ends with.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PROD_WAIT, PROJECT, Person, api, board, card, dev, sql

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
STEP_UP_401 = (r"^401 (PUT|POST) /v1/decide/", r"^401 POST /v1/auth/step-up", r"status of 401")


def _around_now() -> tuple[str, str]:
    """A quiet window that holds right now (UTC): from yesterday 00:00 to tomorrow 23:00."""
    today = datetime.now(timezone.utc).weekday()
    return DAYS[(today - 1) % 7], DAYS[(today + 1) % 7]


def _add_window(p: Person, pop, from_day: str, from_time: str, to_day: str, to_time: str) -> None:
    pop.locator("[data-testid='wd-window-new']").click()
    form = pop.locator("[data-testid='wd-window-form']")
    form.wait_for()
    form.get_by_label("From day").select_option(from_day)
    form.get_by_label("From time").fill(from_time)
    form.get_by_label("To day").select_option(to_day)
    form.get_by_label("To time").fill(to_time)
    form.locator("[data-testid='wd-window-add']").click()
    form.wait_for(state="detached")


# ── the summary line ────────────────────────────────────────────────────────────────────────

def test_the_summary_line_counts_and_links_the_oldest_wait(person: Person):
    p = person
    p.go()
    p.tid("wd-sum-agents").wait_for()
    status, act = api("GET", f"/v1/decide/activity?project_id={PROJECT}")
    assert status == 200, act
    s = act["summary"]
    assert p.tid("wd-sum-agents").inner_text() == str(s["handled_by_agents"])
    assert p.tid("wd-sum-asked").inner_text() == str(s["asked_a_person"])
    assert p.tid("wd-sum-waiting").inner_text() == str(s["waiting"])
    assert s["waiting"] >= 2, "the seeded merge and production deploy are waiting"
    assert "Only these 7 wait for a person" in p.tid("wd").inner_text()
    assert p.p.locator("[data-testid='wd-card']").count() == 8, "7 kinds + the fixed 8th"
    # Each card's 24 h count is the API's.
    for c in board()["cards"]:
        if not c["fixed"]:
            assert p.card(c["key"]).locator("[data-testid='wd-card-count']").inner_text().startswith(f"{c['count_24h']} ")
    p.check("summary")

    oldest = s["oldest"]
    link = p.tid("wd-sum-oldest")
    assert link.get_attribute("title") == oldest["question"]
    link.click()
    p.p.wait_for_url(re.compile(rf"area=decisions.*decision={oldest['id']}"), timeout=60_000)
    p.p.wait_for_selector("section[data-area='decisions']", timeout=120_000)
    p.p.get_by_text(oldest["question"]).first.wait_for(timeout=60_000)


# ── a card's popover ────────────────────────────────────────────────────────────────────────

def test_a_card_changes_who_backup_and_window_and_saves_with_step_up(person: Person):
    p = person
    p.expect(*STEP_UP_401)
    p.go()
    pop = p.open_card("prod_deploy")
    trail = pop.locator("[data-testid='wd-trail']").inner_text()
    assert "WD Owner approved" in trail, trail
    assert not re.search(r"\b(ceo|cto|release_manager|autonomy:|auto_approval)\b", trail), f"a raw key on the trail: {trail}"
    assert "Never" in pop.inner_text(), "Full auto is locked"
    pop.locator("[data-testid='wd-pick-decider']").select_option("p:usr_wdsarah")
    pop.locator("[data-testid='wd-pick-backup']").select_option("p:usr_wddev")
    assert "answers after 120 min" in pop.inner_text()
    _add_window(p, pop, "fri", "15:00", "mon", "08:00")
    assert pop.locator("[data-testid='wd-window']").count() == 1
    assert "Fri 15:00 → Mon 08:00" in pop.locator("[data-testid='wd-window']").inner_text()
    p.check("popover filled")

    # cancel the step-up: said in words, nothing saved, Save usable again
    dev(mfa="lapsed")
    pop.locator("[data-testid='wd-save']").click()
    p.step_up(confirm=False)
    refusal = pop.locator("[data-testid='wd-refusal']")
    refusal.wait_for()
    said = refusal.inner_text()
    assert said.startswith("Not saved") and "confirm" in said.lower(), said
    assert "step_up" not in said
    assert pop.locator("[data-testid='wd-save']").is_enabled()
    assert card("prod_deploy")["rule"]["decider"].get("person") is None, "a cancelled step-up saves nothing"

    # confirm: the same Save goes through and the card says so
    pop.locator("[data-testid='wd-save']").click()
    p.step_up(confirm=True)
    pop.wait_for(state="detached", timeout=30_000)
    saved = card("prod_deploy")["rule"]
    assert saved["decider"] == {"person": "usr_wdsarah", "name": "Sarah Chen"} or saved["decider"]["person"] == "usr_wdsarah", saved
    assert saved["backup"]["person"] == "usr_wddev"
    assert saved["quiet"][0]["from_day"] == "fri" and saved["quiet"][0]["to_time"] == "08:00"
    c = p.card("prod_deploy")
    c.locator("[data-testid='wd-card-who']").filter(has_text="Sarah Chen").wait_for(timeout=40_000)
    assert "quiet Fri 15:00 → Mon 08:00" in c.inner_text()
    # the change is on the card's trail
    pop = p.open_card("prod_deploy")
    assert "changed it" in pop.locator("[data-testid='wd-trail']").inner_text()
    p.check("saved")


def test_a_card_scope_this_project_adds_to_the_install(person: Person):
    p = person
    p.go()
    pop = p.open_card("prod_deploy")
    pop.locator("[data-testid='wd-scope-project']").click()
    assert pop.locator("[data-testid='wd-scope-project']").get_attribute("aria-checked") == "true"
    pop.locator("[data-testid='wd-pick-backup']").select_option("p:usr_wdsarah")
    pop.locator("[data-testid='wd-save']").click()
    pop.wait_for(state="detached", timeout=30_000)
    c = card("prod_deploy")
    assert c["scope"]["projects"] == [PROJECT], c["scope"]
    assert c["rule"].get("backup") in (None, {}), "the install's rule is unchanged"
    assert (c["here"].get("backup") or {}).get("person") == "usr_wdsarah", "this project adds the backup"
    p.card("prod_deploy").locator(".wd-scope").filter(has_text="1 project").wait_for(timeout=40_000)
    p.check("scope")


def test_a_looser_edit_is_refused_in_words(person: Person):
    p = person
    p.expect(r"^422 PUT /v1/decide/rules/spend", r"status of 422")
    p.go()
    pop = p.open_card("spend")
    cap = pop.locator("[data-testid='wd-cap']")
    assert cap.input_value() == "500"
    cap.fill("900")
    pop.locator("[data-testid='wd-save']").click()
    refusal = pop.locator("[data-testid='wd-refusal']")
    refusal.wait_for()
    said = refusal.inner_text()
    assert re.search(r"\$?500", said) and re.search(r"stricter|looser|at most|above|backstop|cap", said, re.I), said
    assert "422" not in said and "stricter_only" not in said, f"said in words, not a code: {said}"
    assert card("spend")["rule"]["cap_usd"] == 500, "nothing was saved"
    # a stricter cap saves
    cap.fill("300")
    pop.locator("[data-testid='wd-save']").click()
    pop.wait_for(state="detached", timeout=30_000)
    assert card("spend")["rule"]["cap_usd"] == 300
    p.card("spend").locator("[data-testid='wd-card-line']").filter(has_text="$300").wait_for(timeout=40_000)
    p.check("looser refused")


def test_the_fixed_card_explains_and_has_no_save(person: Person):
    p = person
    p.go()
    pop = p.open_card("change_list")
    assert pop.locator("[data-testid='wd-save']").count() == 0
    assert "Owner · passkey · always" in pop.inner_text()
    pop.get_by_role("button", name="Cancel").click()
    pop.wait_for(state="detached")
    p.check("fixed")


# ── change a rule in plain words ────────────────────────────────────────────────────────────

WORDS = ("Nothing goes to prod on Fridays after 3pm. Anything over $200 a day needs Sarah, not me. "
         "Let agents merge into staging branches on their own.")


def test_a_rule_in_words_becomes_a_rule_change_card_and_applies(person: Person):
    p = person
    p.expect(*STEP_UP_401)
    dev(model="cli")
    p.go()
    box = p.tid("wd-words-input")
    box.fill(WORDS)
    p.tid("wd-words-read").click()
    p.tid("wd-rule-change-reading").wait_for(timeout=5_000)
    p.p.wait_for_selector("[data-testid='wd-rule-change'], [data-testid='wd-rule-change-fallback']", timeout=240_000)
    assert p.tid("wd-rule-change").count() == 1, f"Helm did not read it: {p.tid('wd-rule-change-fallback').inner_text()}"
    rc = p.tid("wd-rule-change")
    text = rc.inner_text()
    assert "Rule change" in text and "understood" not in text.lower(), text[:300]
    edits = rc.locator("[data-testid='wd-edit']")
    assert edits.count() >= 2, text
    kinds = [k.lower() for k in rc.locator("[data-testid='wd-edit'] .wd-lbl").all_inner_texts()]
    assert any("production deploy" in k for k in kinds) and any("spend" in k for k in kinds), kinds
    assert rc.locator("[data-testid='wd-already']").count() >= 1, "merging into staging is already allowed"
    assert "Nothing to change." in rc.locator("[data-testid='wd-already']").first.inner_text()
    assert rc.locator("[data-testid='wd-replay']").count() == 1
    replay = rc.locator("[data-testid='wd-replay']").inner_text()
    assert "last 30 days" in replay.lower() and "would have" in replay, replay
    p.check("rule change")

    # Edit: the drop buttons and the cap field appear; Done puts them away
    rc.locator("[data-testid='wd-edit-toggle']").click()
    assert rc.locator("[data-testid='wd-edit-drop']").count() == edits.count()
    capped = rc.get_by_label("Cap")
    if capped.count():
        capped.first.fill("150")
        p.p.wait_for_timeout(800)
    rc.locator("[data-testid='wd-edit-toggle']").click()
    assert rc.locator("[data-testid='wd-edit-drop']").count() == 0
    n = edits.count()
    apply = rc.locator("[data-testid='wd-apply']")
    assert apply.inner_text().strip().endswith(f"Apply {n} change{'s' if n > 1 else ''}"), apply.inner_text()

    # Apply asks for the passkey (changing the list is on the list)
    dev(mfa="lapsed")
    apply.click()
    p.step_up(confirm=True)
    rc.wait_for(state="detached", timeout=30_000)
    assert box.input_value() == "", "the words are cleared once applied"
    spend = card("spend")["rule"]
    prod = card("prod_deploy")["rule"]
    assert spend["cap_usd"] <= 200 or (spend.get("decider") or {}).get("person") == "usr_wdsarah", spend
    assert prod["quiet"] and prod["quiet"][0]["from_day"] == "fri", prod
    p.card("prod_deploy").filter(has_text="quiet Fri").wait_for(timeout=40_000)
    pop = p.open_card("spend")
    assert "in words" in pop.locator("[data-testid='wd-trail']").inner_text()
    p.check("applied")


def test_no_model_keeps_the_words(person: Person):
    p = person
    dev(model="none")
    p.go()
    p.tid("wd-words-input").fill(WORDS)
    p.tid("wd-words-input").press("Meta+Enter" if "Mac" in p.p.evaluate("navigator.platform") else "Control+Enter")
    fb = p.tid("wd-rule-change-fallback")
    fb.wait_for(timeout=30_000)
    assert "words are kept" in fb.inner_text().lower()
    assert p.tid("wd-words-input").input_value() == WORDS
    p.check("no model")


# ── the doors out ───────────────────────────────────────────────────────────────────────────

def test_what_agents_did_opens_agents_at_work(person: Person):
    p = person
    p.go()
    link = p.tid("wd-what-agents-did")
    link.click()
    p.p.wait_for_url(re.compile(r"area=agent_ops"), timeout=60_000)
    p.p.wait_for_selector("[data-testid='wd-lanes']", timeout=180_000)
    status, act = api("GET", f"/v1/decide/activity?project_id={PROJECT}")
    assert status == 200
    agents = p.p.locator("[data-testid='wd-agent']")
    agents.first.wait_for(timeout=60_000)
    assert agents.count() == len(act["agents"]), (agents.count(), len(act["agents"]))
    first = agents.first
    assert first.locator(".wd-lane i").count() == 24, "a 24 h lane, hour by hour"
    assert first.locator(".wd-split").count() == 1
    p.check("agents at work")


def test_stop_all_opens_the_confirm_and_cancel_stops_nothing(person: Person):
    p = person
    p.go()
    posts: list[str] = []
    p.p.on("request", lambda r: posts.append(r.url) if r.method != "GET" and "/v1/" in r.url else None)
    p.tid("wd-stop-all").click()
    confirm = p.tid("aa-kill-confirm")
    confirm.wait_for(timeout=30_000)
    assert p.tid("aa-kill-confirm-go").is_visible(), "Stop now is offered"
    p.check("stop confirm")
    confirm.get_by_role("button", name="Cancel").click()
    confirm.wait_for(state="detached")
    assert not posts, f"Cancel stops nothing: {posts}"
    assert "Agents stopped" not in p.tid("wd-agents-state").inner_text()


def test_more_per_area_autonomy_keeps_the_old_panel_and_the_old_link_lands_here(person: Person):
    p = person
    p.go("area=budget&tab=autonomy")
    assert p.tid("gv-tab-autonomy").get_attribute("aria-selected") == "true"
    assert p.tid("gv-tab-autonomy").inner_text() == "Where people decide"
    p.tid("gv-more").locator("summary").click()
    p.tid("gv-tab-areas").click()
    p.tid("aa-autonomy").wait_for(timeout=60_000)
    assert p.tid("wd").count() == 0
    assert "tab=areas" in p.p.url
    p.check("per-area autonomy")


# ── a quiet window really holds an approval ─────────────────────────────────────────────────

def test_a_quiet_window_holds_an_approval(person: Person):
    p = person
    p.go()
    pop = p.open_card("prod_deploy")
    start, end = _around_now()
    _add_window(p, pop, start, "00:00", end, "23:00")
    pop.locator("[data-testid='wd-save']").click()
    pop.wait_for(state="detached", timeout=30_000)
    p.card("prod_deploy").locator(".wd-quiet-chip").wait_for(timeout=40_000)

    # the approval is refused, in words, and the decision still waits
    status, body = api("POST", f"/v1/decisions/{PROD_WAIT}/approve", {"actor": "usr_wddev"})
    assert status == 409, (status, body)
    said = body.get("detail") if isinstance(body, dict) else str(body)
    assert "quiet window" in said and "waits until" in said, said
    assert sql("SELECT status FROM decisions WHERE id = :i", i=PROD_WAIT)[0][0] == "pending"

    # and it is the window that held it: without the window the same approval goes through
    sql("DELETE FROM decide_rules")
    status, body = api("POST", f"/v1/decisions/{PROD_WAIT}/approve", {"actor": "usr_wddev"})
    assert status in (200, 202), (status, body)
    p.check("quiet window")


@pytest.mark.parametrize("bad", ["a staging deploy"])
def test_a_staging_deploy_is_not_on_the_list(bad: str):
    """The classifier the hold rests on: a staging deploy is the agents' to ship (API read)."""
    b = board()
    prod = next(c for c in b["cards"] if c["key"] == "prod_deploy")
    assert all("staging" not in (t.get("what") or "").lower() for t in prod["trail"]), prod["trail"]
