"""Always-on agents on a fresh project ask ONE plain card, never a flood (2026-11-07 replay).

THE CASE (laptop, always-on mode "route" from 10:54 to ~11:12 UTC). The new project `helpdesk`
was created at 10:57 and onboarded from paramg/helpdesk-ai-v3. Within twelve minutes, beside the
onboarding backlog the person had not looked at yet ("Add CI", tests for hot files), the squads
raised five cards of their own — "Start a run: Run the dependency_cve workflow to bump
body-parser past GHSA-27r3-…", "Open draft PR: …esbuild…", "Two consensus-approved attempts …
(act_01M6…)", "A2 gate has twice refused …", and "Open draft PR: … a standard CI workflow"
(onboarding's own "Add CI") — and the SRE squad asked "svc_0d4d… (production) still fails and
no fix has landed — take over?" on two projects whose apps were healthy: the alert was BOSS's own
evaluator running late (BossEvaluatorLag, a meta alert), it cleared 80 s later, and nothing had
said "production" — the squad defaulted to it.

THE RULES, AS A CLASS (ambient/asks.py, policy.grace, incident_squad.app_place/alert_recovery):
  * every always-on ask joins the project's ONE card (the rate guard counts cards);
  * questions are built from rows: package, app + environment, action — never a raw id;
  * BOSS refusing an action twice is a policy outcome on the ledger, not a question;
  * nothing is worked twice: what onboarding's backlog proposes is left to it;
  * a new project's grace: no acting before the person has seen the backlog;
  * BOSS's own self-check is never an app incident; "unknown" is never "still fails";
  * the cards raised before the fix are re-judged on the next tick (no DB edits by hand).
No test calls a model.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import incident_squad
from boss_engine.services.ambient import (adapters, asks, board, chief_of_staff as cos, collab,
                                          ledger, owners, policy, signals, squads)
from boss_engine.services.project_experience import onboarding as onb

from ._sre_squad_support import eng, project  # noqa: F401  (purges incidents and alerts too)
from ._squad_support import (CVE_ROLES, ENGINEER, LEAD, Launchers, ScriptedSquadModel, add_roles,
                             move, settings)

REPO = "paramg/helpdesk-ai-v3"
#: What a person must never read on a card: raw ids, a bare advisory code, gate jargon, keys.
RAW = re.compile(r"\b[a-z]{2,6}_[0-9A-Za-z]{6,}\b|GHSA(-[0-9a-z]{4}){3}|A2 gate|"
                 r"consensus-approved|\(production\)|module:|repo:|dep:")


def _pending(c, pid):
    return [dict(r) for r in c.execute(text(
        "SELECT id, question, recommendation, options FROM decisions WHERE project_id = :p "
        "AND status = 'pending' ORDER BY created_at"), {"p": pid}).mappings()]


def _tick(eng, pid, *, model=None, mode="route"):
    return cos.tick(eng, settings(), project_id=pid, sweep=False, mode=mode, squad_model=model,
                    launchers=Launchers())


def _onboarded(eng, pid):
    """The project as onboarding left it at 10:58: its dossier's signals, and the backlog it
    proposed (the Roadmap's open recommendations, written by onboarding's own code)."""
    with eng.begin() as c:
        add_roles(c, pid, CVE_ROLES + ("operations_engineer", "design_critic"))
        c.execute(text("INSERT INTO project_onboarding_runs (id, project_id, state) "
                       "VALUES (:i, :p, 'ready')"), {"i": f"onb_{uuid.uuid4().hex[:12]}",
                                                      "p": pid})
        run_id = c.execute(text("SELECT id FROM project_onboarding_runs WHERE project_id = :p"),
                           {"p": pid}).scalar()
        rec = lambda **kw: signals.record_signal(c, project_id=pid, **kw)["signal"]  # noqa: E731
        no_ci = rec(source="scan", kind="scan.no_ci", title=f"{REPO} has no CI",
                    subject_kind="repo", subject_id=REPO, severity="medium")
        hot = rec(source="scan", kind="scan.untested_hotspot",
                  title="Hot file without tests: web/src/pages/TicketDetailPage.jsx",
                  subject_kind="module", subject_id=f"{REPO}:web/src/pages/TicketDetailPage.jsx",
                  severity="medium")
        for name, version, adv in (("body-parser", "2.3.0", "GHSA-27r3-jxmh-p828"),
                                   ("esbuild", "0.21.5", "GHSA-67mh-4wv8-2f99")):
            rec(source="osv", kind="osv.vulnerable_dependency",
                title=f"{name} {version} has {adv}", subject_kind="dependency", subject_id=name,
                severity="high",
                evidence=[{"kind": "url", "ref": f"https://osv.dev/vulnerability/{adv}"}])
    onb._propose_on_roadmap(eng, pid, run_id, [  # noqa: SLF001 — onboarding's own writer
        onb._candidate("Add CI", source="roadmap", task_class="ci_setup",  # noqa: SLF001
                       severity="medium", kind="chore", ref=f"signal:{no_ci['id']}"),
        onb._candidate("Test TicketDetailPage.jsx", source="roadmap",  # noqa: SLF001
                       task_class="missing_tests", severity="medium", kind="chore",
                       ref=f"signal:{hot['id']}")])
    return {"no_ci": no_ci["id"], "hot": hot["id"], "run_id": run_id}


#: The CVE squad's turns: the lead proposes a draft PR, the engineer agrees (approved by the
#: squad) — but a fresh project's grace means a person decides, on the one card.
def _cve_model():
    draft = move("proposal", action="open_draft_pr", verify="CI green",
                 summary="Bump the package past the advisory (act_01M6TTJ4SEKSXWHRRXFBPBFSRV)")
    return ScriptedSquadModel({
        LEAD: [draft, draft],
        ENGINEER: [move("critique", position="agree", summary="Patch level, no API change")] * 2})


def test_a_fresh_project_with_the_agents_on_gets_one_card_in_plain_words(eng, project):
    pid = project
    refs = _onboarded(eng, pid)
    with eng.begin() as c:
        held = policy.grace(c, pid)
        assert held and held["open"] == 2, "a new project's grace until the backlog is seen"
        gate = policy.gate(c, pid, "security_patch", "open_draft_pr")
        assert gate["needs_person"] and not gate["allowed"]
        # Incidents never wait for a backlog review.
        assert policy.gate(c, pid, "incident_staging_mitigation", "staging_rollback")["allowed"]

    first = _tick(eng, pid, model=_cve_model())
    report = first["projects"][pid]
    assert report["grace"] is True
    # The two advisories were handed to the security squad; onboarding's own findings wait.
    assert sorted(i["outcome"] for i in report["items"]) == ["routed", "routed"]
    with eng.begin() as c:
        assert {s["id"] for s in signals.list_signals(c, project_id=pid, status=["new"])} == \
            {refs["no_ci"], refs["hot"]}
        [card] = _pending(c, pid)
        rec = card["recommendation"]
        entries = rec["batch"]
        assert rec["card"] == "ambient" and len(entries) == 2
        assert {e["ask_kind"] for e in entries} == {"squad"}
        words = sorted(e["words"] for e in entries)
        assert words == [
            "open a draft PR: body-parser has a known security hole (GHSA-27r3…) · upgrade past "
            "2.3.0",
            "open a draft PR: esbuild has a known security hole (GHSA-67mh…) · upgrade past "
            "0.21.5"]
        assert card["question"] == "Agents ask about 2 things: 2 security patches"
        for text_ in [card["question"]] + words + [e.get("detail") or "" for e in entries]:
            assert not RAW.search(text_), text_
        assert [o["key"] for o in card["options"]] == ["approve", "reject"]
        items = {i["id"]: i for i in board.list_items(c, pid, limit=50)
                 if i["task_class"] != "roadmap_item"}
        assert {i["lifecycle"] for i in items.values()} == {"needs_you"}
        assert {i["needs_you_decision_id"] for i in items.values()} == {card["id"]}

    # Ticks go on: nothing new is asked, nothing duplicates onboarding's backlog.
    again = _tick(eng, pid, model=_cve_model())
    assert again["projects"][pid]["items"] == []
    with eng.begin() as c:
        assert len(_pending(c, pid)) == 1
        # The person looks at the backlog (accepts "Add CI"): the grace lifts, onboarding's own
        # findings are triaged — and left to the backlog, never worked a second time.
        c.execute(text("UPDATE ambient_recommendations SET status = 'accepted', decided_at = now(), "
                       "decided_by = 'person:ceo' WHERE project_id = :p AND title = 'Add CI'"),
                  {"p": pid})
        assert policy.grace(c, pid) is None
    third = _tick(eng, pid, model=_cve_model())
    assert sorted(i["outcome"] for i in third["projects"][pid]["items"]) == \
        ["left_to_backlog", "left_to_backlog"]
    with eng.begin() as c:
        [card] = _pending(c, pid)
        assert len(card["recommendation"]["batch"]) == 2, "still the one card, nothing added"
        left = [i for i in board.list_items(c, pid, limit=50, lifecycle=["dismissed"])]
        assert sorted(i["outcome"] for i in left) == [
            "Left to the onboarding backlog: Add CI",
            "Left to the onboarding backlog: Test TicketDetailPage.jsx"]
        counts = dict(c.execute(text(
            "SELECT title, evidence_count FROM ambient_recommendations WHERE project_id = :p"),
            {"p": pid}).all())
        assert counts == {"Add CI": 2, "Test TicketDetailPage.jsx": 2}


def test_one_answer_on_the_card_reaches_every_squad(eng, project):
    pid = project
    _onboarded(eng, pid)
    _tick(eng, pid, model=_cve_model())
    from boss_engine.services.decisions import DecisionService

    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid})
        [card] = _pending(c, pid)
    with eng.begin() as c:
        DecisionService(settings()).transition(c, card["id"], action="approve", actor="ceo",
                                               note="Go ahead.")
    with eng.begin() as c:
        items = [i for i in board.list_items(c, pid, limit=50) if i["task_class"] != "roadmap_item"]
        assert {i["lifecycle"] for i in items} <= {"claimed", "proposed"}
        people = [d for i in items for d in ledger.list_decisions(c, pid, work_item_id=i["id"])
                  if d["decided_by"] == "person:ceo"]
        assert sorted(d["action"] for d in people) == ["open_draft_pr", "open_draft_pr"]


# ── the cards raised before the fix ──────────────────────────────────────────────────────────

def _incident(c, pid, *, service_name, env, state, meta):
    """The SRE squad's legacy "take over?" card on an alert, as the old code raised it."""
    svc = f"svc_{uuid.uuid4().hex[:20]}"
    c.execute(text("INSERT INTO monitored_services (id, project_id, name, environment) "
                   "VALUES (:i, :p, :n, :e)"), {"i": svc, "p": pid, "n": service_name, "e": env})
    alert = f"alrt_{uuid.uuid4().hex[:20]}"
    labels = {"layer": "boss", "severity": "critical", "boss_meta": "true"} if meta else {}
    c.execute(text(
        "INSERT INTO app_alerts (id, project_id, rule_kind, fingerprint, alertname, state, "
        "active_at, labels, service_id) VALUES (:i, :p, :k, :f, :n, :s, now(), "
        "CAST(:l AS JSONB), :svc)"),
        {"i": alert, "p": pid, "k": "meta" if meta else "error_rate", "f": f"fp_{alert}",
         "n": "BossEvaluatorLag" if meta else "AppErrorRate", "s": state,
         "l": json.dumps(labels), "svc": svc})
    sig = signals.record_signal(c, project_id=pid, source="alert", kind="alert.firing",
                                title="1 rule group(s) are evaluating late", subject_kind="app",
                                subject_id=svc, app=svc, severity="critical",
                                evidence=[{"kind": "alert", "ref": alert}])["signal"]
    item = board.open_item(c, settings(), project_id=pid, fingerprint=f"fp:{svc}",
                           title=sig["title"], task_class="incident_response",
                           subject_ref=f"app:{svc}", app=svc, signal_ids=[sig["id"]],
                           actor=cos.PARTY)["item"]
    held = board.claim(c, item["id"], owner="agent:sre")
    moved = board.needs_you(c, item["id"], actor="agent:sre", token=held["lease_token"],
                            question=f"{svc} (production) still fails and no fix has landed — "
                                     "take over?",
                            recommendation={"action": "incident_squad.mitigate",
                                            "mitigation": "escalate", "demo": False},
                            options=[{"key": "hold", "label": "I'll take it"}])
    return moved["needs_you_decision_id"], item["id"]


