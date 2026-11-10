"""A LOCAL PROJECT's stack deploy reads its code from the bare repository, never from GitHub.

Live (BOSSAPP-946 follow-up, helpdesk-laptop, wfr_e9d48d68c8504304a6e5): the merged commit lived
in local_helpdesk-laptop/helpdesk-laptop, and the deploy step failed three times with "GitHub has
no ref '47e8687…' in local_helpdesk-laptop/helpdesk-laptop" — the stack deploy's source was
GitHub's whatever the project's host. Now `stack_deploy.source_for` is the ONE source every
deploy, preflight and smoke check reads through: a local repository answers from its bare
repository, a GitHub one from GitHub with its local mirror behind it during an outage.

The full path runs here — plan (the manifest read at the commit), the approved decision, the
worker's leased apply (the tree packed and uploaded), health — with GitHub switched OFF and every
call to it counted. The count must stay zero. The host is the fake the stack-deploy suite uses.
"""
from __future__ import annotations

import gzip
import io
import tarfile
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (
    HOME,
    MANIFEST,
    FakeHost,
    _approve_and_execute,
)
from boss_engine.services import local_repo, stack_deploy


@pytest.fixture
def github_off(monkeypatch):
    """Every HTTP request to GitHub is refused and recorded."""
    calls: list[str] = []
    real = httpx.Client.send

    def send(self, request, *args, **kwargs):
        if "github" in str(request.url.host):
            calls.append(f"{request.method} {request.url}")
            raise httpx.ConnectError("GitHub is off in this test", request=request)
        return real(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    from boss_engine.services import github_app_adapter

    def no_app(*_a, **_k):
        calls.append("github app token")
        raise github_app_adapter.GitHubAppConfigurationError("GitHub is off in this test")

    monkeypatch.setattr(github_app_adapter.GitHubAppAdapter, "installation_token", no_app)
    return calls


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setattr(local_repo, "root", lambda settings=None: root)
    return root


@pytest.fixture
def local_stack(client, engine, monkeypatch, repo_root, github_off):
    owner, repo = local_repo.owner_for(HOME), f"shop{uuid4().hex[:6]}"
    bare = local_repo.init_bare(local_repo.bare_path(owner, repo))
    first = local_repo.commit_files(bare, "main", {
        "boss.deploy.yaml": MANIFEST, "install.sh": "#!/bin/sh\necho installed\n",
        "upgrade.sh": "#!/bin/sh\necho upgraded\n", "app.txt": "v1\n"}, "first")
    local_repo.ensure_branch(bare, "boss/code/crun_x", "main")
    merged = local_repo.commit_files(bare, "boss/code/crun_x", {"app.txt": "v2\n"}, "the change")
    local_repo.merge(bare, "boss/code/crun_x", "main", "Merge the change")

    journal: list[str] = []
    host = FakeHost(journal)
    monkeypatch.setattr(
        stack_deploy, "host_access_for",
        lambda connection, settings, row, adapter=None: stack_deploy.HostAccess(
            host=row["address"], user=row.get("ssh_user") or "root", port=22,
            key_material="KEYMATERIAL", adapter=host))
    monkeypatch.setattr(api_main.settings, "boss_edition", "community")

    created = client.post("/v1/deployment-hosts", json={
        "project_id": HOME, "name": f"vm-{uuid4().hex[:8]}", "address": "203.0.113.41",
        "ssh_user": "deploy", "actor": "ceo"})
    assert created.status_code == 200, created.text
    host_id = created.json()["id"]
    key_ref = f"sref_{uuid4().hex[:10]}"
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                 "ref_key, secret_kind, storage_backend, created_by) VALUES "
                 "(:i, :p, 'deployment_host', :h, 'database', :k, 'private_key', 'database', "
                 "'test')"), {"i": key_ref, "p": HOME, "h": host_id, "k": f"K_{key_ref}"})
        connection.execute(text("UPDATE deployment_hosts SET status = 'verified', "
                                "key_secret_ref = :r WHERE id = :i"),
                           {"i": host_id, "r": key_ref})
    target = client.post("/v1/deployments/stack/targets", json={
        "project_id": HOME, "host_id": host_id, "repository": f"{owner}/{repo}",
        "app_name": f"shop-{uuid4().hex[:6]}", "public_url": "https://shop.example.test",
        "actor": "ceo"})
    assert target.status_code == 201, target.text
    return {"host": host, "journal": journal, "target": target.json(), "bare": bare,
            "owner": owner, "repo": repo, "first": first, "merged": merged,
            "github": github_off}


