"""The front door on Postgres (P9a): verify → insert → commit → 2xx; duplicates, paused
connections and storms are 2xx; a bad signature is 401 and counted; 503 only when the database is
down. T3 exactly-once under redelivery, T4 "the API died between INSERT and COMMIT", T8 a storm on
one connection sheds beyond its hard cap and leaves the other connection untouched."""
from __future__ import annotations

import base64
import json
import random
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import exc as sa_exc

from boss_engine.connectors.intake.contract import RawRequest
from boss_engine.services import event_intake

from ._intake_support import (
    boss_v1_request,
    event_row,
    github_pr_opened,
    github_request,
    make_connection,
    make_project,
    pg_engine,
    purge,
    standard_webhook_request,
    token_of,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture()
def world():
    engine = pg_engine()
    pid = make_project(engine)
    event_intake.BUCKETS.reset()
    event_intake.REFUSALS.reset()
    event_intake._BEFORE_COMMIT = None
    try:
        yield engine, pid
    finally:
        event_intake._BEFORE_COMMIT = None
        event_intake.REFUSALS.reset()
        purge(engine, pid)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


def _rows(engine, connection_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, status, dedupe_key, duplicates, retry_count_seen, verify_scheme, headers, "
            "raw, raw_sha256 FROM intake_events WHERE connection_id = :c ORDER BY received_at, id"),
            {"c": connection_id}).mappings().all()]


# ---- connections ---------------------------------------------------------------------------------


def test_a_webhook_connection_mints_its_secret_once_and_never_shows_it_again(world):
    engine, pid = world
    out = make_connection(engine, pid, "webhook", name="Grafana alerts")
    assert out["secret_shown_once"] is True and out["secret"].startswith("whsec_")
    assert out["secrets_shown_once"] == {"signing_secret": out["secret"]}
    assert out["url_path"].startswith("/v1/intake/webhook/") and len(token_of(out["url_path"])) >= 32
    assert out["secret_fields"] == ["signing_secret"] and out["status"] == "active"
    assert out["limits"] == event_intake.DEFAULT_LIMITS
    with engine.begin() as c:
        row = event_intake.get_connection(c, out["id"])
        listed = event_intake.list_connections(c, pid)
        described = event_intake.describe_connection(c, row)
    everything = json.dumps([row, listed, described], default=str)
    assert out["secret"] not in everything          # a reference only, never the value
    assert "secret" not in described or described.get("secret") is None
    assert listed[0]["rules"] == 0 and listed[0]["backlog"] == 0
    with engine.begin() as c:
        rotated = event_intake.rotate_secret(c, project_id=pid, connection_id=out["id"], actor="ceo")
    assert rotated["secret"].startswith("whsec_") and rotated["secret"] != out["secret"]
    assert rotated["secret_fields"] == ["signing_secret"]           # the previous ref is hidden
    # the old secret keeps verifying for the rotation grace (a person re-points the sender),
    # the new one verifies at once; after the grace only the new one does
    token = token_of(out["url_path"])
    assert event_intake.receive(engine, "webhook", token,
                                standard_webhook_request(out["secret"], {"a": 1}, msg_id="o1"))[
        "http_status"] == 202
    assert event_intake.receive(engine, "webhook", token,
                                standard_webhook_request(rotated["secret"], {"a": 1}, msg_id="n1"))[
        "http_status"] == 202
    later = time.time() + event_intake.ROTATION_GRACE_SECONDS + 5
    assert event_intake.receive(engine, "webhook", token, standard_webhook_request(
        out["secret"], {"a": 1}, msg_id="o2", timestamp=int(later)), now=later)["http_status"] == 401
    assert event_intake.receive(engine, "webhook", token, standard_webhook_request(
        rotated["secret"], {"a": 1}, msg_id="n2", timestamp=int(later)), now=later)["http_status"] == 202


