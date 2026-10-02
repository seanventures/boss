"""The outside world's fakes behave like the services BOSS calls, and keep what they were sent.

The incidents: every example's last step reaches an external service — mail, a tracker, YouTube —
and on boss-1 none was configured, so each would fail only after a human approved it. A build's
PR was "opened" while the next clone found none of the work ("this app is not in main"). And a
scripted run that quietly reached a real provider would be a green result that spent money. So:
mail lands in the sink, a PR written through the Git Data API is what the next clone sees, and a
host with no fake is refused and logged.
"""
from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

import fakes

STUB_SSH = Path(fakes.__file__).resolve().parent / "bin" / "ssh"


@pytest.fixture()
def world(tmp_path):
    http, smtp, state = fakes.make_servers(0, 0, tmp_path / "fakes")
    for server in (http, smtp):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{http.server_address[1]}", "smtp": smtp.server_address[1],
           "ca": str(tmp_path / "fakes" / "tls" / "ca.pem"), "state": state, "tmp": tmp_path}
    http.shutdown()
    smtp.shutdown()


def _via_proxy(world, method: str, url: str, body=None):
    context = ssl.create_default_context(cafile=world["ca"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": world["url"], "http": world["url"]}),
        urllib.request.HTTPSHandler(context=context))
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _git_env(world) -> dict[str, str]:
    return {**os.environ, "HTTPS_PROXY": world["url"], "https_proxy": world["url"],
            "GIT_SSL_CAINFO": world["ca"], "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(world["tmp"]), "GIT_TERMINAL_PROMPT": "0"}


def test_mail_sent_over_smtp_lands_in_the_sink(world):
    with smtplib.SMTP("127.0.0.1", world["smtp"], timeout=10) as client:
        client.sendmail("boss@x.test", ["reader@x.test"],
                        "Subject: Your weekly newsletter\n\nThree stories this week.")
    mail = world["state"].mail
    assert mail[-1]["to"] == ["reader@x.test"]
    assert mail[-1]["subject"] == "Your weekly newsletter"
    assert "Three stories" in mail[-1]["body"]


def test_a_pr_written_through_the_git_data_api_is_what_the_next_clone_sees(world, tmp_path):
    api = "https://api.github.com/repos/e2e-org/e2e-app"
    status, ref = _via_proxy(world, "GET", f"{api}/git/ref/heads/main")
    assert status == 200
    base = ref["object"]["sha"]
    _, commit = _via_proxy(world, "GET", f"{api}/git/commits/{base}")
    _, blob = _via_proxy(world, "POST", f"{api}/git/blobs", {
        "content": base64.b64encode(b"def answer():\n    return 42\n").decode(),
        "encoding": "base64"})
    _, tree = _via_proxy(world, "POST", f"{api}/git/trees", {
        "base_tree": commit["tree"]["sha"],
        "tree": [{"path": "e2e_feature.py", "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
    _, new = _via_proxy(world, "POST", f"{api}/git/commits", {
        "message": "add e2e_feature", "tree": tree["sha"], "parents": [base]})
    assert _via_proxy(world, "POST", f"{api}/git/refs", {
        "ref": "refs/heads/boss/e2e", "sha": new["sha"]})[0] == 201
    status, pr = _via_proxy(world, "POST", f"{api}/pulls", {
        "title": "e2e", "head": "boss/e2e", "base": "main", "body": "b"})
    assert status == 201 and pr["html_url"].endswith("/pull/1")
    status, files = _via_proxy(world, "GET", f"{api}/pulls/1/files")
    assert [f["filename"] for f in files] == ["e2e_feature.py"]
    assert _via_proxy(world, "PUT", f"{api}/pulls/1/merge", {})[1]["merged"] is True

    clone = tmp_path / "clone"
    done = subprocess.run(["git", "clone", "--depth", "1", "--branch", "main",
                           "https://x-access-token:t@github.com/e2e-org/e2e-app.git", str(clone)],
                          env=_git_env(world), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert (clone / "e2e_feature.py").read_text().startswith("def answer")


def test_a_push_over_https_reaches_the_bare_repo_the_api_reads(world, tmp_path):
    clone = tmp_path / "c"
    env = _git_env(world)
    url = "https://x-access-token:t@github.com/e2e-org/pushed.git"
    assert subprocess.run(["git", "clone", url, str(clone)], env=env,
                          capture_output=True, timeout=60).returncode == 0
    (clone / "new.txt").write_text("x")
    for argv in (["add", "-A"], ["-c", "user.name=e", "-c", "user.email=e@x", "commit", "-m", "n"],
                 ["push", "origin", "HEAD:refs/heads/boss/pushed"]):
        done = subprocess.run(["git", "-C", str(clone), *argv], env=env, capture_output=True,
                              text=True, timeout=60)
        assert done.returncode == 0, done.stderr
    status, ref = _via_proxy(world, "GET",
                             "https://api.github.com/repos/e2e-org/pushed/git/ref/heads/boss/pushed")
    assert status == 200 and len(ref["object"]["sha"]) == 40


def test_a_host_with_no_fake_is_refused_and_logged(world):
    status, body = _via_proxy(world, "GET", "https://api.openai.com/v1/models")
    assert status == 502 and "api.openai.com" in body["message"]
    blocked = [e for e in world["state"].log if e.get("blocked")]
    assert blocked and blocked[-1]["egress"] == "api.openai.com"


def test_the_deployed_app_host_serves_a_page(world):
    status, _ = _via_proxy(world, "GET", "http://deploy.e2e.test:8000/health")
    assert status == 200


def test_the_fake_ssh_answers_the_host_probe_and_the_health_poll(world, tmp_path):
    (tmp_path / ".boss-e2e-fakes-url").write_text(world["url"])
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    probe = subprocess.run([sys.executable, str(STUB_SSH), "-i", "k", "-p", "22", "-n",
                            "deploy@deploy.e2e.test",
                            "echo BOSS_SSH_OK; command -v docker >/dev/null && echo DOCKER_OK; uname -m"],
                           env=env, capture_output=True, text=True, timeout=30)
    assert probe.stdout.split() == ["BOSS_SSH_OK", "DOCKER_OK", "x86_64"]
    health = subprocess.run([sys.executable, str(STUB_SSH), "-n", "deploy@deploy.e2e.test",
                             "curl -fsS -o /dev/null -w '%{http_code}' http://localhost:8001/"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert health.stdout == "200"
    assert any(e.get("path") == "/deploy/ssh" for e in world["state"].log)


def test_the_mcp_fake_answers_the_engines_own_mcp_client_and_keeps_every_call(world):
    """Round-two review: the R1 journey registered an MCP server at the fakes' /mcp and nothing
    answered there, so no journey could bind its tool to a step and show the step called it.
    The engine's own client lists the tool and calls it; the call is kept at /mcp/_calls."""
    from boss_engine.services.mcp_client import MCPClient

    client = MCPClient(settings=None)
    tools = client.list_tools(f"{world['url']}/mcp")
    assert [t["name"] for t in tools] == [fakes.MCP_ECHO_TOOL["name"]]
    answer = client.call_tool(f"{world['url']}/mcp", "e2e_echo", {"note": "E2E journey 101500"})
    assert answer["echoed"] == {"note": "E2E journey 101500"}
    with urllib.request.urlopen(f"{world['url']}/mcp/_calls", timeout=10) as response:
        calls = json.loads(response.read())["calls"]
    assert [(c["tool"], c["arguments"]) for c in calls] == [
        ("e2e_echo", {"note": "E2E journey 101500"})]
    with pytest.raises(RuntimeError, match="no tool"):
        client.call_tool(f"{world['url']}/mcp", "not_a_tool", {})