def test_a_local_project_plans_applies_and_is_healthy_with_zero_github_calls(
        client, engine, local_stack, monkeypatch):
    uploaded: dict[str, bytes] = {}
    real_write = stack_deploy.StackApplier._write

    def keep_tree(self, step, command, **kwargs):
        if kwargs.get("stdin_path") and "BOSS_UPLOADED" in command:
            with open(kwargs["stdin_path"], "rb") as handle:
                data = handle.read()
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data)), mode="r:") as archive:
                uploaded.update({m.name.split("/", 1)[1]: archive.extractfile(m).read()
                                 for m in archive.getmembers() if m.isfile()})
        return real_write(self, step, command, **kwargs)

    monkeypatch.setattr(stack_deploy.StackApplier, "_write", keep_tree)

    # The merged commit itself — the ref the live run's deploy step was given.
    response = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": local_stack["target"]["id"],
        "ref": local_stack["merged"], "actor": "ceo"})
    assert response.status_code == 200, response.text
    deployment = response.json()["deployment"]
    assert deployment["commit"] == local_stack["merged"]
    assert local_repo.branch_sha(local_stack["bare"], "main") == local_stack["merged"]
    assert deployment["manifest"]["env_keys"] == ["PUBLIC_URL", "DB_PASSWORD"]

    result, _ = _approve_and_execute(client, engine, deployment)
    assert result["status"] == "healthy", result
    assert [s["status"] for s in result["deployment"]["steps"]].count("failed") == 0
    # The tree that went to the host is the merged commit's, from the bare repository.
    assert uploaded.get("app.txt") == b"v2\n" and "boss.deploy.yaml" in uploaded
    assert local_stack["github"] == [], f"the deploy called GitHub: {local_stack['github']}"
    # The one source every caller uses answers the same commit, from the bare repository.
    with engine.connect() as connection:
        source = stack_deploy.source_for(connection, api_main.settings, HOME,
                                         local_stack["owner"], local_stack["repo"])
    assert source.resolve_commit(local_stack["owner"], local_stack["repo"],
                                 local_stack["merged"]) == local_stack["merged"]
    assert local_stack["github"] == []


def test_a_local_project_names_a_missing_ref_in_its_own_words(client, local_stack):
    response = client.post("/v1/deployments/stack/plan", json={
        "project_id": HOME, "target_id": local_stack["target"]["id"], "ref": "f" * 40,
        "actor": "ceo"})
    assert response.status_code in (404, 409, 422), response.text
    said = response.text
    assert "GitHub" not in said and "local repository" in said
    assert local_stack["github"] == []


def test_every_deploy_caller_reads_through_the_one_source():
    """No deploy, preflight or smoke caller builds a GitHub reader of its own."""
    from pathlib import Path

    import boss_engine

    services = Path(boss_engine.__file__).parent / "services"
    for path in services.rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        if path.name == "stack_deploy.py":
            continue
        assert "GitHubSource(" not in body and "github_source(" not in body, \
            f"{path.name} reads code past stack_deploy.source_for"


def test_source_for_picks_local_for_a_local_owner_and_github_with_its_mirror_otherwise(
        engine, repo_root):
    settings = api_main.settings
    with engine.connect() as connection:
        local = stack_deploy.source_for(connection, settings, HOME, "local_p1", "app")
        hub = stack_deploy.source_for(connection, settings, HOME, "acme", "shop")
    assert isinstance(local, stack_deploy.LocalSource)
    assert isinstance(hub, stack_deploy.MirroredSource)


class _DownGitHub:
    def __init__(self, status: int = 0):
        self.status = status

    def _fail(self, *a):
        if self.status:
            raise stack_deploy.StackDeployError(f"GitHub answered HTTP {self.status}.", "x",
                                                outage=self.status >= 500)
        raise stack_deploy.StackDeployError("BOSS could not reach GitHub (ConnectError).", "x",
                                            outage=True)

    resolve_commit = read_file = download_tarball = compare = _fail


def test_a_github_outage_reads_the_mirror_and_a_4xx_never_does(repo_root):
    mirror = local_repo.init_bare(local_repo.mirror_path("acme", "shop"))
    sha = local_repo.commit_files(mirror, "main", {"boss.deploy.yaml": MANIFEST}, "first")

    down = stack_deploy.MirroredSource(_DownGitHub(), owner="acme", repo="shop",
                                       settings=api_main.settings)
    assert down.resolve_commit("acme", "shop", "main") == sha
    assert down.read_file("acme", "shop", "boss.deploy.yaml", sha) == MANIFEST
    assert down.used_mirror

    refused = stack_deploy.MirroredSource(_DownGitHub(404), owner="acme", repo="shop",
                                          settings=api_main.settings)
    with pytest.raises(stack_deploy.StackDeployError, match="HTTP 404"):
        refused.resolve_commit("acme", "shop", "main")
    # A mirror without the asked ref re-raises GitHub's own words.
    with pytest.raises(stack_deploy.StackDeployError, match="could not reach GitHub"):
        down.resolve_commit("acme", "shop", "no-such-branch")