def test_a_github_connection_needs_the_sources_secret_and_rejects_unknown_kinds(world):
    engine, pid = world
    with pytest.raises(event_intake.IntakeServiceError, match="Webhook secret is required"):
        make_connection(engine, pid, "github")
    out = make_connection(engine, pid, "github", secrets={"webhook_secret": "gh-s"},
                          config={"repository": "northpeak/payments-api", "api_token": "ghp_x"})
    assert sorted(out["secret_fields"]) == ["api_token", "webhook_secret"]
    assert "api_token" not in out["config"] and out["config"]["repository"] == "northpeak/payments-api"
    with pytest.raises(event_intake.IntakeServiceError, match="unknown source kind"):
        make_connection(engine, pid, "teletype")
    with pytest.raises(event_intake.IntakeServiceError, match="supports modes"):
        with engine.begin() as c:
            event_intake.create_connection(c, project_id=pid, kind="webhook", actor="ceo", mode="poll")
    with engine.begin() as c:
        got = event_intake.test_connection(c, _settings(), project_id=pid, connection_id=out["id"])
    assert got["ok"] is True and got["checks"] == {"secret:api_token": "ok", "secret:webhook_secret": "ok"}
    with engine.begin() as c:
        assert event_intake.delete_connection(c, project_id=pid, connection_id=out["id"], actor="ceo")
        assert event_intake.get_connection(c, out["id"]) is None


def test_a_connection_config_hides_a_url_credential_and_a_masked_save_keeps_it(world):
    """BOSSAPP-1203: config and health are read views. A query token and a URL password are
    hidden; a service-account username stays. Saving the masked config back does not replace
    the stored secret, and a genuinely new value does."""
    engine, pid = world
    token = "fake-qs-token-1203"
    password = "fake-qs-pw-1203"
    user = "deploy-bot-prod-account-01"
    clone = f"https://{user}:{password}@git.example/r?access_token={token}"
    plain = f"https://{user}@git.example/r"
    out = make_connection(engine, pid, "webhook", config={
        "callback": clone, "mirror": plain, "label": "alerts",
        "hooks": {"url": clone}})
    shown = json.dumps(out["config"])
    assert token not in shown and password not in shown
    assert out["config"]["mirror"] == plain and out["config"]["label"] == "alerts"
    assert out["config"]["callback"] == f"https://{user}:***@git.example/r?access_token=***"
    assert out["config"]["hooks"]["url"] == out["config"]["callback"]
    with engine.begin() as c:
        c.execute(text("UPDATE intake_connections SET health = CAST(:h AS JSONB) WHERE id = :id"),
                  {"h": json.dumps({"last_error": clone}), "id": out["id"]})
        described = event_intake.describe_connection(c, event_intake.get_connection(c, out["id"]))
        event_intake.update_connection(c, project_id=pid, connection_id=out["id"], actor="ceo",
                                        config=out["config"])
        kept = event_intake.get_connection(c, out["id"])["config"]
    assert token not in json.dumps(described["health"]) and password not in json.dumps(described["health"])
    assert token in kept["callback"] and password in kept["callback"]
    assert kept["hooks"]["url"] == clone and kept["mirror"] == plain
    changed = "https://other.example/new"
    with engine.begin() as c:
        event_intake.update_connection(c, project_id=pid, connection_id=out["id"], actor="ceo",
                                        config={"callback": changed})
        row = event_intake.get_connection(c, out["id"])
    assert row["config"]["callback"] == changed
    assert row["config"]["hooks"]["url"] == clone


# ---- the front door ------------------------------------------------------------------------------


def test_verified_insert_commit_ack_then_duplicates_are_2xx_and_counted(world):
    engine, pid = world
    out = make_connection(engine, pid, "webhook")
    token, secret = token_of(out["url_path"]), out["secret"]
    req = standard_webhook_request(secret, {"alert": "err > 2%"}, msg_id="msg_1")
    got = event_intake.receive(engine, "webhook", token, req)
    assert got["http_status"] == 202 and got["outcome"] == "received" and got["event_id"]
    rows = _rows(engine, out["id"])
    assert len(rows) == 1 and rows[0]["status"] == "received"
    assert rows[0]["dedupe_key"] == "webhook:msg_1" and rows[0]["verify_scheme"] == "standard_webhooks"
    assert bytes(rows[0]["raw"]) == req.body and "webhook-signature" not in rows[0]["headers"]
    # the source redelivers (same id, new signature/timestamp): still 2xx, no second row
    again = standard_webhook_request(secret, {"alert": "err > 2%"}, msg_id="msg_1")
    got2 = event_intake.receive(engine, "webhook", token, again)
    assert got2["http_status"] == 202 and got2["outcome"] == "duplicate"
    rows = _rows(engine, out["id"])
    assert len(rows) == 1 and rows[0]["duplicates"] == 1
    # the BOSS v1 scheme on the same connection, with X-BOSS-Delivery as the key
    v1 = boss_v1_request(secret, {"alert": 2}, delivery="d-2")
    assert event_intake.receive(engine, "webhook", token, v1)["outcome"] == "received"
    assert [r["dedupe_key"] for r in _rows(engine, out["id"])] == ["webhook:msg_1", "webhook:d-2"]


