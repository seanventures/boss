"""ADD A PROJECT, CLICK BY CLICK (new-project-v2 a-add-project.html, b-adjust.html, c-after-create.html).

THE REPORT (operator, 2026-11-07, laptop 0ffdd0ae5): "it's so pathetic ui … every button action is
so aweful". Typing threw React #185 (an update loop); a plain English sentence was read as a
FOLDER ("/srv/apps/expense tracker for our…", "A folder of code"); the folder probe asked for a
step-up and said "Not created: you cancelled the check"; ↵ showed nothing for 90 s; long box
values ran under the footer; Create and start looked disabled with no reason.

Every control and every path of the sheet is pressed here the way a person presses it, and each
test asserts what the person sees and what was made (read back from the API). See conftest.py for
the environment and the guard every test ends with.
"""
from __future__ import annotations

import re
import time

import pytest

from conftest import (CONSOLE, FIRST_READ_S, IDEA, PROJECT, TEN_LINES, Person, api, dev, lines_of,
                      projects_named, started, unique)

ACME = "acme/ledger-api"


def _repo_id(full_name: str) -> str:
    status, body = api("GET", f"/v1/github/repos?project_id={PROJECT}")
    assert status == 200, body
    row = next((r for r in body.get("items", []) if r.get("full_name") == full_name), None)
    assert row, f"{full_name} is not in the dev registry"
    return row["id"]


# ── doors ───────────────────────────────────────────────────────────────────────────────────

def test_every_door_opens_the_one_sheet(person: Person):
    p = person
    # 1 the Projects page's button
    p.go()
    p.tid("px-add-project").click()
    p.sheet.wait_for()
    assert p.box.evaluate("el => el === document.activeElement"), "the input has the focus"
    assert p.kind() == "idea", "an empty box reads as an idea (the wireframe's default)"
    assert "new_project=" in p.p.url
    p.tid("ap-close").click()
    p.sheet.wait_for(state="detached")
    assert "new_project=" not in p.p.url, "closing clears the door from the URL"

    # 2 the sidebar's + Add project (the drawer on a phone)
    if not p.phone:
        p.tid("nav-add-project").click()
        p.sheet.wait_for()
        p.p.keyboard.press("Escape")
        p.sheet.wait_for(state="detached")

    # 3 ⌘K › Add project
    p.p.keyboard.press("Meta+k")
    door = p.tid("palette-new-build")
    door.wait_for()
    assert "Add project" in door.inner_text(), door.inner_text()
    door.click()
    p.sheet.wait_for()
    p.p.keyboard.press("Escape")
    p.sheet.wait_for(state="detached")

    # 4 the New build "Moved" banner
    p.go("tab=new", area="delivery_workflows", wait="[data-testid='moved-new-build']")
    p.tid("moved-new-build-new-app").click()
    p.sheet.wait_for(timeout=60_000)
    assert p.kind() == "idea"
    p.p.keyboard.press("Escape")

    # 5 the links: idea, import (a repo hint), sample (the sample chip lit), 1
    for door, kind in (("idea", "idea"), ("import", "repo"), ("sample", "idea"), ("1", "idea")):
        p.open_sheet(door)
        assert p.kind() == kind, f"?new_project={door} opened as {p.kind()}"
        if door == "sample":
            assert p.tid("ap-sample").get_attribute("data-lit") is not None
        if door == "import":
            p.tid("ap-results").wait_for()
            assert p.p.locator("[data-testid^='ap-repo-']").count() >= 1, "the recent repos are listed"
    p.check("doors")


# ── the one input ───────────────────────────────────────────────────────────────────────────

