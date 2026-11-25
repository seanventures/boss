"""The golden journeys' runner and the freeze counter (BOSSAPP-1284/1285), with fake journeys.

A journey is green ONLY when the Acceptance Agent's verdict module (acceptance/verdict.judge)
accepts every item against the record of calls the runner kept — never on the judge's word.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import pytest

from boss_engine.services.golden_journeys import definitions as d
from boss_engine.services.golden_journeys import fakes, results, runner

ROOT = Path(__file__).resolve().parents[2]


def journey(**over):
    raw = {
        "id": "J90", "title": "Fake build", "summary": "A fake build journey.",
        "ticket": "BOSSAPP-1284", "modes": ["scripted", "real"],
        "steps": [
            {"id": "start", "call": "POST /v1/things", "body": {"name": "{{name}}"},
             "expect_status": [201], "save": {"thing_id": "id"}},
            {"id": "work", "do": "drain"},
            {"id": "done", "call": "GET /v1/things/{{thing_id}}",
             "until": {"path": "status", "in": ["done", "failed"]}, "timeout_s": 0.3, "every_s": 0},
        ],
        "acceptance": [
            {"text": "The thing was created", "step": "start", "status": 201},
            {"text": "The thing finished as done", "step": "done", "path": "status", "equals": "done"},
            {"text": "Its checks all passed", "step": "done", "path": "checks[name=lint].ok", "equals": True},
        ],
    }
    raw.update(over)
    return d.parse(raw, "J90.json")


class FakeApi:
    """A tiny API: POST /v1/things makes one, GET answers `running` until drained."""

    mode = "scripted"

    def __init__(self, *, final="done", lint=True, create_status=201):
        self.things: dict[str, dict] = {}
        self.final, self.lint, self.create_status = final, lint, create_status
        self.calls: list[tuple[str, str, object]] = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/v1/things":
            tid = f"t{len(self.things) + 1}"
            self.things[tid] = {"id": tid, "name": (body or {}).get("name"), "status": "running",
                                "checks": [{"name": "lint", "ok": self.lint}]}
            return self.create_status, {"id": tid}
        m = re.fullmatch(r"/v1/things/(\w+)", path)
        if method == "GET" and m and m.group(1) in self.things:
            return 200, self.things[m.group(1)]
        return 404, {"detail": "not found"}

    def drain(self):
        for t in self.things.values():
            t["status"] = self.final
        return f"drained {len(self.things)}"


def run(j, api, **kw):
    return runner.run_journey(j, api, variables={"name": "x", **kw.pop("variables", {})},
                              sleep=lambda s: None, **kw)


# ── The runner and the verdict ───────────────────────────────────────────────────────────────

def test_a_journey_whose_items_all_hold_is_green_on_the_verdict():
    r = run(journey(), FakeApi())
    assert r["status"] == runner.GREEN, r["why"]
    assert r["verdict"] == "met"
    assert [i["status"] for i in r["items"]] == ["met"] * 3
    # every MET cites a call the runner recorded
    assert all(re.search(r"\bE\d+\b", i["evidence"]) for i in r["items"])


def test_an_item_that_does_not_hold_makes_it_red_and_says_which():
    r = run(journey(), FakeApi(lint=False))
    assert r["status"] == runner.RED
    assert r["verdict"] == "not_met"
    assert "AC-3" in r["why"] and "lint" in r["why"]


def test_a_failed_build_is_red():
    r = run(journey(), FakeApi(final="failed"))
    assert r["status"] == runner.RED and "AC-2" in r["why"]


def test_an_unexpected_status_stops_the_journey_and_says_where():
    r = run(journey(), FakeApi(create_status=500))
    assert r["status"] == runner.RED
    assert "start" in r["why"] and "HTTP 500" in r["why"]


def test_a_judge_cannot_make_a_journey_green_by_saying_so():
    """A MET quoting words that are not in the recorded call is NOT JUDGED by the verdict."""
    def liar(run_):
        return ("AC-1: MET — evidence: E1 (exit 0, HTTP 201) `HTTP 201`\n"
                "AC-2: MET — evidence: E3 (exit 0, HTTP 200) `\"status\": \"done\"`\n"
                "AC-3: MET — evidence: E3 (exit 0, HTTP 200) `\"ok\": true, \"name\": \"lint\"`\n"
                "ACCEPTANCE: MET")
    r = runner.run_journey(journey(), FakeApi(lint=False), variables={"name": "x"},
                           judge=liar, sleep=lambda s: None)
    assert r["status"] == runner.RED
    assert any(i["status"] == "unjudged" for i in r["items"])


def test_a_judge_citing_a_call_that_never_ran_is_not_believed():
    def ghost(run_):
        return "\n".join(f"AC-{n}: MET — evidence: E99 (exit 0, HTTP 200) `HTTP 200 ok`"
                         for n in (1, 2, 3)) + "\nACCEPTANCE: MET"
    r = runner.run_journey(journey(), FakeApi(), variables={"name": "x"}, judge=ghost,
                           sleep=lambda s: None)
    assert r["status"] == runner.RED


def test_a_pending_journey_is_pending_and_never_called():
    api = FakeApi()
    r = run(journey(pending={"waits_for": "PR #1", "why": "not on main"}), api)
    assert r["status"] == runner.PENDING and "PR #1" in r["why"]
    assert api.calls == []


def test_a_journey_not_runnable_on_this_engine_is_not_run():
    api = FakeApi()
    r = run(journey(modes=["real"]), api)
    assert r["status"] == runner.NOT_RUN and api.calls == []


def test_a_missing_variable_is_named_before_anything_runs():
    api = FakeApi()
    r = runner.run_journey(journey(), api, variables={}, sleep=lambda s: None)
    assert r["status"] == runner.RED and "name" in r["why"]
    assert api.calls == []


def test_a_poll_that_never_settles_times_out_red():
    class Stuck(FakeApi):
        def drain(self):
            return "nothing"
    j = journey()
    r = runner.run_journey(j, Stuck(), variables={"name": "x"}, sleep=lambda s: None)
    assert r["status"] == runner.RED and "still waiting" in r["why"]


def test_run_all_keeps_going_after_a_journey_raises():
    class Boom(FakeApi):
        def call(self, *a, **k):
            raise RuntimeError("boom")
    rows = runner.run_all([journey(), journey(id="J91")], Boom(), variables={"name": "x"},
                          sleep=lambda s: None)
    assert [r["journey_id"] for r in rows] == ["J90", "J91"]
    assert all(r["status"] == runner.RED for r in rows)


def test_save_takes_the_first_path_the_answer_has():
    class Import(FakeApi):
        def call(self, method, path, body=None):
            if path == "/v1/things":
                return 201, {"already_in_boss": [{"id": "t9"}]}
            return 200, {"status": "done", "checks": [{"name": "lint", "ok": True}]}
    j = journey(steps=[
        {"id": "start", "call": "POST /v1/things", "expect_status": [201],
         "save": {"thing_id": "imported[0].id|already_in_boss[0].id"}},
        {"id": "done", "call": "GET /v1/things/{{thing_id}}"}])
    r = runner.run_journey(j, Import(), sleep=lambda s: None)
    assert r["status"] == runner.GREEN, r["why"]
    assert any(c["url"] == "/v1/things/t9" for c in r["calls"])


# ── A key the runner holds never reaches the record ──────────────────────────────────────────

def test_a_fake_mcp_key_is_sent_but_never_recorded():
    fake = fakes.start("mcp_http")
    try:
        sent = []

        class Echo(FakeApi):
            def call(self, method, path, body=None):
                sent.append(body)
                return 201, {"id": "s1", "url": (body or {}).get("url")}

        j = d.parse({
            "id": "J92", "title": "Key", "summary": "A key journey.", "ticket": "BOSSAPP-1479",
            "modes": ["scripted"], "needs": {"fakes": ["mcp_http"]},
            "steps": [{"id": "add", "call": "POST /v1/mcp/servers",
                       "body": {"url": "{{fake.mcp_http.url}}",
                                "auth": {"type": "bearer", "secret": "{{secret.mcp_http.key}}"}}}],
            "acceptance": [{"text": "The server was added", "step": "add", "status": 201}]}, "J92")
        r = runner.run_journey(j, Echo(), fakes={"mcp_http": fake}, sleep=lambda s: None)
        assert r["status"] == runner.GREEN, r["why"]
        assert sent[0]["auth"]["secret"] == fake.key           # BOSS was given the key
        assert fake.key not in json.dumps(r, default=str)        # the record never holds it
        assert "[held]" in json.dumps(r["calls"])
    finally:
        fake.stop()


def test_the_fake_mcp_server_refuses_without_its_key_and_records_tool_calls():
    fake = fakes.start("mcp_http")
    try:
        def post(body, key=None):
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            req = urllib.request.Request(fake.url, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, None

        assert post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
        assert post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "wrong")[0] == 401
        status, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, fake.key)
        assert status == 200 and body["result"]["tools"][0]["name"] == "ledger_lookup"
        status, body = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "ledger_lookup", "arguments": {"account": "ACC-1"}}}, fake.key)
        assert status == 200 and "1250.00" in body["result"]["content"][0]["text"]
        log = fake.log()
        assert log["refused"] == 2 and log["calls"][0]["arguments"] == {"account": "ACC-1"}
        assert fake.key not in json.dumps(log)
    finally:
        fake.stop()


# ── The shipped definitions ─────────────────────────────────────────────────────────────────

def test_every_plan_journey_is_defined_and_the_mcp_journey_too():
    ids = [j.id for j in d.load_all()]
    assert ids[:9] == [f"J{n}" for n in range(1, 10)]
    assert "J10" in ids


def test_at_least_two_journeys_run_on_the_scripted_engine():
    assert len([j for j in d.load_all() if j.runnable_in("scripted")]) >= 2


def test_every_journey_on_an_open_pr_is_pending_and_says_on_what():
    by_id = {j.id: j for j in d.load_all()}
    for jid, waits in (("J2", "#330"), ("J8", "#329"), ("J9", "#330"), ("J10", "#331"),
                       ("J4", "1363")):
        assert by_id[jid].is_pending and waits in by_id[jid].pending["waits_for"], jid


_MATRIX = ROOT / "tests" / "identity" / "matrix.yaml"


def _routes() -> list[tuple[str, re.Pattern]]:
    out = []
    for line in _MATRIX.read_text().splitlines():
        m = re.match(r'^"(GET|POST|PUT|PATCH|DELETE) (\S+)":', line)
        if m:
            out.append((m.group(1), re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", m.group(2)) + "$")))
    return out


def test_every_call_of_a_runnable_journey_is_a_route_the_api_declares():
    """A renamed route fails here, not on the first morning's run."""
    routes = _routes()
    assert routes, "tests/identity/matrix.yaml lists no routes"
    for j in d.load_all():
        if j.is_pending:
            continue
        for s in j.steps:
            if not s.call:
                continue
            path = re.sub(r"\{\{[^}]+\}\}", "x", s.path)
            assert any(m == s.method and rx.match(path) for m, rx in routes), \
                f"{j.id} step {s.id}: {s.call} is not a route the API declares"


