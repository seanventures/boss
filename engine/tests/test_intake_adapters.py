"""The two P9a adapters without a database: the generic signed webhook (Standard Webhooks and
BOSS v1) and GitHub (X-Hub-Signature-256, delivery-id dedupe, own-bot loop filter, normalised
types, push with a lazily fetched changed-files list)."""
from __future__ import annotations

import base64
import hashlib
import hmac
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
WHSEC = "whsec_" + base64.b64encode(b"k" * 32).decode()   # a Standard Webhooks secret


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


def _reference_standard_webhooks_sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    """The standardwebhooks reference library's algorithm, written out independently: strip the
    optional `whsec_`, base64-decode the key, HMAC-SHA256 over `{id}.{timestamp}.{body}`,
    base64 the digest, prefix `v1,`."""
    key = base64.b64decode(secret.split("whsec_", 1)[-1])
    signed = f"{msg_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()


def test_a_minted_secret_is_standard_webhooks_compatible_and_a_compliant_sender_verifies():
    from boss_engine.services.event_intake import _mint

    adapter = get_intake_adapter("webhook")
    for _ in range(5):
        secret = _mint("signing_secret")
        assert secret.startswith("whsec_")
        assert len(base64.b64decode(secret[len("whsec_"):], validate=True)) == 32
        conn = _conn("webhook", "signing_secret", secret)
        body = b'{"test": 2432232314}'
        headers = {"webhook-id": "msg_1", "webhook-timestamp": str(int(NOW)),
                   "webhook-signature": _reference_standard_webhooks_sign(secret, "msg_1",
                                                                          str(int(NOW)), body)}
        assert adapter.verify(conn, RawRequest(method="POST", headers=headers, body=body),
                              NOW).scheme == "standard_webhooks"
    # the published vector verifies end to end, with and without the prefix
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    body = b'{"test": 2432232314}'
    headers = {"webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek", "webhook-timestamp": "1614265330",
               "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="}
    req = RawRequest(method="POST", headers=headers, body=body)
    assert adapter.verify(_conn("webhook", "signing_secret", secret), req, 1614265330.0).scheme \
        == "standard_webhooks"
    assert adapter.verify(_conn("webhook", "signing_secret", "MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"),
                          req, 1614265330.0).scheme == "standard_webhooks"
    # a secret that is not base64 cannot be what a compliant sender signed with: not_configured
    with pytest.raises(VerifyRefused, match="not_configured"):
        adapter.verify(_conn("webhook", "signing_secret", "whsec_not base64!!"), req, 1614265330.0)
    with pytest.raises(wh.SecretNotBase64):
        wh.standard_webhooks_signature("whsec_not base64!!", "m", "1", b"")


def test_the_previous_secret_verifies_only_inside_its_rotation_grace():
    adapter = get_intake_adapter("webhook")
    old, new = "whsec_" + base64.b64encode(b"o" * 32).decode(), "whsec_" + base64.b64encode(b"n" * 32).decode()
    from datetime import datetime, timezone

    until = datetime.fromtimestamp(NOW + 600, tz=timezone.utc).isoformat()
    conn = IntakeConnection("icn_r", "p_1", "webhook", config={"signing_secret_previous_until": until},
                            secret_resolver=lambda n: {"signing_secret": new,
                                                       "signing_secret_previous": old}.get(n))
    req_old = standard_webhook_request(old, {"a": 1}, timestamp=int(NOW))
    req_new = standard_webhook_request(new, {"a": 1}, timestamp=int(NOW))
    assert adapter.verify(conn, req_old, NOW).scheme == "standard_webhooks"
    assert adapter.verify(conn, req_new, NOW).scheme == "standard_webhooks"
    late = standard_webhook_request(old, {"a": 1}, timestamp=int(NOW) + 601)
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(conn, late, NOW + 601)
    assert adapter.verify(conn, boss_v1_request(old, {"a": 1}, timestamp=int(NOW)), NOW).scheme == "boss_v1"


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
        adapter.verify(_conn("webhook", "signing_secret", "whsec_" + base64.b64encode(b"z" * 32).decode()),
                       v1, NOW)


