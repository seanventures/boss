"""One reading of a deploy's state (infra_view.deploy_status), drawn by every surface.

After approving HelpDesk's deploy the operator saw "Approve" still offered, no move to in
progress, a run step saying "Nothing has deployed" over a deploy that had shipped, and no way to
open the app. Each surface had derived the state its own way; they now all draw this.
"""
from __future__ import annotations

from boss_engine.services.governed_host_deploy import same_release
from boss_engine.services.infra_view import deploy_status, health_checks, health_line

HEALTH_OK = {"healthy": True, "checks": [{"ok": True, "url": "http://h:8000/readyz",
                                          "status": 200, "expected": 200}]}
HEALTH_BAD = [{"ok": False, "url": "http://h:8000/readyz", "status": 404, "expected": 200}]


def test_every_phase_has_a_word_a_sentence_and_no_link_until_it_is_live():
    waiting = deploy_status("awaiting_approval", decision_status="pending", url="h:8000")
    assert (waiting["phase"], waiting["word"]) == ("needs_approval", "Needs you")
    assert "Nothing has run" in waiting["sentence"] and waiting["open_url"] is None

    queued = deploy_status("approved", decision_status="approved")
    assert (queued["phase"], queued["word"]) == ("queued", "Starting")

    deploying = deploy_status("running", steps=[{"label": "Preflight checks", "status": "ok"},
                                                {"label": "Plan and build", "status": "running"}])
    assert deploying["phase"] == "deploying" and deploying["now"] == "Plan and build"
    assert deploying["sentence"] == "Deploying: Plan and build."


def test_live_links_the_app_only_when_its_recorded_health_check_passed():
    live = deploy_status("succeeded", health=HEALTH_OK, url="h:8000")
    assert live["word"] == "Live" and live["open_url"] == "http://h:8000/"
    assert live["sentence"] == "Live at http://h:8000/. Health check passed: GET /readyz → 200."

    unhealthy = deploy_status("succeeded", health=HEALTH_BAD, url="h:8000")
    assert unhealthy["open_url"] is None
    assert "did not pass: GET /readyz → 404 (expected 200)" in unhealthy["sentence"]

    unchecked = deploy_status("succeeded", url="h:8000")
    assert unchecked["open_url"] is None and "no result was recorded" in unchecked["sentence"]

    # A stack deployment only reaches succeeded through its manifest's health checks.
    stack = deploy_status("succeeded", url="h:9000", success_means_healthy=True)
    assert stack["open_url"] == "http://h:9000/"


def test_a_request_whose_approval_went_elsewhere_is_superseded_not_waiting_on_you():
    orphan = deploy_status("awaiting_approval", decision_status="approved")
    assert (orphan["phase"], orphan["word"]) == ("superseded", "Superseded")
    assert "Nothing ran" in orphan["sentence"]


def test_only_the_current_deploy_is_live():
    """An older success's drawer said "Live at …" beside a row that said Done (rc10 tester)."""
    seen = deploy_status("succeeded", health=HEALTH_OK, url="192.0.2.202:8000", newest_for_target=False)
    assert seen["phase"] == "replaced" and seen["word"] == "Done"
    assert seen["open_url"] is None and "Live at" not in seen["sentence"]
    assert "replaced it" in seen["sentence"]


def test_a_failure_a_later_success_fixed_is_history():
    """A blocked deploy stayed red, and counted under Failed, after later deploys succeeded."""
    seen = deploy_status("blocked", failure="/opt/boss-apps is not writable", fixed_later=True)
    assert seen["tone"] == "grey" and seen["word"] == "Failed earlier"
    assert "A later deploy of this app succeeded." in seen["sentence"]
    assert deploy_status("blocked", failure="x")["tone"] == "red"


def test_a_failure_says_its_first_line():
    failed = deploy_status("blocked", failure="/opt/boss-apps could not be made\nmore")
    assert failed["phase"] == "failed" and failed["sentence"] == "/opt/boss-apps could not be made"


def test_recorded_health_reads_as_a_list_in_both_shapes():
    assert health_checks(HEALTH_OK) == HEALTH_OK["checks"]
    assert health_checks(HEALTH_BAD) == HEALTH_BAD
    assert health_line([{"ok": True, "url": "http://h/", "status": 200}]) == "GET / → 200"


def test_a_release_is_matched_not_its_spelling():
    assert same_release(None, "main") and same_release("main", "")
    assert same_release("8a009a6", "8a009a6f00d1e2")
    assert not same_release("main", "8a009a6") and not same_release("8a009a6", "8a019a6")
