"""Did the app's tests FAIL, or could the suite not RUN? Read from the failures' structured data.

THE CASE (crun_de96bf12, helpdesk-ai-demo, 2026-11-06). A follow-up build ran the app's full
suite; its setup hook could not log in to the test database BOSS had given the run (the
build-services server had restarted empty, build_services.ensure). 119 of 190 tests "failed",
every one in the same hook with the same SQLSTATE 28P01. The build report, the project's Tests
ring and the onboarding agent all said "119 failing tests", and onboarding proposed "Fix the 119
failing tests" as the first work: a BOSS environment fault, handed to the person as app bugs.

WHAT THIS DECIDES. Each failure the runner reports carries structure next to its message: the
node:test/TAP block's `code` and `failureType`, pytest's exception class on its summary line, a
driver's SQLSTATE. A failure is ENVIRONMENTAL when that structure says the suite could not reach
what it runs against, by its standard class, not by its words:

  * SQLSTATE class 08 (connection exception), 28 (invalid authorization: the login was
    refused), 3D (the database does not exist), 53 (insufficient resources), 57P (the server is
    shutting down / restarting);
  * an operating-system network errno (connection refused/reset, host unreachable, name not
    resolved, timed out);
  * a DB-API ``OperationalError`` (PEP 249: "errors related to the database's operation and not
    necessarily under the control of the programmer") or a Python ``ConnectionError``.

The suite could not run when the failures that say anything mostly share ONE such cause
(ONE_CAUSE_SHARE). Then BOSS says "BOSS couldn't run the full suite: <reason>" and owns it; it
never reports those failures as the app's. An assertion, a TypeError in the app's code or a
missing module is the app's, whatever its message says.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Mapping, Optional

from sqlalchemy.engine import Connection

#: The share of the failures with a cause that must share the one environmental cause.
ONE_CAUSE_SHARE = 0.6

#: SQLSTATE classes that mean "could not reach / use the database", and what each says.
_SQLSTATE_CLASSES = (
    ("28", "login", "the test database refused the login"),
    ("08", "connect", "the suite could not connect to its test database"),
    ("3D", "missing", "the suite's test database does not exist"),
    ("53", "resources", "the test database ran out of resources"),
    ("57P", "restarting", "the test database was shutting down or restarting"),
)
#: POSIX/libuv network errnos: the service the suite needs did not answer.
_NETWORK_ERRNOS = frozenset({
    "ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "EHOSTUNREACH", "ENETUNREACH", "ENOTFOUND",
    "EAI_AGAIN", "ETIMEDOUT", "EPIPE",
})
#: Exception classes whose type (not message) says the environment failed.
_ENV_EXCEPTIONS = frozenset({
    "OperationalError", "InterfaceError", "ConnectionError", "ConnectionRefusedError",
    "ConnectionResetError", "ConnectionAbortedError", "gaierror", "ConnectionFailure",
    "ServerSelectionTimeoutError", "ConnectionTimeoutError",
})

_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
_TAP_BLOCK = re.compile(r"^not ok \d+ - (?P<name>.*?)\n(?P<body>(?:(?!^(?:not )?ok \d+ ).*\n?)*)",
                        re.M)
_YAML_FIELD = re.compile(r"^\s+(code|failureType|name|error):\s*(?P<v>.+?)\s*$", re.M)
#: pytest's short summary: "FAILED tests/x.py::t - psycopg.OperationalError: connection …"
_PYTEST_LINE = re.compile(r"^(?:FAILED|ERROR) (?P<test>\S+)(?: - (?P<exc>[\w.]+?)(?::|$)(?P<msg>.*))?$",
                          re.M)
#: A SQLSTATE or errno a driver printed inside a message ("SQLSTATE 28P01", "code: 'ECONN…'").
_INLINE_CODE = re.compile(r"(?:SQLSTATE[ =:]*|code[\"']?\s*[:=]\s*[\"']?)([0-9A-Z]{5}|E[A-Z_]{3,})\b")
_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'|\b[0-9a-f]{8,}\b|\b\d+\b")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def classify_code(code: Optional[str], exc: Optional[str] = None) -> Optional[dict[str, str]]:
    """{kind, reason} when this structured code / exception class is environmental, else None."""
    code = (code or "").strip().upper()
    if code and _SQLSTATE.match(code):
        for prefix, kind, reason in _SQLSTATE_CLASSES:
            if code.startswith(prefix):
                return {"kind": kind, "reason": reason, "code": code}
    if code in _NETWORK_ERRNOS:
        return {"kind": "connect", "reason": "the suite could not reach a service it needs",
                "code": code}
    leaf = (exc or "").rsplit(".", 1)[-1]
    if leaf in _ENV_EXCEPTIONS:
        return {"kind": "connect", "reason": "the suite could not reach a service it needs",
                "code": leaf}
    return None


def failures(output: Any) -> list[dict[str, str]]:
    """Each failure the output reports, with its structured fields: {test, code, exc, type,
    error}. node:test/TAP blocks and pytest summary lines; others give nothing (never a guess)."""
    out = str(output or "")
    found: list[dict[str, str]] = []
    for m in _TAP_BLOCK.finditer(out):
        fields = {k: _unquote(v) for k, v in
                  ((f.group(1), f.group("v")) for f in _YAML_FIELD.finditer(m.group("body")))}
        found.append({"test": m.group("name").strip(), "code": fields.get("code", ""),
                      "exc": fields.get("name", ""), "type": fields.get("failureType", ""),
                      "error": fields.get("error", "")})
    for m in _PYTEST_LINE.finditer(out):
        msg = (m.group("msg") or "").strip()
        inline = _INLINE_CODE.search(msg)
        found.append({"test": m.group("test"), "code": inline.group(1) if inline else "",
                      "exc": m.group("exc") or "", "type": "", "error": msg})
    return found


def _phrase(error: str) -> str:
    """The error without its identifiers, numbers and quoted values (a generated name is not
    news): "password authentication failed for user"."""
    return " ".join((_QUOTED.sub("", error).strip(" :,.") or error).split())[:120]


def classify(output: Any, *, boss_service: bool = False) -> Optional[dict[str, Any]]:
    """When the suite could not RUN: {reason, sentence, kind, code, error, shared, failures,
    owner}. None when the failures are the app's (or nothing says).

    `boss_service`: the run's database was BOSS's own (build_services) — the fault is BOSS's to
    fix, never the person's. A generated build-services role named in the error says the same."""
    seen = failures(output)
    if not seen:
        return None
    env = []
    for f in seen:
        verdict = classify_code(f["code"], f["exc"])
        if verdict:
            env.append((f, verdict))
    # Failures that carry a cause at all (a code, an exception class or an error line).
    said = [f for f in seen if f["code"] or f["exc"] or f["error"]]
    if not env or not said:
        return None
    key, n = Counter((v["kind"], v["code"]) for _, v in env).most_common(1)[0]
    if n / len(said) < ONE_CAUSE_SHARE:
        return None
    first_f, first_v = next((f, v) for f, v in env if (v["kind"], v["code"]) == key)
    from .build_services import DEFAULT_PREFIX, LEGACY_PREFIXES

    ours = boss_service or any(p in first_f["error"] for p in (DEFAULT_PREFIX, *LEGACY_PREFIXES))
    reason = first_v["reason"] + (" BOSS gave it" if ours and first_v["kind"] == "login" else "")
    return {"reason": reason,
            "sentence": f"BOSS couldn't run the full suite: {reason}.",
            "kind": first_v["kind"], "code": first_v["code"],
            "error": _phrase(first_f["error"]) if first_f["error"] else "",
            "shared": n, "failures": len(said),
            "owner": "boss" if ours else "environment"}


