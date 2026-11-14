"""THE ticket-connector conformance suite.

Every connector passes the same questions against its recorded fake (a real HTTP server on
127.0.0.1 speaking the provider's shapes) or, for the scripted `fake` connector, an in-memory
store. A capability the manifest declares false must raise CapabilityNotSupported; one it
declares true must behave as below.

Adding a connector (ServiceNow, say): a folder under connectors/tickets/ with manifest.yaml +
adapter.py, a fake server here with that system's response shapes, and a Harness subclass in
HARNESSES. Then this suite runs against it unchanged. See docs/infra/TICKET_CONNECTORS.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from boss_engine.connectors.tickets import http as ticket_http
from boss_engine.connectors.tickets.contract import (
    CAPABILITIES,
    STATUS_CATEGORIES,
    TICKET_TYPES,
    AuthFailed,
    CapabilityNotSupported,
    HostNotAllowed,
    NotFound,
    RateLimited,
    Ticket,
    TicketConnection,
    TicketRef,
)
from boss_engine.connectors.tickets.registry import REGISTRY, get_ticket_adapter

from . import dataset as D
from .fake_jira import EMAIL, TOKEN as JIRA_TOKEN, FakeJira
from .fake_plane import SLUG, TOKEN as PLANE_TOKEN, WEBHOOK_SECRET, FakePlane


class Harness:
    """One connector + its fake. Subclasses say how to connect and how the dataset is named."""

    provider = ""
    http = True                 # False: an in-memory adapter (no transport to fault)
    #: Labels the dataset's story carries in this system (Plane expresses the type as a label).
    story_labels = {"boss", "payments"}

    def __init__(self) -> None:
        self.fake: Any = None

    @property
    def adapter(self) -> Any:
        return get_ticket_adapter(self.provider)

    def connection(self, **overrides: Any) -> TicketConnection:
        raise NotImplementedError

    def ref(self, n: int) -> TicketRef:
        return TicketRef(self.provider, key=f"KAN-{n}")

    def url_of(self, n: int) -> str:
        raise NotImplementedError

    def project(self) -> str:
        return "KAN"

    def webhook(self, secret: str) -> tuple[dict[str, str], bytes]:
        raise NotImplementedError

    def fault(self, status: int, times: int = 1, retry_after: Optional[str] = None) -> None:
        self.fake.fail_next(status, times, headers={"Retry-After": retry_after}
                            if retry_after else None)

    def close(self) -> None:
        if self.fake is not None:
            self.fake.close()


class JiraHarness(Harness):
    provider = "jira"

    def __init__(self) -> None:
        super().__init__()
        self.fake = FakeJira()

    def connection(self, **overrides: Any) -> TicketConnection:
        config = {"base_url": self.fake.base, "email": EMAIL, "project_key": "KAN",
                  "api_token": JIRA_TOKEN, **overrides}
        return TicketConnection("jira", config)

    def url_of(self, n: int) -> str:
        return f"{self.fake.base}/browse/KAN-{n}"

    def webhook(self, secret: str) -> tuple[dict[str, str], bytes]:
        body = json.dumps({"webhookEvent": "jira:issue_updated", "timestamp": 1,
                           "user": {"displayName": "Ada Lovelace"},
                           "issue": {"id": "10002", "key": "KAN-2",
                                     "self": f"{self.fake.base}/rest/api/3/issue/10002"},
                           "changelog": {"items": [{"field": "status"}]}}).encode()
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Hub-Signature": f"sha256={digest}"}, body


class PlaneHarness(Harness):
    provider = "plane"
    story_labels = {"boss", "payments", "story"}

    def __init__(self) -> None:
        super().__init__()
        self.fake = FakePlane()

    def connection(self, **overrides: Any) -> TicketConnection:
        config = {"api_base_url": self.fake.base, "workspace_slug": SLUG,
                  "project_id": self.fake.kan, "api_token": PLANE_TOKEN,
                  "settings": {"project_keys": ["KAN"]}, **overrides}
        if "settings" in overrides:
            config["settings"] = {"project_keys": ["KAN"], **overrides["settings"]}
        return TicketConnection("plane", config)

    def url_of(self, n: int) -> str:
        return self.fake.ticket_url(n)

    def webhook(self, secret: str) -> tuple[dict[str, str], bytes]:
        assert secret == WEBHOOK_SECRET
        return self.fake.webhook(D.STORY)


class FakeConnectorHarness(Harness):
    provider = "fake_connector"
    http = False

    def __init__(self) -> None:
        super().__init__()
        folder = Path(__file__).resolve().parent / "fake_connector"
        if REGISTRY.get("fake_connector") is None:
            REGISTRY.add_folder(folder)
        # The module the REGISTRY loaded (from the folder, as a plug-in is loaded).
        self.module = sys.modules[type(REGISTRY.get("fake_connector")).__module__]
        self.store = f"s{id(self)}"
        self.module.new_store(self.store)

    def connection(self, **overrides: Any) -> TicketConnection:
        config = {"base_url": "https://tickets.fake.example", "api_token": self.module.TOKEN,
                  "store": self.store, "settings": {"project_keys": ["KAN"]}, **overrides}
        if "settings" in overrides:
            config["settings"] = {"project_keys": ["KAN"], **overrides["settings"]}
        return TicketConnection("fake_connector", config)

    def url_of(self, n: int) -> str:
        return f"https://tickets.fake.example/browse/KAN-{n}"

    def webhook(self, secret: str) -> tuple[dict[str, str], bytes]:
        body = json.dumps({"kind": "updated", "key": "KAN-2", "actor": "Ada"}).encode()
        return {"X-Fake-Signature": hmac.new(secret.encode(), body,
                                             hashlib.sha256).hexdigest()}, body


HARNESSES = {"jira": JiraHarness, "plane": PlaneHarness, "fake_connector": FakeConnectorHarness}
SECRETS = {"jira": "jira-hook-secret", "plane": WEBHOOK_SECRET, "fake_connector": "fake-secret"}


@pytest.fixture(params=sorted(HARNESSES))
def harness(request, monkeypatch):
    monkeypatch.setattr(ticket_http, "SLEEP", lambda seconds: None)
    made = HARNESSES[request.param]()
    yield made
    made.close()


def _needs(h: Harness, capability: str) -> None:
    if not h.adapter.can(capability):
        pytest.skip(f"{h.provider} declares {capability}=false (checked by "
                    "test_declared_false_capabilities_refuse)")


def _needs_http(h: Harness) -> None:
    if not h.http:
        pytest.skip(f"{h.provider} has no HTTP transport")


# ---------------------------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------------------------


def test_manifest_and_adapter_agree(harness):
    adapter = harness.adapter
    assert set(adapter.ticket_capabilities) == set(CAPABILITIES)
    assert adapter.manifest.key == harness.provider
    assert adapter.catalog_entry().provider_key == harness.provider


def test_declared_false_capabilities_refuse(harness):
    adapter, conn = harness.adapter, harness.connection()
    calls = {
        "read": lambda: adapter.get_ticket(conn, harness.ref(D.STORY)),
        "browse": lambda: adapter.list_projects(conn),
        "write_comment": lambda: adapter.comment(conn, harness.ref(D.STORY), "x"),
        "transition": lambda: adapter.transition(conn, harness.ref(D.STORY), "doing"),
        "link": lambda: adapter.link(conn, harness.ref(D.STORY), "https://x.example/p/1", "PR"),
        "webhook": lambda: adapter.verify_webhook({}, b"", "s"),
    }
    for capability, call in calls.items():
        if not adapter.can(capability):
            with pytest.raises(CapabilityNotSupported):
                call()


def test_get_ticket_is_normalised(harness):
    _needs(harness, "read")
    ticket = harness.adapter.get_ticket(harness.connection(), harness.ref(D.STORY))
    assert isinstance(ticket, Ticket)
    assert ticket.provider == harness.provider
    assert ticket.key == "KAN-2"
    assert ticket.title == "Add Apple Pay"
    assert ticket.type == "story" and ticket.type in TICKET_TYPES
    assert ticket.status == "To Do" and ticket.status_category == "todo"
    assert ticket.status_category in STATUS_CATEGORIES
    assert "We need Apple Pay." in ticket.body_markdown
    assert "## Acceptance criteria" in ticket.body_markdown
    assert ticket.acceptance_criteria == D.AC_ITEMS
    assert harness.story_labels <= set(ticket.labels)
    assert ticket.assignee == "Ada Lovelace" and ticket.reporter == "Grace Hopper"
    assert str(ticket.priority).lower() == "high"
    assert [c.body for c in ticket.comments] == ["First note", "Second note", "Third note"]
    assert ticket.comments[0].author == "Grace Hopper" and ticket.comments[0].at
    assert ticket.parent_key == "KAN-1"
    assert [(a.name, a.size) for a in ticket.attachments] == [("mock.png", 2048)]
    assert ticket.attachments[0].url
    assert ticket.updated_at
    assert ticket.url
    assert ticket.screen.get("verdict") == "clean" and not ticket.untrusted
    json.dumps(ticket.to_dict())   # serialisable for Phase B


def test_comments_are_the_latest_n(harness):
    _needs(harness, "read")
    conn = harness.connection(settings={"comments_limit": 2})
    ticket = harness.adapter.get_ticket(conn, harness.ref(D.STORY))
    assert [c.body for c in ticket.comments] == ["Second note", "Third note"]


def test_get_ticket_by_url(harness):
    _needs(harness, "read")
    conn = harness.connection()
    refs = harness.adapter.parse_ref(f"see {harness.url_of(D.STORY)} please", conn=conn)
    assert len(refs) == 1
    assert harness.adapter.get_ticket(conn, refs[0]).title == "Add Apple Pay"
    # a plain URL string is accepted too
    assert harness.adapter.get_ticket(conn, harness.url_of(D.STORY)).title == "Add Apple Pay"


def test_epic_and_children_count(harness):
    _needs(harness, "read")
    epic = harness.adapter.get_ticket(harness.connection(), harness.ref(D.EPIC))
    assert epic.type == "epic"
    assert epic.children_count == len(D.CHILDREN)


def test_missing_ticket_is_not_found(harness):
    _needs(harness, "read")
    with pytest.raises(NotFound):
        harness.adapter.get_ticket(harness.connection(), harness.ref(9999))


def test_wrong_token_is_auth_failed_and_never_echoed(harness):
    _needs(harness, "read")
    bad = "wrong-token-ABCDEFGH12345"
    with pytest.raises(AuthFailed) as raised:
        harness.adapter.get_ticket(harness.connection(api_token=bad), harness.ref(D.STORY))
    assert bad not in str(raised.value) and bad not in repr(raised.value)
    assert bad not in repr(harness.connection(api_token=bad))


def test_injected_text_is_marked_untrusted(harness):
    _needs(harness, "read")
    ticket = harness.adapter.get_ticket(harness.connection(), harness.ref(D.INJECTED))
    assert ticket.untrusted
    assert ticket.screen["verdict"] == "suspicious" and ticket.screen["rules"]
    assert "Ignore all previous instructions" in ticket.body_markdown   # never rewritten


def test_rate_limit_is_retried_then_typed(harness):
    _needs(harness, "read")
    _needs_http(harness)
    harness.fault(429, times=2, retry_after="1")
    assert harness.adapter.get_ticket(harness.connection(), harness.ref(D.STORY)).key == "KAN-2"
    harness.fault(429, times=ticket_http.MAX_RETRIES + 1, retry_after="7")
    with pytest.raises(RateLimited) as raised:
        harness.adapter.get_ticket(harness.connection(), harness.ref(D.STORY))
    assert raised.value.retry_after == 7


def test_server_errors_are_retried(harness):
    _needs(harness, "read")
    _needs_http(harness)
    harness.fault(503, times=2)
    assert harness.adapter.get_ticket(harness.connection(), harness.ref(D.STORY)).key == "KAN-2"


def test_search_paginates(harness):
    _needs(harness, "browse")
    found = harness.adapter.search(harness.connection(), "Filler", limit=500)
    assert len(found) == D.FILLERS
    assert len({t.key for t in found}) == D.FILLERS
    assert len(harness.adapter.search(harness.connection(), "Filler", limit=7)) == 7


def test_search_by_key_and_filters(harness):
    _needs(harness, "browse")
    conn = harness.connection()
    assert [t.key for t in harness.adapter.search(conn, "KAN-2")] == ["KAN-2"]
    done = harness.adapter.search(conn, "", filters={"status_category": "done"}, limit=500)
    assert {t.key for t in done} == {"KAN-3", "KAN-5"}
    labelled = harness.adapter.search(conn, "", filters={"labels": ["payments"]}, limit=500)
    assert [t.key for t in labelled] == ["KAN-2"]


def test_list_projects_paginates(harness):
    _needs(harness, "browse")
    projects = harness.adapter.list_projects(harness.connection())
    assert {p.key for p in projects} == {"KAN", "OPS", "WEB"}
    kan = next(p for p in projects if p.key == "KAN")
    assert kan.name == "Kanban demo" and kan.id
    assert [p.key for p in harness.adapter.list_projects(harness.connection(), "kanban")] == \
        ["KAN"]


def test_epics_children_unparented(harness):
    _needs(harness, "browse")
    conn = harness.connection()
    epics = harness.adapter.list_epics(conn, harness.project())
    assert [e.key for e in epics] == ["KAN-1"]
    assert epics[0].children_count == len(D.CHILDREN)
    children = harness.adapter.list_children(conn, harness.ref(D.EPIC))
    assert sorted(int(t.key.split("-")[1]) for t in children) == D.CHILDREN
    assert all(t.parent_key == "KAN-1" for t in children)
    loose = harness.adapter.list_unparented(conn, harness.project())
    assert sorted(int(t.key.split("-")[1]) for t in loose) == D.UNPARENTED_OPEN
    assert next(t for t in loose if t.key == "KAN-6").untrusted   # screened in lists too


def test_comment_round_trip(harness):
    _needs(harness, "write_comment")
    conn = harness.connection()
    posted = harness.adapter.comment(conn, harness.ref(D.STORY),
                                     "BOSS picked this up.\n\n- plan ready\n- PR soon")
    assert "BOSS picked this up." in posted.body
    again = harness.adapter.get_ticket(harness.connection(), harness.ref(D.STORY))
    assert "BOSS picked this up." in again.comments[-1].body
    assert "plan ready" in again.comments[-1].body


def test_transition_by_category_and_mapping(harness):
    _needs(harness, "transition")
    conn = harness.connection()
    adapter = harness.adapter
    moved = adapter.transition(conn, harness.ref(D.STORY), "doing")
    assert moved.changed and moved.to_status == "In Progress"
    assert adapter.get_ticket(harness.connection(), harness.ref(D.STORY)).status_category == "doing"
    review = adapter.transition(harness.connection(), harness.ref(D.STORY), "review")
    assert review.to_status == "In Review"
    assert adapter.get_ticket(harness.connection(), harness.ref(D.STORY)).status_category == \
        "review"
    done = adapter.transition(harness.connection(), harness.ref(D.STORY), "done")
    assert done.to_status == "Done"
    same = adapter.transition(harness.connection(), harness.ref(D.STORY), "done")
    assert not same.changed
    with pytest.raises(ValueError):
        adapter.transition(conn, harness.ref(D.STORY), "finished")


def test_transition_follows_a_custom_mapping(harness):
    _needs(harness, "transition")
    conn = harness.connection(settings={"status_mapping": {"doing": "In Review"}})
    moved = harness.adapter.transition(conn, harness.ref(D.STORY), "doing")
    assert moved.to_status == "In Review"
    # and reading maps that status back to the category it was mapped from
    assert harness.adapter.get_ticket(conn, harness.ref(D.STORY)).status_category == "doing"


def test_link_is_idempotent(harness):
    _needs(harness, "link")
    conn = harness.connection()
    first = harness.adapter.link(conn, harness.ref(D.STORY), "https://github.com/a/b/pull/7",
                                 "PR #7")
    second = harness.adapter.link(harness.connection(), harness.ref(D.STORY),
                                  "https://github.com/a/b/pull/7", "PR #7")
    assert first.created and not second.created
    with pytest.raises(ValueError):
        harness.adapter.link(conn, harness.ref(D.STORY), "javascript:alert(1)", "x")


def test_create(harness):
    _needs(harness, "create")
    result = harness.adapter.create(harness.connection(), title="New from BOSS",
                                    body_markdown="Body", labels=["boss"])
    assert result.external_issue_id
    if harness.adapter.can("read") and result.external_key:
        made = harness.adapter.get_ticket(harness.connection(), harness.ref(
            int(result.external_key.split("-")[1])))
        assert made.title == "New from BOSS"
        assert "boss" in made.labels


def test_webhook_verify_and_parse(harness):
    _needs(harness, "webhook")
    secret = SECRETS[harness.provider]
    headers, body = harness.webhook(secret)
    adapter = harness.adapter
    assert adapter.verify_webhook(headers, body, secret)
    assert not adapter.verify_webhook(headers, body + b" ", secret)
    assert not adapter.verify_webhook(headers, body, "another-secret")
    assert not adapter.verify_webhook({}, body, secret)
    assert not adapter.verify_webhook(headers, body, "")
    event = adapter.parse_webhook(body)
    assert event.provider == harness.provider
    assert event.kind == "updated"
    assert event.ref is not None
    conn = harness.connection()
    assert adapter.get_ticket(conn, event.ref).title == "Add Apple Pay"


def test_requests_never_leave_for_another_host(harness, monkeypatch):
    _needs(harness, "read")
    _needs_http(harness)
    sent: list[str] = []
    real = ticket_http.TRANSPORT

    def spy(method, url, *a, **k):
        sent.append(url)
        return real(method, url, *a, **k)

    monkeypatch.setattr(ticket_http, "TRANSPORT", spy)
    http = harness.adapter.session(harness.connection())
    with pytest.raises(HostNotAllowed):
        http.request("GET", "https://evil.example/steal")
    with pytest.raises(HostNotAllowed):
        http.request("GET", "http://169.254.169.254/latest/meta-data/")
    assert sent == []
    # the configured base URL itself is allowed (that is how a self-hosted system is reached)
    harness.adapter.get_ticket(harness.connection(), harness.ref(D.STORY))
    assert sent and all(u.startswith(harness.fake.base) for u in sent)


def test_parse_ref_ignores_lookalikes(harness):
    _needs(harness, "read")
    conn = harness.connection()
    text = ("Encoding is UTF-8, hash SHA-256, ISO-8601 dates, RFC-7231 and KAN-0 and xKAN-2 "
            "and OPS-12 but not https://evil.example/browse/KAN-9")
    assert harness.adapter.parse_ref(text, conn=conn) == []
    found = harness.adapter.parse_ref("Fix KAN-2 and KAN-2 again, then (KAN-4).", conn=conn)
    assert [r.key for r in found] == ["KAN-2", "KAN-4"]
