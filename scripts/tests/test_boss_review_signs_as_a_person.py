"""`boss review` and `boss login` (BOSSAPP-948): a review comment is signed by a PERSON.

`boss review` used to fall back to the host tools' key when no personal token was stored, and the
comment was recorded as the host tools', not a person's. Now, with no personal token, it refuses
in words before any request is made; `boss login` keeps a person's own access token (checked with
BOSS: GET /v1/identity/me must be a person's) in git's credential helper, never printing it.

The real scripts/install/boss.sh against a stand-in API on a loopback port; no Docker, no DB."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BOSS = ROOT / "scripts" / "install" / "boss.sh"
PERSON = "boss_pat_" + "P" * 43 + "abcdef"
SERVICE = "boss_pat_" + "S" * 43 + "abcdef"
HOST = "boss_sak_" + "H" * 43 + "abcdef"
REFUSAL = "Run `boss clone <project>` or `boss login` first: comments are signed by a person."


class _Api(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str]] = []

    def log_message(self, *_a):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        _Api.seen.append(("GET", self.path, auth))
        if self.path == "/v1/identity/me":
            if auth == f"Bearer {PERSON}":
                return self._send(200, {"principal_id": "usr_1", "kind": "user",
                                        "display_name": "Param"})
            if auth == f"Bearer {SERVICE}":
                return self._send(200, {"principal_id": "sva_1", "kind": "service_account",
                                        "display_name": "CI"})
            return self._send(401, {"error": "unauthenticated"})
        if self.path.endswith("/local-changes/1/comments") and auth == f"Bearer {PERSON}":
            return self._send(200, {"number": 1, "items": [
                {"id": 7, "state": "open", "path": "src/app.js", "line": 5, "body": "Skip weekends.",
                 "author": "Param"}], "summary": {}})
        return self._send(404, {"error": "not_found"})


@pytest.fixture
def api():
    _Api.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def install(tmp_path, api):
    d = tmp_path / "install"
    (d / "scripts" / "install").mkdir(parents=True)
    (d / "scripts" / "install" / "community-compose.yml").write_text("services: {}\n")
    (d / ".env").write_text(f"BOSS_API_PORT={api}\nBOSS_HOST_API_KEY={HOST}\n")
    return d


def _env(tmp_path, **extra) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_TERMINAL_PROMPT": "0", "BOSS_GIT_REMOTE_BASE": "http://127.0.0.1:9",
           "BOSS_GIT_CREDENTIAL_HELPER": f"store --file={tmp_path / 'creds'}"}
    env.update(extra)
    return env


def _boss(install, env, *args, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(BOSS), "--dir", str(install), *args], env=env,
                          input=stdin if stdin is not None else "", capture_output=True,
                          text=True, timeout=60, start_new_session=True)  # no /dev/tty


def test_review_with_no_personal_token_refuses_in_words_and_sends_nothing(install, tmp_path):
    for action in (["comment", "1", "src/app.js:5", "Skip weekends."], ["list", "1"],
                   ["reply", "1", "7", "ok"], ["resolve", "1", "7"]):
        said = _boss(install, _env(tmp_path), "review", *action, "--project", "p1")
        assert said.returncode == 1, said
        assert REFUSAL in said.stderr, said.stderr
    assert _Api.seen == []                       # never fell back to the host tools' key


def test_review_refuses_the_host_tools_key_even_when_it_is_handed_over(install, tmp_path):
    said = _boss(install, _env(tmp_path, BOSS_TOKEN=HOST), "review", "comment", "1",
                 "src/app.js:5", "x", "--project", "p1")
    assert said.returncode == 1 and REFUSAL in said.stderr
    assert HOST not in said.stdout + said.stderr
    assert _Api.seen == []


def test_login_keeps_a_persons_token_in_the_credential_helper_and_review_signs_with_it(
        install, tmp_path):
    env = _env(tmp_path)
    said = _boss(install, env, "login", "--with-token", stdin=PERSON + "\n")
    assert said.returncode == 0, said.stderr
    assert "Signed in as Param" in said.stdout
    assert PERSON not in said.stdout + said.stderr
    assert PERSON in (tmp_path / "creds").read_text()      # the helper holds it, nothing else
    assert ("GET", "/v1/identity/me", f"Bearer {PERSON}") in _Api.seen

    listed = _boss(install, env, "review", "list", "1", "--project", "p1")
    assert listed.returncode == 0, listed.stderr
    assert "Skip weekends." in listed.stdout and "Param" in listed.stdout
    assert ("GET", "/v1/projects/p1/local-changes/1/comments", f"Bearer {PERSON}") in _Api.seen
    assert PERSON not in listed.stdout + listed.stderr


@pytest.mark.parametrize("token, words", [
    (HOST, "not a personal access token"),          # the host tools' key: refused unasked
    ("not-a-token", "not a personal access token"),
    (SERVICE, "not a person's"),                    # BOSS says it is a service account's
    ("boss_pat_" + "X" * 49, "BOSS refused that token"),
])
def test_login_refuses_anything_but_a_persons_token_and_never_prints_it(install, tmp_path,
                                                                        token, words):
    said = _boss(install, _env(tmp_path), "login", "--with-token", stdin=token + "\n")
    assert said.returncode == 1 and words in said.stderr, said.stderr
    assert token not in said.stdout + said.stderr
    assert not (tmp_path / "creds").exists() or token not in (tmp_path / "creds").read_text()
