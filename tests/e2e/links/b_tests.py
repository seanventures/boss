"""App B for the F5 proof: a test app that holds only what BOSS gave it (BOSS_LINK_BANK_*), run
as a build sandbox uid. `auth` mode: sign in to A as its test user through the link (exit 0 when
it works). `full` mode: that, plus everything B must NOT reach. Prints one JSON line."""
import json
import os
import socket
import sys
import urllib.error
import urllib.request

URL = os.environ["BOSS_LINK_BANK_URL"]
PASS = os.environ["BOSS_LINK_BANK_TOKEN"]
USER = json.loads(os.environ["BOSS_LINK_BANK_USERS"])[0]


def call(method, url, body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except OSError as e:
        return 0, {"error": type(e).__name__}


def signs_in():
    s, b = call("POST", URL + "/login", {"username": USER["username"], "password": USER["password"]},
                {"x-boss-link": PASS})
    if s != 200:
        return False, s, b
    s2, me = call("GET", URL + "/me", None, {"x-boss-link": PASS, "authorization": "Bearer " + b["token"]})
    return s2 == 200 and me.get("user") == USER["username"], s2, me


def tcp(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return "open"
    except OSError as e:
        return type(e).__name__


def raw(port, request):
    host = URL.split("//")[1].split(":")[0]
    try:
        with socket.create_connection((host, port), timeout=3) as s:
            s.sendall(request.encode())
            return s.recv(200).decode(errors="replace").split("\r\n")[0]
    except OSError as e:
        return type(e).__name__


ok, status, body = signs_in()
out = {"signs_in_through_link": ok, "status": status, "reason": body.get("reason")}
if sys.argv[1:] == ["full"]:
    gw_port = int(URL.rsplit(":", 1)[1])
    out["direct"] = {
        "A_on_loopback": tcp("127.0.0.1", os.environ["A_PORT"]),
        "A_on_api_address": tcp("api", os.environ["A_PORT"]),
        "other_preview_on_loopback": tcp("127.0.0.1", os.environ["C_PORT"]),
        "database": tcp("db", 5432),
        "metadata_ip": tcp("169.254.169.254", 80),
        "console": tcp("web", 3000),
    }
    out["gateway"] = {
        "no_pass": call("GET", URL + "/me")[1].get("reason"),
        "absolute_form_to_api": raw(gw_port, f"GET http://api:8000/v1/projects HTTP/1.1\r\nHost: api\r\nx-boss-link: {PASS}\r\n\r\n"),
        "bank_pass_on_ledger_port": call("GET", os.environ["BOSS_LINK_LEDGER_URL"] + "/me", None,
                                         {"x-boss-link": PASS})[1].get("reason"),
    }
print(json.dumps(out))
sys.exit(0 if ok else 1)
