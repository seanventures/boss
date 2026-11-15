"""THE BOSS AGENT CA: each replica's serving certificate and service accounts' caller certificates
(P8b-9, BOSSAPP-1076; boss-agent-hosts-lld.md §3.17.2, §3.17.3, D7).

SEPARATE FROM THE FLEET CA. services/fleet_ca.py certifies hosts and directors (who may CALL the
control plane). This CA certifies AGENTS to their callers, and callers to agents:

    BOSS agent root CA (10 y, EC P-256, pathlen 1)
      ├── serving intermediate (5 y, pathlen 0)  → replica certificates, 24 h, serverAuth,
      │                                             SAN DNS <agent>.<project>.agents.<domain> + VIP
      └── caller intermediate  (5 y, pathlen 0)  → service-account client certificates (mTLS
                                                    callers), ≤ 30 d, clientAuth, scoped to
                                                    agent:invoke

THE KEY IS BORN IN THE VM. boss-runtime generates its key after the snapshot reseed, sends only
a CSR to `POST /v1/run-plane/replica/cert` through boss-hostd (which signs the call as its host
and sets X-Boss-Replica from the VM's socket). BOSS issues only when:
  * the replica is DESIRED: placed on the calling host, wanted running, of a live microvm
    deployment (run_plane.replica_for);
  * its host ATTESTS for serving: enrolled, ready, not revoked, a heartbeat under 10 s old, and on
    the Firecracker backend KVM and Firecracker live (the `process` backend is the parity lane:
    it may serve TLS, it never counts as strong isolation, services/strong_isolation.py);
  * its image is SIGNED or ACTIVE in vm_images (and never revoked); a Firecracker replica with no
    pinned image is refused.

RENEWAL at two thirds of the life (16 h) with a fresh CSR; the old certificate stays valid until
it expires, so live connections never drop (P8b-T4). REVOCATION is short life plus immediate
withdraw: a revoked serial is listed at /v1/pki/agent-ca (`revoked`), a revoked host or a retired
deployment gets no renewal, and AP-D11 counts only a replica's live, unrevoked certificate.

ACME (a publicly trusted certificate per deployment) and a customer-supplied CA with BOSS as a
sub-CA are the documented D7 options; neither is built here (docs/infra/HOST_API.md §10).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import os
import re
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sqlalchemy import text

from . import fleet_ca

REPLICA_CERT_HOURS = 24
RENEW_FRACTION = fleet_ca.RENEW_FRACTION
CALLER_CERT_DAYS = 7
CALLER_CERT_MAX_DAYS = 30
#: A heartbeat older than this means the host no longer attests (§3.17.4).
ATTESTATION_MAX_AGE_SECONDS = 10
ROOT_DAYS, INTERMEDIATE_DAYS = 3650, 1825
_LOCK_KEY = int.from_bytes(hashlib.sha256(b"boss.agent_ca").digest()[:8], "big", signed=True)
#: The only scope a caller certificate ever carries: it may invoke agents, nothing else.
CALLER_SCOPES = frozenset({"agent:invoke"})


class AgentCAError(Exception):
    """A refusal. `code` is safe to show; it never carries key material."""

    def __init__(self, code: str, status: int = 403, message: str = ""):
        super().__init__(message or code)
        self.code, self.status, self.message = code, status, message or code

    def body(self) -> dict:
        return {"error": self.code, "message": self.message}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ── The CA ──────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Cert:
    id: str
    cert: Any
    key: Any
    pem: str


@dataclass(frozen=True)
class AgentCA:
    root: _Cert
    serving: _Cert
    caller: _Cert

    def serving_bundle(self) -> str:
        """What a caller pins to reach an agent: the serving intermediate and the root."""
        return self.serving.pem + self.root.pem

    def caller_bundle(self) -> str:
        """What an agent's listener trusts for client certificates."""
        return self.caller.pem + self.root.pem


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def _seal_key() -> bytes:
    from ..identity import vault

    return vault.derived_key("agent-ca/v1")


