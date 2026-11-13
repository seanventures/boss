"""BOSSAPP-866: a secret value handed to a run is scrubbed from that run's output by exact match,
in its raw, base64 and URL-encoded forms — turns, tool logs and PR text — and is never written
anywhere itself."""
from __future__ import annotations

import base64
from urllib.parse import quote, quote_plus

import pytest

from boss_engine.services import run_secret_redaction as rsr
from boss_engine.services.redaction import redact, redact_secrets

# No recognisable shape: pattern redaction alone lets it through.
PASSWORD = "plain horse battery 42!"


@pytest.fixture(autouse=True)
def _clean():
    rsr.reset_for_tests()
    yield
    rsr.reset_for_tests()


def test_pattern_redaction_alone_misses_a_shapeless_password():
    assert PASSWORD in redact_secrets(f"connected with {PASSWORD} ok")


def test_a_registered_value_is_scrubbed_in_every_encoding():
    rsr.register(PASSWORD, run_id="run-1")
    raw = PASSWORD.encode()
    for form in (PASSWORD, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(),
                 base64.b64encode(raw).decode().rstrip("="), quote(PASSWORD, safe=""),
                 quote_plus(PASSWORD, safe="")):
        out = rsr.scrub(f"before {form} after")
        assert form not in out, form
        assert "[SECRET]" in out


def test_turn_capture_and_tool_logs_go_through_it():
    rsr.register(PASSWORD, run_id="run-1")
    # Turn capture (agent_turns._clip) calls redact(); tool-argument previews call redact_secrets().
    text, summary = redact(f"psql said: {PASSWORD}")
    assert PASSWORD not in text and summary.get("SECRET")
    assert PASSWORD not in redact_secrets(f"echo {PASSWORD}")
    from boss_engine.services.argument_binding import redacted_preview

    assert redacted_preview({"cmd": f"login {PASSWORD} now"})["cmd"] == "login [SECRET] now"


def test_structures_are_scrubbed_recursively():
    rsr.register(PASSWORD, run_id="run-1")
    out = rsr.scrub({"a": [PASSWORD, ("x", PASSWORD)], "n": 3})
    assert out == {"a": ["[SECRET]", ("x", "[SECRET]")], "n": 3}


def test_short_values_are_not_registered():
    assert rsr.register("admin", run_id="run-1") == 0
    assert rsr.scrub("admin logged in") == "admin logged in"


def test_empty_registry_is_byte_identical():
    text = "nothing to see: password=hunter22"
    assert rsr.scrub(text) is text
    assert redact_secrets(text) == "nothing to see: password=[SECRET]"


def test_a_released_run_is_forgotten_after_its_grace():
    rsr.register(PASSWORD, run_id="run-2")
    rsr.release("run-2", grace_seconds=0)
    assert rsr.live_count() == 0
    assert rsr.scrub(PASSWORD) == PASSWORD


def test_values_follow_the_current_run_scope():
    from boss_engine.services.harness_failures import run_scope

    with run_scope("run-3"):
        rsr.register(PASSWORD)
        assert rsr.live_count("run-3") > 0
    # still held for the grace period after the scope ends
    assert PASSWORD not in rsr.scrub(PASSWORD)


def test_resolving_a_secret_ref_registers_its_value(monkeypatch):
    from boss_engine.services import secret_backends

    class _Backend:
        def get(self, key):
            return PASSWORD

    class _Row(dict):
        pass

    class _Result:
        def mappings(self):
            return self

        def first(self):
            return _Row(ref_kind="env", ref_key="K", storage_backend="environment",
                        status="active", project_id="p", scope_type="project", scope_id="p",
                        provider=None, metadata={})

    class _Conn:
        def execute(self, *a, **k):
            return _Result()

    monkeypatch.setattr(secret_backends, "backend_for", lambda kind, settings: _Backend())
    assert secret_backends.resolve_secret_ref(_Conn(), "sref_1", settings=None) == PASSWORD
    assert rsr.live_count() > 0
    assert PASSWORD not in redact_secrets(f"value {PASSWORD}")


def test_pr_text_is_scrubbed_before_it_leaves(monkeypatch):
    from boss_engine.services.mcp_repo_host import McpRepoHost

    sent = {}
    host = McpRepoHost.__new__(McpRepoHost)
    monkeypatch.setattr(McpRepoHost, "repository_full_name", "o/r")
    monkeypatch.setattr(host, "_call", lambda name, args: sent.update(args) or {"url": "u", "id": 1},
                        raising=False)
    rsr.register(PASSWORD, run_id="run-4")
    host.create_pull_request("b", f"title {PASSWORD}", f"body {quote(PASSWORD, safe='')}")
    assert PASSWORD not in sent["title"] and "[SECRET]" in sent["title"]
    assert "[SECRET]" in sent["description"]


def test_nothing_in_the_module_logs_or_returns_a_value():
    rsr.register(PASSWORD, run_id="run-5")
    assert rsr.register(PASSWORD, run_id="run-5") > 0      # a count, not the value
    assert isinstance(rsr.live_count(), int)
