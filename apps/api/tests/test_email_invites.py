"""Email invites: one transport for every sender, a branded multipart invitation, and a status the
inviter can act on (sent / failed with the server's reason / not configured → copy the link).

The SMTP server is a real socket (fake_smtp.FakeSMTP), so what is asserted is what a mail server
receives, not what a mock was called with.
"""
from __future__ import annotations

import ast
import re
import smtplib
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.tests.fake_smtp import FakeSMTP
from apps.api.tests.identity_s1_world import (  # noqa: F401  (world is a fixture)
    csrf,
    fresh_client,
    make_user,
    promote_fresh_mfa,
    sign_in,
    world,
)
from boss_engine.config import get_settings
from boss_engine.identity import authn, authn_support
from boss_engine.services import mail_templates, mail_transport

ROOT = Path(__file__).resolve().parents[3]
REAL_SMTP_CONFIGURED = authn_support.smtp_configured
REAL_MAIL_TRANSPORT = authn_support.MAIL_TRANSPORT
EXPIRES = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


# ---- the template ----------------------------------------------------------------------------------

def _parts(msg):
    text_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    images = [p for p in msg.walk() if p.get_content_maintype() == "image"]
    return text_part.get_content(), html_part.get_content(), images


def test_the_invitation_is_multipart_with_the_mark_attached_not_fetched():
    r = mail_templates.invitation(inviter="Ana Rivera", org="Example Co", roles=["Operator"],
                                  link="https://boss.example.com/invite#t=abc", expires_at=EXPIRES,
                                  from_address="boss@example.com")
    with FakeSMTP() as server:
        settings = get_settings().model_copy(update={
            "email_smtp_host": "127.0.0.1", "email_smtp_port": server.port,
            "email_smtp_use_tls": False, "email_smtp_use_ssl": False,
            "email_smtp_username": None, "email_smtp_password": None,
            "email_from_address": "boss@example.com", "email_real_send_allowed": True})
        mail_transport.send_with(settings, "new@example.com", r.subject, r.text, html=r.html,
                                 inline_images=r.inline_images)
    msg = server.messages[0]
    assert msg["Subject"] == "Ana Rivera invited you to Example Co on BOSS"
    assert msg.get_content_type() == "multipart/alternative"
    plain, html, images = _parts(msg)
    for words in ("Ana Rivera invited you to Example Co on BOSS", "Operator",
                  "https://boss.example.com/invite#t=abc", "26 October 2026, 14:00 UTC",
                  "Didn't expect this? You can ignore it", "boss@example.com"):
        assert words in plain, words
        assert words in html or words.replace("'", "&#x27;") in html, words
    assert "Accept invite" in html and 'alt="BOSS"' in html
    # No remote image: the mark is a cid: attachment of the HTML part.
    assert not re.search(r"<img[^>]+src=\"https?:", html)
    assert len(images) == 1 and images[0]["Content-ID"] == f"<{mail_templates.LOGO_CID}>"


def test_names_are_escaped_and_cannot_split_a_header():
    r = mail_templates.invitation(
        inviter='<img src=x onerror=alert(1)> "Eve"', org="Acme <b>&</b>\r\nBcc: victim@example.com",
        roles=["<script>x</script>"], link="https://boss.example.com/invite#t=abc",
        expires_at=EXPIRES)
    assert "<img src=x" not in r.html and "<script>x" not in r.html and "<b>&</b>" not in r.html
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.html and "&lt;script&gt;" in r.html
    assert "\r" not in r.subject and "\n" not in r.subject
    with pytest.raises(ValueError):
        mail_templates.invitation(inviter="a", org="b", roles=[], link="javascript:alert(1)",
                                  expires_at=EXPIRES)


def test_reset_and_codes_reuse_the_layout():
    r = mail_templates.notice("Reset your BOSS password",
                              "Reset your BOSS password with this link (30 minutes, once):\n\n"
                              "https://boss.example.com/reset#t=xyz\n\nIf you did not ask, ignore this email.")
    assert "Reset password" in r.html and "cid:boss-mark" in r.html
    assert r.text.startswith("Reset your BOSS password with this link")
    code = mail_templates.notice("Your BOSS sign-in code", "Your BOSS sign-in code is 123456.")
    assert "123456" in code.html and "<a href" not in code.html.split("<body", 1)[1].split("cid:")[0]


# ---- one transport ---------------------------------------------------------------------------------

#: The only modules that may build the SMTP wire, read the SMTP settings, or import smtplib.
TRANSPORT = {"engine/boss_engine/services/mail_transport.py",
             "engine/boss_engine/services/email_adapter.py",
             "engine/boss_engine/services/email_config.py",
             "engine/boss_engine/config.py"}
_SETTING_READ = re.compile(
    r"(?<![.\w])settings\.email_(smtp_|real_send|from_address)|getattr\(\s*\w*settings\s*,\s*[\"']email_")