def _legacy_squad_card(c, pid, *, name, version, adv, recommendation, question, refused=0):
    sig = signals.record_signal(c, project_id=pid, source="osv",
                                kind="osv.vulnerable_dependency",
                                title=f"{name} {version} has {adv}", subject_kind="dependency",
                                subject_id=name, severity="high")["signal"]
    item = board.open_item(c, settings(), project_id=pid, fingerprint=f"fp:dep:{name}",
                           title=sig["title"], task_class="security_patch",
                           subject_ref=f"dep:{name}", signal_ids=[sig["id"]],
                           actor=cos.PARTY)["item"]
    seated = squads.assemble(c, item["id"])
    for _ in range(refused):
        collab.post(c, item["id"], kind="finding", sender="boss",
                    body={"summary": "Could not start dependency_cve: forbidden"})
    rec = dict(recommendation)
    if rec.get("proposal_id") == "$proposal":
        rec["proposal_id"] = collab.post(c, item["id"], kind="proposal", sender=LEAD, body={
            "action": rec["action"], "summary": f"Bump {name} past {adv}"})["id"]
    moved = board.needs_you(c, item["id"], actor=LEAD, token=seated["lease_token"],
                            question=question, recommendation=rec)
    return moved["needs_you_decision_id"], item["id"]


