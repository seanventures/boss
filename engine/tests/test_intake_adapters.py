"""The two P9a adapters without a database: the generic signed webhook (Standard Webhooks and
BOSS v1) and GitHub (X-Hub-Signature-256, delivery-id dedupe, own-bot loop filter, normalised
types, push with a lazily fetched changed-files list)."""
from __future__ import annotations

import base64
import json
import time

import pytest

from boss_engine.connectors.intake import get_intake_adapter
from boss_engine.connectors.intake.contract import (
    IntakeConnection,
    ParseRefused,
    RawRequest,
    StoredRaw,
    VerifyRefused,
)
from boss_engine.connectors.intake.github import adapter as gh
from boss_engine.connectors.intake.webhook import adapter as wh
from boss_engine.services.workflow_webhooks import sign as boss_sign

from ._intake_support import (
    boss_v1_request,
    github_pr_opened,
    github_push,
    github_request,
    standard_webhook_request,
)

NOW = 1_763_100_000.0


def _conn(kind: str, secret_name: str, secret: str, **config) -> IntakeConnection:
    return IntakeConnection(f"icn_{kind}", "p_1", kind, config=config,
                            secret_resolver=lambda n: secret if n == secret_name else None)


def _stored(req: RawRequest, adapter, conn) -> StoredRaw:
    return StoredRaw(body=req.body, headers=dict(req.headers), dedupe_key=adapter.dedupe_key(conn, req),
                     event_row_id="iev_1")


# ═══════════════════════════════════════════════════════════════════════════════ webhook ═══


