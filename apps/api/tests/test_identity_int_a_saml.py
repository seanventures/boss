"""int-a · SAML providers keep their SSO URL and trusted MFA contexts in real columns (amendment S1-5).

`identity_providers.saml_sso_url` replaces the SSO URL carried in `client_id` (still read for a row
saved before the column), and `trusted_authn_contexts` is the AuthnContextClassRef set that counts
as MFA under "trust IdP MFA" (NULL: BOSS's default list). Each test fails if the protection is removed.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text

from apps.api.tests.identity_s1_world import fresh_client, world  # noqa: F401  (world is a fixture)
from apps.api.tests.test_identity_authn_sso import (
    IDP_ENTITY,
    SSO_URL,
    SamlIdP,
    _owner_client,
    assert_signed_in,
    invite,
    post_acs,
    saml_start,
)
from boss_engine.identity import sso_oidc, sso_saml


def _provider(engine, idp: SamlIdP, *, contexts: Optional[list[str]], trust: bool = True) -> str:
    from boss_engine.identity import ids
    from boss_engine.identity.authn_support import default_org_id

    pid = ids.new_id("idp")
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO identity_providers (id, org_id, protocol, name, issuer, client_id, saml_sso_url, "
            "trusted_authn_contexts, trust_idp_mfa, saml_certificates) VALUES (:id, :o, 'saml', 'Corp SAML', :i, "
            "NULL, :sso, :ctx, :t, :c)"),
            {"id": pid, "o": default_org_id(db), "i": IDP_ENTITY, "sso": SSO_URL, "ctx": contexts, "t": trust,
             "c": [idp.pem]})
    return pid


def _with_context(xml: str, class_ref: str) -> str:
    statement = (f'<saml:AuthnStatement AuthnInstant="2026-01-01T00:00:00Z"><saml:AuthnContext>'
                 f'<saml:AuthnContextClassRef>{class_ref}</saml:AuthnContextClassRef></saml:AuthnContext>'
                 f'</saml:AuthnStatement>')
    return xml.replace("<saml:AttributeStatement>", statement + "<saml:AttributeStatement>", 1)


def _sign_in(engine, idp: SamlIdP, pid: str, class_ref: str):
    client = fresh_client()
    res = client.get(f"/v1/auth/sso/{pid}/start", follow_redirects=False)
    assert res.status_code == 302, res.text
    assert res.headers["location"].startswith(SSO_URL + "?")            # the column, client_id is NULL
    rid = saml_start(client, pid)
    signed = idp.sign(_with_context(idp.assertion_xml(in_response_to=rid), class_ref))
    res = post_acs(client, idp.encode(idp.response(signed, in_response_to=rid)))
    assert_signed_in(res)
    return client.get("/v1/identity/me").json()


def test_saml_sign_in_reads_the_sso_url_column_and_its_own_trusted_mfa_contexts(world, engine):
    idp = SamlIdP()
    pid = _provider(engine, idp, contexts=["urn:corp:ac:hardware-key"])
    invite(engine, "pat@corp.test")
    me = _sign_in(engine, idp, pid, "urn:corp:ac:hardware-key")
    assert me["mfa_at"] is not None                                          # the configured context counts
    # The default list no longer applies once the provider names its own set.
    me = _sign_in(engine, idp, pid, "urn:oasis:names:tc:SAML:2.0:ac:classes:MultiFactor")
    assert me["mfa_at"] is None


def test_provider_admin_writes_the_columns_and_older_rows_still_read_client_id(world, engine):
    idp = SamlIdP()
    client, h = _owner_client(engine)
    res = client.post("/v1/identity/providers", headers=h, json={
        "protocol": "saml", "name": "Corp", "issuer": IDP_ENTITY, "saml_sso_url": SSO_URL,
        "saml_certificates": [idp.pem], "trust_idp_mfa": True,
        "trusted_authn_contexts": ["urn:corp:ac:hardware-key", " "]})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["saml_sso_url"] == SSO_URL and body["trusted_authn_contexts"] == ["urn:corp:ac:hardware-key"]
    with engine.begin() as db:
        row = db.execute(text("SELECT client_id, saml_sso_url, trusted_authn_contexts FROM identity_providers "
                              "WHERE id = :p"), {"p": body["id"]}).one()
    assert row.client_id is None and row.saml_sso_url == SSO_URL and list(row.trusted_authn_contexts) == [
        "urn:corp:ac:hardware-key"]
    upd = client.patch(f"/v1/identity/providers/{body['id']}", headers=h,
                       json={"saml_sso_url": SSO_URL + "/v2", "trusted_authn_contexts": []})
    assert upd.status_code == 200 and upd.json()["saml_sso_url"] == SSO_URL + "/v2"
    with engine.begin() as db:
        row = db.execute(text("SELECT saml_sso_url, trusted_authn_contexts FROM identity_providers WHERE id = :p"),
                         {"p": body["id"]}).one()
    assert row.saml_sso_url == SSO_URL + "/v2" and row.trusted_authn_contexts is None   # back to the default list
    # A provider saved before the column (SSO URL in client_id) still works.
    legacy = sso_oidc.Provider("idp_old", "org", "saml", "generic", "Old", IDP_ENTITY, SSO_URL, None, (), (), None,
                               (idp.pem,), (), "off", True, False, None, "active")
    assert legacy.sso_url == SSO_URL and sso_saml.trusted_mfa_contexts(legacy) == sso_saml.MFA_CONTEXTS
    oidc = sso_oidc.Provider("idp_o", "org", "oidc", "generic", "O", "https://x", "client", None, (), (), None,
                             (), (), "off", False, False, None, "active", saml_sso_url="https://evil")
    assert oidc.sso_url is None                                              # never for a non-SAML provider