# ── BOSS owns it: the build, and the Self-fixes list ─────────────────────────────────────

EVENT = "code_task.suite_could_not_run"


def signature(env: Mapping[str, Any]) -> str:
    """One recommendation per cause class, not per run: suite_environment:login:28P01."""
    return f"suite_environment:{env.get('kind') or 'unknown'}:{env.get('code') or 'none'}"


def report(connection: Connection, settings: Any, *, project_id: str, env: Mapping[str, Any],
           build_id: str, restored: str = "") -> dict[str, Any]:
    """Put it where BOSS's own faults go (boss_recommendations → Self-fixes): one row per cause,
    each build that hit it a ref. Never raises (record() does not)."""
    from . import boss_recommendations

    ours = env.get("owner") == "boss"
    return boss_recommendations.record(
        connection, settings, project_id=project_id, signature=signature(env),
        title=f"BOSS couldn't run the app's full suite: {env.get('reason')}",
        symptom=(f"{env.get('shared')} of {env.get('failures')} reported failures share one "
                 f"environmental cause ({env.get('code')}: {env.get('error') or env.get('kind')}); "
                 "they were reported as not run, not as the app's failures."),
        evidence=[f"build {build_id}: {env.get('code')} {env.get('error') or ''}".strip(),
                  *([f"the run's database was checked and {restored}"] if restored else [])],
        root_cause=("The suite's setup could not use the database BOSS gave the run "
                    "(build_services). A build-services server that restarts comes back empty "
                    "(tmpfs), so a live run's role is gone; build_services.ensure re-creates it "
                    "before the run's next command. If this recurs, the lease was lost another "
                    "way." if ours else
                    "The suite could not reach a service it needs in BOSS's sandbox."),
        proposed_change=("Find why the run's service was unusable at test time and make BOSS "
                         "provide it (engine/boss_engine/services/build_services.py)."),
        files=["engine/boss_engine/services/build_services.py"] if ours else [],
        ref={"build_id": build_id}, actor="boss", run_id=None)


