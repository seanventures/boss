"""LOCAL-FIRST PROJECTS, EVERY USE CASE (BOSSAPP-915; operator, 2026-11-08: "I really wanted to
build things locally before taking to git later … we should provide option to build locally and
also offline use cases. I want you to not miss any use cases and also be very slick").

Each test is a person at 1440 and at 390, ending with the harness's guard (conftest). What runs
through a dev seam instead of a whole agent build is said in the test (a stage's commits, the
worker's merge and push jobs, the preview's clone): the pipeline's own functions, not stand-ins.
"""
from __future__ import annotations

import re
import time

import pytest

from conftest import (IDEA_LOCAL, PROJECT, Person, api, bare_of, dev, fake, fresh_project, git, local_repo,
                      project_name, seam, started, unique)

pytestmark = pytest.mark.usefixtures("folders")


def _new_app(p: Person, words: str, name: str) -> None:
    """The two doors → Start a new app → the idea → its Project brief → a name."""
    p.open_sheet("1")
    p.pick_door("new")
    p.idea_with_brief(words)
    p.name(name)


def _code(p: Person, where: str) -> None:
    """The Code box: where the code lives (local | github | repo)."""
    p.tid("ap-box-code").click()
    p.tid("ap-pop-code").wait_for()
    p.tid(f"ap-where-{where}").click()
    assert p.tid(f"ap-where-{where}").get_attribute("aria-checked") == "true"


def _done_code(p: Person) -> None:
    p.tid("ap-pop-code-done").click()
    p.tid("ap-pop-code").wait_for(state="detached")


def _where(p: Person) -> str:
    """Where the code lives, as the Code box and its popover say it (a phone shows the box's
    value only; the popover has the whole sentence at every width)."""
    p.tid("ap-box-code").click()
    p.tid("ap-pop-code").wait_for()
    said = p.tid("ap-box-code").inner_text() + " " + p.tid("ap-pop-code").inner_text()
    _done_code(p)
    return said


# ── 1 a new app, on this machine, all the way: onboarding checks → a stage commits locally → a
#      change set → the review page → Merge → a preview from the local repo ────────────────────

