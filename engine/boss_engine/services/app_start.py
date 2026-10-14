"""THE RECORD of every process BOSS starts to check an app: what ran, where, with which variables
(names and where each came from — never a value), how it ended, and the app's OWN output.

WHY. HelpDesk AI (crun_f246d33024654f889d6a, 2026-10-14) ended blocked: "The backend (npm install
--no-audit --no-fund --loglevel=error && npm start) exited (code 1) before it answered HTTP. Its
output ended: up to date in 142ms." That is npm install's last line. The app had printed
"[helpdesk-ai] failed to start: Error: <reason>" and exited 1, and nothing BOSS kept said so: the
one-line headline took the line after "ended:", app_logs held only the sandbox's per-command
summaries (the start is not a sandbox command), and no event was written on that path. The
Shepherd saw "class unknown". The agent's own boot test had started the same app — so BOSS's
start environment differed (it gave the multi-service backend no declared environment at all:
stack_verify launched it with HOME only) — and nobody could see how.

So, for every start (the serve proof, a multi-service backend in verify and the template gate, a
preview):
  * the command is MARKED (`marked`): an install step chained in front of the start (`npm install
    && npm start`) prints a marker line before the start, so the app's own output is told apart
    from the install's (`split_output`) in the same log;
  * `record` keeps command, cwd, the variables BOSS set by NAME with their source (generated /
    build services / bound / launcher / unset / needs a secret), exit code, the app's last error
    lines and its last TAIL_LINES lines, redacted (identity.redaction plus every value BOSS gave
    the launch);
  * `publish` writes it to app_logs (with the run id) and as a `code_task.app_start` event;
  * `quote` is what a failure message says: the app's own last error lines.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

log = logging.getLogger(__name__)

#: Printed between an install step and the app's start, so the two outputs are told apart.
MARKER = "[boss] ---- the app output starts here ----"  # no quote: it is printed in '...'
#: How much of the app's own output a record keeps.
TAIL_LINES = 200
#: The event every app start writes.
EVENT = "code_task.app_start"
#: app_logs.message holds at most this much per row (log_capture's limit).
_ROW_LIMIT = 3800

#: A leading install step: npm/pnpm/yarn install|ci, pip install (also `python3 -m pip`), with an
#: optional path in front of the tool (`./npm`, `/usr/bin/npm`).
_INSTALL = re.compile(
    r"^\s*((?:\S*/)?(?:npm|pnpm|yarn)\s+(?:install|ci|i)\b[^&|;]*"
    r"|(?:(?:\S*/)?python3?\s+-m\s+)?(?:\S*/)?pip3?\s+install\b[^&|;]*)\s*&&\s*(.+)$",
    re.DOTALL)
#: A `cd <dir> &&` in front (the serve proof runs a declared command from its app's directory).
_CD = re.compile(r"^\s*(cd\s+[^&|;]+?)\s*&&\s*(.+)$", re.DOTALL)
_SHELLS = ("sh", "bash", "/bin/sh", "/bin/bash", "/usr/bin/bash", "/usr/bin/sh")

#: Lines that carry the reason a start died (not a stack frame under it).
_ERROR_LINE = re.compile(
    r"error|fail|fatal|refus|cannot|can't|must be (?:set|provided|defined)|is (?:not set|required|"
    r"missing)|missing|not found|denied|invalid|exception|traceback|panic|eaddrinuse", re.I)
_FRAME = re.compile(r"^\s*(?:at |File \"|\^|~)")


def marked(command: list[str]) -> list[str]:
    """`command` with MARKER printed between its leading install step(s) and the start. A command
    that is not a shell script with an install in front is returned unchanged."""
    if len(command) < 3 or command[0] not in _SHELLS or not str(command[-2]).startswith("-") \
            or "c" not in str(command[-2]):
        return list(command)
    script = str(command[-1])
    front: list[str] = []
    installs = 0
    rest = script
    while True:
        m = _INSTALL.match(rest)
        if m:
            installs += 1
        else:
            m = _CD.match(rest)
            if not m:
                break
        front.append(m.group(1).strip())
        rest = m.group(2)
    if not installs or MARKER in script:
        return list(command)
    joined = " && ".join(front)
    return [*command[:-1], f"{joined} && printf '%s\\n' '{MARKER}' && {rest}"]


def split_output(text: str) -> tuple[str, str]:
    """(the install's output, the app's own output) of one start's log. With no marker the whole
    log is the app's."""
    text = text or ""
    at = text.rfind(MARKER)
    if at < 0:
        return "", text
    return text[:at], text[at + len(MARKER):].lstrip("\n")


def _lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]