def after_verify(engine: Any, settings: Any, *, project_id: str, run_id: str, output: Any,
                 lease: Any = None) -> Optional[dict[str, Any]]:
    """A build's verify failed. When the suite could not RUN (classify): make the run's own
    database usable again (build_services.ensure, forced), say so on the run, and record it for
    Self-fixes. Returns the verdict with `restored` ("restored" → the caller re-runs the suite
    once), or None when the failures are the app's."""
    env = classify(output, boss_service=lease is not None)
    if env is None:
        return None
    restored = "skipped"
    if lease is not None:
        from . import build_services

        restored = build_services.ensure(lease, force=True)
    from .core import emit_event

    try:
        with engine.begin() as c:
            emit_event(c, project_id, EVENT, "boss", "code_task_run", run_id,
                       {"sentence": env["sentence"], "kind": env["kind"], "code": env["code"],
                        "shared": env["shared"], "failures": env["failures"],
                        "owner": env["owner"], "database": restored})
            report(c, settings, project_id=project_id, env=env, build_id=run_id,
                   restored=restored if restored in ("ok", "restored") else "")
    except Exception:  # noqa: BLE001 — the verdict stands without its record
        logging.getLogger(__name__).warning("suite_environment record failed for %s", run_id,
                                            exc_info=True)
    return {**env, "restored": restored}


def failed_check(env: Mapping[str, Any], command: str = "") -> dict[str, Any]:
    """The line a blocked build shows (verification_target.checks_headline) for a suite that
    could not run: BOSS's sentence, never the first "not ok" line."""
    return {"command": command, "line": str(env.get("sentence") or "")[:300],
            "environment": True, "owner": env.get("owner")}


__all__ = ["EVENT", "ONE_CAUSE_SHARE", "after_verify", "classify", "classify_code",
           "failed_check", "failures", "report", "signature"]
