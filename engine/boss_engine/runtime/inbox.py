"""THE WORK INBOX: how a run reaches boss-runtime inside a microVM (boss-agent-hosts-lld.md
§3.13.0; docs/infra/HOST_API.md §4.5). The guest has NO pull loop: boss-hostd keeps the host's
one `hosts/next` request, and hands each claimed run to the VM it assigned it to, here.

    vsock port 7105 in a VM; a Unix socket per VM on the process backend. One JSON object per
    line, one answer line per request:

    host → guest  {"op":"run","run_id":"…","job_id":"…","replica_id":"…","epoch":3}
    guest → host  {"ok":true}  |  {"ok":false,"reason":"no_free_slot"|"draining"|"duplicate"|"bad_request"}
    host → guest  {"op":"ping"}  (or "slots")
    guest → host  {"ok":true,"busy_slots":1,"total_slots":4}

The guest accepts a run only while it has a free slot; the host learns the slots from the ping
it already sends every 500 ms (the watchdog's), so it never offers more than there is room for.
An accepted run is driven by runner.drive against the run plane, through the guest gateway,
which attaches the run token: the guest never holds one. A refused run is the host's to hand back.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any, Callable, Optional

from .runner import drive

log = logging.getLogger(__name__)

WORK_PORT = 7105
_REQUIRED = ("run_id", "job_id", "replica_id", "epoch")


class Inbox:
    """`handle(message) -> answer`; serve it with `serve_unix` or `serve_vsock`. `handle_for`
    turns an accepted {run_id, job_id, replica_id, epoch} into a RunHandle (the replica's
    RunPlaneClient.assigned)."""

    def __init__(self, handle_for: Callable[[dict], Any], *, total_slots: int = 4,
                 drain: Optional[threading.Event] = None,
                 on_result: Optional[Callable[[dict], None]] = None):
        self.handle_for, self.total_slots = handle_for, max(1, int(total_slots))
        self.drain = drain or threading.Event()
        self.on_result = on_result
        self._lock = threading.Lock()
        self._running: dict = {}  # run_id -> thread

    @property
    def busy_slots(self) -> int:
        with self._lock:
            return len(self._running)

    def slots(self) -> dict:
        return {"ok": True, "busy_slots": 0 if self.drain.is_set() and not self._running
                else self.busy_slots,
                "total_slots": 0 if self.drain.is_set() else self.total_slots}

    def handle(self, message: Any) -> dict:
        if not isinstance(message, dict):
            return {"ok": False, "reason": "bad_request"}
        op = message.get("op")
        if op in ("ping", "slots"):
            return self.slots()
        if op != "run" or any(message.get(k) in (None, "") for k in _REQUIRED):
            return {"ok": False, "reason": "bad_request"}
        if self.drain.is_set():
            return {"ok": False, "reason": "draining"}
        run_id = str(message["run_id"])
        with self._lock:
            if run_id in self._running:
                return {"ok": False, "reason": "duplicate"}
            if len(self._running) >= self.total_slots:
                return {"ok": False, "reason": "no_free_slot"}
            work = {k: message[k] for k in _REQUIRED}
            thread = threading.Thread(target=self._drive, args=(work,), daemon=True,
                                      name=f"run-{run_id[-8:]}")
            self._running[run_id] = thread
        thread.start()
        return {"ok": True}

    def start_local(self, run_id: str, target: Callable[[], Any]) -> Optional[str]:
        """A run the VM's own TLS listener admitted (already leased to this replica, P8b-9):
        driven in a slot like a run the host handed over. Returns a refusal reason or None."""
        if self.drain.is_set():
            return "draining"
        with self._lock:
            if run_id in self._running:
                return "duplicate"
            if len(self._running) >= self.total_slots:
                return "no_free_slot"

            def run() -> None:
                try:
                    target()
                finally:
                    with self._lock:
                        self._running.pop(run_id, None)

            thread = threading.Thread(target=run, daemon=True, name=f"run-{run_id[-8:]}")
            self._running[run_id] = thread
        thread.start()
        return None

    def _drive(self, work: dict) -> None:
        result: dict = {"run_id": work["run_id"]}
        try:
            handle = self.handle_for(work)
            package = handle.package()
            result = drive(handle, package, drain=self.drain.is_set)
        except Exception as exc:  # noqa: BLE001 — the lease expires; another replica resumes
            log.warning("run %s could not be driven: %s", work["run_id"], exc)
            result = {"run_id": work["run_id"], "error": str(exc)[:300]}
        finally:
            with self._lock:
                self._running.pop(str(work["run_id"]), None)
        if self.on_result is not None:
            self.on_result(result)

    def join(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            threads = list(self._running.values())
        for thread in threads:
            thread.join(timeout)

    # ── serving ───────────────────────────────────────────────────────────────────────────

    def _serve_connection(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as stream:
            for line in stream:
                try:
                    answer = self.handle(json.loads(line))
                except ValueError:
                    answer = {"ok": False, "reason": "bad_request"}
                stream.write(json.dumps(answer).encode() + b"\n")
                stream.flush()

    def _serve(self, listener: socket.socket, stop: threading.Event) -> None:
        listener.settimeout(0.2)
        with listener:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except (socket.timeout, TimeoutError):
                    continue
                except OSError:
                    return
                threading.Thread(target=self._serve_connection, args=(conn,),
                                 daemon=True).start()

    def serve_unix(self, path: str, stop: threading.Event) -> threading.Thread:
        """The process backend's inbox: a Unix socket the host connects to (0600)."""
        if os.path.exists(path):
            os.unlink(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        os.chmod(path, 0o600)
        listener.listen(16)
        thread = threading.Thread(target=self._serve, args=(listener, stop), daemon=True,
                                  name="work-inbox")
        thread.start()
        return thread

    def serve_vsock(self, stop: threading.Event, port: int = WORK_PORT) -> threading.Thread:
        """In a microVM: vsock port 7105, which only the host can reach."""
        listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        listener.bind((socket.VMADDR_CID_ANY, port))  # type: ignore[attr-defined]
        listener.listen(16)
        thread = threading.Thread(target=self._serve, args=(listener, stop), daemon=True,
                                  name="work-inbox")
        thread.start()
        return thread


def send(path: str, message: dict, *, timeout: float = 2.0) -> dict:
    """One request to an inbox's Unix socket (what boss-hostd does over vsock)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(path)
        conn.sendall(json.dumps(message).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data or b"{}")


__all__ = ["Inbox", "WORK_PORT", "send"]