def test_standard_webhooks_published_vector():
    """The vector from the Standard Webhooks specification: a base64 whsec_ key, msg id,
    timestamp and body give exactly this signature."""
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    body = b'{"test": 2432232314}'
    sig = wh.standard_webhooks_signature(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", "1614265330", body)
    assert sig == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_webhook_verifies_standard_webhooks_and_boss_v1_and_nothing_else():
    adapter = get_intake_adapter("webhook")
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    conn = _conn("webhook", "signing_secret", secret)
    req = standard_webhook_request(secret, {"hello": 1}, timestamp=int(NOW))
    assert adapter.verify(conn, req, NOW).scheme == "standard_webhooks"
    # several signatures while a sender rotates: any match counts
    rotated = RawRequest(method="POST", headers={**req.headers, "webhook-signature":
                         "v1,AAAA " + req.header("webhook-signature")}, body=req.body)
    assert adapter.verify(conn, rotated, NOW).scheme == "standard_webhooks"
    v1 = boss_v1_request(secret, {"hello": 1}, timestamp=int(NOW))
    assert adapter.verify(conn, v1, NOW).scheme == "boss_v1"
    # the BOSS v1 scheme is byte-for-byte the one workflow/agent webhooks already use
    assert v1.header("x-boss-signature") == boss_sign(secret, int(NOW), v1.body)
    with pytest.raises(VerifyRefused) as refused:
        adapter.verify(conn, RawRequest(method="POST", headers={}, body=b"{}"), NOW)
    assert refused.value.reason == "missing_signature"


@pytest.mark.parametrize("mutate, reason", [
    (lambda h: h.update({"webhook-signature": "v1,bm9wZQ=="}), "bad_signature"),
    (lambda h: h.update({"webhook-timestamp": str(int(NOW) - 301)}), "stale_timestamp"),
    (lambda h: h.update({"webhook-timestamp": str(int(NOW) + 301)}), "stale_timestamp"),
    (lambda h: h.update({"webhook-timestamp": "soon"}), "bad_timestamp"),
    (lambda h: h.pop("webhook-id"), "missing_signature"),
])
def test_webhook_refuses_with_one_word(mutate, reason):
    adapter = get_intake_adapter("webhook")
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    conn = _conn("webhook", "signing_secret", secret)
    req = standard_webhook_request(secret, {"hello": 1}, timestamp=int(NOW))
    headers = dict(req.headers)
    mutate(headers)
    with pytest.raises(VerifyRefused) as refused:
        adapter.verify(conn, RawRequest(method="POST", headers=headers, body=req.body), NOW)
    assert refused.value.reason == reason


def test_webhook_tampered_body_and_missing_secret_are_refused():
    adapter = get_intake_adapter("webhook")
    secret = "whsec_" + base64.b64encode(b"k" * 32).decode()
    conn = _conn("webhook", "signing_secret", secret)
    req = standard_webhook_request(secret, {"amount": 10}, timestamp=int(NOW))
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(conn, RawRequest(method="POST", headers=req.headers,
                                        body=b'{"amount": 1000}'), NOW)
    unconfigured = _conn("webhook", "signing_secret", "")
    with pytest.raises(VerifyRefused, match="not_configured"):
        adapter.verify(unconfigured, req, NOW)
    v1 = boss_v1_request(secret, {"a": 1}, timestamp=int(NOW))
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(_conn("webhook", "signing_secret", "whsec_other"), v1, NOW)


def test_webhook_dedupe_key_prefers_the_senders_delivery_id_then_the_signed_body():
    adapter = get_intake_adapter("webhook")
    conn = _conn("webhook", "signing_secret", "s")
    req = standard_webhook_request("s", {"x": 1}, msg_id="msg_1", timestamp=int(NOW))
    assert adapter.dedupe_key(conn, req) == "webhook:msg_1"
    v1 = boss_v1_request("s", {"x": 1}, delivery="d-9", timestamp=int(NOW))
    assert adapter.dedupe_key(conn, v1) == "webhook:d-9"
    anon = boss_v1_request("s", {"x": 1}, timestamp=int(NOW))
    k1 = adapter.dedupe_key(conn, anon)
    assert k1.startswith("webhook:") and len(k1) == len("webhook:") + 64
    assert adapter.dedupe_key(conn, boss_v1_request("s", {"x": 1}, timestamp=int(NOW))) == k1
    assert adapter.dedupe_key(conn, boss_v1_request("s", {"x": 2}, timestamp=int(NOW))) != k1


def test_webhook_parse_carries_cloudevents_through_and_wraps_plain_json():
    adapter = get_intake_adapter("webhook")
    conn = _conn("webhook", "signing_secret", "s", tenant="grafana")
    ce = {"specversion": "1.0", "id": "A1", "source": "grafana://prod", "type": "Com.Grafana.Alert",
          "subject": "payments-api", "time": "2026-11-14T09:13:58Z", "data": {"title": "err > 2%"}}
    req = standard_webhook_request("s", ce, msg_id="msg_ce", timestamp=int(NOW))
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert (ev.id, ev.source, ev.type, ev.subject) == ("A1", "grafana://prod", "com.grafana.alert",
                                                       "payments-api")
    assert ev.data == {"title": "err > 2%"} and ev.bossdedupe == "webhook:msg_ce"
    assert ev.bossconnection == conn.id and ev.bossactor.kind == "service"
    assert adapter.conversation_key(conn, ev) == "subject:payments-api"
    plain = standard_webhook_request("s", {"status": "firing", "alerts": [1, 2]}, msg_id="msg_p",
                                     timestamp=int(NOW))
    [ev2] = adapter.parse(conn, _stored(plain, adapter, conn))
    assert ev2.type == "com.boss.webhook.received" and ev2.source == "webhook://grafana"
    assert ev2.id == "msg_p" and ev2.data["alerts"] == [1, 2] and ev2.subject is None
    assert ev2.time.startswith("2025") or ev2.time.startswith("2026")   # from webhook-timestamp
    assert adapter.conversation_key(conn, ev2) is None
    with_causality = standard_webhook_request("s", {"boss": {"run_id": "r1", "hops": 2}},
                                              msg_id="msg_c", timestamp=int(NOW))
    [ev3] = adapter.parse(conn, _stored(with_causality, adapter, conn))
    assert ev3.bosscausality == {"run_id": "r1", "deployment_id": None, "hops": 2}
    bad = standard_webhook_request("s", b"not json", msg_id="msg_b", timestamp=int(NOW))
    with pytest.raises(ParseRefused):
        adapter.parse(conn, _stored(bad, adapter, conn))


def test_webhook_cloudevents_abuse_protection_handshake():
    adapter = get_intake_adapter("webhook")
    conn = _conn("webhook", "signing_secret", "s", allowed_origins="grafana.example, other.example")
    ok = adapter.handshake(conn, RawRequest(method="OPTIONS", headers={
        "webhook-request-origin": "grafana.example"}, body=b""))
    assert ok.status == 200 and ok.headers["WebHook-Allowed-Origin"] == "grafana.example"
    denied = adapter.handshake(conn, RawRequest(method="OPTIONS", headers={
        "webhook-request-origin": "evil.example"}, body=b""))
    assert denied.status == 403
    assert adapter.handshake(conn, RawRequest(method="OPTIONS", headers={}, body=b"")).status == 400
    assert adapter.handshake(conn, RawRequest(method="POST", headers={}, body=b"{}")) is None


# ═══════════════════════════════════════════════════════════════════════════════ github ════


def test_github_verifies_x_hub_signature_256_only():
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "gh-secret")
    req = github_request("gh-secret", "ping", {"zen": "Keep it logically awesome."})
    assert adapter.verify(conn, req, NOW).scheme == "github_sha256"
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(_conn("github", "webhook_secret", "other"), req, NOW)
    with pytest.raises(VerifyRefused, match="missing_signature"):
        adapter.verify(conn, RawRequest(method="POST", headers={"x-github-event": "push"},
                                        body=req.body), NOW)
    with pytest.raises(VerifyRefused, match="not_configured"):
        adapter.verify(_conn("github", "webhook_secret", ""), req, NOW)
    # ping is a handshake: answered, never stored
    assert adapter.handshake(conn, req).status == 200
    assert adapter.handshake(conn, github_request("gh-secret", "push", github_push())) is None


