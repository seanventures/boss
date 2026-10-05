"""A page read never waits for a coding CLI.

Connecting Claude Code hung the whole console: GET /v1/setup asked the CLI for its model catalog
on the request thread, the catalog read blocked on `readline()` for a CLI that wrote nothing
(the deadline was only checked between reads), and every page load added one more hung
`claude -p` holding a database transaction open. Two fixes, both held here:

  * inside `catalogs_from_cache_only()` (what the setup view runs under) a catalog comes from
    the cache, and a missing one is fetched on a background thread — the read returns at once;
  * the Claude catalog read has a hard deadline: the CLI is killed when it passes.
"""
from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace

from boss_engine.services import cli_harness, setup_view


def test_a_read_returns_at_once_and_the_catalog_arrives_in_the_background(monkeypatch):
    release = threading.Event()
    calls = []

    def slow(spec):
        calls.append(spec.name)
        release.wait(5)
        return ("opus", "sonnet")

    monkeypatch.setattr(cli_harness, "_read_model_catalog", slow)
    monkeypatch.setattr(cli_harness, "_catalog_cache", {})
    started = time.monotonic()
    with cli_harness.catalogs_from_cache_only():
        assert cli_harness.offered_models("claude_code") == ()
        assert cli_harness.offered_models("claude_code") == ()     # one refresh, not two
    assert time.monotonic() - started < 0.5
    release.set()
    for _ in range(50):
        if cli_harness._catalog_cache.get("claude_code"):
            break
        time.sleep(0.05)
    assert calls == ["claude_code"]
    with cli_harness.catalogs_from_cache_only():
        assert cli_harness.offered_models("claude_code") == ("opus", "sonnet")


def test_the_claude_catalog_is_killed_at_its_deadline(monkeypatch):
    """A CLI that never answers: the read gives up at the deadline and kills it."""
    killed = []

    class Silent:
        def __init__(self, *_a, **_k):
            self.stdin = io.StringIO()
            self._r, self._w = __import__("os").pipe()
            self.stdout = open(self._r, "r")

        def kill(self):
            killed.append(True)
            __import__("os").close(self._w)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(cli_harness.owned_process, "popen", Silent)
    monkeypatch.setattr(cli_harness, "CATALOG_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(cli_harness.GenericCliCodeAgent, "_env", lambda self: {})
    spec = cli_harness.BUILTIN_CLI_HARNESSES["claude_code"]
    started = time.monotonic()
    assert cli_harness._claude_catalog(spec, "/bin/claude") == ()
    assert time.monotonic() - started < 2
    assert killed == [True]


def test_the_view_reads_detection_and_the_store_without_waiting(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(setup_view, "_detect_clis", lambda _s: gate.wait(5) or {})
    monkeypatch.setattr(setup_view, "_pick_backend", lambda _s: gate.wait(5) or "database")
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    setup_view.forget_cached()
    started = time.monotonic()
    assert setup_view._cli_detection(SimpleNamespace()) == {}
    assert setup_view._secret_backend(SimpleNamespace()) is None
    assert setup_view.detection_pending() is True
    assert time.monotonic() - started < 0.5
    gate.set()