def test_typing_one_line_ten_lines_and_pasting(person: Person):
    p = person
    p.open_sheet("import")          # the door that read a sentence as a repo
    p.type(IDEA, delay=8)           # key by key: React's update loop showed here
    assert p.kind() == "idea", f"a sentence is an idea, never a {p.kind()}"
    assert p.tid("ap-results").count() == 0, "no repo or folder results for a sentence"
    assert "/srv/apps" not in p.sheet.inner_text()
    assert "1 line" in p.tid("ap-input-foot").inner_text()
    p.check("one line")

    p.type(TEN_LINES)
    assert p.kind() == "idea"
    assert "10 lines" in p.tid("ap-input-foot").inner_text()
    assert 10 <= lines_of(p.box) <= 12, f"10 lines show whole, the box grows to at most 12 ({lines_of(p.box)})"
    p.type(TEN_LINES + "\n" + TEN_LINES)
    assert lines_of(p.box) <= 13, "past 12 lines the box scrolls instead of growing"

    # paste: from the clipboard, into an empty box
    p.type("")
    p.p.evaluate("t => navigator.clipboard.writeText(t)", "https://github.com/acme/ledger-api")
    p.box.click()
    p.p.keyboard.press("Meta+v" if "Mac" in p.p.evaluate("navigator.platform") else "Control+v")
    p.p.wait_for_timeout(300)
    assert p.box.input_value() == "https://github.com/acme/ledger-api"
    assert p.kind() == "repo"
    p.check("paste")


@pytest.mark.parametrize("words,kind", [
    (IDEA, "idea"),
    ("Fix: the login page is slow", "idea"),
    ("acme/ledger-api", "repo"),
    ("https://github.com/acme/ledger-api", "repo"),
    ("github.com/acme/ledger-api.git", "repo"),
    ("/srv/apps/ledger-api", "folder"),
    ("helpdesk", "repo"),              # one bare word: a repo search
    ("/srv/apps/my app for: the team", "idea"),   # a path with a sentence in it is a sentence
])
def test_kind_detection(person: Person, words: str, kind: str):
    p = person
    p.open_sheet("idea")
    p.type(words)
    p.p.wait_for_timeout(200)
    assert p.kind() == kind, f"{words!r} read as {p.kind()}, not {kind}"
    if kind == "idea":
        assert p.tid("ap-results").count() == 0
    if kind == "repo" and "acme" in words:
        first = p.p.locator("[data-testid='ap-results'] [role='option']").first
        assert ACME.split("/")[1] in first.inner_text(), "the exact repo is the top match"
    if kind == "folder":
        rows = p.p.locator("[data-testid^='ap-folder-']")
        rows.first.wait_for()
        assert "/srv/apps/ledger-api" in rows.first.inner_text()
    p.check(f"detect {words}")


def test_a_manual_kind_switch_holds_and_lets_go(person: Person):
    p = person
    p.open_sheet("idea")
    p.type("helpdesk")
    assert p.kind() == "repo"
    p.tid("ap-kind-idea").click()
    assert p.kind() == "idea", "the pill forces another reading"
    assert "Reading it as an idea" in p.tid("ap-detect-line").inner_text()
    p.type("helpdesk ai")
    assert p.kind() == "idea"
    p.tid("ap-kind-repo").click()
    assert p.kind() == "repo"
    assert p.tid("ap-kind-repo").get_attribute("aria-checked") == "true"
    p.tid("ap-kind-folder").click()
    assert p.kind() == "folder"
    p.check("kind switch")


def test_enter_reads_shift_enter_adds_a_line_cmd_enter_creates(person: Person):
    p = person
    p.open_sheet("idea")
    p.type("Expense tracker for our team")
    p.box.press("Shift+Enter")
    p.box.press_sequentially("managers approve receipts")
    assert p.box.input_value() == "Expense tracker for our team\nmanagers approve receipts", "⇧↵ is a new line"
    p.type(IDEA)
    t = time.monotonic()
    p.box.press("Enter")
    assert p.box.input_value() == IDEA, "↵ never adds a line to an idea"
    # Instant feedback: the card says Helm is reading within a moment.
    p.p.wait_for_selector("[data-testid='ap-understood'][data-state='reading']", timeout=1500)
    state = p.understood(timeout_s=FIRST_READ_S + 5)
    took = time.monotonic() - t
    assert state == "ok", state
    assert took <= FIRST_READ_S, f"the first understanding took {took:.0f}s (> {FIRST_READ_S}s)"
    assert p.tid("ap-ub-heading").inner_text() == "Project brief"
    assert "understood" not in p.tid("ap-understood").inner_text().lower()
    # Exactly one model turn for one ↵ (the pause timer and ↵ never both ask).
    asks = [c for c in p.calls if c.endswith("/add-project/understand")]
    assert len(asks) == 1, asks
    p.check("brief")


