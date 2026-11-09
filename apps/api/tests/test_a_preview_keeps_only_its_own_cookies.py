"""Sign-in inside a preview: a previewed app keeps ITS OWN cookies, and nothing else ever crosses.

boss_engine.services.preview_cookies + the preview gateway (apps/api/app/main.py preview_web_proxy,
apps/api/app/preview_origin.py). The adversarial cases, each against the real gateway and a real
app on a loopback port:

  * a preview can neither read nor set a BOSS cookie (the console session, pre-auth, device,
    boss_* cookies; a Set-Cookie naming one is dropped);
  * one preview's cookies never reach another preview, and a preview cannot plant a cookie on
    another preview's path (or widen one with Domain);
  * the console session never reaches a preview, on the preview origin or on the API's.
"""
from __future__ import annotations

import http.server
import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.preview_origin import preview_only
from boss_engine.services.preview_service import preview_host_label
from boss_engine.services.preview_cookies import (
    cookie_path, cookie_tag, inbound_cookie_header, is_relayable_set_cookie, outbound_set_cookie,
)

HOME = "boss-dogfood"
BOSS_COOKIES = {"__Host-boss_session": "bss_operator_session", "__Host-boss_preauth": "pre",
                "__Host-boss_device": "dev", "boss_tz": "UTC", "__Host-boss_app_session": "app"}


