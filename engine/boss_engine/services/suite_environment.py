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


# ── Could the INSTALL run? The same question, one step earlier ──────────────────────────────
#
# THE CASE (an enterprise install, 2026-11). Behind a TLS-intercepting proxy without its CA,
# `npm install` failed UNABLE_TO_VERIFY_LEAF_SIGNATURE (or stalled to the install timeout); an
# internal registry answered E401 without the token the sandbox lacked. BOSS read each as a
# dependency conflict: the coder was told to "resolve the dependency conflict", repair rounds
# ran on tests that never ran, the slow install re-ran several times per build, and a coder
# could pin or drop packages to "fix" a certificate. One classifier, for the suite's failures
# above and for every package manager's install here (npm, pnpm, yarn, pip, Go, Maven), says
# which install failures no code change can fix.
#
# Kinds (`classify_install`):
#   tls               the registry's certificate is not trusted here (an intercepting proxy's CA)
#   network           DNS / connect / reset — `transient`: one backoff retry may clear it
#   registry_auth     the registry or a proxy wants a credential the sandbox does not hold
#                     (npm E401, proxy E407 / 407, pnpm ERR_PNPM_FETCH_401, yarn/pip/Maven/Go 401)
#   registry_forbidden the registry (or BOSS's egress proxy) refuses the registry itself: 403 on
#                     its root or metadata, a CONNECT tunnel refused
#   registry_policy_blocked  a package-curation firewall (Artifactory Curation, Nexus Firewall,
#                     and the like) refused ONE package version: NOT environmental — the coder
#                     picks a version the registry allows
#   timeout           the install hit its time limit; environmental only with network evidence
#
# `environment` says whether the failure is the install's environment (no code change helps).

#: Each kind's words: (kind, pattern). Ordered: the first match wins; a curation block is read
#: before a plain 403, a proxy's 407 before a registry's 401.
_INSTALL_SIGNALS: tuple[tuple[str, str], ...] = (
    ("registry_policy_blocked",
     r"curation|nexus (?:repository )?firewall|firewall (?:policy|blocked|quarantin)|quarantin"
     r"|blocked by (?:the )?(?:\w+ ){0,3}(?:policy|policies)|polic(?:y|ies) violated"
     r"|violat\w* (?:the )?(?:following )?polic"),
    ("tls",
     r"UNABLE_TO_VERIFY_LEAF_SIGNATURE|SELF_SIGNED_CERT_IN_CHAIN|UNABLE_TO_GET_ISSUER_CERT"
     r"|CERT_HAS_EXPIRED|DEPTH_ZERO_SELF_SIGNED_CERT|ERR_TLS_CERT_ALTNAME_INVALID"
     r"|CERTIFICATE_VERIFY_FAILED|SSLCertVerificationError|certificate verify failed"
     r"|x509: certificate|PKIX path building failed|unable to find valid certification path"
     r"|self[- ]signed certificate"),
    ("registry_auth",
     r"\bE40[17]\b|ERR_PNPM_FETCH_40[17]|\b407 Proxy Authentication|proxy authentication required"
     r"|Request failed \"40[17]|status code:? 40[17]\b|HTTP error 40[17]|40[17] Client Error"
     r"|\b40[17] Unauthorized|\bUnauthorized\b.{0,40}\b401\b|\b401\b.{0,40}\bUnauthorized"
     r"|authentication (?:is )?required|not authorized\b|ERR_PNPM_FETCH_407"),
    ("registry_forbidden",
     r"\bE403\b|ERR_PNPM_FETCH_403|\b403 Forbidden|status code:? 403\b|HTTP error 403"
     r"|403 Client Error|Request failed \"403|statusCode=403|tunneling socket could not be "
     r"established"),
    ("network",
     r"\b(?:ENOTFOUND|EAI_AGAIN|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENETUNREACH|EHOSTUNREACH"
     r"|ESOCKETTIMEDOUT|ECONNABORTED)\b|ERR_PNPM_META_FETCH_FAIL|socket hang up"
     r"|Temporary failure in name resolution|Name or service not known|NewConnectionError"
     r"|Max retries exceeded|Could not resolve host|no such host|i/o timeout"
     r"|dial tcp\b|connection (?:refused|reset|timed out)|UnknownHostException"
     r"|Connect to \S+ failed|network timeout|network is unreachable|fetch failed"),
)
#: A 403/401 naming ONE package version (a tarball, a wheel, a jar, a Go module zip): a
#: curation firewall's refusal even when it does not say "curation".
_VERSION_ARTIFACT = re.compile(
    r"/(?P<npmpkg>(?:@[\w.-]+/)?[\w.-]+)/-/[\w.-]+?-(?P<npmver>\d[\w.+-]*?)\.tgz"
    r"|/(?P<gomod>[\w.-]+(?:/[\w.-]+)*)/@v/(?P<gover>v[\w.+-]+?)\.(?:zip|mod)\b"
    r"|(?P<file>[\w.-]+-\d[\w.+!-]*?\.(?:whl|tar\.gz|zip|jar|pom))\b", re.I)
