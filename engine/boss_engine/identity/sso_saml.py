"""BOSS as a SAML 2.0 service provider for CONSOLE sign-in (IDENTITY.md §4.8 "SAML"). Stream S1.

  - SP-initiated by default: every Response must answer a stored AuthnRequest (`InResponseTo`),
    bound to the browser that started it; an unsolicited (IdP-initiated) Response is refused (A68)
    — unless the provider opts in with `allow_idp_initiated` (off by default; BOSSAPP-1048, an
    Okta dashboard tile). Then a Response that answers NO request (no InResponseTo anywhere) is
    accepted with every other check unchanged (signature, Destination, Recipient, Audience,
    validity window), plus: its SubjectConfirmationData must carry NotOnOrAfter, its IssueInstant
    must be recent, and its assertion id is spent once (the replay cache).
  - Parsing: lxml with no DTD, no entities, no network; a DOCTYPE is refused outright.
  - Signature: signxml against the CONFIGURED certificate(s) only (two for rollover). Only the
    element signxml reports as signed is read, and the document must contain exactly one
    Assertion, which must be that signed element (or sit inside the signed Response). Every XML
    signature wrapping layout therefore fails (A66).
  - Conditions: Audience == SP entity id; Destination and Recipient == the ACS URL; NotBefore /
    NotOnOrAfter with 60 s skew; each assertion id is spent once (replay cache, A67).
  - Identity: (IdP entityID, NameID).

Schema (amendment S1-5): the IdP SSO URL is `identity_providers.saml_sso_url` and the MFA
AuthnContextClassRef set is `trusted_authn_contexts` (NULL: MFA_CONTEXTS). A provider saved before
those columns has its SSO URL in `client_id`, still read as a fallback (`Provider.sso_url`).
"""
from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from sqlalchemy import text

from .authn import PREAUTH_COOKIE
from .authn_support import (
    challenge_by_secret,
    consume_challenge,
    create_challenge,
    random_token,
    utcnow,
)
from .sso_oidc import (FederatedIdentity, Provider, SsoError, StartedFlow, _PROVIDER_COLS, provider_from_row,
                       safe_next)

SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
DSIG = "http://www.w3.org/2000/09/xmldsig#"
NS = {"samlp": SAMLP, "saml": SAML, "ds": DSIG}
REQUEST_TTL_SECONDS = 600
#: An IdP-initiated Response must have been issued this recently (it answers no request of ours).
UNSOLICITED_MAX_AGE = timedelta(minutes=5)
SKEW = timedelta(seconds=60)
MAX_RESPONSE_BYTES = 256_000
EMAIL_ATTRIBUTES = ("email", "mail", "emailaddress",
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                    "urn:oid:0.9.2342.19200300.100.1.3")
NAME_ATTRIBUTES = ("name", "displayName", "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
                   "urn:oid:2.16.840.1.113730.3.1.241")
MFA_CONTEXTS = frozenset({
    "urn:oasis:names:tc:SAML:2.0:ac:classes:MultiFactor",
    "http://schemas.microsoft.com/claims/multipleauthn",
    "urn:oasis:names:tc:SAML:2.0:ac:classes:TimeSyncToken",
    "urn:oasis:names:tc:SAML:2.0:ac:classes:X509",
})


def trusted_mfa_contexts(provider: Provider) -> frozenset[str]:
    """The AuthnContextClassRef values this provider's assertions may vouch MFA with."""
    configured = frozenset(c.strip() for c in (provider.trusted_authn_contexts or ()) if c and c.strip())
    return configured or MFA_CONTEXTS


def acs_url(origin: str) -> str:
    return f"{origin}/saml/v2/acs"


def sp_entity_id(provider: Optional[Provider], origin: str) -> str:
    return (provider.saml_sp_entity_id if provider and provider.saml_sp_entity_id
            else f"{origin}/saml/v2/metadata")


def metadata(origin: str, entity_id: Optional[str] = None) -> str:
    eid = escape(entity_id or sp_entity_id(None, origin), {'"': "&quot;"})
    acs = escape(acs_url(origin), {'"': "&quot;"})
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{eid}">'
            '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
            'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
            '<md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:persistent</md:NameIDFormat>'
            '<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{acs}" index="0" isDefault="true"/></md:SPSSODescriptor></md:EntityDescriptor>')