# ── full screen, Ideas, Undo ────────────────────────────────────────────────────────────────

def test_full_screen_ideas_and_undo(person: Person):
    p = person
    p.open_sheet("idea")
    p.type("A small app")
    p.tid("ap-full").click()
    ed = p.tid("ap-full-editor")
    ed.wait_for()
    area = ed.locator("textarea")
    assert area.evaluate("el => el === document.activeElement")
    area.press("End")
    area.press_sequentially(" for booking rooms")
    p.p.keyboard.press("Escape")
    ed.wait_for(state="detached")
    assert p.sheet.is_visible(), "Esc closes the full-screen editor, not the sheet"
    assert p.box.input_value() == "A small app for booking rooms"

    p.tid("ap-ideas").click()
    p.tid("ap-ideas-list").wait_for()
    first = p.p.locator("[data-testid^='ap-idea-']").first
    title = first.inner_text().strip()
    first.click()
    assert title.split()[0].lower() in p.box.input_value().lower()
    assert p.tid("ap-ideas-list").count() == 0
    p.tid("ap-undo").click()
    assert p.box.input_value() == "A small app for booking rooms"
    assert p.tid("ap-undo").count() == 0
    p.check("ideas")


# ── repos and folders ───────────────────────────────────────────────────────────────────────

def test_repo_search_arrows_enter_and_add_account(person: Person):
    p = person
    p.open_sheet("idea")
    p.type("acme/")
    rows = p.p.locator("[data-testid='ap-results'] [role='option']")
    rows.first.wait_for()
    assert rows.count() >= 2
    assert rows.nth(0).get_attribute("aria-selected") == "true"
    p.box.press("ArrowDown")
    assert rows.nth(1).get_attribute("aria-selected") == "true"
    assert rows.nth(0).get_attribute("aria-selected") == "false"
    p.box.press("ArrowUp")
    p.box.press("ArrowDown")
    picked = rows.nth(1).inner_text().split("\n")[0].replace(" ", "")
    p.box.press("Enter")
    p.tid("ap-results").wait_for(state="detached")
    assert p.box.input_value().split("\n")[0] in picked or picked.startswith(p.box.input_value().split("\n")[0])
    card = p.tid("ap-found")
    p.p.wait_for_selector("[data-testid='ap-onboarding-will']", timeout=30_000)
    assert "Repo summary" in card.inner_text()
    assert "src" in card.inner_text(), "the main parts come from the repo's tree"
    assert re.search(r"\d+ test files", card.inner_text())
    # A note under the repo
    p.box.press("End")
    p.box.press("Shift+Enter")
    p.box.press_sequentially("start with the SLA bugs")
    assert "start with the SLA bugs" in p.tid("ap-note").inner_text()
    # + account opens Repo and models (global)
    href = p.tid("ap-add-account").get_attribute("href") or ""
    assert "area=setup" in href and "focus=repository" in href and "new_project" not in href
    p.check("repo picked")


def test_a_folder_on_a_host_reads_without_a_step_up(person: Person):
    p = person
    dev(mfa="lapsed")
    p.open_sheet("idea")
    p.type("/srv/apps/ledger-api")
    p.p.locator("[data-testid^='ap-folder-']").first.wait_for()
    p.box.press("Enter")
    p.p.wait_for_function("() => /42 files/.test(document.querySelector(\"[data-testid='ap-found']\")?.innerText || '')", timeout=30_000)
    text = p.tid("ap-found").inner_text()
    assert "Python" in text and "src" in text, text
    assert "cancelled" not in text and p.tid("step-up-sheet").count() == 0, "a read never asks for a step-up"
    p.check("folder")


