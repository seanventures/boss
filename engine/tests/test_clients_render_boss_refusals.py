"""P0-2 (BOSSAPP-769): every installer client renders BOSS's refusal as "headline — fix".

BOSS refuses with {state, headline, fix} (setup_actions.SetupActionError) and no `message`.
Clients that read `message` printed `None`; one added "re-run with --accept-database-fallback",
which the CLI already passed, so people looped. Each client is fed the real refusal here.
"""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from boss_engine.services.setup_actions import SetupActionError, subscription_import_refusal

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "scripts" / "install"
sys.path.insert(0, str(INSTALL))

import boss_refusal  # noqa: E402
import coding_subscriptions  # noqa: E402
import connect_ollama  # noqa: E402

REFUSALS = [
    subscription_import_refusal().detail,
    SetupActionError(422, "failed", "API key", "Paste the provider's API key.").detail,
    {"state": "failed", "headline": "Org admin", "fix": "Only an org admin can change this."},
]


def _http_error(detail: dict, code: int = 409):
    def refuse(*_a, **_k):
        raise urllib.error.HTTPError("http://boss", code, "Refused", {},
                                     io.BytesIO(json.dumps({"detail": detail}).encode()))
    return refuse


def _assert_rendered(text: str, detail: dict) -> None:
    assert f"{detail['headline']} — {detail['fix']}" in text, text
    assert "None" not in text
    assert "accept-database-fallback" not in text
    assert "inspect" not in text.lower()  # no advice the server did not give


@pytest.mark.parametrize("detail", REFUSALS)
def test_the_shared_renderer(detail):
    assert boss_refusal.render(detail) == f"{detail['headline']} — {detail['fix']}"
    assert boss_refusal.from_body(json.dumps({"detail": detail})) == boss_refusal.render(detail)


@pytest.mark.parametrize("body, expected", [
    ({"detail": {"state": "failed", "headline": "Held"}}, "Held"),
    ({"detail": {"state": "failed", "headline": None, "fix": None}}, ""),
    ({"detail": "project_id and provider are required"}, "project_id and provider are required"),
    ({"detail": None}, ""),
    ("not json", ""),
])
def test_the_renderer_never_says_none(body, expected):
    raw = body if isinstance(body, str) else json.dumps(body)
    assert boss_refusal.from_body(raw) == expected


@pytest.mark.parametrize("detail", REFUSALS)
def test_coding_subscriptions_connect(monkeypatch, detail):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", _http_error(detail))
    with pytest.raises(RuntimeError) as refused:
        coding_subscriptions.connect("http://127.0.0.1:8000", "k", "codex")
    _assert_rendered(str(refused.value), detail)


@pytest.mark.parametrize("detail", REFUSALS)
def test_coding_subscriptions_sign_in(monkeypatch, detail):
    monkeypatch.setattr(coding_subscriptions.urllib.request, "urlopen", _http_error(detail))
    with pytest.raises(RuntimeError) as refused:
        coding_subscriptions.sign_in("http://127.0.0.1:8000", "k", "codex",
                                     terminal=io.StringIO())
    _assert_rendered(str(refused.value), detail)


@pytest.mark.parametrize("detail", REFUSALS)
def test_connect_ollama(monkeypatch, detail):
    monkeypatch.setattr(connect_ollama.urllib.request, "urlopen", _http_error(detail, 422))
    with pytest.raises(connect_ollama.ConnectError) as refused:
        connect_ollama._request("http://127.0.0.1:8000/v1/setup/model", "k", {"x": 1})
    _assert_rendered(str(refused.value), detail)


FAKE_SSH = """#!/usr/bin/env bash
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|-i|-p|-l) shift 2 ;;
    -t|-T|-q|-N) shift ;;
    *) break ;;
  esac
done
shift
HOME="$FAKE_HOST_HOME" exec bash -c "$*"
"""


@pytest.mark.parametrize("detail", REFUSALS)
def test_remote_sh_model(tmp_path, detail):
    """remote.sh model: the host's API refuses; the laptop prints BOSS's own words."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    host = tmp_path / "host"
    (host / "boss").mkdir(parents=True)
    (host / "boss" / ".env").write_text("BOSS_HOST_API_KEY=boss_sak_test\n")
    (tmp_path / "body.json").write_text(json.dumps({"detail": detail}))
    for name, script in (("ssh", FAKE_SSH), ("docker", "#!/usr/bin/env bash\nexit 0\n"),
                         ("curl", f"#!/usr/bin/env bash\ncat >/dev/null\ncat '{tmp_path}/body.json'\n")):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "FAKE_HOST_HOME": str(host)}
    result = subprocess.run(
        ["bash", str(INSTALL / "remote.sh"), "--host", "h", "--dir", str(host / "boss"),
         "model", "--provider", "openrouter"],
        input="sk-test-key\n", env=env, text=True, capture_output=True, timeout=60)
    assert result.returncode != 0
    _assert_rendered(result.stdout + result.stderr, detail)
    assert "sk-test-key" not in result.stdout + result.stderr
