"""BOSSAPP-860: a preview's own HTTPS origin through the least-privilege boss-expose helper.

The helper (scripts/expose/boss-expose) is driven for real here, as BOSS drives it — by argv —
against a fake `tailscale` binary and a fake Caddy admin API. BOSS's side must fail closed: a
preview is marked ready only after its exposed URL answered over HTTPS, and anything less
unmaps and fails it. With BOSS_PREVIEW_EXPOSE off nothing is called."""
from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from boss_engine.services import preview_expose as pe

HELPER = Path(__file__).resolve().parents[2] / "scripts" / "expose" / "boss-expose"


@pytest.fixture()
def listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    yield srv.getsockname()[1]
    srv.close()


def _config(tmp_path, port, backend="tailscale", **extra):
    log = tmp_path / "ts.log"
    fake = tmp_path / "tailscale"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                    "case \"$*\" in *fail*) exit 1;; esac\nexit 0\n")
    fake.chmod(0o755)
    cfg = {"backend": backend, "port_range": [port - 1, port + 1],
           "state": str(tmp_path / "state.json"),
           "tailscale": {"binary": str(fake), "host": "node.example.ts.net",
                         "https_ports": [8443, 8444]}, **extra}
    path = tmp_path / "expose.json"
    path.write_text(json.dumps(cfg))
    return path, log


def _helper(cfg, *args):
    env = {**os.environ, "BOSS_EXPOSE_CONFIG": str(cfg)}
    done = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True,
                          env=env, timeout=30)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


def test_the_helper_maps_a_preview_to_its_own_tailnet_origin_and_back(tmp_path, listener):
    cfg, log = _config(tmp_path, listener)
    code, said = _helper(cfg, "map", "prev-abc", str(listener))
    assert code == 0 and said["url"] == "https://node.example.ts.net:8443"
    code, again = _helper(cfg, "map", "prev-def", str(listener))
    assert again["url"].endswith(":8444")                # its own origin, not a shared one
    assert _helper(cfg, "map", "prev-ghi", str(listener))[0] == 126   # ports exhausted
    assert set(_helper(cfg, "list")[1]["exposed"]) == {"prev-abc", "prev-def"}
    code, gone = _helper(cfg, "unmap", "prev-abc")
    assert code == 0 and gone["removed"] is True
    calls = log.read_text().splitlines()
    assert calls[0] == f"serve --bg --https=8443 http://127.0.0.1:{listener}"
    assert calls[-1] == "serve --https=8443 off"


@pytest.mark.parametrize("args,why", [
    (["map", "Bad_Label", "{port}"], "label"),
    (["map", "ok", "{far}"], "outside the preview range"),
    (["map", "ok", "1x"], "number"),
    (["rm", "-rf"], "usage"),
])
def test_the_helper_refuses_what_its_configuration_does_not_allow(tmp_path, listener, args, why):
    cfg, log = _config(tmp_path, listener)
    args = [a.format(port=listener, far=listener + 50) for a in args]
    code, said = _helper(cfg, *args)
    assert code == 125 and said["refused"] and why in said["error"]
    assert not log.exists()                               # nothing reached the backend


