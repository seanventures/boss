"""Plane → BOSS inbound webhook (Phase 1): signature verification, filtering, field mapping,
and idempotency. Hermetic — the pure pieces need no DB, and ingest() runs against a fake
connection with start_code_task monkeypatched, so nothing here touches Postgres."""
import hashlib
import hmac
import json

from apps.api.app import main as api_main
from apps.api.app.config import Settings
from apps.api.app.services import plane_webhooks
from apps.api.app.services.plane_webhooks import PlaneWebhookService

SECRET = "plane-webhook-secret"
PROJECT_MAP = json.dumps(
    {"proj-1": {"owner": "paramg", "repo": "boss-sandbox", "project_id": "boss-dogfood"}}
)


def _settings(**over):
    base = dict(
        PLANE_INTEGRATION_ENABLED="true",
        PLANE_WEBHOOK_SECRET=SECRET,
        PLANE_PROJECT_MAP=PROJECT_MAP,
        PLANE_WATCH_LABELS="boss:coding,boss:bug,boss:design",
    )
    base.update(over)
    return Settings(**base)


def _svc(**over):
    return PlaneWebhookService(_settings(**over))


def _sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _issue_payload(**over):
    issue = {
        "id": "issue-abc",
        "name": "Fix the broken export button",
        "description_stripped": "Clicking Export throws a 500.",
        "project": "proj-1",
        "sequence_id": 42,
        "labels": [{"name": "boss:bug"}],
    }
    issue.update(over.pop("issue", {}))
    payload = {"event": "issue", "action": "created", "data": issue}
    payload.update(over)
    return payload


# --- signature ---------------------------------------------------------------
def test_verify_signature_accepts_matching_hex():
    body = b'{"event":"issue"}'
    assert _svc().verify_signature(body, _sign(body)) is True


def test_verify_signature_tolerates_sha256_prefix():
    body = b'{"event":"issue"}'
    assert _svc().verify_signature(body, "sha256=" + _sign(body)) is True


def test_verify_signature_rejects_wrong_secret_and_empty():
    body = b'{"event":"issue"}'
    svc = _svc()
    assert svc.verify_signature(body, _sign(body, "not-the-secret")) is False
    assert svc.verify_signature(body, "") is False               # unsigned
    assert _svc(PLANE_WEBHOOK_SECRET="").verify_signature(body, _sign(body)) is False  # no secret


# --- parsing -----------------------------------------------------------------
def test_parse_flattens_nested_issue_and_labels():
    p = _svc().parse(_issue_payload())
    assert p["action"] == "created" and p["event"] == "issue"
    assert p["plane_issue_id"] == "issue-abc" and p["plane_project_id"] == "proj-1"
    assert p["title"] == "Fix the broken export button"
    assert p["description"] == "Clicking Export throws a 500."
    assert p["labels"] == {"boss:bug"}


_UUID = "055f8484-ff76-4cdb-9446-27eb046d476d"


def test_resolve_labels_maps_uuids_to_names(monkeypatch):
    # Plane's real webhook sends label UUIDs, not names — resolve them via the (mocked) API.
    svc = _svc()
    monkeypatch.setattr(svc, "_project_labels", lambda pid, force=False: {_UUID: "boss:coding"})
    assert svc._resolve_labels("proj-1", {_UUID}) == {"boss:coding"}
    # plain names pass through untouched (no API call needed)
    assert svc._resolve_labels("proj-1", {"boss:bug"}) == {"boss:bug"}
    # no project id -> can't resolve, returns as-is
    assert svc._resolve_labels(None, {_UUID}) == {_UUID}


def test_ingest_accepts_uuid_labelled_issue_after_resolution(monkeypatch):
    calls = []
    _patch_start(monkeypatch, calls)
    svc = _svc()
    monkeypatch.setattr(svc, "_project_labels", lambda pid, force=False: {_UUID: "boss:coding"})
    # a payload exactly like Plane's — labels are UUIDs
    payload = _issue_payload(issue={"labels": [_UUID]})
    body = json.dumps(payload).encode()
    res = svc.ingest(_FakeConn(), body, {"x-plane-signature": _sign(body)}, jobs=object())
    assert res["status"] == "accepted" and res["label"] == "boss:coding"
    assert calls[0]["finalize_ctx"]["plane_label"] == "boss:coding"


def test_labels_tolerates_string_and_dict_shapes_and_bare_ids():
    svc = _svc()
    assert svc._labels_of({"labels": ["boss:coding", "BOSS:Bug"]}) == {"boss:coding", "boss:bug"}
    assert svc._labels_of({"labels": [{"name": "boss:design"}]}) == {"boss:design"}
    # bare label ids can't be matched to names — they simply don't appear
    assert svc._labels_of({"labels": ["7f3a-uuid-only"]}) == {"7f3a-uuid-only"}
    assert svc._labels_of({}) == set()


def test_project_map_is_defensive_against_bad_json():
    assert _svc(PLANE_PROJECT_MAP="not json")._project_map() == {}
    assert _svc(PLANE_PROJECT_MAP="[1,2]")._project_map() == {}
    assert "proj-1" in _svc()._project_map()


# --- routing (pure decision) -------------------------------------------------
def test_route_accepts_bug_in_watched_project():
    d = _svc().route(_svc().parse(_issue_payload()))
    assert d["decision"] == "accept"
    assert d["label"] == "boss:bug"
    assert d["owner"] == "paramg" and d["repo"] == "boss-sandbox"
    assert d["project_id"] == "boss-dogfood"


