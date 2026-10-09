"""GET /hello for a Python app (template `hello`). No dependencies: mount `hello_app` (WSGI) or
call `greeting()` from the app's framework route."""
import json
import os


def greeting() -> str:
    return os.environ.get("HELLO_GREETING") or "hello from BOSS"


def hello_app(environ, start_response):
    if environ.get("REQUEST_METHOD") != "GET":
        start_response("405 Method Not Allowed", [("Allow", "GET")])
        return [b""]
    body = json.dumps({"message": greeting()}).encode()
    start_response("200 OK", [("Content-Type", "application/json")])
    return [body]
