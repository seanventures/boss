"""THE BOSS FLEET CA: host certificates, host SVID intermediates and the host proof (P8a,
BOSSAPP-1063; boss-agent-hosts-lld.md §3.2.2, §3.12.1, D4, D18).

One CA per install (`fleet_ca`), an EC P-256 key sealed with a key derived from the identity
vault's KEK (vault.derived_key), so a database dump alone never yields it.

  host certificate      24 h, clientAuth, SAN URI spiffe://<trust domain>/host/<host_id>.
                        Issued at enrol from the host's CSR (the key is born on the host and never
                        leaves it) and renewed at two thirds of its life. REVOCATION IS REFUSING
                        THE RENEWAL — and, here, refusing every call the revoked host makes.
  host SVID intermediate  24 h, CA:true pathlen 0, permitted URI subtree = the trust domain; the
                        host mints its VMs' X.509-SVIDs (1 h) under it (§3.12.1). Optional at enrol
                        (a second CSR).

HOW A HOST AUTHENTICATES A CALL (the control-plane side of "mTLS with BOSS as CA"). Two forms,
one outcome (the host row the certificate is pinned to):

  1. Proof of possession, transport-independent (what the process backend and CI use, and what
     works through any TLS terminator):
        Authorization: BossHost <compact JWS>
        header  {"alg": "ES256" | "EdDSA", "typ": "boss-host+jwt", "x5t#S256": b64url(sha256(DER))}
        claims  {"htm": "POST", "htu": "/v1/hosts/<id>/heartbeat", "iat": <unix s>, "jti": <16+ chars>}
     `alg` must match the key type of the PINNED certificate (never the header's choice), `htu` is
     the request path, `iat` within 60 s (+30 s skew), and `jti` is single-use (the DPoP replay
     cache, identity_dpop_replay).
  2. mTLS terminated in front of the API: the terminator verifies the chain against the fleet CA
     and forwards the leaf as `X-Boss-Client-Cert` (URL-escaped PEM). Honoured only from a peer in
     BOSS_HOST_CERT_TERMINATORS (empty by default: form 2 off); the certificate's signature is
     re-checked against the CA here.

Either way the certificate must be one this CA issued (agent_certs, by SHA-256 fingerprint), not
expired, not revoked, and its host not revoked or retired.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import unquote

from sqlalchemy import text

HOST_CERT_HOURS = 24
#: A host renews at two thirds of its certificate's life (§3.2.2).
RENEW_FRACTION = 2 / 3
PROOF_TYP = "boss-host+jwt"
PROOF_MAX_AGE_SECONDS = 60
PROOF_SKEW_SECONDS = 30
FORWARDED_CERT_HEADER = "x-boss-client-cert"
AUTH_SCHEME = "bosshost"


class FleetAuthError(Exception):
    """A host credential that does not hold. `code` is safe to show; never any credential."""

    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code, self.status = code, status


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def trust_domain() -> str:
    """`<install>.boss` (§3.12.1). The install name is BOSS_FLEET_INSTALL (default `local`)."""
    name = (os.environ.get("BOSS_FLEET_INSTALL") or "local").strip().lower() or "local"
    return f"{name}.boss"


def host_spiffe_id(host_id: str) -> str:
    return f"spiffe://{trust_domain()}/host/{host_id}"


def vm_spiffe_id(host_id: str, vm_id: str) -> str:
    return f"{host_spiffe_id(host_id)}/vm/{vm_id}"


# ── The CA ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _CA:
    id: str
    cert: Any
    key: Any
    cert_pem: str


_CA_CACHE: dict = {}
_CA_LOCK = threading.Lock()


def _seal_key() -> bytes:
    from ..identity import vault

    return vault.derived_key("fleet-ca/v1")


def _seal(raw: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return (nonce + AESGCM(_seal_key()).encrypt(nonce, raw, b"boss-fleet-ca")).hex()


def _unseal(sealed: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = bytes.fromhex(sealed)
    return AESGCM(_seal_key()).decrypt(blob[:12], blob[12:], b"boss-fleet-ca")


def ensure_ca(connection: Any) -> _CA:
    """The install's fleet CA, created on first use (EC P-256, 10 years, self-signed)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    row = connection.execute(text(
        "SELECT id, cert_pem, key_sealed FROM fleet_ca ORDER BY created_at LIMIT 1")).first()
    if row is None:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BOSS"),
                          x509.NameAttribute(NameOID.COMMON_NAME,
                                             f"BOSS fleet CA ({trust_domain()})")])
        now = _now()
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(minutes=5))
                .not_valid_after(now + _dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                             key_encipherment=False, data_encipherment=False,
                                             key_agreement=False, key_cert_sign=True,
                                             crl_sign=True, encipher_only=False,
                                             decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                               critical=False)
                .sign(key, hashes.SHA256()))
        pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        raw = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        connection.execute(text(
            "INSERT INTO fleet_ca (id, cert_pem, key_sealed, install_domain) "
            "VALUES ('fleet_ca', :c, :k, :d) ON CONFLICT (id) DO NOTHING"),
            {"c": pem, "k": _seal(raw), "d": trust_domain()})
        row = connection.execute(text(
            "SELECT id, cert_pem, key_sealed FROM fleet_ca ORDER BY created_at LIMIT 1")).first()
    with _CA_LOCK:
        hit = _CA_CACHE.get(row[1])
        if hit is not None:
            return hit
        cert = x509.load_pem_x509_certificate(row[1].encode("ascii"))
        try:
            raw = _unseal(row[2])
        except Exception as exc:  # noqa: BLE001 — InvalidTag: another master key sealed it
            raise RuntimeError("the fleet CA cannot be unsealed with this install's master key "
                               "(BOSS_MASTER_KEY changed since the CA was made); restore that "
                               "key, or retire the CA and re-enrol every host") from exc
        key = serialization.load_der_private_key(raw, password=None)
        ca = _CA(str(row[0]), cert, key, str(row[1]))
        _CA_CACHE[row[1]] = ca
        return ca


