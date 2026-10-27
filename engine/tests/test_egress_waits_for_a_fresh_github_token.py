"""Egress re-sends a READ that GitHub refuses while the run's installation token is still fresh.

crun_4e4b5828, crun_ada5fe88 and crun_a1a0a5a4 (2026-10-27) each failed at clone, "remote:
Repository not found": egress resolved the run, the rules allowed the read, a token for exactly
paramg/boss was minted, and GitHub answered the first git read 404. Replayed from the egress
container with crun_ada5fe88's own claims: 7 of 8 tries 200, one 404; minting and polling a
fresh token directly: 1 mint in 6 answered 404 for ~5 s, then 200. A just-minted installation
token is not yet known everywhere at GitHub, and git takes the 404 as final. Not the carried
authority: crun_6ddf545b cloned at 07:12 under the same run-binding shape.
"""
from __future__ import annotations

import pytest

from apps.egress.boss_egress import proxy as egress_proxy
from apps.egress.boss_egress.proxy import EgressResponse

from .test_identity_agents_egress_and_cascade import _git, _setup, env  # noqa: F401

CARRIED = "run_wf:wfr_e/retry:crun_parent/retry:crun_new/ct:crun_new"


class _Lagging:
    """GitHub that answers `status` for the first `misses` calls, then 200."""

    def __init__(self, misses: int, status: int = 404):
        self.misses, self.status, self.calls = misses, status, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        if len(self.calls) <= self.misses:
            return EgressResponse(self.status, {}, b"Repository not found.")
        return EgressResponse(200, {"content-type": "application/x-git-upload-pack-advertisement"}, b"ok")


@pytest.fixture()
def pauses(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(egress_proxy, "_sleep", slept.append)
    return slept


def _proxy(env, monkeypatch, upstream, run=CARRIED):
    hub, g, shim, _up, proxy = _setup(env, monkeypatch, run=run)
    proxy.upstream = upstream
    return hub, shim, proxy


@pytest.mark.parametrize("status", [404, 401])
def test_a_clone_under_carried_authority_waits_for_its_fresh_token(env, monkeypatch, pauses, status):  # noqa: F811
    up = _Lagging(misses=2, status=status)
    hub, shim, proxy = _proxy(env, monkeypatch, up)
    resp = _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    assert resp.status == 200 and len(up.calls) == 3 and pauses == [0.5, 1.0]
    # The same request with the same token every time: nothing re-minted, nothing wider.
    assert len({c[2]["Authorization"] for c in up.calls}) == 1 and len(hub.issued) == 1
    assert {c[1] for c in up.calls} == {"https://github.com/acme/web.git/info/refs?service=git-upload-pack"}


def test_the_wait_is_bounded(env, monkeypatch, pauses):  # noqa: F811
    up = _Lagging(misses=99)
    _hub, shim, proxy = _proxy(env, monkeypatch, up)
    resp = _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    assert resp.status == 404 and len(up.calls) == 1 + len(egress_proxy.FRESH_TOKEN_BACKOFF)
    assert sum(pauses) <= 20


def test_a_push_is_never_re_sent(env, monkeypatch, pauses):  # noqa: F811
    up = _Lagging(misses=1)
    _hub, shim, proxy = _proxy(env, monkeypatch, up)
    monkeypatch.setattr(proxy, "_check_push", lambda *a, **k: egress_proxy.egress_rules.RuleDecision(True, "t"))
    resp = _git(proxy, shim, "POST", "/acme/web.git/git-receive-pack")
    assert resp.status == 404 and len(up.calls) == 1 and pauses == []


def test_a_token_that_is_no_longer_fresh_is_not_waited_on(env, monkeypatch, pauses):  # noqa: F811
    monkeypatch.setattr(egress_proxy, "FRESH_TOKEN_SECONDS", -1)
    up = _Lagging(misses=1)
    _hub, shim, proxy = _proxy(env, monkeypatch, up)
    resp = _git(proxy, shim, "GET", "/acme/web.git/info/refs", "service=git-upload-pack")
    assert resp.status == 404 and len(up.calls) == 1 and pauses == []
