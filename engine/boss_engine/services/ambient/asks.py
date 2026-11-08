"""THE ONE WAY an always-on agent asks a person (2026-11-07, the second always-on noise fix).

Every person-facing ask an always-on agent makes — the Chief of Staff's suggestions, a squad's
proposal that needs a person, a squad that is stuck, the SRE squad's "not seen to recover" —
goes through `ask` here, never straight to `board.needs_you`. The rules, as a class:

* ONE CARD PER PROJECT. A project has at most one open ambient card (`recommendation.card =
  "ambient"`). A new ask JOINS it (its item waits on that card); only when none is open is a new
  card raised — and only while the hour has room (policy.ASKS_PER_PROJECT_HOUR). With no room
  the ask is PARKED (the item waits on a person, no card yet); the Chief of Staff's next card
  (`chief_of_staff.ask_batch`) carries it. The production-mitigation card (roll back prod?
  three answers, urgent) is the one exception and stays its own card.
* PLAIN WORDS FROM STRUCTURED DATA. A card says what and where in a few words built from rows:
  the app by name and environment ("HelpDesk AI Demo (staging)"), the package ("body-parser has
  a known security hole (GHSA-27r3…) · upgrade past 2.3.0"), the action ("open a draft PR").
  Never a raw id (svc_, act_, wim_ …), never gate jargon. What an agent wrote in its own words
  rides behind it as `detail`, with ids scrubbed.
* EACH ENTRY KEEPS ITS OWN CARRIER. One answer reaches every item on the card; each entry is
  carried by its own kind: `handoff` (the Chief of Staff's suggestion) → chief_of_staff.carry_card,
  `squad` (a squad's proposal) → squad_turns.person_answered, `question` (a squad is stuck) →
  the squad goes on / the person takes it, `incident` (the SRE squad) → incident_squad.
* RE-JUDGED EVERY TICK (`verdict`, called by chief_of_staff.reconsider). An entry is withdrawn,
  with a note in words, when the policy no longer asks it: the always-on agents are off; it
  duplicates an item on onboarding's backlog (the person decides that there); BOSS refused the
  action twice (a policy outcome, recorded on the ledger, not a question); the app is healthy
  (the alert cleared, or it was BOSS's own monitor); the squad that asked now holds reads it did
  not hold when it asked (it looks for itself first). Cards raised before this module existed
  are re-judged the same way, and one that still asks is moved onto the project's one card.
* A QUESTION SAYS WHAT IS BLOCKED AND WHAT TO GIVE. "Infra Steward's squad can't read the
  install's logs to explain the restarts — grant access, or take it?", from the squad's own
  summary (`blocked`, `need`); its answers say what they do, and approving does it ("Grant
  access" grants the skill: identity is on the compliance boundary, so a person approves it).
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import board, collab, ledger, policy
from . import task_classes as tc
from ._common import clean_text, emit, person

COS = "agent:chief_of_staff"
CARD = "ambient"
KINDS = ("handoff", "squad", "question", "incident", "budget")
#: A squad whose decided action BOSS refused this many times stops: a policy outcome.
MAX_REFUSED_LAUNCHES = 2

#: A raw id a person must never read ("svc_0d4d…", "act_01M6…", "wim_…", "crun_…").
RAW_ID = re.compile(r"\b[a-z]{2,6}_[0-9A-Za-z]{6,}\b")
_ADVISORY = re.compile(r"\b(GHSA(?:-[0-9a-z]{4}){3}|CVE-\d{4}-\d{4,}|PYSEC-\d{4}-\d+)\b", re.I)
#: The dossier/OSV signal's own title shape: "<package> <version> has <advisory>".
_DEP_TITLE = re.compile(r"^(?P<name>\S+)\s+(?P<version>v?\d[\w.+\-]*)\s+has\s+(?P<adv>\S+)")

#: An action, said the way a person says it.
ACTION_WORDS = {
    "open_draft_pr": "open a draft PR", "start_workflow": "start a fix run",
    "convene_meeting": "ask a panel", "staging_rollback": "roll back staging",
    "staging_restart": "restart staging", "prod_rollback": "roll back production",
    "prod_restart": "restart production", "silence_rule": "silence the alert for an hour",
    "quarantine_test": "quarantine the test", "merge": "merge the pull request",
    "deploy_prod": "deploy to production", "comment": "post a comment", "label": "add a label",
    "nudge": "nudge the owner", "propose": "hand it over", "route": "hand it over",
}

#: Why an entry left a card, in words (the withdrawal note).
WITHDRAW_WHY = {
    "off": "the always-on agents are off, so nothing waits on this answer",
    "backlog": "it is already on the onboarding backlog, which is yours to decide there",
    "policy": "BOSS refused that action twice; the outcome is recorded, nobody needs to answer",
    "healthy": "the app is healthy: the alert cleared, nothing failing is left",
    "rebatch": "it moved onto the project's one card",
    "moot": "the item it asked about has moved on",
    "moving": "the work it was stuck on is moving again (an agent took a turn on its build "
              "after it asked, or the build finished), so nobody needs to take it",
    "retry": "BOSS tried the squad's launch once more now that what stopped it is fixed, so "
             "nobody needs to answer",
    "settled": "the build it was about has ended, or waits on your own decision already in "
               "Needs you, so nothing is left for its squad and nobody is asked",
    "funded": "the squad's budget for it was raised, so it goes on without an answer here",
    "granted": "the squad now reads with the tools it holds (BOSS's logs, health and run "
               "history), so it looks for itself first and asks again only if it is still stuck",
    "stale_rollback": "the rollback it proposed is not to a known-good release (that release "
                      "was rolled back from, or an incident blamed it), so the squad fixes "
                      "forward instead",
}


# ── words ────────────────────────────────────────────────────────────────────────────────────

def scrub(value: Any, limit: int = 240) -> str:
    """Free text an agent wrote, with raw ids taken out (it rides behind the card as detail)."""
    body = RAW_ID.sub("", clean_text(value, 2000))
    body = re.sub(r"\(\s*[,;]?\s*\)", "", body)
    return clean_text(re.sub(r"\s{2,}", " ", body).strip(" ,;"), limit)


def action_words(action: Any) -> str:
    key = str(action or "")
    if key in ACTION_WORDS:
        return ACTION_WORDS[key]
    found = tc.ACTIONS.get(key)
    return found.title.lower() if found else "act"


def _advisory_short(adv: str) -> str:
    adv = adv.strip()
    return adv[:9] + "…" if adv.upper().startswith("GHSA-") and len(adv) > 9 else adv


def _signal_titles(connection: Connection, item_id: str) -> list[str]:
    return [str(t) for t in connection.execute(text(
        "SELECT s.title FROM work_item_links l JOIN signals s ON s.id = l.ref "
        "WHERE l.work_item_id = :w AND l.kind = 'signal' ORDER BY s.first_seen_at"),
        {"w": item_id}).scalars() if t]


def dependency_words(connection: Connection, item: Mapping[str, Any]) -> Optional[str]:
    """"body-parser has a known security hole (GHSA-27r3…) · upgrade past 2.3.0", from the
    package on the item and its signal (the dossier/OSV signal's own title shape)."""
    kind, _, name = str(item.get("subject_ref") or "").partition(":")
    if kind not in ("dep", "package") or not name:
        return None
    titles = _signal_titles(connection, item["id"]) + [str(item.get("title") or "")]
    version = adv = None
    for t in titles:
        m = _DEP_TITLE.match(t.strip())
        if m:
            version = version or m.group("version")
            adv = adv or (m.group("adv") if _ADVISORY.match(m.group("adv")) else None)
        found = _ADVISORY.search(t)
        adv = adv or (found.group(1) if found else None)
    name = clean_text(name, 60)
    if adv or item.get("task_class") == "security_patch":
        head = f"{name} has a known security hole" + (f" ({_advisory_short(adv)})" if adv else "")
    else:
        head = f"{name} is behind"
    return head + (f" · upgrade past {version}" if version else "")


def place_words(connection: Connection, item: Mapping[str, Any]) -> str:
    """The app by name and environment: "HelpDesk AI Demo (staging)"."""
    from ..incident_squad import app_place

    name, env = app_place(connection, item.get("project_id"), item)
    return name + (f" ({env})" if env else "")


def subject_words(connection: Connection, item: Mapping[str, Any]) -> str:
    """What an item is about, in a few plain words, from its rows."""
    dep = dependency_words(connection, item)
    if dep:
        return dep
    if item.get("task_class") in ("incident_response", "incident_staging_mitigation"):
        return place_words(connection, item)
    name = board.plain_subject(item)
    if name:
        return name
    title = scrub(item.get("title"), 80)
    words = title.split()
    return " ".join(words[:8]) + ("…" if len(words) > 8 else "")


def squad_entry(connection: Connection, item: Mapping[str, Any], *, actor: str,
                action: str, proposal_id: Optional[str] = None, recommended: str = "hold",
                why: str = "", consensus: Optional[Mapping[str, Any]] = None,
                dissent: Sequence[Any] = (), squad_title: str = "") -> dict[str, Any]:
    """A squad's proposal a person must decide on, as structured data."""
    what = action_words(action)
    subject = subject_words(connection, item)
    return {"ask_kind": "squad", "work_item_id": item["id"], "task_class": item["task_class"],
            "asked_by": actor, "action": action, "proposal_id": proposal_id,
            "recommended": recommended, "why_a_person": scrub(why, 300),
            "consensus": dict(consensus or {}), "dissent": list(dissent),
            "squad": squad_title, "subject": subject, "words": f"{what}: {subject}",
            "question": f"{what[:1].upper()}{what[1:]}? {subject}"}


def squad_name(title: Any, lead: Any) -> str:
    """The squad by a name a person knows: its template's ("CVE squad"), or its lead's
    ("Infra Steward's squad") — never the bare "Squad" of the general template."""
    title = clean_text(title, 60)
    if title and title.lower() not in ("squad", "the squad"):
        return title
    from .read_model import _party_label

    lead = str(lead or "")
    return f"{_party_label(lead)}'s squad" if lead.startswith("agent:") else "The squad"


def _phrase(value: Any, limit: int) -> str:
    """An agent's few words for a card: ids scrubbed, no closing stop, lower-case start."""
    body = scrub(value, limit).rstrip(" .;:")
    return body[:1].lower() + body[1:] if body[:2] != body[:2].upper() else body


def question_entry(connection: Connection, item: Mapping[str, Any], *, actor: str,
                   reason: Any, squad_title: str = "", blocked: Any = None, need: Any = None,
                   grant: Optional[str] = None, reads: int = 0) -> dict[str, Any]:
    """A squad is stuck and asks a person. The card says in a few words what is blocked and what
    the person must give, from the squad's own summary of its reason (the escalate move's
    `blocked` and `need`): "Infra Steward's squad can't read the install's logs to explain the
    restarts — grant access, or take it?". The full reason rides behind it as `detail`.
    `grant`: the skill an approval gives the asker (asks.grantable), when it asked for access;
    `reads`: how many read tools it held when it asked (a later grant withdraws the ask)."""
    subject = subject_words(connection, item)
    who = squad_name(squad_title, actor)
    blocked_words, need_words = _phrase(blocked, 90), _phrase(need, 40)
    # The question offers only what its buttons do (card_options / carry): grant the skill it
    # names, or close it as expected; either way "or take it". Never a do-nothing "let it go on".
    shown = blocked_words
    if grant:
        need_words = "grant access"
        tail = "grant access, or take it?"
    else:
        tail = "close it as expected, or take it?"
        if need_words and blocked_words:
            shown = f"{blocked_words} (needs: {need_words})"
    if blocked_words:
        question = f"{who} can't {shown} — {tail}"
        words = f"{who.lower()} can't {blocked_words}"
    else:
        question = f"{who} is stuck on {subject} — {tail}"
        words = f"{who.lower()} is stuck: {subject}"
    return {"ask_kind": "question", "work_item_id": item["id"], "task_class": item["task_class"],
            "asked_by": actor, "squad": who, "subject": subject,
            "detail": scrub(reason, 600), "words": words, "question": question,
            "blocked": blocked_words or None, "need": need_words or None,
            "grant": grant or None, "reads": int(reads or 0)}


def grantable(connection: Connection, project_id: str, member: str,
              wanted: Any) -> Optional[str]:
    """The skill that would give `member` the access it asked for (`wanted`: a tool a bundle
    offers, or a skill id), when it does not hold it yet — else None. Granting is identity and
    permissions, on the compliance boundary: a person approves it, and approving grants it
    (role_skills through skill_definitions.assign_skill_to_role; allowed_callers never widen)."""
    wanted = clean_text(wanted, 120)
    if not wanted:
        return None
    from ..agent_baseline import bundles
    from ..skill_definitions import _skill_exists
    from .squad_turns import held_skill_ids

    held = held_skill_ids(connection, project_id, member)
    skill = next((s for s, tools in bundles().items() if wanted in tools), None) or wanted
    if skill in held:
        return None
    try:
        with connection.begin_nested():
            return skill if _skill_exists(connection, project_id, skill) else None
    except Exception:  # noqa: BLE001 — an unknown skill is not grantable
        return None


def _money(micros: int) -> str:
    dollars = int(micros) / 1_000_000
    return f"${dollars:.0f}" if dollars == int(dollars) else f"${dollars:.2f}"


def found_so_far(connection: Connection, item_id: str, limit: int = 200) -> str:
    """What the squad established so far, in ONE sentence: its last finding or hypothesis
    (never several run together)."""
    said = [m["body"].get("summary") for m in collab.messages(
        connection, item_id, kinds=("finding", "hypothesis")) if m["sender"] != "boss"]
    last = next((s for s in reversed(said) if s), "")
    sentence = re.split(r"(?<=[.!?])\s", scrub(last, 600), maxsplit=1)[0].rstrip(" .;")
    return clip_words(sentence, limit) or "nothing settled yet"


def clip_words(text: Any, limit: int) -> str:
    """Text cut to `limit` characters at a clause or word boundary, with an ellipsis — never
    mid-word ("… that has nothing to d" on boss-dogfood's card, 2026-11-08)."""
    body = " ".join(str(text or "").split())
    if len(body) <= limit:
        return body
    cut = body[:limit - 1]
    clause = max(cut.rfind("; "), cut.rfind(", "), cut.rfind(" — "))
    if clause >= limit * 0.6:
        cut = cut[:clause]
    elif " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:—-(") + "…"