class _App(http.server.BaseHTTPRequestHandler):
    """A previewed app: echoes the Cookie header it got, sets what the test asks it to."""

    seen: list = []
    answer_cookies: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append(self.headers.get("Cookie"))
        body = json.dumps({"cookie": self.headers.get("Cookie")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        for c in type(self).answer_cookies:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def app_server():
    _App.seen = []
    _App.answer_cookies = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def two_previews(engine, app_server):
    ids = [f"prv_{uuid.uuid4().hex[:12]}" for _ in range(2)]
    with engine.begin() as db:
        for pid in ids:
            db.execute(text("INSERT INTO app_previews (id, project_id, repo, branch, command, mode, status, url) "
                            "VALUES (:i, :p, 'o/r', 'b', 'npm start', 'process', 'running', :u)"),
                       {"i": pid, "p": HOME, "u": app_server})
    yield ids
    with engine.begin() as db:
        db.execute(text("DELETE FROM app_previews WHERE id = ANY(:i)"), {"i": ids})


def _jar(*own: tuple[str, str, str]) -> str:
    """A browser's Cookie header on a preview path: BOSS's cookies plus the given (preview, name,
    value) preview cookies, tagged as the gateway set them."""
    parts = [f"{k}={v}" for k, v in BOSS_COOKIES.items()]
    parts += [f"{cookie_tag(p)}{n}={v}" for p, n, v in own]
    return "; ".join(parts)


# ---- the rule ------------------------------------------------------------------------------------

def test_a_set_cookie_is_tagged_and_scoped_or_dropped():
    pid = "prv_a"
    made = outbound_set_cookie(pid, "sid=s%3Aabc; Path=/; Domain=example.com; HttpOnly; SameSite=Lax")
    assert made == f"{cookie_tag(pid)}sid=s%3Aabc; HttpOnly; SameSite=Lax; Path=/v1/previews/prv_a/"
    assert is_relayable_set_cookie(pid, made)
    assert not is_relayable_set_cookie("prv_b", made), "another preview's cookie leaves as its own"
    for boss in ("__Host-boss_session=x; Path=/", "boss_tz=UTC", "__Secure-boss_x=1", "BOSS-thing=1",
                 "__host-BOSS_preauth=1"):
        assert outbound_set_cookie(pid, boss) is None, boss
    for elsewhere in ("x=1; Path=/v1/previews/prv_b/", "x=1; Path=/v1/previews/prv_ab/",
                      "x=1; Path=/v1/previews/"):
        assert outbound_set_cookie(pid, elsewhere) is None, elsewhere
    assert outbound_set_cookie(pid, "x=1\r\nSet-Cookie: __Host-boss_session=y") is None
    assert outbound_set_cookie("../x", "x=1") is None
    # a hand-made Set-Cookie (untagged, wrong path, Domain) never passes the edge check
    for forged in ("sid=1; Path=/v1/previews/prv_a/", f"{cookie_tag(pid)}sid=1; Path=/",
                   f"{cookie_tag(pid)}sid=1; Path=/v1/previews/prv_a/; Domain=evil.test",
                   f"{cookie_tag(pid)}__Host-boss_session=1; Path=/v1/previews/prv_a/"):
        assert not is_relayable_set_cookie(pid, forged), forged


def test_only_the_previews_own_cookies_come_in_untagged():
    header = _jar(("prv_a", "sid", "mine"), ("prv_b", "sid", "theirs"))
    assert inbound_cookie_header("prv_a", header) == "sid=mine"
    assert inbound_cookie_header("prv_b", header) == "sid=theirs"
    assert inbound_cookie_header("prv_c", header) is None
    assert inbound_cookie_header("prv_a", "; ".join(f"{k}={v}" for k, v in BOSS_COOKIES.items())) is None
    assert inbound_cookie_header("prv_a", f"{cookie_tag('prv_a')}__Host-boss_session=x") is None


# ---- the gateway, end to end ----------------------------------------------------------------------

def _edge(preview_id: str) -> TestClient:
    from apps.api.app.main import app

    return TestClient(preview_only(app), base_url=f"http://{preview_host_label(preview_id)}.localhost")


def test_on_the_preview_origin_the_app_sees_only_its_own_cookies(two_previews):
    a, b = two_previews
    jar = _jar((a, "sid", "a-session"), (b, "sid", "b-session"))
    got = _edge(a).get(f"/v1/previews/{a}/web/api/me", headers={"cookie": jar})
    assert got.status_code == 200, got.text
    assert got.json()["cookie"] == "sid=a-session"
    got = _edge(b).get(f"/v1/previews/{b}/web/api/me", headers={"cookie": jar})
    assert got.json()["cookie"] == "sid=b-session"
    # BOSS's cookies only: the app gets no Cookie header at all
    got = _edge(a).get(f"/v1/previews/{a}/web/", headers={"cookie": _jar()})
    assert got.json()["cookie"] is None
    for seen in _App.seen:
        for name, value in BOSS_COOKIES.items():
            assert name not in (seen or "") and value not in (seen or ""), seen


def test_a_preview_cannot_set_a_boss_cookie_or_another_previews(two_previews):
    a, b = two_previews
    _App.answer_cookies = [
        "sid=s1; Path=/; HttpOnly; SameSite=Lax",                 # its own: relayed, scoped
        "__Host-boss_session=stolen; Path=/; Secure; HttpOnly",   # BOSS's: dropped
        "boss_tz=Mars/Base",                                      # BOSS's: dropped
        f"theirs=1; Path=/v1/previews/{b}/",                      # another preview's path: dropped
        f"{cookie_tag(b)}sid=planted; Path=/",                    # another preview's tag: stays A's
        "wide=1; Domain=localhost; Path=/api",                    # Domain removed, path scoped
    ]
    answer = _edge(a).get(f"/v1/previews/{a}/web/")
    set_cookies = answer.headers.get_list("set-cookie")
    assert set_cookies, "the app's own cookie reaches the browser"
    for c in set_cookies:
        name = c.split("=", 1)[0]
        assert name.startswith(cookie_tag(a)), c
        assert c.endswith(f"Path={cookie_path(a)}") and "domain=" not in c.lower(), c
        assert "boss" not in name.lower()[len(cookie_tag(a)):], c
    names = sorted(c.split("=", 1)[0][len(cookie_tag(a)):] for c in set_cookies)
    assert names == sorted(["sid", f"{cookie_tag(b)}sid", "wide"])
    # and what A planted under B's tag comes back to B as nothing of B's
    planted = f"{cookie_tag(a)}{cookie_tag(b)}sid=planted"
    assert inbound_cookie_header(b, planted) is None


def test_on_the_apis_origin_too_no_boss_cookie_reaches_the_app(client, two_previews):
    """The API's own origin serves a preview only to a browser without the console session; any
    other BOSS cookie it carries (pre-auth, device, boss_tz) and another preview's never pass."""
    a, b = two_previews
    jar = "; ".join(f"{k}={v}" for k, v in BOSS_COOKIES.items() if k != "__Host-boss_session")
    jar += f"; {cookie_tag(a)}sid=a-session; {cookie_tag(b)}sid=b-session"
    got = client.get(f"/v1/previews/{a}/web/x", headers={"cookie": jar})
    assert got.status_code == 200, got.text
    assert got.json()["cookie"] == "sid=a-session"
    # with the console session: never served here, sent to the preview origin, nothing proxied
    _App.seen = []
    moved = client.get(f"/v1/previews/{a}/web/x", headers={"cookie": _jar((a, "sid", "a"))},
                       follow_redirects=False)
    assert moved.status_code in (307, 403), moved.text
    assert _App.seen == []