def test_T3_exactly_once_under_github_redelivery(world):
    """Every delivery is replayed 1-3 times with the same X-GitHub-Delivery: rows == distinct ids,
    and the redeliveries are counted on the row, never stored twice."""
    engine, pid = world
    out = make_connection(engine, pid, "github", secrets={"webhook_secret": "gh-s"})
    token = token_of(out["url_path"])
    rnd = random.Random(7)
    deliveries = [f"d-{uuid.uuid4()}" for _ in range(40)]
    sent = 0
    for d in deliveries:
        for n in range(rnd.randint(1, 3)):
            req = github_request("gh-s", "pull_request", github_pr_opened(1), delivery=d)
            got = event_intake.receive(engine, "github", token, req)
            assert got["http_status"] == 202
            assert got["outcome"] == ("received" if n == 0 else "duplicate")
            sent += 1
    rows = _rows(engine, out["id"])
    assert len(rows) == len(deliveries)
    assert {r["dedupe_key"] for r in rows} == {f"github:{d}" for d in deliveries}
    assert sum(r["duplicates"] for r in rows) == sent - len(deliveries)


def test_T4_a_crash_between_insert_and_commit_loses_nothing_and_duplicates_nothing(world):
    """1,000 deliveries; ~10 % of the time "the API dies" after INSERT and before COMMIT (the hook
    raises, the transaction aborts, the source gets 503 and retries). Accepted (2xx) events lost:
    0; duplicate rows: 0; every delivery that was ever acked is exactly one row."""
    engine, pid = world
    out = make_connection(engine, pid, "webhook")
    token, secret = token_of(out["url_path"]), out["secret"]
    rnd = random.Random(1093)
    crashes = {"n": 0}

    def die_sometimes(event_id: str) -> None:
        if rnd.random() < 0.10:
            crashes["n"] += 1
            raise sa_exc.OperationalError("INSERT", None, ConnectionResetError("api died"))

    event_intake._BEFORE_COMMIT = die_sometimes
    acked: set[str] = set()
    for i in range(1000):
        msg_id = f"msg_{i}"
        for attempt in range(5):
            got = event_intake.receive(engine, "webhook", token,
                                       standard_webhook_request(secret, {"i": i}, msg_id=msg_id))
            if got["http_status"] == 202:
                acked.add(msg_id)
                break
            assert got["http_status"] == 503 and got["outcome"] == "unavailable"
        else:  # pragma: no cover
            pytest.fail(f"{msg_id} never accepted")
    event_intake._BEFORE_COMMIT = None
    rows = _rows(engine, out["id"])
    assert crashes["n"] > 50                                  # the fault really fired
    assert len(acked) == 1000
    assert {r["dedupe_key"] for r in rows} == {f"webhook:{m}" for m in acked}
    assert len(rows) == 1000                                   # zero duplicate rows
    assert all(r["duplicates"] == 0 for r in rows)             # a 503 was never a stored row