def entry_short(entry: Mapping[str, Any]) -> str:
    """An entry's short title, naming its work: "This install: squad out of budget"."""
    name = re.sub(r"[“”\"]", "", str(entry.get("name") or entry.get("subject") or "")).strip()
    name = clip_words(name, 40)
    what = {"budget": "squad out of budget", "question": "squad stuck",
            "squad": "squad asks", "incident": "incident"}.get(entry_kind(entry), "hand-off")
    return f"{name[:1].upper()}{name[1:]}: {what}" if name else what.capitalize()


def card_short(entries: Sequence[Mapping[str, Any]]) -> str:
    """THE CARD'S SHORT TITLE (Needs-you list, chip, drawer header), naming the work — never
    the question cut to its first words ("Infra Steward's squad used")."""
    if len(entries) == 1:
        return entry_short(entries[0])
    kinds = {entry_kind(e) for e in entries}
    if kinds == {"budget"}:
        return f"{len(entries)} squads out of budget"
    if kinds == {"question"}:
        return f"{len(entries)} squads stuck"
    return f"{len(entries)} agent questions"


def quoted(value: Any, words: int = 10) -> str:
    """A title for a card, in ONE pair of quotes and a few words: quotes it already carries are
    dropped first (a doctor title "“Open draft PR …” build has not moved" rendered as ““…”)."""
    body = re.sub(r"[“”\"]", "", scrub(value, 160)).split()
    return "“" + " ".join(body[:words]) + ("…" if len(body) > words else "") + "”"


def entry_name(connection: Connection, item: Mapping[str, Any]) -> str:
    """The work an entry is about in a word or two, for a batch line: the build it names (by
    the item that started it: "esbuild bump"), else its plain subject, else its first words."""
    from .chief_of_staff import item_runs, stalled_name

    if item_runs(connection, item):
        name, noun = stalled_name(connection, item)
        name = re.sub(r"[“”\"]", "", name)
        return f"{name} {noun}".strip()
    return board.plain_subject(item) or " ".join(
        re.sub(r"[“”\"]", "", scrub(item.get("title"), 80)).split()[:4])


