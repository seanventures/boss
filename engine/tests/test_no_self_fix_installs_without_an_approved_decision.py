"""P0: nothing installs a BOSS self-fix but a person's approved decision (BOSSAPP-571 R5, R6,
spec §6.5, §8.8, operator decision 1).

The REAL host self-installer (scripts/install/self_fix_installer.py, run as a subprocess) against
a real git checkout, a fake Docker, and a fake API whose every route is the REAL engine function
on the test database. Forged, expired, replayed, wrong-commit, wrong-tree and unapproved tickets
are all refused, and a refused ticket changes nothing: no image, no ref, no restart.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from sqlalchemy import text

from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_build
from boss_engine.services import self_fix_install as sfi

from ._self_fix_support import FakeHost, LocalSigner, approve, harness, self_fix_on  # noqa: F401
from .test_workflow_capability_runner import eng, project  # noqa: F401


@pytest.fixture()
def card(harness):
    """A good self-fix, built by the scripted engine, waiting on its card."""
    harness.script("good_fix")
    sfx = harness.propose()["self_fix"]
    out = self_fix_build.Build(engine=harness.eng, settings=harness.settings, sfx_id=sfx["id"],
                               source=harness.mirror).run()
    assert out["status"] == sf.AWAITING_APPROVAL, out
    return sfx["id"]


@pytest.fixture()
def host(harness):
    h = FakeHost(harness)
    yield h
    h.close()


def _calls(host, verb):
    return [c for c in host.docker_state()["calls"] if verb in c]


def _nothing_changed(host):
    assert host.stack_ref() == "", "no stack ref"
    assert not _calls(host, "build") and not _calls(host, "up"), "nothing built or restarted"
    assert host.running_head() == host.release


def test_an_approved_fix_installs_and_nothing_else_moves(harness, card, host):
    row = approve(harness, card)
    assert row["status"] == sf.APPROVED
    done = host.installer("watch", "--once")
    assert "installed" in done.stdout, done.stdout + done.stderr
    with harness.eng.begin() as c:
        row = sf.get(c, card)
        ticket = c.execute(text("SELECT status, phases FROM self_fix_tickets WHERE "
                                "self_fix_id = :i AND kind = 'install'"), {"i": card}
                           ).mappings().first()
    assert row["status"] == sf.INSTALLED and row["installed_stack_head"] == row["fix_commit_sha"]
    assert ticket["status"] == "done"
    assert [p["phase"] for p in ticket["phases"]] == ["backup", "apply", "build", "start",
                                                     "health", "done"]
    assert host.stack_ref() == row["fix_commit_sha"]
    assert host.running_head() == row["fix_commit_sha"]
    from ._self_fix_support import git

    assert git(host.dir, "rev-parse", "HEAD") == host.release, "the checkout stays on the release"
    assert "boss-community-api:prev" in host.docker_state()["images"], "the old images are kept"
    release = (host.dir / ".boss-release").read_text()
    assert f"BOSS_STACK_HEAD={row['fix_commit_sha']}" in release
    assert "BOSS_STACK_REF=refs/boss/stack/v0.0.0-test" in release


def test_no_ticket_exists_before_a_person_approves(harness, card, host):
    done = host.installer("watch", "--once")
    assert done.returncode == 0
    _nothing_changed(host)
    with harness.eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM self_fix_tickets WHERE self_fix_id = :i"),
                         {"i": card}).scalar() == 0


def test_no_policy_agent_or_system_account_approves_it(harness, card):
    from boss_engine.services.decisions import (DecisionService, InvalidDecisionTransitionError,
                                                approve_by_policy)

    with harness.eng.begin() as c:
        decision_id = sf.get(c, card)["decision_id"]
        assert approve_by_policy(c, decision_id, project_id=harness.project,
                                 actor="auto_approval", note="policy") is False
        for actor, policy in (("auto_approval", True), ("sys_worker", False),
                              ("boss_maintainer", False), ("agt_mender", False)):
            with pytest.raises(InvalidDecisionTransitionError, match="A person approves"):
                DecisionService(harness.settings).transition(
                    c, decision_id, action="approve", actor=actor, note=None,
                    system_policy=policy)
        assert sf.get(c, card)["status"] == sf.AWAITING_APPROVAL


def _forge(harness, card, signer, **override) -> None:
    """Replace the issued ticket with one whose claims or signature are not BOSS's own."""
    with harness.eng.begin() as c:
        row = c.execute(text("SELECT id, body FROM self_fix_tickets WHERE self_fix_id = :i AND "
                             "status = 'issued'"), {"i": card}).mappings().first()
        claims = {**row["body"], **override}
        token = signer.sign(c, claims)
        c.execute(text("UPDATE self_fix_tickets SET token = :t, body = CAST(:b AS JSONB) "
                       "WHERE id = :i"), {"t": token, "b": json.dumps(claims), "i": row["id"]})