#: What package@version a curation refusal names, when it says so.
_NAMED_PACKAGE = re.compile(r"(?P<pkg>(?:@[\w.-]+/)?[\w.-]+(?:/[\w.-]+)*)@(?P<ver>v?\d[\w.+-]*)")


def _artifact_name(found: "re.Match[str]") -> str:
    if found.group("npmpkg"):
        return f"{found.group('npmpkg')}@{found.group('npmver')}"
    if found.group("gomod"):
        return f"{found.group('gomod')}@{found.group('gover')}"
    return found.group("file") or ""
#: Network words inside a timed-out install: the slow part was the network, not the code.
_TIMEOUT_NETWORK = re.compile(
    r"network|fetch|registry|proxy|ETIMEDOUT|ESOCKETTIMEDOUT|socket|retry|retrying|timeout",
    re.I)

_INSTALL_REASON = {
    "tls": "the package registry's certificate is not trusted in BOSS's sandbox (a TLS-"
           "intercepting proxy needs its CA installed)",
    "network": "the package registry could not be reached from BOSS's sandbox",
    "registry_auth": "the package registry (or a proxy) wants a credential BOSS's builds do not "
                     "hold",
    "registry_forbidden": "the package registry (or BOSS's egress proxy) refused access to the "
                          "registry itself",
    "registry_policy_blocked": "the package registry's policy blocked a specific package version",
    "timeout": "the install did not finish in time, and the registry was slow or unreachable",
}


def classify_install(output: Any, *, timed_out: bool = False) -> Optional[dict[str, Any]]:
    """Why an install failed, when it is one of the kinds above, else None (a dependency
    conflict, a missing version, a failing build script: the code's). {kind, reason,
    environment, transient, evidence, package}."""
    text = str(output or "")
    for kind, pattern in _INSTALL_SIGNALS:
        hit = re.search(pattern, text, re.I)
        if hit is None:
            continue
        line_start = text.rfind("\n", 0, hit.start()) + 1
        line_end = text.find("\n", hit.end())
        evidence = text[line_start:line_end if line_end >= 0 else len(text)].strip()[:300]
        refused = [ln for ln in text.splitlines() if re.search(r"\b403\b|E403|_403", ln)]
        if kind == "registry_forbidden" and any(_VERSION_ARTIFACT.search(ln) for ln in refused):
            kind = "registry_policy_blocked"     # 403 on one version's artifact, not the registry
        package = ""
        if kind == "registry_policy_blocked":
            artifact = next((m for ln in refused or text.splitlines()
                             if (m := _VERSION_ARTIFACT.search(ln))), None)
            named = _NAMED_PACKAGE.search(evidence)
            package = (f"{named.group('pkg')}@{named.group('ver')}" if named else
                       _artifact_name(artifact) if artifact else "")
        return {"kind": kind, "reason": _INSTALL_REASON[kind],
                "environment": kind != "registry_policy_blocked",
                "transient": kind == "network", "evidence": evidence, "package": package or ""}
    if timed_out:
        networky = bool(_TIMEOUT_NETWORK.search(text))
        return {"kind": "timeout", "reason": _INSTALL_REASON["timeout"] if networky else
                "the install did not finish in time",
                "environment": networky, "transient": True,
                "evidence": (text.strip().splitlines() or [""])[-1][:300], "package": ""}
    return None


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
           "classify_install",
           "failed_check", "failures", "report", "signature"]