def budget_entry(connection: Connection, item: Mapping[str, Any], *, actor: str,
                 squad_title: str = "") -> dict[str, Any]:
    """A squad that spent its budget on the item (spend above a cap is a person's call: the
    compliance boundary). "Infra Steward's squad used its $1.50 on “Updates restarted a build 2
    times” — give it $1 more, or close it?"; what it found so far rides as `detail`."""
    from .squad_turns import BUDGET_STEP_USD_MICROS, budget_of

    who = squad_name(squad_title, actor)
    title = quoted(item.get("title"))
    budget, step = budget_of(item), BUDGET_STEP_USD_MICROS
    question = (f"{who} used its {_money(budget)} on {title} — give it {_money(step)} more, "
                "or close it?")
    return {"ask_kind": "budget", "work_item_id": item["id"], "task_class": item["task_class"],
            "asked_by": actor, "squad": who, "subject": title,
            "name": entry_name(connection, item),
            "spent_usd_micros": int(item.get("cost_usd_micros") or 0),
            "budget_usd_micros": budget, "step_usd_micros": step,
            "detail": f"Found so far: {found_so_far(connection, item['id'])}",
            "words": f"{who.lower()} spent its budget: {title}", "question": question}


def settle_settled(connection: Connection, item: Mapping[str, Any], *,
                   card_id: Optional[str]) -> str:
    """The item's build ended, or waits on the person's own card: nothing is left for its
    squad, so it is closed — recorded, linked to that card, never asked."""
    from .chief_of_staff import runs_settled

    item_id = item["id"]
    board.unask(connection, item_id, actor=COS, reason=WITHDRAW_WHY["settled"])
    got = runs_settled(connection, item["project_id"], item, exclude=card_id)
    if got and got[0] == "waits":
        board.link(connection, item_id, "decision", str(got[1]), actor=COS,
                   note="the work waits on this")
        said = "Its build is done and waits on your own decision, already in Needs you"
    else:
        said = "The build it was about has ended"
    token = _take_from_agent(connection, item_id)
    ledger.record_decision(
        connection, project_id=item["project_id"], work_item_id=item_id,
        task_class=tc.task_class(item["task_class"]).key, decided_by=COS, chosen="close",
        options=["close", "ask"], question="Is anything left for its squad to do?",
        reversibility="reversible", undo={"action": "reopen", "work_item_id": item_id},
        evidence=item_evidence(connection, item_id, card_id)
        or [{"kind": "work_item", "ref": item_id}],
        rationale=f"{said}, so nothing is left for its squad and nobody is asked.")
    board.finish(connection, item_id, actor=COS, lifecycle="resolved", token=token, outcome=said)
    return "settled"


def ask_budget(connection: Connection, settings: Any, item_id: str) -> dict[str, Any]:
    """Put a spent-budget item in front of a person, on the project's ONE card (joined, else
    raised while the hour has room, else parked for the Chief of Staff's next card). Its
    asking lease is let go: whoever works it after the answer claims it fresh."""
    from . import squads

    from .chief_of_staff import runs_settled

    item = board._must(connection, item_id)  # noqa: SLF001
    if runs_settled(connection, item["project_id"], item):
        # The build it is about ended, or waits on the person's own card: nothing to fund.
        return {"outcome": settle_settled(connection, item, card_id=None)}
    roster = squads.roster(connection, item_id) or {}
    lead = str(roster.get("lead") or item.get("owner_actor") or COS)
    entry = budget_entry(connection, item, actor=lead, squad_title=str(roster.get("title") or ""))
    connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL WHERE id = :id AND lease_kind = 'agent'"), {"id": item_id})
    got = place(connection, settings, item["project_id"], [entry], actor=lead)
    if got.get("held"):
        park(connection, item_id, entry, actor=lead)
        return {"outcome": "parked"}
    return {"outcome": "joined" if got.get("joined") else "asked",
            "decision_id": got.get("decision_id")}


def incident_entry(connection: Connection, item: Mapping[str, Any], *, actor: str,
                   question: str) -> dict[str, Any]:
    q = scrub(question, 200)
    return {"ask_kind": "incident", "work_item_id": item["id"], "task_class": item["task_class"],
            "asked_by": actor, "subject": place_words(connection, item),
            "words": q, "question": q}


# ── the project's one card ───────────────────────────────────────────────────────────────────

def _open_statuses() -> list[str]:
    from ..decision_actions import OPEN_STATUSES

    return sorted(OPEN_STATUSES)


def open_card(connection: Connection, project_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, question, recommendation, options, created_at FROM decisions "
        "WHERE project_id = :p AND status = ANY(:open) AND recommendation->>'card' = :c "
        "ORDER BY created_at LIMIT 1 FOR UPDATE"),
        {"p": project_id, "open": _open_statuses(), "c": CARD}).mappings().first()
    return dict(row) if row else None


def entry_kind(entry: Mapping[str, Any]) -> str:
    kind = str(entry.get("ask_kind") or "")
    return kind if kind in KINDS else "handoff"


def card_question(entries: Sequence[Mapping[str, Any]]) -> str:
    """The card's one line, from its entries' structured words."""
    from .chief_of_staff import batch_question

    if all(entry_kind(e) == "handoff" for e in entries):
        return batch_question(entries)
    if len(entries) == 1:
        return str(entries[0].get("question") or entries[0].get("words") or "")
    if all(entry_kind(e) == "budget" for e in entries):
        # A batch names the work: "2 squads ran out of budget: this install, esbuild bump".
        names = ", ".join(dict.fromkeys(str(e.get("name") or e.get("subject") or "")
                                        for e in entries))
        return f"{len(entries)} squads ran out of budget: {names}"
    from .chief_of_staff import _MANY, _SHORT

    groups: dict[str, int] = {}
    for e in entries:
        key = (entry_kind(e) if entry_kind(e) in ("incident", "budget")
               else str(e.get("task_class") or ""))
        groups[key] = groups.get(key, 0) + 1
    parts = []
    for key, n in sorted(groups.items(), key=lambda kv: -kv[1]):
        if key == "incident":
            parts.append("an incident" if n == 1 else f"{n} incidents")
        elif key == "budget":
            parts.append("a spent budget" if n == 1 else f"{n} spent budgets")
        elif n == 1:
            parts.append(_SHORT.get(key) or tc.task_class(key).title.lower())
        else:
            parts.append(_MANY.get(key, "{n} × " + tc.task_class(key).title.lower()).format(n=n))
    return f"Agents ask about {len(entries)} things: {', '.join(parts)}"


