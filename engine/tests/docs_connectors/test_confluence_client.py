"""The Confluence client against recorded HTTP (BOSSAPP-1384): search paging, page read with
storage-format -> markdown, the page tree, versioned update and its conflicts, the Authorization
header's shape (never logged), and the "connect Confluence in Setup" messages. No network."""
from __future__ import annotations

import base64
import logging

import pytest

from boss_engine.connectors.docs import (
    ConfluenceClient,
    ConfluenceError,
    ConfluenceNotConnected,
    VersionConflict,
    auth_headers,
    build_cql,
    markdown_to_storage,
    page_id_from_url,
    storage_to_markdown,
    validate_site,
)
from boss_engine.connectors.tickets import http as ticket_http
from boss_engine.connectors.tickets.contract import AuthFailed, HostNotAllowed, NotFound

from .fake_confluence import EMAIL, SITE, TOKEN, FakeConfluence

CONFIG = {"base_url": SITE, "deployment": "cloud", "auth_method": "basic", "email": EMAIL}


@pytest.fixture
def fake(monkeypatch):
    transport = FakeConfluence()
    monkeypatch.setattr(ticket_http, "TRANSPORT", transport)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    return transport


@pytest.fixture
def client(fake):
    return ConfluenceClient(CONFIG, lambda: TOKEN)


# ---- search ---------------------------------------------------------------------------------

def test_search_pages_through_cql_results_with_filters(client, fake):
    found = client.search("deploy", spaces=("ENG",), labels=("runbook",), limit=3)
    assert [p.id for p in found] == ["101", "102", "103"]
    first, second = [r for r in fake.requests if r["path"].endswith("/rest/api/search")]
    assert first["query"]["start"] == "0" and second["query"]["start"] == "2"
    cql = first["query"]["cql"]
    assert 'type = "page"' in cql and 'space in ("ENG")' in cql and 'label in ("runbook")' in cql
    assert 'text ~ "deploy"' in cql and cql.endswith("order by lastmodified desc")
    top = found[0]
    assert (top.title, top.space_key) == ("Deploy Runbook", "ENG")
    assert top.url == f"{SITE}/spaces/ENG/pages/101/Deploy+Runbook"
    assert "@@@" not in top.excerpt and "deploy" in top.excerpt and top.last_modified


def test_search_stops_at_the_limit_and_caps_it(client, fake):
    assert len(client.search("x", limit=2)) == 2
    assert len([r for r in fake.requests if "search" in r["path"]]) == 1
    client.search("x", limit=9999)
    assert fake.requests[-2]["query"]["limit"] == "50"


def test_cql_quoting_and_raw_cql_keeps_its_order_by():
    assert 'text ~ "say \\"hi\\""' in build_cql('say "hi"')
    cql = build_cql(cql='creator = "bot" order by title asc', spaces=("A", "B"))
    assert cql.startswith('(creator = "bot")') and cql.endswith("order by title asc")
    assert 'space in ("A", "B")' in cql


# ---- read -----------------------------------------------------------------------------------

def test_read_page_is_clean_markdown_with_links_kept(client):
    page = client.read(page_id="101")
    md = page.body_markdown
    assert (page.id, page.title, page.space_key, page.version) == ("101", "Deploy Runbook", "ENG", 4)
    assert page.labels == ["runbook", "prod"] and page.url.startswith(SITE)
    assert md.startswith("# Deploy Runbook")
    assert "[health](https://status.acme.test/health)" in md          # a plain link survives
    assert "`boss upgrade`" in md
    assert "- Announce" in md and "  - canary" in md                   # nested list
    assert "```bash\nboss upgrade --check\n```" in md                 # the code macro
    assert "> **Info:** Never on Fridays." in md                       # a panel
    assert "[the rollback guide](confluence-page:Rollback%20Guide)" in md  # a page link
    assert "| Step | Owner |" in md and "| --- | --- |" in md and "| Announce | Ops |" in md
    assert "<" not in md and "ac:" not in md and "toc" not in md.lower().replace("toc.", "")


def test_read_by_url_and_by_title_and_space(client, fake):
    by_url = client.read(url=f"{SITE}/spaces/ENG/pages/101/Deploy+Runbook")
    assert by_url.id == "101"
    assert page_id_from_url("https://acme.atlassian.net/wiki/pages/viewpage.action?pageId=77") == "77"
    by_title = client.read(title="Deploy Runbook", space="ENG")
    assert by_title.id == "101" and by_title.space_key == "ENG"
    lookup = [r for r in fake.requests if r["path"].endswith("/api/v2/pages")][-1]
    assert lookup["query"]["space-id"] == "98"                        # the key was resolved
    with pytest.raises(NotFound):
        client.read(title="Nope", space="ENG")
    with pytest.raises(ConfluenceError, match="Name the page"):
        client.read()


def test_a_long_page_is_cut_at_the_cap_and_says_so(client):
    page = client.read(page_id="101", max_chars=60, labels=False)
    assert page.truncated and len(page.body_markdown) == 60 and page.body_chars > 60


def test_attachments_labels_and_last_modified(client):
    atts = client.attachments("101")
    assert atts == [{"id": "att1", "name": "diagram.png", "media_type": "image/png", "size": 2048,
                     "download": f"{SITE}/download/attachments/101/diagram.png", "version": 1,
                     "modified": "2026-11-01T00:00:00.000Z"}]
    assert client.labels("101") == ["runbook", "prod"]
    assert client.get_page("101", body=False).last_modified == "2026-11-10T09:00:00.000Z"