def test_github_dedupes_on_the_delivery_id():
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s")
    req = github_request("s", "push", github_push(), delivery="72d3162e-cc78-11e3-81ab-4c9367dc0958")
    assert adapter.dedupe_key(conn, req) == "github:72d3162e-cc78-11e3-81ab-4c9367dc0958"
    headers = {k: v for k, v in req.headers.items() if k != "x-github-delivery"}
    anon = RawRequest(method="POST", headers=headers, body=req.body)
    assert adapter.dedupe_key(conn, anon) == "github:sha256:" + req.body_sha256()


def test_github_normalises_a_pull_request_to_the_record_shape():
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s")
    req = github_request("s", "pull_request", github_pr_opened(7), delivery="d-pr")
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert ev.type == "com.github.pull_request.opened"
    assert ev.source == "github://9001" and ev.subject == "northpeak/payments-api#7"
    assert ev.id == "d-pr" and ev.time == "2026-11-14T09:14:02Z"
    assert ev.data["record_type"] == "pull_request" and ev.data["record_id"] == "7"
    assert ev.data["operation"] == "created" and ev.data["fields"]["labels"] == ["bug"]
    assert ev.data["fields"]["base"] == {"ref": "main", "sha": "a" * 40}
    assert ev.data["title"] == "Fix the payments job" and ev.data["head_ref"] == "fix/payments"
    assert ev.bossactor.kind == "user" and ev.bossactor.display == "priya"
    assert adapter.conversation_key(conn, ev) == "northpeak/payments-api#7"
    assert ev.text_fields()["title"] == "Fix the payments job"
    hint = adapter.resolve_person(conn, ev.bossactor)
    assert hint.display == "priya" and hint.is_bot is False


def test_github_push_takes_changed_files_from_a_complete_payload():
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s")
    req = github_request("s", "push", github_push(commits=3), delivery="d-push")
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert ev.type == "com.github.push" and ev.subject == "northpeak/payments-api@refs/heads/main"
    assert ev.data["commit_count"] == 3 and ev.data["before"] == "c" * 40
    assert ev.data["changed_files_source"] == "payload"
    names = {f["filename"]: f["status"] for f in ev.data["changed_files"]}
    assert names == {"src/new_0.py": "added", "src/new_1.py": "added", "src/new_2.py": "added",
                     "README.md": "modified"}
    assert ev.data["message"] == "commit 2"
    assert adapter.enrich(conn, ev) is ev            # nothing to fetch


def test_github_push_fetches_changed_files_lazily_when_the_payload_is_truncated(monkeypatch):
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s")
    calls: list = []

    def transport(method, url, headers, body, purpose):
        calls.append((method, url, dict(headers), purpose))
        return 200, {"files": [{"filename": "a.py", "status": "modified", "additions": 3,
                                "deletions": 1}, {"filename": "b.py", "status": "added"}]}

    monkeypatch.setattr(gh, "TRANSPORT", transport)
    req = github_request("s", "push", github_push(commits=25), delivery="d-big")
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert ev.data["changed_files"] is None and ev.data["changed_files_source"] == "pending"
    assert len(ev.data["commits"]) == 20          # the payload's own cap
    # no token on the connection: fetched anonymously, through the guarded transport
    ev = adapter.enrich(conn, ev)
    assert ev.data["changed_files_source"] == "compare_api"
    assert [f["filename"] for f in ev.data["changed_files"]] == ["a.py", "b.py"]
    (method, url, headers, purpose), = calls
    assert method == "GET" and purpose == "intake.github.compare"
    assert url == ("https://api.github.com/repos/northpeak/payments-api/compare/"
                   + "c" * 40 + "..." + "d" * 40)
    assert "Authorization" not in headers
    # with a token the call is authenticated; the token never lands on the event
    with_token = IntakeConnection("icn_t", "p_1", "github", secret_resolver=lambda n: {
        "webhook_secret": "s", "api_token": "ghp_SECRET"}.get(n))
    [ev2] = adapter.parse(with_token, _stored(req, adapter, with_token))
    ev2 = adapter.enrich(with_token, ev2)
    assert calls[-1][2]["Authorization"] == "Bearer ghp_SECRET"
    assert "ghp_SECRET" not in json.dumps(ev2.to_dict())