def ca_pem(connection: Any) -> str:
    return ensure_ca(connection).cert_pem


# ── Issuing ─────────────────────────────────────────────────────────────────────────────────

def fingerprint_of(cert: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _csr(csr_pem: str) -> Any:
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    try:
        csr = x509.load_pem_x509_csr(str(csr_pem or "").encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise FleetAuthError("csr_unreadable", 400) from exc
    if not csr.is_signature_valid:
        raise FleetAuthError("csr_signature_invalid", 400)
    key = csr.public_key()
    if isinstance(key, ec.EllipticCurvePublicKey):
        if key.curve.name != "secp256r1":
            raise FleetAuthError("csr_key_must_be_p256_or_ed25519", 400)
    elif not isinstance(key, ed25519.Ed25519PublicKey):
        raise FleetAuthError("csr_key_must_be_p256_or_ed25519", 400)
    return csr


@dataclass(frozen=True)
class Issued:
    cert_pem: str
    fingerprint: str
    not_after: _dt.datetime
    serial: str


def _issue(connection: Any, *, org_id: str, host_id: str, csr_pem: str, kind: str,
           hours: int = HOST_CERT_HOURS, address: Optional[str] = None) -> Issued:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca = ensure_ca(connection)
    csr = _csr(csr_pem)
    now = _now()
    not_after = now + _dt.timedelta(hours=hours)
    serial = x509.random_serial_number()
    spiffe = host_spiffe_id(host_id)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([
                   x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"BOSS org {org_id}"),
                   x509.NameAttribute(NameOID.COMMON_NAME,
                                      host_id if kind in ("host", "director")
                                      else f"{host_id} SVID issuer")]))
               .issuer_name(ca.cert.subject).public_key(csr.public_key()).serial_number(serial)
               .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(not_after)
               .add_extension(x509.SubjectAlternativeName(
                   [x509.UniformResourceIdentifier(spiffe)]
                   + ([x509.IPAddress(ipaddress.ip_address(address))] if address else [])),
                   critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                   ca.key.public_key()), critical=False))
    if kind in ("host", "director"):
        # A director (boss-lbd) also SERVES: hosts push withdraws to it over mTLS and verify
        # its certificate against the fleet CA, so it carries server auth too (P8b-10).
        ekus = [ExtendedKeyUsageOID.CLIENT_AUTH]
        if kind == "director":
            ekus.append(ExtendedKeyUsageOID.SERVER_AUTH)
        builder = (builder
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(
                       digital_signature=True, content_commitment=False, key_encipherment=False,
                       data_encipherment=False, key_agreement=False, key_cert_sign=False,
                       crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage(ekus), critical=False))
    else:
        # The host's SVID issuer: it may sign VM leaf SVIDs only (pathlen 0), only inside the
        # install's trust domain. URI name constraints bind the host part; the `/host/<id>/`
        # path prefix is checked by every SVID verifier (P8b).
        builder = (builder
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(
                       digital_signature=True, content_commitment=False, key_encipherment=False,
                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                       crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.NameConstraints(
                       permitted_subtrees=[x509.UniformResourceIdentifier(trust_domain())],
                       excluded_subtrees=None), critical=True))
    cert = builder.sign(ca.key, hashes.SHA256())
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    fp = fingerprint_of(cert)
    public = csr.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    connection.execute(text("""
        INSERT INTO agent_certs (serial, org_id, kind, host_id, fingerprint, subject,
                                 public_key_pem, not_after)
        VALUES (:s, :o, :k, :h, :f, :subj, :pub, :na)"""),
        {"s": format(serial, "x"), "o": org_id, "k": kind, "h": host_id, "f": fp,
         "subj": spiffe, "pub": public.decode("ascii"), "na": not_after})
    return Issued(pem, fp, not_after, format(serial, "x"))