def test_every_read_needs_no_step_up(person: Person):
    """With the second factor long lapsed, every read the sheet makes answers: preview, the
    registry, hosts, a repo's summary, a folder probe, Helm's brief."""
    p = person
    dev(mfa="lapsed")
    p.open_sheet("idea")
    p.idea_with_brief()
    p.type(ACME)
    p.box.press("Enter")
    p.p.wait_for_selector("[data-testid='ap-onboarding-will']", timeout=30_000)
    assert p.tid("step-up-sheet").count() == 0
    reads = [b for b in p.bad if b.startswith("401")]
    assert not reads, reads
    p.check("reads lapsed")


# ── the brief ───────────────────────────────────────────────────────────────────────────────

def test_the_brief_looks_right_fix_it_rewrite_and_questions(person: Person):
    p = person
    p.open_sheet("idea")
    p.idea_with_brief()
    # open questions: a quick answer toggles
    chips = p.p.locator("[data-testid^='ap-q0-']")

    def still_open() -> int:
        m = re.search(r"open · (\d+)", p.tid("ap-understood").inner_text().lower())
        return int(m.group(1)) if m else 0

    if chips.count():
        before = still_open()
        chips.first.click()
        assert chips.first.get_attribute("aria-pressed") == "true"
        assert still_open() == before - 1, "an answered question is no longer open"
        chips.first.click()
        assert chips.first.get_attribute("aria-pressed") == "false"
        assert still_open() == before

    # Fix it › Cancel keeps the brief as it was
    title = p.tid("ap-ub-row-title").inner_text()
    p.tid("ap-ub-fix").click()
    p.tid("ap-fix-title").fill("Something else entirely")
    p.tid("ap-ub-cancel").click()
    assert p.tid("ap-ub-row-title").inner_text() == title
    assert p.tid("ap-fix-title").count() == 0

    # Fix it: title, a must-have edited, moved up, one added, one deleted; Save brief
    p.tid("ap-ub-fix").click()
    assert "Save brief" in p.tid("ap-ub-fix").inner_text()
    p.tid("ap-fix-title").fill("Receipts: snap, approve, export")
    musts = p.tid("ap-fix-musts").locator("li input")
    n = musts.count()
    assert n >= 2
    musts.nth(1).fill("A manager approves each receipt")
    p.tid("ap-fix-musts").locator("li").nth(1).get_by_role("button", name="Move up").click()
    assert musts.nth(0).input_value() == "A manager approves each receipt"
    p.tid("ap-fix-add").click()
    musts.nth(n).fill("Finance downloads a CSV")
    p.tid("ap-fix-musts").locator("li").nth(n - 1).get_by_role("button", name="Delete").click()
    p.check("fix it")
    p.tid("ap-ub-fix").click()      # Done
    card = p.tid("ap-understood").inner_text()
    assert "Receipts: snap, approve, export" in card
    assert card.index("A manager approves each receipt") < card.index("Finance downloads a CSV")

    # Rewrite with a hint: Helm asks again, the edited lines are kept as written
    p.tid("ap-ub-hint").fill("shorter")
    p.tid("ap-ub-rewrite").click()
    p.p.wait_for_selector("[data-testid='ap-understood'] [data-testid='ap-ub'] .px-tag", timeout=3000)
    p.p.wait_for_function("() => !document.querySelector(\"[data-testid='ap-understood'] .px-dot[data-s='now']\")", timeout=90_000)
    assert "Receipts: snap, approve, export" in p.tid("ap-understood").inner_text(), "a rewrite keeps lines you edited"

    # Looks right: confirmed, folded to its title
    p.tid("ap-ub-ok").click()
    assert p.tid("ap-understood").get_attribute("data-ok") is not None
    assert "confirmed" in p.tid("ap-understood").inner_text().lower()
    p.check("brief confirmed")