def test_a_new_local_app_end_to_end(person: Person):
    p = person
    name = unique("Expenses local")
    _new_app(p, IDEA_LOCAL, name)
    _code(p, "local")
    assert "publish to GitHub any time" in p.tid("ap-pop-code").inner_text()
    p.check("code: on this machine")
    _done_code(p)
    box = p.tid("ap-box-code")
    assert box.get_attribute("data-where") == "local" and "This machine" in box.inner_text()
    p.create()
    pid = p.landed()
    assert started(pid)["seed"]["kind"] == "idea"
    repo = local_repo(pid)
    assert repo["local"] and repo["full_name"].startswith(f"local_{pid}/")
    assert git("log", "--format=%s", git_dir=bare_of(pid)) == "Initial commit (BOSS)", "the first commit is made"

    # The header says where the code is, with Publish one press away.
    chip = p.tid("px-head-publish")
    chip.wait_for(timeout=60_000)
    assert "Local" in chip.inner_text() and "Publish to GitHub" in chip.inner_text()

    # The onboarding agent's machine checks (git, repo, disk, network, model), fixing what it can.
    status, checks = api("POST", f"/v1/projects/{pid}/local-repo/checks", {"fix": True})
    assert status == 200 and checks["ok"], checks
    assert [c["key"] for c in checks["checks"]] == ["git", "local_repo", "disk", "network", "model"]

    # A build stage, on the pipeline's own functions (dev seam: the agent's coding skipped).
    stage = seam("stage", {"project_id": pid})
    assert stage["plane"] == "local" and stage["number"] == 1
    branch = stage["branch"]
    assert set(git("ls-tree", "--name-only", branch, git_dir=bare_of(pid)).split()) >= {"index.html", "app.js"}

    # The change set, from Work, opens BOSS's own review page (never github.com).
    p.go(f"project_id={pid}&tab=work", area="project", wait="[data-testid='px-work-rows'], [data-testid='px-work-planned']")
    link = p.p.locator("a[href*='/changes/']").first
    link.wait_for(timeout=60_000)
    assert "github.com" not in (link.get_attribute("href") or "")
    link.click()
    p.tid("change-set").wait_for(timeout=120_000)
    p.tid("change-set-title").wait_for()
    assert "Expense list page" in p.tid("change-set-title").inner_text()
    assert "→" in p.tid("change-set-branch").inner_text() or "main" in p.tid("change-set-branch").inner_text()
    files = p.tid("change-set-files").inner_text()
    assert "index.html" in files and "app.js" in files
    diff = p.tid("change-set-diff")
    assert "<h1>Expenses</h1>" in diff.inner_text()
    assert p.tid("change-set-review").inner_text().count("Validate the amount") >= 1, "the review panel's comment shows"
    assert "sandbox" in p.tid("change-set-checks").inner_text().lower()
    p.check("the change set")

    # Merge: a person (step-up when the decision asks for it), then the worker's governed merge.
    merge = p.tid("change-set-merge")
    merge.wait_for()
    merge.click()
    step = p.tid("step-up-sheet")
    try:
        step.wait_for(timeout=4000)
        pw = step.locator("input[type='password']")
        if pw.count():
            pw.fill("pxdev-pass")
            step.get_by_role("button", name="Confirm").click()
    except Exception:  # noqa: BLE001 — the decision did not ask for a step-up
        pass
    p.p.wait_for_function("() => /merging|Merged/.test(document.querySelector(\"[data-testid='change-set']\")?.innerText || '')", timeout=60_000)
    merged = seam("run-merges", {"project_id": pid})["merged"]
    assert merged and merged[0]["status"] in ("created", "reused"), merged
    p.tid("change-set-merged").wait_for(timeout=90_000)
    assert git("rev-parse", "main", git_dir=bare_of(pid)) == merged[0]["merged_sha"]
    p.check("merged")

    # The preview clones the local repository (file://), never GitHub.
    preview = seam("preview", {"project_id": pid, "branch": "main"})
    assert preview["url"].startswith("file://") and preview["via"] == "local"
    assert {"index.html", "app.js"} <= set(preview["files"])


# ── 2 a new app with a GitHub repo BOSS makes (the plumbing gate's fake GitHub) ───────────────

def test_a_new_app_with_a_github_repo_boss_makes(person: Person):
    p = person
    name = unique("Expenses gh")
    _new_app(p, IDEA_LOCAL, name)
    _code(p, "github")
    form = p.tid("ap-newrepo")
    assert p.tid("ap-newrepo-owner").input_value() == "acme"
    slug = p.tid("ap-newrepo-name").input_value()
    assert re.fullmatch(r"[a-z0-9-]+", slug) and slug.startswith("expenses-gh"), slug
    assert p.tid("ap-newrepo-private").is_checked(), "private by default"
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='ap-newrepo-check']\")?.dataset.ok === 'true'", timeout=30_000)
    assert "is free on GitHub" in p.tid("ap-newrepo-check").inner_text()
    assert "your decision" in p.tid("ap-newrepo-decision").inner_text() and "press Create" in form.inner_text()
    p.check("code: a new GitHub repo")

    # A name GitHub already has: the engine's words, with the way out, and Create waits.
    fake("/github/_seed", {"owner": "acme", "repo": f"{slug}-taken"})
    p.tid("ap-newrepo-name").fill(f"{slug}-taken")
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='ap-newrepo-check']\")?.dataset.ok === 'false'", timeout=30_000)
    assert "already exists" in p.tid("ap-newrepo-check").inner_text()
    assert p.tid("ap-create").is_disabled() and "GitHub can" in p.tid("ap-create-why").inner_text()
    p.tid("ap-newrepo-name").fill(slug)
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='ap-newrepo-check']\")?.dataset.ok === 'true'", timeout=30_000)
    _done_code(p)
    assert f"acme/{slug}" in p.tid("ap-box-code").inner_text()
    p.create()
    pid = p.landed()
    made = [r["full_name"] for r in fake("/github/_created")["repos"]]
    assert f"acme/{slug}" in made, made
    assert not local_repo(pid)["local"], "the project builds on GitHub now (the local copy is a mirror)"
    status, story = api("GET", f"/v1/projects/{pid}/add-project/started")
    assert status == 200
    assert p.tid("px-head-repo").inner_text().strip().endswith(f"acme/{slug}")