def _seal(raw: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    return (nonce + AESGCM(_seal_key()).encrypt(nonce, raw, b"boss-agent-ca")).hex()


def _unseal(sealed: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = bytes.fromhex(sealed)
    return AESGCM(_seal_key()).decrypt(blob[:12], blob[12:], b"boss-agent-ca")


def _ca_cert(name: str, key: Any, *, issuer: Any, issuer_key: Any, days: int,
             path_length: int) -> Any:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID

    subject = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BOSS"),
                         x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = _now()
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer.subject if issuer is not None else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - _dt.timedelta(minutes=5))
               .not_valid_after(now + _dt.timedelta(days=days))
               .add_extension(x509.BasicConstraints(ca=True, path_length=path_length),
                              critical=True)
               .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                            key_encipherment=False, data_encipherment=False,
                                            key_agreement=False, key_cert_sign=True, crl_sign=True,
                                            encipher_only=False, decipher_only=False),
                              critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                              critical=False))
    if issuer_key is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False)
    return builder.sign(issuer_key if issuer_key is not None else key, hashes.SHA256())


def _pem(cert: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _store(connection: Any, kind: str, cert: Any, key: Any) -> None:
    from cryptography.hazmat.primitives import serialization

    raw = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    connection.execute(text(
        "INSERT INTO agent_ca (id, kind, cert_pem, key_sealed, install_domain, not_after) "
        "VALUES (:i, :k, :c, :s, :d, :na)"),
        {"i": f"agentca_{kind}_{secrets.token_hex(6)}", "k": kind, "c": _pem(cert),
         "s": _seal(raw), "d": fleet_ca.trust_domain(), "na": cert.not_valid_after_utc})


_LIVE = text("SELECT id, kind, cert_pem, key_sealed FROM agent_ca WHERE retired_at IS NULL")


def ensure_ca(connection: Any) -> AgentCA:
    """The install's agent CA, created on first use (one creator: an advisory lock)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    rows = {r["kind"]: r for r in connection.execute(_LIVE).mappings().all()}
    if len(rows) < 3:
        connection.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
        rows = {r["kind"]: r for r in connection.execute(_LIVE).mappings().all()}
        if "root" not in rows:
            root_key = ec.generate_private_key(ec.SECP256R1())
            root = _ca_cert(f"BOSS agent root CA ({fleet_ca.trust_domain()})", root_key,
                            issuer=None, issuer_key=None, days=ROOT_DAYS, path_length=1)
            _store(connection, "root", root, root_key)
            rows = {r["kind"]: r for r in connection.execute(_LIVE).mappings().all()}
        root_row = rows["root"]
        root_cert = x509.load_pem_x509_certificate(root_row["cert_pem"].encode("ascii"))
        root_key = serialization.load_der_private_key(_unseal(root_row["key_sealed"]), None)
        for kind, label in (("serving", "serving"), ("caller", "caller")):
            if kind not in rows:
                key = ec.generate_private_key(ec.SECP256R1())
                cert = _ca_cert(f"BOSS agent {label} CA ({fleet_ca.trust_domain()})", key,
                                issuer=root_cert, issuer_key=root_key, days=INTERMEDIATE_DAYS,
                                path_length=0)
                _store(connection, kind, cert, key)
        rows = {r["kind"]: r for r in connection.execute(_LIVE).mappings().all()}
    cache_key = tuple(sorted((k, r["id"]) for k, r in rows.items()))
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit is not None:
            return hit
        loaded = {}
        for kind in ("root", "serving", "caller"):
            row = rows[kind]
            try:
                raw = _unseal(row["key_sealed"])
            except Exception as exc:  # noqa: BLE001 — InvalidTag: another master key sealed it
                raise RuntimeError("the agent CA cannot be unsealed with this install's master "
                                   "key (BOSS_MASTER_KEY changed since it was made)") from exc
            loaded[kind] = _Cert(str(row["id"]),
                                 x509.load_pem_x509_certificate(row["cert_pem"].encode("ascii")),
                                 serialization.load_der_private_key(raw, None),
                                 str(row["cert_pem"]))
        ca = AgentCA(loaded["root"], loaded["serving"], loaded["caller"])
        _CACHE[cache_key] = ca
        return ca


def reset_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ── Names ───────────────────────────────────────────────────────────────────────────────────

def _label(value: Any) -> str:
    out = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return (out[:63].rstrip("-")) or "x"


def agent_domain() -> str:
    """`agents.<domain>`: BOSS_AGENT_DNS_DOMAIN when set, else the install's trust domain."""
    base = (os.environ.get("BOSS_AGENT_DNS_DOMAIN") or "").strip().strip(".").lower()
    return f"agents.{base or fleet_ca.trust_domain()}"


def agent_dns_name(deployment: dict) -> str:
    """`<agent>.<project>.agents.<domain>` (§3.17.2), or the deployment's own hostname."""
    named = str(deployment.get("hostname") or "").strip().strip(".").lower()
    if named and re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", named):
        return named
    return f"{_label(deployment.get('agent_key'))}.{_label(deployment.get('project_id'))}." \
           f"{agent_domain()}"


# ── Issuing a replica's serving certificate ─────────────────────────────────────────────────

_HOST = text("""
    SELECT id, org_id, backend, status, revoked_at, kvm_live, firecracker_live,
           (last_heartbeat_at IS NOT NULL
            AND last_heartbeat_at > now() - make_interval(secs => :age)) AS fresh
      FROM agent_hosts WHERE id = :h""")


def host_attests_for_serving(row: Any) -> Optional[str]:
    """Why this host may not hold a serving certificate now, or None (module doc)."""
    if row is None:
        return "host_unknown"
    if row["revoked_at"] is not None or row["status"] == "retired":
        return "host_revoked"
    if row["status"] != "ready":
        return f"host_{row['status']}"
    if not row["fresh"]:
        return "host_heartbeat_stale"
    if row["backend"] == "firecracker" and not (row["kvm_live"] and row["firecracker_live"]):
        return "host_not_attested"
    return None


def image_refusal(connection: Any, digest: Optional[str], *, backend: str) -> Optional[str]:
    """The image must be signed or active, and never revoked (§3.5)."""
    if not digest:
        return None if backend == "process" else "image_unpinned"
    row = connection.execute(text(
        "SELECT status, signature FROM vm_images WHERE digest = :d"), {"d": digest}
    ).mappings().first()
    if row is None:
        return "image_unknown"
    if row["status"] == "revoked":
        return "image_revoked"
    if row["status"] != "active" and not row["signature"]:
        return "image_unsigned"
    return None


def _live_serial(connection: Any, replica_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT serial FROM agent_certs WHERE kind = 'replica' AND replica_id = :r "
        "AND revoked_at IS NULL AND not_after > now() ORDER BY not_after DESC LIMIT 1"),
        {"r": replica_id}).scalar()