# ── what BOSS will do: the eight popovers, each used at create ──────────────────────────────

def test_each_popover_changes_what_is_created(person: Person):
    p = person
    # This dev install has no mail server: the email channel picked under Tell is refused with
    # the checklist, and the person starts anyway (landed() ticks it).
    p.expect(r"^409 POST /v1/add-project \{\"error\":\"not_ready\"")
    p.open_sheet("idea")
    p.idea_with_brief()
    name = unique("Receipts")
    p.name(name)

    # Captain & crew
    p.tid("ap-box-captain").click()
    pop = p.tid("ap-pop-captain")
    pop.wait_for()
    other = pop.locator("[data-testid^='ap-captain-']:not([aria-selected='true'])").first
    captain = (other.get_attribute("data-testid") or "").removeprefix("ap-captain-")
    other.click()
    p.tid("ap-pop-captain-done").click()
    pop.wait_for(state="detached")

    # Stack
    p.tid("ap-box-stack").click()
    p.tid("ap-pop-stack").wait_for()
    stacks = p.p.locator("[data-testid^='ap-stack-']:not([data-testid='ap-stack-auto'])")
    stack_label = stacks.first.locator("b").inner_text()
    stacks.first.click()
    p.tid("ap-pop-stack-done").click()
    assert stack_label.split(",")[0] in p.tid("ap-box-stack").inner_text()

    # Connectors: drop every pick, so this create needs no egress or keys
    p.tid("ap-box-connectors").click()
    p.tid("ap-pop-connectors").wait_for()
    for on in p.p.locator("[data-testid^='ap-cx-'][aria-pressed='true']").all():
        on.click()
    p.tid("ap-pop-connectors-done").click()

    # Workflow
    p.tid("ap-box-workflow").click()
    p.tid("ap-pop-workflow").wait_for()
    wf = p.p.locator("[data-testid^='ap-wf-'][role='radio'][aria-checked='false']").first
    wf_key = (wf.get_attribute("data-testid") or "").removeprefix("ap-wf-")
    wf.click()
    p.tid("ap-pop-workflow-done").click()

    # Code: an existing repo
    p.tid("ap-box-code").click()
    p.tid("ap-pop-code").wait_for()
    repo_btn = p.p.locator("[data-testid^='ap-code-repo-']").first
    repo_name = repo_btn.inner_text().split("\n")[0].strip()
    repo_btn.click()
    p.tid("ap-pop-code-done").click()
    assert repo_name.split("/")[-1] in p.tid("ap-box-code").inner_text()

    # Staging: preview only
    p.tid("ap-box-staging").click()
    p.tid("ap-pop-staging").wait_for()
    p.tid("ap-staging-none").click()
    p.tid("ap-pop-staging-done").click()
    assert "Preview only" in p.tid("ap-box-staging").inner_text()

    # Tell
    p.tid("ap-chip-tell").click()
    p.tid("ap-tell-input").fill("finance@example.com")
    p.tid("ap-when-deployed").click()
    p.tid("ap-pop-tell-done").click()
    assert "finance@example.com" in p.tid("ap-chip-tell").inner_text()

    # More detail
    p.tid("ap-chip-more").click()
    p.tid("ap-more-audience").fill("Finance team on laptops")
    p.tid("ap-more-check").fill("A CSV opens in Excel")
    p.tid("ap-more-check").press("Enter")
    p.tid("ap-more-link").fill("https://example.com/policy")
    p.tid("ap-more-link").press("Enter")
    p.tid("ap-more-talks").fill("Xero API")
    p.tid("ap-more-talks").press("Enter")
    p.tid("ap-pop-more-done").click()
    assert "4" in p.tid("ap-chip-more").inner_text()

    # Esc closes only a popover
    p.tid("ap-box-stack").click()
    p.tid("ap-pop-stack").wait_for()
    p.p.keyboard.press("Escape")
    p.tid("ap-pop-stack").wait_for(state="detached")
    assert p.sheet.is_visible()
    p.check("popovers")

    p.create()
    pid = p.landed()
    seed = started(pid)["seed"]
    picks = seed["picks"]
    assert picks["captain"] == captain
    assert picks["stack"] and stack_label.split(",")[0] in picks["stack"]
    assert picks["workflow_key"] == wf_key
    assert picks["connectors"] == []
    assert picks["repos"] == [repo_name]
    assert picks["staging"] is None
    assert picks["notify"]["channel"] == "email:finance@example.com"
    assert "deployed" in picks["notify"]["when"]
    assert picks["audience"] == "Finance team on laptops"
    assert picks["done_when"] == ["A CSV opens in Excel"]
    assert picks["read_first"] == ["https://example.com/policy"]
    assert seed["brief"]["title"], "the brief Helm wrote is the seed"
    assert projects_named(name) in (None, 1)