def test_no_github_credential_is_said_with_its_one_fix(person: Person):
    """No App that can create repos: the default is this machine, and choosing GitHub says why
    not in the engine's words, with the one fix (Repo and models)."""
    p = person
    dev(github="none")
    _new_app(p, IDEA_LOCAL, unique("No app"))
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='ap-box-code']\")?.dataset.where === 'local'", timeout=30_000)
    _code(p, "github")
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='ap-newrepo-check']\")?.dataset.ok === 'false'", timeout=30_000)
    said = p.tid("ap-newrepo-check").inner_text()
    assert "no GitHub credential" in said and "Administration" in said, said
    fix = p.tid("ap-newrepo-fix")
    assert "Repo and models" in fix.inner_text() and "focus=repository" in (fix.get_attribute("href") or "")
    assert p.tid("ap-create").is_disabled()
    p.check("no credential")
    p.tid("ap-where-local").click()
    _done_code(p)
    assert p.tid("ap-create").is_enabled(), "on this machine needs no GitHub at all"


# ── 3 a new app in a folder of a repo you have (a monorepo) ───────────────────────────────────

def test_a_new_app_in_a_monorepo_folder(person: Person):
    p = person
    p.expect(r"^409 POST /v1/add-project \{\"error\":\"not_ready\"")
    name = unique("Expenses mono")
    _new_app(p, IDEA_LOCAL, name)
    _code(p, "repo")
    p.tid("ap-code-search").fill("helpdesk-ai-demo")
    opt = p.p.locator("button.npx-opt[data-testid^='ap-code-repo-']").first
    opt.wait_for()
    full = opt.locator(".npx-mono").inner_text().strip()
    if opt.get_attribute("data-in-use") is None:
        opt.click()
    else:  # already in a project: a separate one on the same code, asked once
        rid = (opt.get_attribute("data-testid") or "").removeprefix("ap-code-repo-")
        p.tid(f"ap-code-repo-{rid}-separate").click()
        p.tid(f"ap-code-repo-{rid}-separate-yes").click()
    _done_code(p)
    assert full.split("/")[-1] in p.tid("ap-box-code").inner_text()
    p.create()
    pid = p.landed()
    assert started(pid)["seed"]["picks"]["repos"] == [full]
    assert not local_repo(pid)["local"]


# ── 4 existing code: a folder on this machine, with its history and without ────────────────────

def _bring_local(p: Person, folder: str) -> None:
    p.open_sheet("1")
    p.pick_door("existing")
    p.type(folder.split("-", 1)[1])
    row = p.tid(f"ap-local-{folder}")
    row.wait_for(timeout=30_000)
    assert "files" in row.inner_text()
    row.click()
    p.tid("ap-bring").wait_for()


def _onboarding_dots(p: Person, pid: str, offline: bool = False) -> None:
    """The onboarding agent's machine checks, as dots (BOSSAPP-924): git, repo, disk, network,
    model; what it fixed itself; one line for what it couldn't (offline with a remote model).
    The dev stack runs no worker: its onboarding job's first pass runs through a dev seam."""
    dev(offline=offline)
    try:
        seam("onboard", {"project_id": pid})
    finally:
        dev(offline=False)
    p.p.reload(wait_until="domcontentloaded")      # the page polls slowly; the pass just ran
    dots = p.tid("ob-machine")
    dots.wait_for(timeout=120_000)
    for key in ("git", "local_repo", "disk", "network", "model"):
        assert p.tid(f"ob-dot-{key}").count() == 1, f"no {key} dot"
    assert p.tid("ob-dot-git").get_attribute("data-ok") == "true"
    if offline:
        cant = p.tid("ob-machine-cant")
        cant.wait_for()
        assert "Offline" in cant.inner_text() and "local engine" in cant.inner_text(), cant.inner_text()
        assert p.tid("ob-dot-model").get_attribute("data-ok") == "false"
    else:
        assert p.tid("ob-dot-model").get_attribute("data-ok") == "true"


