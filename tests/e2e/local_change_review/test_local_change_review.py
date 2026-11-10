"""REVIEW A LOCAL CHANGE LIKE A PULL REQUEST, AND WORK ON IT WITH PLAIN GIT (BOSSAPP-948).

Each test is a person at 1440 and at 390 (conftest), ending with the harness's guard. What runs
through a dev seam is said where it happens: a build's change set (/__dev/lf/stage) and the fix
round's builder answering (/__dev/lf/answer) — the pipeline's own functions, not stand-ins.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from conftest import CONSOLE, Person, api, bare_of, git, seam


def _open_change(p: Person, pid: str, number: int) -> None:
    p.p.goto(f"{CONSOLE}/changes/{pid}/{number}", wait_until="domcontentloaded", timeout=180_000)
    p.tid("change-set-title").wait_for(timeout=120_000)


def _line(p: Person, path: str, side_no: int):
    """The diff row of `path` at new line `side_no` (the file's card, then its row)."""
    card = p.p.locator("[data-testid^='change-set-file-']").filter(has_text=path).first
    return card.locator("tr.cs-ln").filter(has=p.p.locator(f"td.cs-no:nth-child(2):text-is('{side_no}')")).first


def _comment_on(p: Person, row, body: str, severity: str = "Must fix") -> None:
    row.hover()
    row.locator("[data-testid='lr-plus']").click()
    composer = p.tid("lr-composer")
    composer.wait_for()
    composer.locator("textarea").fill(body)
    composer.get_by_role("radio", name=severity).click()
    composer.locator("[data-testid='lr-send']").click()
    composer.wait_for(state="detached", timeout=30_000)


def _tab(p: Person, name: str) -> None:
    """A tab switches the main view in place: no anchor scroll, the URL says which tab."""
    p.tid(f"change-set-tab-{name}").click()
    p.p.wait_for_function(f"() => document.querySelector(\"[data-testid='change-set-tab-{name}']\")?.getAttribute('aria-selected') === 'true'")


def _thread(p: Person, text: str):
    return p.p.locator("[data-testid='lr-thread']").filter(has_text=text).first


def test_review_a_local_change_comment_request_changes_and_resolve(person: Person, review_project: str):
    p = person
    pid = review_project
    # A build's change set on this machine, with its review decision and a panel note (seam).
    staged = seam("stage", {"project_id": pid, "title": f"Expense list {p.width}"})
    n = staged["number"]
    _open_change(p, pid, n)

    # (a) Where it lives: in the Clone popover (two-line header, no prose).
    p.tid("gr-clone").click()
    p.tid("gr-clone-pop").wait_for()
    where = p.tid("lr-where")
    assert pid in p.tid("lr-storage").inner_text() and p.tid("lr-storage").inner_text().endswith(".git")
    assert p.tid("lr-remote").inner_text().endswith(f"/git/{pid}.git")
    assert staged["branch"] in where.inner_text()
    p.tid("lr-get-copy").click()
    sheet = p.tid("lr-copy-sheet")
    sheet.wait_for()
    command = p.tid("lr-clone-command").inner_text()
    assert command == f"boss clone {pid} ~/code/{pid} --branch {staged['branch']}"
    p.tid("lr-copy-command").click()
    assert "Copied" in p.tid("lr-copy-command").inner_text()
    p.p.keyboard.press("Escape")
    sheet.wait_for(state="detached")
    p.p.keyboard.press("Escape")
    p.tid("gr-clone-pop").wait_for(state="detached")

    # (c) A comment on a line of the diff: it holds Merge, and says so on the button.
    _comment_on(p, _line(p, "app.js", 3), "Check the amount is a number before adding it.")
    t1 = _thread(p, "Check the amount is a number")
    t1.wait_for()
    assert t1.locator("[data-testid='lr-thread-state']").inner_text() == "Open"
    p.tid("change-set-open-comments").wait_for()
    assert "1 open" in p.tid("change-set-open-comments").inner_text()
    merge = p.tid("change-set-merge")
    assert merge.is_disabled() and "1 open comment" in merge.inner_text()
    _tab(p, "checks")
    assert "no reply yet" in p.tid("change-set-held").inner_text()

    # (b) The Code tab: a folder tree with the change's status and comment counts, the whole file.
    _tab(p, "code")
    p.tid("lr-code").wait_for()
    item = p.p.locator("[data-testid='lr-tree-item'][data-path='app.js']")
    item.wait_for()
    assert "A" in item.inner_text() and "1" in item.inner_text()
    item.click()
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='lr-file-path']\")?.dataset.path === 'app.js'")
    body = p.p.locator(".lr-filebody")
    body.wait_for()
    assert "export function add" in body.inner_text()
    assert _thread(p, "Check the amount is a number").is_visible()          # the thread, on its line
    # A second comment, from the whole file, on line 5.
    row5 = body.locator("tr.cs-ln").nth(4)
    row5.hover()
    row5.locator("[data-testid='lr-plus']").click()
    p.tid("lr-composer").locator("textarea").fill("Return the new item, not the count.")
    p.tid("lr-composer").get_by_role("radio", name="Should fix").click()
    p.tid("lr-send").click()
    p.tid("lr-composer").wait_for(state="detached")
    _thread(p, "Return the new item").wait_for()
    # The base: README.md as it is on main (Go to file, fuzzy, then Enter).
    p.tid("lr-goto").fill("readme")
    p.tid("gr-goto-results").wait_for()
    p.tid("lr-goto").press("Enter")
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='lr-file-path']\")?.dataset.path === 'README.md'")
    p.tid("lr-ref-base").click()
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='lr-ref-base']\")?.getAttribute('aria-checked') === 'true'")
    body.wait_for()
    assert "#" in body.inner_text()

    # The agent pane lists both, people first; one is resolved here by the person.
    _tab(p, "files")
    rail = p.tid("gr-agent-comments")
    assert "2 open" in rail.inner_text()
    second = _thread(p, "Return the new item")
    if not second.is_visible():
        _tab(p, "conversation")
    second = _thread(p, "Return the new item")
    second.wait_for()
    second.locator("[data-testid='lr-resolve']").click()
    p.p.wait_for_function("() => /1 open/.test(document.querySelector(\"[data-testid='gr-agent-comments']\")?.textContent || '')")
    assert _thread(p, "Return the new item").locator("[data-testid='lr-thread-state']").inner_text() == "Resolved"

    # (d) Request changes: the fix run on this branch, the person's open comment its required input.
    # Its build acts for the person: the first one asks for their consent (409, then the sheet).
    p.expect(r"^409 POST /v1/projects/[^/]+/local-changes/\d+/request-changes \{\"error\":\"consent_required\"")
    p.tid("change-set-request-changes").click()
    p.tid("lr-rc-note").fill("Please also refuse negative amounts.")
    p.tid("lr-rc-send").click()
    allow = p.p.locator("[data-testid='consent-durations'] button, [data-testid='consent-mr-allow']")
    try:
        allow.first.wait_for(timeout=4000)
        allow.first.click()
        p.consented += 1
    except Exception:  # noqa: BLE001 — a grant already covers the builder
        pass
    _tab(p, "checks")
    p.tid("lr-agent-actions").wait_for(timeout=60_000)
    actions = p.tid("lr-agent-actions")
    assert "Agent actions" in actions.inner_text() and "Check the amount is a number" in actions.inner_text()
    assert "Return the new item" not in actions.inner_text()            # resolved: not required
    status, view = api("GET", f"/v1/projects/{pid}/local-changes/{n}")
    assert status == 200 and view["decision"]["status"] == "revision_requested", view.get("decision")

    # The builder answers (seam: the pipeline's own functions); the person sees it and resolves.
    answered = seam("answer", {"project_id": pid, "number": n})
    assert answered["answered"] == 1 and answered["replied"] == 1
    p.p.reload(wait_until="domcontentloaded")
    p.tid("change-set-title").wait_for(timeout=120_000)
    _tab(p, "conversation")
    t1 = _thread(p, "Check the amount is a number")
    t1.wait_for()
    assert re.search(r"Changed in commit [0-9a-f]{7}: checks the amount", t1.inner_text())
    assert t1.locator("[data-testid='lr-thread-state']").inner_text() == "Answered"
    assert t1.locator("[data-testid='lr-fixed-in']").is_visible()           # "fixed in <sha>"
    assert re.search(r"fixed in [0-9a-f]{7}", p.tid("gr-agent-comments").inner_text())
    _tab(p, "checks")
    assert "fixed" in p.tid("lr-agent-actions").inner_text()
    _tab(p, "conversation")
    _thread(p, "Check the amount is a number").locator("[data-testid='lr-resolve']").click()
    p.p.wait_for_function("() => !document.querySelector(\"[data-testid='change-set-open-comments']\")")
    p.check("local change review")


