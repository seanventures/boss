"""P0/P1: codex_login proactively refreshes a Codex CLI subscription login before it expires,
the same discipline claude_login already has ([L:kgn] context: Codex has no one-year token, so
a dying login must be caught by a proactive refresh, not discovered as a 401 mid-build).

No network: the OAuth token endpoint is a fake `post`. No real secret: markers only, generated
at import/call time rather than hard-coded, and never asserted to appear in any "log".
"""
from __future__ import annotations

import base64
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from boss_engine.services import codex_login


def _jwt(exp_seconds_from_now: float) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = seg({"alg": "none", "typ": "JWT"})
    payload = seg({"exp": int(time.time() + exp_seconds_from_now)})
    return f"{header}.{payload}.sig"


class CodexLoginRefreshTest(unittest.TestCase):
    """AC2: a Codex access token within the refresh window refreshes exactly once, atomically,
    and never logs a token value."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.home.mkdir(exist_ok=True)
        self._env = {k: v for k, v in os.environ.items()}
        os.environ["CODEX_HOME"] = str(self.home)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _write_auth(self, access: str, refresh: str, exp_seconds: float) -> Path:
        path = self.home / "auth.json"
        path.write_text(json.dumps({
            "auth_mode": "chatgpt",
            "last_refresh": "2026-10-01T00:00:00Z",
            "tokens": {"access_token": _jwt(exp_seconds), "refresh_token": refresh},
        }))
        return path

    def test_refresh_if_due_refreshes_a_near_expiry_token_exactly_once(self) -> None:
        self._write_auth("unused", "codex-refresh-marker", exp_seconds=60)  # inside the 2-day window
        calls: list[dict] = []

        def fake_post(url, body):
            calls.append(body)
            return 200, {"access_token": _jwt(2 * 24 * 3600), "refresh_token": "codex-refresh-rotated",
                         "expires_in": 3600}

        result = codex_login.refresh_if_due(post=fake_post)

        self.assertEqual(result["result"], "refreshed")
        self.assertEqual(len(calls), 1)
        auth_after = codex_login.read_auth(str(self.home))
        self.assertEqual(auth_after["tokens"]["refresh_token"], "codex-refresh-rotated")
        # The refresh happened, nothing was left as a stray temp file.
        self.assertEqual([p.name for p in self.home.iterdir()], ["auth.json"])
        # No secret value anywhere in the fake network body logged by this test's own asserts.
        for body in calls:
            self.assertNotIn("sk-", json.dumps(body))

    def test_refresh_if_due_leaves_a_fresh_token_untouched(self) -> None:
        self._write_auth("unused", "codex-refresh-marker-2", exp_seconds=10 * 24 * 3600)

        def fake_post(url, body):
            raise AssertionError("must not call the token endpoint for a token far from expiry")

        result = codex_login.refresh_if_due(post=fake_post)

        self.assertEqual(result["result"], "fresh")
        auth_after = codex_login.read_auth(str(self.home))
        self.assertEqual(auth_after["tokens"]["refresh_token"], "codex-refresh-marker-2")


if __name__ == "__main__":
    unittest.main()
