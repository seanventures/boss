"""The Community installer may record a CLI login without an unmetered model turn."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "install"))
import coding_subscriptions  # noqa: E402


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_community_cli_connection_skips_live_proof_and_reports_unverified(monkeypatch, capsys):
    sent = []

    def answer(request, timeout):
        sent.append((request, timeout))
        return _Response(json.dumps({
            "connected": True,
            "entitlement_recorded": True,
            "proof": {"ok": True, "skipped": True},
        }).encode())

    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", answer)
    coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", "codex")

    request, timeout = sent[0]
    assert timeout == 120
    assert request.full_url.endswith("/v1/setup/coding-agents/connect")
    assert json.loads(request.data) == {
        "project_id": "boss-dogfood", "actor": "ceo", "provider": "codex",
        "prove": False,
    }
    output = capsys.readouterr().out
    assert "connection recorded" in output
    assert "unverified" in output
    assert "private-api-key" not in output
    assert "proved" not in output


@pytest.mark.parametrize("reply", [
    {"connected": False, "entitlement_recorded": False,
     "proof": {"ok": True, "skipped": True}},
    {"connected": True, "entitlement_recorded": False,
     "proof": {"ok": True, "skipped": True}},
    {"connected": True, "entitlement_recorded": True,
     "proof": {"ok": True}},
    {"connected": True, "entitlement_recorded": True, "proof": None},
])
def test_installer_refuses_to_claim_an_unrecorded_or_unexpected_connection(monkeypatch, reply):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen",
                        lambda *_args, **_kwargs: _Response(json.dumps(reply).encode()))
    with pytest.raises(RuntimeError, match="BOSS (did not record|returned an unexpected)"):
        coding_subscriptions.connect("http://127.0.0.1:8000", "private-api-key", "claude_code")
