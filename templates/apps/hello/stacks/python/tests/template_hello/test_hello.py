"""The `hello` template's own tests, against the HTTP route (GET /hello), not internals."""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from hello import hello_app  # noqa: E402


class _Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


def _call(method):
    server = make_server("127.0.0.1", 0, hello_app, handler_class=_Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/hello", method=method,
                                     data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
    finally:
        server.shutdown()
        server.server_close()


def test_get_hello_answers_the_greeting_as_json():
    status, body = _call("GET")
    assert status == 200
    assert json.loads(body) == {"message": os.environ.get("HELLO_GREETING") or "hello from BOSS"}


def test_post_hello_is_refused_with_405():
    status, _ = _call("POST")
    assert status == 405
