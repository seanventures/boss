"""The Email (SMTP) connector, through the API a console uses: connect with one URL or with
fields, Send test with the server's real reply, Disconnect — and the key never comes back.

The mail server is a real socket (fake_smtp.FakeSMTP, with AUTH), so "the key reached the server
and nowhere else" is checked where it would leak: the response, the row, the events, the test
record and the process output.
"""
from __future__ import annotations

import base64
import json
import logging

import pytest
from sqlalchemy import text

from apps.api.tests.fake_smtp import FakeSMTP
from boss_engine.config import get_settings
from boss_engine.services import email_adapter, email_config, mail_transport

PROJECT = "boss-dogfood"
SECRET = "xkey-p@ss/w:rd#?%&+ 0042"
ENCODED = "xkey-p%40ss%2Fw%3Ard%23%3F%25%26%2B%200042"
LOGIN = "login@example.com"


@pytest.fixture
def smtp(monkeypatch, engine):
    """No mail configured anywhere, and STARTTLS a no-op against the plain fake server (everything
    else — EHLO, AUTH, MAIL, RCPT, DATA — goes over the real socket)."""
    settings = get_settings()
    for key, value in {"email_smtp_host": None, "email_from_address": None,
                       "email_smtp_username": None, "email_smtp_password": None,
                       "email_real_send_allowed": False, "email_dev_inbox": "",
                       "email_dev_inbox_url": ""}.items():
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(email_adapter.smtplib.SMTP, "starttls",
                        lambda self, *a, **k: (220, b"2.0.0 Ready"))
    with engine.begin() as db:
        db.execute(text("DELETE FROM project_configurations WHERE config_key = 'email_smtp'"))
    mail_transport.forget_cached()
    with FakeSMTP(auth=True) as server:
        yield server
    mail_transport.forget_cached()


def _connect(client, fields, provider="smtp"):
    return client.post("/v1/kit/email/connect", json={
        "project_id": PROJECT, "actor": "ceo", "provider": provider, "fields": fields})


def _url(server, password=ENCODED):
    return f"smtp://login%40example.com:{password}@127.0.0.1:{server.port}"


def _row(engine):
    with engine.connect() as db:
        return db.execute(text("SELECT body FROM project_configurations WHERE project_id = :p "
                               "AND config_key = 'email_smtp'"), {"p": PROJECT}).scalar()


def _leaks(*texts):
    hay = "\n".join(str(t) for t in texts)
    return [s for s in (SECRET, ENCODED, SECRET[:12], base64.b64encode(SECRET.encode()).decode())
            if s in hay]


def test_the_connector_is_listed_once_as_email_smtp(client, smtp):
    kit = client.get("/v1/kit", params={"project_id": PROJECT}).json()
    emails = [a for a in kit["abilities"] if "email" in a["key"]]
    assert [a["label"] for a in emails] == ["Email (SMTP)"]
    assert emails[0]["status"] == "setup"
    fields = [f["name"] for f in emails[0]["connection"]["providers"][0]["fields"]]
    assert fields[:3] == ["url", "from_address", "from_name"]
    assert {"host", "port", "security", "username", "password"} <= set(fields)