def card_options(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The card's answers, saying what each does (and approving does exactly that, `carry`).
    One question: "Close it: expected" / "I'll take it", or "Grant access" when the squad asked for a
    skill it lacks. Never "Approve all" over a single question."""
    from .chief_of_staff import OWN_ACTIONS, PARTY

    if len(entries) > 1:
        if all(entry_kind(e) == "handoff" for e in entries):
            own = all(e.get("owner") == PARTY and str(e.get("next_action")) in OWN_ACTIONS
                      for e in entries)
            label = (tc.ACTIONS[str(entries[0].get("next_action"))].title + " all"
                     if own and len({str(e.get("next_action")) for e in entries}) == 1
                     else "Hand all over")
            return [{"key": "approve", "label": label},
                    {"key": "reject", "label": "Dismiss all"}]
        if all(entry_kind(e) == "budget" for e in entries):
            # "All" answers say what they do; each entry also has its own (answer_entry).
            n = len(entries)
            step = _money(int(entries[0].get("step_usd_micros") or 1_000_000))
            return [{"key": "approve", "label": f"Give each {step} more"},
                    {"key": "reject", "label": "Close both" if n == 2 else f"Close all {n}"},
                    {"key": "take", "label": "I'll take them"}]
        if all(entry_kind(e) == "question" for e in entries):
            grants = [bool(e.get("grant")) for e in entries]
            label = ("Grant access to all" if all(grants) else
                     "Close all: expected" if not any(grants) else
                     "Grant where asked, close the rest")
            return [{"key": "approve", "label": label},
                    {"key": "reject", "label": "I'll take them"}]
        # Mixed kinds: each entry has its own answers (answer_entry); "all" says what it does.
        return [{"key": "approve", "label": "Yes to each, as each asks"},
                {"key": "reject", "label": "No to each"}]
    e = entries[0]
    kind = entry_kind(e)
    if kind == "squad":
        return [{"key": "approve", "label": f"Approve: {action_words(e.get('action'))}"},
                {"key": "hold", "label": "Hold"}]
    if kind == "question":
        return [{"key": "approve",
                 "label": "Grant access" if e.get("grant") else "Close it: expected"},
                {"key": "reject", "label": "I'll take it"}]
    if kind == "incident":
        return [{"key": "approve", "label": "I'll take it"},
                {"key": "reject", "label": "Close it"}]
    if kind == "budget":
        # Three answers, each carried by `carry`: approve raises the budget, reject closes it,
        # "take" (sent as the request-changes action) hands it to the person.
        return [{"key": "approve",
                 "label": f"Give it {_money(int(e.get('step_usd_micros') or 1_000_000))} more"},
                {"key": "reject", "label": "Close it"},
                {"key": "take", "label": "I'll take it"}]
    label = ("Answer" if e.get("asked") else
             tc.ACTIONS.get(str(e.get("next_action")), tc.ACTIONS["propose"]).title
             if e.get("owner") == PARTY else f"Hand to {e.get('owner_label')}")
    return [{"key": "approve", "label": label}, {"key": "reject", "label": "Dismiss"}]


#: Entry fields lifted to the card's top level when it asks one thing (the console and the
#: carriers read a one-item card the way they always did).
_LIFT = ("asked_by", "owner", "owner_label", "next_action", "owner_may_act", "act_id", "words",
         "action", "recommended", "proposal_id", "why_a_person", "consensus", "dissent", "squad",
         "estimate_usd")


def _recommendation(entries: Sequence[Mapping[str, Any]], *, manual: bool = False,
                    base: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    rec = {k: v for k, v in dict(base or {}).items() if k not in _LIFT}
    lead = entries[0]
    rec.update({"card": CARD, "batch": list(entries), "count": len(entries),
                "asked_by": COS, "work_item_id": lead["work_item_id"],
                "work_item_ids": [e["work_item_id"] for e in entries],
                "manual": bool(manual or rec.get("manual"))})
    only_handoffs = all(entry_kind(e) == "handoff" for e in entries)
    rec["action"] = "ambient_route" if only_handoffs else "ambient_card"
    if len(entries) == 1:
        rec.update({k: lead[k] for k in _LIFT if k in lead})
        if entry_kind(lead) == "incident":
            rec["mitigation"] = "escalate"
    return rec


def _risk_tier(entries: Sequence[Mapping[str, Any]]) -> int:
    risky = any(tc.task_class(str(e.get("task_class"))).risk != "low" for e in entries
                if e.get("task_class") in tc.TASK_CLASSES)
    return 2 if risky else 1


def _attach(connection: Connection, card_id: str, item_id: str, *, actor: str) -> None:
    """The item waits on the card."""
    item = board._must(connection, item_id)  # noqa: SLF001 — the board's own row read
    board.link(connection, item_id, "decision", card_id, actor=actor)
    connection.execute(text(
        "UPDATE work_items SET needs_you_decision_id = :d, needs_you_at = COALESCE(needs_you_at, "
        "now()), answered_at = NULL WHERE id = :id"), {"d": card_id, "id": item_id})
    if item["lifecycle"] != "needs_you":
        board._transition(connection, item, "needs_you", actor=actor,  # noqa: SLF001
                          extra={"decision_id": card_id})


def raise_card(connection: Connection, project_id: str, entries: Sequence[Mapping[str, Any]],
               *, actor: str = COS, manual: bool = False) -> str:
    """A new card asking every entry, each item waiting on it. Returns the decision id."""
    from ..core import create_blocking_decision

    entries = [dict(e) for e in entries]
    rec = _recommendation(entries, manual=manual)
    items = [board._must(connection, e["work_item_id"]) for e in entries]  # noqa: SLF001
    rec.update({"task_class": items[0]["task_class"],
                "autonomy_level": items[0]["autonomy_level"],
                "cost_of_waiting": f"{len(entries)} item{'s' if len(entries) > 1 else ''} wait"})
    decision_id = create_blocking_decision(
        connection, project_id, None, clean_text(card_question(entries), 300), rec,
        {"ambient_cost_usd_micros": sum(int(i.get("cost_usd_micros") or 0) for i in items)},
        "ambient_needs_you", risk_tier=_risk_tier(entries), default_action="hold")
    merged = connection.execute(text(
        "SELECT id, question, recommendation, options, created_at FROM decisions "
        "WHERE id = :d AND NOT (COALESCE(recommendation->'work_item_ids', '[]'::jsonb) "
        "@> CAST(:w AS JSONB))"),
        {"d": decision_id, "w": json.dumps([e["work_item_id"] for e in entries])}
    ).mappings().first()
    if merged is not None:
        # create_blocking_decision folds a same-question pending decision into the open one. An
        # ambient card that already asks the same words is joined (its batch gains these
        # entries) — never left asking without them while their items wait on it.
        card = dict(merged, project_id=project_id)
        if (card.get("recommendation") or {}).get("card") == CARD:
            join_card(connection, card, entries, actor=actor)
            return decision_id
    connection.execute(text("UPDATE decisions SET options = CAST(:o AS JSONB) WHERE id = :d"),
                       {"o": json.dumps(card_options(entries)), "d": decision_id})
    for e in entries:
        _attach(connection, decision_id, e["work_item_id"], actor=actor)
    return decision_id


def join_card(connection: Connection, card: Mapping[str, Any],
              entries: Sequence[Mapping[str, Any]], *, actor: str = COS) -> list[str]:
    """Add entries to the open card (one per item; an item already on it is not added twice)."""
    rec = dict(card.get("recommendation") or {})
    current = list(rec.get("batch") or [])
    have = {str(e.get("work_item_id")) for e in current}
    added = []
    for e in entries:
        if str(e["work_item_id"]) in have:
            continue
        current.append(dict(e))
        have.add(str(e["work_item_id"]))
        added.append(str(e["work_item_id"]))
    if not added:
        return []
    new = _recommendation(current, base=rec)
    connection.execute(text(
        "UPDATE decisions SET recommendation = CAST(:r AS JSONB), question = :q, "
        "options = CAST(:o AS JSONB), risk_tier = GREATEST(risk_tier, :t), updated_at = now() "
        "WHERE id = :d"),
        {"r": json.dumps(new, default=str), "q": clean_text(card_question(current), 300),
         "o": json.dumps(card_options(current)), "t": _risk_tier(current), "d": card["id"]})
    for item_id in added:
        _attach(connection, card["id"], item_id, actor=actor)
    emit(connection, str(card.get("project_id") or board._must(  # noqa: SLF001
        connection, added[0])["project_id"]), "ambient.card_joined", actor, "decision",
        card["id"], {"items": added})
    return added


def park(connection: Connection, item_id: str, entry: Mapping[str, Any], *, actor: str) -> None:
    """No room for a card this hour: the item waits on a person with no card yet; the Chief of
    Staff's next card carries the ask (`parked`)."""
    collab.post(connection, item_id, kind="escalate", sender=actor,
                body={"to": COS, "reason": entry.get("words") or entry.get("question") or "asks",
                      "ask": dict(entry)})
    item = board._must(connection, item_id)  # noqa: SLF001
    if item["lifecycle"] != "needs_you":
        board._transition(connection, item, "needs_you", actor=actor,  # noqa: SLF001
                          extra={"parked": True})
    connection.execute(text(
        "UPDATE work_items SET needs_you_decision_id = NULL, needs_you_at = now() "
        "WHERE id = :id"), {"id": item_id})


def parked(connection: Connection, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Asks waiting for a card: the item waits on a person with no card, its last parked ask."""
    rows = connection.execute(text(
        "SELECT w.id, m.body FROM work_items w JOIN LATERAL (SELECT body FROM work_item_messages "
        "  x WHERE x.work_item_id = w.id AND x.kind = 'escalate' AND x.body ? 'ask' "
        "  ORDER BY x.created_at DESC LIMIT 1) m ON true "
        "WHERE w.project_id = :p AND w.ambient AND w.lifecycle = 'needs_you' "
        "AND w.needs_you_decision_id IS NULL ORDER BY w.effective_score DESC NULLS LAST, "
        "w.created_at LIMIT :n"), {"p": project_id, "n": limit}).mappings().all()
    return [dict((r["body"] or {}).get("ask") or {}, work_item_id=r["id"]) for r in rows]


def place(connection: Connection, settings: Any, project_id: str,
          entries: Sequence[Mapping[str, Any]], *, actor: str = COS,
          manual: bool = False, urgent: bool = False) -> dict[str, Any]:
    """Put entries in front of a person: join the open card, else raise one if the hour has
    room (`urgent` — an incident — needs no room), else report them held.
    {"decision_id", "asked": [...]} or {"held": [...]}"""
    entries = [dict(e) for e in entries if e.get("work_item_id")]
    if not entries:
        return {"asked": []}
    card = open_card(connection, project_id)
    if card is not None:
        card["project_id"] = project_id
        return {"decision_id": card["id"], "joined": True,
                "asked": join_card(connection, card, entries, actor=actor)}
    if not urgent and policy.ask_room(connection, project_id, settings) <= 0:
        return {"asked": [], "held": [e["work_item_id"] for e in entries],
                "reason": f"{policy.asks_per_hour(settings)} cards already this hour"}
    decision_id = raise_card(connection, project_id, entries, actor=actor, manual=manual)
    return {"decision_id": decision_id, "asked": [e["work_item_id"] for e in entries]}


def ask(connection: Connection, settings: Any, item_id: str, *, actor: str,
        token: Optional[str], entry: Mapping[str, Any]) -> dict[str, Any]:
    """THE ONE WAY an always-on agent asks a person about its item (it holds the item's lease).
    Returns {"outcome": joined | asked | parked, "needs_you_decision_id"?}."""
    item = board._must(connection, item_id)  # noqa: SLF001
    board.assert_lease(connection, item_id, owner=actor, token=token)
    if item["lifecycle"] in board.TERMINAL:
        raise board.Conflict(f"the item is already {item['lifecycle']}")
    got = place(connection, settings, item["project_id"], [dict(entry, work_item_id=item_id)],
                actor=actor)
    if got.get("held"):
        park(connection, item_id, entry, actor=actor)
        return {"outcome": "parked", "needs_you_decision_id": None}
    return {"outcome": "joined" if got.get("joined") else "asked",
            "needs_you_decision_id": got["decision_id"]}


# ── re-judging an entry ──────────────────────────────────────────────────────────────────────

def backlog_twin(connection: Connection, item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The onboarding backlog item this work item duplicates: onboarding proposed the same
    signal (its recommendation's subject is that signal). Open, snoozed, accepted or rejected —
    either the person is deciding on it there, or already did."""
    subjects = [f"signal:{s}" for s in connection.execute(text(
        "SELECT ref FROM work_item_links WHERE work_item_id = :w AND kind = 'signal'"),
        {"w": item["id"]}).scalars()]
    if not subjects:
        return None
    row = connection.execute(text(
        "SELECT work_item_id, title, status FROM ambient_recommendations WHERE project_id = :p "
        "AND detector = 'onboarding' AND status IN ('open', 'snoozed', 'accepted', 'rejected') "
        "AND subject = ANY(:s) ORDER BY created_at LIMIT 1"),
        {"p": item["project_id"], "s": subjects}).mappings().first()
    return dict(row) if row else None


def refused_launches(connection: Connection, item_id: str) -> int:
    """How many times BOSS refused to start what the squad decided (launch_workflow /
    convene_panel post these findings themselves)."""
    return int(connection.execute(text(
        "SELECT count(*) FROM work_item_messages WHERE work_item_id = :w AND kind = 'finding' "
        "AND sender = 'boss' AND (body->>'summary' LIKE 'Could not start %' "
        "OR body->>'summary' LIKE 'Could not convene%')"), {"w": item_id}).scalar() or 0)


def alerts_state(connection: Connection, item: Mapping[str, Any]) -> str:
    """The alerts behind an incident item: 'meta' (BOSS's own monitor, not the app), 'cleared'
    (none still firing), 'firing', or 'none' (the item cites no alert)."""
    ids = [str(r) for r in connection.execute(text(
        "SELECT ref FROM work_item_links WHERE work_item_id = :w AND kind = 'alert' "
        "UNION SELECT e->>'ref' FROM work_item_links l JOIN signals s ON s.id = l.ref "
        "CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(s.evidence) = 'array' "
        "  THEN s.evidence ELSE '[]'::jsonb END) e "
        "WHERE l.work_item_id = :w AND l.kind = 'signal' AND e->>'kind' = 'alert'"),
        {"w": item["id"]}).scalars() if r]
    if not ids:
        return "none"
    rows = connection.execute(text(
        "SELECT state, rule_kind, labels FROM app_alerts WHERE id = ANY(:ids)"),
        {"ids": ids}).mappings().all()
    if not rows:
        return "none"
    if all(is_meta_alert(r) for r in rows):
        return "meta"
    return "firing" if any(r["state"] == "firing" for r in rows) else "cleared"


def is_meta_alert(row: Mapping[str, Any]) -> bool:
    """BOSS's own self-check (its evaluator, its scheduler), not the app: never an incident."""
    labels = row.get("labels") or {}
    return (str(row.get("rule_kind") or "") == "meta" or str(labels.get("boss_meta")) == "true"
            or str(labels.get("layer") or "") == "boss")


def verdict(connection: Connection, card: Mapping[str, Any], entry: Mapping[str, Any],
            mode: str) -> tuple[str, Optional[dict[str, Any]]]:
    """Does a person still need to answer this (non-hand-off) entry? ('ask' | 'off' | 'backlog'
    | 'policy' | 'healthy' | 'moot', the item)."""
    item = board.get(connection, str(entry.get("work_item_id") or ""))
    if (item is None or item["lifecycle"] != "needs_you"
            or item.get("needs_you_decision_id") != card["id"]):
        return "moot", item
    # What settles the item itself comes first, in any mode: a healthy app, a duplicate of the
    # backlog, a policy outcome. Only what would still ask is withdrawn for "off".
    kind = entry_kind(entry)
    if kind == "incident":
        if alerts_state(connection, item) in ("meta", "cleared"):
            return "healthy", item
        from ..incident_squad import rollback_still_known_good

        if rollback_still_known_good(connection, item, card.get("recommendation") or {}) \
                is False:
            return "stale_rollback", item
    elif backlog_twin(connection, item):
        return "backlog", item
    elif refused_launches(connection, item["id"]) >= MAX_REFUSED_LAUNCHES:
        return "policy", item
    if kind == "question" and item_moving(connection, item["id"], card.get("created_at")):
        # A squad that said it is stuck, whose work is moving again: a stale ask.
        return "moving", item
    if kind in ("question", "budget"):
        from .chief_of_staff import runs_settled

        if runs_settled(connection, item["project_id"], item, exclude=card.get("id")):
            return "settled", item         # its build ended or waits on the person's own card
    if kind == "budget":
        from .squad_turns import budget_of

        if int(item.get("cost_usd_micros") or 0) < budget_of(item):
            return "funded", item          # its budget was raised elsewhere: nothing to ask
    if kind == "question" and can_now_look(connection, item, entry):
        # A squad that asked before it held (or could use) the reads it now has: it looks
        # itself first (boss-dogfood 2026-11-07, "no tool access to host:install logs").
        return "granted", item
    if mode == "off" and not (card.get("recommendation") or {}).get("manual"):
        return "off", item
    if kind == "question" and launch_to_retry(connection, item) is not None:
        return "retry", item
    return "ask", item


def can_now_look(connection: Connection, item: Mapping[str, Any],
                 entry: Mapping[str, Any]) -> bool:
    """The squad that asked holds more read tools now than when it asked (`reads` on the entry;
    an ask from before squads could read has none): a grant since, or the read phase its turns
    gained, lets it look for itself before anyone is asked."""
    asker = str(entry.get("asked_by") or "")
    if not asker.startswith("agent:"):
        return False
    from .squad_turns import held_reads

    return len(held_reads(connection, None, item["project_id"], asker)) > int(
        entry.get("reads") or 0)


def item_moving(connection: Connection, item_id: str, asked_at: Any) -> bool:
    """A build the item launched has moved since the ask (stuck_work.moved_since: an agent turn
    after it, or it finished). The same test the Shepherd's own "stuck" card is withdrawn by."""
    from .. import stuck_work

    if asked_at is None:
        return False
    try:
        with connection.begin_nested():
            return any(stuck_work.moved_since(connection, run, asked_at)
                       for run in stuck_work.runs_of_item(connection, item_id))
    except Exception:  # noqa: BLE001 — an ask that cannot be judged stays asked
        return False


#: The finding BOSS posts when it tries a failed launch once more (never "Could not start …",
#: so the retry itself is not counted as a refusal).
RETRY_NOTE = "Trying the launch once more"


def launch_to_retry(connection: Connection, item: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The squad decision whose launch BOSS refused ONCE and that was never retried, or None.
    A squad stuck on a launch that failed for a reason BOSS has since fixed (8bfebcde7: the
    launch now acts as the squad's agent, verified; a workflow the lead named that the project
    does not have now falls back to the squad's own) asks a person nothing new: the reconsider
    pass tries it once more. A second refusal is the policy's answer (MAX_REFUSED_LAUNCHES)."""
    if refused_launches(connection, item["id"]) != 1:
        return None
    if connection.execute(text(
            "SELECT 1 FROM work_item_messages WHERE work_item_id = :w AND kind = 'finding' "
            "AND sender = 'boss' AND body->>'summary' LIKE :r LIMIT 1"),
            {"w": item["id"], "r": RETRY_NOTE + "%"}).first():
        return None
    from .squad_plugins import EXECUTORS

    row = connection.execute(text(
        "SELECT act_id, action, chosen, decided_by, proposal_id, consensus, created_at "
        "FROM agent_act_decisions WHERE work_item_id = :w AND action = ANY(:a) "
        "AND chosen = action AND proposal_id IS NOT NULL ORDER BY created_at DESC LIMIT 1"),
        {"w": item["id"], "a": list(EXECUTORS)}).mappings().first()
    if row is None:
        return None
    failed_after = connection.execute(text(
        "SELECT 1 FROM work_item_messages WHERE work_item_id = :w AND kind = 'finding' "
        "AND sender = 'boss' AND created_at >= :t AND (body->>'summary' LIKE 'Could not start %' "
        "OR body->>'summary' LIKE 'Could not convene%') LIMIT 1"),
        {"w": item["id"], "t": row["created_at"]}).first()
    carried = any((((m.get("body") or {}).get("packet") or {}).get("decided") or {})
                  .get("act_id") == row["act_id"]
                  for m in collab.messages(connection, item["id"], kinds=("handoff",)))
    if not failed_after or carried:
        return None
    return dict(row)


def retry_launch(connection: Connection, project_id: str, item: Mapping[str, Any], *,
                 launchers: Any = None) -> str:
    """Try a refused squad launch once more, as the squad's lead under the decision it already
    made (the launchers verify that identity, live). Started: the item runs on, linked to the
    run. Refused again: the policy's answer — recorded, the item ends, nobody is asked."""
    from . import squad_plugins, squads

    found = launch_to_retry(connection, item)
    roster = squads.roster(connection, item["id"])
    if found is None or roster is None:
        return "back_on_board"
    prop = next((m for m in collab.messages(connection, item["id"], kinds=("proposal",))
                 if m["id"] == found["proposal_id"]), None)
    if prop is None:
        return "back_on_board"
    lead = roster["lead"]
    connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL WHERE id = :id AND lease_kind = 'agent' AND lease_owner <> :o"),
        {"id": item["id"], "o": lead})
    token = board.claim(connection, item["id"], owner=lead)["lease_token"]
    board.start(connection, item["id"], owner=lead, token=token)
    collab.post(connection, item["id"], kind="finding", sender="boss", body={
        "summary": f"{RETRY_NOTE}: what stopped it is fixed",
        "evidence": [{"kind": "act", "ref": found["act_id"]}]})
    if launchers is None:
        from ...config import get_settings

        launchers = squad_plugins.ToolLaunchers(get_settings())
    done = squad_plugins.EXECUTORS[found["action"]](
        connection, board.get(connection, item["id"]), squad=roster, proposal=prop,
        decision=found, launchers=launchers)
    if done.get("error"):
        from .squad_turns import stop_by_policy

        stop_by_policy(connection, board.get(connection, item["id"]), roster, token,
                       action=found["action"])
        return "stopped_by_policy"
    return "relaunched"


def is_mitigation_card(card: Mapping[str, Any]) -> bool:
    """The squad's own "Roll back X to Y?" card (three answers, urgent): it stays its own card,
    never batched or withdrawn for the mode — but it is still re-judged for being VALID."""
    rec = card.get("recommendation") or {}
    return (rec.get("action") == "incident_squad.mitigate"
            and rec.get("mitigation") != "escalate" and rec.get("card") != CARD)


def mitigation_verdict(connection: Connection, card: Mapping[str, Any]
                       ) -> tuple[str, Optional[dict[str, Any]]]:
    """A mitigation card re-judged: 'stale_rollback' when the release it proposes is no longer
    the last known-good one (incident_squad.rollback_still_known_good — the same rule as
    stack_deploy.rollback_option and the blamed releases), 'moot' when its item moved on, else
    'ask' (it stands, as its own card). Being its own card never exempts it from this check
    (HelpDesk AI 2026-11-07: "Roll back helpdesk-ai-v3 to c6d4d3a (known fix)?" — c6d4d3a was
    the release INC-1 rolled back from — sat pending after the rule changed)."""
    rec = card.get("recommendation") or {}
    item = board.get(connection, str(rec.get("work_item_id") or ""))
    if (item is None or item["lifecycle"] != "needs_you"
            or item.get("needs_you_decision_id") != card["id"]):
        return "moot", item
    from ..incident_squad import rollback_still_known_good

    if rollback_still_known_good(connection, item, rec) is False:
        return "stale_rollback", item
    return "ask", item


def legacy_entry(card: Mapping[str, Any]) -> dict[str, Any]:
    """A card raised before this module (one item, its asker's own shape) read as an entry."""
    rec = dict(card.get("recommendation") or {})
    if rec.get("action") == "incident_squad.mitigate":
        kind = "incident"
    elif rec.get("proposal_id"):
        kind = "squad"
    else:
        kind = "question"
    return {**rec, "ask_kind": kind, "work_item_id": rec.get("work_item_id"),
            "words": card.get("question"), "question": card.get("question")}


def rewords(connection: Connection, entry: Mapping[str, Any],
            item: Mapping[str, Any]) -> dict[str, Any]:
    """A legacy entry, said again in plain words from its item's rows."""
    kind = entry_kind(entry)
    actor = str(entry.get("asked_by") or COS)
    if kind == "budget":
        return budget_entry(connection, item, actor=actor,
                            squad_title=str(entry.get("squad") or ""))
    if kind == "squad":
        return squad_entry(connection, item, actor=actor, action=str(entry.get("action")),
                           proposal_id=entry.get("proposal_id"),
                           recommended=str(entry.get("recommended") or "hold"),
                           why=str(entry.get("why_a_person") or ""),
                           consensus=entry.get("consensus"), dissent=entry.get("dissent") or (),
                           squad_title=str(entry.get("squad") or ""))
    if kind == "incident":
        from ..incident_squad import recovery_question

        name, env = _place(connection, item)
        return incident_entry(connection, item, actor=actor, question=recovery_question(
            name, env, rolled_back=False, unconfirmed=False))
    return question_entry(connection, item, actor=actor,
                          reason=entry.get("detail") or entry.get("question"),
                          squad_title=str(entry.get("squad") or ""), blocked=entry.get("blocked"),
                          need=entry.get("need"), grant=entry.get("grant"),
                          reads=int(entry.get("reads") or 0))


def _place(connection: Connection, item: Mapping[str, Any]) -> tuple[str, str]:
    from ..incident_squad import app_place

    return app_place(connection, item.get("project_id"), item)


def item_evidence(connection: Connection, item_id: str,
                  card_id: Optional[str] = None) -> list[dict[str, str]]:
    """What a settling decision cites: the card it withdrew, BOSS's refusals, the signals."""
    out = [{"kind": "decision", "ref": card_id}] if card_id else []
    out += [{"kind": "message", "ref": m} for m in connection.execute(text(
        "SELECT id FROM work_item_messages WHERE work_item_id = :w AND kind = 'finding' "
        "AND sender = 'boss' AND (body->>'summary' LIKE 'Could not start %' "
        "OR body->>'summary' LIKE 'Could not convene%') ORDER BY created_at LIMIT 3"),
        {"w": item_id}).scalars()]
    out += [{"kind": "signal", "ref": r} for r in connection.execute(text(
        "SELECT ref FROM work_item_links WHERE work_item_id = :w AND kind = 'signal' LIMIT 5"),
        {"w": item_id}).scalars()]
    return out


def _take_from_agent(connection: Connection, item_id: str) -> str:
    """The Chief of Staff takes the item to settle it: an agent's lease is let go (a person's
    never is — claim refuses that). Returns the token."""
    connection.execute(text(
        "UPDATE work_items SET lease_owner = NULL, lease_kind = NULL, lease_token = NULL, "
        "lease_expires_at = NULL WHERE id = :id AND lease_kind = 'agent'"), {"id": item_id})
    return board.claim(connection, item_id, owner=COS, minutes=5)["lease_token"]


def settle(connection: Connection, project_id: str, card_id: Optional[str],
           item: Mapping[str, Any], entry: Mapping[str, Any], why: str) -> str:
    """The item a withdrawn (non-hand-off) entry asked about: back on the board, then ended or
    left to proceed, with the reason on the ledger. Returns what happened."""
    item_id = item["id"]
    board.unask(connection, item_id, actor=COS, reason=WITHDRAW_WHY[why])
    if why in ("off", "rebatch", "moot", "moving", "granted", "funded"):
        return "back_on_board"
    if why == "settled":
        return settle_settled(connection, item, card_id=card_id)
    if why == "retry":
        return retry_launch(connection, project_id, item)
    if why == "stale_rollback":
        from ..incident_squad import withdraw_rollback

        withdraw_rollback(connection, project_id=project_id, item_id=item_id,
                          why=WITHDRAW_WHY[why])
        return "fix_forward"
    if why == "backlog":
        return _settle_backlog(connection, item, card_id=card_id,
                               proposal_id=entry.get("proposal_id"))
    token = _take_from_agent(connection, item_id)
    klass = tc.task_class(item["task_class"])
    common = dict(project_id=project_id, work_item_id=item_id, task_class=klass.key,
                  evidence=item_evidence(connection, item_id, card_id), reversibility="reversible",
                  undo={"action": "reopen", "work_item_id": item_id})
    if why == "policy":
        said = entry.get("action")
        what = action_words(said if said in tc.ACTIONS else
                            _last_decided_action(connection, item_id))
        ledger.record_decision(
            connection, **common, decided_by="policy:squad_launch", chosen="stop",
            options=["stop", "ask"], proposal_id=entry.get("proposal_id"),
            question=f"BOSS refused to {what} twice: ask a person?",
            rationale=(f"BOSS refused to {what} here twice. That is the policy's answer, so it "
                       "is recorded as the outcome and nobody is asked."))
        board.finish(connection, item_id, actor=COS, lifecycle="failed", token=token,
                     outcome=f"BOSS refused to {what} twice (policy)")
        return "stopped_by_policy"
    # healthy
    state = alerts_state(connection, item)
    said = ("The alert was about BOSS's own monitor, not the app, and it cleared"
            if state == "meta" else "The alert cleared; nothing in the app is failing")
    ledger.record_decision(
        connection, **common, decided_by=COS, chosen="close", options=["close", "ask"],
        question="Is the app still failing?", rationale=f"{said}. Nobody needs to take over.")
    board.finish(connection, item_id, actor=COS, lifecycle="resolved", token=token,
                 outcome=said)
    return "resolved_healthy"


def _last_decided_action(connection: Connection, item_id: str) -> Optional[str]:
    return connection.execute(text(
        "SELECT action FROM agent_act_decisions WHERE work_item_id = :w AND action IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1"), {"w": item_id}).scalar()


def _add_evidence(connection: Connection, rec_id: Optional[str],
                  item: Mapping[str, Any]) -> None:
    """The agents' findings join the backlog item as evidence (never a second piece of work)."""
    if not rec_id:
        return
    found = [m["body"].get("summary") for m in collab.messages(
        connection, item["id"], kinds=("finding", "hypothesis")) if m["sender"] != "boss"]
    note = scrub("; ".join(f for f in found[-3:] if f), 280) or "the agents looked at this too"
    connection.execute(text(
        "UPDATE ambient_recommendations SET evidence = COALESCE(evidence, '[]'::jsonb) || "
        "CAST(:e AS JSONB), evidence_count = COALESCE(evidence_count, 0) + 1, "
        "updated_at = now() WHERE work_item_id = :r"),
        {"e": json.dumps([{"kind": "work_item", "ref": item["id"], "note": note}]),
         "r": rec_id})


def defer_to_backlog(connection: Connection, item_id: str) -> bool:
    """An item onboarding's backlog already proposes is left to it: dismissed with the reason,
    its findings added to the backlog item. True when it was. Never while a person holds it."""
    item = board.get(connection, item_id)
    if item is None or item["lifecycle"] in board.TERMINAL or item.get("lease_kind") == "person":
        return False
    if not backlog_twin(connection, item):
        return False
    if item["lifecycle"] == "needs_you":
        return False   # its card is re-judged by reconsider, which withdraws it with a note
    if item["lifecycle"] != "proposed":
        board._transition(connection, board._must(connection, item_id),  # noqa: SLF001
                          "proposed", actor=COS, extra={"deferred": "onboarding backlog"})
    _settle_backlog(connection, item)
    return True


def _settle_backlog(connection: Connection, item: Mapping[str, Any], *,
                    card_id: Optional[str] = None, proposal_id: Optional[str] = None) -> str:
    token = _take_from_agent(connection, item["id"])
    twin = backlog_twin(connection, item) or {}
    title = clean_text(twin.get("title"), 80) or "the onboarding backlog"
    _add_evidence(connection, twin.get("work_item_id"), item)
    klass = tc.task_class(item["task_class"])
    ledger.record_decision(
        connection, project_id=item["project_id"], work_item_id=item["id"],
        task_class=klass.key, decided_by=COS, chosen="observe", action="observe",
        question=f"Work on this {klass.title.lower()} now?", options=["observe", "work"],
        reversibility="reversible", undo={"action": "reopen", "work_item_id": item["id"]},
        evidence=item_evidence(connection, item["id"], card_id), proposal_id=proposal_id,
        rationale=(f"Onboarding's backlog already proposes “{title}”, and the person decides it "
                   "there. What the agents found is added to that item as evidence."))
    board.finish(connection, item["id"], actor=COS, lifecycle="dismissed",
                 token=token, outcome=f"Left to the onboarding backlog: {title}")
    return "left_to_backlog"


# ── carrying one answer to every entry ───────────────────────────────────────────────────────

def carry(connection: Connection, settings: Any, decision: Mapping[str, Any],
          entry: Mapping[str, Any], actor: str, verdict_word: str,
          note: Optional[str] = None) -> Optional[str]:
    """A person answered the card: carry it to one (non-hand-off) entry's item. Idempotent: an
    item no longer waiting on this card is left alone."""
    item = board.get(connection, str(entry.get("work_item_id") or ""))
    if (item is None or item["lifecycle"] != "needs_you"
            or item.get("needs_you_decision_id") != decision["id"]):
        return None
    approve = verdict_word == "approve"
    kind = entry_kind(entry)
    if kind == "squad":
        from .squad_turns import person_answered

        person_answered(connection, item["id"], actor=actor, approve=approve,
                        rationale=note or "", entry=entry)
        return "squad"
    if kind == "incident":
        if not approve:
            board.answered(connection, item["id"], actor=actor, to="dismissed")
            return "incident:closed"
        from ..incident_squad import on_needs_you_answered

        on_needs_you_answered(connection, settings, {
            "id": decision["id"], "project_id": item["project_id"],
            "recommendation": {**dict(entry), "action": "incident_squad.mitigate",
                               "mitigation": "escalate", "work_item_id": item["id"]}},
            actor, "approve", note)
        return "incident"
    if kind == "budget":
        return carry_budget(connection, item, entry, actor=actor, verdict_word=verdict_word,
                            note=note)
    if approve and entry.get("grant"):
        # "Grant access" DOES grant it: the skill goes to the asking agent's role (the one grant,
        # role_skills), as the person who approved — then the squad goes on, now able to look.
        grant_access(connection, item, entry, actor=actor)
        board.answered(connection, item["id"], actor=actor)
        return "question:granted"
    if approve:
        # "Close it: expected": the person says what the squad was stuck on is expected — the
        # item ends resolved, in their name, with that said on the board.
        collab.post(connection, item["id"], kind="finding", sender="boss", body={
            "summary": f"{person(actor)} closed it as expected" + (f": {clean_text(note, 200)}"
                                                                    if note else "")})
        board.answered(connection, item["id"], actor=actor, to="resolved")
        return "question:closed"
    board.answered(connection, item["id"], actor=actor)
    board.take_over(connection, item["id"], actor=actor, reason="took it from Needs you")
    return "question"


def carry_budget(connection: Connection, item: Mapping[str, Any], entry: Mapping[str, Any], *,
                 actor: str, verdict_word: str, note: Optional[str] = None) -> str:
    """A spent-budget card answered. approve: the item's budget rises by the step, in the
    person's name, and it is back with its squad, owed the next turn (answered_at); reject: it
    is closed, resolved in their name; anything else ("I'll take it"): it is theirs."""
    from .squad_turns import BUDGET_STEP_USD_MICROS, budget_of

    who = person(actor)
    if verdict_word == "approve":
        step = int(entry.get("step_usd_micros") or BUDGET_STEP_USD_MICROS)
        raised = budget_of(item) + step
        connection.execute(text(
            "UPDATE work_items SET squad_budget_usd_micros = :b WHERE id = :w"),
            {"b": raised, "w": item["id"]})
        collab.post(connection, item["id"], kind="finding", sender="boss", body={
            "summary": f"{who} gave the squad {_money(step)} more (budget now {_money(raised)})"})
        board.answered(connection, item["id"], actor=actor)
        return "budget:raised"
    if verdict_word == "reject":
        collab.post(connection, item["id"], kind="finding", sender="boss", body={
            "summary": f"{who} closed it" + (f": {clean_text(note, 200)}" if note else "")})
        board.answered(connection, item["id"], actor=actor, to="resolved")
        return "budget:closed"
    board.answered(connection, item["id"], actor=actor)
    board.take_over(connection, item["id"], actor=actor, reason="took it from Needs you")
    return "budget:taken"


def grant_access(connection: Connection, item: Mapping[str, Any], entry: Mapping[str, Any], *,
                 actor: str) -> bool:
    """Give the asking agent the skill its question asked for. A grant that cannot be made is
    said on the board (the squad reads it), never silently dropped. True when granted."""
    from ._common import party_id
    from ..skill_definitions import assign_skill_to_role

    skill, asker = str(entry.get("grant") or ""), str(entry.get("asked_by") or "")
    try:
        with connection.begin_nested():
            assign_skill_to_role(connection, project_id=item["project_id"], actor=person(actor),
                                 role_key=party_id(asker), skill_id=skill)
        said, ok = f"{person(actor)} granted {skill} to {asker}", True
    except Exception as exc:  # noqa: BLE001 — a refused grant is reported, not raised
        said, ok = f"Could not grant {skill} to {asker}: {clean_text(str(exc), 200)}", False
    collab.post(connection, item["id"], kind="finding", sender="boss", body={"summary": said})
    return ok


#: An entry's own answer keys, as the verdict its carrier reads ("take" is request-changes).
ENTRY_ANSWERS = {"approve": "approve", "reject": "reject", "take": "revise", "hold": "reject"}


def entry_options(entry: Mapping[str, Any]) -> list[dict[str, str]]:
    """One entry's own answers, as a card asking only it would offer them."""
    return card_options([entry])


def answer_entry(connection: Connection, settings: Any, decision_id: str, work_item_id: str,
                 *, answer: str, actor: str, note: Optional[str] = None) -> dict[str, Any]:
    """ONE ENTRY OF THE PROJECT'S CARD, ANSWERED ON ITS OWN ("Give it $1 more" on one spent
    budget, "Close it" on the other). Carried by its own kind (`carry`), then it leaves the card;
    the card is narrowed, or closed once nothing is left. A Chief-of-Staff hand-off is answered
    on the card as a whole (chief_of_staff.carry_card). Raises LookupError / ValueError."""
    if answer not in ENTRY_ANSWERS:
        raise ValueError(f"answer is one of {', '.join(ENTRY_ANSWERS)}")
    row = connection.execute(text(
        "SELECT id, project_id, question, recommendation, options, status, created_at "
        "FROM decisions WHERE id = :d FOR UPDATE"), {"d": decision_id}).mappings().first()
    if row is None or (row["recommendation"] or {}).get("card") != CARD:
        raise LookupError("no such always-on card")
    if row["status"] not in _open_statuses():
        raise ValueError("this card is already answered")
    card = dict(row)
    rec = dict(card["recommendation"] or {})
    batch = list(rec.get("batch") or [])
    entry = next((e for e in batch if str(e.get("work_item_id")) == work_item_id), None)
    if entry is None:
        raise LookupError("that work is not on this card")
    if entry_kind(entry) == "handoff":
        raise ValueError("a hand-off is answered on the card as a whole")
    keys = {o["key"] for o in entry_options(entry)}
    if answer not in keys:
        raise ValueError(f"this entry's answers are {', '.join(sorted(keys))}")
    carried = carry(connection, settings, card, entry, person(actor), ENTRY_ANSWERS[answer],
                    note)
    rest = [e for e in batch if str(e.get("work_item_id")) != work_item_id]
    emit(connection, str(card["project_id"]), "ambient.entry_answered", person(actor),
         "decision", decision_id, {"work_item_id": work_item_id, "answer": answer,
                                    "carried": carried})
    if not rest:
        from ..run_decisions import _withdraw_with_note

        _withdraw_with_note(connection, decision_id, str(card["project_id"]),
                            "Answered entry by entry.", actor=person(actor),
                            why={"why": "answered entry by entry", "items": [work_item_id]})
        return {"carried": carried, "card": "closed"}
    connection.execute(text(
        "UPDATE decisions SET recommendation = CAST(:r AS JSONB), question = :q, "
        "options = CAST(:o AS JSONB), updated_at = now() WHERE id = :d"),
        {"r": json.dumps(_recommendation(rest, base=rec), default=str),
         "q": clean_text(card_question(rest), 300), "o": json.dumps(card_options(rest)),
         "d": decision_id})
    return {"carried": carried, "card": "narrowed", "left": len(rest)}


def carry_card(connection: Connection, settings: Any, decision: Mapping[str, Any], actor: str,
               verdict_word: str, note: Optional[str] = None) -> list[str]:
    rec = dict(decision.get("recommendation") or {})
    out = []
    for e in rec.get("batch") or []:
        if entry_kind(e) == "handoff":
            continue
        took = carry(connection, settings, decision, e, actor, verdict_word, note)
        if took:
            out.append(took)
    return out


__all__ = ["ask", "place", "park", "parked", "open_card", "raise_card", "join_card",
           "card_question", "card_options", "squad_entry", "question_entry", "incident_entry",
           "subject_words", "dependency_words", "place_words", "action_words", "scrub",
           "verdict", "settle", "legacy_entry", "rewords", "backlog_twin", "defer_to_backlog",
           "refused_launches", "alerts_state", "is_meta_alert", "carry", "carry_card",
           "WITHDRAW_WHY", "RAW_ID", "MAX_REFUSED_LAUNCHES", "CARD", "answer_entry",
           "entry_options", "budget_entry", "ask_budget", "quoted", "ENTRY_ANSWERS"]
