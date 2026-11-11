"""GIT, FIRST-CLASS IN THE CONSOLE, CLICK BY CLICK (BOSSAPP-992: 993, 995, 996).

The operator: "introduce the git logs in the ui … introduce the overview tab … we should also
provide link to jump to specific code piece … provide few important git commands in the ui, to
pull, resolve, showing history etc.. we dont have to go to CLI to execute these things."

A person at 1440 and 390 (conftest), the harness's guard after every test. What this walks:
the change page's Overview tab (the description whole, Active and Resolved comments, a panel
note's file:line landing on the exact diff row), a pasted ?file=&line= link, the header's sync
line (ahead/behind, Sync as a recorded decision, the conflicting files behind the expander), and
the project's History (author · when · change · sha, the header's History chip). The work-stream
and build-card change links are unit-tested (every-card-names-its-change.test.tsx).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from conftest import CONSOLE, Person, bare_of, git, seam
from test_local_change_review import _comment_on, _line, _open_change, _tab


def _in_view(p: Person, locator) -> bool:
    box = locator.bounding_box()
    assert box, "the row is not rendered"
    height = p.p.evaluate("() => window.innerHeight")
    return 0 <= box["y"] <= height - 10


def _commit_main(pid: str, files: dict[str, str], message: str) -> None:
    """A commit straight to the project's default branch, as a person at a CLI would make it."""
    bare = bare_of(pid)
    with tempfile.TemporaryDirectory(prefix="boss-e2e-sync-") as tmp:
        w = Path(tmp) / "w"
        git("clone", "--quiet", str(bare), str(w))
        for path, content in files.items():
            target = w / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        git("add", "-A", cwd=w)
        git("commit", "-qm", message, cwd=w)
        git("push", "-q", "origin", "HEAD:main", cwd=w)


def test_the_overview_tab_reads_the_change_and_lands_on_the_code(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Overview {p.width}"})
    n = staged["number"]
    _open_change(p, pid, n)

    # A person's line comment, so Active has a thread to work on.
    _tab(p, "files")
    _comment_on(p, _line(p, "app.js", 2), "Name the list for what it holds.")

    # The Overview tab: the description whole (never the Conversation tab's 3-line clamp),
    # Active first, and the review panel's note with its file:line.
    _tab(p, "overview")
    p.tid("gr-ov-description").wait_for()
    assert not p.p.locator("[data-testid='gr-ov-description'] [data-clamp3]").count()
    active = p.tid("gr-ov-active")
    # The page re-reads after a comment posts; the new thread lands in Active.
    p.p.wait_for_function("() => (document.querySelector(\"[data-testid='gr-ov-active']\")?.textContent || '')"
                          ".includes('Name the list')", timeout=30_000)
    note = p.tid("gr-ov-note")
    assert "Validate the amount" in note.inner_text()

    # The thread's file:line opens the diff at that thread.
    active.locator("[data-testid='gr-comment-anchor']").first.click()
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='change-set-tab-files']\")?.getAttribute('aria-selected') === 'true'")
    thread = p.p.locator("[data-testid='lr-thread']").filter(has_text="Name the list").first
    thread.wait_for()
    assert _in_view(p, thread)

    # The panel note's file:line lands on the exact row (app.js:3), and the URL can say so.
    _tab(p, "overview")
    p.tid("gr-ov-note-loc").click()
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='change-set-tab-files']\")?.getAttribute('aria-selected') === 'true'")
    row = p.p.locator("#gr-f-app_js-R3")
    row.wait_for()
    p.p.wait_for_function("() => { const r = document.getElementById('gr-f-app_js-R3')?.getBoundingClientRect(); "
                          "return !!r && r.top >= 0 && r.top <= window.innerHeight; }")
    assert "file=app.js" in p.p.url and "line=3" in p.p.url
    p.check("overview tab")


def test_a_pasted_file_line_link_lands_on_the_row(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Deep link {p.width}"})
    p.p.goto(f"{CONSOLE}/changes/{pid}/{staged['number']}?file=app.js&line=3",
             wait_until="domcontentloaded", timeout=180_000)
    p.tid("change-set-title").wait_for(timeout=120_000)
    assert p.tid("change-set-tab-files").get_attribute("aria-selected") == "true"
    p.p.wait_for_function("() => { const r = document.getElementById('gr-f-app_js-R3')?.getBoundingClientRect(); "
                          "return !!r && r.top >= 0 && r.top <= window.innerHeight; }", timeout=30_000)
    p.check("deep link")


def test_sync_conflicts_and_history_without_a_cli(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Sync {p.width}"})
    n = staged["number"]
    _open_change(p, pid, n)
    p.tid("gr-sync-stand").wait_for(timeout=60_000)
    assert "up to date" in p.tid("gr-sync-stand").inner_text()

    # The base moves on: the line says behind, and Sync (a recorded decision) pulls it in.
    _commit_main(pid, {f"docs/note-{p.width}.md": "the base moved\n"}, "base moves on")
    p.p.reload(wait_until="domcontentloaded")
    p.tid("gr-sync-run").wait_for(timeout=60_000)
    assert "1 behind" in p.tid("gr-sync-stand").inner_text()
    p.tid("gr-sync-run").click()
    p.p.wait_for_function("() => (document.querySelector(\"[data-testid='gr-sync-stand']\")?.textContent || '')"
                          ".includes('up to date')", timeout=60_000)

    # A real conflict: the base rewrites a file this branch adds. The pill names the file, whole
    # in view, and says the honest next step.
    _commit_main(pid, {"app.js": f"// a different app.js on main ({p.width})\n"}, "base rewrites app.js")
    p.p.reload(wait_until="domcontentloaded")
    p.tid("gr-conflicts").wait_for(timeout=60_000)
    assert "1 conflicting file" in p.tid("gr-conflicts").inner_text()
    p.tid("gr-conflicts").click()
    pop = p.tid("gr-conflicts-list")
    pop.wait_for()
    assert "app.js" in pop.inner_text() and "Request changes" in pop.inner_text()
    p.p.keyboard.press("Escape")
    pop.wait_for(state="detached")

    # History, one press from here: the project's log with author · when · its change · short sha.
    with p.p.expect_navigation(wait_until="domcontentloaded"):
        p.tid("gr-history-link").click()
    p.tid("px-history").wait_for(timeout=120_000)
    rows = p.p.locator("[data-testid='px-history-commit']")
    rows.first.wait_for(timeout=60_000)
    assert rows.count() >= 2
    first = rows.first
    assert first.locator("[data-testid='px-history-author']").inner_text()
    if p.width >= 700:
        assert first.locator("[data-testid='px-history-when']").inner_text()
    assert first.locator("code").inner_text()
    # The header names it too: the History chip opens Repo & knowledge from anywhere on the page.
    chip = p.tid("px-head-history")
    assert chip.count() == 1
    p.check("sync and history")