@pytest.mark.parametrize("history", [True, False], ids=["with-history", "without-history"])
def test_import_a_folder_from_this_machine(person: Person, folders: dict, history: bool):
    p = person
    _bring_local(p, folders["history"])
    assert p.tid("ap-bring-copy").get_attribute("aria-checked") == "true", "copy it here is the way in for a folder with no remote"
    keep = p.tid("ap-keep-history")
    assert keep.is_checked(), "its history is kept by default"
    if not history:
        keep.uncheck()
    where = _where(p)
    assert "This machine" in where and ("history kept" if history else "one commit") in where, where
    p.check("bring a folder")
    name = unique("Notes " + ("hist" if history else "flat"))
    p.name(name)
    p.create()
    pid = p.landed()
    assert "tab=onboarding" in p.p.url
    commits = git("log", "--all", "--format=%s", git_dir=bare_of(pid)).splitlines()
    branches = git("branch", "--format=%(refname:short)", git_dir=bare_of(pid)).split()
    if history:
        assert commits[:2] == ["second", "first"] and "feature-1" in branches, (commits, branches)
    else:
        assert len(commits) == 1 and branches == ["main"], (commits, branches)
    # Offline with a remote model is said in one line, with its fix (the second variant).
    _onboarding_dots(p, pid, offline=not history)
    p.check("onboarding of an imported folder")


def test_a_folder_without_git_becomes_one_commit(person: Person, folders: dict):
    p = person
    _bring_local(p, folders["plain"])
    assert p.tid("ap-keep-history").count() == 0, "no history to keep"
    p.name(unique("Plain site"))
    p.create()
    pid = p.landed()
    assert len(git("log", "--format=%s", git_dir=bare_of(pid)).splitlines()) == 1


# ── 5 a folder with a GitHub remote: keep using GitHub, or copy it here ───────────────────────

@pytest.mark.parametrize("choice", ["github", "copy"])
def test_a_folder_with_a_github_remote(person: Person, folders: dict, choice: str):
    p = person
    _bring_local(p, folders["remote"])
    assert "acme/kaizen-notes" in p.tid("ap-bring").inner_text()
    assert p.tid("ap-bring-github").get_attribute("aria-checked") == "true", "BOSS knows the repo: keep using it is picked"
    p.tid(f"ap-bring-{choice}").click()
    if choice == "github":
        block = p.tid("ap-bring-gh-inuse")
        if block.count():   # already in a project: the same rules as the repo list
            p.tid("ap-bring-gh-separate").click()
            p.tid("ap-bring-gh-separate-yes").click()
        p.p.wait_for_selector("[data-testid='ap-onboarding-will']", timeout=30_000)
        assert "kaizen-notes" in _where(p)
    else:
        assert "This machine" in _where(p)
    p.check(f"remote: {choice}")
    p.name(unique(f"Kaizen {choice}"))
    p.create()
    pid = p.landed()
    seed = started(pid)["seed"]
    if choice == "github":
        assert seed["kind"] == "repo" and seed["picks"]["repos"] == ["acme/kaizen-notes"]
        assert not local_repo(pid)["local"]
    else:
        assert local_repo(pid)["local"]
        assert "first" in git("log", "--format=%s", git_dir=bare_of(pid))


def test_a_folder_on_a_server_copied_here(person: Person):
    """A server's folder (with a GitHub remote): Copy it here brings it local, history kept
    (dev seam: the server's tar stream is a fixture folder; the hosts are TEST-NET addresses)."""
    p = person
    p.open_sheet("existing")
    p.type("/srv/apps/ledger-api")
    p.p.locator("[data-testid^='ap-folder-']").first.wait_for()
    p.box.press("Enter")
    p.tid("ap-bring").wait_for(timeout=30_000)
    p.tid("ap-bring-copy").click()
    assert "copied from" in _where(p)
    p.name(unique("Ledger here"))
    p.create()
    pid = p.landed()
    assert local_repo(pid)["local"]
    assert git("log", "--format=%s", git_dir=bare_of(pid)).splitlines()[:2] == ["second", "first"]


