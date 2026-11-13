"""BOSSAPP-855: hosted remote agents as harnesses, governed rather than replaced.

A fake hosted agent speaks boss.harness.remote/v1 over real HTTP; a real git repository is the
workspace. The agent's patch must land in BOSS's own tree (so the same gates judge it), the spend
ceiling must cancel a run that crosses it, and a profile without a ceiling or a person's
retention acknowledgement must never run. Every remote profile registers verified=False."""
from __future__ import annotations

import contextlib
import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest
from sqlalchemy import text

from boss_engine.services import remote_agents as ra
from boss_engine.services.custom_harness import CustomHarnessError

from ._ambient_support import eng, make_project, project, purge  # noqa: F401

PATCH = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+hello from the hosted agent
"""


class FakeAgent:
    """A hosted agent: each run is a scripted list of poll answers."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.submitted: list[dict] = []
        self.cancelled: list[str] = []
        self.auth: list[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.submitted.append(json.loads(self.rfile.read(n)))
                outer.auth.append(self.headers.get("Authorization") or "")
                self._send(201, {"run_id": "r1", "poll_after_ms": 10})

            def do_GET(self):
                answer = outer.polls.pop(0) if len(outer.polls) > 1 else outer.polls[0]
                self._send(200, answer)

            def do_DELETE(self):
                outer.cancelled.append(self.path)
                self._send(200, {})

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@t.test")
    git("config", "user.name", "t")
    (tmp_path / "hello.txt").write_text("hello\n")
    git("add", ".")
    git("commit", "-qm", "init")
    git("remote", "add", "origin", "https://user:secret@example.test/acme/app.git")
    return tmp_path


def _git(argv, cwd, input=None):
    return subprocess.run(argv, cwd=cwd, input=input, capture_output=True, text=True)


def _profile(url, **kw):
    base = dict(id="rag_1", org_id="org", name="cloud", provider="generic", base_url=url,
                token_env="W4R_REMOTE_TOKEN", spend_ceiling_usd_micros=5_000_000,
                retention_ack_by="op", retention_ack_at="2026-11-13", status="active",
                poll_seconds=0.01, timeout=60)
    base.update(kw)
    return ra.RemoteProfile(**base)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ra.clear_spend_for_tests()
    monkeypatch.setenv("W4R_REMOTE_TOKEN", "tok-123")
    monkeypatch.setattr("boss_engine.services.code_agent._cap_to_deadline",
                        lambda t, read_only=False: t)
    yield
    ra.clear_spend_for_tests()


def test_the_patch_lands_in_bosss_own_tree_and_nothing_secret_leaves(repo):
    agent = FakeAgent([{"status": "running"},
                       {"status": "done", "summary": "changed the greeting",
                        "usage": {"cost_usd_micros": 1_200_000},
                        "result": {"kind": "diff", "patch": PATCH}}])
    try:
        out = ra.RemoteAgentCodeAgent(None, _profile(agent.url), git=_git).run_role(
            "change the greeting", repo, model="m1")
    finally:
        agent.close()
    assert (repo / "hello.txt").read_text() == "hello from the hosted agent\n"
    assert out["files_changed"] == ["hello.txt"] and out["cost_usd_micros"] == 1_200_000
    assert out["spend"] == {"run_usd_micros": 1_200_000, "ceiling_usd_micros": 5_000_000}
    sent = agent.submitted[0]
    assert sent["contract"] == ra.CONTRACT and sent["spend_ceiling_usd_micros"] == 5_000_000
    assert sent["repository"]["url"] == "https://example.test/acme/app.git"   # no credential
    assert len(sent["repository"]["base_sha"]) == 40
    assert agent.auth == ["Bearer tok-123"]


def test_the_ceiling_cancels_the_remote_run_and_holds_for_the_rest_of_the_run(repo):
    agent = FakeAgent([{"status": "running", "usage": {"cost_usd_micros": 2_000_000}},
                       {"status": "running", "usage": {"cost_usd_micros": 6_000_000}},
                       {"status": "done"}])
    agent_impl = ra.RemoteAgentCodeAgent(None, _profile(agent.url), git=_git)
    try:
        with pytest.raises(CustomHarnessError, match="spend ceiling"):
            agent_impl.run_role("x", repo)
        assert agent.cancelled == ["/v1/runs/r1"]
        with pytest.raises(CustomHarnessError, match="spend ceiling"):
            agent_impl.run_role("again", repo)             # refused before submitting
        assert len(agent.submitted) == 1
    finally:
        agent.close()
    assert (repo / "hello.txt").read_text() == "hello\n"


@pytest.mark.parametrize("change,why", [
    ({"retention_ack_at": None}, "retention acknowledgement"),
    ({"status": "disabled"}, "disabled"),
    ({"spend_ceiling_usd_micros": 0}, "spend ceiling"),
])
def test_an_incomplete_profile_never_runs(repo, change, why):
    agent = FakeAgent([{"status": "done"}])
    try:
        with pytest.raises(CustomHarnessError, match=why):
            ra.RemoteAgentCodeAgent(None, _profile(agent.url, **change), git=_git).run_role(
                "x", repo)
        assert agent.submitted == []
    finally:
        agent.close()


def test_a_patch_outside_the_tree_or_a_failed_run_changes_nothing(repo):
    bad = PATCH.replace("a/hello.txt", "a/../evil").replace("b/hello.txt", "b/../evil")
    for answer, said in (({"status": "done", "result": {"kind": "diff", "patch": bad}},
                          "outside the working tree"),
                         ({"status": "error", "error": "it broke"}, "it broke"),
                         ({"status": "done", "result": {"kind": "pr", "url": "u"}},
                          "needs patch")):
        agent = FakeAgent([answer])
        try:
            with pytest.raises(CustomHarnessError, match=said):
                ra.RemoteAgentCodeAgent(None, _profile(agent.url), git=_git).run_role("x", repo)
        finally:
            agent.close()
    assert (repo / "hello.txt").read_text() == "hello\n"


def test_a_review_seat_reads_only(repo):
    agent = FakeAgent([{"status": "done", "summary": "VERDICT: approve",
                        "result": {"kind": "diff", "patch": PATCH}}])
    try:
        out = ra.RemoteAgentCodeAgent(None, _profile(agent.url), git=_git).run_role(
            "review", repo, read_only=True)
    finally:
        agent.close()
    assert agent.submitted[0]["mode"] == "read_only"
    assert out["files_changed"] == [] and (repo / "hello.txt").read_text() == "hello\n"


# ── profiles and registration, on a real database ────────────────────────────────────────────

@pytest.fixture()
def db_begin(eng, monkeypatch):  # noqa: F811
    @contextlib.contextmanager
    def begin(engine=None):
        with eng.begin() as c:
            yield c

    monkeypatch.setattr("boss_engine.db.begin", begin)
    return begin


def test_a_profile_needs_a_ceiling_and_an_acknowledgement_before_it_registers(
        eng, project, db_begin, monkeypatch):  # noqa: F811
    from boss_engine.services import harness_registry as hr

    monkeypatch.setattr(ra, "_project_in_context", lambda: project)
    try:
        with eng.begin() as c:
            for bad, why in (({"spend_ceiling_usd": 0}, "spend_ceiling_usd"),
                             ({"base_url": "http://agents.example.test"}, "https"),
                             ({"provider": "cursor"}, "provider")):
                kw = {"name": "cloud", "base_url": "https://agents.example.test",
                      "spend_ceiling_usd": 5, **bad}
                with pytest.raises(ra.RemoteAgentError, match=why):
                    ra.create_profile(c, project, actor="op", **kw)
            made = ra.create_profile(c, project, actor="op", name="cloud",
                                     base_url="https://agents.example.test",
                                     spend_ceiling_usd=5, token_env="W4R_REMOTE_TOKEN")
        assert made["harness"] == "remote:cloud" and not made["runnable"]
        assert "retention" in made["blocked_because"] and made["verified"] is False
        spec = hr.get_harness("remote:cloud")
        assert spec.kind == ra.KIND and spec.verified is False
        with pytest.raises(CustomHarnessError, match="retention"):
            spec.build(None)
        with eng.begin() as c:
            acked = ra.acknowledge_retention(c, project, made["id"], actor="op",
                                             terms="30-day retention, no training")
        assert acked["runnable"] and acked["retention"]["by"] == "op"
        agent = hr.get_harness("remote:cloud").build(None)
        assert isinstance(agent, ra.RemoteAgentCodeAgent)
        assert agent.profile.spend_ceiling_usd_micros == 5_000_000
        assert all(r["name"] != "remote:cloud" or not r["verified"]
                   for r in hr.available_harnesses())
        with eng.begin() as c:
            ra.update_profile(c, project, made["id"], actor="op", status="disabled")
        with pytest.raises(CustomHarnessError, match="disabled"):
            hr.get_harness("remote:cloud").build(None)
        with pytest.raises(hr.UnknownHarness):
            hr.get_harness("remote:nobody")
    finally:
        ra.forget("cloud")
        with eng.begin() as c:
            c.execute(text("DELETE FROM remote_agent_profiles WHERE name = 'cloud'"))
