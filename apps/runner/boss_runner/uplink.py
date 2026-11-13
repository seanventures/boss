"""The runner's push channel to the API: the outbox, a heartbeat, and run-token renewal.

For every session whose result the API has not taken yet, every `interval` seconds (and at once
when there is something new): POST the events after the ack to

    POST {api}/v1/runner/sessions/{session_id}/events
    Authorization: DPoP <the session's runner-bound run token>
    DPoP: <proof for POST + {api_base}/v1/runner/sessions/{session_id}/events>
    {"runner_id", "boot_id", "task_run_id", "state", "alive", "events": [{"seq","type","data"}]}

and apply the API's cumulative answer `{"ack": n}`. An empty `events` list is the heartbeat. A
push that fails (the API is restarting) changes nothing: the same events go again next time, so
the API's view resumes at exactly the seq it acknowledged.

Run tokens live 5 minutes (delegation.RUN_TOKEN_TTL_SECONDS); one within RENEW_BEFORE of expiry is
renewed at /v1/identity/token (grant_type run-renewal, a proof by the runner's key). A token that
expired while the API was away cannot be renewed: the session keeps running, and the next start
command for it (the restarted API re-attaching) brings a fresh one.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from .auth import RunnerKey, unverified_claims
from .store import Store

_log = logging.getLogger("boss.runner.uplink")

EVENTS_PATH = "/v1/runner/sessions/{session_id}/events"
TOKEN_PATH = "/v1/identity/token"
GRANT_TYPE_RENEW = "urn:boss:grant-type:run-renewal"     # identity/tokens.GRANT_TYPE_RENEW
RENEW_BEFORE_SECONDS = 90

#: (method, url, headers, body bytes, content type) -> (status, parsed JSON body)
Transport = Callable[[str, str, dict[str, str], bytes, str], tuple[int, Any]]


def http_transport(method: str, url: str, headers: dict[str, str], body: bytes, ctype: str) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=body, method=method, headers={**headers, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — the configured API URL
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None


class Uplink:
    def __init__(self, store: Store, key: RunnerKey, *, api_url: str, runner_id: str, boot_id: str,
                 tokens: dict[str, str], alive: Callable[[str], bool], interval: float = 10.0,
                 transport: Optional[Transport] = None):
        self.store = store
        self.key = key
        self.api_url = api_url.rstrip("/")
        self.runner_id = runner_id
        self.boot_id = boot_id
        self.tokens = tokens            # session_id -> current run token (memory only)
        self.alive = alive
        self.interval = interval
        self.transport = transport or http_transport
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.reachable: Optional[bool] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="boss-runner-uplink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def poke(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.push_all()
            except Exception:  # noqa: BLE001 — the loop must outlive any one bad push
                _log.warning("runner uplink pass failed", exc_info=True)
            self._wake.wait(self.interval)
            self._wake.clear()

    # -- one pass -------------------------------------------------------------------------------

    def push_all(self) -> dict[str, int]:
        acks: dict[str, int] = {}
        for row in self.store.sessions():
            if row["consumed"] or row["session_id"] not in self.tokens:
                continue
            ack = self.push(row)
            if ack is not None:
                acks[row["session_id"]] = ack
        return acks

    def push(self, row: dict[str, Any]) -> Optional[int]:
        sid = row["session_id"]
        token = self._fresh_token(sid)
        if token is None:
            return None
        events = self.store.pending(sid)
        path = EVENTS_PATH.format(session_id=urllib.parse.quote(sid, safe=""))
        base = (row.get("api_base") or self.api_url).rstrip("/")
        body = json.dumps({"runner_id": self.runner_id, "boot_id": self.boot_id, "task_run_id": row.get("task_run_id"),
                           "state": row["state"], "alive": self.alive(sid), "events": events}).encode("utf-8")
        try:
            status, answer = self.transport("POST", self.api_url + path, self.key.headers(token, "POST", base + path),
                                            body, "application/json")
        except Exception:  # noqa: BLE001 — the API is away: keep everything, try again later
            self.reachable = False
            return None
        self.reachable = status < 500
        if status != 200 or not isinstance(answer, dict) or "ack" not in answer:
            _log.info("runner push for %s answered %s", sid, status)
            return None
        return self.store.ack(sid, int(answer["ack"]))

    def _fresh_token(self, sid: str) -> Optional[str]:
        token = self.tokens.get(sid)
        if not token:
            return None
        try:
            exp = int(unverified_claims(token).get("exp", 0))
        except Exception:  # noqa: BLE001
            return None
        now = time.time()
        if exp <= now:
            return None             # expired: wait for the API to hand a fresh one with a start
        if exp - now > RENEW_BEFORE_SECONDS:
            return token
        url = self.api_url + TOKEN_PATH
        body = urllib.parse.urlencode({"grant_type": GRANT_TYPE_RENEW, "subject_token": token}).encode("ascii")
        headers = {"DPoP": self.key.headers(token, "POST", url)["DPoP"]}
        try:
            status, answer = self.transport("POST", url, headers, body, "application/x-www-form-urlencoded")
        except Exception:  # noqa: BLE001
            return token
        if status == 200 and isinstance(answer, dict) and answer.get("access_token"):
            # Renewal is single-use: the renewed token replaces the old one at once.
            self.tokens[sid] = str(answer["access_token"])
            return self.tokens[sid]
        return token