def issue_director_cert(connection: Any, *, org_id: str, director_id: str, csr_pem: str,
                        address: Optional[str] = None) -> Issued:
    """A director's 24 h certificate (client + server auth, IP SAN = its address)."""
    return _issue(connection, org_id=org_id, host_id=director_id, csr_pem=csr_pem,
                  kind="director", address=address)


def issue_host_cert(connection: Any, *, org_id: str, host_id: str, csr_pem: str) -> Issued:
    return _issue(connection, org_id=org_id, host_id=host_id, csr_pem=csr_pem, kind="host")


def issue_svid_intermediate(connection: Any, *, org_id: str, host_id: str,
                            csr_pem: str) -> Issued:
    return _issue(connection, org_id=org_id, host_id=host_id, csr_pem=csr_pem, kind="host_svid")


def renew_after(issued: Issued) -> _dt.datetime:
    return issued.not_after - _dt.timedelta(hours=HOST_CERT_HOURS * (1 - RENEW_FRACTION))


# ── Verifying a host's call ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HostIdentity:
    host_id: str
    org_id: str
    fingerprint: str
    status: str
    role: str = "host"  # host | director (P8b-10: the same certificate shape, its own table)


#: The certificate must be one this CA issued, unexpired, AND the host's CURRENT one: a renewal
#: supersedes the certificate it replaced (agent_hosts.cert_fingerprint is the pin).
#: Hosts (agent_hosts) and directors (director_nodes, P8b-10) share the certificate shape; the
#: row says which (`role`), and the fleet router holds each to its own routes.
_CERT_ROWS = """
      SELECT c.host_id, c.org_id, c.fingerprint, c.public_key_pem, h.status, h.revoked_at,
             c.revoked_at AS cert_revoked_at, (h.cert_fingerprint = c.fingerprint) AS current,
             'host' AS role
        FROM agent_certs c JOIN agent_hosts h ON h.id = c.host_id
       WHERE c.fingerprint = :f AND c.kind = 'host' AND c.not_after > now()
      UNION ALL
      SELECT c.host_id, c.org_id, c.fingerprint, c.public_key_pem, n.status, n.revoked_at,
             c.revoked_at AS cert_revoked_at, (n.cert_fingerprint = c.fingerprint) AS current,
             'director' AS role
        FROM agent_certs c JOIN director_nodes n ON n.id = c.host_id
       WHERE c.fingerprint = :f AND c.kind = 'director' AND c.not_after > now()"""

_HOST_SQL = text(_CERT_ROWS)

_PINNED_AND_SPENT = text(f"""
    WITH cert AS ({_CERT_ROWS}),
    spent AS (
      -- the jti is spent only once the certificate matched: an unknown certificate fills
      -- nothing (the replay cache is not a sink for strangers' proofs)
      INSERT INTO identity_dpop_replay (jkt, jti, expires_at)
      SELECT :k, :j, :e WHERE EXISTS (SELECT 1 FROM cert)
      ON CONFLICT (jkt, jti) DO NOTHING RETURNING 1)
    SELECT cert.*, EXISTS (SELECT 1 FROM spent) AS fresh FROM cert""")


def _pinned(db: Any, fingerprint: str) -> tuple[HostIdentity, str]:
    return _host_of(db.execute(_HOST_SQL, {"f": fingerprint}).mappings().first())


