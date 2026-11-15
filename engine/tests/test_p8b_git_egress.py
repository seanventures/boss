"""P8b S11 (BOSSAPP-1072) without a database: the rules the host route adds, the Bitbucket REST
surface (D19), the guest's git configuration, and the names the three sides agree on.
The end-to-end cases live in test_p8b_git_egress_db.py and internal/gateway/p8b_git_test.go."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from boss_engine.identity import egress_rules as rules

ROOT = Path(__file__).resolve().parents[2]
PERMS = {"contents": "write", "pull_requests": "write"}
PATTERN = "refs/heads/boss/arun_1-*"


def test_a_branch_delete_is_refused_on_the_host_route_only():
    delete = [rules.RefUpdate("a" * 40, rules.ZERO_SHA, "refs/heads/boss/arun_1-x")]
    lane = rules.check_push(delete, {}, push_pattern=PATTERN, perms=PERMS, is_ancestor=lambda *_: False)
    assert lane.allowed                                   # the local lane keeps its own branch hygiene
    vm = rules.check_push(delete, {}, push_pattern=PATTERN, perms=PERMS,
                          is_ancestor=lambda *_: False, allow_delete=False)
    assert not vm.allowed and vm.reason == "branch_delete"
    other = [rules.RefUpdate("a" * 40, "b" * 40, "refs/heads/main")]
    assert rules.check_push(other, {}, push_pattern=PATTERN, perms=PERMS, is_ancestor=lambda *_: True,
                            allow_delete=False).reason == "ref_not_allowed"


@pytest.mark.parametrize("method,path,body,reason", [
    ("GET", "/2.0/repositories/ws/app", None, None),
    ("GET", "/2.0/repositories/ws/app/pullrequests/3", None, None),
    ("POST", "/2.0/repositories/ws/app/pullrequests",
     {"source": {"branch": {"name": "boss/arun_1-x"}}, "destination": {"branch": {"name": "main"}}}, None),
    ("POST", "/2.0/repositories/ws/app/pullrequests/3/comments", {"content": {"raw": "hi"}}, None),
    ("POST", "/2.0/repositories/ws/app/pullrequests", {"source": {"branch": {"name": "main"}}}, "pr_not_the_runs"),
    ("POST", "/2.0/repositories/ws/app/pullrequests/3/merge", {}, "not_in_rule_table"),
    ("DELETE", "/2.0/repositories/ws/app/refs/branches/boss/arun_1-x", None, "not_in_rule_table"),
    ("GET", "/2.0/repositories/ws/app/hooks", None, "forbidden_endpoint"),
    ("GET", "/2.0/repositories/ws/other", None, "repo_outside_run"),
    ("PUT", "/2.0/repositories/ws/app/hooks", {}, "forbidden_endpoint"),
    ("GET", "/2.0/user", None, "not_in_rule_table"),
])
def test_the_bitbucket_rest_surface(method, path, body, reason):
    decision = rules.check_bitbucket_api(method, path, perms=PERMS, repos=["ws/app"], body=body,
                                         push_pattern=PATTERN)
    assert decision.allowed is (reason is None), decision
    if reason:
        assert decision.reason == reason


def test_read_only_runs_cannot_write_bitbucket():
    decision = rules.check_bitbucket_api(
        "POST", "/2.0/repositories/ws/app/pullrequests", perms={"contents": "read"},
        repos=["ws/app"], body={"source": {"branch": {"name": "boss/arun_1-x"}}}, push_pattern=PATTERN)
    assert not decision.allowed and decision.reason == "insufficient_permission"


def test_the_host_route_names_match_across_the_three_sides():
    from apps.egress.boss_egress import proxy

    assert proxy.GIT_HOSTS == {"github.com": "github", "bitbucket.org": "bitbucket"}
    assert "gitlab.com" not in proxy.GIT_HOSTS and "gitlab.com" not in proxy.API_HOSTS
    agent = (ROOT / "sandbox-host" / "guest" / "in_guest_agent.py").read_text()
    assert agent == (ROOT / "services" / "sandbox-service" / "internal" / "backend" / "guest"
                     / "in_guest_agent.py").read_text(), "the two copies of the guest agent differ"
    assert 'RELAY_GIT_HOSTS = ("github.com", "bitbucket.org")' in agent
    routes = (ROOT / "services" / "sandbox-service" / "internal" / "gateway" / "routes.go").read_text()
    assert 'GitCredentialPath = "/_boss/git-credential"' in routes and "/_boss/git-credential" in agent
    for rule in ("git-smart-http", "git-rest-api"):
        line = next(x for x in routes.splitlines() if f'ID: "{rule}"' in x)
        assert "HostProof: true" in line and "AudEgress" in line and "Challenge: true" in line
    app = (ROOT / "apps" / "egress" / "boss_egress" / "app.py").read_text()
    assert 'Route("/git/{path:path}"' in app and 'Route("/api/{path:path}"' in app


def test_the_guest_git_config_holds_no_credential(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("no git")
    spec = importlib.util.spec_from_file_location("iga_p8b5", ROOT / "sandbox-host" / "guest" / "in_guest_agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    handle = "boss_egr_" + "h" * 43
    agent.configure_git_relay({"endpoints": {"git": "http://127.0.0.1:7102"}, "handles": {"egr": handle},
                               "run_id": "arun_1"})
    cfg = (tmp_path / ".gitconfig").read_text()
    assert handle not in cfg and "x-access-token" not in cfg and "token" not in cfg.lower().replace(
        "git-credential", "")
    assert handle not in "".join(os.environ.values())
    out = subprocess.run(["git", "config", "--global", "--get-regexp", r"^url\..*insteadof$"],
                         capture_output=True, text=True, env={**os.environ}).stdout
    assert "http://127.0.0.1:7102/git/github.com/.insteadof https://github.com/" in out
    assert "http://127.0.0.1:7102/git/bitbucket.org/.insteadof https://bitbucket.org/" in out
    assert "gitlab" not in out
    assert re.search(r"_boss/git-credential\?run=arun_1", cfg)
    # Configuring twice replaces, never stacks helpers.
    agent.configure_git_relay({"endpoints": {"git": "http://127.0.0.1:7102"}})
    helpers = subprocess.run(["git", "config", "--global", "--get-all", "credential.helper"],
                             capture_output=True, text=True).stdout.splitlines()
    assert helpers[0] == "" and len(helpers) == 2
    with pytest.raises(RuntimeError):
        agent.configure_git_relay({"endpoints": {"git": "http://evil.example:7102"}})