# ── 6 Publish to GitHub: history, PRs for open changes, the switch, idempotent ────────────────

def test_publish_to_github(person: Person):
    p = person
    pid = fresh_project("Publish me")
    seam("stage", {"project_id": pid})          # an open change set to become a PR
    repo = f"pub-{unique('x').split()[-1]}"
    p.go(f"project_id={pid}", area="project", wait="[data-testid='px-head']")
    p.tid("px-head-publish").click()
    sheet = p.tid("publish-sheet")
    sheet.wait_for()
    for step in ("repository", "push", "pull_requests", "switch"):
        assert p.tid(f"publish-step-{step}").count() == 1
    assert "1 open change" in sheet.inner_text()
    p.tid("publish-repo").fill(repo)
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='publish-check']\")?.dataset.ok === 'true'", timeout=30_000)
    p.check("publish sheet")

    # A repository that already holds other history: refused in words, at the push step.
    fake("/github/_seed", {"owner": "acme", "repo": f"{repo}-full"})
    p.tid("publish-mode-existing").click()
    p.tid("publish-repo").fill(f"{repo}-full")
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='publish-check']\")?.dataset.ok === 'true'", timeout=30_000)
    p.expect(r"^409 POST /v1/projects/[^ ]+/publish")
    p.tid("publish-go").click()
    p.tid("publish-refusal").wait_for(timeout=60_000)
    assert "other history" in p.tid("publish-refusal").inner_text()
    assert p.tid("publish-step-push").get_attribute("data-s") == "failed"
    p.check("publish refused")

    # A new, empty one: the whole way.
    p.tid("publish-mode-new").click()
    p.tid("publish-repo").fill(repo)
    p.p.wait_for_function("() => document.querySelector(\"[data-testid='publish-check']\")?.dataset.ok === 'true'", timeout=30_000)
    p.tid("publish-go").click()
    p.tid("publish-done").wait_for(timeout=120_000)
    for step in ("repository", "push", "pull_requests", "switch"):
        assert p.tid(f"publish-step-{step}").get_attribute("data-s") == "done", step
    assert "became pull requests" in p.tid("publish-done").inner_text()
    p.check("published")
    p.tid("publish-finish").click()

    # GitHub has the history and the PR; the project builds there; a second publish is a no-op.
    prs = [pr for pr in fake("/github/_prs")["prs"] if pr["repo"] == f"acme/{repo}"]
    assert len(prs) == 1 and prs[0]["head"].startswith("boss/code/"), prs
    assert not local_repo(pid)["local"]
    status, again = api("POST", f"/v1/projects/{pid}/publish", {"owner": "acme", "repo": repo, "private": True, "create": True})
    assert status == 200 and again["status"] == "published" and not again.get("steps"), again
    assert len([pr for pr in fake("/github/_prs")["prs"] if pr["repo"] == f"acme/{repo}"]) == 1
    p.p.reload()
    p.tid("px-head").wait_for()
    assert p.tid("px-head-publish").count() == 0, "a published project has no Publish chip"


def test_settings_code_publishes_too(person: Person):
    p = person
    pid = fresh_project("Settings publish")
    p.go(f"project_id={pid}&tab=settings&section=repos", area="project", wait="[data-testid='px-settings']")
    panel = p.tid("px-set-local")
    panel.wait_for(timeout=60_000)
    assert "On this machine" in panel.inner_text()
    assert p.tid("px-set-nav-repos").inner_text().strip() == "Code"
    p.tid("px-set-publish").click()
    p.tid("publish-sheet").wait_for()
    p.check("settings › code › publish")
    p.tid("publish-close").click()


# ── 7 waiting for GitHub: the wording everywhere, then back, then the PR ──────────────────────

