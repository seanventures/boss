"""A80: the redaction sweep (IDENTITY.md §8.3, §15.3).

Every credential shape BOSS has, plus the configured secrets that have no shape (the shared key and
password, env keys, SCIM tokens), is pushed through:
  - logging: root and named loggers, %-args, dict args, f-strings, exceptions and their tracebacks;
  - error reporting: logger.exception from a handler, as main.py's catch-all does;
  - the HTTP access log: uvicorn's access logger and formatter, with OAuth callback query strings;
  - the security event log: event details and free-text columns;
  - the identity tables after a full migration seeded with all of them;
and none survives anywhere.
"""
from __future__ import annotations

import io
import logging

import pytest
from sqlalchemy import text

from boss_engine.identity import audit, legacy, migration, redaction
from boss_engine.identity.audit import Outcome, SecurityEvent
from engine.tests.identity_s5_support import (  # noqa: F401, F811  (db, s5_engine: fixtures)
    CONSOLE_KEY,
    CONSOLE_PASSWORD,
    MCP_KEY,
    NOW,
    ORG,
    REPO,
    SCIM_TOKEN,
    WORKER_KEY,
    community_install,
    creds,
    db,
    fake_claim,
    s5_engine,
    settings,
)

JWT = ("eyJhbGciOiJFZERTQSIsInR5cCI6ImJvc3MtcnVuK2p3dCJ9.eyJzdWIiOiJ1c3JfYWxpY2UiLCJzY29wZSI6"
       "ImNvZGVfdGFzazpjcmVhdGUifQ.c2lnbmF0dXJlX2J5dGVzX2hlcmVfYWJj")
SHAPED = {
    "pat": "boss_pat_" + "Ab3" * 14 + "x" + "Q1w2E3",
    "sak": "boss_sak_" + "Zz9" * 14 + "y" + "R4t5Y6",
    "scim": "boss_scim_" + "Mm7" * 14 + "z" + "U7i8O9",
    "egr": "boss_egr_" + "Kk5" * 14 + "w",
    "session": "bss_" + "S3ss10nT0k3n_" * 3,
    "jwt": JWT,
    "oauth_code": "4/0AeanS0aCodeFromGoogle",
    "pkce": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "saml": "PHNhbWxwOlJlc3BvbnNlIHhtbG5zOnNhbWxwPSJ1cm46b2FzaXMi",
    "csrf": "csrf9f8e7d6c5b4a3210",
    "pem": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
           "-----END PRIVATE KEY-----",
}
#: Secrets with no shape of their own: redacted where they travel (a header, a query string, a
#: key=value pair), which is the only place a filter can know them. Every code path that has one
#: in hand names it with a credential key, and `audit.record` refuses those keys outright.
CONTEXT_ONLY = {"oauth_code", "pkce", "saml", "csrf"}
CONFIGURED = {"console_key": CONSOLE_KEY, "worker_key": WORKER_KEY, "mcp_key": MCP_KEY,
              "scim_token": SCIM_TOKEN, "console_password": CONSOLE_PASSWORD}
ALL = {**SHAPED, **CONFIGURED}


def cfg():
    return settings(legacy_console_api_key=CONSOLE_KEY, legacy_console_password=CONSOLE_PASSWORD,
                    api_key_principal_map={CONSOLE_KEY: "ceo", WORKER_KEY: "system",
                                           MCP_KEY: "cto"},
                    api_key_values=(CONSOLE_KEY, WORKER_KEY, MCP_KEY),
                    enterprise_scim_bearer_token_values=(SCIM_TOKEN,))


def leaked(output: str) -> dict[str, str]:
    found = {}
    for name, secret in ALL.items():
        probes = [secret] if name != "pem" else ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"]
        if name in ("pat", "sak", "scim", "egr"):
            probes.append(secret[len("boss_xxx_"):][:20])           # no part of a key either
        if any(p in output for p in probes):
            found[name] = secret
    return found


@pytest.fixture()
def capture():
    redaction.install()
    redaction.register_configured_secrets(cfg())
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    loggers = [logging.getLogger(), logging.getLogger("uvicorn.access"),
               logging.getLogger("uvicorn.error")]
    saved = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield stream
    for lg, level in saved:
        lg.removeHandler(handler)
        lg.setLevel(level)


def test_a80_the_api_process_installs_the_filter_by_importing_the_guard():
    """A fresh interpreter that imports what main.py imports (the guard's legacy bridge) and
    logs a key: the key never reaches stderr."""
    import subprocess
    import sys
    code = ("import logging; logging.basicConfig(level=logging.INFO);"
            "import apps.api.app.identity_guard;"
            f"logging.getLogger('uvicorn.error').error('got %s', {SHAPED['pat']!r})")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(REPO), env={**__import__("os").environ,
                                             "PYTHONPATH": f"{REPO}:{REPO / 'engine'}"})
    assert "got [REDACTED]" in out.stderr, out.stderr[-500:]
    assert leaked(out.stderr) == {}


def test_a80_the_filter_is_installed_at_the_logging_root():
    assert redaction.installed()
    # a handler added AFTER install (uvicorn configures logging late) still gets redacted records
    stream = io.StringIO()
    late = logging.StreamHandler(stream)
    logging.getLogger("boss.late").addHandler(late)
    try:
        logging.getLogger("boss.late").warning("key %s", SHAPED["pat"])
    finally:
        logging.getLogger("boss.late").removeHandler(late)
    assert "key [REDACTED]" in stream.getvalue()