def test_webhook_dedupe_key_prefers_the_senders_delivery_id_then_the_signed_body():
    adapter = get_intake_adapter("webhook")
    conn = _conn("webhook", "signing_secret", WHSEC)
    req = standard_webhook_request(WHSEC, {"x": 1}, msg_id="msg_1", timestamp=int(NOW))
    assert adapter.dedupe_key(conn, req) == "webhook:msg_1"
    v1 = boss_v1_request(WHSEC, {"x": 1}, delivery="d-9", timestamp=int(NOW))
    assert adapter.dedupe_key(conn, v1) == "webhook:d-9"
    anon = boss_v1_request(WHSEC, {"x": 1}, timestamp=int(NOW))
    k1 = adapter.dedupe_key(conn, anon)
    assert k1.startswith("webhook:") and len(k1) == len("webhook:") + 64
    assert adapter.dedupe_key(conn, boss_v1_request(WHSEC, {"x": 1}, timestamp=int(NOW))) == k1
    assert adapter.dedupe_key(conn, boss_v1_request(WHSEC, {"x": 2}, timestamp=int(NOW))) != k1


def test_webhook_parse_carries_cloudevents_through_and_wraps_plain_json():
    adapter = get_intake_adapter("webhook")
    conn = _conn("webhook", "signing_secret", WHSEC, tenant="grafana")
    ce = {"specversion": "1.0", "id": "A1", "source": "grafana://prod", "type": "Com.Grafana.Alert",
          "subject": "payments-api", "time": "2026-11-14T09:13:58Z", "data": {"title": "err > 2%"}}
    req = standard_webhook_request(WHSEC, ce, msg_id="msg_ce", timestamp=int(NOW))
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert (ev.id, ev.source, ev.type, ev.subject) == ("A1", "grafana://prod", "com.grafana.alert",
                                                       "payments-api")
    assert ev.data == {"title": "err > 2%"} and ev.bossdedupe == "webhook:msg_ce"
    assert ev.bossconnection == conn.id and ev.bossactor.kind == "service"
    assert adapter.conversation_key(conn, ev) == "subject:payments-api"
    plain = standard_webhook_request(WHSEC, {"status": "firing", "alerts": [1, 2]}, msg_id="msg_p",
                                     timestamp=int(NOW))
    [ev2] = adapter.parse(conn, _stored(plain, adapter, conn))
    assert ev2.type == "com.boss.webhook.received" and ev2.source == "webhook://grafana"
    assert ev2.id == "msg_p" and ev2.data["alerts"] == [1, 2] and ev2.subject is None
    assert ev2.time.startswith("2025") or ev2.time.startswith("2026")   # from webhook-timestamp
    assert adapter.conversation_key(conn, ev2) is None
    with_causality = standard_webhook_request(WHSEC, {"boss": {"run_id": "r1", "hops": 2}},
                                              msg_id="msg_c", timestamp=int(NOW))
    [ev3] = adapter.parse(conn, _stored(with_causality, adapter, conn))
    assert ev3.bosscausality == {"run_id": "r1", "deployment_id": None, "hops": 2}
    bad = standard_webhook_request(WHSEC, b"not json", msg_id="msg_b", timestamp=int(NOW))
    with pytest.raises(ParseRefused):
        adapter.parse(conn, _stored(bad, adapter, conn))


def test_webhook_cloudevents_abuse_protection_handshake():
    adapter = get_intake_adapter("webhook")
    conn = _conn("webhook", "signing_secret", WHSEC, allowed_origins="grafana.example, other.example")
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
    # a repository name that is not owner/name, or an odd sha, never reaches the compare URL
    for odd in ("../../evil", "owner/name/extra", "owner", "owner/na me"):
        body = github_push(commits=25, repo=odd)
        [ev] = adapter.parse(conn, _stored(github_request("s", "push", body), adapter, conn))
        assert adapter.enrich(conn, ev).data["changed_files_source"] == "unavailable", odd
    bad_sha = github_push(commits=25)
    bad_sha["after"] = "HEAD; rm -rf"
    [ev] = adapter.parse(conn, _stored(github_request("s", "push", bad_sha), adapter, conn))
    assert adapter.enrich(conn, ev).data["changed_files_source"] == "unavailable"
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