@pytest.mark.parametrize("mode", ["route", "off"])
def test_the_seven_cards_raised_before_the_fix_are_settled_on_the_next_tick(eng, project, mode):
    """The laptop's rows, as the old code raised them. The first tick after the install —
    whichever mode the always-on agents are in (the laptop is "off" now): the healthy-app
    "take over?" cards are withdrawn and their items closed; "BOSS refused twice" becomes a
    policy outcome; the CI draft PR is left to onboarding's "Add CI". The real security ask
    moves onto ONE card in plain words — or, with the agents off, is withdrawn and waits."""
    pid = project
    refs = _onboarded(eng, pid)
    with eng.begin() as c:
        c.execute(text("UPDATE ambient_recommendations SET decided_at = now(), status = 'open' "
                       "WHERE project_id = :p AND title = 'Test TicketDetailPage.jsx'"),
                  {"p": pid})   # the person has looked: no grace, the squads' cards stand
        meta_card, meta_item = _incident(c, pid, service_name="BOSS (self-check)", env="boss",
                                         state="resolved", meta=True)
        body, _ = _legacy_squad_card(
            c, pid, name="body-parser", version="2.3.0", adv="GHSA-27r3-jxmh-p828",
            question="Start a run: Run the dependency_cve workflow to bump body-parser past "
                     "GHSA-27r3-jxmh-p828?",
            recommendation={"action": "start_workflow", "recommended": "hold",
                            "proposal_id": "$proposal",
                            "why_a_person": "nobody checked it within two turns each"})
        undici, undici_item = _legacy_squad_card(
            c, pid, name="undici", version="6.21.1", adv="GHSA-2m5v-pch6-74hx", refused=2,
            question="A2 gate has twice refused to execute the squad-approved open_draft_pr for "
                     "GHSA-2m5v-pch6-74hx (forbidden on dependency_cve) — undici",
            recommendation={"action": "hold", "squad": "CVE squad"})
        # The CI draft PR the ops squad asked about: onboarding's "Add CI" signal.
        ci_item = board.open_item(c, settings(), project_id=pid, fingerprint="fp:ci",
                                  title=f"{REPO} has no CI", task_class="ci_setup",
                                  subject_ref=f"repo:{REPO}", signal_ids=[refs["no_ci"]],
                                  actor=cos.PARTY)["item"]
        held = board.claim(c, ci_item["id"], owner="agent:operations_engineer")
        ci_card = board.needs_you(
            c, ci_item["id"], actor="agent:operations_engineer", token=held["lease_token"],
            question="Open draft PR: Open a draft PR adding a standard CI workflow?",
            recommendation={"action": "open_draft_pr", "recommended": "hold",
                            "proposal_id": collab.post(c, ci_item["id"], kind="proposal",
                                                       sender="agent:operations_engineer",
                                                       body={"action": "open_draft_pr",
                                                             "summary": "Add CI"})["id"]}
        )["needs_you_decision_id"]
        assert len(_pending(c, pid)) == 4

    report = _tick(eng, pid, mode=mode)
    done = report["reconsidered"][pid] if mode == "off" else report["projects"][pid]
    assert "error" not in done, done
    changed = {r["decision_id"]: r for r in (done if mode == "off" else done["reconsidered"])}
    assert set(changed) == {meta_card, body, undici, ci_card}
    with eng.begin() as c:
        notes = dict(c.execute(text(
            "SELECT id, resolution_note FROM decisions WHERE id = ANY(:ids)"),
            {"ids": list(changed)}).all())
        assert "app is healthy" in notes[meta_card]
        assert "refused that action twice" in notes[undici]
        assert "onboarding backlog" in notes[ci_card]
        assert ("one card" if mode == "route" else "agents are off") in notes[body]
        assert board.get(c, meta_item)["lifecycle"] == "resolved"
        assert "BOSS's own monitor" in board.get(c, meta_item)["outcome"]
        assert board.get(c, undici_item)["lifecycle"] == "failed"
        [stop] = [d for d in ledger.list_decisions(c, pid, work_item_id=undici_item)
                  if d["decided_by"] == "policy:squad_launch"]
        assert stop["chosen"] == "stop" and not RAW.search(stop["question"])
        assert board.get(c, ci_item["id"])["lifecycle"] == "dismissed"
        for note in notes.values():
            assert not RAW.search(note), note
        if mode == "off":
            assert _pending(c, pid) == []
            return
        [card] = _pending(c, pid)
        assert card["recommendation"]["card"] == "ambient"
        # (the Decisions store writes the ellipsis as three dots)
        assert card["question"].replace("...", "…") == (
            "Start a fix run? body-parser has a known security hole (GHSA-27r3…) · upgrade past "
            "2.3.0")
        assert not RAW.search(card["question"])
    # The next tick changes nothing.
    again = _tick(eng, pid)
    assert again["projects"][pid]["reconsidered"] == []
    with eng.begin() as c:
        assert len(_pending(c, pid)) == 1