def test_bad_signatures_are_401_counted_in_memory_and_flushed_with_one_sampled_row(world):
    engine, pid = world
    out = make_connection(engine, pid, "webhook")
    token = token_of(out["url_path"])
    wrong = "whsec_" + base64.b64encode(b"w" * 32).decode()
    for n in range(5):
        got = event_intake.receive(engine, "webhook", token, standard_webhook_request(wrong, {"n": n}))
        assert got["http_status"] == 401 and got["body"]["reason"] == "bad_signature"
    assert _rows(engine, out["id"]) == []

    def health_and_samples():
        with engine.begin() as c:
            row = event_intake.get_connection(c, out["id"])
            n = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND event_type = "
                               "'intake.verify_refused'"), {"p": pid}).scalar()
        return row["health"], n

    # the first refusal flushed (one UPDATE + one sampled event); the other four are in memory
    health, samples = health_and_samples()
    assert health["refusals"] == 1 and health["last_refusal"] == "bad_signature" and samples == 1
    assert event_intake.REFUSALS.pending(out["id"]) == 4
    assert event_intake.flush_refusals(engine, connection_id=out["id"]) == 0   # not due yet
    assert event_intake.flush_refusals(engine, connection_id=out["id"], force=True) == 1
    health, samples = health_and_samples()
    assert health["refusals"] == 5 and samples == 2 and event_intake.REFUSALS.pending(out["id"]) == 0
    unsigned = RawRequest(method="POST", headers={"content-type": "application/json"}, body=b"{}")
    assert event_intake.receive(engine, "webhook", token, unsigned)["body"]["reason"] == "missing_signature"


def test_unknown_urls_handshakes_paused_connections_and_too_large_bodies(world):
    engine, pid = world
    out = make_connection(engine, pid, "github", secrets={"webhook_secret": "gh-s"})
    token = token_of(out["url_path"])
    assert event_intake.receive(engine, "github", "nope", github_request("gh-s", "push", {}))[
        "http_status"] == 404
    assert event_intake.receive(engine, "teletype", token, github_request("gh-s", "push", {}))[
        "http_status"] == 404
    # GitHub's ping is a handshake: 200, nothing stored
    ping = event_intake.receive(engine, "github", token, github_request("gh-s", "ping", {"zen": "x"}))
    assert ping["http_status"] == 200 and ping["outcome"] == "handshake"
    assert _rows(engine, out["id"]) == []
    # a paused connection: still 2xx (the source must not retry), nothing stored, counted
    with engine.begin() as c:
        event_intake.set_connection_status(c, project_id=pid, connection_id=out["id"], actor="ceo",
                                           status="paused", reason="maintenance")
    got = event_intake.receive(engine, "github", token,
                               github_request("gh-s", "pull_request", github_pr_opened(3)))
    assert got["http_status"] == 202 and got["outcome"] == "paused"
    assert _rows(engine, out["id"]) == []
    with engine.begin() as c:
        assert event_intake.get_connection(c, out["id"])["health"]["dropped_while_paused"] == 1
        event_intake.set_connection_status(c, project_id=pid, connection_id=out["id"], actor="ceo",
                                           status="active")
    assert event_intake.receive(engine, "github", token, github_request(
        "gh-s", "pull_request", github_pr_opened(3)))["outcome"] == "received"
    # OPTIONS without a handshake capability answer is 405; a 1 MB+ body is 413 before anything
    big = RawRequest(method="POST", headers={}, body=b"x" * (1024 * 1024 + 1))
    assert event_intake.receive(engine, "github", token, big)["http_status"] == 413


def test_the_cloudevents_options_handshake_is_answered_for_the_webhook_kind(world):
    engine, pid = world
    out = make_connection(engine, pid, "webhook", config={"allowed_origins": "grafana.example"})
    token = token_of(out["url_path"])
    got = event_intake.receive(engine, "webhook", token, RawRequest(
        method="OPTIONS", headers={"webhook-request-origin": "grafana.example"}, body=b""))
    assert got["http_status"] == 200 and got["headers"]["WebHook-Allowed-Origin"] == "grafana.example"
    assert event_intake.receive(engine, "webhook", token, RawRequest(
        method="OPTIONS", headers={"webhook-request-origin": "evil.example"}, body=b""))[
        "http_status"] == 403


