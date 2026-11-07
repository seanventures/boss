"""Add a project (screen 03), ⌘K (screen 11) and Infra › server (screen 14), on a real database.

* Folder on host: a verified server's folder is READ (one read-only ssh session, faked here at
  the transport) and recorded as the project's source; a remote's credentials never come back;
  a bad path, an unknown server and a viewer are refused.
* ⌘K: "pr 3" finds ONE row for PR #3 (every retry carries the same PR), labelled with the short
  title, marked needs-you, with its GitHub link and hint; the captain and the live staging app.
* Infra › server: capacity from the server's own answer, the projects on it (with their
  Project › Deployments links), plugins from what it runs, recent changes.
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import HOST_ADDRESS, seed
from boss_engine.services.project_experience import host_folder, host_overview

PROBE_OUT = ("DIR\nGIT\nREMOTE https://someone:ghp_SECRETTOKEN@github.com/acme/ledger-api.git\n"
             "BRANCH main\nFILES 214\nTOP package.json\nTOP Makefile\nTOP Dockerfile\nTOP src\n")
CAP_OUT = ("BOSS_CAP cpus 8\nBOSS_CAP load 2.72\nBOSS_CAP mem 16384000 6389760\n"
           "BOSS_CAP disk 209715200 150994944\nBOSS_CAP os Ubuntu 22.04.4 LTS\n"
           "BOSS_IMG caddy:2\nBOSS_IMG prom/node-exporter:v1.8\nBOSS_IMG helpdesk-app\n")


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("np-owner")
    w.bind(w.owner, "owner")
    w.viewer = w.person("np-viewer")
    w.bind(w.viewer, "viewer")
    return w


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def helpdesk(world, client, engine):
    slug = f"helpdesk-np-{uuid.uuid4().hex[:6]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "HelpDesk AI Demo", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        story = seed(db, slug)
    return {"id": slug, **story}


@pytest.fixture
def fake_ssh(monkeypatch):
    calls: list[tuple[str, str]] = []

    def runner(settings):
        def run(host, command):
            calls.append((host["address"], command))
            if "BOSS_CAP" in command:
                return 0, CAP_OUT.replace("cpus 8", "cpus 4").replace("load 2.72", "load 1.0"), ""
            return 0, (PROBE_OUT if "/srv/apps/ledger-api" in command else "NODIR\n"), ""
        return run

    monkeypatch.setattr(host_folder, "_ssh_runner", runner)
    return calls


def _as(world, who):
    return world.acting_as(world.ctx(who))


# ── Folder on host ─────────────────────────────────────────────────────────────────────────

def test_the_probe_is_one_read_only_line_with_every_value_quoted():
    cmd = host_folder.command_for("/srv/my app")
    assert "'/srv/my app'" in cmd
    for verb in (" rm ", " mv ", " chmod", " tee ", "> /", " install"):
        assert verb not in cmd
    for bad in ("relative/path", "/srv/../etc", "/srv/x;rm -rf /", "/srv/$(id)", ""):
        with pytest.raises(host_folder.FolderError):
            host_folder.clean_path(bad)


def test_a_folder_is_read_and_its_remote_never_carries_a_credential():
    facts = host_folder.parse("/srv/apps/ledger-api", PROBE_OUT)
    assert facts["exists"] and facts["git"] and facts["branch"] == "main"
    assert facts["remote"] == "https://github.com/acme/ledger-api.git"
    assert facts["github"] == "acme/ledger-api"
    assert facts["files"] == 214 and facts["stack"] == ["Node", "Docker"]
    assert facts["build"] == "npm install · npm test"
    assert facts["suggested_name"] == "Ledger api"
    assert host_folder.parse("/nope", "NODIR\n") == {"path": "/nope", "exists": False}


def test_folder_on_host_lists_servers_reads_a_folder_and_records_it(world, client, helpdesk,
                                                                     fake_ssh, engine):
    pid, host = helpdesk["id"], helpdesk["host_id"]
    with _as(world, world.owner):
        hosts = client.get(f"/v1/projects/{pid}/host-folders/hosts")
        assert hosts.status_code == 200, hosts.text
        assert [h["id"] for h in hosts.json()["hosts"]] == [host]
        read = client.post(f"/v1/projects/{pid}/host-folders/probe",
                           json={"host_id": host, "path": "/srv/apps/ledger-api/"})
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["host"] == "demo-host" and body["github"] == "acme/ledger-api"
        assert "SECRETTOKEN" not in json.dumps(body)
        missing = client.post(f"/v1/projects/{pid}/host-folders/probe",
                              json={"host_id": host, "path": "/srv/apps/none"})
        assert missing.json()["exists"] is False
        refused = client.post(f"/v1/projects/{pid}/host-folders",
                              json={"host_id": host, "path": "/srv/apps/none"})
        assert refused.status_code == 422 and "no folder" in refused.json()["message"].lower()
        bad = client.post(f"/v1/projects/{pid}/host-folders/probe",
                          json={"host_id": host, "path": "/srv/x;id"})
        assert bad.status_code == 422
        unknown = client.post(f"/v1/projects/{pid}/host-folders/probe",
                              json={"host_id": "dhost_nope", "path": "/srv"})
        assert unknown.status_code == 404
        saved = client.post(f"/v1/projects/{pid}/host-folders",
                            json={"host_id": host, "path": "/srv/apps/ledger-api"})
        assert saved.status_code == 200, saved.text
        again = client.post(f"/v1/projects/{pid}/host-folders",
                            json={"host_id": host, "path": "/srv/apps/ledger-api"})
        assert again.json()["id"] == saved.json()["id"]
        listed = client.get(f"/v1/projects/{pid}/host-folders").json()["folders"]
    assert [(f["path"], f["host"], f["facts"]["github"]) for f in listed] == [
        ("/srv/apps/ledger-api", "demo-host", "acme/ledger-api")]
    assert "SECRETTOKEN" not in json.dumps(listed)
    # Only read-only commands went over the wire, one session per read.
    assert all(addr == HOST_ADDRESS for addr, _ in fake_ssh)
    with engine.connect() as c:
        events = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                "event_type = 'project.host_folder_added'"), {"p": pid}).scalar()
    assert events == 2


def test_a_server_boss_cannot_reach_is_said_in_words(world, client, helpdesk, monkeypatch):
    def runner(settings):
        def run(host, command):
            raise RuntimeError("No SSH key configured: set BOSS_SSH_HOST_PRIVATE_KEY_FILE")
        return run

    monkeypatch.setattr(host_folder, "_ssh_runner", runner)
    with _as(world, world.owner):
        r = client.post(f"/v1/projects/{helpdesk['id']}/host-folders/probe",
                        json={"host_id": helpdesk["host_id"], "path": "/srv/apps/ledger-api"})
        cap = client.post(f"/v1/infra/hosts/{helpdesk['host_id']}/capacity",
                          json={"project_id": helpdesk["id"]})
    assert r.status_code == 502 and r.json()["message"].startswith("demo-host did not answer: No SSH key")
    assert cap.status_code == 502


def test_a_viewer_cannot_read_a_server_folder(world, client, helpdesk, fake_ssh):
    with _as(world, world.viewer):
        r = client.post(f"/v1/projects/{helpdesk['id']}/host-folders/probe",
                        json={"host_id": helpdesk["host_id"], "path": "/srv/apps/ledger-api"})
    assert r.status_code == 403
    assert not fake_ssh


# ── ⌘K ─────────────────────────────────────────────────────────────────────────────────────

def test_jump_finds_pr_3_once_with_its_short_title_and_what_it_waits_for(world, client,
                                                                          helpdesk):
    pid = helpdesk["id"]
    with _as(world, world.owner):
        items = client.get("/v1/jump", params={"q": "pr 3", "project_id": pid}).json()["items"]
    prs = [i for i in items if i["kind"] == "pr"]
    assert len(prs) == 1
    pr = prs[0]
    assert pr["label"] == "PR #3 · Finish the visual refresh"   # the brief, as a short title
    assert pr["needs_you"] is True and pr["hint"] == "↵ drawer · ⌘↵ GitHub"
    assert pr["external_url"].endswith("/pull/3")
    assert any(i["kind"] == "build" and i["label"] == "Build · Finish the visual refresh"
               and i["hint"] == "Code Tasks" for i in items)
    assert any(i["kind"] == "run" and "visual refresh" in i["label"].lower()
               and i["label"].startswith("Run · ") and i["hint"] == "Workflows" for i in items)
    for spelled in ("PR #3", "#3", "3", "pr3"):
        with _as(world, world.owner):
            again = client.get("/v1/jump", params={"q": spelled, "project_id": pid}).json()["items"]
        assert [i["label"] for i in again if i["kind"] == "pr"] == ["PR #3 · Finish the visual refresh"], spelled
    with _as(world, world.owner):
        deploys = client.get("/v1/jump", params={"q": "deploy", "project_id": pid}).json()["items"]
    assert [i["label"] for i in deploys if i["kind"] == "deploy"] == [f"Staging · {HOST_ADDRESS}:8004"]
    staging = next(i for i in items if i["kind"] == "deploy")
    assert staging["label"] == f"Staging · {HOST_ADDRESS}:8004" and staging["hint"] == "open ↗"
    assert staging["external_url"].startswith("http://")
    captain = next(i for i in items if i["kind"] == "captain")
    assert captain["label"] == "Helm" and captain["href"].endswith("drawer=captain")


def test_jump_with_no_query_opens_on_the_projects_newest_things(world, client, helpdesk):
    with _as(world, world.owner):
        items = client.get("/v1/jump", params={"project_id": helpdesk["id"]}).json()["items"]
    kinds = [i["kind"] for i in items]
    assert "run" in kinds and "deploy" in kinds and "project" in kinds
    assert kinds.index("run") < kinds.index("project")


# ── Infra › server ────────────────────────────────────────────────────────────────────────

def test_the_server_page_reads_capacity_projects_plugins_and_changes(world, client, helpdesk,
                                                                     fake_ssh, engine):
    pid, host = helpdesk["id"], helpdesk["host_id"]
    with _as(world, world.owner):
        before = client.get(f"/v1/infra/hosts/{host}/overview", params={"project_id": pid})
        assert before.status_code == 200, before.text
        # What the server said at its last preflight (the seed's), never a made-up number…
        assert before.json()["capacity"]["cpu"] == {"pct": 34, "label": "8 vCPU"}
        read = client.post(f"/v1/infra/hosts/{host}/capacity", json={"project_id": pid})
        assert read.status_code == 200, read.text
        o = client.get(f"/v1/infra/hosts/{host}/overview", params={"project_id": pid}).json()
    cap = o["capacity"]
    # …and a read now replaces it with what the server says now.
    assert cap["cpu"] == {"pct": 25, "label": "4 vCPU"}
    assert cap["memory"]["pct"] == 61 and cap["memory"]["label"] == "16 GB"
    assert cap["disk"]["pct"] == 72 and cap["disk"]["label"] == "200 GB"
    assert cap["ports"]["used"] == 1
    assert o["sub"].endswith("Ubuntu 22.04.4 LTS · shared by 1 project")
    [row] = o["projects"]
    assert row["id"] == pid and row["initials"] == "HD" and row["env"] == "staging"
    assert row["port"] == 8004 and row["health"]["word"]
    assert row["href"] == f"?project_id={pid}&area=project&tab=deployments"
    assert {p["label"] for p in o["plugins"]} >= {"Caddy TLS", "node-exporter"}
    assert o["changes"] and o["changes"][0]["label"] == "HelpDesk staging deploy"


def test_capacity_rides_on_every_deploy_preflight():
    from boss_engine.services.stack_deploy import host_facts_from

    facts = host_facts_from("BOSS_SSH_OK\nDOCKER_PRESENT\n" + CAP_OUT)
    assert facts["capacity"]["cpus"] == 8 and facts["capacity"]["disk_pct"] == 72
    assert host_overview.plugins({"docker": True, "versions": {"docker": "Docker version 27.3.1"},
                                  "capacity": facts["capacity"]})[0]["label"] == "Docker 27"
