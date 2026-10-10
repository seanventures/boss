"""Every state-changing route honours Idempotency-Key, or is named here with its reason.

Four duplicate-action incidents (2026-08-22 a run started twice by the operator and the
autopilot, 2026-08-30 "Open preview happened twice", 2026-09-10 duplicate chat actions,
2026-10-02 "Stop and run again" started two workflows) were each fixed for the one endpoint that
broke, and the next button had the same hole. The rule is now global middleware — and a global
rule is only as good as its exceptions. This enumerates every mutating route the app mounts, so
an exclusion cannot be added quietly and a new route cannot fall outside the contract unseen.
"""
import inspect
import os

os.environ.setdefault("BOSS_EDITION", "community")

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402

from apps.api.app import idempotency  # noqa: E402
from apps.api.app.main import app  # noqa: E402

# Exactly what the exclusion list is allowed to take out, by route template. Adding a route
# here is a design decision about that route; write the reason into idempotency.EXCLUSIONS.
EXPECTED_EXCLUDED = {
    ("POST", "/v1/delivery-workflows/agreement"),  # authenticated read; no replay DB write
    ("POST", "/v1/worker/poll"), ("POST", "/v1/worker/heartbeat"),
    ("POST", "/v1/worker/complete"), ("POST", "/v1/worker/fail"),
    *{(m, p) for m in ("POST", "PUT", "PATCH", "DELETE")
      for p in ("/v1/previews/{preview_id}/web", "/v1/previews/{preview_id}/web/{path:path}")},
}

# Mutating routes outside /v1/ speak someone else's protocol (SCIM, SAML ACS); the identity
# provider defines their retry semantics, and they are outside this contract by design.
FOREIGN_PROTOCOL_PREFIXES = ("/scim/v2/", "/saml/v2/")


def _mutating_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods & idempotency.MUTATING_METHODS):
                yield method, route


def test_every_mutating_v1_route_is_covered_or_explicitly_excluded():
    excluded, covered = set(), set()
    for method, route in _mutating_routes():
        if not route.path.startswith("/v1/"):
            continue
        reason = idempotency.exclusion_reason(method, route.path)
        (excluded if reason else covered).add((method, route.path))
    assert len(covered) > 400, "the enumeration found too few routes to be the real app"
    unexpected = excluded - EXPECTED_EXCLUDED
    assert not unexpected, (
        f"routes silently outside the idempotency contract: {sorted(unexpected)}")
    missing = EXPECTED_EXCLUDED - excluded
    assert not missing, f"listed as excluded but now covered or gone: {sorted(missing)}"


def test_every_exclusion_names_a_real_route_and_a_real_reason():
    """An exclusion nobody can point at is a hole waiting for the next route to fall into."""
    paths = [route.path for _, route in _mutating_routes()]
    for pattern, reason in idempotency.EXCLUSIONS:
        assert any(pattern.search(path) for path in paths), f"stale exclusion {pattern.pattern}"
        assert len(reason.split()) >= 12, f"say why {pattern.pattern} is excluded, in a sentence"


def test_no_mutating_route_outside_v1_except_foreign_protocols():
    outside = sorted((m, r.path) for m, r in _mutating_routes() if not r.path.startswith("/v1/"))
    strays = [item for item in outside if not item[1].startswith(FOREIGN_PROTOCOL_PREFIXES)]
    assert not strays, f"a write outside /v1/ is outside the idempotency contract: {strays}"


def test_a_streaming_write_has_a_durable_live_replay_contract_or_explicit_exclusion():
    """A stream that can spend is fenced through completion, not exempted at headers.

    The named contract yields live and stores only a bounded, fully drained response;
    actual HTTP/PG duplicate and unknown-outcome proofs live in test_agent_voice_stream_intent.
    A new write stream cannot silently inherit this route-specific policy.
    """
    from apps.api.app import stream_intent
    discovered = set()
    for method, route in _mutating_routes():
        streams = (route.response_class is StreamingResponse
                   or "StreamingResponse(" in inspect.getsource(route.endpoint)
                   or inspect.signature(route.endpoint).return_annotation in
                   (StreamingResponse, "StreamingResponse"))
        if streams and route.path.startswith("/v1/"):
            discovered.add(route.path)
            assert (stream_intent.governs(method, route.path)
                    or idempotency.exclusion_reason(method, route.path)), (method, route.path)
    assert {f"/v1/conversations/{{conversation_id}}/{part}"
            for part in ("turns", "summary", "propose")} <= discovered
    assert idempotency.is_streaming("text/event-stream; charset=utf-8")
    assert not idempotency.exclusion_reason("POST", "/v1/agents/qa/test")