def test_T8_a_storm_on_one_connection_is_shed_beyond_its_hard_cap_and_the_other_is_untouched(world):
    engine, pid = world
    stormy = make_connection(engine, pid, "webhook", name="alerts",
                             limits={"hard_cap_per_min": 60, "rps": 1000, "burst": 1000})
    calm = make_connection(engine, pid, "webhook", name="calm")
    s_tok, s_sec = token_of(stormy["url_path"]), stormy["secret"]
    c_tok, c_sec = token_of(calm["url_path"]), calm["secret"]
    now = (time.time() // 60) * 60 + 1          # inside one minute window, deterministic
    outcomes = []
    calm_latencies = []
    for i in range(150):
        got = event_intake.receive(engine, "webhook", s_tok,
                                   standard_webhook_request(s_sec, {"i": i}, msg_id=f"s{i}"),
                                   now=now + i * 0.01)
        assert got["http_status"] == 202                        # never a non-2xx to the source
        outcomes.append(got["outcome"])
        if i % 10 == 0:
            t0 = time.perf_counter()
            calm_got = event_intake.receive(engine, "webhook", c_tok,
                                            standard_webhook_request(c_sec, {"i": i}, msg_id=f"c{i}"),
                                            now=now + i * 0.01)
            calm_latencies.append(time.perf_counter() - t0)
            assert calm_got["outcome"] == "received"
    assert outcomes.count("received") == 60 and outcomes.count("shed") == 90
    stormy_rows = _rows(engine, stormy["id"])
    assert len(stormy_rows) == 150                             # kept for replay, marked shed
    assert sum(1 for r in stormy_rows if r["status"] == "shed") == 90
    assert all(r["raw"] is not None for r in stormy_rows)      # small bodies are kept
    big = standard_webhook_request(s_sec, {"pad": "x" * (event_intake.SHED_RAW_BYTES + 100)},
                                   msg_id="sbig")
    got = event_intake.receive(engine, "webhook", s_tok, big, now=now + 2)
    assert got["outcome"] == "shed"
    big_row = event_row(engine, got["event_id"])
    assert big_row["raw"] is None and big_row["raw_sha256"]     # shed + large: hash only
    assert all(r["status"] == "received" for r in _rows(engine, calm["id"]))
    assert max(calm_latencies) < 1.0                           # the calm connection is not delayed
    # the minute window rolls over: the stormy connection is admitted again
    later = event_intake.receive(engine, "webhook", s_tok,
                                 standard_webhook_request(s_sec, {"i": 999}, msg_id="s999"),
                                 now=now + 61)
    assert later["outcome"] == "received"
    with engine.begin() as c:
        counts = event_intake.status_counts(c, pid)
    assert counts["shed"] == 91 and counts["received"] == 60 + 15 + 1


def test_when_the_database_is_down_the_answer_is_503_and_the_source_retries():
    dead = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/nowhere", future=True)
    secret = "whsec_" + base64.b64encode(b"d" * 32).decode()
    got = event_intake.receive(dead, "webhook", "tok", standard_webhook_request(secret, {"a": 1}))
    assert got["http_status"] == 503 and got["outcome"] == "unavailable"


def test_retention_sweep_nulls_old_raw_bodies_and_deletes_expired_rows(world):
    engine, pid = world
    out = make_connection(engine, pid, "webhook")
    token, secret = token_of(out["url_path"]), out["secret"]
    ids = [event_intake.receive(engine, "webhook", token,
                                standard_webhook_request(secret, {"i": i}, msg_id=f"m{i}"))["event_id"]
           for i in range(3)]
    with engine.begin() as c:
        # a worked row is never touched by the sweep; finished rows age out
        c.execute(text("UPDATE intake_events SET raw_expires_at = now() - interval '1 day' "
                       "WHERE id = ANY(:ids)"), {"ids": ids})
        c.execute(text("UPDATE intake_events SET status = 'routed' WHERE id = :i"), {"i": ids[0]})
        c.execute(text("UPDATE intake_events SET status = 'ignored', expires_at = now() - interval "
                       "'1 day' WHERE id = :i"), {"i": ids[1]})
        swept = event_intake.sweep_retention(c)
    assert swept == {"raw_nulled": 2, "deleted": 1}      # ids[0] and ids[1]; ids[2] is still worked
    assert event_row(engine, ids[0])["raw"] is None and event_row(engine, ids[0])["raw_sha256"]
    assert event_row(engine, ids[2])["raw"] is not None       # still received: untouched
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM intake_events WHERE id = :i"),
                         {"i": ids[1]}).scalar() == 0
