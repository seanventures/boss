"""Govern › Where people decide, through the real API.

The page is only real if the list it draws is the list the decision flow enforces:
  * the cards count real decisions, and a card's Save is stricter-only (422 when looser);
  * a quiet window HOLDS an approval of an entry on the list, and lifting it releases it;
  * a named decider answers first; the backup only after the wait;
  * "Change a rule in plain words" goes through Helm's one structured turn, shows the 30-day
    replay from real decisions, and Apply asks for the passkey (changing the list is on it);
  * the Spend card's cap clamps Full auto.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import AuthContext, AuthMethod, SessionRef
from boss_engine.services import autonomy, decide_list
from boss_engine.services.project_experience import understanding

PROJECT = "boss-dogfood"
DAYS = decide_list.DAYS


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean_rules(engine):
    def wipe():
        with engine.begin() as c:
            c.execute(text("DELETE FROM decide_rules"))
            c.execute(text("DELETE FROM decide_rule_changes"))
    wipe()
    yield
    wipe()


def _decision(engine, question: str, *, rec: dict | None = None, ago: timedelta = timedelta(),
              status: str = "pending", cost: dict | None = None) -> str:
    did = f"dec_wpd_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, risk_tier, "
            "default_action, status, cost_summary, created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), "
            "1, 'hold', :s, CAST(:c AS JSONB), now() - make_interval(secs => :a))"),
            {"i": did, "p": PROJECT, "q": question, "r": json.dumps(rec or {"action": "governance_review"}),
             "s": status, "c": json.dumps(cost or {}), "a": ago.total_seconds()})
    return did


def _window_around_now() -> dict:
    now = datetime.now(timezone.utc)
    return {"from_day": DAYS[(now.weekday() - 1) % 7], "from_time": "00:00",
            "to_day": DAYS[(now.weekday() + 1) % 7], "to_time": "23:00", "tz": "UTC"}


def _put(client, kind: str, rule: dict, scope: str = "install"):
    return client.put(f"/v1/decide/rules/{kind}", json={"project_id": PROJECT, "scope": scope,
                                                         "rule": rule, "actor": "ceo"})


def test_the_page_reads_the_list_and_counts_real_decisions(client, engine):
    before = {c["key"]: c.get("count_24h") for c in client.get(
        f"/v1/decide?project_id={PROJECT}").json()["cards"]}
    _decision(engine, "Deploy HelpDesk v1.4 to production?")
    _decision(engine, "Deploy HelpDesk to staging")                 # off the list
    _decision(engine, "Merge?", rec={"action": "review_pull_request"})
    got = client.get(f"/v1/decide?project_id={PROJECT}")
    assert got.status_code == 200, got.text
    body = got.json()
    cards = {c["key"]: c for c in body["cards"]}
    assert list(cards) == [b.key for b in decide_list.LIST]
    assert cards["prod_deploy"]["count_24h"] == before["prod_deploy"] + 1
    assert cards["merge"]["count_24h"] == before["merge"] + 1
    assert cards["change_list"]["fixed"] and cards["change_list"]["step_up"] == "passkey"
    assert cards["secrets"]["step_up"] == "passkey" and cards["delete"]["step_up"] == "typed_name"
    assert cards["prod_deploy"]["decider"]["kind"] == "seat"
    assert cards["prod_deploy"]["trail"][0]["type"] == "decision"
    summary = body["activity"]
    assert summary["waiting"] >= 3 and summary["oldest"]["id"]
    assert {"handled_by_agents", "asked_a_person"} <= set(summary)


def test_a_card_saves_stricter_and_refuses_looser(client, engine):
    assert _put(client, "spend", {"cap_usd": 200}).status_code == 200
    looser = _put(client, "spend", {"cap_usd": 900})
    assert looser.status_code == 422 and looser.json()["detail"]["error"] == "stricter_only"
    for kind, rule in (("prod_deploy", {"always_person": False}), ("merge", {"full_auto": True}),
                       ("secrets", {"step_up": ""}), ("change_list", {})):
        assert _put(client, kind, rule).status_code == 422, kind
    # Per project: a project adds to the install's rule, never takes away.
    assert _put(client, "spend", {"cap_usd": 300}, scope=PROJECT).status_code == 422
    assert _put(client, "spend", {"cap_usd": 100}, scope=PROJECT).status_code == 200
    assert _put(client, "secrets", {}, scope=PROJECT).status_code == 422   # install only
    with engine.begin() as c:
        rows = c.execute(text("SELECT kind, scope, via, actor FROM decide_rule_changes "
                              "ORDER BY created_at")).all()
    assert [(r[0], r[1], r[2]) for r in rows] == [("spend", "install", "card"),
                                                  ("spend", PROJECT, "card")]
    with engine.begin() as c:
        # The Spend card clamps Full auto: below the cap, FinOps decides; above it, a person.
        assert autonomy.load_policy(c, PROJECT).threshold_usd == 100.0


def test_a_quiet_window_holds_the_approval_and_lifting_it_releases_it(client, engine):
    did = _decision(engine, "Deploy HelpDesk v1.4 to production?")
    assert _put(client, "prod_deploy", {"quiet": [_window_around_now()]}).status_code == 200
    held = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo", "note": "ship"})
    assert held.status_code == 409, held.text
    assert "quiet window" in held.json()["detail"]
    # A decision off the list is not held by the window.
    other = _decision(engine, "Deploy HelpDesk to staging")
    assert client.post(f"/v1/decisions/{other}/approve",
                       json={"actor": "ceo", "note": "ok"}).status_code == 200
    assert _put(client, "prod_deploy", {"quiet": []}).status_code == 200
    ok = client.post(f"/v1/decisions/{did}/approve", json={"actor": "ceo", "note": "ship"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "approved"


def test_the_named_decider_answers_first_and_the_backup_after_the_wait(client, engine):
    rule = {"decider": {"person": "release_manager", "name": "Rel"},
            "backup": {"person": "ceo", "name": "Owner"}, "backup_after_minutes": 60}
    assert _put(client, "merge", rule).status_code == 200
    fresh = _decision(engine, "Merge PR #3?", rec={"action": "review_pull_request"})
    early = client.post(f"/v1/decisions/{fresh}/approve", json={"actor": "ceo", "note": "go"})
    assert early.status_code == 409, early.text
    assert "backup" in early.json()["detail"] and "60 min" in early.json()["detail"]
    old = _decision(engine, "Merge PR #4?", rec={"action": "review_pull_request"},
                    ago=timedelta(hours=2))
    late = client.post(f"/v1/decisions/{old}/approve", json={"actor": "ceo", "note": "go"})
    assert late.status_code == 200, late.text
    first = client.post(f"/v1/decisions/{fresh}/approve",
                        json={"actor": "release_manager", "note": "go"})
    assert first.status_code == 200, first.text
    # Anyone else is told who decides.
    third = _decision(engine, "Merge PR #5?", rec={"action": "review_pull_request"})
    other = client.post(f"/v1/decisions/{third}/approve", json={"actor": "cto", "note": "go"})
    assert other.status_code in (403, 409), other.text


def test_the_30_day_replay_counts_real_decisions(client, engine):
    w = {"from_day": "mon", "from_time": "00:00", "to_day": "sun", "to_time": "23:59", "tz": "UTC"}
    base = client.post("/v1/decide/replay", json={"project_id": PROJECT, "edits": [
        {"kind": "prod_deploy", "field": "quiet", "value": w}]}).json()["replay"][0]["n"]
    _decision(engine, "Deploy v2 to production?", ago=timedelta(days=3))
    _decision(engine, "Deploy v3 to production?", ago=timedelta(days=40))   # outside 30 days
    got = client.post("/v1/decide/replay", json={"project_id": PROJECT, "edits": [
        {"kind": "prod_deploy", "field": "quiet", "value": w},
        {"kind": "spend", "field": "decider", "value": {"person": "usr_x", "name": "Sarah"}},
        {"kind": "spend", "field": "cap_usd", "value": 200}]})
    assert got.status_code == 200, got.text
    replay = got.json()["replay"]
    assert replay[0]["n"] == base + 1 and "would have waited" in replay[0]["words"]
    assert "would have gone to Sarah" in replay[1]["words"]
    assert replay[2]["field"] == "cap_usd"


# ── Change a rule in plain words: Helm's structured turn → Rule change → Apply · passkey ────

def _helm_says(monkeypatch, arguments: dict, seen: list):
    def turn(connection, settings, project_id, **kw):
        seen.append(kw)
        assert kw["subject"] == "rule_change"
        problem = kw["check"]({"arguments": arguments}, "")
        if problem:
            return SimpleNamespace(decided=False, action=None, arguments={}, refused=problem,
                                   error=None, by={}, status="refused")
        return SimpleNamespace(decided=True, action="brief", arguments=arguments, refused=None,
                               error=None, by={"engine": "scripted"}, status="decided")
    monkeypatch.setattr(understanding, "turn", turn)


QUIET = {"from_day": "fri", "from_time": "15:00", "to_day": "mon", "to_time": "08:00",
         "tz": "Europe/London"}


def test_words_become_a_rule_change_through_the_one_structured_turn(client, engine, monkeypatch):
    seen: list = []
    _helm_says(monkeypatch, {
        "edits": [{"kind": "prod_deploy", "field": "quiet", "value": QUIET,
                   "says": "Nothing goes to prod from Friday 15:00 to Monday 08:00."},
                  {"kind": "spend", "field": "cap_usd", "value": 200, "says": "Cap is $200."}],
        "already_allowed": [{"says": "Merging into staging/* is not the default branch.",
                             "kind": "merge"}],
        "reply": "Two changes, one already allowed."}, seen)
    got = client.post("/v1/decide/understand", json={
        "project_id": PROJECT, "tz": "Europe/London", "actor": "ceo",
        "words": "Nothing goes to prod on Fridays after 3pm. Cap spend at $200. Let agents "
                 "merge into staging branches."})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "understood" and len(body["edits"]) == 2
    assert body["already_allowed"][0]["kind"] == "merge"
    assert [r["kind"] for r in body["replay"]] == ["prod_deploy", "spend"]
    # Helm was shown the list itself (the one source), the people and the person's time zone.
    ctx = seen[0]["context"]
    assert [e["key"] for e in ctx["list"]] == [b.key for b in decide_list.LIST]
    assert ctx["tz"] == "Europe/London"
    # Nothing was written by reading.
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decide_rules")).scalar() == 0


def test_a_looser_edit_from_the_model_is_refused_by_the_check(client, monkeypatch):
    seen: list = []
    _helm_says(monkeypatch, {"edits": [{"kind": "spend", "field": "cap_usd", "value": 5000,
                                        "says": "Raise the cap"}], "reply": "ok"}, seen)
    got = client.post("/v1/decide/understand", json={"project_id": PROJECT, "words": "raise it",
                                                     "actor": "ceo"})
    assert got.status_code == 200 and got.json()["status"] == "no_model"


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("decide-owner")
    world.bind(person, "owner")
    return person


def _stale(person) -> AuthContext:
    now = datetime.now(timezone.utc)
    session = SessionRef(id=f"ses_{uuid.uuid4().hex[:10]}", mfa_at=now - timedelta(hours=3),
                         auth_methods=("password", "totp"), idle_expires_at=now + timedelta(hours=1),
                         absolute_expires_at=now + timedelta(days=1))
    return AuthContext(person, AuthMethod.SESSION, session=session)


def test_apply_asks_for_the_passkey_and_writes_all_or_nothing(client, engine, world, owner):
    edits = [{"kind": "prod_deploy", "field": "quiet", "value": QUIET, "scope": "install"},
             {"kind": "spend", "field": "cap_usd", "value": 200, "scope": "install"}]
    body = {"project_id": world.project_a, "edits": edits, "words": "the words"}
    with world.acting_as(_stale(owner)):
        stale = client.post("/v1/decide/apply", json=body)
    assert stale.status_code == 401 and stale.json().get("error") == "step_up_required", stale.text
    operator = world.person(f"decide-operator-{uuid.uuid4().hex[:4]}")
    world.bind(operator, "operator")
    with world.acting_as(world.ctx(operator)):
        assert client.post("/v1/decide/apply", json=body).status_code == 403
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decide_rules")).scalar() == 0
    # One looser edit refuses the whole card: nothing is written.
    with world.acting_as(world.ctx(owner)):
        looser = client.post("/v1/decide/apply", json={**body, "edits": edits + [
            {"kind": "spend", "field": "cap_usd", "value": 9000}]})
    assert looser.status_code == 422, looser.text
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decide_rules")).scalar() == 0
    with world.acting_as(world.ctx(owner)):
        ok = client.post("/v1/decide/apply", json=body)
    assert ok.status_code == 200, ok.text
    with engine.begin() as c:
        rows = dict(c.execute(text("SELECT kind, rule FROM decide_rules WHERE scope = 'install'")).all())
        via = c.execute(text("SELECT DISTINCT via, actor FROM decide_rule_changes")).all()
    assert rows["prod_deploy"]["quiet"][0]["from_day"] == "fri"
    assert rows["spend"]["cap_usd"] == 200
    assert via == [("words", owner.id)]
