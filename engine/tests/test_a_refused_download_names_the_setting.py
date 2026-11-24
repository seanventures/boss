"""A download the company network refused names the host and the ONE setting that fixes it
(BOSSAPP-1435: company CA; BOSSAPP-1436: BOSS's fence and the company proxy).

Builds classified a TLS-inspecting gateway as "the package registry's certificate is not
trusted" and a refused tunnel as "refused access to the registry itself". Neither card said
which host, which setting, or who acts, and a template build even told the person to "resolve
the dependency conflict in package.json". Now every surface reads `network_refusal.explain`:
what happened (with the host), who acts (an operator, or IT for the company proxy) and what they
do (the .env setting, then `boss upgrade`). It is the person's to do: no repair round, no retry,
changes a setting in .env.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.egress.boss_egress import connect  # noqa: E402
from boss_engine.services import failure_guidance, failure_owner as fo, network_refusal as nr  # noqa: E402
from boss_engine.services.suite_environment import classify_install  # noqa: E402
from boss_engine.templates.gate import hold_outcome, install_reason, INSTALL_NEXT  # noqa: E402

NPM_TLS = ("npm error code SELF_SIGNED_CERT_IN_CHAIN\n"
           "npm error errno SELF_SIGNED_CERT_IN_CHAIN\n"
           "npm error request to https://registry.npmjs.org/express failed, reason: self-signed "
           "certificate in certificate chain\n")
PIP_TLS = ("WARNING: Retrying (Retry(total=4)) after connection broken by 'SSLError(SSLCertVerificationError(1, "
           "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
           "certificate (_ssl.c:1006)'))': /simple/requests/\n"
           "ERROR: Could not fetch URL https://pypi.org/simple/requests/: There was a problem confirming "
           "the ssl certificate: HTTPSConnectionPool(host='pypi.org', port=443)\n")
GO_TLS = ('go: example.com/m@v1.2.0: Get "https://proxy.golang.org/example.com/m/@v/v1.2.0.mod": '
          "tls: failed to verify certificate: x509: certificate signed by unknown authority\n")


def _words_are_usable(what: str, action: str) -> None:
    """The failure-words contract (what happened; who acts; what to do, and where)."""
    assert what and not what.lower().startswith(("failed", "error"))
    assert "operator" in action or "Ask IT" in action
    assert "`boss upgrade`" in action or "boss upgrade" in action


@pytest.mark.parametrize("output, host", [(NPM_TLS, "registry.npmjs.org"), (PIP_TLS, "pypi.org"),
                                          (GO_TLS, "proxy.golang.org")])
def test_an_untrusted_gateway_names_the_host_and_the_ca_setting(output, host):
    none = nr.explain(output, ca=False)
    assert none["kind"] == "company_ca" and none["host"] == host
    assert host in none["what"] and "BOSS has no company CA" in none["what"]
    assert "BOSS_EXTRA_CA_FILE" in none["action"]
    _words_are_usable(none["what"], none["action"])
    have = nr.explain(output, ca=True)
    assert "does not cover" in have["what"] and "ROOT CA" in have["action"]


@pytest.mark.parametrize("output", [
    "npm error request to https://x.example.com/a failed, reason: certificate has expired",
    "SSLCertVerificationError: hostname mismatch, certificate is not valid for 'x.example.com'",
    "npm error code ERESOLVE\nnpm error ERESOLVE unable to resolve dependency tree",
])
def test_what_a_ca_does_not_fix_is_not_called_a_ca_problem(output):
    assert nr.explain(output, ca=False) is None


@pytest.mark.parametrize("host, port, reason, setting", [
    ("evil.example.com", 443, "host_not_allowed", "BOSS_EGRESS_CONNECT_ALLOW"),
    ("pkg.example.com", 8443, "port_not_allowed", "BOSS_EGRESS_CONNECT_ALLOW"),
    ("nexus.corp.example", 443, "not_a_public_address", "BOSS_EGRESS_PRIVATE_ALLOW"),
    ("gone.example.com", 443, "unresolvable", "BOSS_UPSTREAM_PROXY"),
])
def test_the_fences_own_refusal_is_read_back_with_its_host_and_setting(host, port, reason, setting):
    phrase = connect.refusal_reason(host, port, reason)
    npm = f"npm error 403 403 Forbidden - GET https://{host}/x - {phrase}\n"
    got = nr.explain(npm)
    assert got["kind"] == "egress_fence" and got["setting"] == setting
    where = host if port == 443 else f"{host}:{port}"
    assert got["host"] == where and where in got["what"] + got["action"]
    assert setting in got["action"]
    _words_are_usable(got["what"], got["action"])


def test_githubs_own_route_is_not_a_setting_to_widen():
    assert nr.explain(connect.refusal_reason("github.com", 443, "host_not_allowed")) is None


def test_the_company_proxys_refusal_says_who_allows_it():
    phrase = connect.upstream_refusal("pkg.example.com", 443, "HTTP/1.1 403 Forbidden")
    got = nr.explain(f"pip: ProxyError('Tunnel connection failed: 502 {phrase}')")
    assert got["kind"] == "company_proxy" and got["host"] == "pkg.example.com"
    assert "(HTTP/1.1 403 Forbidden)" in got["what"] and got["action"].startswith("Ask IT")
    assert "BOSS_UPSTREAM_NO_PROXY" in got["action"]
    # A proxy that wants a login is the registry credential's card, not this one.
    assert nr.explain(connect.upstream_refusal("pkg.example.com", 443, "HTTP/1.1 407 Proxy "
                                                "Authentication Required")) is None


@pytest.mark.parametrize("error, code", [
    # The words a blocked build carries; "502" and "QualityGate"/"npm ERR" used to send these to
    # BOSS's own side or to an app repair.
    (f"Blocked: dependencies did not install: {NPM_TLS} QualityGate FAIL (qg_1)", "company_ca"),
    (f"npm ERR! {connect.refusal_reason('evil.example.com', 443, 'host_not_allowed')}", "egress_fence"),
    (f"HTTP/1.1 502 {connect.upstream_refusal('pkg.example.com', 443, 'HTTP/1.1 403 Forbidden')}",
     "company_proxy"),
])
def test_the_failure_card_is_the_persons_and_says_exactly_what_to_do(error, code):
    cls, cause, action = fo.classify(error, failure_class="infra")
    assert (cls, cause) == (fo.PERSON, code)
    assert action.endswith("Then press Done — retry.")
    g = failure_guidance.build(cause_code=cause, cls=cls, title="Build", cause=error[:200],
                               action=action, reason=fo._WHY_PERSON[code])
    assert failure_guidance.problems(g) == []
    assert any("boss upgrade" in step for step in g["steps"])


def _install_failure(output: str) -> dict:
    return {"command": "npm ci", "exit_code": 1, "summary": output.strip().splitlines()[-1],
            "environment": classify_install(output)}


def test_a_blocked_build_says_the_setting_and_a_template_hold_never_says_package_json(monkeypatch):
    monkeypatch.setattr(nr, "ca_configured", lambda path=nr.CA_PATH: False)
    failure = _install_failure(NPM_TLS)
    reason = install_reason(failure)
    assert "registry.npmjs.org" in reason and "BOSS_EXTRA_CA_FILE" in reason
    assert reason.endswith("Then retry the build") and ".." not in reason
    held = hold_outcome(["template_tests"], failure)
    assert "BOSS_EXTRA_CA_FILE" in held["next_step"] and held["next_step"] != INSTALL_NEXT
    assert "package.json" not in held["error"]
    assert held["error"].count("BOSS_EXTRA_CA_FILE") == 1          # said once, not twice
    # An ordinary dependency conflict keeps its own words and next step.
    plain = _install_failure("npm error code ERESOLVE\nnpm error ERESOLVE could not resolve")
    assert hold_outcome(["template_tests"], plain)["next_step"] == INSTALL_NEXT


@pytest.mark.parametrize("error", [
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired",
    "403 " + connect.refusal_reason("github.com", 443, "host_not_allowed"),
])
def test_what_the_module_rules_out_never_reaches_a_network_card(error):
    """Review P1: the fallback rules reused the bare patterns, so an expired certificate became
    "set BOSS_EXTRA_CA_FILE" and GitHub's own route became "widen the fence"."""
    _cls, cause, _action = fo.classify(error)
    assert cause not in nr.CODES


def test_a_huge_log_is_read_in_bounded_time():
    import time

    log = ("a." * 20000) + "\ncertificate verify failed\n" + ("b." * 20000)
    t0 = time.monotonic()
    nr.explain(log, ca=False)
    assert time.monotonic() - t0 < 1.0