def issue_replica_cert(connection: Any, *, host_id: str, replica_id: str, csr_pem: str) -> dict:
    """A replica's 24 h serving certificate (module doc). Raises AgentCAError / RunPlaneError."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from .run_plane import replica_for

    replica = replica_for(connection, host_id, replica_id)
    host = connection.execute(_HOST, {"h": host_id, "age": ATTESTATION_MAX_AGE_SECONDS}
                              ).mappings().first()
    why = host_attests_for_serving(host)
    if why:
        raise AgentCAError(why, 403, "this replica's host does not attest; no certificate")
    dep = connection.execute(text(
        "SELECT id, project_id, agent_key, hostname, host(vip) AS vip, image_digest, "
        "isolation_required FROM agent_deployments WHERE id = :d"), {"d": replica["deployment_id"]}
    ).mappings().first()
    image = connection.execute(text("SELECT image_digest FROM agent_replicas WHERE id = :r"),
                               {"r": replica_id}).scalar() or dep["image_digest"]
    why = image_refusal(connection, image, backend=str(host["backend"]))
    if why:
        raise AgentCAError(why, 403, "this replica's image is not signed or active")
    try:
        csr = fleet_ca._csr(csr_pem)  # noqa: SLF001 — the same CSR rules as hosts
    except fleet_ca.FleetAuthError as exc:
        raise AgentCAError(exc.code, exc.status) from exc
    ca = ensure_ca(connection)
    dns = agent_dns_name(dict(dep))
    ips = [dep["vip"]] if dep["vip"] else []
    now = _now()
    not_after = now + _dt.timedelta(hours=REPLICA_CERT_HOURS)
    serial = x509.random_serial_number()
    spiffe = (f"spiffe://{fleet_ca.trust_domain()}/deployment/{dep['id']}/replica/{replica_id}")
    sans: list = [x509.DNSName(dns), x509.UniformResourceIdentifier(spiffe)]
    sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BOSS agents"),
                x509.NameAttribute(NameOID.COMMON_NAME, dns[:64])]))
            .issuer_name(ca.serving.cert.subject).public_key(csr.public_key())
            .serial_number(serial)
            .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca.serving.key.public_key()), critical=False)
            .sign(ca.serving.key, hashes.SHA256()))
    previous = _live_serial(connection, replica_id)
    record(connection, cert, kind="replica", org_id=str(host["org_id"]), host_id=host_id,
           deployment_id=str(dep["id"]), replica_id=replica_id, subject=spiffe,
           dns_names=[dns], ips=ips, renews=previous, public_key=csr.public_key())
    wake: list = []
    if dep["isolation_required"] and previous is None:
        # The replica just became eligible to serve an isolation-required VIP (AP-D11).
        from . import fleet_directors

        wake = fleet_directors.bump(connection, str(host["org_id"]))
    return {"certificate_pem": _pem(cert), "chain_pem": ca.serving_bundle(), "_wake": wake,
            "serial": format(serial, "x"), "not_after": not_after.isoformat(),
            "renew_after": renew_after(now, not_after).isoformat(),
            "dns_names": [dns], "ip_addresses": ips, "renews": previous}


def renew_after(not_before: _dt.datetime, not_after: _dt.datetime) -> _dt.datetime:
    """Two thirds into the life (§3.17.2)."""
    return not_before + (not_after - not_before) * RENEW_FRACTION


def record(connection: Any, cert: Any, *, kind: str, org_id: str, subject: str,
           public_key: Any, host_id: Optional[str] = None, deployment_id: Optional[str] = None,
           replica_id: Optional[str] = None, principal_id: Optional[str] = None,
           dns_names: Iterable[str] = (), ips: Iterable[str] = (),
           renews: Optional[str] = None) -> None:
    from cryptography.hazmat.primitives import serialization

    connection.execute(text("""
        INSERT INTO agent_certs (serial, org_id, kind, host_id, deployment_id, replica_id,
                                 principal_id, fingerprint, subject, public_key_pem, not_after,
                                 dns_names, ip_addresses, renews_serial)
        VALUES (:s, :o, :k, :h, :d, :r, :p, :f, :subj, :pub, :na, :dns, :ips, :ren)"""),
        {"s": format(cert.serial_number, "x"), "o": org_id, "k": kind, "h": host_id,
         "d": deployment_id, "r": replica_id, "p": principal_id,
         "f": fleet_ca.fingerprint_of(cert), "subj": subject,
         "pub": public_key.public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo
                                        ).decode("ascii"),
         "na": cert.not_valid_after_utc, "dns": list(dns_names), "ips": list(ips),
         "ren": renews})


# ── Revocation and the facts AP-D11 reads ───────────────────────────────────────────────────

def revoke(connection: Any, *, reason: str, serial: Optional[str] = None,
           replica_id: Optional[str] = None, deployment_id: Optional[str] = None,
           host_id: Optional[str] = None, org_id: Optional[str] = None) -> list:
    """Revoke live agent certificates (replica or caller) by one selector. Returns the serials."""
    if not any((serial, replica_id, deployment_id, host_id)):
        raise ValueError("name what to revoke")
    rows = connection.execute(text("""
        UPDATE agent_certs SET revoked_at = now(), revoked_reason = :why
         WHERE kind IN ('replica', 'caller') AND revoked_at IS NULL AND not_after > now()
           AND (CAST(:s AS TEXT) IS NULL OR serial = :s)
           AND (CAST(:r AS TEXT) IS NULL OR replica_id = :r)
           AND (CAST(:d AS TEXT) IS NULL OR deployment_id = :d)
           AND (CAST(:h AS TEXT) IS NULL OR (kind = 'replica' AND host_id = :h))
           AND (CAST(:o AS TEXT) IS NULL OR org_id = :o)
        RETURNING serial, credential_id"""),
        {"why": (reason or "revoked")[:200], "s": serial, "r": replica_id, "d": deployment_id,
         "h": host_id, "o": org_id}).all()
    credentials = [r[1] for r in rows if r[1]]
    if credentials:
        connection.execute(text(
            "UPDATE identity_api_credentials SET revoked_at = now(), revoked_by = 'agent_ca' "
            "WHERE id = ANY(:c) AND revoked_at IS NULL"), {"c": credentials})
    return [r[0] for r in rows]


def replicas_with_valid_cert(connection: Any, replica_ids: Iterable[str]) -> set:
    ids = [str(r) for r in replica_ids]
    if not ids:
        return set()
    return {str(r[0]) for r in connection.execute(text(
        "SELECT DISTINCT replica_id FROM agent_certs WHERE kind = 'replica' "
        "AND replica_id = ANY(:r) AND revoked_at IS NULL AND not_after > now()"),
        {"r": ids}).all()}


def revoked_serials(connection: Any, org_id: Optional[str] = None) -> list:
    """Unexpired, revoked agent certificates (a short list: revocation is short life + withdraw)."""
    return [r[0] for r in connection.execute(text(
        "SELECT serial FROM agent_certs WHERE kind IN ('replica', 'caller') "
        "AND revoked_at IS NOT NULL AND not_after > now() "
        "AND (CAST(:o AS TEXT) IS NULL OR org_id = :o) ORDER BY serial"), {"o": org_id}).all()]


def trust_document(connection: Any, org_id: Optional[str] = None) -> dict:
    """What callers and agents pin (`GET /v1/pki/agent-ca`)."""
    ca = ensure_ca(connection)
    return {"serving_bundle_pem": ca.serving_bundle(), "caller_bundle_pem": ca.caller_bundle(),
            "root_pem": ca.root.pem,
            "root_sha256": fleet_ca.fingerprint_of(ca.root.cert),
            "revoked": revoked_serials(connection, org_id),
            "replica_cert_hours": REPLICA_CERT_HOURS, "renew_fraction": RENEW_FRACTION,
            "dns_suffix": agent_domain(),
            "options": {"acme": "documented, not built (D7)",
                        "customer_ca": "documented, not built (D7: BOSS as a sub-CA)"}}


def published_anchor(connection: Any, *, base_url: str = "") -> Optional[dict]:
    """The trust anchor an agent card and an agent's JWKS name (§3.17.2: "named in the agent's
    signed A2A card"): where the bundle lives and the root's SHA-256. Read-only — it never
    creates the CA; None until one exists."""
    from cryptography import x509

    pem = connection.execute(text(
        "SELECT cert_pem FROM agent_ca WHERE kind = 'root' AND retired_at IS NULL LIMIT 1")
    ).scalar() if connection.execute(text("SELECT to_regclass('agent_ca')")).scalar() else None
    if not pem:
        return None
    root = x509.load_pem_x509_certificate(str(pem).encode("ascii"))
    return {"url": f"{base_url.rstrip('/')}/v1/pki/agent-ca",
            "root_sha256": fleet_ca.fingerprint_of(root)}


# ── Caller certificates (mTLS callers) ──────────────────────────────────────────────────────

def issue_caller_cert(connection: Any, *, org_id: str, principal_id: str, csr_pem: str,
                      days: int = CALLER_CERT_DAYS, actor: str = "") -> dict:
    """A service account's client certificate for calling agents over mTLS. Its scope is
    `agent:invoke` only, and every call is still decided by the PDP at admit."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from ..identity import users
    from ..identity.principal import PrincipalKind

    principal = users.load_principal(connection, principal_id)
    if (principal is None or principal.org_id != org_id
            or principal.kind != PrincipalKind.SERVICE_ACCOUNT):
        raise AgentCAError("not_a_service_account", 404,
                           "caller certificates are issued to this org's service accounts only")
    if not principal.active:
        raise AgentCAError("principal_inactive", 409)
    days = int(days or CALLER_CERT_DAYS)
    if not 1 <= days <= CALLER_CERT_MAX_DAYS:
        raise AgentCAError("days_out_of_range", 400,
                           f"a caller certificate lives 1 to {CALLER_CERT_MAX_DAYS} days")
    try:
        csr = fleet_ca._csr(csr_pem)  # noqa: SLF001
    except fleet_ca.FleetAuthError as exc:
        raise AgentCAError(exc.code, exc.status) from exc
    ca = ensure_ca(connection)
    now = _now()
    not_after = now + _dt.timedelta(days=days)
    uri = f"spiffe://{fleet_ca.trust_domain()}/caller/{principal_id}"
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"BOSS org {org_id}"),
                x509.NameAttribute(NameOID.COMMON_NAME, principal_id)]))
            .issuer_name(ca.caller.cert.subject).public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(not_after)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(uri)]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca.caller.key.public_key()), critical=False)
            .sign(ca.caller.key, hashes.SHA256()))
    record(connection, cert, kind="caller", org_id=org_id, principal_id=principal_id,
           subject=uri, public_key=csr.public_key())
    # THE CERTIFICATE IS ONE OF THE SERVICE ACCOUNT'S CREDENTIALS: a row beside its keys (scope
    # agent:invoke, the certificate's expiry), so the PDP's live credential check, suspension,
    # revocation from the console and the audit trail treat it exactly as a key. Its hash names
    # the certificate (`cert:` + SHA-256), which no bearer can ever hash to.
    serial = format(cert.serial_number, "x")
    credential_id = f"key_{secrets.token_hex(12)}"
    connection.execute(text(
        "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
        "expires_at, created_by) VALUES (:i, :p, 'sak', :n, :h, :s, :x, :by)"),
        {"i": credential_id, "p": principal_id, "n": f"mTLS caller certificate {serial[:12]}",
         "h": f"cert:{fleet_ca.fingerprint_of(cert)}", "s": sorted(CALLER_SCOPES),
         "x": not_after, "by": actor or "operator"})
    connection.execute(text("UPDATE agent_certs SET credential_id = :c WHERE serial = :s"),
                       {"c": credential_id, "s": serial})
    return {"credential_id": credential_id,"certificate_pem": _pem(cert), "chain_pem": ca.caller_bundle(),
            "serial": format(cert.serial_number, "x"), "not_after": not_after.isoformat(),
            "principal_id": principal_id, "scopes": sorted(CALLER_SCOPES)}