def error_lines(app_text: str, limit: int = 3) -> list[str]:
    """The app's last lines that say why it stopped (its own words), else its last line."""
    lines = [ln for ln in _lines(app_text) if MARKER not in ln]
    culprits = [ln.strip() for ln in lines if _ERROR_LINE.search(ln) and not _FRAME.match(ln)
                and not re.match(r"^\s*>\s", ln)]  # npm's "> app@1 start" echo is not a reason
    if culprits:
        # The app's own lines first: npm adds an epilogue ("npm error Lifecycle script `start`
        # failed …") after the app's reason, and that is not the reason.
        own = [c for c in culprits if not re.match(r"(?i)^npm (?:error|err!|warn)", c)]
        return (own or culprits)[-limit:]
    return [lines[-1].strip()] if lines else []


def quote(record: Optional[Mapping[str, Any]]) -> str:
    """The app's own words for why it stopped, one line: "[helpdesk-ai] failed to start: Error:
    SESSION_SECRET must be set…". Empty when it printed nothing."""
    rec = record or {}
    return " | ".join(str(x) for x in rec.get("error_lines") or [])[:500]


def env_sources(*, run_env: Any = None, launcher: Iterable[str] = ("PORT", "HOST", "HOME"),
                build_services: Optional[Mapping[str, str]] = None,
                extra: Optional[Mapping[str, str]] = None,
                extra_source: str = "set by BOSS for this launch") -> dict[str, str]:
    """{NAME: where its value came from} for every variable BOSS set or decided for this start.
    Names only: no value is read into the result."""
    out: dict[str, str] = {}
    for name in launcher:
        out[name] = "launcher"
    for name in (extra or {}):
        out.setdefault(name, extra_source)
    if run_env is not None:
        values = getattr(run_env, "values", {}) or {}
        generated = set(getattr(run_env, "generated", []) or [])
        shown = set(getattr(run_env, "shown", []) or [])
        supplied = set(getattr(run_env, "supplied", []) or [])
        declared = getattr(run_env, "declared", {}) or {}
        for name in values:
            if name in generated:
                out[name] = "generated (demo login)" if name in shown else "generated"
            elif name in supplied:
                out[name] = "build services"
            else:
                out[name] = "bound"
        for name in supplied:
            out.setdefault(name, "launcher" if name in ("PORT", "HOST") else "build services")
        for name in getattr(run_env, "unset", []) or []:
            need = declared.get(name)
            out[name] = f"unset (declared in {need.source})" if need is not None else "unset"
        for need in getattr(run_env, "needs", []) or []:
            out[need.name] = f"needs a secret ({need.where()})"
    for name in (build_services or {}):
        out[name] = "build services"
    return dict(sorted(out.items()))


def redactor(secrets: Iterable[str]):
    values = sorted({str(v) for v in secrets if isinstance(v, str) and len(v) >= 6},
                    key=len, reverse=True)

    def redact(text: str) -> str:
        out = text or ""
        for v in values:
            if v in out:
                out = out.replace(v, "[REDACTED]")
        try:
            from ..identity.redaction import redact_text

            out = redact_text(out)
        except Exception:  # noqa: BLE001 — the literal pass above still ran
            pass
        try:
            from .redaction import redact_secrets

            out = redact_secrets(out)
        except Exception:  # noqa: BLE001
            pass
        return out
    return redact


def record(*, path: str, command: Any, cwd: Any, env: Mapping[str, str], log_text: str,
           exit_code: Optional[int], ok: bool, error: Optional[str] = None,
           secrets: Iterable[str] = (), label: Optional[str] = None) -> dict:
    """One start's record. `secrets` are the values BOSS gave the launch: each is redacted from
    the output kept (an app that prints its environment must not put a key in app_logs)."""
    redact = redactor(secrets)
    install_text, app_text = split_output(log_text)
    app_lines = [ln for ln in _lines(redact(app_text)) if MARKER not in ln]
    cmd = command[-1] if isinstance(command, (list, tuple)) and command else command
    rec = {
        "path": path, "label": label or path, "ok": bool(ok),
        "command": str(cmd or ""), "cwd": str(cwd or ""),
        "env": dict(env), "exit_code": exit_code,
        "error_lines": [] if ok else error_lines("\n".join(app_lines)),
        "output_tail": app_lines[-TAIL_LINES:],
        "install_tail": _lines(redact(install_text))[-5:],
    }
    if error:
        rec["error"] = redact(str(error))[:1500]
    return rec


def env_line(rec: Mapping[str, Any]) -> str:
    """"SESSION_SECRET (generated), DATABASE_URL (build services), ..." for a message."""
    env = rec.get("env") or {}
    return ", ".join(f"{k} ({v})" for k, v in env.items()) or "(none)"