# ── the SRE squad: BOSS's own monitor, the environment, "still fails" ─────────────────────────

def _target(c, pid):
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
        "target_kind, target_ref, created_by) VALUES (:i, :p, 'HelpDesk AI Demo', 'acme/hd', "
        "'staging', 'compose', 'local', 'test')"), {"i": f"deptgt_{pid}", "p": pid})


def test_boss_own_self_check_never_reaches_the_inbox_and_an_app_is_named(eng, project):
    pid = project
    with eng.begin() as c:
        _, meta_item = _incident(c, pid, service_name="BOSS (self-check)", env="boss",
                                 state="firing", meta=True)
        _target(c, pid)
        c.execute(text(
            "INSERT INTO app_alerts (id, project_id, rule_kind, fingerprint, alertname, state, "
            "active_at, target_id, labels) VALUES (:i, :p, 'error_rate', :i, "
            "'AppErrorRate', 'firing', now(), :t, '{}'::jsonb)"),
            {"i": f"alrt_app_{pid}", "p": pid, "t": f"deptgt_{pid}"})
        seen = list(adapters.app_alerts(c, "epoch", project_id=pid))
        assert [s["app"] for s in seen] == ["HelpDesk AI Demo"], "the meta alert is left out"
        assert cos.rule_candidates({"kind": "alert.firing"}, {"meta": True})[0][0] == \
            "system_health"
        item = board.get(c, meta_item)
        assert incident_squad.app_place(c, pid, item) == ("BOSS (self-check)", "boss")
        assert incident_squad.app_place(
            c, pid, {"app": "HelpDesk AI Demo", "subject_ref": "app:HelpDesk AI Demo"}) == \
            ("HelpDesk AI Demo", "staging")
        # An app nothing names an environment for is never called "production".
        assert incident_squad.app_place(c, pid, {"app": "svc_0d4d03fd0f324ecbafdf"}) == \
            ("The app", "")
        assert incident_squad.recovery_question("The app", "", rolled_back=False,
                                                unconfirmed=False) == \
            "The app still fails and no fix has landed — take over?"