def caller_context(connection: Any, cert_pem: str) -> Any:
    """The AuthContext of a caller certificate an agent's listener verified and forwarded: it must
    chain to the caller intermediate, be one BOSS issued (by fingerprint), unexpired, unrevoked,
    and name a live service account. Scoped to agent:invoke. Raises AgentCAError (401).

    The handshake happened in the VM, so BOSS takes the replica's word that the caller held the
    key — the LLD's bound (§3.17.3): a compromised VM can at most replay a credential it was
    shown (a certificate presented to it, like a bearer), never mint one, and the PDP still
    decides every call. Callers who want more bind a token to the certificate and send both."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ec

    from ..identity import users
    from ..identity.principal import AuthContext, AuthMethod, CredentialRef, PrincipalKind

    try:
        cert = x509.load_pem_x509_certificate(str(cert_pem or "").encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise AgentCAError("caller_cert_unreadable", 401) from exc
    ca = ensure_ca(connection)
    try:
        ca.caller.cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes,
                                           ec.ECDSA(cert.signature_hash_algorithm))
    except (InvalidSignature, Exception) as exc:  # noqa: BLE001
        raise AgentCAError("caller_cert_not_ours", 401) from exc
    now = _now()
    if not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
        raise AgentCAError("caller_cert_expired", 401)
    row = connection.execute(text(
        "SELECT serial, principal_id, revoked_at, not_after, credential_id FROM agent_certs "
        "WHERE fingerprint = :f AND kind = 'caller'"),
        {"f": fleet_ca.fingerprint_of(cert)}).mappings().first()
    if row is None:
        raise AgentCAError("caller_cert_unknown", 401)
    if row["revoked_at"] is not None or not row["credential_id"]:
        raise AgentCAError("caller_cert_revoked", 401)
    principal = users.load_principal(connection, str(row["principal_id"]))
    if (principal is None or not principal.active
            or principal.kind != PrincipalKind.SERVICE_ACCOUNT):
        raise AgentCAError("caller_inactive", 401)
    return AuthContext(principal=principal, method=AuthMethod.API_KEY,
                       credential=CredentialRef(id=str(row["credential_id"]), kind="sak",
                                                principal_id=principal.id, scopes=CALLER_SCOPES,
                                                expires_at=row["not_after"]))


__all__ = ["AgentCA", "AgentCAError", "ATTESTATION_MAX_AGE_SECONDS", "CALLER_SCOPES",
           "REPLICA_CERT_HOURS", "agent_dns_name", "published_anchor", "caller_context", "ensure_ca",
           "host_attests_for_serving", "image_refusal", "issue_caller_cert",
           "issue_replica_cert", "renew_after", "replicas_with_valid_cert", "reset_cache",
           "revoke", "revoked_serials", "trust_document"]