def test_the_middleware_is_installed_inside_authentication():
    """Inside, so it keys by the authenticated principal and a request refused for auth never
    claims a key. Starlette wraps each later @app.middleware around the earlier ones, and
    `user_middleware[0]` is the outermost."""
    names = [getattr(m.kwargs.get("dispatch"), "__name__", None) for m in app.user_middleware]
    assert "one_effect_per_idempotency_key" in names
    assert "require_transport_auth_for_v1" in names
    assert (names.index("require_transport_auth_for_v1")
            < names.index("one_effect_per_idempotency_key"))


def test_what_a_key_remembers():
    """Outcomes that describe the request are kept; outcomes that describe the moment free the
    key so the same press can try again — the "you'll never trigger a run" fear."""
    assert idempotency.remembers(200) and idempotency.remembers(201)
    assert idempotency.remembers(404) and idempotency.remembers(422)
    for transient in (408, 409, 425, 429, 500, 502, 503):
        assert not idempotency.remembers(transient), transient


def test_every_covered_route_really_reaches_the_claim_and_every_excluded_one_does_not(
        monkeypatch, client, engine):
    """NOT A TAUTOLOGY. The test above calls a route "covered" when the exclusion regex does not
    match its TEMPLATE — but the middleware decides on the CONCRETE path a request arrives on,
    and nothing proved a covered route ever reaches the claim (review of 45e7693, still open
    after 6ca9119). Here every mutating /v1 route is requested for real, through the real
    middleware stack, with an Idempotency-Key and a concrete path built from its template. The
    claim is replaced by a recorder that answers "replay", so no handler runs and nothing is
    written; a covered route must reach it, on its own template, and come back replayed — an
    excluded one must not reach it at all."""
    from fastapi.testclient import TestClient

    from apps.api.app import main

    from apps.api.tests.identity_fixtures import IdentityWorld
    world = IdentityWorld.create(engine)
    owner = world.person("owned census caller")
    world.bind(owner, "owner", project=world.project_a)
    with engine.begin() as c:
        from sqlalchemy import text
        c.execute(text("INSERT INTO agent_conversations(conversation_id,project_id,actor,kind,title,seats) "
                       "VALUES('cnv_census',:p,:a,'talk','Owned census','[\"qa\"]')"),
                  {"p":world.project_a,"a":owner.id})
    reached: list[tuple[str, str]] = []

    def recorder(**kwargs):
        reached.append((kwargs["method"], kwargs["path"]))
        return idempotency.Claim(outcome="replay", status_code=200, body=b"{}",
                                 content_type="application/json")

    monkeypatch.setattr(main, "_idempotency_claim", recorder)
    client = TestClient(app, raise_server_exceptions=False)
    missed, leaked = [], []
    import re as _re

    for method, route in _mutating_routes():
        if not route.path.startswith("/v1/"):
            continue
        concrete = _re.sub(r"\{[^}:]+(?::path)?\}", "zzparam", route.path)
        reached.clear()
        # Governed voice replays check current project permission before the claim.
        # Use the actual seeded scope; do not mock away that new admission boundary.
        from apps.api.app import stream_intent
        probe_body = ({"project_id": "boss-dogfood", "actor": "ceo",
                       "probe": "intro", "field": "mission"}
                      if stream_intent.governs(method, concrete) else {})
        if stream_intent.family(method, concrete) == "conversation":
            concrete = concrete.replace("zzparam", "cnv_census")
            with world.acting_as(world.ctx(owner)):
                response = client.request(method, concrete, json={"project_id": world.project_a},
                                          headers={idempotency.HEADER: "route-probe-1"})
        else:
            response = client.request(method, concrete, json=probe_body,
                                      headers={idempotency.HEADER: "route-probe-1"})
        excluded = idempotency.exclusion_reason(method, route.path)
        if excluded:
            if reached:
                leaked.append((method, route.path))
            continue
        if reached != [(method, concrete)] \
                or response.headers.get(idempotency.REPLAYED_HEADER) != "true":
            missed.append((method, route.path, response.status_code))
    assert not leaked, f"excluded routes that still claim a key: {sorted(leaked)}"
    assert not missed, (
        f"covered routes whose requests never reach the claim: {sorted(missed)[:40]}")