def test_a80_logs_and_error_reports_carry_no_credential(capture):
    log = logging.getLogger("boss.identity.sweep")
    for name, secret in ALL.items():
        if name not in CONTEXT_ONLY:
            log.info("presented %s", secret)
            log.warning(f"inline {name}: {secret}")
            log.info("%(who)s sent %(what)s", {"who": "alice", "what": secret})
            try:
                raise RuntimeError(f"upstream refused {secret}")
            except RuntimeError:
                log.exception("unhandled error")            # what main.py's catch-all does
            logging.getLogger().error("root saw %s", secret)
        log.info("callback /cb?code=%s&state=%s", secret, secret)
        log.info("headers %s", {"Authorization": f"Bearer {secret}", "Cookie":
                                f"__Host-boss_session={secret}", "X-Boss-CSRF": secret})
        log.debug("Authorization: Bearer %s\r\nCookie: a=b", secret)
        log.info("password=%s token: %s", secret, secret)
        log.info(f"client_secret={secret} code_verifier={secret}")
        try:
            raise RuntimeError(f"upstream refused password={secret}")
        except RuntimeError:
            log.exception("unhandled error")
    # a secret split across two args, joined only by formatting
    log.info("%s%s", CONSOLE_KEY[:30], CONSOLE_KEY[30:])
    output = capture.getvalue()
    assert "presented" in output and "unhandled error" in output
    assert leaked(output) == {}


def test_a80_the_http_access_log_carries_no_credential(capture):
    from uvicorn.logging import AccessFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    access = logging.getLogger("uvicorn.access")
    access.addHandler(handler)
    try:
        for name, secret in ALL.items():
            for path in (f"/v1/auth/sso/idp_1/callback?code={secret}&state={secret}",
                         f"/v1/auth/magic-link/verify?token={secret}",
                         f"/v1/identity/token?client_secret={secret}&assertion={secret}",
                         f"/saml/v2/acs?SAMLResponse={secret}"):
                access.info('%s - "%s %s HTTP/%s" %d', "100.64.0.1:5000", "GET", path, "1.1",
                            200)
    finally:
        access.removeHandler(handler)
    output = stream.getvalue() + capture.getvalue()
    assert "/v1/auth/sso/idp_1/callback?code=" in output and " 200" in output
    assert leaked(output) == {}


def test_a80_the_security_event_log_and_identity_tables_carry_no_credential(db, capture):
    community_install(db)
    s = cfg()
    migration.seed(db, settings=s, now=NOW)
    for key, member in ((CONSOLE_KEY, "ceo"), (WORKER_KEY, "system"), (MCP_KEY, "cto")):
        legacy.bridge(creds(method="api_key", member=member, key=key), db=db, now=NOW, settings=s)
    legacy.scim_bearer(SCIM_TOKEN, db=db)
    legacy.check_shared_password(CONSOLE_PASSWORD, db=db, settings=s, now=NOW)
    claim = fake_claim(db, name="Param", email="param@example.com", via="legacy_password",
                       now=NOW, cfg=s)
    for name, secret in ALL.items():
        audit.record(SecurityEvent(
            org_id=ORG, event_type="sso.login", outcome=Outcome.FAILURE,
            user_agent=f"curl/8 Bearer {secret}", target_id=f"https://x/cb?code={secret}",
            details={"note": f"Authorization: Bearer {secret}", "url": f"/cb?state={secret}",
                     "list": [f"password={secret}"],
                     "nested": {"message": secret if name not in CONTEXT_ONLY
                                else f"Cookie: {secret}"}}), db=db)
    legacy.note_claimed_actor(claim.ctx, f"ceo {CONSOLE_KEY}", db=db)
    tables = db.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = "
                             "current_schema() AND tablename LIKE 'identity\\_%'")).scalars().all()
    dump = "".join(db.execute(text(f"SELECT coalesce(string_agg(row_to_json(t)::text, ''), '') "
                                   f"FROM {table} t")).scalar() for table in tables)
    assert "sys_legacy_console" in dump and "legacy_env" in dump
    assert leaked(dump) == {}
    assert leaked(capture.getvalue()) == {}
    assert audit.verify_chain(ORG, db=db).ok


def test_a80_redact_drops_credential_named_keys_and_keeps_public_ids():
    out = redaction.redact({"Authorization": "x", "clientSecret": "y", "refresh_token": "z",
                            "password": "p", "code": "c", "SAMLResponse": "s", "otp": "1",
                            "token_jti": "rt_1", "credential_id": "key_1", "status_code": 500,
                            "tokens_in": 12, "note": "ok"})
    assert {k for k, v in out.items() if v == redaction.REDACTED} == {
        "Authorization", "clientSecret", "refresh_token", "password", "code", "SAMLResponse",
        "otp"}
    assert out["token_jti"] == "rt_1" and out["status_code"] == 500 and out["note"] == "ok"
    assert redaction.redact_text("state=running tokens_in=5 code_task ct_1") == \
        "state=running tokens_in=5 code_task ct_1"
