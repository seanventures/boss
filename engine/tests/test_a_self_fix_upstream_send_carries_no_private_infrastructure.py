"""NOTHING PRIVATE LEAVES WITH A SELF-FIX (P0; BOSSAPP-571 R9, spec §7.3, F10).

What a person sends to the BOSS team is two kinds of thing, under two rules:
  * CODE (the diff, the title, the trailers, the commit message) is CHECKED, NEVER MASKED: the
    private-infrastructure denylist (the P0 tree test's own list), this install's names, an
    address outside the documentation ranges, a credential — any hit BLOCKS the send.
  * PROSE (the body, the evidence) is SCRUBBED: names, hosts, addresses, run ids masked.
RFC 5737 / RFC 6598 / loopback fixtures pass: they are what a neutral test is written with.
"""
from __future__ import annotations

import pytest

from boss_engine.services import private_infra
from boss_engine.services import self_fix_upstream as up

from ._self_fix_upstream_support import settings as _settings
from ._self_fix_upstream_support import DIFF, FakeGitHub, client_for, insert_verified
from ._self_fix_support import clean_project
from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_documentation_addresses_and_neutral_names_pass():
    assert up.check_code({"diff": DIFF, "title": "fix(deploy): retire the app"}) == []
    for ok in ("192.0.2.7", "198.51.100.9", "203.0.113.10", "100.64.0.3", "127.0.0.1",
               "0.0.0.0:8000", "::1", "2001:db8::5", "x[::2]", "12:30:45", "version 1.2.3.400"):
        assert up.check_code({"diff": f"+ host = '{ok}'"}) == [], ok


@pytest.mark.parametrize("address", ["8.8.4.4", "10.0.4.12", "172.16.9.9", "192.168.1.20",
                                     "2606:4700:4700::1111", "fd00:1:2::9"])
def test_a_real_address_in_the_diff_blocks_the_send(address):
    found = up.check_code({"diff": f"+ HOST = '{address}'\n"})
    assert found and found[0]["kind"] == "address" and found[0]["where"] == "diff"
    assert address not in found[0]["detail"], "the finding says where, never the address"


def test_a_denylisted_identifier_blocks_it_wherever_it_is(monkeypatch):
    # A neutral word stands in for a real private one (the denylist stores digests only).
    monkeypatch.setattr(private_infra, "WORDS", {private_infra._h("kestrelbox"): "private host"})
    for where in ("diff", "title", "trailers", "message"):
        found = up.check_code({where: "deploy to kestrelbox failed"})
        assert {"where": where, "kind": "private infrastructure", "detail": "private host"} \
            in found, where


def test_this_installs_names_and_known_machines_block_the_diff():
    found = up.check_code({"diff": "+ app = 'acme-payroll'  # on build-box-7\n"},
                          names=[("NAME", "acme-payroll"), ("FILE", "main.py")],
                          known_hosts=["build-box-7"])
    kinds = {f["kind"] for f in found}
    assert "install name (name)" in kinds and "a machine this install knows" in kinds
    # A user's app FILE name is not a leak in BOSS's own diff.
    assert up.check_code({"diff": "+ open('main.py')"}, names=[("FILE", "main.py")]) == []


def test_a_credential_blocks_it():
    found = up.check_code({"diff": "+ token = 'ghp_" + "a1B2" * 9 + "'\n"})
    assert any(f["kind"] == "secret-shaped value" for f in found)


def test_prose_is_scrubbed_not_blocked():
    text, n = up.scrub_prose(
        "deploy of acme-payroll on build-box-7.example.org (203.0.113.10 then 8.8.4.4) in run "
        "wfr_0123456789abcdef failed", [("NAME", "acme-payroll"), ("HOST", "build-box-7")],
        5000)
    assert "acme-payroll" not in text and "8.8.4.4" not in text
    assert "wfr_0123456789abcdef" not in text and "[id]" in text
    assert n >= 3


@pytest.fixture()
def verified(eng, project, monkeypatch):  # noqa: F811
    monkeypatch.delenv("BOSS_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    fake = FakeGitHub(fork="pat-example/boss")
    monkeypatch.setattr(up, "CLIENT", lambda c, actor: client_for(fake))
    monkeypatch.setattr(up, "_names", lambda c, p: [("NAME", "acme-payroll")])
    yield {"eng": eng, "project": project, "fake": fake}
    clean_project(eng, project)


def test_a_private_name_in_the_diff_blocks_the_preview_and_the_send(verified):
    
    eng, project = verified["eng"], verified["project"]
    sfx_id = insert_verified(eng, project, diff=DIFF + "+    app = 'acme-payroll'\n")
    with eng.begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_test")
    assert got["status"] == "blocked" and not got["check"]["ok"]
    assert any(f["kind"].startswith("install name") and f["where"] == "diff"
               for f in got["check"]["findings"])
    assert "acme-payroll" in got["diff"], "the diff is checked, never masked"
    with pytest.raises(up.UpstreamRefused, match="private content"):
        with eng.begin() as c:
            up.send(c, _settings(), sfx_id, got["send_id"], actor="usr_test", checked=True)
    assert not any(m == "POST" and p.endswith("/pulls") for m, p, _ in verified["fake"].calls)


def test_the_body_and_evidence_are_scrubbed_and_the_machine_block_survives(verified):
    
    eng, project = verified["eng"], verified["project"]
    sfx_id = insert_verified(eng, project, symptom="acme-payroll failed on 8.8.4.4",
                             log_tail="connect 8.8.4.4:5432 refused for acme-payroll")
    with eng.begin() as c:
        got = up.preview(c, _settings(), sfx_id, actor="usr_test")
    assert got["status"] == "previewed", got["check"]
    assert "acme-payroll" not in got["evidence"] and "8.8.4.4" not in got["evidence"]
    assert got["scrubbed"]["evidence"] >= 2
    block = up.parse_machine_block(got["body"])
    assert block and block["class_signature"] == \
        "deploy:port_held@stack_deploy.plan_stack_deploy"
    assert "wfr_" not in got["body"] and "wfr_" not in got["evidence"]
    assert all("@" not in v or k == "BOSS-Signature" for k, v in got["trailers"].items()), \
        "no person's email in a trailer"
