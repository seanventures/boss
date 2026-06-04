from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from secrets import token_urlsafe
from typing import Any, Optional
import xml.etree.ElementTree as ET

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .core import emit_event, make_id
from .members import (
    MemberDirectoryService,
    ProjectMemberNotFoundError,
    ProjectMemberPermissionError,
)


SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
SAML_PROTOCOL_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"


class SamlRuntimeError(RuntimeError):
    pass


class SamlConfigurationError(SamlRuntimeError):
    pass


class SamlAuthenticationError(SamlRuntimeError):
    pass


@dataclass
class SamlPrincipal:
    member_id: str
    subject: str
    provider_id: str
    claims: dict[str, Any]


@dataclass
class SamlSession:
    id: str
    token: str
    member_id: str
    provider_id: str
    expires_at: str


def _normalize_fingerprint(value: Optional[str]) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    for prefix in ("sha256:", "sha1:"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix) :]
    return "".join(ch for ch in lowered if ch.isalnum())


def _parse_saml_time(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _element_text(root: ET.Element, path: str) -> Optional[str]:
    item = root.find(path)
    if item is None or item.text is None:
        return None
    text_value = item.text.strip()
    return text_value or None


def _canonicalize(element: ET.Element) -> bytes:
    xml = ET.tostring(element, encoding="unicode")
    canonical = ET.canonicalize(xml_data=xml, with_comments=False)
    return canonical.encode("utf-8")


def _find_by_id(root: ET.Element, element_id: str) -> Optional[ET.Element]:
    for element in root.iter():
        if element.attrib.get("ID") == element_id:
            return element
    return None


def _remove_signature(element: ET.Element) -> None:
    for child in list(element):
        if child.tag == f"{{{DSIG_NS}}}Signature":
            element.remove(child)
        else:
            _remove_signature(child)


def _attributes_from_assertion(assertion: ET.Element) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for attribute in assertion.findall(f".//{{{SAML_NS}}}Attribute"):
        name = attribute.attrib.get("Name") or attribute.attrib.get("FriendlyName")
        if not name:
            continue
        values = [
            value.text.strip()
            for value in attribute.findall(f"{{{SAML_NS}}}AttributeValue")
            if value.text and value.text.strip()
        ]
        if not values:
            continue
        attributes[name] = values[0] if len(values) == 1 else values
    return attributes


class SamlAuthenticator:
    def __init__(self, settings: Settings):
        self.settings = settings

    def metadata(self, project_id: str, entity_id: str, base_url: str) -> str:
        acs_url = f"{base_url.rstrip('/')}/saml/v2/{project_id}/acs"
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" '
            f'entityID="{entity_id}">'
            '<SPSSODescriptor protocolSupportEnumeration='
            '"urn:oasis:names:tc:SAML:2.0:protocol">'
            '<AssertionConsumerService '
            'Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
            f'Location="{acs_url}" index="0" isDefault="true" />'
            "</SPSSODescriptor>"
            "</EntityDescriptor>"
        )

    def verify_response(
        self,
        saml_response: str,
        provider: dict[str, Any],
    ) -> SamlPrincipal:
        if not self.settings.enterprise_saml_enabled:
            raise SamlConfigurationError(
                "BOSS_ENTERPRISE_SAML_ENABLED is required before SAML login."
            )
        root = self._decode_response(saml_response)
        assertion = root.find(f".//{{{SAML_NS}}}Assertion")
        if assertion is None:
            raise SamlAuthenticationError("SAMLResponse did not include an Assertion.")
        signature = assertion.find(f"{{{DSIG_NS}}}Signature")
        if signature is None:
            raise SamlAuthenticationError(
                "SAML Assertion must include an enveloped XML signature."
            )

        certificate = self._certificate_from_signature(signature)
        self._verify_certificate_fingerprint(certificate, provider)
        self._verify_signature(root, assertion, signature, certificate)
        self._verify_conditions(assertion, provider)

        claims = _attributes_from_assertion(assertion)
        subject = _element_text(assertion, f".//{{{SAML_NS}}}NameID") or ""
        if not subject:
            raise SamlAuthenticationError("SAML Assertion is missing Subject NameID.")
        member_attribute = (
            provider.get("metadata", {}).get("saml_member_attribute")
            if isinstance(provider.get("metadata"), dict)
            else None
        ) or self.settings.enterprise_saml_member_attribute
        member_id = claims.get(str(member_attribute)) or claims.get("member_id") or subject
        if isinstance(member_id, list):
            member_id = member_id[0] if member_id else ""
        if not isinstance(member_id, str) or not member_id.strip():
            raise SamlAuthenticationError(
                "SAML Assertion did not resolve to a BOSS project member."
            )
        claims["subject"] = subject
        claims["issuer"] = _element_text(assertion, f"{{{SAML_NS}}}Issuer")
        return SamlPrincipal(
            member_id=member_id.strip(),
            subject=subject,
            provider_id=provider["id"],
            claims=claims,
        )

    def _decode_response(self, saml_response: str) -> ET.Element:
        try:
            decoded = base64.b64decode(saml_response, validate=True)
        except ValueError as error:
            raise SamlAuthenticationError("SAMLResponse must be valid base64.") from error
        if len(decoded) > 256_000:
            raise SamlAuthenticationError("SAMLResponse is too large.")
        if b"<!DOCTYPE" in decoded.upper():
            raise SamlAuthenticationError("SAMLResponse must not include a DOCTYPE.")
        try:
            return ET.fromstring(decoded)
        except ET.ParseError as error:
            raise SamlAuthenticationError("SAMLResponse XML is invalid.") from error

    def _certificate_from_signature(self, signature: ET.Element) -> x509.Certificate:
        raw_certificate = _element_text(
            signature,
            f".//{{{DSIG_NS}}}X509Certificate",
        )
        if not raw_certificate:
            raise SamlAuthenticationError(
                "SAML signature must include an X509Certificate."
            )
        try:
            return x509.load_der_x509_certificate(base64.b64decode(raw_certificate))
        except ValueError as error:
            raise SamlAuthenticationError(
                "SAML signature X509Certificate is invalid."
            ) from error

    def _verify_certificate_fingerprint(
        self,
        certificate: x509.Certificate,
        provider: dict[str, Any],
    ) -> None:
        expected = _normalize_fingerprint(provider.get("certificate_fingerprint"))
        sha256 = certificate.fingerprint(hashes.SHA256()).hex()
        sha1 = certificate.fingerprint(hashes.SHA1()).hex()  # noqa: S324
        if expected not in {_normalize_fingerprint(sha256), _normalize_fingerprint(sha1)}:
            raise SamlAuthenticationError(
                "SAML signing certificate fingerprint does not match provider metadata."
            )

    def _verify_signature(
        self,
        root: ET.Element,
        assertion: ET.Element,
        signature: ET.Element,
        certificate: x509.Certificate,
    ) -> None:
        signed_info = signature.find(f"{{{DSIG_NS}}}SignedInfo")
        signature_value = _element_text(signature, f"{{{DSIG_NS}}}SignatureValue")
        if signed_info is None or not signature_value:
            raise SamlAuthenticationError("SAML signature is incomplete.")
        signature_method = signed_info.find(f"{{{DSIG_NS}}}SignatureMethod")
        algorithm = signature_method.attrib.get("Algorithm") if signature_method is not None else ""
        if algorithm != RSA_SHA256:
            raise SamlAuthenticationError(
                "Only RSA-SHA256 SAML signatures are supported."
            )
        self._verify_reference_digest(root, assertion, signed_info)

        public_key = certificate.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise SamlAuthenticationError("SAML signing certificate must use RSA.")
        try:
            public_key.verify(
                base64.b64decode(signature_value),
                _canonicalize(signed_info),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError) as error:
            raise SamlAuthenticationError("SAML signature verification failed.") from error

    def _verify_reference_digest(
        self,
        root: ET.Element,
        assertion: ET.Element,
        signed_info: ET.Element,
    ) -> None:
        reference = signed_info.find(f"{{{DSIG_NS}}}Reference")
        if reference is None:
            raise SamlAuthenticationError("SAML signature reference is missing.")
        uri = reference.attrib.get("URI", "")
        if not uri.startswith("#"):
            raise SamlAuthenticationError("SAML signature reference must target an ID.")
        target_id = uri[1:]
        target = _find_by_id(root, target_id)
        if target is None or target.attrib.get("ID") != assertion.attrib.get("ID"):
            raise SamlAuthenticationError(
                "SAML signature must reference the signed Assertion."
            )
        digest_method = reference.find(f"{{{DSIG_NS}}}DigestMethod")
        digest_algorithm = (
            digest_method.attrib.get("Algorithm") if digest_method is not None else ""
        )
        if digest_algorithm != SHA256:
            raise SamlAuthenticationError("Only SHA-256 SAML digests are supported.")
        expected_digest = _element_text(reference, f"{{{DSIG_NS}}}DigestValue")
        if not expected_digest:
            raise SamlAuthenticationError("SAML signature digest is missing.")
        target_copy = deepcopy(target)
        _remove_signature(target_copy)
        actual_digest = base64.b64encode(
            hashlib.sha256(_canonicalize(target_copy)).digest()
        ).decode("ascii")
        if actual_digest != expected_digest:
            raise SamlAuthenticationError("SAML signature digest mismatch.")

    def _verify_conditions(
        self,
        assertion: ET.Element,
        provider: dict[str, Any],
    ) -> None:
        issuer = _element_text(assertion, f"{{{SAML_NS}}}Issuer")
        expected_issuer = provider.get("issuer")
        if expected_issuer and issuer != expected_issuer:
            raise SamlAuthenticationError("SAML issuer does not match provider metadata.")
        conditions = assertion.find(f"{{{SAML_NS}}}Conditions")
        now = datetime.now(timezone.utc)
        if conditions is not None:
            not_before = conditions.attrib.get("NotBefore")
            not_on_or_after = conditions.attrib.get("NotOnOrAfter")
            if not_before and now < _parse_saml_time(not_before):
                raise SamlAuthenticationError("SAML Assertion is not valid yet.")
            if not_on_or_after and now >= _parse_saml_time(not_on_or_after):
                raise SamlAuthenticationError("SAML Assertion has expired.")
        audiences = [
            audience.text.strip()
            for audience in assertion.findall(f".//{{{SAML_NS}}}Audience")
            if audience.text and audience.text.strip()
        ]
        expected_audience = provider.get("audience") or provider.get("entity_id")
        if expected_audience and expected_audience not in audiences:
            raise SamlAuthenticationError(
                "SAML Assertion audience does not match provider metadata."
            )


class SamlSessionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.members = MemberDirectoryService(settings)
        self.authenticator = SamlAuthenticator(settings)

    def active_provider(
        self,
        connection: Connection,
        project_id: str,
    ) -> Optional[dict[str, Any]]:
        row = connection.execute(
            text(
                """
                SELECT id, org_id, project_id, provider_key, issuer, audience,
                       entity_id, certificate_fingerprint, metadata
                FROM enterprise_identity_providers
                WHERE project_id = :project_id
                  AND provider_type = 'saml'
                  AND status = 'active'
                  AND jsonb_array_length(blocking_gaps) = 0
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"project_id": project_id},
        ).mappings().first()
        return dict(row) if row else None

    def metadata(self, connection: Connection, project_id: str, base_url: str) -> str:
        provider = self.active_provider(connection, project_id)
        entity_id = (
            provider.get("entity_id")
            if provider
            else f"urn:boss:{project_id}:saml:service-provider"
        )
        return self.authenticator.metadata(project_id, str(entity_id), base_url)

    def create_session(
        self,
        connection: Connection,
        project_id: str,
        saml_response: str,
    ) -> SamlSession:
        provider = self.active_provider(connection, project_id)
        if not provider:
            emit_event(
                connection,
                project_id,
                "enterprise.saml_runtime_blocked",
                "saml",
                "enterprise_identity_provider",
                project_id,
                {"reason": "active_saml_provider_missing"},
            )
            raise SamlConfigurationError(
                "No active non-blocked SAML identity provider is configured."
            )
        try:
            principal = self.authenticator.verify_response(saml_response, provider)
            self.members.require_member(
                connection,
                project_id,
                principal.member_id,
                permission="command_submit",
            )
        except (SamlRuntimeError, ProjectMemberNotFoundError, ProjectMemberPermissionError) as error:
            emit_event(
                connection,
                project_id,
                "enterprise.saml_login_failed",
                "saml",
                "enterprise_identity_provider",
                provider["id"],
                {"reason": str(error)},
            )
            if isinstance(error, SamlRuntimeError):
                raise
            raise SamlAuthenticationError(
                "SAML principal is not an authorized BOSS project member."
            ) from error

        token = f"boss_saml_{token_urlsafe(32)}"
        token_hash = self.token_hash(token)
        session_id = make_id("saml_session")
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=max(5, self.settings.enterprise_saml_session_ttl_minutes)
        )
        connection.execute(
            text(
                """
                INSERT INTO enterprise_saml_sessions (
                    id, org_id, project_id, provider_id, member_id, subject,
                    token_hash, status, claims, expires_at
                )
                VALUES (
                    :id, :org_id, :project_id, :provider_id, :member_id,
                    :subject, :token_hash, 'active', CAST(:claims AS JSONB),
                    :expires_at
                )
                """
            ),
            {
                "id": session_id,
                "org_id": provider["org_id"],
                "project_id": project_id,
                "provider_id": provider["id"],
                "member_id": principal.member_id,
                "subject": principal.subject,
                "token_hash": token_hash,
                "claims": json.dumps(principal.claims),
                "expires_at": expires_at,
            },
        )
        emit_event(
            connection,
            project_id,
            "enterprise.saml_login_succeeded",
            "saml",
            "enterprise_saml_session",
            session_id,
            {"provider_id": provider["id"], "member_id": principal.member_id},
        )
        return SamlSession(
            id=session_id,
            token=token,
            member_id=principal.member_id,
            provider_id=provider["id"],
            expires_at=expires_at.isoformat(),
        )

    def authenticate_token(
        self,
        connection: Connection,
        token: str,
    ) -> Optional[dict[str, Any]]:
        token_hash = self.token_hash(token)
        row = connection.execute(
            text(
                """
                UPDATE enterprise_saml_sessions
                SET last_seen_at = now()
                WHERE token_hash = :token_hash
                  AND status = 'active'
                  AND expires_at > now()
                RETURNING id, project_id, member_id, provider_id, expires_at
                """
            ),
            {"token_hash": token_hash},
        ).mappings().first()
        return dict(row) if row else None

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