def test_every_acceptance_item_can_be_met_on_verified_evidence():
    """No item's words trip the verdict's stack or dependency readers: given a verified call
    for each item, the verdict module accepts every journey's list."""
    from boss_engine.services.acceptance import verdict
    from boss_engine.services.acceptance.session import Command, CommandRecord

    for j in d.load_all():
        rec = CommandRecord()
        lines = []
        for item in j.acceptance:
            cid = rec.next_id()
            rec.add(Command(id=cid, cmd=f"GET /proof/{item.n}", kind="http", exit=0, status=200,
                            stdout=f"HTTP 200\nproof of item {item.n}", plain=True,
                            tree_dirty=False))
            lines.append(f"AC-{item.n}: MET — evidence: {cid} (exit 0, HTTP 200) "
                         f"`proof of item {item.n}`")
        reply = "\n".join(lines) + "\nACCEPTANCE: MET"
        record = verdict.judge([i.as_contract() for i in j.acceptance], reply, {},
                               root=str(ROOT / "engine" / "boss_engine" / "services" / "golden_journeys" / "defs"),
                               exec_log=rec)
        assert record["verdict"] == "met", (j.id, record.get("why"), record.get("items"))


@pytest.mark.parametrize("bad, words", [
    ({"id": "X1"}, "id must look like J1"),
    ({"modes": ["fast"]}, "modes"),
    ({"steps": [{"id": "a", "call": "FETCH /x"}]}, "METHOD /path"),
    ({"steps": [{"id": "a", "call": "GET /x", "do": "drain"}]}, "exactly one of call or do"),
    ({"acceptance": []}, "at least one"),
    ({"acceptance": [{"text": "t", "step": "nope", "status": 200}]}, "not a step here"),
    ({"pending": {"why": "x"}}, "waits_for"),
])
def test_a_bad_definition_is_refused_naming_the_field(bad, words):
    with pytest.raises(d.DefinitionError, match=re.escape(words)):
        journey(**bad)