# ── readiness, refusals, consent ────────────────────────────────────────────────────────────

def test_readiness_dots_say_what_each_is(person: Person):
    p = person
    p.open_sheet("idea")
    dots = p.p.locator("[data-testid^='ap-dot-']")
    dots.first.wait_for()
    assert dots.count() >= 3
    for i in range(dots.count()):
        d = dots.nth(i)
        assert d.get_attribute("title"), "each dot says what it checks"
        if d.get_attribute("data-ok") is None:
            assert d.get_attribute("href"), "a dot that is not ready links to where it is fixed"
    p.check("dots")


def test_a_readiness_refusal_then_start_anyway(person: Person):
    p = person
    p.expect(r"^409 POST /v1/add-project")
    dev(refuse=True)
    p.open_sheet("idea")
    p.idea_with_brief()
    name = unique("Refused")
    p.name(name)
    p.create()
    panel = p.tid("ap-readiness")
    panel.wait_for(timeout=60_000)
    btn = p.tid("ap-create")
    assert "Start anyway" in btn.inner_text()
    assert btn.is_disabled()
    assert (btn.get_attribute("title") or "").strip() and "tick" in (btn.get_attribute("title") or "").lower(), \
        "a locked Start anyway says why on hover"
    for box in panel.locator("input[type='checkbox']").all():
        box.check()
    assert btn.is_enabled()
    btn.click()
    pid = p.landed()
    assert started(pid)["seed"]["kind"] == "idea"
    assert projects_named(name) in (None, 1), "Start anyway reuses the project, never a second one"
    p.check("start anyway")


def test_a_name_clash_is_said_in_words(person: Person):
    p = person
    p.expect(r"^409 POST /v1/add-project")
    p.open_sheet("idea")
    p.type(IDEA)
    p.name(PROJECT.replace("-", " ").title().replace("Ai", "AI"))  # "Helpdesk AI Demo"
    status, body = api("GET", f"/v1/projects/{PROJECT}/add-project/started")
    p.create()
    err = p.tid("ap-error")
    err.wait_for(timeout=60_000)
    assert "exists" in err.inner_text() or "Choose another name" in err.inner_text(), err.inner_text()
    assert p.tid("ap-create").is_enabled(), "after a refusal the person can press again"
    p.check("clash")


def test_egress_consent_is_asked_inline(person: Person):
    p = person
    p.open_sheet("idea")
    p.idea_with_brief("A shop page: customers pay by card with Stripe and get an email receipt.")
    p.name(unique("Shop"))
    p.tid("ap-box-stack").click()
    p.p.locator("[data-testid^='ap-stack-']:not([data-testid='ap-stack-auto'])").first.click()
    p.tid("ap-pop-stack-done").click()
    p.tid("ap-box-connectors").click()
    with_egress = None
    for c in p.p.locator("[data-testid^='ap-cx-'][aria-pressed]").all():
        if c.get_attribute("aria-pressed") == "false":
            c.click()
        if p.tid("ap-cx-egress").count():
            with_egress = c
            break
    if with_egress is None:
        pytest.skip("no connector here talks to an outside host")
    p.tid("ap-pop-connectors-done").click()
    p.create(allow_egress=False)
    ask = p.tid("ap-egress")
    ask.wait_for()
    assert ask.locator(".npx-chip").count() >= 1
    btn = p.tid("ap-create")
    assert btn.is_disabled() and btn.get_attribute("title"), "Create waits for the tick and says so"
    p.tid("ap-egress-allow").check()
    assert btn.is_enabled()
    p.check("egress")