def test_space_tree_is_depth_ordered_and_can_start_at_a_page(client):
    tree = client.space_tree("ENG")
    assert [(n["title"], n["depth"]) for n in tree] == [
        ("Home", 0), ("Deploy Runbook", 1), ("Rollback Guide", 2), ("Postmortems", 1)]
    sub = client.space_tree("ENG", root_id="101")
    assert [(n["id"], n["depth"]) for n in sub] == [("101", 0), ("103", 1)]
    assert len(client.space_tree("ENG", limit=2)) == 2


# ---- write ----------------------------------------------------------------------------------

def test_create_page_sends_storage_format_under_a_parent(client, fake):
    page = client.create_page(space="ENG", title="New Page", markdown="# Hi\n\n- a\n- b\n",
                              parent_id="10")
    sent = fake.pages_created[0]
    assert sent["spaceId"] == "98" and sent["parentId"] == "10" and sent["status"] == "current"
    assert sent["body"]["representation"] == "storage"
    assert "<h1>Hi</h1><ul><li>a</li><li>b</li></ul>" in sent["body"]["value"]
    assert page.id == "555"


def test_versioned_update_bumps_the_version(client, fake):
    page = client.update_page("101", markdown="new body", expected_version=4)
    sent = fake.pages_updated[0]
    assert sent["version"]["number"] == 5 and sent["title"] == "Deploy Runbook"
    assert page.version == 5


def test_update_against_a_moved_page_refuses_and_writes_nothing(client, fake):
    fake.versions["101"] = 6                       # someone edited it since it was read at 4
    with pytest.raises(VersionConflict) as caught:
        client.update_page("101", markdown="mine", expected_version=4)
    assert caught.value.current_version == 6 and caught.value.expected_version == 4
    assert "version 6" in str(caught.value) and fake.pages_updated == []


def test_a_conflict_the_server_reports_is_a_version_conflict(client, fake):
    fake.force_conflict = True                     # the page moved between our GET and our PUT
    with pytest.raises(VersionConflict) as caught:
        client.update_page("101", markdown="mine")
    assert caught.value.status == 409 and "Read it again" in str(caught.value)


# ---- the credential -------------------------------------------------------------------------

def test_auth_header_shapes():
    basic = auth_headers({"auth_method": "basic", "email": EMAIL}, TOKEN)["Authorization"]
    assert basic == "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert auth_headers({"auth_method": "bearer"}, "pat-1")["Authorization"] == "Bearer pat-1"


def test_the_credential_is_sent_but_never_logged_or_in_an_error(client, fake, caplog):
    caplog.set_level(logging.DEBUG)
    client.read(page_id="101")
    assert fake.requests[0]["headers"]["Authorization"].startswith("Basic ")
    wrong = ConfluenceClient(CONFIG, lambda: "a-wrong-token-value")
    with pytest.raises(AuthFailed) as caught:
        wrong.read(page_id="101")
    blob = caplog.text + str(caught.value) + repr(client.conn) + repr(client) + str(client.config)
    for secret in (TOKEN, "a-wrong-token-value", base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()):
        assert secret not in blob


def test_missing_credential_says_connect_confluence_in_setup(fake):
    empty = ConfluenceClient(CONFIG, lambda: "")
    with pytest.raises(ConfluenceNotConnected) as caught:
        empty.search("x")
    assert "Connect Confluence in Setup › Connections" in str(caught.value) and fake.requests == []
    with pytest.raises(ConfluenceNotConnected):
        ConfluenceClient({"deployment": "cloud"}, lambda: TOKEN)       # no site at all


def test_an_invalid_credential_says_reconnect_in_setup(fake):
    with pytest.raises(AuthFailed) as caught:
        ConfluenceClient(CONFIG, lambda: "not-the-token").search("x")
    assert "Reconnect Confluence in Setup › Connections" in str(caught.value)
    assert caught.value.status == 401


def test_only_the_configured_atlassian_host_is_ever_called(client, fake, monkeypatch):
    client.search("x")
    client.read(page_id="101")
    assert {r["host"] for r in fake.requests} == {"acme.atlassian.net"}
    with pytest.raises(HostNotAllowed):
        client._http.request("GET", "https://evil.example.com/wiki/api/v2/pages")
    assert all(r["host"] == "acme.atlassian.net" for r in fake.requests)


def test_site_validation_and_the_data_center_seam():
    assert validate_site("https://acme.atlassian.net") == "https://acme.atlassian.net/wiki"
    assert validate_site("https://acme.atlassian.net/wiki/") == "https://acme.atlassian.net/wiki"
    for bad in ("http://acme.atlassian.net", "https://confluence.internal.example", "acme", ""):
        with pytest.raises(ConfluenceError):
            validate_site(bad)
    with pytest.raises(ConfluenceError, match="Data Center"):
        ConfluenceClient({**CONFIG, "deployment": "datacenter"}, lambda: TOKEN)


def test_an_operator_may_allow_another_host(monkeypatch):
    monkeypatch.setenv("BOSS_CREDENTIAL_HOSTS", "confluence=wiki.corp.example")
    assert validate_site("https://wiki.corp.example", "cloud").startswith("https://wiki.corp.example")


# ---- storage format <-> markdown ------------------------------------------------------------

def test_markdown_to_storage_escapes_and_keeps_safe_links():
    html = markdown_to_storage("<script>alert(1)</script> [ok](https://a.b/c) [bad](javascript:x) "
                               "**b** *i* `c<d`")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '<a href="https://a.b/c">ok</a>' in html and "javascript:" not in html
    assert "<strong>b</strong>" in html and "<em>i</em>" in html and "<code>c&lt;d</code>" in html


def test_a_page_survives_a_round_trip_through_storage_format():
    md = "# Title\n\nSome **bold** text with a [link](https://x.test).\n\n- one\n- two\n\n" \
         "```python\nprint('hi')\n```\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert storage_to_markdown(markdown_to_storage(md)) == md
