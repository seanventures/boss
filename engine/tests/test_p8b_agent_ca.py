"""P8b-9 (BOSSAPP-1076): the BOSS agent CA on Postgres, against a FAKE boss-hostd
(boss-agent-hosts-lld.md §3.17.2, §3.17.3, D7).

  * a replica's key is born in the VM; only its CSR reaches `POST /v1/run-plane/replica/cert`
    (through the host, which names the replica); BOSS issues a 24 h serverAuth certificate from
    the agent CA's SERVING intermediate (separate from the fleet CA), SAN = the agent's DNS name
    (+ the VIP when it has one), renew at two thirds;
  * refused for a replica that is not desired, for a host that does not attest (stale heartbeat,
    Firecracker without KVM, revoked), for an image that is neither signed nor active;
  * renewal keeps the previous certificate valid until it expires (no dropped connections);
    revocation lists the serial, and revoking the host revokes its replicas' certificates;
  * /v1/pki/agent-ca publishes the bundles; a service account's caller certificate maps to it at
    admit, scoped to agent:invoke; a stranger's or a revoked one is refused.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from ._fleet_support import (FakeHost, cleanup_project, deploy_agent, fleet_app, join_token,
                             make_project, org_of, own_pool, use_test_master_key)

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL",
                         "postgresql+psycopg://boss:boss@localhost:5432/boss_test")


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM agent_ca LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable: {error}")
    return engine


class Fleet:
    def __init__(self, eng, monkeypatch):
        from boss_engine.services import agent_ca, fleet_scheduler

        use_test_master_key(monkeypatch)
        agent_ca.reset_cache()
        self.eng = eng
        self.project = make_project(eng)
        self.org = org_of(eng, self.project)
        self.client = TestClient(fleet_app(eng))
        self.pool = own_pool()
        self.dep = deploy_agent(eng, self.project, replicas=1, pool=self.pool)
        self.host = FakeHost(self.client)
        self.host.enrol(join_token(eng, self.org, pool=self.pool))
        self.hosts = [self.host]
        fleet_scheduler.reconcile(eng)
        with eng.begin() as c:
            self.replica_id = str(c.execute(text(
                "SELECT id FROM agent_replicas WHERE deployment_id = :d AND desired_state = "
                "'running'"), {"d": self.dep["id"]}).scalar())
        self.host.serve_all()
        assert self.host.heartbeat().status_code == 200

    def cert(self, key=None, *, replica: str | None = None, host: FakeHost | None = None):
        from cryptography.hazmat.primitives.asymmetric import ec

        from boss_engine.services.fleet_ca import make_csr

        key = key or ec.generate_private_key(ec.SECP256R1())
        host = host or self.host
        return key, host.call("POST", "/v1/run-plane/replica/cert",
                              json={"csr_pem": make_csr(key, "boss-runtime")},
                              headers={"X-Boss-Replica": replica or self.replica_id})

    def close(self):
        cleanup_project(self.eng, self.project, [h.host_id for h in self.hosts if h.host_id])


@pytest.fixture()
def fleet(eng, monkeypatch):
    f = Fleet(eng, monkeypatch)
    yield f
    f.close()


def _load(pem: str):
    from cryptography import x509

    return x509.load_pem_x509_certificate(pem.encode("ascii"))


def test_a_replica_gets_a_24h_serving_cert_for_a_key_born_in_the_vm(fleet):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import ExtendedKeyUsageOID

    from boss_engine.services import agent_ca

    key, response = fleet.cert()
    assert response.status_code == 200, response.text
    out = response.json()
    cert = _load(out["certificate_pem"])
    # the certificate certifies the VM's own key: the key never left
    assert cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == \
        key.public_key().public_bytes(serialization.Encoding.DER,
                                      serialization.PublicFormat.SubjectPublicKeyInfo)
    life = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert dt.timedelta(hours=24) <= life <= dt.timedelta(hours=24, minutes=6)
    renew = dt.datetime.fromisoformat(out["renew_after"])
    assert abs((renew - cert.not_valid_before_utc) / life - 2 / 3) < 0.01, "renew at two thirds"
    dns = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    name = dns.get_values_for_type(x509.DNSName)[0]
    assert name == agent_ca.agent_dns_name({"agent_key": fleet.dep["agent_key"],
                                            "project_id": fleet.project})
    assert ".agents." in name and out["dns_names"] == [name]
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    # issued by the agent CA's serving intermediate — NOT the fleet CA
    with fleet.eng.begin() as c:
        ca = agent_ca.ensure_ca(c)
        fleet_ca_pem = c.execute(text("SELECT cert_pem FROM fleet_ca LIMIT 1")).scalar()
    assert cert.issuer == ca.serving.cert.subject
    assert cert.issuer != _load(fleet_ca_pem).subject
    assert ca.serving.pem in out["chain_pem"] and ca.root.pem in out["chain_pem"]
    with fleet.eng.begin() as c:
        row = c.execute(text("SELECT kind, replica_id, deployment_id, host_id FROM agent_certs "
                             "WHERE serial = :s"), {"s": out["serial"]}).mappings().one()
    assert dict(row) == {"kind": "replica", "replica_id": fleet.replica_id,
                         "deployment_id": fleet.dep["id"], "host_id": fleet.host.host_id}


def test_the_vip_is_a_san_when_the_deployment_has_one(fleet):
    import ipaddress

    from cryptography import x509

    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_deployments SET vip = '198.51.100.77' WHERE id = :d"),
                  {"d": fleet.dep["id"]})
    _, response = fleet.cert()
    cert = _load(response.json()["certificate_pem"])
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert ipaddress.ip_address("198.51.100.77") in sans.get_values_for_type(x509.IPAddress)


def test_refused_for_a_replica_that_is_not_desired_or_not_this_hosts(fleet):
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_replicas SET desired_state = 'stopped' WHERE id = :r"),
                  {"r": fleet.replica_id})
    _, stopped = fleet.cert()
    assert stopped.status_code == 409 and stopped.json()["detail"]["error"] == "replica_not_serving"
    _, stranger = fleet.cert(replica=f"rep_{uuid.uuid4().hex[:12]}")
    assert stranger.status_code == 403
    # a guest cannot speak for another replica: the body may not contradict the host's header
    from boss_engine.services.fleet_ca import make_csr
    from cryptography.hazmat.primitives.asymmetric import ec

    lie = fleet.host.call("POST", "/v1/run-plane/replica/cert",
                          json={"csr_pem": make_csr(ec.generate_private_key(ec.SECP256R1())),
                                "replica_id": "rep_other"},
                          headers={"X-Boss-Replica": fleet.replica_id})
    assert lie.status_code == 403 and lie.json()["detail"]["error"] == "replica_mismatch"
    with fleet.eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM agent_certs WHERE replica_id = :r"),
                         {"r": fleet.replica_id}).scalar() == 0


def test_refused_for_a_host_that_does_not_attest(fleet):
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET last_heartbeat_at = now() - interval '11 seconds' "
                       "WHERE id = :h"), {"h": fleet.host.host_id})
    _, stale = fleet.cert()
    assert stale.status_code == 403 and stale.json()["detail"]["error"] == "host_heartbeat_stale"
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET last_heartbeat_at = now(), backend = 'firecracker',"
                       " kvm_live = false, firecracker_live = false WHERE id = :h"),
                  {"h": fleet.host.host_id})
    _, no_kvm = fleet.cert()
    assert no_kvm.status_code == 403 and no_kvm.json()["detail"]["error"] == "host_not_attested"
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_hosts SET kvm_live = true, firecracker_live = true "
                       "WHERE id = :h"), {"h": fleet.host.host_id})
    _, unpinned = fleet.cert()
    assert unpinned.status_code == 403 and unpinned.json()["detail"]["error"] == "image_unpinned"


def test_the_image_must_be_signed_or_active_and_never_revoked(fleet):
    digest = f"sha256:{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_replicas SET image_digest = :d WHERE id = :r"),
                  {"d": digest, "r": fleet.replica_id})
    try:
        _, unknown = fleet.cert()
        assert unknown.status_code == 403 and unknown.json()["detail"]["error"] == "image_unknown"
        with fleet.eng.begin() as c:
            c.execute(text("INSERT INTO vm_images (digest, status) VALUES (:d, 'candidate')"),
                      {"d": digest})
        _, unsigned = fleet.cert()
        assert unsigned.json()["detail"]["error"] == "image_unsigned"
        with fleet.eng.begin() as c:   # `active` alone is not enough: issuance needs SIGNED
            c.execute(text("UPDATE vm_images SET status = 'active' WHERE digest = :d"),
                      {"d": digest})
        _, active_unsigned = fleet.cert()
        assert active_unsigned.json()["detail"]["error"] == "image_unsigned"
        with fleet.eng.begin() as c:
            c.execute(text("UPDATE vm_images SET signature = 'cosign:sig' WHERE digest = :d"),
                      {"d": digest})
        _, signed = fleet.cert()
        assert signed.status_code == 200, signed.text
        with fleet.eng.begin() as c:
            c.execute(text("UPDATE vm_images SET status = 'revoked' WHERE digest = :d"),
                      {"d": digest})
        _, revoked = fleet.cert()
        assert revoked.json()["detail"]["error"] == "image_revoked"
    finally:
        with fleet.eng.begin() as c:
            c.execute(text("DELETE FROM vm_images WHERE digest = :d"), {"d": digest})


def test_renewal_keeps_the_old_cert_valid_and_revocation_is_listed(fleet):
    from boss_engine.services import agent_ca

    _, first = fleet.cert()
    _, second = fleet.cert()
    a, b = first.json(), second.json()
    assert b["renews"] == a["serial"] and a["serial"] != b["serial"]
    with fleet.eng.begin() as c:
        live = c.execute(text("SELECT serial FROM agent_certs WHERE replica_id = :r AND "
                              "revoked_at IS NULL AND not_after > now()"),
                         {"r": fleet.replica_id}).scalars().all()
        assert set(live) == {a["serial"], b["serial"]}, "the old one serves until it expires"
        assert agent_ca.replicas_with_valid_cert(c, [fleet.replica_id]) == {fleet.replica_id}
        assert agent_ca.revoke(c, serial=a["serial"], reason="test") == [a["serial"]]
    with fleet.eng.begin() as c:
        mine = agent_ca.trust_document(c, fleet.org)["revoked"]
    assert a["serial"] in mine and b["serial"] not in mine, "revocation is listed per org"
    doc = fleet.client.get("/v1/pki/agent-ca").json()
    assert "revoked" not in doc, "the public bundle names no org's serials"
    assert "BEGIN CERTIFICATE" in doc["serving_bundle_pem"] and doc["root_sha256"]
    assert doc["options"]["acme"].startswith("documented")
    # revoking the HOST revokes every certificate it holds, replicas' included; no renewal after
    from boss_engine.services import fleet as fleet_service

    with fleet.eng.begin() as c:
        fleet_service.revoke(c, org_id=fleet.org, host_id=fleet.host.host_id, actor="test")
        assert agent_ca.replicas_with_valid_cert(c, [fleet.replica_id]) == set()
    _, after = fleet.cert()
    assert after.status_code == 403 and after.json()["detail"]["error"] == "host_revoked"


def test_a_service_accounts_caller_cert_is_scoped_to_invoke_and_checked_at_admit(fleet):
    from cryptography.hazmat.primitives.asymmetric import ec

    from boss_engine.identity.principal import PrincipalKind
    from boss_engine.services import agent_ca
    from boss_engine.services.fleet_ca import make_csr

    sva = f"sva_{uuid.uuid4().hex[:12]}"
    with fleet.eng.begin() as c:
        c.execute(text("INSERT INTO identity_principals (id, kind, org_id, status, display_name) "
                       "VALUES (:i, 'service_account', :o, 'active', 'portal')"),
                  {"i": sva, "o": fleet.org})
        issued = agent_ca.issue_caller_cert(c, org_id=fleet.org, principal_id=sva,
                                            csr_pem=make_csr(ec.generate_private_key(
                                                ec.SECP256R1()), "portal"), days=2)
        ctx = agent_ca.caller_context(c, issued["certificate_pem"])
        assert ctx.principal.id == sva and ctx.principal.kind == PrincipalKind.SERVICE_ACCOUNT
        assert ctx.credential.scopes == frozenset({"agent:invoke"})
        with pytest.raises(agent_ca.AgentCAError):     # a person never gets one
            agent_ca.issue_caller_cert(c, org_id="org_other", principal_id=sva,
                                       csr_pem=make_csr(ec.generate_private_key(ec.SECP256R1())))
        # a self-signed lookalike is not ours
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID

        k = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sva)])
        now = dt.datetime.now(dt.timezone.utc)
        fake = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(k.public_key()).serial_number(1).not_valid_before(now)
                .not_valid_after(now + dt.timedelta(days=1)).sign(k, hashes.SHA256()))
        with pytest.raises(agent_ca.AgentCAError) as stranger:
            agent_ca.caller_context(c, fake.public_bytes(serialization.Encoding.PEM).decode())
        assert stranger.value.code == "caller_cert_not_ours"
        agent_ca.revoke(c, serial=issued["serial"], reason="test")
        with pytest.raises(agent_ca.AgentCAError) as revoked:
            agent_ca.caller_context(c, issued["certificate_pem"])
        assert revoked.value.code == "caller_cert_revoked"
        assert c.execute(text("SELECT scopes, revoked_at IS NOT NULL FROM "
                              "identity_api_credentials WHERE id = :k"),
                         {"k": issued["credential_id"]}).one() == (["agent:invoke"], True), \
            "the certificate is one of the account's credentials, revoked with it"
        c.execute(text("DELETE FROM agent_certs WHERE principal_id = :i"), {"i": sva})
        c.execute(text("DELETE FROM identity_api_credentials WHERE principal_id = :i"),
                  {"i": sva})
        c.execute(text("DELETE FROM identity_principals WHERE id = :i"), {"i": sva})


def test_the_agent_ca_is_separate_from_the_fleet_ca_and_stable(fleet):
    from boss_engine.services import agent_ca, fleet_ca

    with fleet.eng.begin() as c:
        one = agent_ca.ensure_ca(c)
        agent_ca.reset_cache()
        two = agent_ca.ensure_ca(c)
        fleet_root = fleet_ca.ensure_ca(c)
    assert one.root.pem == two.root.pem and one.serving.pem == two.serving.pem
    assert one.root.cert.subject != fleet_root.cert.subject
    assert one.serving.cert.issuer == one.root.cert.subject
    assert one.caller.cert.issuer == one.root.cert.subject
    assert one.serving.cert.subject != one.caller.cert.subject


def test_the_public_bundle_never_creates_the_ca(eng, monkeypatch):
    from boss_engine.services import agent_ca

    calls: list = []
    monkeypatch.setattr(agent_ca, "ensure_ca", lambda c: calls.append(1))
    with eng.begin() as c:
        agent_ca.public_bundle(c)
        agent_ca.published_anchor(c)
    assert calls == []


def test_names_are_confined_unique_and_never_collide(fleet):
    from cryptography import x509

    from boss_engine.services import agent_ca

    assert agent_ca._label("a_b") != agent_ca._label("a-b")
    assert agent_ca._label("Bot") != agent_ca._label("bot") and agent_ca._label("bot") == "bot"
    assert len(agent_ca._label("x" * 80)) <= 63
    dom = agent_ca.agent_domain()
    assert agent_ca.agent_dns_name({"hostname": "evil.example.com", "agent_key": "k",
                                    "project_id": "p"}).endswith("." + dom)
    assert agent_ca.agent_dns_name({"hostname": f"pay.{dom}"}) == f"pay.{dom}"
    with fleet.eng.begin() as c:
        ca = agent_ca.ensure_ca(c)
    for inter, kind in ((ca.serving, "serving"), (ca.caller, "caller")):
        nc = inter.cert.extensions.get_extension_for_class(x509.NameConstraints)
        assert nc.critical and list(nc.value.permitted_subtrees) == agent_ca._permitted(kind)
    # uniqueness: a name another deployment holds is refused
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_deployments SET hostname = :h WHERE id = :d"),
                  {"h": f"pay.{dom}", "d": fleet.dep["id"]})
    _, first = fleet.cert()
    assert first.status_code == 200, first.text
    with fleet.eng.begin() as c:
        c.execute(text("UPDATE agent_certs SET deployment_id = 'dep_someone_else' "
                       "WHERE serial = :s"), {"s": first.json()["serial"]})
    _, clash = fleet.cert()
    assert clash.status_code == 409 and clash.json()["detail"]["error"] == "dns_name_taken"