# ── step-up ─────────────────────────────────────────────────────────────────────────────────

def test_step_up_confirm_continues_and_cancel_says_so(person: Person):
    p = person
    p.expect(r"^401 POST /v1/add-project", r"^401 POST /v1/auth/step-up", r"status of 401")
    p.open_sheet("idea")
    p.idea_with_brief()
    dev(mfa="lapsed")
    # cancel: a clear sentence, nothing made, the button usable again
    name = unique("Stepped")
    p.name(name)
    p.create()
    sheet = p.tid("step-up-sheet")
    sheet.wait_for(timeout=30_000)
    sheet.get_by_role("button", name="Cancel").click()
    err = p.tid("ap-error")
    err.wait_for()
    said = err.inner_text()
    assert "Not created" in said and "confirm" in said.lower(), said
    assert p.tid("ap-create").is_enabled()
    assert projects_named(name) in (None, 0)
    # confirm: the create continues on its own
    p.create()
    sheet.wait_for(timeout=30_000)
    pw = sheet.locator("input[type='password']")
    if pw.count():
        pw.fill("pxdev-pass")
        sheet.get_by_role("button", name="Confirm").click()
    else:
        sheet.locator("input").first.fill("000000")
    pid = p.landed()
    assert started(pid)["seed"]["kind"] == "idea"
    assert projects_named(name) in (None, 1)


# ── no model, too slow ──────────────────────────────────────────────────────────────────────

def test_no_model_keeps_the_words_and_creates(person: Person):
    p = person
    dev(model="none")
    p.open_sheet("idea")
    p.type(IDEA)
    t = time.monotonic()
    p.box.press("Enter")
    assert p.understood(timeout_s=15) == "no_model"
    assert time.monotonic() - t < 10
    card = p.tid("ap-understood").inner_text()
    assert "your words" in card.lower()
    assert p.tid("ap-ub-retry").count() == 1
    p.name(unique("No model"))
    btn = p.tid("ap-create")
    assert btn.is_enabled(), "no model never blocks a start"
    btn.click()
    pid = p.landed()
    seed = started(pid)["seed"]
    assert seed["words"].startswith("Expense tracker")
    p.check("no model")


def test_a_slow_model_times_out_and_try_again_asks_again(person: Person):
    p = person
    dev(model="slow")
    p.open_sheet("idea")
    p.type(IDEA)
    p.box.press("Enter")
    assert p.understood(timeout_s=40) == "timeout"
    assert "longer than" in p.tid("ap-understood").inner_text()
    dev(model="cli")
    p.tid("ap-ub-retry").click()
    p.p.wait_for_selector("[data-testid='ap-understood'][data-state='reading']", timeout=3000)
    assert p.understood(timeout_s=60) == "ok"
    p.check("timeout")


# ── the other ways ──────────────────────────────────────────────────────────────────────────

def test_try_a_sample_starts_in_one_press(person: Person):
    p = person
    p.open_sheet("sample")
    btn = p.tid("ap-sample")
    btn.click()
    assert "Starting" in btn.inner_text() or p.p.url.count("project_id=") == 1
    pid = p.landed(timeout_s=120)
    assert pid != PROJECT
    p.check("sample")