def start(db: Any, provider: Provider, *, origin: str, next_path: Optional[str] = None) -> StartedFlow:
    from .sessions import set_cookie_header

    sso_url = provider.sso_url or ""
    if provider.protocol != "saml" or not sso_url.startswith("https://") or not provider.issuer:
        raise SsoError("provider_misconfigured")
    request_id = "_" + random_token(nbytes=20).replace("-", "a").replace("_", "b")
    preauth = random_token("bpa_")
    create_challenge(db, kind="saml_request", binding_secret=preauth, ttl_seconds=REQUEST_TTL_SECONDS,
                     secret=request_id, payload={"provider_id": provider.id, "next": safe_next(next_path)})
    instant = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = (f'<samlp:AuthnRequest xmlns:samlp="{SAMLP}" xmlns:saml="{SAML}" ID="{request_id}" '
           f'Version="2.0" IssueInstant="{instant}" Destination="{escape(sso_url)}" '
           f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
           f'AssertionConsumerServiceURL="{escape(acs_url(origin))}">'
           f'<saml:Issuer>{escape(sp_entity_id(provider, origin))}</saml:Issuer></samlp:AuthnRequest>')
    deflated = zlib.compress(xml.encode("utf-8"))[2:-4]
    query = urlencode({"SAMLRequest": base64.b64encode(deflated).decode("ascii")})
    # The IdP POSTs back cross-site, so the pre-auth cookie must be SameSite=None to come along.
    cookie = set_cookie_header(PREAUTH_COOKIE, preauth, max_age=REQUEST_TTL_SECONDS, samesite="None")
    sep = "&" if "?" in sso_url else "?"
    return StartedFlow(f"{sso_url}{sep}{query}", cookie)