# ── The counter ──────────────────────────────────────────────────────────────────────────────

TODAY = date(2026, 11, 25)


def rows_for(days: dict[int, dict[str, str]], host="laptop", mode="real"):
    """{days_ago: {journey: status}} as stored rows."""
    return [{"host": host, "day": TODAY - timedelta(days=k), "journey_id": j, "status": s,
             "mode": mode} for k, js in days.items() for j, s in js.items()]


ALL_GREEN = {"J1": "green", "J2": "green"}


def streak(rows, hosts=("laptop",), expected=("J1", "J2"), now=TODAY):
    return results.green_streak(results.day_states(rows, expected, hosts), now)


def test_consecutive_green_days_count_up_to_today():
    assert streak(rows_for({0: ALL_GREEN, 1: ALL_GREEN, 2: ALL_GREEN})) == 3


def test_a_red_journey_resets_the_counter():
    assert streak(rows_for({0: ALL_GREEN, 1: {"J1": "green", "J2": "red"}, 2: ALL_GREEN})) == 1


def test_a_journey_that_did_not_run_makes_the_day_red():
    assert streak(rows_for({0: ALL_GREEN, 1: {"J1": "green"}, 2: ALL_GREEN})) == 1


def test_a_pending_journey_keeps_every_day_red():
    assert streak(rows_for({0: {"J1": "green", "J2": "pending"}})) == 0


