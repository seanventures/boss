"""OPTIONAL: the Jira connector against a real Jira Cloud site.

Runs only with ``BOSS_TICKET_LIVE=jira`` (default CI uses the fake server only), and skips
cleanly when the macOS Keychain item ``boss-jira-test`` is missing. The token is read in-process
(``security find-generic-password -s boss-jira-test -w``) and never printed, logged or put in an
assertion message; the account attribute (acct) is the login email.

Read-only checks first. The write checks create ONE ticket labelled ``boss-conformance`` in KAN,
comment on it, move it doing -> done, add a remote link, and leave it in Done. Nothing else on
the site is changed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

import pytest

from boss_engine.connectors.tickets.contract import TicketConnection, TicketRef
from boss_engine.connectors.tickets.registry import get_ticket_adapter

SITE = os.environ.get("BOSS_JIRA_LIVE_SITE", "https://paramg-1794632206014.atlassian.net")
PROJECT = os.environ.get("BOSS_JIRA_LIVE_PROJECT", "KAN")
ITEM = "boss-jira-test"
LABEL = "boss-conformance"

pytestmark = pytest.mark.skipif("jira" not in os.environ.get("BOSS_TICKET_LIVE", "").split(","),
                                reason="live Jira run is opt-in: BOSS_TICKET_LIVE=jira")


def _keychain() -> tuple[str, str]:
    if not shutil.which("security"):
        pytest.skip("no macOS Keychain on this host")
    token = subprocess.run(["security", "find-generic-password", "-s", ITEM, "-w"],
                           capture_output=True, text=True)
    if token.returncode != 0 or not token.stdout.strip():
        pytest.skip(f"Keychain item {ITEM} is missing")
    attrs = subprocess.run(["security", "find-generic-password", "-s", ITEM],
                           capture_output=True, text=True)
    match = re.search(r'"acct"<blob>="([^"]+)"', attrs.stdout or "")
    if not match:
        pytest.skip(f"Keychain item {ITEM} has no account (email)")
    return match.group(1), token.stdout.strip()


@pytest.fixture(scope="module")
def live():
    email, token = _keychain()
    config = {"base_url": SITE, "email": email, "project_key": PROJECT, "api_token": token}

    def conn() -> TicketConnection:
        return TicketConnection("jira", dict(config))

    return conn


def test_live_read_only(live):
    jira = get_ticket_adapter("jira")
    outcome = jira.test_connection(live().configuration)
    assert outcome.get("ok") is True, "test_connection failed"
    projects = jira.list_projects(live())
    assert PROJECT in {p.key for p in projects}
    jira.list_epics(live(), PROJECT)                       # no epics is fine; no error is not
    found = jira.search(live(), "", project=PROJECT, limit=20)
    assert found and all(t.key.startswith(PROJECT + "-") for t in found)
    one = jira.get_ticket(live(), found[-1].ref)
    assert one.key == found[-1].key and one.status_category in ("todo", "doing", "review", "done")
    assert one.screen.get("verdict") in ("clean", "suspicious")
    refs = jira.parse_ref(f"see {SITE}/browse/{one.key} and SHA-256", conn=live())
    assert [r.key for r in refs] == [one.key]


def test_live_write_on_our_own_ticket(live):
    jira = get_ticket_adapter("jira")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    created = jira.create(live(), title=f"BOSS conformance check {stamp}",
                          body_markdown="Created by the BOSS ticket-connector conformance run.\n\n"
                                        "## Acceptance criteria\n\n- it is commented\n"
                                        "- it ends in Done", labels=[LABEL])
    key = created.external_key
    assert key and key.startswith(PROJECT + "-")
    ref = TicketRef("jira", key=key)
    ticket = jira.get_ticket(live(), ref)
    assert LABEL in ticket.labels
    assert ticket.acceptance_criteria == ["it is commented", "it ends in Done"]
    jira.comment(live(), ref, "BOSS conformance: comment round trip.\n\n- one\n- two")
    assert "comment round trip" in jira.get_ticket(live(), ref).comments[-1].body
    doing = jira.transition(live(), ref, "doing")
    assert jira.get_ticket(live(), ref).status_category == "doing", doing
    jira.transition(live(), ref, "done")
    assert jira.get_ticket(live(), ref).status_category == "done"
    first = jira.link(live(), ref, "https://github.com/seanventures/boss/pulls", "BOSS PRs")
    second = jira.link(live(), ref, "https://github.com/seanventures/boss/pulls", "BOSS PRs")
    assert first.created and not second.created
    mine = jira.search(live(), "", project=PROJECT, filters={"labels": [LABEL]}, limit=50)
    assert key in {t.key for t in mine}