def test_github_ghes_base_url_resolution(monkeypatch):
    from boss_engine.connectors.intake.contract import Cursor

    adapter = get_intake_adapter("github")
    seen_urls: list[str] = []

    def fake_transport(method, url, headers, body, purpose):
        seen_urls.append(url)
        if "pulls?" in url:
            return 200, [{"number": 9, "updated_at": "2026-11-25T00:00:01Z",
                          "head": {"sha": "a" * 40}, "base": {"repo": {"full_name": "northpeak/payments-api"}},
                          "user": {"login": "alice", "id": 1}}], {}
        if "branches?" in url:
            return 200, [], {}
        return 200, [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0}], {}

    monkeypatch.setattr(gh, "TRANSPORT", fake_transport)

    # 1. Explicit api_base_url on connection config
    ghes_conn = IntakeConnection(
        "icn_ghes", "p_1", "github",
        config={"repository": "northpeak/payments-api", "api_base_url": "https://git.corp.example/api/v3/"},
        secret_resolver=lambda k: "tok" if k == "api_token" else "sec",
    )
    [pr_ev] = adapter.parse(ghes_conn, _stored(github_request("sec", "pull_request", github_pr_opened(9)), adapter, ghes_conn))
    pr_ev = adapter.enrich(ghes_conn, pr_ev)
    adapter.load_lazy(ghes_conn, pr_ev, "data.changed_files")
    adapter.poll(ghes_conn, Cursor("default", None, "2026-11-25T00:00:00Z"), 10)
    assert all(u.startswith("https://git.corp.example/api/v3/repos/northpeak/payments-api/") for u in seen_urls), seen_urls

    # 2. Operator BOSS_GITHUB_API_URL env fallback when api_base_url is omitted
    seen_urls.clear()
    monkeypatch.setenv("BOSS_GITHUB_API_URL", "https://ghe.env.example/api/v3")
    env_conn = IntakeConnection("icn_env", "p_1", "github", config={"repository": "northpeak/payments-api"})
    adapter.poll(env_conn, Cursor("default", None, "2026-11-25T00:00:00Z"), 10)
    assert all(u.startswith("https://ghe.env.example/api/v3/repos/northpeak/payments-api/") for u in seen_urls), seen_urls