@pytest.mark.parametrize("label", ["forged", "expired", "wrong_commit", "wrong_tree"])
def test_a_ticket_that_is_not_exactly_the_approved_fix_is_refused(harness, card, host, label):
    approve(harness, card)
    with harness.eng.begin() as c:
        body = c.execute(text("SELECT body FROM self_fix_tickets WHERE self_fix_id = :i"),
                         {"i": card}).scalar()
    if label == "forged":
        _forge(harness, card, LocalSigner())                     # a key BOSS does not publish
    elif label == "expired":
        _forge(harness, card, harness.signer, exp=int(time.time()) - 5)
    elif label == "wrong_commit":
        _forge(harness, card, harness.signer, fix_commit_sha="f" * 40)
    else:
        _forge(harness, card, harness.signer, fix_tree_sha="e" * 40)
    done = host.installer("watch", "--once")
    assert "refused" in done.stdout.lower(), done.stdout + done.stderr
    _nothing_changed(host)
    with harness.eng.begin() as c:
        assert sf.get(c, card)["status"] in (sf.APPROVED, sf.INSTALL_FAILED)
    assert body["fix_commit_sha"]


def test_a_ticket_is_used_once(harness, card, host):
    approve(harness, card)
    with harness.eng.begin() as c:
        ticket_id = c.execute(text("SELECT id FROM self_fix_tickets WHERE self_fix_id = :i"),
                              {"i": card}).scalar()
        sfi.take(c, ticket_id)
    with harness.eng.begin() as c:
        with pytest.raises(sfi.InstallRefused, match="used once"):
            sfi.take(c, ticket_id)


def test_a_ticket_whose_decision_is_no_longer_approved_is_refused(harness, card, host):
    approve(harness, card)
    with harness.eng.begin() as c:
        c.execute(text("UPDATE decisions SET status = 'rejected' WHERE id = :i"),
                  {"i": sf.get(c, card)["decision_id"]})
    done = host.installer("watch", "--once")
    assert "not approved" in done.stdout, done.stdout
    _nothing_changed(host)


def test_only_the_self_installers_key_reads_tickets(harness, card, host):
    approve(harness, card)
    import urllib.error
    import urllib.request

    request = urllib.request.Request(host.url + "/v1/self-fixes/tickets",
                                     headers={"Authorization": "Bearer boss_sak_someone_else"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request)  # noqa: S310
    assert refused.value.code == 403


def test_the_installer_verifies_ed25519_without_dependencies():
    """The host has python3 and nothing else: its Ed25519 must agree with `cryptography`."""
    import importlib.util
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = Path(__file__).resolve().parents[2] / "scripts" / "install" / "self_fix_installer.py"
    spec = importlib.util.spec_from_file_location("sfi_host", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    public = key.public_key().public_bytes(serialization.Encoding.Raw,
                                           serialization.PublicFormat.Raw)
    for message in (b"", b"a ticket", bytes(range(200))):
        signature = key.sign(message)
        assert module.ed25519_verify(public, message, signature)
        assert not module.ed25519_verify(public, message + b"x", signature)
        bad = bytearray(signature)
        bad[0] ^= 1
        assert not module.ed25519_verify(public, message, bytes(bad))
    assert base64  # the ticket encoding is base64url, exercised by the installs above


def test_a_build_that_fails_restarts_nothing(harness, card, host):
    approve(harness, card)
    done = host.installer("watch", "--once", env={"FAKE_DOCKER_BUILD_FAILS": "1"})
    assert "stopped at build" in done.stdout, done.stdout
    with harness.eng.begin() as c:
        assert sf.get(c, card)["status"] == sf.INSTALL_FAILED
    assert host.stack_ref() == "" and not _calls(host, "up")
    assert host.running_head() == host.release
