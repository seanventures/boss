"""A single-page app previewed under /v1/previews/<id>/web sees its OWN paths.

THE CASE (HelpDesk AI recording run, 2026-10-16): the preview of a React Router SPA rendered a
blank page. Its router read `location.pathname` = "/v1/previews/<id>/web/", matched no route and
rendered nothing — and the shim made it worse, prefixing every pushState so even a navigation
landed on a path the router does not know. No app can fix a prefix it never heard of, and BOSS
must not ask each app to.

The class, fixed in the gateway (preview_service._inject_shim, at_root — the preview origin):
before the app's scripts run the address becomes the app's own path, history calls stay
unprefixed, and every request still goes out under the prefix through the gateway. A reload of
the app's own path is sent back to the preview by the origin's bootstrap (preview_origin.py),
from the tab's window.name. The shim is run here, in node, against the browser surface it uses
(location, history, fetch, XHR, window.name), the way a router reads it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from boss_engine.services.preview_service import PREVIEW_TAB_NAME, _inject_shim

PREFIX = "/v1/previews/prev_1/web"

HARNESS = r"""
const shim = require("fs").readFileSync(0, "utf8");
let cur = new URL(process.argv[1]);
const calls = {fetch: [], xhr: [], pushed: []};
global.location = {
  get pathname() { return cur.pathname; }, get search() { return cur.search; },
  get hash() { return cur.hash; }, get origin() { return cur.origin; },
  get host() { return cur.host; }, get protocol() { return cur.protocol; },
  get href() { return cur.href; },
};
global.history = {
  state: {key: "initial"},
  pushState(s, t, u) { if (u !== undefined && u !== null) cur = new URL(u, cur); this.state = s; },
  replaceState(s, t, u) { if (u !== undefined && u !== null) cur = new URL(u, cur); this.state = s; },
};
global.window = global;
window.name = process.argv[2] || "";
window.fetch = function (u) { calls.fetch.push(String(u)); return Promise.resolve(); };
global.XMLHttpRequest = function () {};
XMLHttpRequest.prototype.open = function (m, u) { calls.xhr.push(String(u)); };
global.MutationObserver = function () { this.observe = function () {}; };
global.document = {documentElement: {}, addEventListener() {}, querySelector() { return null; }};
eval(shim);
// What a router does: read the path it starts at, navigate, read again.
const started = location.pathname + location.search;
history.pushState({usr: 1}, "", "/tickets/42");
const afterPush = location.pathname;
history.pushState({usr: 2}, "", location.origin + "/v1/previews/prev_1/web/settings");
const afterPrefixedPush = location.pathname;
fetch("/api/tickets");
fetch(location.origin + "/api/me");
fetch("https://elsewhere.example/x");
new XMLHttpRequest().open("GET", "/api/xhr");
process.stdout.write(JSON.stringify({started, afterPush, afterPrefixedPush, state: history.state,
                                     name: window.name, ...calls}));
"""


def _script(html: str) -> str:
    found = re.search(r"<script data-boss-preview-shim>(.*?)</script>", html, re.S)
    assert found, html[:300]
    return found.group(1)


def _run(at_root: bool, start: str, name: str = "") -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = _inject_shim(b"<html><head></head><body><div id=root></div></body></html>", PREFIX,
                        at_root=at_root).decode()
    done = subprocess.run([node, "-e", HARNESS, start, name], input=_script(html),
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_on_the_preview_origin_the_router_sees_the_apps_own_paths():
    got = _run(True, f"http://203.0.113.10:3001{PREFIX}/tickets?open=1#top")
    assert got["started"] == "/tickets?open=1", "the router starts at the app's own path"
    assert got["afterPush"] == "/tickets/42", "a navigation stays on the app's own path"
    assert got["afterPrefixedPush"] == "/settings", "a prefixed navigation is unprefixed"
    assert got["state"] == {"usr": 2}
    # Every request still goes out under the prefix, through the gateway.
    assert got["fetch"] == [f"{PREFIX}/api/tickets", f"http://203.0.113.10:3001{PREFIX}/api/me",
                            "https://elsewhere.example/x"]
    assert got["xhr"] == [f"{PREFIX}/api/xhr"]
    assert got["name"] == PREVIEW_TAB_NAME + "prev_1", "a reload can find its preview"


def test_the_root_of_the_preview_is_the_apps_root():
    assert _run(True, f"http://203.0.113.10:3001{PREFIX}/")["started"] == "/"
    assert _run(True, f"http://203.0.113.10:3001{PREFIX}")["started"] == "/"


def test_an_apps_own_window_name_is_left_alone():
    assert _run(True, f"http://203.0.113.10:3001{PREFIX}/", "checkout-popup")["name"] == \
        "checkout-popup"


def test_off_the_preview_origin_the_address_keeps_its_prefix():
    """The API origin has no bootstrap at its root, so a reload there must stay prefixed."""
    got = _run(False, f"http://203.0.113.10:8000{PREFIX}/tickets")
    assert got["started"] == f"{PREFIX}/tickets"
    assert got["afterPush"] == f"{PREFIX}/tickets/42"
    assert got["name"] == ""
