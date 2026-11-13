"""The runner: commands in, processes owned, events out. HTTP lives in server.py.

COMMANDS (each exactly once by `command_id`, through the journal):

    start   {run_id, task_run_id, session_key, argv, cwd, env, input, timeout, collect, api_base}
            -> {session_id, adopted, state}
            A start whose (run_id, session_key) matches a session whose result the API has not
            taken yet does NOT spawn: it RE-ATTACHES (adopted=true) and hands the session the
            fresh token the start carried. That is how a restarted API resumes a turn instead of
            paying for it again. A start for the same owner with a different turn (`key` differs
            after the '#') ends the stale live one first: one turn per owner at a time.
    steer   {session_id, message}  -> appended to the session's steer file + a `steered` event
    cancel  {session_id}           -> the process group is ended (SIGTERM, grace, SIGKILL)
    close   {session_id}           -> the API took the result: the session is consumed, its files go

BOOT. A session the previous runner boot was running is `lost` (its processes died with that boot)
and gets a final `exited {lost: true}`; a command still `pending` in the journal is replayed —
cancel/steer/close re-applied (each is idempotent), start marked failed (its spawn never
happened or died with that boot; the API's next start makes a new session).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .auth import RunnerAuthError, RunnerKey, TokenVerifier, run_of
from .store import LIVE_STATES, Store
from .supervisor import Supervisor
from .uplink import Transport, Uplink

_log = logging.getLogger("boss.runner")

KINDS = ("start", "steer", "cancel", "close")


class CommandError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


class Runner:
    def __init__(self, state_dir: Path | str, *, api_url: str, key_for_kid: Callable[[str], Any],
                 key: Optional[RunnerKey] = None, issuer: Optional[str] = None, interval: float = 10.0,
                 transport: Optional[Transport] = None, runner_id: Optional[str] = None):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state_dir / "runner.db")
        self.key = key or RunnerKey()
        self.runner_id = runner_id or self.store.meta("runner_id") or f"rnr_{secrets.token_hex(8)}"
        self.store.set_meta("runner_id", self.runner_id)
        self.boot_id = f"boot_{uuid.uuid4().hex[:12]}"
        self.supervisor = Supervisor(self.store, self.state_dir, boot_id=self.boot_id)
        self.verifier = TokenVerifier(key_for_kid, runner_jkt=self.key.jkt, issuer=issuer)
        self.tokens: dict[str, str] = {}
        self.uplink = Uplink(self.store, self.key, api_url=api_url, runner_id=self.runner_id, boot_id=self.boot_id,
                             tokens=self.tokens, alive=self.supervisor.alive, interval=interval, transport=transport)
        self._lock = threading.RLock()
        self.replayed = self.replay_journal()

    # -- lifecycle ------------------------------------------------------------------------------

    def start(self) -> None:
        self.uplink.start()

    def stop(self, *, end_processes: bool = True) -> None:
        self.uplink.stop()
        if end_processes:
            self.supervisor.stop_all()

    def identity(self) -> dict[str, Any]:
        return {"runner_id": self.runner_id, "boot_id": self.boot_id, "jkt": self.key.jkt,
                "jwk": self.key.public_jwk}

    def replay_journal(self) -> dict[str, int]:
        out = {"lost": 0, "replayed": 0, "failed": 0}
        for row in self.store.sessions(states=LIVE_STATES):
            if row["boot_id"] == self.boot_id:
                continue
            self.store.update_session(row["session_id"], state="lost", ended_at=time.time())
            self.store.append(row["session_id"], "exited", {"exit_code": None, "lost": True,
                                                            "reason": "the runner restarted; its process ended"})
            out["lost"] += 1
        for cmd in self.store.journal_pending():
            if cmd["kind"] == "start":
                self.store.journal_finish(cmd["command_id"], {"error": "runner_restarted_before_start"},
                                          status="failed")
                out["failed"] += 1
                continue
            try:
                result = self._apply(cmd["kind"], cmd["body"], token=None)
                self.store.journal_finish(cmd["command_id"], result)
                out["replayed"] += 1
            except CommandError as exc:
                self.store.journal_finish(cmd["command_id"], {"error": exc.code}, status="failed")
                out["failed"] += 1
        return out

    # -- commands -------------------------------------------------------------------------------

    def command(self, body: dict[str, Any], authorization: Optional[str]) -> dict[str, Any]:
        kind = str(body.get("kind") or "")
        command_id = str(body.get("command_id") or "")
        if kind not in KINDS:
            raise CommandError("unknown_kind")
        if not (8 <= len(command_id) <= 128):
            raise CommandError("command_id_required")
        if kind == "start":
            run_id = str(body.get("run_id") or "")
            if not run_id:
                raise CommandError("run_id_required")
            token, _claims = self.verifier.verify(authorization, run_id=run_id)
        else:
            row = self.store.session(str(body.get("session_id") or ""))
            if row is None:
                raise CommandError("unknown_session", 404)
            token, _claims = self.verifier.verify(authorization, run_id=row["run_id"])
        with self._lock:
            seen = self.store.journal_get(command_id)
            if seen is not None:
                if seen["kind"] != kind:
                    raise CommandError("command_id_reused", 409)
                result = dict(seen["result"] or {"status": seen["status"]})
                sid = result.get("session_id")
                if kind == "start" and sid and seen["status"] == "applied":
                    self._hand_token(sid, token)
                result["duplicate"] = True
                return result
            self.store.journal_begin(command_id, kind, _journalled(body), session_id=body.get("session_id"))
            try:
                result = self._apply(kind, body, token=token)
            except CommandError as exc:
                self.store.journal_finish(command_id, {"error": exc.code}, status="failed")
                raise
            except Exception as exc:  # noqa: BLE001 — a spawn that failed is a failed command, recorded
                self.store.journal_finish(command_id, {"error": f"{type(exc).__name__}: {exc}"[:300]},
                                          status="failed")
                raise CommandError("apply_failed", 500) from exc
            self.store.journal_finish(command_id, result, session_id=result.get("session_id"))
        self.uplink.poke()
        return result

    def _hand_token(self, session_id: str, token: Optional[str]) -> None:
        if token:
            self.tokens[session_id] = token

    def _apply(self, kind: str, body: dict[str, Any], *, token: Optional[str]) -> dict[str, Any]:
        if kind == "start":
            return self._start(body, token)
        sid = str(body.get("session_id") or "")
        row = self.store.session(sid)
        if row is None:
            raise CommandError("unknown_session", 404)
        if kind == "cancel":
            ended = self.supervisor.cancel(sid)
            return {"session_id": sid, "cancelled": ended}
        if kind == "steer":
            message = body.get("message")
            d = self.supervisor.session_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "steer.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"message": message, "at": time.time()}) + "\n")
            seq = self.store.append(sid, "steered", {"message": message})
            return {"session_id": sid, "seq": seq}
        if kind == "close":
            if row["state"] in LIVE_STATES:
                raise CommandError("session_still_running", 409)
            self.store.update_session(sid, consumed=1)
            self.store.ack(sid, 10 ** 12)
            self.tokens.pop(sid, None)
            self.supervisor.forget(sid)
            return {"session_id": sid, "closed": True}
        raise CommandError("unknown_kind")

    def _start(self, body: dict[str, Any], token: Optional[str]) -> dict[str, Any]:
        run_id = str(body["run_id"])
        key = str(body.get("session_key") or "")
        if not key:
            raise CommandError("session_key_required")
        existing = self.store.unconsumed_for(run_id, key)
        if existing is not None:
            # RE-ATTACH: the same turn of the same run, still running or finished but not taken.
            self._hand_token(existing["session_id"], token)
            self.store.update_session(existing["session_id"], api_base=str(body.get("api_base") or "")
                                      or existing.get("api_base"))
            return {"session_id": existing["session_id"], "adopted": True, "state": existing["state"],
                    "cwd": existing["cwd"]}
        owner = key.split("#", 1)[0]
        for stale in self.store.unconsumed_of_run(run_id):
            if stale["session_key"].split("#", 1)[0] == owner:
                # A different turn for the same owner: the old one is history (one turn at a time).
                self.supervisor.cancel(stale["session_id"])
                self.store.update_session(stale["session_id"], consumed=1)
        argv = [str(a) for a in body.get("argv") or []]
        if not argv:
            raise CommandError("argv_required")
        env = body.get("env")
        if env is not None and not isinstance(env, dict):
            raise CommandError("bad_env")
        sid = f"rs_{uuid.uuid4().hex}"
        timeout = body.get("timeout")
        self.store.create_session(
            session_id=sid, run_id=run_id, task_run_id=body.get("task_run_id"), session_key=key,
            boot_id=self.boot_id, cwd=str(body.get("cwd") or ""), argv=argv,
            collect=[str(p) for p in body.get("collect") or []], api_base=str(body.get("api_base") or ""),
            deadline=None)
        self._hand_token(sid, token)
        self.supervisor.spawn(sid, argv=argv, cwd=str(body.get("cwd") or "") or os.getcwd(),
                              env={str(k): str(v) for k, v in env.items()} if env is not None else None,
                              stdin_text=body.get("input"), timeout=float(timeout) if timeout else None)
        return {"session_id": sid, "adopted": False, "state": "running", "cwd": str(body.get("cwd") or "")}

    # -- reads ----------------------------------------------------------------------------------

    def status(self, session_id: str, authorization: Optional[str]) -> dict[str, Any]:
        row = self._authorized(session_id, authorization)
        return self._public(row)

    def result(self, session_id: str, authorization: Optional[str]) -> dict[str, Any]:
        row = self._authorized(session_id, authorization)
        if row["state"] in LIVE_STATES:
            raise CommandError("session_still_running", 409)
        return {**self._public(row), **self.supervisor.result(session_id)}

    def _authorized(self, session_id: str, authorization: Optional[str]) -> dict[str, Any]:
        row = self.store.session(session_id)
        if row is None:
            raise CommandError("unknown_session", 404)
        self.verifier.verify(authorization, run_id=row["run_id"])
        return row

    def _public(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"session_id": row["session_id"], "run_id": row["run_id"], "task_run_id": row["task_run_id"],
                "state": row["state"], "alive": self.supervisor.alive(row["session_id"]),
                "exit_code": row["exit_code"], "timed_out": bool(row["timed_out"]),
                "cancelled": bool(row["cancelled"]), "consumed": bool(row["consumed"]),
                "next_seq": row["next_seq"], "acked_seq": row["acked_seq"], "cwd": row["cwd"],
                "started_at": row["started_at"], "boot_id": row["boot_id"], "runner_id": self.runner_id}


def _journalled(body: dict[str, Any]) -> dict[str, Any]:
    """What the journal keeps of a command: never its environment (it can hold the CLI's
    credential) nor its stdin (the prompt) — a replay needs neither."""
    return {k: v for k, v in body.items() if k not in ("env", "input")}


__all__ = ["CommandError", "Runner", "RunnerAuthError", "run_of"]
