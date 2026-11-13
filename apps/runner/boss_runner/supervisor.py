"""The runner OWNS the harness processes: it spawns them, pumps their output, enforces their
deadline and ends them — the same rules as services/owned_process.py, one process group per turn.

* every turn is its own session (`start_new_session=True`), so its pgid ends the whole tree;
* stdout/stderr go to the session's log files (the full output the result carries) AND, in
  chunks, to the outbox as `stdout`/`stderr` events (progress the API sees);
* ending — exit, deadline, cancel — is SIGTERM to the group, a grace, SIGKILL;
* `exited` is the last event of a session; the result (exit code, output, collected files) is
  read from the session directory, so it survives any number of API restarts until the API
  takes it (`close`).
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .store import Store

_log = logging.getLogger("boss.runner.supervisor")

GRACE_SECONDS = 5.0
CHUNK_BYTES = 4096
#: A collected file larger than this is truncated in the result (a final-message file is small).
MAX_COLLECT_BYTES = 1024 * 1024


class Supervisor:
    def __init__(self, store: Store, root: Path, *, boot_id: str):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.boot_id = boot_id
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id

    # -- spawn ----------------------------------------------------------------------------------

    def spawn(self, session_id: str, *, argv: list[str], cwd: str, env: Optional[dict[str, str]],
              stdin_text: Optional[str], timeout: Optional[float]) -> int:
        d = self.session_dir(session_id)
        d.mkdir(parents=True, exist_ok=True)
        out = open(d / "stdout.log", "wb")
        err = open(d / "stderr.log", "wb")
        try:
            proc = subprocess.Popen(  # noqa: S603 — argv list from an authenticated API command
                argv, cwd=cwd or None, env=env if env is not None else None,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except BaseException:
            out.close()
            err.close()
            raise
        started = time.time()
        self.store.update_session(session_id, state="running", pid=proc.pid, pgid=proc.pid, started_at=started,
                                  deadline=(started + float(timeout)) if timeout else None)
        self.store.append(session_id, "started", {"pid": proc.pid, "boot_id": self.boot_id})
        with self._lock:
            self._procs[session_id] = proc
        pumps = [threading.Thread(target=self._pump, args=(session_id, proc.stdout, out, "stdout"), daemon=True),
                 threading.Thread(target=self._pump, args=(session_id, proc.stderr, err, "stderr"), daemon=True)]
        for t in pumps:
            t.start()
        if stdin_text is not None:
            threading.Thread(target=self._feed, args=(proc, stdin_text), daemon=True).start()
        threading.Thread(target=self._reap, args=(session_id, proc, pumps), daemon=True).start()
        return proc.pid

    def _feed(self, proc: subprocess.Popen, text: str) -> None:
        with contextlib.suppress(Exception):
            proc.stdin.write(text.encode("utf-8"))
        with contextlib.suppress(Exception):
            proc.stdin.close()

    def _pump(self, session_id: str, stream: Any, sink: Any, kind: str) -> None:
        try:
            while True:
                chunk = stream.read1(CHUNK_BYTES) if hasattr(stream, "read1") else stream.read(CHUNK_BYTES)
                if not chunk:
                    break
                sink.write(chunk)
                sink.flush()
                with contextlib.suppress(Exception):
                    self.store.append(session_id, kind, {"text": chunk.decode("utf-8", errors="replace")})
        finally:
            with contextlib.suppress(Exception):
                sink.close()

    def _reap(self, session_id: str, proc: subprocess.Popen, pumps: list[threading.Thread]) -> None:
        timed_out = False
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                row = self.store.session(session_id) or {}
                deadline = row.get("deadline")
                if deadline and time.time() > float(deadline):
                    timed_out = True
                    self._end_group(proc.pid)
        # The group may outlive its leader (an agent's `npm run dev &`): end it either way.
        self._end_group(proc.pid, grace=1.0)
        for t in pumps:
            t.join(timeout=5)
        row = self.store.session(session_id) or {}
        collected = self._collect(json.loads(row.get("collect") or "[]"))
        (self.session_dir(session_id) / "collected.json").write_text(json.dumps(collected), encoding="utf-8")
        self.store.update_session(session_id, state="exited", exit_code=proc.returncode,
                                  timed_out=1 if timed_out else 0, ended_at=time.time())
        self.store.append(session_id, "exited", {"exit_code": proc.returncode, "timed_out": timed_out})
        with self._lock:
            self._procs.pop(session_id, None)

    @staticmethod
    def _collect(paths: list[str]) -> list[Optional[str]]:
        out: list[Optional[str]] = []
        for p in paths:
            try:
                with open(p, "rb") as fh:
                    out.append(fh.read(MAX_COLLECT_BYTES).decode("utf-8", errors="replace"))
            except OSError:
                out.append(None)
        return out

    # -- end ------------------------------------------------------------------------------------

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _end_group(self, pgid: Optional[int], *, grace: float = GRACE_SECONDS) -> None:
        if not pgid:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        end = time.monotonic() + grace
        while time.monotonic() < end and self._group_alive(pgid):
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)

    def cancel(self, session_id: str) -> bool:
        row = self.store.session(session_id)
        if row is None or row["state"] not in ("starting", "running"):
            return False
        self.store.update_session(session_id, cancelled=1)
        self.store.append(session_id, "cancelling", {})
        threading.Thread(target=self._end_group, args=(row.get("pgid"),), daemon=True).start()
        return True

    def alive(self, session_id: str) -> bool:
        """Does THIS runner boot hold the session's process, still running?"""
        with self._lock:
            proc = self._procs.get(session_id)
        return proc is not None and proc.poll() is None

    def result(self, session_id: str, *, max_bytes: Optional[int] = None) -> dict[str, Any]:
        d = self.session_dir(session_id)

        def read(name: str) -> str:
            try:
                data = (d / name).read_bytes()
            except OSError:
                return ""
            if max_bytes is not None:
                data = data[-max_bytes:]
            return data.decode("utf-8", errors="replace")

        try:
            collected = json.loads((d / "collected.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            collected = []
        return {"stdout": read("stdout.log"), "stderr": read("stderr.log"), "collected": collected}

    def forget(self, session_id: str) -> None:
        import shutil

        shutil.rmtree(self.session_dir(session_id), ignore_errors=True)

    def stop_all(self) -> None:
        with self._lock:
            procs = list(self._procs.items())
        for _sid, proc in procs:
            self._end_group(proc.pid, grace=1.0)