def test_every_sender_goes_through_the_one_transport():
    offenders = []
    for top in ("engine/boss_engine", "apps/api/app", "apps/worker"):
        for path in (ROOT / top).rglob("*.py"):
            if "tests" in path.parts:
                continue
            rel = str(path.relative_to(ROOT))
            if rel in TRANSPORT:
                continue
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == "EmailAdapter":
                        offenders.append(f"{rel}: builds EmailAdapter")
                    if name == "settings_for" and "email_config" in ast.unparse(node.func) + source:
                        offenders.append(f"{rel}: email_config.settings_for")
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    if any(m.split(".")[0] == "smtplib" for m in mods):
                        offenders.append(f"{rel}: imports smtplib")
            if _SETTING_READ.search(source):
                offenders.append(f"{rel}: reads the SMTP settings")
    assert offenders == [], offenders


def test_identity_mail_reads_the_same_resolver():
    """smtp_configured, the policy screen and the sender agree because they are one function."""
    src = (ROOT / "engine/boss_engine/identity/authn_support.py").read_text()
    fn = src[src.index("def smtp_configured"):src.index("def _smtp_send")]
    assert "mail_transport.ready()" in fn
    shapes = (ROOT / "engine/boss_engine/identity/console_shapes.py").read_text()
    assert "mail_transport.route(db, settings).ready" in shapes


def _no_saved(monkeypatch):
    monkeypatch.setattr(mail_transport, "_with_connection", lambda fn, settings: None)


def test_the_route_is_env_then_dev_inbox_then_none(monkeypatch):
    """With nothing connected (the connector-first order: engine/tests/test_email_is_a_connector.py)."""
    _no_saved(monkeypatch)
    base = get_settings().model_copy(update={
        "email_smtp_host": None, "email_from_address": None, "email_real_send_allowed": False,
        "email_dev_inbox": "", "email_dev_inbox_url": ""})
    none = mail_transport.route(None, base)
    assert none.source == "none" and "Email (SMTP)" in none.detail

    env_off = base.model_copy(update={"email_smtp_host": "smtp.example.com",
                                      "email_from_address": "boss@example.com"})
    assert "BOSS_EMAIL_REAL_SEND_ALLOWED" in mail_transport.route(None, env_off).detail
    dev = env_off.model_copy(update={"email_dev_inbox": "mailpit:1025"})
    routed = mail_transport.route(None, dev)
    assert routed.source == "dev_inbox" and not routed.real
    assert (routed.settings.email_smtp_host, routed.settings.email_smtp_port) == ("mailpit", 1025)
    assert routed.settings.email_smtp_use_tls is False and routed.settings.email_smtp_password is None
    on = env_off.model_copy(update={"email_real_send_allowed": True, "email_dev_inbox": "mailpit:1025"})
    assert mail_transport.route(None, on).source == "env"


def test_the_failure_reason_names_the_server_and_never_the_credential():
    secret = "xsmtpsib-not-a-real-key-0000"
    s = get_settings().model_copy(update={"email_smtp_host": "smtp.example.com",
                                          "email_smtp_port": 587, "email_smtp_password": secret,
                                          "email_smtp_username": "login@example.com"})
    auth = smtplib.SMTPAuthenticationError(535, f"5.7.8 bad credentials {secret}".encode())
    why = mail_transport.failure_reason(auth, s)
    assert "smtp.example.com:587" in why and "535" in why and "SMTP key" in why
    assert secret not in why and secret[:6] not in why.replace("xsmtpsib", "")
    refused = smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"5.1.1 no such user")})
    assert "550 5.1.1 no such user" in mail_transport.failure_reason(refused, s)
    assert "refused the connection" in mail_transport.failure_reason(ConnectionRefusedError(), s)


# ---- invite → a real SMTP socket -------------------------------------------------------------------

@pytest.fixture
def mail(world, monkeypatch, engine):
    """The real identity transport, pointed at a fake SMTP server by environment settings."""
    monkeypatch.setattr(authn_support, "smtp_configured", REAL_SMTP_CONFIGURED)
    monkeypatch.setattr(authn_support, "MAIL_TRANSPORT", REAL_MAIL_TRANSPORT)
    with engine.begin() as db:
        db.execute(text("DELETE FROM project_configurations WHERE config_key = 'email_smtp'"))
    mail_transport.forget_cached()
    settings = get_settings()

    def point(server=None, **extra):
        values = {"email_smtp_host": "127.0.0.1" if server else None,
                  "email_smtp_port": server.port if server else 587,
                  "email_smtp_use_tls": False, "email_smtp_use_ssl": False,
                  "email_smtp_username": None, "email_smtp_password": None,
                  "email_from_address": "boss@example.com", "email_from_name": None,
                  "email_real_send_allowed": bool(server), "email_dev_inbox": ""}
        values.update(extra)
        for k, v in values.items():
            monkeypatch.setattr(settings, k, v)
        mail_transport.forget_cached()

    yield point
    mail_transport.forget_cached()