def test_the_helper_refuses_a_port_nothing_listens_on(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    cfg, _ = _config(tmp_path, free)
    code, said = _helper(cfg, "map", "prev-x", str(free))
    assert code == 125 and "nothing is listening" in said["error"]


def test_the_helper_drives_caddys_admin_api(tmp_path, listener):
    seen = []

    class Admin(http.server.BaseHTTPRequestHandler):
        def _ok(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append((self.command, self.path, json.loads(self.rfile.read(n) or b"null")))
            self.send_response(200)
            self.end_headers()

        do_POST = do_DELETE = _ok

        def log_message(self, *a):
            pass

    admin = http.server.HTTPServer(("127.0.0.1", 0), Admin)
    threading.Thread(target=admin.serve_forever, daemon=True).start()
    try:
        cfg, _ = _config(tmp_path, listener, backend="caddy", caddy={
            "admin": f"http://127.0.0.1:{admin.server_address[1]}", "server": "srv0",
            "domain": "previews.example.com"})
        code, said = _helper(cfg, "map", "prev-abc", str(listener))
        assert code == 0 and said["url"] == "https://prev-abc.previews.example.com"
        method, path, body = seen[0]
        assert (method, path) == ("POST", "/config/apps/http/servers/srv0/routes")
        assert body["match"] == [{"host": ["prev-abc.previews.example.com"]}]
        assert body["handle"][0]["upstreams"] == [{"dial": f"127.0.0.1:{listener}"}]
        assert _helper(cfg, "unmap", "prev-abc")[0] == 0
        assert seen[-1][:2] == ("DELETE", "/id/boss-expose-prev-abc")
    finally:
        admin.shutdown()


# ── BOSS's side ───────────────────────────────────────────────────────────────────────────────

def _wrapper(tmp_path, cfg) -> str:
    """BOSS_EXPOSE_HELPER is an executable: a wrapper that runs the helper on this python."""
    w = tmp_path / "boss-expose-wrapper"
    w.write_text(f"#!/bin/sh\nBOSS_EXPOSE_CONFIG={cfg} exec {sys.executable} {HELPER} \"$@\"\n")
    w.chmod(0o755)
    return str(w)


def test_off_by_default_nothing_is_called(monkeypatch):
    monkeypatch.delenv(pe.ENABLE_ENV, raising=False)
    monkeypatch.setenv(pe.HELPER_ENV, "/nonexistent/boss-expose")
    assert pe.attach("prev_1", 19000) is None
    pe.detach("prev_1")                                   # no helper, no error


def test_a_preview_is_ready_only_after_its_origin_answers(tmp_path, listener, monkeypatch):
    cfg, log = _config(tmp_path, listener)
    monkeypatch.setenv(pe.ENABLE_ENV, "on")
    monkeypatch.setenv(pe.HELPER_ENV, _wrapper(tmp_path, cfg))
    probed = []
    out = pe.attach("prev_AB1", listener, probe_fn=lambda u: probed.append(u) or 200)
    assert out == {"url": "https://node.example.ts.net:8443", "label": "prev-ab1", "status": 200}
    assert probed == [out["url"]]
    pe.detach("prev_AB1")
    assert "serve --https=8443 off" in log.read_text()


def test_an_origin_that_does_not_answer_is_unmapped_and_fails(tmp_path, listener, monkeypatch):
    cfg, log = _config(tmp_path, listener)
    monkeypatch.setenv(pe.ENABLE_ENV, "on")
    monkeypatch.setenv(pe.HELPER_ENV, _wrapper(tmp_path, cfg))
    monkeypatch.setenv(pe.PROBE_ENV, "1")
    with pytest.raises(pe.ExposeFailed, match="did not answer over HTTPS"):
        pe.attach("prev_2", listener, probe_fn=lambda u: None)
    assert log.read_text().splitlines()[-1] == "serve --https=8443 off"
    assert json.loads((tmp_path / "state.json").read_text()) == {}


def test_a_missing_or_refusing_helper_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv(pe.ENABLE_ENV, "on")
    monkeypatch.setenv(pe.HELPER_ENV, str(tmp_path / "absent"))
    with pytest.raises(pe.ExposeFailed, match="not installed"):
        pe.attach("prev_3", 19000)
    cfg, _ = _config(tmp_path, 30000)
    monkeypatch.setenv(pe.HELPER_ENV, _wrapper(tmp_path, cfg))
    with pytest.raises(pe.ExposeFailed, match="outside the preview range"):
        pe.attach("prev_3", 19000)


def test_the_probe_wants_verified_https_and_an_answer():
    assert pe.probe("http://x.test", timeout=0) is None
    calls = []

    class Resp:
        status = 404

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(url, timeout):
        calls.append(url)
        if len(calls) < 2:
            raise OSError("not yet")
        return Resp()

    assert pe.probe("https://x.test", timeout=30, opener=opener, sleep=lambda s: None) == 404


def test_the_failure_is_named():
    from boss_engine.services.preview_service import _classify_start_failure

    kind, advice = _classify_start_failure("expose: x did not answer over HTTPS")
    assert kind == "expose_failed" and "boss-expose" in advice