def test_a_healthy_app_never_gets_a_take_over_card(eng, project):
    """The live pass, replayed: no BOSS monitor (no incident row, no target in its context), the
    alert still firing for a minute. Before: "unknown" fell through to "Still failing" and a
    "(production) still fails — take over?" card. Now the alert is the evidence: firing →
    watching; cleared → recovered and closed. Nobody is asked."""
    pid = project
    with eng.begin() as c:
        _target(c, pid)
        alert = f"alrt_{uuid.uuid4().hex[:20]}"
        c.execute(text(
            "INSERT INTO app_alerts (id, project_id, rule_kind, fingerprint, alertname, state, "
            "active_at, target_id, labels) VALUES (:i, :p, 'error_rate', :i, "
            "'AppErrorRate', 'firing', now(), :t, '{}'::jsonb)"),
            {"i": alert, "p": pid, "t": f"deptgt_{pid}"})
        sig = signals.record_signal(c, project_id=pid, source="alert", kind="alert.firing",
                                    title="HelpDesk AI Demo 5xx", subject_kind="app",
                                    subject_id="HelpDesk AI Demo", app="HelpDesk AI Demo",
                                    severity="critical",
                                    evidence=[{"kind": "alert", "ref": alert}])["signal"]
        item = board.open_item(c, settings(), project_id=pid, fingerprint="fp:hd",
                               title=sig["title"], task_class="incident_response",
                               subject_ref="app:HelpDesk AI Demo", app="HelpDesk AI Demo",
                               signal_ids=[sig["id"]], actor=cos.PARTY)["item"]
        ctx = incident_squad.load_context(c, pid, item["id"])
        assert (ctx.app_name, ctx.env_known) == ("HelpDesk AI Demo", "staging")
        out = incident_squad.route_incident(c, project_id=pid, item_id=item["id"],
                                            effects=incident_squad.BossEffects(None))
        assert out["stage"] == "verifying"
        assert _pending(c, pid) == []
        c.execute(text("UPDATE app_alerts SET state = 'resolved' WHERE id = :a"), {"a": alert})
        moved = incident_squad.tick(c, project_id=pid, effects=incident_squad.BossEffects(None))
        assert [m["stage"] for m in moved] == ["resolved"]
        assert _pending(c, pid) == []
        verdicts = [d["chosen"] for d in ledger.list_decisions(c, pid, work_item_id=item["id"])
                    if d["question"] == incident_squad.Q_VERIFY]
        assert verdicts == ["Recovered"]


def test_words_never_carry_a_raw_id():
    assert incident_squad.recovery_question("HelpDesk AI Demo", "staging", rolled_back=False,
                                            unconfirmed=True).startswith(
        "HelpDesk AI Demo (staging): too little traffic to confirm")
    words = asks.scrub("Two decisions to open_draft_pr (act_01M6TTN3V17K59P0Q8WFMGSP50, "
                       "act_01M6TTPRRZJVF3GR7AFSC063T7) both failed")
    assert "act_" not in words and words.startswith("Two decisions to open_draft_pr")