def test_plan_with_an_agent_first(person: Person):
    p = person
    p.open_sheet("idea")
    if p.phone:
        p.tid("ap-more-ways").click()
        p.tid("ap-way-plan").click()
    else:
        p.tid("ap-plan-agent").click()
    p.sheet.wait_for(state="detached")
    p.tid("ag-talk").wait_for(timeout=20_000)    # the plan conversation with the PM opens
    p.check("plan")


def test_more_ways_blank_kg_and_presets(person: Person):
    p = person
    p.open_sheet("idea")
    p.tid("ap-more-ways").click()
    p.tid("ap-ways").wait_for()
    p.tid("ap-way-blank").click()
    p.p.wait_for_selector("[data-testid='new-project-sheet'], [data-testid='np-name'], form", timeout=10_000)
    p.go()
    p.open_sheet("idea")
    p.tid("ap-more-ways").click()
    p.tid("ap-way-presets").click()
    p.tid("ap-presets").wait_for()
    assert p.p.locator("[data-testid^='ap-preset-']").count() >= 3
    p.check("presets")


# ── closing, double presses ─────────────────────────────────────────────────────────────────

def test_esc_with_unsaved_words_keeps_them(person: Person):
    p = person
    p.open_sheet("idea")
    p.type("A rota planner for the night shift")
    p.p.keyboard.press("Escape")
    p.sheet.wait_for(state="detached")
    p.open_sheet("idea")
    assert p.box.input_value() == "A rota planner for the night shift", "the draft comes back"
    assert p.tid("ap-draft").is_visible()
    p.tid("ap-draft-clear").click()
    assert p.box.input_value() == ""
    p.tid("ap-close").click()
    p.open_sheet("idea")
    assert p.box.input_value() == ""
    p.check("draft")


def test_double_pressing_create_makes_one_project(person: Person):
    p = person
    p.open_sheet("idea")
    p.idea_with_brief()
    name = unique("Twice")
    p.name(name)
    btn = p.tid("ap-create")
    btn.dblclick()
    if p.tid("ap-egress-allow").count():       # Helm's connectors talk out: allow, then again
        p.tid("ap-egress-allow").check()
        btn.dblclick()
    assert "Creating" in btn.inner_text() or "Created" in btn.inner_text(), "the press shows at once"
    p.landed()
    creates = [c for c in p.calls if c.endswith("/v1/add-project")]
    # the press, and its retry after consent (or Start anyway) — never the second click
    assert len(creates) == 1 + p.consented + p.started_anyway, creates
    assert projects_named(name) in (None, 1)


# ── where Create and start lands ────────────────────────────────────────────────────────────

def test_create_an_idea_lands_on_planning(person: Person):
    p = person
    p.open_sheet("idea")
    p.idea_with_brief()
    name = unique("Expenses")
    p.name(name)
    p.create()
    pid = p.landed()
    p.p.wait_for_selector("[data-testid='px-started']", timeout=60_000)
    assert "Idea to PR" in p.tid("px-started").inner_text()
    what = p.tid("px-started-what").inner_text()
    assert "Workflow" in what
    seed = started(pid)["seed"]
    assert seed["kind"] == "idea" and seed["brief"]["title"]
    assert projects_named(name) in (None, 1)


def test_create_a_repo_lands_on_onboarding(person: Person):
    p = person
    p.open_sheet("idea")
    p.type(ACME)
    p.box.press("Enter")
    p.p.wait_for_selector("[data-testid='ap-onboarding-will']", timeout=30_000)
    name = unique("Ledger")
    p.name(name)
    p.tid("ap-chip-rules").click()
    assert p.tid("ap-chip-rules").get_attribute("aria-pressed") == "true"
    p.create()
    pid = p.landed()
    assert "tab=onboarding" in p.p.url
    p.p.wait_for_selector("[data-testid='px-ob-stages']", timeout=60_000)
    seed = started(pid)["seed"]
    assert seed["kind"] == "repo" and seed["picks"]["repos"] == [ACME] and seed["picks"]["map_rules"] is True
    assert projects_named(name) in (None, 1)