def test_route_ignores_updates_to_prevent_loops():
    d = _svc().route(_svc().parse(_issue_payload(action="updated")))
    assert d["decision"] == "ignore" and "not created" in d["reason"]


def test_route_ignores_unwatched_project():
    payload = _issue_payload(issue={"project": "some-other-project"})
    d = _svc().route(_svc().parse(payload))
    assert d["decision"] == "ignore" and "watch/repo map" in d["reason"]


def test_route_ignores_issue_without_boss_label():
    payload = _issue_payload(issue={"labels": [{"name": "backend"}]})
    d = _svc().route(_svc().parse(payload))
    assert d["decision"] == "ignore" and "no boss: label" in d["reason"]


def test_route_defers_design_only_to_phase_3():
    payload = _issue_payload(issue={"labels": [{"name": "boss:design"}]})
    d = _svc().route(_svc().parse(payload))
    assert d["decision"] == "ignore" and "Phase 3" in d["reason"]


# --- ingest (fake connection, start_code_task monkeypatched) -----------------
class _FakeResult:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _FakeConn:
    """Stands in for a SQLAlchemy connection; execute() only serves the dedup SELECT."""
    def __init__(self, existing=None):
        self.existing = existing

    def execute(self, *_a, **_k):
        return _FakeResult(self.existing)


def _patch_start(monkeypatch, sink):
    def fake_start(connection, **kw):
        sink.append(kw)
        return "crun_fake123"
    monkeypatch.setattr(plane_webhooks, "start_code_task", fake_start)


def test_ingest_accepts_signed_bug_and_starts_code_task(monkeypatch):
    calls = []
    _patch_start(monkeypatch, calls)
    body = json.dumps(_issue_payload()).encode()
    res = _svc().ingest(_FakeConn(), body, {"x-plane-signature": _sign(body)}, jobs=object())
    assert res["status"] == "accepted" and res["run_id"] == "crun_fake123"
    # mapping carried through to start_code_task
    kw = calls[0]
    assert kw["owner"] == "paramg" and kw["repo"] == "boss-sandbox"
    assert kw["project_id"] == "boss-dogfood"
    assert kw["finalize_ctx"]["plane_issue_id"] == "issue-abc"
    assert kw["finalize_ctx"]["source"] == "plane"
    assert kw["finalize_ctx"]["plane_label"] == "boss:bug"


def test_ingest_rejects_bad_signature(monkeypatch):
    calls = []
    _patch_start(monkeypatch, calls)
    body = json.dumps(_issue_payload()).encode()
    res = _svc().ingest(_FakeConn(), body, {"x-plane-signature": "deadbeef"}, jobs=object())
    assert res["status"] == "rejected" and not calls   # never dispatched


def test_ingest_not_configured_when_disabled_or_secretless(monkeypatch):
    _patch_start(monkeypatch, [])
    body = json.dumps(_issue_payload()).encode()
    hdr = {"x-plane-signature": _sign(body)}
    assert _svc(PLANE_INTEGRATION_ENABLED="false").ingest(_FakeConn(), body, hdr, object())["status"] == "not_configured"
    assert _svc(PLANE_WEBHOOK_SECRET="").ingest(_FakeConn(), body, hdr, object())["status"] == "not_configured"


def test_ingest_dedups_on_existing_plane_issue(monkeypatch):
    calls = []
    _patch_start(monkeypatch, calls)
    body = json.dumps(_issue_payload()).encode()
    # connection reports an existing run for this issue -> duplicate, no new dispatch
    res = _svc().ingest(_FakeConn(existing="crun_prev"), body, {"x-plane-signature": _sign(body)}, object())
    assert res["status"] == "duplicate" and res["run_id"] == "crun_prev"
    assert not calls


def test_ingest_ignores_wrong_label_but_valid_signature(monkeypatch):
    calls = []
    _patch_start(monkeypatch, calls)
    payload = _issue_payload(issue={"labels": [{"name": "backend"}]})
    body = json.dumps(payload).encode()
    res = _svc().ingest(_FakeConn(), body, {"x-plane-signature": _sign(body)}, object())
    assert res["status"] == "ignored" and not calls


def test_ingest_invalid_json_body(monkeypatch):
    _patch_start(monkeypatch, [])
    body = b"not json at all"
    res = _svc().ingest(_FakeConn(), body, {"x-plane-signature": _sign(body)}, object())
    assert res["status"] == "invalid"


# --- endpoint auth-exemption (integration) -----------------------------------
def test_endpoint_is_auth_exempt(client, monkeypatch):
    """With transport auth ON, an unauthenticated POST still reaches the handler (503 not_configured
    when the integration is off) rather than being blocked by the middleware (401) — proving the
    /v1/webhooks/plane exemption, mirroring the GitHub webhook."""
    monkeypatch.setattr(api_main.settings, "require_api_auth", True)
    monkeypatch.setattr(api_main.plane_webhooks.settings, "plane_integration_enabled", False)
    resp = client.post(
        "/v1/webhooks/plane",
        content=b'{"event":"issue","action":"created"}',
        headers={"Content-Type": "application/json", "X-Plane-Signature": "whatever"},
    )
    assert resp.status_code == 503   # handler reached (not_configured), NOT a middleware 401
