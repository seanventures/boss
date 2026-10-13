"""A transient GitHub/network error is retried inside the operation, never by failing the build.

crun_2f76585e (2026-10-13) failed after 1.1 min on one GitHub 502 during the clone: "git -c failed:
Cloning into '.'... error: RPC failed; HTTP 502 curl 22 ... fatal: expected 'packfile'". Every git
command a build sends to the remote goes through EphemeralSandbox._git, and every GitHub REST call
through GitHubAdapter._send; both retry a transient failure (transient_retry: 3 tries, ~40 s of
backoff), say each retry, and never retry an auth answer (401/403/404).
"""
from __future__ import annotations

import types

import httpx
import pytest

from boss_engine.services import transient_retry
from boss_engine.services.github_adapter import GitHubAdapter
from boss_engine.services.sandbox import EphemeralSandbox, SandboxError

CLONE_502 = ("git -c failed: Cloning into '.'...\nerror: RPC failed; HTTP 502 curl 22 The requested "
             "URL returned error: 502\nfatal: expected 'packfile'")


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(transient_retry, "_sleep", waits.append)
    return waits


@pytest.mark.parametrize("said", [
    CLONE_502,
    "git fetch failed: fatal: early EOF",
    "git fetch failed: error: RPC failed; curl 56 Recv failure: Connection reset by peer",
    "git ls-remote failed: fatal: unable to access 'https://github.com/a/b/': The requested URL returned error: 503",
    "git clone timed out",
    "git fetch failed: fatal: unable to access '...': Failed to connect to github.com port 443: Connection timed out",
])
def test_transient_git_failures_are_recognised(said):
    assert transient_retry.is_transient_git_error(said)


@pytest.mark.parametrize("said", [
    "git -c failed: remote: Repository not found.\nfatal: repository 'https://github.com/a/b/' not found",
    "git clone failed: fatal: Authentication failed for 'https://github.com/a/b/'",
    "git fetch failed: fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "git -c failed: fatal: unable to access '...': The requested URL returned error: 403",
    "git -c failed: fatal: Remote branch nope not found in upstream origin",
    "git checkout failed: error: pathspec 'x' did not match",
])
def test_auth_and_request_errors_are_not_transient(said):
    assert not transient_retry.is_transient_git_error(said)


def _sandbox(tmp_path, answers):
    box = EphemeralSandbox.__new__(EphemeralSandbox)
    box.path = tmp_path
    calls: list[list[str]] = []

    def once(args, *, raw=False, redact=None):
        calls.append(list(args))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            # A clone cut off mid-transfer leaves files behind.
            (tmp_path / "partial.pack").write_text("x")
            raise answer
        return answer

    box._git_once = once
    return box, calls


def test_a_clone_that_hits_a_502_is_tried_again_and_the_build_goes_on(tmp_path, _no_wait):
    seen: list[dict] = []
    box, calls = _sandbox(tmp_path, [SandboxError(CLONE_502), SandboxError(CLONE_502), "ok"])
    with transient_retry.reporting(seen.append):
        out = box._git(["-c", "http.extraHeader=x", "clone", "--depth", "1", "u", "."])
    assert out == "ok" and len(calls) == 3
    assert _no_wait == list(transient_retry.DELAYS)            # backoff: 10 s, then 30 s
    assert 30 <= sum(_no_wait) <= 60
    # Each retry is said, naming the operation and why.
    assert [s["operation"] for s in seen] == ["git clone", "git clone"]
    assert "502" in seen[0]["error"] and seen[0]["next_attempt"] == 2
    # The partial tree of the failed attempt is cleared before the next clone.
    assert not (tmp_path / "partial.pack").exists()


def test_a_clone_that_keeps_failing_fails_after_three_tries_with_the_real_error(tmp_path):
    box, calls = _sandbox(tmp_path, [SandboxError(CLONE_502)] * 3)
    with pytest.raises(SandboxError, match="expected 'packfile'"):
        box._git(["-c", "k=v", "clone", "u", "."])
    assert len(calls) == transient_retry.ATTEMPTS == 3


def test_an_auth_failure_is_never_retried(tmp_path, _no_wait):
    box, calls = _sandbox(tmp_path, [SandboxError("git clone failed: fatal: Authentication failed")])
    with pytest.raises(SandboxError, match="Authentication failed"):
        box._git(["clone", "u", "."])
    assert len(calls) == 1 and _no_wait == []


def test_local_git_commands_are_not_retried(tmp_path):
    box, calls = _sandbox(tmp_path, [SandboxError("git checkout failed: timed out")])
    with pytest.raises(SandboxError):
        box._git(["checkout", "-b", "x"])
    assert len(calls) == 1


def _adapter():
    return GitHubAdapter(types.SimpleNamespace(target_github_owner="acme", target_github_repo="web"),
                         owner="acme", repo="web", token="t")


def _answers(monkeypatch, codes):
    sent: list[str] = []

    def request(method, url, **kw):
        sent.append(method)
        code = codes.pop(0)
        if isinstance(code, Exception):
            raise code
        return httpx.Response(code, json={"ok": code}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", request)
    return sent


def test_a_github_api_5xx_or_timeout_is_retried(monkeypatch, _no_wait):
    seen: list[dict] = []
    sent = _answers(monkeypatch, [502, httpx.ReadTimeout("slow"), 200])
    with transient_retry.reporting(seen.append):
        assert _adapter()._request("GET", "/repos/acme/web") == {"ok": 200}
    assert len(sent) == 3 and len(seen) == 2 and seen[0]["operation"] == "GitHub API GET"


@pytest.mark.parametrize("code", [401, 403, 404, 422])
def test_a_github_api_auth_or_request_error_is_not_retried(monkeypatch, code, _no_wait):
    sent = _answers(monkeypatch, [code, 200])
    with pytest.raises(httpx.HTTPStatusError):
        _adapter()._request("GET", "/repos/acme/web")
    assert len(sent) == 1 and _no_wait == []


def test_a_file_push_through_the_api_is_retried_too(monkeypatch):
    sent: list[str] = []

    # GET (404: a new file), then PUT 503, then PUT 200 with the commit.
    def request(method, url, **kw):
        sent.append(method)
        code = [404, 503, 200][len(sent) - 1]
        body = {"commit": {"sha": "abc"}} if code == 200 else {}
        return httpx.Response(code, json=body, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", request)
    import boss_engine.services.agent_acts as acts
    monkeypatch.setattr(acts, "sign_commit_message", lambda m: m)
    assert _adapter().upsert_file("b", "a.txt", "x", "m") == "abc"
    assert sent == ["GET", "PUT", "PUT"]


def test_a_builds_retries_are_recorded_as_events_on_its_run(monkeypatch, tmp_path):
    """run_code_task installs this recorder around the whole build."""
    import contextlib

    from boss_engine.services import code_task_service as cts

    events: list[tuple] = []
    monkeypatch.setattr(cts, "_run_project", lambda engine, run_id: "p1")
    monkeypatch.setattr(cts, "_transaction", lambda engine: contextlib.nullcontext("db"))
    monkeypatch.setattr(cts, "emit_event", lambda c, p, kind, actor, et, eid, data: events.append(
        (p, kind, eid, data)))
    box, _ = _sandbox(tmp_path, [SandboxError(CLONE_502), "ok"])
    with transient_retry.reporting(cts._transient_retry_recorder(object(), "crun_x")):
        box._git(["clone", "u", "."])
    assert [(p, k, e) for p, k, e, _ in events] == [("p1", "code_task.transient_retry", "crun_x")]
    assert events[0][3]["operation"] == "git clone" and "502" in events[0][3]["error"]