def _parse(saml_response: str) -> Any:
    from lxml import etree

    try:
        raw = base64.b64decode(saml_response or "", validate=True)
    except (ValueError, TypeError):
        raise SsoError("saml_malformed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SsoError("saml_malformed")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SsoError("saml_malformed")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             dtd_validation=False, huge_tree=False, remove_comments=False)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError:
        raise SsoError("saml_malformed") from None
    if root.tag != f"{{{SAMLP}}}Response":
        raise SsoError("saml_malformed")
    return root


def _time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise SsoError("saml_malformed") from None


def _find_provider(db: Any, issuer: Optional[str]) -> Provider:
    if not issuer:
        raise SsoError("unknown_provider")
    r = db.execute(text(f"SELECT {_PROVIDER_COLS} FROM identity_providers WHERE protocol = 'saml' "
                        "AND status = 'active' AND issuer = :i"), {"i": issuer}).first()
    if r is None:
        raise SsoError("unknown_provider")
    return provider_from_row(r)


def _verify_signature(root: Any, provider: Provider) -> Any:
    """The element the signature covers, per the configured certificates — or SsoError."""
    from signxml import XMLVerifier

    last: Optional[Exception] = None
    for pem in provider.saml_certificates[:2]:
        try:
            result = XMLVerifier().verify(root, x509_cert=pem)
            return result.signed_xml
        except Exception as exc:  # noqa: BLE001 — every signxml failure is a refusal
            last = exc
    del last
    raise SsoError("saml_signature_invalid")


@dataclass(frozen=True)
class SamlResult:
    provider: Provider
    identity: FederatedIdentity
    next_path: Optional[str] = None
    idp_initiated: bool = False


def consume_response(db: Any, *, saml_response: str, preauth: Optional[str], origin: str,
                     relay_state: Optional[str] = None) -> SamlResult:
    root = _parse(saml_response)
    assertions = root.findall(f".//{{{SAML}}}Assertion")
    if root.findall(f".//{{{SAML}}}EncryptedAssertion") or len(assertions) != 1:
        raise SsoError("saml_wrapping")                       # A66: exactly one assertion
    doc_assertion = assertions[0]
    from lxml import etree

    if any(True for _ in root.iter(etree.Comment)) or any(True for _ in root.iter(etree.ProcessingInstruction)):
        # Comments inside a signed value (NameID "user@corp.test<!---->.evil") survive exclusive
        # canonicalisation; a parser that reads `.text` would see a truncated identity. Refused.
        raise SsoError("saml_malformed")
    issuer_el = doc_assertion.find(f"{{{SAML}}}Issuer")
    provider = _find_provider(db, issuer_el.text.strip() if issuer_el is not None and issuer_el.text else None)
    signed = _verify_signature(root, provider)
    # Only the signed element is trusted, and it must BE the document's one assertion (or the
    # Response that contains it). A wrapped copy elsewhere means two assertions: refused above.
    if signed.tag == f"{{{SAML}}}Assertion":
        if signed.get("ID") != doc_assertion.get("ID") or doc_assertion.getparent() is not root:
            raise SsoError("saml_wrapping")
        assertion = signed
    elif signed.tag == f"{{{SAMLP}}}Response":
        if signed.get("ID") != root.get("ID"):
            raise SsoError("saml_wrapping")
        inner = signed.findall(f"{{{SAML}}}Assertion")
        if len(inner) != 1 or inner[0].get("ID") != doc_assertion.get("ID"):
            raise SsoError("saml_wrapping")
        assertion = inner[0]
    else:
        raise SsoError("saml_wrapping")
    acs = acs_url(origin)
    destination = root.get("Destination")
    if destination is not None and destination != acs:
        raise SsoError("saml_destination")
    status = root.find(f"{{{SAMLP}}}Status/{{{SAMLP}}}StatusCode")
    if status is None or status.get("Value") != "urn:oasis:names:tc:SAML:2.0:status:Success":
        raise SsoError("saml_status")
    a_issuer = assertion.find(f"{{{SAML}}}Issuer")
    if a_issuer is None or (a_issuer.text or "").strip() != provider.issuer:
        raise SsoError("issuer_mismatch")
    now = utcnow()
    conditions = assertion.find(f"{{{SAML}}}Conditions")
    if conditions is None:
        raise SsoError("saml_conditions")
    nb, noa = _time(conditions.get("NotBefore")), _time(conditions.get("NotOnOrAfter"))
    if noa is None or now >= noa + SKEW or (nb is not None and now < nb - SKEW):
        raise SsoError("saml_expired")
    audiences = [(a.text or "").strip() for a in conditions.findall(
        f"{{{SAML}}}AudienceRestriction/{{{SAML}}}Audience")]
    if sp_entity_id(provider, origin) not in audiences:
        raise SsoError("audience_mismatch")
    subject = assertion.find(f"{{{SAML}}}Subject")
    name_id = subject.find(f"{{{SAML}}}NameID") if subject is not None else None
    if name_id is None or not (name_id.text or "").strip():
        raise SsoError("saml_subject")
    confirmation = subject.find(f"{{{SAML}}}SubjectConfirmation/{{{SAML}}}SubjectConfirmationData")
    if confirmation is None or confirmation.get("Recipient") != acs:
        raise SsoError("saml_recipient")
    c_noa = _time(confirmation.get("NotOnOrAfter"))
    if c_noa is not None and now >= c_noa + SKEW:
        raise SsoError("saml_expired")
    in_response_to = confirmation.get("InResponseTo") or ""
    next_path: Optional[str] = None
    unsolicited = not in_response_to and root.get("InResponseTo") is None
    if unsolicited and provider.allow_idp_initiated:
        # IdP-initiated (opt-in): nothing to answer, so nothing binds it to a browser. It must be
        # fresh and short-lived, and its assertion id is spent below like every other.
        issued = _time(assertion.get("IssueInstant"))
        if c_noa is None or issued is None or abs(now - issued) > UNSOLICITED_MAX_AGE + SKEW:
            raise SsoError("saml_unsolicited")
        next_path = safe_next(relay_state, origin=origin)
    else:
        if not in_response_to or (root.get("InResponseTo") not in (None, in_response_to)):
            raise SsoError("saml_unsolicited")                # A68
        ch = challenge_by_secret(db, kind="saml_request", secret=in_response_to, binding_secret=preauth,
                                 require_binding=True)
        if ch is None or ch.payload.get("provider_id") != provider.id or not consume_challenge(db, ch.id_hash):
            raise SsoError("saml_unsolicited")
        next_path = safe_next(ch.payload.get("next"))
    _spend_assertion_id(db, assertion.get("ID") or "", noa)
    attrs = _attributes(assertion)
    email = next((attrs[k][0] for k in EMAIL_ATTRIBUTES if attrs.get(k)), None)
    if email is None and name_id.get("Format", "").endswith("emailAddress"):
        email = name_id.text.strip()
    name = next((attrs[k][0] for k in NAME_ATTRIBUTES if attrs.get(k)), None)
    groups = tuple(attrs.get(provider.groups_claim, ())) if provider.groups_claim else ()
    ctx_ref = assertion.find(f"{{{SAML}}}AuthnStatement/{{{SAML}}}AuthnContext/{{{SAML}}}AuthnContextClassRef")
    mfa = bool(provider.trust_idp_mfa and ctx_ref is not None
               and (ctx_ref.text or "").strip() in trusted_mfa_contexts(provider))
    # M1: a SAML IdP's signed assertion vouches for an email ONLY in a domain the admin listed on
    # the provider (its `domains`). Issuers are free text, and any IdP can name any address.
    trusted = {d.lower().lstrip("@") for d in (provider.domains or ())}
    domain = email.rsplit("@", 1)[1].lower() if email and "@" in email else ""
    return SamlResult(provider, FederatedIdentity(
        issuer=provider.issuer or "", subject=name_id.text.strip(), email=email,
        email_verified=bool(email and domain in trusted), name=name, groups=groups, mfa=mfa),
        next_path=next_path, idp_initiated=unsolicited)


def _spend_assertion_id(db: Any, assertion_id: str, expires: datetime) -> None:
    """The replay cache: each assertion id is stored once (a spent challenge row); a second use
    collides on the primary key (A67)."""
    from sqlalchemy.exc import IntegrityError

    if not assertion_id:
        raise SsoError("saml_malformed")
    ttl = max(60, int((expires + SKEW - utcnow()).total_seconds()) + 60)
    nested = db.begin_nested()
    try:
        id_hash = create_challenge(db, kind="saml_request", binding_secret=None, ttl_seconds=ttl,
                                   secret="assertion:" + assertion_id)
        consume_challenge(db, id_hash)
        nested.commit()
    except IntegrityError:
        nested.rollback()
        raise SsoError("saml_replay") from None


def _attributes(assertion: Any) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for attr in assertion.findall(f"{{{SAML}}}AttributeStatement/{{{SAML}}}Attribute"):
        values = [(v.text or "").strip() for v in attr.findall(f"{{{SAML}}}AttributeValue") if (v.text or "").strip()]
        for key in (attr.get("Name"), attr.get("FriendlyName")):
            if key and values:
                out.setdefault(key, values)
    return out