def _host_of(row: Any) -> tuple[HostIdentity, str]:
    if row is None:
        raise FleetAuthError("host_certificate_unknown")
    if (row["revoked_at"] is not None or row["cert_revoked_at"] is not None
            or row["status"] == "retired"):
        raise FleetAuthError("host_revoked", 403)
    if not row["current"]:
        raise FleetAuthError("host_certificate_superseded")
    return (HostIdentity(str(row["host_id"]), str(row["org_id"]), str(row["fingerprint"]),
                         str(row["status"]), str(row.get("role") or "host")),
            str(row["public_key_pem"]))


def _verify_signature(public_pem: str, alg: str, signing_input: bytes, signature: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    key = serialization.load_pem_public_key(public_pem.encode("ascii"))
    try:
        if isinstance(key, ed25519.Ed25519PublicKey):
            if alg != "EdDSA":
                raise FleetAuthError("proof_bad_alg")
            key.verify(signature, signing_input)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            if alg != "ES256" or len(signature) != 64:
                raise FleetAuthError("proof_bad_alg")
            der = encode_dss_signature(int.from_bytes(signature[:32], "big"),
                                       int.from_bytes(signature[32:], "big"))
            key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        else:
            raise FleetAuthError("proof_bad_alg")
    except InvalidSignature as exc:
        raise FleetAuthError("proof_signature_invalid") from exc


def verify_proof(db: Any, token: str, *, htm: str, path: str,
                 now: Optional[float] = None) -> HostIdentity:
    """The `BossHost` proof (module doc, form 1). Raises FleetAuthError."""
    from ..identity import jwks

    try:
        parsed = jwks.split(token)
    except jwks.JoseError as exc:
        raise FleetAuthError("proof_malformed") from exc
    header, claims = parsed.header, parsed.claims
    if header.get("typ") != PROOF_TYP or "crit" in header:
        raise FleetAuthError("proof_bad_typ")
    x5t = header.get("x5t#S256")
    if not isinstance(x5t, str):
        raise FleetAuthError("proof_no_certificate")
    try:
        fingerprint = jwks.b64u_decode(x5t).hex()
    except jwks.JoseError as exc:
        raise FleetAuthError("proof_no_certificate") from exc
    # Everything that needs no database first, so a malformed or stale proof costs no query.
    if str(claims.get("htm") or "").upper() != htm.upper():
        raise FleetAuthError("proof_htm")
    if claims.get("htu") != path:
        raise FleetAuthError("proof_htu")
    at = time.time() if now is None else now
    iat = claims.get("iat")
    if (not isinstance(iat, int) or at - iat > PROOF_MAX_AGE_SECONDS + PROOF_SKEW_SECONDS
            or iat - at > PROOF_SKEW_SECONDS):
        raise FleetAuthError("proof_iat")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not (16 <= len(jti) <= 128):
        raise FleetAuthError("proof_jti")
    # ONE STATEMENT: the pinned certificate and the jti's single use (the DPoP replay cache).
    # A jti is spent even when the signature then fails — a forged proof cannot be retried
    # with the same jti, which costs an honest host nothing (every proof has a fresh one).
    expires = _dt.datetime.fromtimestamp(
        max(at, iat) + PROOF_MAX_AGE_SECONDS + 2 * PROOF_SKEW_SECONDS, _dt.timezone.utc)
    row = db.execute(_PINNED_AND_SPENT, {"f": fingerprint, "k": f"host:{fingerprint}",
                                         "j": jti, "e": expires}).mappings().first()
    host, public_pem = _host_of(row)
    _verify_signature(public_pem, str(header.get("alg") or ""), parsed.signing_input,
                      parsed.signature)
    if not row["fresh"]:
        raise FleetAuthError("proof_replay")
    if secrets.randbelow(200) == 0:
        db.execute(text("DELETE FROM identity_dpop_replay WHERE expires_at < now()"))
    return host


_PROOF_ENGINE: dict = {}


def on_proof_connection(fn: Any) -> Any:
    """`fn(connection)` on an AUTOCOMMIT connection of a small engine kept for host proofs. The
    app's engine pre-pings every checkout (a round trip); this one does not, and instead retries
    once on a connection the database dropped — the same resilience, one round trip fewer on
    every run-plane call."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import DBAPIError

    from ..db import get_engine

    base = get_engine()
    key = str(base.url)
    engine = _PROOF_ENGINE.get(key)
    if engine is None:
        engine = _PROOF_ENGINE[key] = create_engine(
            base.url, future=True, pool_pre_ping=False, pool_size=4, max_overflow=16,
            isolation_level="AUTOCOMMIT")
    for attempt in (1, 2):
        try:
            with engine.connect() as connection:
                return fn(connection)
        except DBAPIError as exc:
            if attempt == 2 or not exc.connection_invalidated:
                raise
    raise RuntimeError("unreachable")


TERMINATORS_ENV = "BOSS_HOST_CERT_TERMINATORS"


def is_cert_terminator(peer: Optional[str]) -> bool:
    """FORM 2 IS ONLY EVER BELIEVED FROM A NAMED TLS TERMINATOR: BOSS_HOST_CERT_TERMINATORS
    (addresses or CIDRs, comma-separated), EMPTY BY DEFAULT — never merely any
    BOSS_TRUSTED_PROXIES peer, since a public reverse proxy forwarding a client's own header
    would let anyone holding a (public) host certificate speak as that host. The terminator
    must verify the client certificate against the fleet CA and strip any inbound
    X-Boss-Client-Cert before setting its own."""
    import ipaddress

    from ..identity.authn_support import _trusted_networks

    raw = (os.environ.get(TERMINATORS_ENV) or "").strip()
    if not raw or not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer.split("%", 1)[0])
    except ValueError:
        return False
    return any(ip in n for n in _trusted_networks(raw))


def verify_forwarded_cert(db: Any, escaped_pem: str) -> HostIdentity:
    """Form 2: a leaf a trusted TLS terminator verified and forwarded. Re-checked against the CA."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        cert = x509.load_pem_x509_certificate(unquote(escaped_pem).encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise FleetAuthError("forwarded_certificate_unreadable") from exc
    ca = ensure_ca(db)
    try:
        ca.cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                    ec.ECDSA(cert.signature_hash_algorithm))
    except (InvalidSignature, Exception) as exc:  # noqa: BLE001
        raise FleetAuthError("forwarded_certificate_not_ours") from exc
    if cert.not_valid_after_utc <= _now():
        raise FleetAuthError("forwarded_certificate_expired")
    host, _ = _pinned(db, fingerprint_of(cert))
    return host


