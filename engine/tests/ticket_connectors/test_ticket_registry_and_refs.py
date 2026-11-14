"""The ticket-connector registry, manifests, ref parsing, the host guard and the settings the
contract reads — the parts every connector shares."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from boss_engine.connectors.tickets import http as ticket_http
from boss_engine.connectors.tickets.base import TicketAdapterBase
from boss_engine.connectors.tickets.contract import (
    CAPABILITIES,
    CapabilityNotSupported,
    ConnectorManifestError,
    HostNotAllowed,
    TicketConnection,
    TicketRef,
    connection_settings,
)
from boss_engine.connectors.tickets.refs import find_keys
from boss_engine.connectors.tickets.registry import (
    REGISTRY,
    ROOT,
    check_adapter,
    load_folder,
    validate,
)
from boss_engine.connectors.tickets.text import acceptance_criteria, adf_to_markdown, \
    markdown_to_adf
from boss_engine.services import env_refs
from boss_engine.services.work_tracker_adapters import (
    TRACKER_ADAPTERS,
    AzureDevOpsWorkTrackerAdapter,
    GitHubIssuesWorkTrackerAdapter,
    JiraWorkTrackerAdapter,
    LinearWorkTrackerAdapter,
    PlaneWorkTrackerAdapter,
    get_adapter,
    list_catalog,
)

JIRA_MANIFEST = yaml.safe_load((ROOT / "jira" / "manifest.yaml").read_text())


# ---- registry --------------------------------------------------------------------------------


def test_every_shipped_folder_loads_and_keeps_the_legacy_classes():
    legacy = {"plane": PlaneWorkTrackerAdapter, "jira": JiraWorkTrackerAdapter,
              "linear": LinearWorkTrackerAdapter, "github_issues": GitHubIssuesWorkTrackerAdapter,
              "azure_devops": AzureDevOpsWorkTrackerAdapter}
    for key, cls in legacy.items():
        adapter = get_adapter(key)
        assert isinstance(adapter, cls) and isinstance(adapter, TicketAdapterBase)
        assert TRACKER_ADAPTERS[key] is adapter
    assert [c["provider_key"] for c in list_catalog()][:5] == \
        ["github_issues", "linear", "jira", "plane", "azure_devops"]
    jira = next(c for c in list_catalog() if c["provider_key"] == "jira")
    assert jira["ticket_capabilities"]["transition"] is True
    assert jira["auth_kinds"] == ["api_token", "oauth2"]


def test_every_shipped_manifest_folder_is_a_registered_connector():
    folders = {p.name for p in ROOT.iterdir() if (p / "manifest.yaml").is_file()}
    assert folders <= set(REGISTRY.adapters)


def test_declared_false_capabilities_refuse_on_the_wrapped_connectors():
    conn = TicketConnection("linear", {"api_token": "x" * 20})
    for key in ("linear", "azure_devops"):
        adapter = get_adapter(key)
        assert adapter.ticket_capabilities["create"] is True
        for capability in ("read", "browse", "write_comment", "transition", "link", "webhook"):
            assert adapter.ticket_capabilities[capability] is False
        with pytest.raises(CapabilityNotSupported):
            adapter.get_ticket(conn, TicketRef(key, key="1"))
        with pytest.raises(CapabilityNotSupported):
            adapter.transition(conn, TicketRef(key, key="1"), "done")
        assert adapter.parse_ref("ENG-1 https://linear.app/x/issue/ENG-1", conn=conn) == []
    github = get_adapter("github_issues")
    assert github.can("read") and github.can("write_comment") and not github.can("browse")
    with pytest.raises(CapabilityNotSupported):
        github.list_projects(TicketConnection("github_issues", {}))


def test_manifest_hosts_are_registered_with_the_outbound_guard():
    assert "auth.atlassian.com" in env_refs.PROVIDER_HOSTS["jira"]
    assert "*.atlassian.net" in env_refs.PROVIDER_HOSTS["jira"]
    assert "PLANE_BASE_URL" in env_refs.OPERATOR_BASE_URLS["plane"]
    env_refs.register_provider_hosts("servicenow", ["*.service-now.com"],
                                     operator_vars=["SERVICENOW_BASE_URL"])
    assert env_refs.PROVIDER_HOSTS["servicenow"] == ("*.service-now.com",)
    before = env_refs.PROVIDER_HOSTS["github"]
    env_refs.register_provider_hosts("github_issues", ["api.github.com"])   # alias, no dupes
    assert env_refs.PROVIDER_HOSTS["github"] == before


@pytest.mark.parametrize("mutate, says", [
    (lambda m: m.pop("icon"), "missing 'icon'"),
    (lambda m: m.update(colour="red"), "unknown field 'colour'"),
    (lambda m: m["capabilities"].update(read="yes"), "capabilities.read"),
    (lambda m: m["capabilities"].update(teleport=True), "unknown capability"),
    (lambda m: m.update(default_hosts=["not a host!"]), "is not a host name"),
    (lambda m: m.update(base_url_field="nope"), "base_url_field 'nope'"),
    (lambda m: m.update(status_mapping_defaults={"shipped": "Done"}), "unknown category"),
    (lambda m: m.update(webhook={"scheme": "md5"}), "webhook.scheme"),
    (lambda m: m["auth_kinds"][1].update(token_url="https://evil.example/token"),
     "not in default_hosts"),
    (lambda m: m["auth_kinds"][1].update(pkce="no"), "pkce must be true/false"),
    (lambda m: m["auth_kinds"][0].update(fields=["password"]), "fields must name auth_fields"),
    (lambda m: m["auth_fields"].append({"name": "email", "label": "again"}), "names repeat"),
])
def test_manifest_schema_refuses(mutate, says):
    manifest = copy.deepcopy(JIRA_MANIFEST)
    mutate(manifest)
    with pytest.raises(ConnectorManifestError) as raised:
        validate(manifest, where="jira")
    assert says in str(raised.value)


def test_the_shipped_jira_manifest_is_valid():
    assert validate(copy.deepcopy(JIRA_MANIFEST)).capabilities["webhook"] is True


class _Half(TicketAdapterBase):
    provider_key = "half"

    def fetch_ticket(self, conn, ref):  # read is two hooks: this one alone is not "read"
        return None


class _Reader(_Half):
    def ref_from_url(self, url, conn=None):
        return None


def _manifest(**caps):
    raw = {"key": "half", "label": "Half", "icon": "x", "description": "d", "adapter": "_Half",
           "auth_fields": [{"name": "api_token", "label": "Token", "secret": True}],
           "capabilities": {c: False for c in CAPABILITIES} | caps,
           "default_hosts": ["half.example"]}
    return validate(raw, where="half")


def test_an_adapter_must_implement_exactly_what_its_manifest_claims():
    with pytest.raises(ConnectorManifestError, match="claims 'read'"):
        check_adapter(_manifest(read=True), _Half)
    with pytest.raises(ConnectorManifestError, match="implements part of 'read'"):
        check_adapter(_manifest(), _Half)
    with pytest.raises(ConnectorManifestError, match="implements 'read' but the manifest"):
        check_adapter(_manifest(), _Reader)
    check_adapter(_manifest(read=True), _Reader)
    with pytest.raises(ConnectorManifestError, match="must extend TicketAdapterBase"):
        check_adapter(_manifest(), object)


def test_a_folder_whose_key_is_not_its_name_is_refused(tmp_path):
    src = Path(__file__).resolve().parent / "fake_connector"
    folder = tmp_path / "renamed"
    shutil.copytree(src, folder)
    with pytest.raises(ConnectorManifestError, match="must match its folder"):
        load_folder(folder)


def test_legacy_fields_and_manifest_auth_fields_must_agree(tmp_path, monkeypatch):
    folder = tmp_path / "jira"
    shutil.copytree(ROOT / "jira", folder)
    manifest = yaml.safe_load((folder / "manifest.yaml").read_text())
    manifest["auth_fields"] = [f for f in manifest["auth_fields"] if f["name"] != "issue_type"]
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (folder / "adapter.py").write_text(
        "from boss_engine.connectors.tickets.jira.adapter import JiraTicketAdapter\n")
    with pytest.raises(ConnectorManifestError, match="disagree"):
        load_folder(folder)


# ---- ref parsing -------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "UTF-8", "SHA-256", "ISO-8601", "RFC-7231", "X-1", "AES-128 and TLS-13", "COVID-19",
    "CVE-2024-1234", "kan-2", "KAN-", "KAN-0", "KAN-007", "pre-KAN-2", "v1.KAN-2", "#KAN-2x",
    "path/KAN-2", "KAN-2abc", "KAN_2", "OPS-12", "https://evil.example/browse/KAN-2",
])
def test_keys_are_only_known_projects(text):
    assert find_keys(text, {"KAN"}) == []


def test_keys_found_once_in_order_and_never_without_known_projects():
    assert find_keys("KAN-2, (KAN-14) and KAN-2; ops: OPS-3.", {"KAN", "OPS"}) == \
        [("KAN-2", "KAN"), ("KAN-14", "KAN"), ("OPS-3", "OPS")]
    assert find_keys("KAN-2", set()) == []
    assert find_keys("KAN-2", None) == []


def test_jira_parse_ref_urls_and_keys():
    jira = get_adapter("jira")
    conn = TicketConnection("jira", {"base_url": "https://acme.atlassian.net",
                                     "project_key": "KAN"})
    found = jira.parse_ref(
        "See https://acme.atlassian.net/browse/KAN-7 and "
        "https://acme.atlassian.net/jira/software/projects/KAN/boards/2?selectedIssue=KAN-9 "
        "and KAN-7 and SHA-256 and https://other.atlassian.net/browse/KAN-8", conn=conn)
    assert [r.key for r in found] == ["KAN-7", "KAN-9"]
    assert found[0].url == "https://acme.atlassian.net/browse/KAN-7"
    # with no connection: only Atlassian Cloud URLs, never bare keys
    assert [r.key for r in jira.parse_ref("KAN-7 https://x.atlassian.net/browse/KAN-1")] == \
        ["KAN-1"]


def test_plane_and_github_urls():
    plane = get_adapter("plane")
    conn = TicketConnection("plane", {"api_base_url": "https://plane.local.sean.network"})
    pid, iid = "3f2a9c1e-0000-4000-8000-000000000001", "3f2a9c1e-0000-4000-8000-000000000002"
    found = plane.parse_ref(f"https://plane.local.sean.network/acme/projects/{pid}/issues/{iid} "
                            "https://plane.local.sean.network/acme/browse/BOSSAPP-12/ "
                            f"https://evil.example/acme/projects/{pid}/issues/{iid}", conn=conn)
    assert [(r.id, r.key) for r in found] == [(iid, None), (None, "BOSSAPP-12")]
    github = get_adapter("github_issues")
    gconn = TicketConnection("github_issues", {"owner": "acme", "repo": "shop"})
    refs = github.parse_ref("https://github.com/acme/shop/issues/12 acme/shop#4 other/repo#5 #6",
                            conn=gconn)
    assert [r.key for r in refs] == ["acme/shop#12", "acme/shop#4"]


# ---- the host guard ----------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.urls = []

    def __call__(self, method, url, headers, data, timeout, context):
        self.urls.append(url)
        return ticket_http.Response(200, {}, b'{"id": "1", "key": "X-1", "fields": {}}')


def test_the_operator_base_url_is_allowed_and_others_are_not(monkeypatch):
    """A self-hosted Plane (https://plane.local.sean.network) is reachable when it is the
    connection's base URL, or the operator's PLANE_BASE_URL — and nothing else is."""
    recorder = _Recorder()
    monkeypatch.setattr(ticket_http, "TRANSPORT", recorder)
    plane = get_adapter("plane")
    conn = TicketConnection("plane", {"api_base_url": "https://plane.local.sean.network",
                                      "workspace_slug": "sean", "api_token": "p" * 24})
    plane.session(conn).request("GET", "/api/v1/workspaces/sean/projects/")
    assert recorder.urls == ["https://plane.local.sean.network/api/v1/workspaces/sean/projects/"]
    with pytest.raises(HostNotAllowed):
        plane.session(conn).request("GET", "https://evil.example/api/v1/")
    # an operator-set host (env) is allowed even with no base URL on the connection
    monkeypatch.setenv("PLANE_BASE_URL", "https://plane.ops.example")
    bare = TicketConnection("plane", {"api_token": "p" * 24})
    plane.session(bare).request("GET", "https://plane.ops.example/api/v1/x/")
    with pytest.raises(HostNotAllowed):
        plane.session(bare).request("GET", "https://plane.local.sean.network/api/v1/x/")
    # official hosts need https
    with pytest.raises(HostNotAllowed):
        plane.session(bare).request("GET", "http://api.plane.so/api/v1/x/")
    assert len(recorder.urls) == 2


def test_a_host_credential_stays_bound_to_its_provider(monkeypatch):
    """PLANE_API_KEY from the host goes to the operator's Plane, never to a base URL a project
    typed (the env_refs rule, through the ticket helper)."""
    from boss_engine.services.env_refs import CredentialMisdirected

    monkeypatch.setattr(ticket_http, "TRANSPORT", _Recorder())
    monkeypatch.setenv("PLANE_API_KEY", "host-plane-key-0123456789")
    monkeypatch.setenv("PLANE_BASE_URL", "https://plane.local.sean.network")
    plane = get_adapter("plane")
    ok = TicketConnection("plane", {"api_base_url": "https://plane.local.sean.network",
                                    "credential_ref": "env:PLANE_API_KEY"})
    plane.session(ok).request("GET", "/api/v1/workspaces/x/projects/")
    bad = TicketConnection("plane", {"api_base_url": "https://evil.example",
                                     "credential_ref": "env:PLANE_API_KEY"})
    with pytest.raises(CredentialMisdirected) as raised:
        plane.session(bad).request("GET", "/api/v1/workspaces/x/projects/")
    assert "host-plane-key" not in str(raised.value)


# ---- settings and text -------------------------------------------------------------------------


def test_connection_settings_defaults_and_typos():
    settings = connection_settings({}, {"review": "In Review"})
    assert settings["auto_start"] is False and settings["pickup_label"] == "boss"
    assert settings["status_mapping"] == {"review": "In Review"}
    typo = connection_settings({"settings": {"auto_start": "yes", "poll_interval_s": 5,
                                             "status_mapping": {"shipped": "Done", "done": "Shipped"},
                                             "comments_limit": 1000}})
    assert typo["auto_start"] is False                  # only a real true turns it on
    assert typo["poll_interval_s"] == 60                # floor
    assert typo["status_mapping"] == {"done": "Shipped"}
    assert typo["comments_limit"] == 10
    assert connection_settings({"settings": {"auto_start": True}})["auto_start"] is True


def test_adf_round_trip_and_acceptance_criteria():
    md = "Intro with a [link](https://x.example) and **bold**\n\n- one\n- two\n\n```py\nx = 1\n```"
    back = adf_to_markdown(markdown_to_adf(md))
    assert "[link](https://x.example)" in back and "**bold**" in back
    assert "- one\n- two" in back and "```py\nx = 1\n```" in back
    assert adf_to_markdown({"type": "doc", "content": [{"type": "taskList", "content": [
        {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [
            {"type": "text", "text": "shipped"}]}]}]}) == "- [x] shipped"
    assert acceptance_criteria("**Acceptance Criteria:**\n1. a\n2. b\n\n**Notes**\n- c") == \
        ["a", "b"]
    assert acceptance_criteria("Given a cart\nWhen I pay\nThen it works") == \
        ["Given a cart", "When I pay", "Then it works"]
    assert acceptance_criteria("nothing here\n- a bullet") == []
    assert acceptance_criteria("", ["from", "field"]) == ["from", "field"]