def test_a_url_connects_and_the_key_is_never_returned_logged_or_emitted(
        client, smtp, engine, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    r = _connect(client, {"url": _url(smtp), "from_address": "boss@example.com",
                          "from_name": "Example Ops"})
    assert r.status_code == 200, r.text
    body = r.json()
    conn = body["connection"]
    assert conn["connected"] is True and conn["provider"] == "smtp"
    assert conn["set_fields"]["password"] == "set"
    assert conn["set_fields"]["url"] == f"smtp://login%40example.com@127.0.0.1:{smtp.port}"
    assert conn["set_fields"]["from_name"] == "Example Ops"
    # Send test shows the server's own reply, not a guess.
    assert "250 2.0.0 Ok: queued as FAKE123" in body["last_test"]["fact"]
    # The key went to the server, as AUTH PLAIN, and only there.
    plain = base64.b64decode(smtp.logins[0].split()[-1]).split(b"\0")
    assert plain[1:] == [LOGIN.encode(), SECRET.encode()]
    assert smtp.messages[0]["From"] == "Example Ops <boss@example.com>"

    again = client.post("/v1/kit/email/test", json={"project_id": PROJECT, "actor": "ceo"})
    assert again.status_code == 200 and again.json()["ok"] is True
    assert "250 2.0.0 Ok: queued as FAKE123" in again.json()["fact"]

    shown = [r.text, again.text,
             client.get("/v1/kit/email", params={"project_id": PROJECT}).text,
             client.get("/v1/kit", params={"project_id": PROJECT}).text,
             client.get("/v1/email/config", params={"project_id": PROJECT}).text]
    with engine.connect() as db:
        events = db.execute(text("SELECT event_type, data::text FROM events "
                                 "WHERE project_id = :p"), {"p": PROJECT}).all()
        tests = db.execute(text("SELECT fact, error FROM kit_tests WHERE project_id = :p"),
                           {"p": PROJECT}).all()
    out = capsys.readouterr()
    assert _leaks(*shown) == [], "the key came back from the API"
    assert _leaks(json.dumps(_row(engine))) == [], "the key is in the configuration row"
    assert _leaks(*events) == [], "the key is in an event"
    assert _leaks(*tests) == [], "the key is in the test record"
    assert _leaks(caplog.text, out.out, out.err) == [], "the key was logged"
    assert any(e[0] == "email.smtp_configured" for e in events)


def test_the_url_and_the_fields_store_the_same_config(client, smtp, engine):
    assert _connect(client, {"url": _url(smtp), "from_address": "boss@example.com"}
                    ).status_code == 200
    by_url = dict(_row(engine))
    assert _connect(client, {"host": "127.0.0.1", "port": str(smtp.port), "security": "starttls",
                             "username": LOGIN, "password": SECRET,
                             "from_address": "boss@example.com"}).status_code == 200
    by_fields = dict(_row(engine))
    for row in (by_url, by_fields):
        row.pop("secret_ref_id")
    assert by_url == by_fields
    assert base64.b64decode(smtp.logins[1].split()[-1]).split(b"\0")[2] == SECRET.encode()


def test_editing_without_the_key_keeps_it(client, smtp, engine):
    assert _connect(client, {"url": _url(smtp), "from_address": "boss@example.com"}
                    ).status_code == 200
    # The URL as shown (no password) saved back with a new From name: the stored key is used.
    shown = f"smtp://login%40example.com@127.0.0.1:{smtp.port}"
    r = _connect(client, {"url": shown, "from_address": "boss@example.com", "from_name": "Ops"})
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["set_fields"]["password"] == "set"
    assert base64.b64decode(smtp.logins[1].split()[-1]).split(b"\0")[2] == SECRET.encode()


@pytest.mark.parametrize("fields,field,words", [
    ({"url": f"https://login:{ENCODED}@smtp.example.com", "from_address": "boss@example.com"},
     "url", "smtp://"),
    ({"url": f"smtp://login:{ENCODED}@:587", "from_address": "boss@example.com"}, "url", "host"),
    ({"url": "smtp://login:k#y@smtp.example.com:587", "from_address": "boss@example.com"},
     "url", "Percent-encode"),
    ({"url": f"smtp://login:{ENCODED}@smtp.example.com:587"}, "from_address", "From"),
])
def test_a_bad_connection_says_which_field_and_writes_nothing(client, smtp, engine, fields,
                                                              field, words):
    r = _connect(client, fields)
    assert r.status_code == 422, r.text
    detail = r.json().get("detail") or r.json()
    assert detail["field"] == field and words.lower() in detail["message"].lower()
    assert _leaks(r.text) == [] and "k#y" not in r.text
    assert _row(engine) is None and smtp.messages == []


def test_a_refused_login_shows_the_servers_reply_and_stores_nothing(client, smtp, engine,
                                                                    monkeypatch):
    def refuse(self, user, password, **_kw):
        raise email_adapter.smtplib.SMTPAuthenticationError(
            535, f"5.7.8 Username and Password not accepted {password}".encode())

    monkeypatch.setattr(email_adapter.smtplib.SMTP, "login", refuse)
    r = _connect(client, {"url": _url(smtp), "from_address": "boss@example.com"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "535 5.7.8 Username and Password not accepted" in detail["message"]
    assert detail["field"] == "password"
    assert _leaks(r.text) == [] and _row(engine) is None


def test_disconnect_forgets_the_connection(client, smtp):
    assert _connect(client, {"url": _url(smtp), "from_address": "boss@example.com"}
                    ).status_code == 200
    r = client.delete("/v1/kit/email/connection", params={"project_id": PROJECT, "actor": "ceo"})
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["connected"] is False and r.json()["status"] == "setup"
    assert mail_transport.route(None, get_settings(), PROJECT).source == "none"


# ---- settings saved before the connector -----------------------------------------------------------

def test_settings_saved_before_the_upgrade_show_as_the_connector(client, smtp, engine):
    """An rc10.9 install saved SMTP from the old Email form: same key, older shape (no mode, a
    password_last4). After the upgrade it is this connector, connected, with the key shown as
    "set" only, and mail goes through it."""
    from boss_engine.services.secret_backends import default_secret_backend
    from boss_engine.services.secrets_service import put_secret

    settings = get_settings()
    with engine.begin() as db:
        stored = put_secret(db, settings, project_id=PROJECT, name=email_config.SECRET_NAME,
                            value=SECRET, secret_kind="api_token",
                            backend=default_secret_backend(settings), actor="test")
        legacy = {"host": "127.0.0.1", "port": smtp.port, "username": LOGIN,
                  "from_address": "boss@example.com", "from_name": "", "use_tls": True,
                  "use_ssl": False, "secret_ref_id": stored.get("id") or stored.get("secret_ref_id"),
                  "password_last4": "0042"}
        db.execute(text("""INSERT INTO project_configurations (id, project_id, config_key, body,
                                                               updated_by)
                           VALUES ('cfg_legacy_email', :p, 'email_smtp', CAST(:b AS JSONB), 'old')"""),
                   {"p": PROJECT, "b": json.dumps(legacy)})
    mail_transport.forget_cached()

    ability = client.get("/v1/kit/email", params={"project_id": PROJECT}).json()
    conn = ability["connection"]
    assert conn["connected"] is True and conn["provider"] == "smtp"
    assert conn["set_fields"]["host"] == "127.0.0.1"
    assert conn["set_fields"]["password"] == "set", "the old last four must not be shown"
    assert conn["set_fields"]["security"] == "starttls"
    assert conn["set_fields"]["url"] == f"smtp://login%40example.com@127.0.0.1:{smtp.port}"
    assert "0042" not in json.dumps(conn)
    described = client.get("/v1/email/config", params={"project_id": PROJECT}).json()
    assert described["mode"] == "smtp" and "password_last4" not in described
    with engine.connect() as db:
        assert mail_transport.route(db, settings, PROJECT).source == "connector"
    tested = client.post("/v1/kit/email/test", json={"project_id": PROJECT, "actor": "ceo"})
    assert tested.json()["ok"] is True, tested.text
    assert base64.b64decode(smtp.logins[0].split()[-1]).split(b"\0")[2] == SECRET.encode()


# ---- the local test inbox --------------------------------------------------------------------------

def test_the_local_test_inbox_is_a_choice_only_when_mailpit_is_on(client, smtp, engine,
                                                                   monkeypatch):
    def providers():
        a = client.get("/v1/kit/email", params={"project_id": PROJECT}).json()
        return {p["key"]: p for p in a["connection"]["providers"]}

    assert "dev_inbox" not in providers()
    assert _connect(client, {}, provider="dev_inbox").status_code == 422

    monkeypatch.setattr(get_settings(), "email_dev_inbox", f"127.0.0.1:{smtp.port}")
    monkeypatch.setattr(get_settings(), "email_dev_inbox_url", "http://localhost:8025")
    offered = providers()["dev_inbox"]
    assert "local" in (offered["label"] + offered.get("tip", "")).lower()
    r = _connect(client, {}, provider="dev_inbox")
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["provider"] == "dev_inbox"
    assert r.json()["connection"]["connected"] is True
    assert _row(engine)["mode"] == "dev_inbox"
    with engine.connect() as db:
        assert mail_transport.route(db, get_settings(), PROJECT).source == "dev_inbox"
    assert len(smtp.messages) == 1