def test_jira_intake_adapter_verify_parse_and_enrich(monkeypatch):
    adapter = get_intake_adapter("jira")
    secret = "jira-intake-secret"
    conn = _conn("jira", "webhook_secret", secret, base_url="https://acme.atlassian.net",
                 email="bot@acme.test", project_key="KAN")
    payload = {
        "webhookEvent": "jira:issue_created",
        "user": {"displayName": "Priya", "accountId": "acc-1", "emailAddress": "priya@acme.test"},
        "issue": {
            "id": "10042",
            "key": "KAN-42",
            "self": "https://acme.atlassian.net/rest/api/3/issue/10042",
            "fields": {"summary": "Fix checkout", "description": "Details", "labels": ["boss"]},
        },
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()

    # HMAC header verification
    req_sig = RawRequest("POST", {"x-hub-signature": sig, "x-atlassian-webhook-identifier": "deliv-1"}, raw_bytes)
    assert adapter.verify(conn, req_sig, time.time()).scheme == "jira_sha256"
    assert adapter.dedupe_key(conn, req_sig) == "jira:deliv-1"

    # ?token= query param verification
    req_tok = RawRequest("POST", {}, raw_bytes, query={"token": secret})
    assert adapter.verify(conn, req_tok, time.time()).scheme == "jira_token"
    assert adapter.dedupe_key(conn, req_tok).startswith("jira:sha256:")

    # Refusals
    with pytest.raises(VerifyRefused, match="missing_signature"):
        adapter.verify(conn, RawRequest("POST", {}, raw_bytes), time.time())
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(conn, RawRequest("POST", {"x-hub-signature": "sha256=dead"}, raw_bytes), time.time())
    with pytest.raises(VerifyRefused, match="not_configured"):
        adapter.verify(_conn("jira", "webhook_secret", ""), req_sig, time.time())

    # Parse
    [ev] = adapter.parse(conn, _stored(req_sig, adapter, conn))
    assert ev.type == "com.atlassian.jira.issue.created"
    assert ev.subject == "KAN-42"
    assert ev.data["title"] == "Fix checkout"
    assert adapter.conversation_key(conn, ev) == "KAN-42"
    hint = adapter.resolve_person(conn, ev.bossactor)
    assert hint.email == "priya@acme.test" and hint.display == "Priya"

    # Enrich delegates to JiraTicketAdapter when api_token + base_url configured
    from boss_engine.connectors.tickets.contract import Ticket

    enriched_conn = IntakeConnection(
        "icn_jira", "p_1", "jira", config={"base_url": "https://acme.atlassian.net"},
        secret_resolver=lambda k: "api-tok" if k == "api_token" else secret,
    )
    monkeypatch.setattr(
        adapter._tickets,
        "get_ticket",
        lambda _tc, _ref: Ticket(
            provider="jira", key="KAN-42", id="10042", title="Fix checkout enriched",
            body_markdown="Full body", status="In Progress", status_category="doing",
            type="bug", labels=["boss", "p0"], url="https://acme.atlassian.net/browse/KAN-42",
        ),
    )
    ev = adapter.enrich(enriched_conn, ev)
    assert ev.data["title"] == "Fix checkout enriched"
    assert ev.data["labels"] == ["boss", "p0"]
    assert ev.data["ticket"]["status"] == "In Progress"


def test_plane_intake_adapter_verify_parse_and_enrich(monkeypatch):
    adapter = get_intake_adapter("plane")
    secret = "plane-intake-secret"
    conn = _conn("plane", "webhook_secret", secret, api_base_url="https://plane.example.com",
                 workspace_slug="ws", project_id="proj-uuid")
    payload = {
        "event": "issue",
        "action": "created",
        "data": {"id": "iss-99", "project": "proj-uuid", "name": "Ship P9b-4",
                 "description_stripped": "Body text"},
        "activity": {"actor": {"id": "u-1", "display_name": "Sean", "email": "sean@acme.test"}},
    }
    raw_bytes = json.dumps(payload).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()

    req = RawRequest("POST", {"x-plane-signature": sig, "x-plane-delivery": "pl-deliv-1"}, raw_bytes)
    assert adapter.verify(conn, req, time.time()).scheme == "plane_sha256"
    assert adapter.dedupe_key(conn, req) == "plane:pl-deliv-1"

    with pytest.raises(VerifyRefused, match="missing_signature"):
        adapter.verify(conn, RawRequest("POST", {}, raw_bytes), time.time())
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(conn, RawRequest("POST", {"x-plane-signature": "bad"}, raw_bytes), time.time())
    with pytest.raises(VerifyRefused, match="not_configured"):
        adapter.verify(_conn("plane", "webhook_secret", ""), req, time.time())

    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert ev.type == "so.plane.issue.created"
    assert ev.subject == "iss-99"
    assert ev.data["title"] == "Ship P9b-4"
    assert adapter.conversation_key(conn, ev) == "iss-99"
    hint = adapter.resolve_person(conn, ev.bossactor)
    assert hint.email == "sean@acme.test" and hint.display == "Sean"

    from boss_engine.connectors.tickets.contract import Ticket

    enriched_conn = IntakeConnection(
        "icn_plane", "p_1", "plane",
        config={"api_base_url": "https://plane.example.com", "workspace_slug": "ws", "project_id": "proj-uuid"},
        secret_resolver=lambda k: "plane-key" if k == "api_token" else secret,
    )
    monkeypatch.setattr(
        adapter._tickets,
        "get_ticket",
        lambda _tc, _ref: Ticket(
            provider="plane", key="BOSSAPP-1103", id="iss-99", title="Ship P9b-4",
            body_markdown="Enriched", status="In Progress", status_category="doing",
            type="task", labels=["boss"], url="https://plane.example.com/ws/projects/proj-uuid/issues/iss-99",
        ),
    )
    ev = adapter.enrich(enriched_conn, ev)
    assert ev.data["ticket"]["key"] == "BOSSAPP-1103"
    assert ev.data["labels"] == ["boss"]


def test_teams_intake_adapter_verify_loop_and_parse(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from boss_engine.connectors.intake.teams import adapter as teams_mod

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key()))
    pub_jwk["kid"] = "bf-test-kid"
    pub_jwk["use"] = "sig"
    pub_jwk["alg"] = "RS256"

    teams_mod.reset_jwks_cache()
    monkeypatch.setattr(
        teams_mod,
        "OIDC_FETCHER",
        lambda url: {"jwks_uri": "https://login.botframework.com/.well-known/keys"}
        if "openid" in url
        else {"keys": [pub_jwk]},
    )

    adapter = get_intake_adapter("teams")
    conn = IntakeConnection(
        "icn_teams", "p_1", "teams",
        config={"app_id": "app-teams-1", "tenant_id": "tenant-1", "chat_endpoint_id": "cep_teams_1"},
    )
    payload = {
        "type": "message",
        "id": "act-123",
        "timestamp": "2026-11-25T10:00:00Z",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "channelId": "msteams",
        "from": {"id": "29:user-1", "name": "Priya", "aadObjectId": "oid-123"},
        "conversation": {"id": "19:chan-1@thread.tacv2", "tenantId": "tenant-1"},
        "recipient": {"id": "28:app-teams-1", "name": "BOSS"},
        "text": "<at>BOSS</at> check status",
        "entities": [{"type": "mention", "mentioned": {"id": "28:app-teams-1"}, "text": "<at>BOSS</at>"}],
    }
    raw = json.dumps(payload).encode("utf-8")
    now = time.time()
    token = jwt.encode(
        {
            "iss": "https://api.botframework.com",
            "aud": "app-teams-1",
            "nbf": int(now) - 30,
            "exp": int(now) + 300,
            "serviceurl": "https://smba.trafficmanager.net/amer/",
        },
        priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        algorithm="RS256",
        headers={"kid": "bf-test-kid"},
    )
    req = RawRequest("POST", {"authorization": f"Bearer {token}", "content-type": "application/json"}, raw)
    assert adapter.verify(conn, req, now).scheme == "teams_oidc_jwt"
    assert adapter.dedupe_key(conn, req) == "teams:19:chan-1@thread.tacv2:act-123"

    # Two channels with the same millisecond activity id never collide.
    other_conv = dict(payload, conversation={"id": "19:chan-2@thread.tacv2", "tenantId": "tenant-1"})
    other_req = RawRequest("POST", req.headers, json.dumps(other_conv).encode("utf-8"))
    assert adapter.dedupe_key(conn, other_req) == "teams:19:chan-2@thread.tacv2:act-123"
    assert adapter.dedupe_key(conn, other_req) != adapter.dedupe_key(conn, req)

    # Disallowed OIDC metadata URL (non-HTTPS, non-loopback) is refused before fetch.
    bad_oidc_conn = IntakeConnection(
        "icn_teams_bad", "p_1", "teams",
        config={"app_id": "app-teams-1", "oidc_config_url": "http://evil.example/openid"},
    )
    with pytest.raises(VerifyRefused, match="not_configured"):
        adapter.verify(bad_oidc_conn, req, now)

    # Activity naming a serviceUrl when the token omits serviceurl/serviceUrl is refused.
    no_svc_token = jwt.encode(
        {"iss": "https://api.botframework.com", "aud": "app-teams-1",
         "nbf": int(now) - 30, "exp": int(now) + 300},
        priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        algorithm="RS256",
        headers={"kid": "bf-test-kid"},
    )
    with pytest.raises(VerifyRefused, match="bad_signature"):
        adapter.verify(
            conn,
            RawRequest("POST", {"authorization": f"Bearer {no_svc_token}"}, raw),
            now,
        )

    with pytest.raises(VerifyRefused, match="missing_signature"):
        adapter.verify(conn, RawRequest("POST", {}, raw), now)

    # TeamsChat host/scheme guard and path segment escaping.
    from boss_engine.services import chat_adapters as ca

    assert ca._teams_service_url_allowed("https://smba.trafficmanager.net/amer/", {}) is True
    assert ca._teams_service_url_allowed("http://smba.trafficmanager.net/amer/", {}) is False
    assert ca._teams_service_url_allowed("https://evil.example/", {}) is False
    assert ca._teams_service_url_allowed("http://127.0.0.1:9999", {"service_url": "http://127.0.0.1:9999"}) is True

    calls: list[tuple[str, str]] = []

    def fake_transport(method, url, headers, body, purpose):
        calls.append((method, url))
        if purpose == "chat.teams.token":
            return 200, {"access_token": "tok_test"}
        return 201, {"id": "act-out-1"}

    monkeypatch.setattr(ca, "TRANSPORT", fake_transport)
    receipt = ca.TeamsChat().send_with_config(
        "secret",
        {"op": "stream_start", "channel": "19:chan-1@thread.tacv2", "thread": "17:act/1", "text": "hi"},
        {"app_id": "app-1", "tenant_id": "ten-1", "service_url": "https://smba.trafficmanager.net/amer/"},
    )
    assert receipt["channel"] == "19:chan-1@thread.tacv2"
    assert receipt["thread"] == "17:act/1"
    assert any("19%3Achan-1%40thread.tacv2" in url for _, url in calls)

    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert ev.type == "com.microsoft.teams.message.mention"
    assert ev.bossactor.external_id == "29:user-1"
    assert ev.data["text"] == "check status"
    assert adapter.conversation_key(conn, ev) == "19:chan-1@thread.tacv2/act-123"
    assert adapter.is_self(conn, ev) is False

    echo = dict(payload, channelData={"boss": {"endpoint_id": "cep_teams_1", "run_id": "r1", "hops": 1}})
    echo_req = RawRequest("POST", {"authorization": f"Bearer {token}"}, json.dumps(echo).encode("utf-8"))
    [echo_ev] = adapter.parse(conn, _stored(echo_req, adapter, conn))
    assert adapter.is_self(conn, echo_ev) is True

    bot_echo = dict(payload, **{"from": {"id": "28:app-teams-1", "name": "BOSS", "role": "bot"}})
    bot_echo_req = RawRequest("POST", {"authorization": f"Bearer {token}"}, json.dumps(bot_echo).encode("utf-8"))
    [bot_echo_ev] = adapter.parse(conn, _stored(bot_echo_req, adapter, conn))
    assert adapter.is_self(conn, bot_echo_ev) is True


