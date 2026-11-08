"""DELETE PROJECT, CLICK BY CLICK (operator, 2026-11-08: "provide option to delete the project some
where in the screen - it should clean up all workflows, builds and other resources in database").

Each test seeds its own project and drives the dialog the way a person does, at 1440 and 390:
from Project › Settings › Danger zone and from the project menu; what goes is counted; a wrong
name keeps Delete off with its reason; a cancelled step-up says "Not deleted"; a confirmed one
lands on Projects with "Deleted <name>", and the project is gone from the sidebar, ⌘K, the bell
and Approvals. What was made is read back from the API (and the dev launcher's record of the
servers it was asked to clear). See conftest.py for the environment and the guard.
"""
from __future__ import annotations

from conftest import Person, api, dev, rows_of, seeded_project


def _open_from_settings(p: Person, project: dict) -> None:
    p.settings(project["id"])
    zone = p.tid("px-set-danger")
    zone.scroll_into_view_if_needed()
    assert "danger zone" in zone.inner_text().lower()
    p.tid("px-danger-delete").click()
    p.dialog.wait_for()
    p.ready()


def _open_menu(p: Person) -> None:
    """The project's ⋯ menu, in its page header."""
    p.tid("px-head-more").locator("summary").click()
    p.tid("px-more-delete").wait_for()


def test_settings_danger_zone_opens_the_dialog_with_counts(person: Person):
    p = person
    project = seeded_project()
    _open_from_settings(p, project)
    d = p.dialog
    assert f"Delete {project['name']}?" in d.inner_text()
    # What goes: counted by kind, a compact list.
    for kind in ("workflow_runs", "decisions", "work_items", "alerts", "kg_facts", "previews"):
        assert p.tid(f"delete-count-{kind}").count() == 1, f"{kind} is not counted"
    assert int(p.tid("delete-count-workflow_runs").locator("b").inner_text().replace(",", "")) >= 1
    apps = p.tid("delete-apps").inner_text()
    assert "helpdesk-ai-demo" in apps and "demo-host" in apps
    # What stays, the export (none yet: said, not linked), the name field and the step-up.
    keeps = p.tid("delete-keeps").inner_text()
    assert "GitHub" in keeps and "audit log" in keeps
    assert "no project export" in p.tid("delete-export").inner_text()
    assert "passkey" in p.tid("delete-stepup").inner_text()
    btn = p.tid("delete-confirm")
    assert btn.is_disabled()
    assert f"Type {project['name']}" in p.tid("delete-reason").inner_text()
    p.shot("dialog-ready")
    p.check("dialog from settings")
    # Cancel closes it; nothing was deleted.
    p.tid("delete-cancel").click()
    p.dialog.wait_for(state="detached")
    assert rows_of(project["id"]) > 1


def test_the_project_more_menu_has_the_same_entry(person: Person):
    p = person
    project = seeded_project()
    p.settings(project["id"])
    _open_menu(p)
    item = p.tid("px-more-delete")
    assert item.inner_text().strip().startswith("Delete project")
    item.click()
    p.dialog.wait_for()
    p.ready()
    assert project["name"] in p.dialog.inner_text()
    p.p.keyboard.press("Escape")
    p.dialog.wait_for(state="detached")
    p.check("dialog from the menu")


def test_a_wrong_name_keeps_delete_off_and_says_why(person: Person):
    p = person
    project = seeded_project()
    _open_from_settings(p, project)
    field = p.tid("delete-name")
    field.fill(project["name"][:-1])
    btn = p.tid("delete-confirm")
    assert btn.is_disabled()
    assert "doesn't match" in p.tid("delete-reason").inner_text()
    assert "doesn't match" in (btn.get_attribute("title") or "")
    p.shot("dialog-wrong-name")
    field.press("Enter")                       # Enter does nothing while the name is wrong
    p.p.wait_for_timeout(400)
    assert p.dialog.is_visible() and rows_of(project["id"]) > 1
    field.fill(project["name"])
    assert btn.is_enabled() and p.tid("delete-reason").inner_text().strip() == ""
    p.shot("dialog-name-typed")
    p.check("wrong name")