def test_github_push_enrich_failures_leave_the_event_routable(monkeypatch):
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s")
    monkeypatch.setattr(gh, "TRANSPORT", lambda *a: (403, {"message": "rate limited"}))
    req = github_request("s", "push", github_push(commits=25), delivery="d-403")
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    ev = adapter.enrich(conn, ev)
    assert ev.data["changed_files"] is None and ev.data["changed_files_source"] == "error"
    assert ev.data["changed_files_error"] == "GitHub answered 403"

    def boom(*a):
        raise OSError("network")

    monkeypatch.setattr(gh, "TRANSPORT", boom)
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert adapter.enrich(conn, ev).data["changed_files_error"] == "OSError"
    # a branch creation (before = 0...0) has nothing to compare against
    created = github_push(commits=25)
    created["before"] = "0" * 40
    [ev] = adapter.parse(conn, _stored(github_request("s", "push", created), adapter, conn))
    assert adapter.enrich(conn, ev).data["changed_files_source"] == "unavailable"


def test_github_own_bot_and_other_bots_are_filtered_unless_allowed(monkeypatch):
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s", bot_login="northpeak-boss[bot]")

    def parse(sender, sender_type):
        body = github_pr_opened(8, sender=sender, sender_type=sender_type)
        [ev] = adapter.parse(conn, _stored(github_request("s", "pull_request", body), adapter, conn))
        return ev

    assert adapter.is_self(conn, parse("priya", "User")) is False
    assert adapter.is_self(conn, parse("NorthPeak-BOSS[bot]", "Bot")) is True
    assert adapter.is_self(conn, parse("dependabot[bot]", "Bot")) is True      # other bots: dropped
    allowing = _conn("github", "webhook_secret", "s", bot_login="northpeak-boss[bot]",
                     allow_bots="dependabot[bot], renovate[bot]")
    assert adapter.is_self(allowing, parse("dependabot[bot]", "Bot")) is False
    # the install's GitHub App bot is the default self identity
    default_conn = _conn("github", "webhook_secret", "s")
    monkeypatch.setattr(gh, "_csv", lambda v: set())
    assert adapter.is_self(default_conn, parse("boss[bot]", "User")) is True


def test_github_issue_comment_and_repository_filter():
    adapter = get_intake_adapter("github")
    conn = _conn("github", "webhook_secret", "s", repository="northpeak/payments-api")
    body = {"action": "created", "issue": {"number": 311, "title": "Q", "html_url": "u",
                                          "updated_at": "2026-11-14T09:12:31Z", "labels": []},
            "comment": {"body": "Can you check?", "html_url": "u#c"},
            "repository": {"full_name": "northpeak/payments-api"},
            "sender": {"login": "dev", "id": 7, "type": "User"}}
    [ev] = adapter.parse(conn, _stored(github_request("s", "issue_comment", body), adapter, conn))
    assert ev.type == "com.github.issue_comment.created" and ev.data["operation"] == "commented"
    assert ev.data["comment"] == "Can you check?" and ev.subject == "northpeak/payments-api#311"
    other = dict(body, repository={"full_name": "someone/else"})
    assert adapter.parse(conn, _stored(github_request("s", "issue_comment", other), adapter, conn)) == []
    with pytest.raises(ParseRefused):
        adapter.parse(conn, _stored(github_request("s", "push", b"nope"), adapter, conn))
    headerless = StoredRaw(body=json.dumps(body).encode(), headers={}, dedupe_key="github:x")
    with pytest.raises(ParseRefused, match="X-GitHub-Event"):
        adapter.parse(conn, headerless)


def test_time_helper_is_current():
    assert abs(wh.now_seconds() - time.time()) < 5
