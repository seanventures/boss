"""THE CHANGE PAGE AS A GIT-STYLE REVIEW, CLICK BY CLICK (BOSSAPP-954; wireframe
docs/product/wireframes/agentic-factory/6-git-review.html).

A person at 1440 and 390 (conftest), the harness's guard after each test. Every new control has a
step: tabs that switch the view (no anchor scroll) and keep the URL, Commits (All commits, one
commit by its own link, Previous/Next, compare two, the URL round trip and Back), one count
source, the folder tree (expand/collapse, go to file, breadcrumb, resize), comments with their code
(inline, a snippet click scrolled to its line, outdated), the PR agent (chat, Fix N open
comments), dockable panes ([ ] \\, rail, drawer, pin, remembered after reload), Full screen, and
the project's default-branch history. A GitHub PR runs the same page; this suite has no live
GitHub, so its API side is covered by apps/api/tests/test_change_review.py (faked at the adapter).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from conftest import CONSOLE, Person, api, seam
from test_local_change_review import _comment_on, _line, _open_change, _tab, _thread


def _url(p: Person) -> str:
    return p.p.evaluate("() => location.pathname + location.search")


def _scroll(p: Person) -> float:
    return float(p.p.evaluate("() => window.scrollY"))


def test_tabs_switch_the_view_and_commits_are_links(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Commits {p.width}"})
    n = staged["number"]
    _open_change(p, pid, n)

    # Every tab switches the main view in place, with no scroll to an anchor, and the URL keeps it.
    for name in ("conversation", "commits", "files", "code", "checks"):
        before = _scroll(p)
        _tab(p, name)
        assert _scroll(p) <= before + 1, f"{name} scrolled the page"
        assert "#" not in p.p.url
        if name != "files":
            assert f"tab={name}" in _url(p) or name == "commits"
    _tab(p, "files")
    assert _url(p) == f"/changes/{pid}/{n}"

    # ONE COUNT SOURCE: the tab badge, the tree's Changed, and the summary agree.
    badge = p.tid("change-set-tab-files").locator("b").inner_text()
    if p.width >= 700:
        assert f"Changed {badge}" in p.tid("gr-mode-changed").inner_text()
    _tab(p, "conversation")
    assert p.tid("gr-what-changed").inner_text().startswith(f"{badge} file")

    # Commits: All commits first and the default, the whole change in the tree and the diff.
    _tab(p, "commits")
    assert p.tid("gr-commit-all").get_attribute("data-on") is not None
    p.tid("gr-commit-all-view").wait_for()
    assert p.p.locator("[data-testid='gr-commit-all-view'] [data-testid^='change-set-file-']").count() == int(badge)
    rows = p.p.locator("[data-testid='gr-commit']")
    assert rows.count() >= 2
    assert re.search(r"\+\d+", rows.first.locator("[data-testid='gr-commit-stat']").inner_text())
    # A commit is a real link: /changes/<project>/<n>/commits/<sha>; a click opens its own diff.
    link = rows.nth(1).locator("[data-testid='gr-commit-open']")
    href = link.get_attribute("href") or ""
    assert re.fullmatch(rf"/changes/{pid}/{n}/commits/[0-9a-f]{{40}}", href), href
    link.click()
    p.tid("gr-commit-view").wait_for()
    assert _url(p) == href
    sha = href.rsplit("/", 1)[-1]
    assert p.tid("gr-commit-sha").inner_text() == sha[:7]
    assert p.tid("gr-commit-message").inner_text()
    # Previous / Next walk the list; Back returns; a reload of the URL lands on the same commit.
    p.tid("gr-commit-next").click()
    p.p.wait_for_function(f"() => location.pathname !== '{href}'")
    newer = _url(p)
    p.p.go_back()
    p.p.wait_for_function(f"() => location.pathname === '{href}'")
    p.tid("gr-commit-view").wait_for()
    assert p.tid("gr-commit-sha").inner_text() == sha[:7]
    p.p.goto(f"{CONSOLE}{newer}", wait_until="domcontentloaded")
    p.tid("gr-commit-view").wait_for(timeout=120_000)
    assert p.tid("gr-commit-prev").get_attribute("href") == href
    # Back to All commits; tick two commits and compare them in Files changed.
    p.tid("gr-commit-all-open").click()
    p.tid("gr-commit-all-view").wait_for()
    ticks = p.p.locator("[data-testid='gr-commit-tick']")
    ticks.nth(0).check()
    ticks.nth(1).check()
    assert "Compare" in p.tid("gr-compare-bar").inner_text()
    p.tid("gr-compare").click()
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='change-set-tab-files']\")?.getAttribute('aria-selected') === 'true'")
    assert "latest" in p.tid("gr-range").inner_text() and "…" in p.tid("gr-range").inner_text()
    # The badge follows the range: it is the diff's own count.
    shown = p.p.locator("[data-testid^='change-set-file-']").count()
    assert p.tid("change-set-tab-files").locator("b").inner_text() == str(shown)
    p.tid("gr-range").click()
    p.tid("gr-range-reset").click()
    assert p.tid("change-set-tab-files").locator("b").inner_text() == badge
    p.check("tabs and commits")


def test_the_tree_go_to_file_breadcrumb_and_resize(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Tree {p.width}"})
    _open_change(p, pid, staged["number"])
    _tab(p, "code")
    p.tid("lr-code").wait_for()
    if p.width < 700:
        p.tid("gr-tree-toggle").click()                                 # a phone: the tree is a sheet
        p.tid("gr-drawer-tree").wait_for()
    tree = p.p.locator("[data-testid='gr-tree']").first
    # Names only, never full paths; folders collapse and expand.
    for name in tree.locator("[data-testid='gr-tree-name']").all_inner_texts():
        assert "/" not in name
    dirs = tree.locator("[data-testid='gr-tree-dir']")
    if dirs.count():
        d = dirs.first
        was = d.locator("xpath=..").get_attribute("aria-expanded")
        d.click()
        assert d.locator("xpath=..").get_attribute("aria-expanded") != was
        d.click()
        assert d.locator("xpath=..").get_attribute("aria-expanded") == was
    # Go to file: fuzzy on the path, ↓ and ↵.
    p.tid("lr-goto").fill("app")
    p.tid("gr-goto-results").wait_for()
    p.tid("lr-goto").press("ArrowDown")
    p.tid("lr-goto").press("ArrowUp")
    p.tid("lr-goto").press("Enter")
    p.p.wait_for_function("() => (document.querySelector(\"[data-testid='lr-file-path']\")?.dataset.path || '').endsWith('app.js')")
    # The breadcrumb: its file is the name; a folder segment opens that folder in the tree.
    assert p.tid("lr-file-path-name").inner_text().endswith("app.js")
    crumbs = p.p.locator("[data-testid='gr-crumb']")
    if crumbs.count():
        crumbs.first.click()
        folder = crumbs.first.inner_text()
        assert tree.locator(f"[data-testid='gr-tree-dir'][data-path^='{folder}']").first.locator("xpath=..").get_attribute("aria-expanded") == "true"
    if p.width >= 1100:
        # Resize by keyboard on the handle (a drag does the same), remembered after a reload.
        handle = p.tid("gr-resize-tree")
        w0 = int(handle.get_attribute("aria-valuenow") or "0")
        handle.focus()
        for _ in range(3):
            p.p.keyboard.press("ArrowRight")
        w1 = int(handle.get_attribute("aria-valuenow") or "0")
        assert w1 == w0 + 48
        box = handle.bounding_box()
        assert box
        p.p.mouse.move(box["x"] + 4, box["y"] + 40)
        p.p.mouse.down()
        p.p.mouse.move(box["x"] - 36, box["y"] + 40)
        p.p.mouse.up()
        assert int(handle.get_attribute("aria-valuenow") or "0") < w1
        w2 = handle.get_attribute("aria-valuenow")
        p.p.reload(wait_until="domcontentloaded")
        p.tid("lr-code").wait_for(timeout=120_000)
        assert p.tid("gr-resize-tree").get_attribute("aria-valuenow") == w2
    p.check("the tree")


def test_comments_sit_on_their_code_and_go_outdated(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Anchors {p.width}"})
    n = staged["number"]
    _open_change(p, pid, n)
    # Inline: under the line it is on, in Files changed and in All commits.
    _comment_on(p, _line(p, "app.js", 3), "This line needs a guard.")
    t = _thread(p, "This line needs a guard.")
    t.wait_for()
    assert t.locator("xpath=ancestor::tr[contains(@class,'cs-inline')]").count() == 1
    _tab(p, "commits")
    p.tid("gr-commit-all-view").wait_for()
    assert _thread(p, "This line needs a guard.").is_visible()
    # Out of the diff: the Conversation shows it with its code; a click opens Files changed there.
    _tab(p, "conversation")
    code = p.p.locator("[data-testid='gr-comment-code']").filter(has_text="app.js:3").first
    code.wait_for()
    assert code.locator("[data-testid='gr-snippet'] tr[data-focus]").count() == 1
    code.locator("[data-testid='gr-comment-anchor']").click()
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='change-set-tab-files']\")?.getAttribute('aria-selected') === 'true'")
    p.p.wait_for_function("""() => { const el = [...document.querySelectorAll("[data-testid='lr-thread']")]
        .find((x) => x.textContent.includes('This line needs a guard.')); if (!el) return false;
        const r = el.getBoundingClientRect(); return r.top >= 0 && r.top < window.innerHeight; }""", timeout=15_000)
    # A general comment says "on the whole change" (through the API: the page's composer is per line).
    status, _ = api("POST", f"/v1/projects/{pid}/local-changes/{n}/comments", {"body": "Overall fine."})
    assert status == 200
    p.p.reload(wait_until="domcontentloaded")
    p.tid("change-set-title").wait_for(timeout=120_000)
    _tab(p, "conversation")
    assert p.p.locator("[data-testid='gr-comment-anchor']", has_text="on the whole change").count() >= 1
    # Outdated: a later commit changes line 3 (plain git through the console's remote).
    tmp = Path(tempfile.mkdtemp(prefix="gr-e2e-"))
    askpass = tmp / "askpass.sh"
    askpass.write_text("#!/bin/sh\nprintf 'boss_pat_dev'\n")
    askpass.chmod(0o755)
    env = {**os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "HOME": str(tmp),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Param", "GIT_AUTHOR_EMAIL": "p@x.test",
           "GIT_COMMITTER_NAME": "Param", "GIT_COMMITTER_EMAIL": "p@x.test"}
    w = tmp / "w"
    subprocess.run(["git", "clone", "-q", "--branch", staged["branch"], f"{CONSOLE}/git/{pid}.git", str(w)], env=env, check=True, timeout=180)
    lines = (w / "app.js").read_text().split("\n")
    lines[2] = lines[2] + " // changed"
    (w / "app.js").write_text("\n".join(lines))
    subprocess.run(["git", "commit", "-qam", "change line 3"], cwd=w, env=env, check=True, timeout=60)
    subprocess.run(["git", "push", "-q", "origin", staged["branch"]], cwd=w, env=env, check=True, timeout=180)
    p.p.reload(wait_until="domcontentloaded")
    p.tid("change-set-title").wait_for(timeout=120_000)
    _tab(p, "conversation")
    old = p.p.locator("[data-testid='gr-comment-code']").filter(has_text="app.js:3").first
    old.wait_for()
    assert old.locator("[data-testid='gr-outdated']").is_visible()
    assert "// changed" not in old.locator("[data-testid='gr-snippet']").inner_text()      # the code as it was
    made_on = old.locator("[data-testid='gr-made-on']")
    assert re.search(rf"/changes/{pid}/{n}/commits/[0-9a-f]{{40}}", made_on.get_attribute("href") or "")
    made_on.click()
    p.tid("gr-commit-view").wait_for()
    assert _thread(p, "This line needs a guard.").is_visible()          # on the commit it was made on
    p.check("comments and their code")


def test_the_pr_agent_and_dockable_panes(person: Person, review_project: str):
    p = person
    pid = review_project
    staged = seam("stage", {"project_id": pid, "title": f"Agent {p.width}"})
    n = staged["number"]
    _open_change(p, pid, n)
    if p.width < 1100:
        p.tid("gr-agent-toggle").click()                                 # tablet/phone: a drawer/sheet
        p.tid("gr-drawer-agent").wait_for()
    agent = p.tid("gr-agent")
    assert "builder of this change" in agent.inner_text()
    # The chat: kept for the next fix run while the task is idle.
    p.tid("gr-chat-input").fill("Why does the total round down?")
    p.tid("gr-chat-send").click()
    p.p.locator("[data-testid='gr-msg']", has_text="Why does the total round down?").wait_for()
    assert "next fix run" in p.tid("gr-chat").inner_text() or "running task" in p.tid("gr-chat").inner_text()
    if p.width < 1100:
        p.tid("gr-drawer-close-agent").click()
        p.check("the agent on a small screen")
        return
    # Fix N open comments: disabled with none open.
    assert "Fix 0 open comments" in p.tid("gr-fix-open").inner_text()
    # Docking: ] collapses the agent to a rail, its icon opens a drawer over the code (no reflow),
    # the pin docks it again; [ the tree; \\ focus mode collapses both and restores them.
    _tab(p, "files")
    p.p.locator("body").click(position={"x": 2, "y": 2})
    diff_box = p.p.locator(".gr-main").first.bounding_box()
    p.p.keyboard.press("]")
    p.tid("gr-rail-agent").wait_for()
    p.tid("gr-rail-agent").click()
    p.tid("gr-drawer-agent").wait_for()
    after = p.p.locator(".gr-main").first.bounding_box()
    p.tid("gr-drawer-pin-agent").click()
    p.tid("gr-collapse-agent").wait_for()
    assert diff_box and after and after["width"] >= diff_box["width"]
    p.tid("gr-collapse-tree").click()
    p.tid("gr-rail-tree").wait_for()
    p.tid("gr-pin-tree").click()
    p.tid("gr-collapse-tree").wait_for()
    p.p.keyboard.press("[")
    p.tid("gr-rail-tree").wait_for()
    p.p.keyboard.press("[")
    p.tid("gr-collapse-tree").wait_for()
    p.p.keyboard.press("\\")
    assert p.tid("gr-layout").get_attribute("data-focus") is not None
    p.p.keyboard.press("\\")
    assert p.tid("gr-layout").get_attribute("data-focus") is None
    p.tid("gr-collapse-agent").wait_for()
    # Remembered after a reload: the agent on a rail stays on a rail.
    p.tid("gr-collapse-agent").click()
    p.p.reload(wait_until="domcontentloaded")
    p.tid("change-set-title").wait_for(timeout=120_000)
    p.tid("gr-rail-agent").wait_for()
    p.tid("gr-pin-agent").click()
    # Full screen: focus mode (and the browser's fullscreen where it is allowed); Esc leaves it.
    p.tid("gr-fullscreen").click()
    p.p.wait_for_function("() => !!document.querySelector(\"[data-testid='gr-layout'][data-focus]\")")
    p.p.keyboard.press("\\")
    p.p.wait_for_function("() => !document.querySelector(\"[data-testid='gr-layout'][data-focus]\")")
    # Split and unified.
    p.tid("gr-split").click()
    assert p.p.locator("[data-layout='split']").count() >= 1
    p.tid("gr-unified").click()
    # Viewed, per person: folds the file and counts.
    first = p.p.locator("[data-testid='gr-viewed']").first
    first.check()
    p.p.wait_for_function("() => /^1 of/.test(document.querySelector(\"[data-testid='gr-viewed-count']\")?.textContent || '')")
    first.uncheck()
    p.check("the agent and the panes")


def test_project_history_and_a_merged_changes_commits(person: Person, review_project: str):
    p = person
    pid = review_project
    status, hist = api("GET", f"/v1/projects/{pid}/history")
    assert status == 200 and hist["branch"] in ("main", "master")
    p.p.goto(f"{CONSOLE}/?project_id={pid}&area=project&tab=knowledge", wait_until="domcontentloaded", timeout=180_000)
    card = p.tid("px-history")
    card.wait_for(timeout=120_000)
    link = card.locator("[data-testid='px-history-link']").first
    href = link.get_attribute("href") or ""
    assert re.fullmatch(rf"/projects/{pid}/commits/[0-9a-f]{{40}}", href), href
    link.click()
    p.tid("project-commit").wait_for(timeout=120_000)
    p.tid("gr-commit-sha").wait_for()
    assert href.rsplit("/", 1)[-1].startswith(p.tid("gr-commit-sha").inner_text())
    merged = [c for c in hist["items"] if c.get("change")]
    if merged:
        p.p.goto(f"{CONSOLE}/projects/{pid}/commits/{merged[0]['sha']}", wait_until="domcontentloaded")
        p.tid("gr-merged-change").wait_for(timeout=120_000)
        p.tid("gr-merged-change").click()
        p.tid("change-set-title").wait_for(timeout=120_000)
        _tab(p, "commits")
        p.tid("gr-commit-all-view").wait_for()                          # still readable after the merge
    p.check("project history")