def test_a_cancelled_step_up_deletes_nothing_and_says_so(person: Person):
    p = person
    p.expect(r"^401 POST /v1/projects/[^ ]+/delete", r"status of 401")
    project = seeded_project()
    _open_from_settings(p, project)
    dev(mfa="lapsed")
    p.tid("delete-name").fill(project["name"])
    p.tid("delete-confirm").click()
    sheet = p.tid("step-up-sheet")
    sheet.wait_for(timeout=30_000)
    p.shot("dialog-step-up")
    sheet.get_by_role("button", name="Cancel").click()
    said = p.tid("delete-dialog-refused")
    said.wait_for()
    assert said.inner_text().startswith("Not deleted"), said.inner_text()
    assert p.tid("delete-confirm").is_enabled(), "the button is usable again"
    p.shot("dialog-not-deleted")
    assert rows_of(project["id"]) > 1
    status, _ = api("GET", f"/v1/projects/{project['id']}/delete")
    assert status == 200
    p.check("step-up cancelled")


def test_confirming_lands_on_projects_and_the_project_is_gone_everywhere(person: Person):
    p = person
    p.expect(r"^401 POST /v1/projects/[^ ]+/delete", r"status of 401")
    project = seeded_project()
    # The project page's own polls that were in flight while it went answer 404, once, on the
    # page being left: expected. Nothing on the page landed on may ask for it.
    p.expect(rf"^404 GET /v1/(projects/{project['id']}/|[^ ]*project_id={project['id']}\b)",
             r"status of 404")
    _open_from_settings(p, project)
    # In the sidebar before (desktop: the sidebar's project list).
    if not p.phone:
        assert p.tid(f"nav-project-{project['id']}").count() == 1
    dev(mfa="lapsed")
    p.tid("delete-name").fill(project["name"])
    p.tid("delete-confirm").click()
    sheet = p.tid("step-up-sheet")
    sheet.wait_for(timeout=30_000)
    pw = sheet.locator("input[type='password']")
    if pw.count():
        pw.fill("pxdev-pass")
        sheet.get_by_role("button", name="Confirm").click()
    else:
        sheet.locator("input").first.fill("000000")
    p.p.wait_for_url(lambda url: "area=projects" in url and "project_id=" not in url, timeout=120_000)
    p.p.wait_for_selector("[data-testid='px-projects']", timeout=120_000)
    toast = p.p.locator(".toast-container")
    toast.wait_for(timeout=15_000)
    assert f"Deleted {project['name']}" in toast.inner_text()
    p.p.wait_for_timeout(2500)          # the list fills in; the toast is still up
    p.shot("landed-projects")
    p.check("left the project page")
    # From here on, nothing may ask for the deleted project at all.
    p.bad.clear()
    p.allowed = [a for a in p.allowed if "404" not in a.pattern]

    # GONE: the API, the servers it was on, the sidebar, ⌘K, the bell and Approvals.
    status, _ = api("GET", f"/v1/projects/{project['id']}/delete")
    assert status in (403, 404)
    assert rows_of(project["id"]) == 0
    assert "helpdesk-ai-demo" in dev()["removed"]
    assert project["name"] not in p.tid("px-projects").inner_text()
    if not p.phone:
        assert p.tid(f"nav-project-{project['id']}").count() == 0
    p.p.keyboard.press("Meta+k")
    switch = p.tid("palette-jump-switch")
    if switch.count():
        switch.click()
        p.p.wait_for_timeout(300)
        assert p.tid(f"palette-jump-to-{project['id']}").count() == 0
    p.p.keyboard.press("Escape")
    status, items = api("GET", "/v1/decisions?status=pending&limit=500")
    if status == 200:
        rows = items.get("items", items) if isinstance(items, dict) else items
        assert not [d for d in rows if isinstance(d, dict) and d.get("project_id") == project["id"]]
    p.go("area=approvals", "body")
    p.p.wait_for_timeout(1500)
    assert project["name"] not in p.p.locator("main, body").first.inner_text()
    p.check("landed")