def test_today_not_yet_run_counts_from_yesterday():
    assert streak(rows_for({1: ALL_GREEN, 2: ALL_GREEN})) == 2


def test_a_gap_day_breaks_the_streak():
    assert streak(rows_for({0: ALL_GREEN, 2: ALL_GREEN, 3: ALL_GREEN})) == 1


def test_a_day_is_green_only_on_every_counted_host():
    rows = rows_for({0: ALL_GREEN, 1: ALL_GREEN}) + rows_for({0: ALL_GREEN}, host="boss-1")
    assert streak(rows, hosts=("laptop", "boss-1")) == 1
    states = results.day_states(rows, ("J1", "J2"), ("laptop", "boss-1"))
    assert "J1 not run on boss-1" in states[TODAY - timedelta(days=1)]["short"]


def test_scripted_rows_never_count():
    assert streak(rows_for({0: ALL_GREEN}, mode="scripted")) == 0


def test_five_green_days_lift_the_freeze():
    assert results.FREEZE_DAYS == 5
    assert streak(rows_for({k: ALL_GREEN for k in range(6)})) == 6


def test_counted_hosts_come_from_the_setting_first(monkeypatch):
    monkeypatch.setenv("BOSS_JOURNEY_HOSTS", "laptop, boss-1")
    assert results.counted_hosts(["x"]) == ["laptop", "boss-1"]
    monkeypatch.delenv("BOSS_JOURNEY_HOSTS")
    assert results.counted_hosts(["b", "a", "a"]) == ["a", "b"]