def test_waiting_for_github_then_back(person: Person):
    p = person
    pid = fresh_project("Outage", code="github", new_repo={"owner": "acme", "repo": f"out-{unique('x').split()[-1]}", "private": True})
    run = seam("outage-run", {"project_id": pid})      # GitHub answers 502 mid-build (the fake)
    words = "Waiting for GitHub"
    try:
        # the build page
        p.go(f"project_id={pid}&build={run['run_id']}", area="code_tasks", wait="[data-testid='github-wait']")
        wait = p.p.locator("[data-testid='github-wait']").first
        assert words in wait.inner_text() and "push when GitHub is back" in wait.inner_text()
        assert re.search(r"next try", wait.inner_text()), wait.inner_text()
        assert "failed" not in p.p.locator("main, body").first.inner_text().lower().split("waiting for github")[0][-200:]
        p.check("build page: waiting")
        # Project › Work
        p.go(f"project_id={pid}&tab=work", area="project", wait="[data-testid='github-wait']")
        assert words in p.p.locator("[data-testid='github-wait']").first.inner_text()
        # Needs you (the project's Overview): an FYI, never a failure
        p.go(f"project_id={pid}&tab=overview", area="project", wait="[data-testid^='px-ov-gw-']")
        assert words in p.p.locator("[data-testid^='px-ov-gw-']").first.inner_text()
        # Agents at work
        p.go(f"project_id={pid}", area="agent_ops", wait="[data-testid='github-wait']")
        assert words in p.p.locator("[data-testid='github-wait']").first.inner_text()
        p.check("agents at work: waiting")
    finally:
        fake("/github/_outage", {"on": False})
    # GitHub is back: the worker's push-pending job pushes and opens the PR (dev seam: run once).
    attempts = seam("push-pending", {"project_id": pid})["attempts"]
    assert attempts and attempts[0]["state"] == "done", attempts
    pr_url = attempts[0]["pull_request_url"]
    assert "github.com" in pr_url or "/pulls/" in pr_url or "/pull/" in pr_url
    p.go(f"project_id={pid}&tab=work", area="project", wait="[data-testid='px-work-rows'], [data-testid='px-work-planned']")
    p.p.wait_for_timeout(1500)
    assert p.p.locator("[data-testid='github-wait']").count() == 0, "back: no longer waiting"


# ── 8 Delete a local project: its repository goes too ─────────────────────────────────────────

def test_delete_a_local_project_removes_its_repo(person: Person):
    p = person
    pid = fresh_project("Delete me")
    bare = bare_of(pid)
    assert bare.is_dir()
    status, proj = api("GET", f"/v1/projects/{pid}/add-project/started")
    name = None
    p.go(f"project_id={pid}&tab=settings", area="project", wait="[data-testid='px-settings']")
    p.tid("px-danger-delete").click()
    field = p.tid("delete-name")
    field.wait_for(timeout=60_000)
    name = p.p.locator("[data-testid='delete-reason']").inner_text().split("Type ", 1)[-1].split(" to", 1)[0].strip()
    field.fill(name)
    p.expect(r"^404 GET /v1/", r"status of 404")
    p.tid("delete-confirm").click()
    step = p.tid("step-up-sheet")
    try:
        step.wait_for(timeout=5000)
        pw = step.locator("input[type='password']")
        if pw.count():
            pw.fill("pxdev-pass")
            step.get_by_role("button", name="Confirm").click()
        else:
            step.locator("input").first.fill("000000")
    except Exception:  # noqa: BLE001 — no step-up asked
        pass
    end = time.monotonic() + 120
    while time.monotonic() < end and bare.exists():
        p.p.wait_for_timeout(500)
    assert not bare.exists(), f"{bare} is still there after Delete"
    assert not bare.parent.exists() or not any(bare.parent.iterdir())


# ── 9 Try a sample, twice: the next free name, no cap ─────────────────────────────────────────

def test_try_a_sample_twice(person: Person):
    p = person
    made = []
    for _ in range(2):
        p.open_sheet("sample")
        assert p.tid("ap-sample").get_attribute("data-lit") is not None
        p.tid("ap-sample").click()
        made.append(p.landed(timeout_s=120))
    assert made[0] != made[1], "a second press makes a second sample"
    names = [project_name(pid) for pid in made]
    if names[0] is not None:
        # The next free "Hello World N", past 20 (the old cap refused there).
        nums = [int(m.group(1)) if (m := re.fullmatch(r"Hello World(?: (\d+))?", n or "")) and m.group(1) else 1 for n in names]
        assert all(n.startswith("Hello World") for n in names) and nums[1] > nums[0], names