def test_plain_git_through_the_console_clone_push_a_branch_refused_main(person: Person, review_project: str):
    """git clone / push against http://<console>/git/<project>.git (the console's own /git proxy,
    then the API's git http-backend and hooks): a pushed branch is a change on the page."""
    p = person
    pid = review_project
    url = f"{CONSOLE}/git/{pid}.git"
    tmp = Path(tempfile.mkdtemp(prefix="lr-e2e-"))
    askpass = tmp / "askpass.sh"
    askpass.write_text("#!/bin/sh\nprintf 'boss_pat_dev'\n")
    askpass.chmod(0o755)
    env = {**os.environ, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "HOME": str(tmp),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Param", "GIT_AUTHOR_EMAIL": "p@x.test",
           "GIT_COMMITTER_NAME": "Param", "GIT_COMMITTER_EMAIL": "p@x.test"}

    def run(*args: str, ok: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(["git", *args], cwd=tmp / "w" if (tmp / "w").exists() else tmp, env=env,
                              capture_output=True, text=True, timeout=180)
        assert (done.returncode == 0) == ok, f"git {' '.join(args)}: {done.stderr}"
        return done

    run("clone", url, str(tmp / "w"))
    branch = f"fix/empty-copy-{p.width}"
    run("switch", "-c", branch)
    (tmp / "w" / "EMPTY.md").write_text("Nothing here yet: add your first expense.\n")
    run("add", "EMPTY.md")
    run("commit", "-qm", "Inbox: clearer empty state")
    pushed = run("push", "-u", "origin", branch)
    m = re.search(r"BOSS opened change #(\d+)", pushed.stderr)
    assert m, pushed.stderr
    assert f"/changes/{pid}/{m.group(1)}" in pushed.stderr
    refused = run("push", "origin", f"HEAD:refs/heads/main", ok=False)
    assert "main moves only through BOSS's merge" in refused.stderr
    assert git("log", "-1", "--format=%s", "main", git_dir=bare_of(pid)) != "Inbox: clearer empty state"

    _open_change(p, pid, int(m.group(1)))
    assert p.tid("change-set-title").inner_text().startswith("Inbox: clearer empty state")
    assert "pushed" in p.p.locator(".cs-head").inner_text()
    p.tid("gr-clone").click()
    assert p.tid("lr-remote").inner_text().endswith(f"/git/{pid}.git")
    p.p.keyboard.press("Escape")
    p.check("a pushed branch is a change")
