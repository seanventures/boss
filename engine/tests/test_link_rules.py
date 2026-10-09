"""The link gateway's decision table (APP_TEMPLATES_AND_SECURITY_LOOP.md §5.9, unit), row by row."""
from __future__ import annotations

import pytest

from boss_engine.identity import link_rules as lr

POLICY = lr.UpstreamPolicy("api", lr.PortRange(19000, 19999), lr.PortRange(18100, 18199))
LINK = {"id": "lnk_a", "project_id": "p1", "status": "active", "env": "preview", "gateway_port": 18100}
PASS = {"link_id": "lnk_a", "project_id": "p1", "consumer_kind": "run", "consumer_id": "r1"}
HEADERS = {lr.PASS_HEADER: "boss_lnk_x", "authorization": "Bearer the-providers-own"}


def _decide(**over):
    kw = dict(method="GET", target="/accounts/2", headers=HEADERS, port=18100, port_link=LINK,
              link_pass=PASS, consumer_live=True, upstream=("api", 19001), policy=POLICY)
    kw.update(over)
    return lr.decide(**kw)


def test_a_valid_link_forwards_the_canonical_path():
    d = _decide(target="/accounts/2?x=1")
    assert (d.allowed, d.status, d.path) == (True, 200, "/accounts/2")


@pytest.mark.parametrize("over, status, reason", [
    ({"target": "http://api:8000/v1/projects"}, 400, "absolute_form_refused"),
    ({"method": "CONNECT", "target": "db:5432"}, 400, "absolute_form_refused"),
    ({"headers": {"authorization": "Bearer x"}}, 401, "no_link_pass"),
    ({"link_pass": None}, 401, "link_pass_dead"),
    ({"port_link": None}, 403, "no_link_on_port"),
    ({"link_pass": {**PASS, "link_id": "lnk_other"}}, 403, "wrong_link"),
    ({"port_link": {**LINK, "status": "revoked"}}, 403, "link_revoked"),
    ({"port_link": {**LINK, "env": "deploy"}}, 403, "not_a_preview_link"),
    ({"link_pass": {**PASS, "project_id": "p2"}}, 403, "cross_project"),
    ({"port": 18101}, 403, "wrong_port"),
    ({"consumer_live": False}, 401, "consumer_not_live"),
    ({"target": "/a/../v1/admin"}, 400, "dot_or_empty_segment"),
    ({"target": "/a%2f..%2fv1"}, 400, "encoded_path_separator"),
    ({"headers": {**HEADERS, "x-http-method-override": "DELETE"}}, 403, "method_override"),
    ({"upstream": None}, 502, "provider_not_running"),
    ({"upstream": ("api", 8000)}, 403, "upstream_is_boss"),          # the BOSS API
    ({"upstream": ("api", 8090)}, 403, "upstream_is_boss"),          # egress's GitHub path
    ({"upstream": ("db", 5432)}, 403, "upstream_not_preview_host"),  # the database
    ({"upstream": ("web", 3000)}, 403, "upstream_not_preview_host"), # the console
    ({"upstream": ("169.254.169.254", 80)}, 403, "upstream_not_preview_host"),
    ({"upstream": ("api", 18150)}, 403, "upstream_is_a_link"),       # chaining into another link
    ({"upstream": ("api", 22)}, 403, "upstream_outside_preview_ports"),
    ({"resolved": ("169.254.169.254",)}, 403, "upstream_metadata_or_unroutable"),
])
def test_every_refusal(over, status, reason):
    d = _decide(**over)
    assert (d.allowed, d.status, d.reason) == (False, status, reason)


def test_the_pass_and_hop_headers_never_reach_the_provider_and_its_own_auth_does():
    out = lr.forward_headers({lr.PASS_HEADER: "boss_lnk_secret", "authorization": "Bearer mine",
                              "cookie": "s=1", "host": "egress:18100", "connection": "keep-alive, x-drop",
                              "x-drop": "1", "x-forwarded-for": "1.2.3.4", "content-type": "application/json"},
                             ("api", 19001))
    assert out == {"authorization": "Bearer mine", "cookie": "s=1", "content-type": "application/json",
                   "host": "api:19001"}


def test_port_ranges_parse_and_fall_back():
    assert lr.PortRange.parse("18100-18110", "1-2") == lr.PortRange(18100, 18110)
    assert lr.PortRange.parse("18100:18110", "1-2").iptables() == "18100:18110"
    assert lr.PortRange.parse("junk", "19000-19999") == lr.PortRange(19000, 19999)
    assert lr.PortRange.parse("80-90", "19000-19999") == lr.PortRange(19000, 19999)   # privileged: refused
    assert 19000 in lr.PortRange(19000, 19999) and 18999 not in lr.PortRange(19000, 19999)
