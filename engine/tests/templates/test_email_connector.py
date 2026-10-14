"""The transactional-email connector (BOSSAPP-549): a fourth built-in connector beside login and
payments, under the same contract.

* It is a connector, pre-tested in its own tier (the Node suite with a fake SMTP server and a fake
  Resend API; `BOSS_TEMPLATE_TRIALS=1` runs it), never in a build.
* Test mode needs no pasted secret: both provider secrets are optional stubs, and with neither set
  the app keeps every message in its local mail catcher, so builds and previews work.
* It declares the hosts it talks to (Resend's API and SMTP relay) and reads APP_BASE_URL (reset
  and invite links), so BOSS fills it.
* A build that adds it is gated by the wiring smoke check: its public GET routes answer.
"""
from __future__ import annotations

import pytest

from boss_engine.templates import gate
from boss_engine.templates.base_url import declares_base_url
from boss_engine.templates.secrets import shape_of
from boss_engine.templates.validator import tree_from_dir, validate_tree

from ._fixtures import REPO_ROOT

EMAIL = REPO_ROOT / "templates" / "apps" / "transactional-email"


def _load():
    tree = tree_from_dir(EMAIL)
    report = validate_tree(tree, source="builtin")
    assert report.ok, report.reasons
    return report, tree.files()


def test_it_is_a_node_connector_with_its_own_suite():
    report, files = _load()
    m = report.manifest
    assert m.item_kind == "connector"
    assert set(m.stacks) == {"node"} and m.stacks["node"].frameworks == ("express",)
    assert m.tests["node"].command == "npm run test:transactional-email"
    assert m.tests["node"].min_tests >= 12
    assert any(p.startswith("stacks/node/tests/template_email/") for p in files)
    for name in ("brief.md", "guide.md", "SECURITY.md", "LICENSE", "stacks/node/schema.sql",
                 "stacks/node/ui/EmailForms.jsx"):
        assert name in files, name


def test_test_mode_needs_no_pasted_secret_and_the_catcher_stands_in():
    report, files = _load()
    m = report.manifest
    secrets = {s.name: s for s in m.secrets}
    assert set(secrets) == {"RESEND_API_KEY", "SMTP_PASSWORD"}
    assert all(s.test_default == "stub" and not s.required for s in secrets.values())
    assert "SMTP_PASSWORD" not in {e.name for e in m.env}
    config = files["stacks/node/email/config.mjs"].decode()
    assert '"catcher"' in config and "production" in config


def test_a_resend_value_is_checked_by_shape_never_by_value():
    report, _ = _load()
    spec = report.manifest.secret("RESEND_API_KEY")
    assert shape_of("re_" + "a1B2" * 6, spec) == "matches_test_pattern"
    assert shape_of("not-a-resend-key", spec) == "unknown"


def test_it_declares_the_hosts_it_talks_to_and_its_base_url():
    report, files = _load()
    m = report.manifest
    assert m.egress == ("api.resend.com", "smtp.resend.com")
    assert declares_base_url(m.model_dump(mode="json", by_alias=True))
    # The Resend host is fixed in code (never configuration), and it is a declared egress host.
    assert 'RESEND_API = "https://api.resend.com"' in files["stacks/node/email/config.mjs"].decode()


def test_the_wiring_smoke_check_probes_its_public_routes():
    report, _ = _load()
    paths = gate.probe_paths(report.manifest)
    assert paths == ["/api/email/status", "/dev/mail", "/dev/mail/messages"]
    assert not any(gate.route_needs_auth(p) for p in paths)


@pytest.mark.parametrize("probes,ok", [
    ({"/api/email/status": 200, "/dev/mail": 200, "/dev/mail/messages": 200}, True),
    # A public route that does not answer is broken wiring.
    ({"/api/email/status": 404, "/dev/mail": 200, "/dev/mail/messages": 200}, False),
    ({"/api/email/status": 200, "/dev/mail": 401, "/dev/mail/messages": 200}, False),
])
def test_a_build_that_adds_it_is_judged_on_its_wiring(probes, ok):
    report, files = _load()
    tpl = {"id": "tpl_email", "stack": "node",
           "manifest": report.manifest.model_dump(mode="json", by_alias=True)}

    def never_run(command, cwd):
        raise AssertionError(f"a build ran the connector's own suite: {command}")

    r = gate.evaluate(template=tpl, files=files, changed={"app/server.mjs": "x"}, cwd=None,
                      run_command=never_run, serve={"ok": True, "proven": True, "route_probes": probes})
    assert r.gates["template_tests"] is ok, r.evidence["template_tests"]
    assert r.evidence["template_tests"]["kind"] == "connector"