def _owner(engine):
    make_user(engine, "inviter@example.test", role="owner", name='Olu <b>Owner</b>')
    client = fresh_client()
    assert sign_in(client, "inviter@example.test").status_code == 200
    promote_fresh_mfa(engine, client.cookies.get(authn.SESSION_COOKIE))
    return client


def _invite(client, email="new@example.com"):
    return client.post("/v1/identity/users", json={
        "email": email, "bindings": [{"role_key": "operator", "scope_type": "org"}]},
        headers=csrf(client))


def test_an_invite_reaches_the_smtp_server_as_the_branded_message(world, engine, mail):
    owner = _owner(engine)
    with FakeSMTP() as server:
        mail(server)
        res = _invite(owner)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["delivery"]["status"] == "sent" and body["emailed"] is True
        assert "link" not in body                     # emailed: the inviter never holds the link
        assert server.envelopes == [("boss@example.com", ["new@example.com"])]
        msg = server.messages[0]
        plain, html, images = _parts(msg)
        assert msg["From"] == "boss@example.com" and msg["To"] == "new@example.com"
        assert "Olu <b>Owner</b> invited you to" in msg["Subject"]
        assert "Olu &lt;b&gt;Owner&lt;/b&gt;" in html and "<b>Owner</b>" not in html
        assert "Operator" in plain and "#t=" in plain and images
        first_token = re.search(r"#t=(\S+)", plain).group(1)

        listed = owner.get("/v1/identity/invitations").json()["invitations"]
        assert listed[0]["delivery"]["status"] == "sent"

        again = owner.post(f"/v1/identity/invitations/{body['invitation_id']}/resend",
                           headers=csrf(owner))
        assert again.status_code == 200 and again.json()["delivery"]["status"] == "sent"
        second_token = re.search(r"#t=(\S+)", _parts(server.messages[1])[0]).group(1)
    assert second_token != first_token                # the old link stops working
    events = [e for e in world.events if e.event_type == "notice.invitation"]
    assert events and all(first_token not in str(e) and second_token not in str(e) for e in events)


def test_a_refused_invite_says_why_and_hands_over_the_link(world, engine, mail):
    owner = _owner(engine)
    with FakeSMTP(refuse_rcpt="550 5.1.1 mailbox unavailable") as server:
        mail(server)
        body = _invite(owner).json()
    assert body["delivery"]["status"] == "failed" and body["emailed"] is False
    assert "550 5.1.1 mailbox unavailable" in body["delivery"]["reason"]
    assert body["link"] and body["shown_once"] is True
    listed = owner.get("/v1/identity/invitations").json()["invitations"]
    assert "550" in listed[0]["delivery"]["reason"]


def test_without_a_mail_server_the_invite_says_so_and_shows_the_link(world, engine, mail):
    owner = _owner(engine)
    mail(None)
    body = _invite(owner).json()
    assert body["delivery"]["status"] == "not_configured"
    assert "Email (SMTP)" in body["delivery"]["reason"] and body["link"]


def test_the_dev_inbox_gets_the_invite_but_does_not_verify_the_address(world, engine, mail):
    owner = _owner(engine)
    with FakeSMTP() as server:
        mail(None, email_dev_inbox=f"127.0.0.1:{server.port}",
             email_dev_inbox_url="http://localhost:8025")
        body = _invite(owner).json()
    assert body["delivery"]["status"] == "sent" and body["delivery"]["via"] == "dev_inbox"
    assert body["emailed"] is False and body["delivery"]["inbox_url"] == "http://localhost:8025"
    assert len(server.messages) == 1


def test_no_secret_reaches_a_reason_or_an_event(world, engine, mail):
    """A server that refuses the login: the reason names the server; the key is nowhere."""
    secret = "xsmtpsib-canary-key-1234567890"
    owner = _owner(engine)

    def refuse(to, subject, body, **_kw):
        raise smtplib.SMTPAuthenticationError(535, f"Authentication failed for {secret}".encode())

    mail(None, email_smtp_host="smtp.example.com", email_smtp_port=587,
         email_smtp_username="login@example.com", email_smtp_password=secret,
         email_real_send_allowed=True)
    authn_support.MAIL_TRANSPORT = refuse
    try:
        res = _invite(owner)
    finally:
        authn_support.MAIL_TRANSPORT = REAL_MAIL_TRANSPORT
    assert res.json()["delivery"]["status"] == "failed"
    assert "535" in res.json()["delivery"]["reason"]
    assert secret not in res.text
    assert all(secret not in str(e) for e in world.events)
    with engine.begin() as db:
        stored = db.execute(text("SELECT delivery_error FROM identity_invitations")).scalars().all()
    assert stored and all(secret not in (s or "") for s in stored)