def sign_proof(private_key: Any, cert_pem: str, *, htm: str, path: str,
               iat: Optional[int] = None, jti: Optional[str] = None) -> str:
    """A `BossHost` proof (what boss-hostd sends; used by the Python fake host and tests)."""
    import json

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    from ..identity import jwks

    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    fp = bytes.fromhex(fingerprint_of(cert))
    alg = "EdDSA" if isinstance(private_key, ed25519.Ed25519PrivateKey) else "ES256"
    header = {"alg": alg, "typ": PROOF_TYP, "x5t#S256": jwks.b64u(fp)}
    claims = {"htm": htm.upper(), "htu": path, "iat": int(iat if iat is not None else time.time()),
              "jti": jti or secrets.token_urlsafe(18)}
    signing_input = (jwks.b64u(json.dumps(header, separators=(",", ":")).encode())
                     + "." + jwks.b64u(json.dumps(claims, separators=(",", ":")).encode()))
    if alg == "EdDSA":
        signature = private_key.sign(signing_input.encode("ascii"))
    else:
        assert isinstance(private_key, ec.EllipticCurvePrivateKey)
        r, s = decode_dss_signature(private_key.sign(signing_input.encode("ascii"),
                                                     ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{signing_input}.{jwks.b64u(signature)}"


def make_csr(private_key: Any, common_name: str = "boss-hostd") -> str:
    """A CSR for `private_key` (the fake host and tests; boss-hostd builds its own in Go)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.x509.oid import NameOID

    algorithm = None if isinstance(private_key, ed25519.Ed25519PrivateKey) else hashes.SHA256()
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
           .sign(private_key, algorithm))
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


__all__ = ["AUTH_SCHEME", "FORWARDED_CERT_HEADER", "FleetAuthError", "HOST_CERT_HOURS",
           "HostIdentity", "Issued", "ca_pem", "ensure_ca", "fingerprint_of",
           "host_spiffe_id", "issue_host_cert", "issue_svid_intermediate", "make_csr",
           "renew_after", "sign_proof", "trust_domain", "verify_forwarded_cert", "verify_proof",
           "vm_spiffe_id"]