def summary(rec: Mapping[str, Any]) -> str:
    """The record's one line, for app_logs and a finding."""
    head = (f"app start ({rec.get('label') or rec.get('path')}): `{rec.get('command')}` in "
            f"{rec.get('cwd')}")
    if rec.get("ok"):
        how = "answered HTTP"
    elif rec.get("exit_code") is not None:
        how = f"exited (code {rec.get('exit_code')})"
    else:
        how = "did not answer HTTP"
    said = quote(rec)
    return (f"{head} {how}" + (f": {said}" if said else "")
            + f". Environment BOSS set: {env_line(rec)}")


def log_rows(rec: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(level, message) rows for app_logs: the summary, then the app's own output tail."""
    level = "info" if rec.get("ok") else "error"
    rows = [(level, summary(rec)[:_ROW_LIMIT])]
    tail = list(rec.get("output_tail") or [])
    if not rec.get("ok") and tail:
        chunk: list[str] = []
        size = 0
        head = "the app's own output (last lines):\n"
        for line in tail:
            if size + len(line) + 1 > _ROW_LIMIT - len(head) and chunk:
                rows.append((level, head + "\n".join(chunk)))
                chunk, size = [], 0
            chunk.append(line[:_ROW_LIMIT - len(head)])
            size += len(line) + 1
        if chunk:
            rows.append((level, head + "\n".join(chunk)))
    return rows


def publish(rec: Mapping[str, Any], *, engine: Any = None, project_id: Optional[str] = None,
            run_id: Optional[str] = None, entity_type: str = "code_task_run",
            entity_id: Optional[str] = None, service: str = "boss-app-start") -> bool:
    """Write the record to app_logs (with the run id) and as a code_task.app_start event. The run
    is the caller's, else the current one (llm_telemetry's context). Never raises."""
    try:
        if engine is None or project_id is None or run_id is None:
            from .llm_telemetry import get_llm_context

            ctx = get_llm_context() or {}
            engine = engine or ctx.get("db_engine")
            project_id = project_id or ctx.get("project_id")
            run_id = run_id or ctx.get("run_id")
        # INFO: the container log's copy. Not WARNING — log_capture would store it in app_logs a
        # second time, without the app's output; the rows below are the Activity Log's.
        log.info("%s", summary(rec)[:_ROW_LIMIT])
        if engine is None or not project_id:
            return False
        from ..db import begin
        from ..ports import sql
        from .core import emit_event, make_id

        with begin(engine) as c:
            for level, message in log_rows(rec):
                c.execute(sql("INSERT INTO app_logs (id, project_id, run_id, service, level, "
                              "logger, message) VALUES (:id,:p,:r,:s,:lv,:lg,:m)"),
                          {"id": make_id("alog"), "p": project_id, "r": run_id, "s": service,
                           "lv": level, "lg": __name__, "m": message})
            emit_event(c, project_id, EVENT, "boss", entity_type, entity_id or run_id or "",
                       dict(rec))
        return True
    except Exception:  # noqa: BLE001 — a record that cannot be stored never fails the check
        log.debug("app start record not stored", exc_info=True)
        return False


def read_log(path: Any) -> str:
    try:
        return Path(str(path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def latest(connection: Any, *, project_id: str, run_id: str) -> Optional[dict]:
    """The run's latest app-start record (its code_task.app_start event), or None."""
    from ..ports import sql

    data = connection.execute(
        sql("SELECT data FROM events WHERE project_id = :p AND entity_id = :r "
            "AND event_type = :t ORDER BY created_at DESC LIMIT 1"),
        {"p": project_id, "r": run_id, "t": EVENT}).scalar()
    if isinstance(data, str):
        import json

        try:
            data = json.loads(data)
        except ValueError:
            return None
    return dict(data) if isinstance(data, dict) else None


def finding(rec: Optional[Mapping[str, Any]]) -> Optional[str]:
    """A diagnosis line for a failed start: the app's own error, what ran, what BOSS set."""
    if not rec or rec.get("ok"):
        return None
    said = quote(rec) or "it printed nothing"
    code = rec.get("exit_code")
    how = f"exited (code {code})" if code is not None else "did not answer HTTP"
    last = list(rec.get("output_tail") or [])[-8:]
    return (f"THE APP {how.upper()} AT START ({rec.get('label') or rec.get('path')}) — its own "
            f"error: {said}. BOSS ran `{rec.get('command')}` in {rec.get('cwd')} with "
            f"{env_line(rec)}." + (" Its last lines: " + " / ".join(last) if last else ""))


__all__ = ["EVENT", "MARKER", "TAIL_LINES", "env_line", "env_sources", "error_lines", "finding",
           "latest", "log_rows", "marked", "publish", "quote", "read_log", "record",
           "split_output", "summary"]