def test_servicenow_intake_adapter_verify_parse_and_self_loop():
    adapter = get_intake_adapter("servicenow")
    sec = "whsec_" + base64.b64encode(b"z" * 32).decode("ascii")
    conn = IntakeConnection(
        "icn_sn", "p_1", "servicenow",
        config={"instance_url": "https://dev99999.service-now.com", "service_account": "boss.bot"},
        secret_resolver=lambda k: sec if k == "signing_secret" else None,
    )
    body = {
        "table": "incident",
        "sys_id": "sn_1",
        "number": "INC0001",
        "short_description": "Disk full",
        "sys_updated_on": "2026-11-25 10:00:00",
        "sys_updated_by": "alex",
        "sys_mod_count": "1",
    }
    raw = json.dumps(body).encode("utf-8")
    msg_id = "msg_sn_1"
    ts = str(int(time.time()))
    from boss_engine.connectors.intake.webhook.adapter import standard_webhooks_signature

    req = RawRequest(
        "POST",
        {
            "webhook-id": msg_id,
            "webhook-timestamp": ts,
            "webhook-signature": standard_webhooks_signature(sec, msg_id, ts, raw),
        },
        raw,
    )
    assert adapter.verify(conn, req, time.time()).scheme == "servicenow_standard_webhooks"
    assert adapter.dedupe_key(conn, req) == "servicenow:msg_sn_1"
    [ev] = adapter.parse(conn, _stored(req, adapter, conn))
    assert ev.type == "com.servicenow.incident.updated"
    assert ev.data["head_key"] == "servicenow:incident:sn_1:1"
    assert adapter.conversation_key(conn, ev) == "servicenow:incident:sn_1"
    assert adapter.is_self(conn, ev) is False

    self_body = dict(body, sys_updated_by="boss.bot")
    [self_ev] = adapter.parse(conn, _stored(RawRequest("POST", req.headers, json.dumps(self_body).encode("utf-8")), adapter, conn))
    assert adapter.is_self(conn, self_ev) is True

