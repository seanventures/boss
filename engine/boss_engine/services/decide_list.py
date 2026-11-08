"""WHERE PEOPLE DECIDE: the compliance boundary, as ONE list (Govern › Where people decide;
docs/product/wireframes/agentic-factory/1-where-people-decide.html and AUDIT.md §1).

Agents act on their own. Only the seven kinds of decision on this list wait for a person, and
an eighth — changing the list — is fixed:

  1 production deploy     2 merge to the default branch   3 spend above a cap
  4 secrets and creds     5 talking to the outside        6 deleting data
  7 who can do what       8 changing this list (fixed: Owner + passkey)

THIS MODULE IS THE ONE SOURCE OF TRUTH FOR THE LIST. Everything that draws or enforces the
boundary reads it from here:

  * autonomy.IRREVERSIBLE (Full auto's backstop) is compiled from each entry's `words`;
  * ambient/task_classes.ALWAYS_A_PERSON (the always-on agents' boundary) is each entry's
    `actions`;
  * DecisionService.transition asks `refuse_approval` before it records an approval, so a quiet
    window and a named decider/backup really hold a decision, not only show on a card;
  * autonomy.load_policy clamps Full auto's threshold to the Spend card's cap;
  * the console's cards (GET /v1/decide) render `LIST` and the stored rules.

A CARD CAN MAKE A RULE STRICTER, NEVER LOOSER THAN THE BACKSTOP (`validate`): every entry stays
"always a person"; Full auto never applies; the spend cap never rises above the backstop's
$500; a passkey or a typed name an entry requires is never dropped; a decider is always a
person or an approval seat, never an agent; the 8th card cannot be edited at all.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

#: The backstop's spend ceiling (Full auto's default threshold since it shipped). A Spend card
#: may lower it; nothing raises it.
BACKSTOP_SPEND_USD = 500.0
#: How long a decision waits for the named decider before the backup may answer it.
DEFAULT_BACKUP_AFTER_MINUTES = 120
#: The longest a "backup after" may be (a week): longer is the same as no backup.
MAX_BACKUP_AFTER_MINUTES = 7 * 24 * 60
#: Quiet windows per card, at most.
MAX_WINDOWS = 6
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
STEP_UPS = ("", "passkey", "typed_name")
INSTALL = "install"


@dataclass(frozen=True)
class Boundary:
    key: str
    title: str
    #: A Phosphor icon name (the card's glyph).
    icon: str
    #: The default rule, in a few words (the card's second line).
    rule: str
    #: Why it stays with a person, one line.
    why: str
    #: Words in a gate's own text that put it on this entry. Their union is
    #: autonomy.IRREVERSIBLE: Full auto never approves a gate that says any of them.
    words: tuple[str, ...]
    #: The ambient catalogue's actions that are this entry (never a level's call).
    actions: tuple[str, ...]
    #: `recommendation.action` values of decisions that are this entry.
    decision_actions: tuple[str, ...]
    #: The approval seat that decides by default (identity seats); vacant = the Owners.
    seat: str
    #: What an agent may still do on its own around it (read-only, as Ops › Approval rules).
    agents_may: str
    #: The step a person's answer always needs: "passkey" | "typed_name" | "".
    step_up: str = ""
    #: Where it can be set: the install, and (when True) per project.
    per_project: bool = True
    #: Whether a person's approval delegate (Account › Approve on my behalf) may ever answer it.
    delegate: str = "never"
    fixed: bool = False


LIST_KINDS: tuple[Boundary, ...] = (
    Boundary(
        "prod_deploy", "Production deploy", "rocket-launch",
        "Always a person", "Can't be taken back: customers see it.",
        words=("deploy", "deploys", "deployment", "ship", "release", "production", r"prod\b"),
        actions=("deploy_prod",),
        decision_actions=("review_deployment", "deploy_stack", "deploy_host"),
        seat="release_manager",
        agents_may="Prepare the deploy, run preflight, roll staging back alone; wait at the button",
        delegate="never"),
    Boundary(
        "merge", "Merge to the default branch", "git-merge",
        "Always a person · after review + tests", "It is what ships next. Self-fixes to BOSS count too.",
        words=("merge", "merges", "merging"),
        actions=("merge",),
        decision_actions=("review_pull_request", "github.community.merge_authorization",
                          "approve_change_set", "self_fix.install"),
        seat="cto",
        agents_may="Build, review, test and open the PR; merge into any other branch alone",
        delegate="release_branch"),
    Boundary(
        "spend", "Spend above a cap", "currency-dollar",
        "Above $500 a run or a day", "Money spent can't be recalled. Under the cap, FinOps decides.",
        words=("pay", "payment", "charge", "purchase", "buy", "refund", "transfer"),
        actions=("pay",),
        decision_actions=("approve_initiative_budget", "create_budget_reservation",
                          "reserve_provider_budget", "reserve_budget_before_deployment",
                          "budget_denied", "budget_deferred", "increase_budget_or_defer",
                          "increase_allocation_or_defer", "raise_cap_or_wait_for_headroom",
                          "budget_tier2_frozen", "increase_budget_or_defer_work",
                          "approve_budget_increase_or_accept_degraded_mode"),
        seat="ceo",
        agents_may="Spend under the cap; switch to a cheaper model at the limit; pause runaway work",
        delegate="never"),
    Boundary(
        "secrets", "Secrets and credentials", "key",
        "Add, reveal, rotate or grant a secret",
        "Only a person can hand over a key. Agents use secrets by name and never see them.",
        words=("rotate", "credential", "secret", "key"),
        actions=(),
        decision_actions=("provide_github_token", "provide_openai_api_key"),
        seat="ceo", step_up="passkey", per_project=False,
        agents_may="Use a secret by its name; never read, copy or rotate it",
        delegate="never"),
    Boundary(
        "outside", "Talking to the outside", "megaphone",
        "Email, post or page anyone outside the team", "Can't be unsent. Messages to your own team don't count.",
        words=("send", "email", "notify", "post", "tweet", "announce", "publish", "customer",
               "external"),
        actions=("send_external",),
        decision_actions=("configure_supported_channel", "upsert_comms_channel_policy"),
        seat="ceo",
        agents_may="Message your own team; draft the outside message for you",
        delegate="never"),
    Boundary(
        "delete", "Deleting data", "trash",
        "Drop, purge, retire a host, forget facts", "Gone is gone. Agents may archive; only a person deletes.",
        words=("delete", "deletes", "destroy", "drop", "purge", "wipe", "teardown", "terminate"),
        actions=(),
        decision_actions=("infra_operation", "self_fix.remove", "delete_project"),
        seat="ceo", step_up="typed_name", per_project=False,
        agents_may="Archive, quarantine, mute; never delete",
        delegate="never"),
    Boundary(
        "access", "Who can do what", "identification-card",
        "Roles, invites, keys, agent powers", "An agent never raises its own powers or anyone else's.",
        words=("grant", "revoke", "permission"),
        actions=(),
        decision_actions=("grant_builder_authority", "role.autonomy_change",
                          "activate_authored_skill"),
        seat="cao", step_up="passkey", per_project=False,
        agents_may="Propose a grant with its evidence; never raise a level itself",
        delegate="never"),
)
CHANGING_THE_LIST = Boundary(
    "change_list", "Changing this list", "lock-simple",
    "Owner · passkey · always", "So no agent can move the line it works under.",
    words=(), actions=(), decision_actions=(), seat="owner", step_up="passkey",
    per_project=False, agents_may="Nothing: only a person changes the list", fixed=True)
#: The seven, then the fixed eighth: what the console draws, in order.
LIST: tuple[Boundary, ...] = LIST_KINDS + (CHANGING_THE_LIST,)
BY_KEY: dict[str, Boundary] = {b.key: b for b in LIST}
KINDS = tuple(b.key for b in LIST_KINDS)

#: Full auto's backstop (autonomy.IRREVERSIBLE): every entry's words. Deliberately broad — a
#: false positive costs a person ten seconds; a false negative deploys something nobody approved.
IRREVERSIBLE = re.compile(r"\b(" + "|".join(w for b in LIST_KINDS for w in b.words) + r")\b",
                          re.I)
_WORDS = {b.key: re.compile(r"\b(" + "|".join(b.words) + r")\b", re.I) for b in LIST_KINDS}
#: The ambient actions no autonomy level may take (ambient/task_classes.ALWAYS_A_PERSON).
AMBIENT_ACTIONS: frozenset[str] = frozenset(a for b in LIST_KINDS for a in b.actions)
_BY_ACTION = {a: b.key for b in LIST_KINDS for a in b.decision_actions}
_NOT_PROD = re.compile(r"\b(staging|preview|dev|development|test|sandbox)\b", re.I)
_PROD = re.compile(r"\b(prod|production|live)\b", re.I)


def on_the_list(action_key: str) -> bool:
    """Is this ambient catalogue action one only a person takes?"""
    return str(action_key or "") in AMBIENT_ACTIONS


# ── which entry a decision is ───────────────────────────────────────────────────────────────

def _rec(decision: Mapping[str, Any]) -> dict[str, Any]:
    rec = decision.get("recommendation")
    if isinstance(rec, str):
        try:
            rec = json.loads(rec)
        except ValueError:
            rec = None
    return rec if isinstance(rec, dict) else {}


def classify(decision: Mapping[str, Any]) -> Optional[str]:
    """The list entry a decision is, or None (agents decide it). By the producer's own action
    first, then by the gate's words — the same words Full auto refuses on."""
    rec = _rec(decision)
    question = str(decision.get("question") or "")
    env = " ".join(str(rec.get(k) or "") for k in ("environment", "target_environment", "env",
                                                  "capability"))
    action = str(rec.get("action") or "")
    reasons = {str(rec.get(k) or "") for k in ("reason_code", "reason")}
    kind = _BY_ACTION.get(action) or next((_BY_ACTION[r] for r in reasons if r in _BY_ACTION),
                                          None)
    if kind is None and str(rec.get("capability") or "") in ("deploy", "redeploy"):
        kind = "prod_deploy"
    if kind is None:
        for b in LIST_KINDS:
            if _WORDS[b.key].search(question):
                kind = b.key
                break
    if kind == "prod_deploy":
        # A staging or preview deploy is not on the list: agents ship it after green checks.
        said = f"{question} {env}"
        if _NOT_PROD.search(said) and not _PROD.search(said):
            return None
    return kind


# ── the rules: defaults, stored, effective ──────────────────────────────────────────────────

def default_rule(kind: str) -> dict[str, Any]:
    b = BY_KEY[kind]
    rule: dict[str, Any] = {
        "always_person": True, "full_auto": False,
        "decider": {"seat": b.seat}, "backup": None,
        "backup_after_minutes": DEFAULT_BACKUP_AFTER_MINUTES,
        "quiet": [], "step_up": b.step_up, "approvers": 1,
    }
    if kind == "spend":
        rule["cap_usd"] = BACKSTOP_SPEND_USD
        rule["cap_per"] = "run_or_day"
    return rule


class RuleLooser(ValueError):
    """A change that would make the list looser than the backstop in code."""


def _hhmm(value: Any) -> str:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise RuleLooser(f"a time is HH:MM, not {str(value)[:20]!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _day(value: Any) -> str:
    d = str(value or "").strip().lower()[:3]
    if d not in DAYS:
        raise RuleLooser(f"a day is one of {', '.join(DAYS)}")
    return d


def _tz(value: Any) -> str:
    name = str(value or "UTC").strip() or "UTC"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception:  # noqa: BLE001 — an unknown zone is refused, never guessed
        raise RuleLooser(f"unknown time zone {name[:60]!r}") from None
    return name


def _window(w: Any) -> dict[str, str]:
    if not isinstance(w, Mapping):
        raise RuleLooser("a quiet window is {from_day, from_time, to_day, to_time, tz}")
    return {"from_day": _day(w.get("from_day")), "from_time": _hhmm(w.get("from_time")),
            "to_day": _day(w.get("to_day")), "to_time": _hhmm(w.get("to_time")),
            "tz": _tz(w.get("tz"))}


def _person(value: Any, what: str) -> Optional[dict[str, str]]:
    if value in (None, "", {}):
        return None
    if not isinstance(value, Mapping):
        raise RuleLooser(f"{what} is a person or an approval seat")
    if value.get("seat"):
        from ..identity.approvals import known_seats

        seat = str(value["seat"])
        if seat not in known_seats() and seat != "owner":
            raise RuleLooser(f"{what}: no approval seat named {seat!r}")
        return {"seat": seat}
    pid = str(value.get("person") or "").strip()
    if not pid:
        raise RuleLooser(f"{what} is a person or an approval seat")
    kind = str(value.get("kind") or "user")
    if kind != "user" or pid.startswith(("agt_", "sva_", "sys_", "agent:", "policy:", "autonomy:")):
        # An agent never decides an entry on the list, and never names itself the backup.
        raise RuleLooser(f"{what} must be a person: an agent never decides what is on the list")
    return {"person": pid[:120], "name": str(value.get("name") or pid)[:120]}


def validate(kind: str, rule: Mapping[str, Any], *, ceiling: Optional[Mapping[str, Any]] = None
             ) -> dict[str, Any]:
    """The rule, normalised, or RuleLooser. STRICTER ONLY: nothing here can make an entry looser
    than the backstop (`default_rule`), and a project's rule nothing looser than the install's
    (`ceiling`)."""
    if kind not in BY_KEY:
        raise RuleLooser(f"{kind!r} is not on the list")
    b = BY_KEY[kind]
    if b.fixed:
        raise RuleLooser("Changing this list is fixed: an Owner, with a passkey, always")
    base = default_rule(kind)
    if rule.get("always_person") is False:
        raise RuleLooser(f"{b.title} always waits for a person; it cannot come off the list")
    if rule.get("full_auto"):
        raise RuleLooser("Full auto never approves anything on the list, at any cost")
    out = dict(base)
    decider = _person(rule.get("decider"), "Who decides") if "decider" in rule else None
    out["decider"] = decider or base["decider"]
    out["backup"] = _person(rule.get("backup"), "The backup") if rule.get("backup") else None
    if out["backup"] and out["backup"] == out["decider"]:
        raise RuleLooser("the backup is someone other than who decides")
    try:
        after = int(rule.get("backup_after_minutes", base["backup_after_minutes"]))
    except (TypeError, ValueError):
        raise RuleLooser("backup_after_minutes is a number of minutes") from None
    out["backup_after_minutes"] = max(5, min(after, MAX_BACKUP_AFTER_MINUTES))
    quiet = rule.get("quiet") or []
    if not isinstance(quiet, list) or len(quiet) > MAX_WINDOWS:
        raise RuleLooser(f"at most {MAX_WINDOWS} quiet windows")
    out["quiet"] = [_window(w) for w in quiet]
    step = str(rule.get("step_up", base["step_up"]) or "")
    if step not in STEP_UPS:
        raise RuleLooser("step_up is passkey, typed_name or nothing")
    if base["step_up"] and step != base["step_up"] and not (base["step_up"] == "typed_name"
                                                            and step == "passkey"):
        raise RuleLooser(f"{b.title} always asks for {'a passkey' if base['step_up'] == 'passkey' else 'the typed name'}")
    out["step_up"] = step
    try:
        approvers = int(rule.get("approvers", 1) or 1)
    except (TypeError, ValueError):
        approvers = 1
    if approvers < 1:
        raise RuleLooser("at least one person decides")
    out["approvers"] = min(approvers, 3)
    if kind == "spend":
        try:
            cap = float(rule.get("cap_usd", base["cap_usd"]))
        except (TypeError, ValueError):
            raise RuleLooser("the cap is a dollar amount") from None
        if cap > BACKSTOP_SPEND_USD:
            raise RuleLooser(f"the cap can be lowered, never raised above the ${BACKSTOP_SPEND_USD:,.0f} "
                             "backstop")
        if cap <= 0:
            raise RuleLooser("the cap is above $0")
        out["cap_usd"] = round(cap, 2)
        per = str(rule.get("cap_per") or base["cap_per"])
        out["cap_per"] = per if per in ("run", "day", "run_or_day") else "run_or_day"
    if ceiling:
        # A project adds to the install's rule; it never takes anything away.
        if kind == "spend" and out.get("cap_usd", 0) > float(ceiling.get("cap_usd") or BACKSTOP_SPEND_USD):
            raise RuleLooser("a project's cap is at most the install's")
        if out["approvers"] < int(ceiling.get("approvers") or 1):
            raise RuleLooser("a project adds approvers; it never removes the install's")
        if ceiling.get("step_up") and not out["step_up"]:
            raise RuleLooser("a project keeps the install's passkey")
    return out


def _table_ready(connection: Any) -> bool:
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            connection.execute(text("SELECT 1 FROM decide_rules LIMIT 1"))
        return True
    except Exception:  # noqa: BLE001 — an install before this schema: the defaults
        return False


def stored(connection: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """{scope: {kind: rule}} as saved (scope 'install' or a project id)."""
    from sqlalchemy import text

    if connection is None or not _table_ready(connection):
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for r in connection.execute(text("SELECT scope, kind, rule FROM decide_rules")).mappings():
        rule = r["rule"] if isinstance(r["rule"], dict) else json.loads(r["rule"] or "{}")
        out.setdefault(str(r["scope"]), {})[str(r["kind"])] = rule
    return out


def install_rule(connection: Any, kind: str, *, rules: Optional[Mapping] = None) -> dict[str, Any]:
    rules = stored(connection) if rules is None else rules
    saved = (rules.get(INSTALL) or {}).get(kind)
    if not saved:
        return default_rule(kind)
    try:
        return validate(kind, saved)
    except RuleLooser:
        # A row that no longer validates (edited by hand) never loosens the list.
        return default_rule(kind)


def effective(connection: Any, kind: str, project_id: Optional[str] = None, *,
              rules: Optional[Mapping] = None) -> dict[str, Any]:
    """The rule in force for one project: the install's, plus whatever the project added (a
    project only ever adds: more windows, a lower cap, a second approver, its own decider)."""
    rules = stored(connection) if rules is None else rules
    base = install_rule(connection, kind, rules=rules)
    extra = (rules.get(str(project_id)) or {}).get(kind) if project_id else None
    if not extra or not BY_KEY[kind].per_project:
        return {**base, "scope": INSTALL}
    try:
        mine = validate(kind, extra, ceiling=base)
    except RuleLooser:
        return {**base, "scope": INSTALL}
    merged = dict(base)
    merged["quiet"] = base["quiet"] + [w for w in mine["quiet"] if w not in base["quiet"]]
    merged["approvers"] = max(base["approvers"], mine["approvers"])
    if mine.get("decider") != default_rule(kind)["decider"]:
        merged["decider"] = mine["decider"]
    if mine.get("backup"):
        merged["backup"] = mine["backup"]
        merged["backup_after_minutes"] = mine["backup_after_minutes"]
    if kind == "spend":
        merged["cap_usd"] = min(base["cap_usd"], mine["cap_usd"])
    merged["scope"] = str(project_id)
    return merged


# ── quiet windows ────────────────────────────────────────────────────────────────────────────

def _minute_of_week(day: str, hhmm: str) -> int:
    h, m = hhmm.split(":")
    return DAYS.index(day) * 1440 + int(h) * 60 + int(m)


def in_window(window: Mapping[str, str], at: datetime) -> Optional[datetime]:
    """When the window ends (UTC) if `at` is inside it, else None."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(window.get("tz") or "UTC")
    local = at.astimezone(zone)
    now_m = local.weekday() * 1440 + local.hour * 60 + local.minute
    start = _minute_of_week(window["from_day"], window["from_time"])
    end = _minute_of_week(window["to_day"], window["to_time"])
    week = 7 * 1440
    inside = (start <= now_m < end) if start < end else (now_m >= start or now_m < end)
    if start == end:
        inside = False
    if not inside:
        return None
    ahead = (end - now_m) % week or week
    ends = local.replace(second=0, microsecond=0) + timedelta(minutes=ahead)
    return ends.astimezone(timezone.utc)


def window_words(w: Mapping[str, str]) -> str:
    cap = lambda d: d[:1].upper() + d[1:]  # noqa: E731
    return f"{cap(w['from_day'])} {w['from_time']} → {cap(w['to_day'])} {w['to_time']}"


def quiet_now(rule: Mapping[str, Any], at: datetime) -> Optional[tuple[dict, datetime]]:
    for w in rule.get("quiet") or []:
        ends = in_window(w, at)
        if ends is not None:
            return dict(w), ends
    return None


# ── the gate: what DecisionService.transition asks before it records an approval ────────────

def _who(person: Optional[Mapping[str, Any]]) -> str:
    if not person:
        return ""
    return str(person.get("name") or person.get("person") or person.get("seat") or "")


def _active(connection: Any, principal_id: str) -> bool:
    from sqlalchemy import text

    if not principal_id.startswith("usr_"):
        return True   # a legacy member id: present while it is named
    try:
        with connection.begin_nested():
            status = connection.execute(text(
                "SELECT status FROM identity_principals WHERE id = :p"), {"p": principal_id}).scalar()
    except Exception:  # noqa: BLE001
        return True
    return status in (None, "active")


def refuse_approval(connection: Any, decision: Mapping[str, Any], approver_ids: Iterable[str],
                    *, at: Optional[datetime] = None) -> Optional[str]:
    """Why this approval must not be recorded now, in the person's words, or None.

    * A QUIET WINDOW holds an entry's decisions: nobody approves a production deploy inside
      "Fri 15:00 → Mon 08:00"; it waits, and says until when.
    * A NAMED DECIDER answers first. The BACKUP may answer once the decision has waited
      `backup_after_minutes`, or at once when the decider is no longer active. Anyone else is
      told who decides.
    """
    kind = classify(decision)
    if kind is None:
        return None
    at = at or datetime.now(timezone.utc)
    rule = effective(connection, kind, str(decision.get("project_id") or "") or None)
    title = BY_KEY[kind].title
    held = quiet_now(rule, at)
    if held is not None:
        w, ends = held
        return (f"{title} is in its quiet window ({window_words(w)}, {w['tz']}). It waits until "
                f"{ends.astimezone(timezone.utc):%a %H:%M} UTC; change the window in Govern › "
                "Where people decide.")
    decider = rule.get("decider") or {}
    if not decider.get("person"):
        backup = rule.get("backup") or {}
        if backup.get("person") and backup["person"] in set(approver_ids):
            waited = _waited_minutes(decision, at)
            if waited is not None and waited < rule["backup_after_minutes"]:
                return (f"{_who(backup)} is the backup for {title}: they answer after it has "
                        f"waited {rule['backup_after_minutes']} min (now {int(waited)} min).")
        return None
    ids = {str(i) for i in approver_ids if i}
    if decider["person"] in ids:
        return None
    backup = rule.get("backup") or {}
    if backup.get("person") and backup["person"] in ids:
        waited = _waited_minutes(decision, at)
        if not _active(connection, decider["person"]):
            return None
        if waited is not None and waited >= rule["backup_after_minutes"]:
            return None
        return (f"{_who(decider)} decides {title} first. You are the backup: you can answer "
                f"after it has waited {rule['backup_after_minutes']} min (now "
                f"{int(waited or 0)} min).")
    tail = f", or {_who(backup)} as the backup" if backup.get("person") else ""
    return (f"{_who(decider)} decides {title}{tail} (Govern › Where people decide).")


def _waited_minutes(decision: Mapping[str, Any], at: datetime) -> Optional[float]:
    created = decision.get("created_at")
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (at - created).total_seconds() / 60)


def spend_cap(connection: Any, project_id: Optional[str]) -> Optional[float]:
    """The Spend card's cap when one was saved (clamps Full auto's threshold); None when the
    card is still the backstop's default."""
    rules = stored(connection)
    if not (rules.get(INSTALL) or {}).get("spend") and not (rules.get(str(project_id)) or {}).get("spend"):
        return None
    return float(effective(connection, "spend", project_id, rules=rules)["cap_usd"])


# ── saving: one write path for the card and for "change a rule in plain words" ─────────────

def save(connection: Any, kind: str, rule: Mapping[str, Any], *, actor: str, scope: str = INSTALL,
         via: str = "card", words: str = "", project_id: Optional[str] = None) -> dict[str, Any]:
    """Validate (stricter only) and store one entry's rule for the install or a project, with
    its audit row and an event. Raises RuleLooser."""
    from sqlalchemy import text

    if not str(actor or "").strip():
        raise RuleLooser("a change to the list has an owner")
    b = BY_KEY.get(kind)
    if b is None:
        raise RuleLooser(f"{kind!r} is not on the list")
    if scope != INSTALL and not b.per_project:
        raise RuleLooser(f"{b.title} is set for the whole install")
    rules = stored(connection)
    ceiling = install_rule(connection, kind, rules=rules) if scope != INSTALL else None
    clean = validate(kind, rule, ceiling=ceiling)
    before = (rules.get(scope) or {}).get(kind)
    connection.execute(text(
        "INSERT INTO decide_rules (scope, kind, rule, updated_by, updated_at) "
        "VALUES (:s, :k, CAST(:r AS JSONB), :a, now()) ON CONFLICT (scope, kind) DO UPDATE "
        "SET rule = EXCLUDED.rule, updated_by = EXCLUDED.updated_by, updated_at = now()"),
        {"s": scope, "k": kind, "r": json.dumps(clean), "a": actor})
    change_id = f"drc_{uuid.uuid4().hex[:16]}"
    connection.execute(text(
        "INSERT INTO decide_rule_changes (id, scope, kind, before, after, actor, via, words) "
        "VALUES (:i, :s, :k, CAST(:b AS JSONB), CAST(:r AS JSONB), :a, :v, :w)"),
        {"i": change_id, "s": scope, "k": kind, "b": json.dumps(before) if before else None,
         "r": json.dumps(clean), "a": actor, "v": via, "w": (words or "")[:4000]})
    if project_id:
        from .core import emit_event

        emit_event(connection, project_id, "decide.rule_changed", actor, "decide_rule", kind,
                   {"kind": kind, "scope": scope, "via": via, "change_id": change_id,
                    "summary": describe_rule(kind, clean)})
    return {"kind": kind, "scope": scope, "rule": clean, "change_id": change_id}


def describe_rule(kind: str, rule: Mapping[str, Any]) -> str:
    """The card's second line, from the rule in force."""
    b = BY_KEY[kind]
    parts = [b.rule if kind != "spend" else
             f"Above ${float(rule.get('cap_usd') or BACKSTOP_SPEND_USD):,.0f} "
             + {"run": "a run", "day": "a day"}.get(str(rule.get("cap_per")), "a run or a day")]
    for w in rule.get("quiet") or []:
        parts.append(f"quiet {window_words(w)}")
    return " · ".join(parts)


# ── reads for the page ───────────────────────────────────────────────────────────────────────

def _decisions(connection: Any, *, hours: int, limit: int = 5000) -> list[dict[str, Any]]:
    from sqlalchemy import text

    rows = connection.execute(text(
        "SELECT id, project_id, question, recommendation, status, approved_by, approved_at, "
        "risk_tier, cost_summary, created_at FROM decisions WHERE created_at > now() - "
        "make_interval(hours => :h) ORDER BY created_at DESC LIMIT :l"),
        {"h": int(hours), "l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def counts(connection: Any, *, hours: int = 24) -> dict[str, dict[str, Any]]:
    """{kind: {n, bars[6]}}: decisions of each entry in the last `hours`, in six buckets."""
    now = datetime.now(timezone.utc)
    out = {k: {"n": 0, "bars": [0] * 6} for k in KINDS}
    for row in _decisions(connection, hours=hours):
        kind = classify(row)
        if kind is None:
            continue
        out[kind]["n"] += 1
        created = row["created_at"]
        if isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (now - created).total_seconds() / 3600
            bucket = 5 - min(5, int(age // (hours / 6)))
            out[kind]["bars"][bucket] += 1
    return out


def trail(connection: Any, kind: str, *, limit: int = 3, days: int = 30) -> list[dict[str, Any]]:
    """The last decisions of this entry and the last changes to its rule, newest first."""
    from sqlalchemy import text

    items: list[dict[str, Any]] = []
    for row in _decisions(connection, hours=days * 24, limit=2000):
        if classify(row) != kind:
            continue
        answered = row["status"] not in ("pending", "delegated")
        items.append({"at": _iso(row.get("approved_at") or row["created_at"]), "type": "decision",
                      "id": row["id"], "project_id": row["project_id"],
                      "who": row.get("approved_by") if answered else None,
                      "status": row["status"], "what": str(row["question"] or "")[:160]})
        if len(items) >= limit * 2:
            break
    if _table_ready(connection):
        for r in connection.execute(text(
                "SELECT id, scope, actor, via, created_at, after FROM decide_rule_changes "
                "WHERE kind = :k ORDER BY created_at DESC LIMIT :l"),
                {"k": kind, "l": limit}).mappings():
            after = r["after"] if isinstance(r["after"], dict) else json.loads(r["after"] or "{}")
            items.append({"at": _iso(r["created_at"]), "type": "change", "id": r["id"],
                          "who": r["actor"], "via": r["via"], "scope": r["scope"],
                          "what": describe_rule(kind, after)})
    items.sort(key=lambda i: i["at"] or "", reverse=True)
    return items[:limit]


def last_change(connection: Any) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    if not _table_ready(connection):
        return None
    r = connection.execute(text(
        "SELECT actor, kind, created_at FROM decide_rule_changes ORDER BY created_at DESC LIMIT 1"
    )).mappings().first()
    return {"at": _iso(r["created_at"]), "who": r["actor"], "kind": r["kind"]} if r else None


def _per_project(rules: Mapping[str, Mapping[str, Any]], kind: str) -> list[str]:
    return sorted(s for s, kinds in rules.items() if s != INSTALL and kind in kinds)


def seats_view(connection: Any, org_id: Optional[str]) -> dict[str, list[dict[str, str]]]:
    """{seat: [{id, name}]} of active holders (identity seats), {} without identity."""
    if not org_id:
        return {}
    try:
        from ..identity import approvals

        with connection.begin_nested():
            held = approvals.holders(connection, org_id)
            names = approvals._names(connection, [p for ids in held.values() for p in ids])  # noqa: SLF001
    except Exception:  # noqa: BLE001 — no identity yet: seats show by name
        return {}
    return {seat: [{"id": p, "name": names.get(p) or p} for p in ids] for seat, ids in held.items()}


def people(connection: Any, org_id: Optional[str]) -> list[dict[str, str]]:
    """Active people who may be named a decider or a backup."""
    from sqlalchemy import text

    if not org_id:
        return []
    try:
        from ..identity.legacy import is_bridge_principal

        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT p.id, p.display_name, u.email FROM identity_principals p LEFT JOIN "
                "identity_users u ON u.principal_id = p.id WHERE p.org_id = :o AND p.kind = 'user' "
                "AND p.status = 'active' AND NOT p.network_bound ORDER BY p.display_name LIMIT 200"),
                {"o": org_id}).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    return [{"id": r["id"], "name": r["display_name"] or r["email"] or r["id"],
             "email": r["email"] or ""} for r in rows if not is_bridge_principal(r["id"])]


SEAT_WORDS = {"ceo": "Owner seat", "cto": "Maintainers", "cao": "Admin seat",
              "release_manager": "Release seat", "vp_agents": "Agents seat", "owner": "Owner"}


def decider_view(decider: Mapping[str, Any], seats: Mapping[str, list[dict[str, str]]]
                 ) -> dict[str, Any]:
    if decider.get("person"):
        return {"kind": "person", "id": decider["person"], "label": _who(decider),
                "people": [{"id": decider["person"], "name": _who(decider)}]}
    seat = str(decider.get("seat") or "ceo")
    holders = list(seats.get(seat) or [])
    return {"kind": "seat", "id": seat, "label": SEAT_WORDS.get(seat, seat),
            "people": holders, "vacant": not holders and seat != "owner"}


def _actor_words(who: str) -> Optional[str]:
    """A trail's actor that is not a person in the roster, in words: a seat by its name, an
    automatic approval as BOSS — never a raw key ("ceo", "autonomy:full_auto")."""
    if not who or who.startswith("usr_"):
        return None
    if who in SEAT_WORDS:
        return SEAT_WORDS[who]
    if who.startswith(("autonomy:", "auto_approval", "policy:", "system")):
        return "BOSS (automatic)"
    return who.replace("_", " ")


def board(connection: Any, *, project_id: Optional[str], org_id: Optional[str],
          viewer: Optional[str] = None) -> dict[str, Any]:
    """GET /v1/decide: the 7 + 1 cards, each with its rule in force, who decides, scope, its 24 h
    count and trail; the people a card may name; the last change to the list."""
    rules = stored(connection)
    seats = seats_view(connection, org_id)
    tally = counts(connection)
    delegations = _viewer_delegations(connection, viewer)
    cards = []
    for b in LIST:
        if b.fixed:
            cards.append({"key": b.key, "title": b.title, "icon": b.icon, "fixed": True,
                          "rule": b.rule, "why": b.why, "line": b.rule,
                          "decider": {"kind": "seat", "id": "owner", "label": "Owner", "people":
                                      seats.get("ceo") or []},
                          "step_up": "passkey", "last_change": last_change(connection),
                          "agents_may": b.agents_may})
            continue
        rule = install_rule(connection, b.key, rules=rules)
        here = effective(connection, b.key, project_id, rules=rules) if project_id else rule
        cards.append({
            "key": b.key, "title": b.title, "icon": b.icon, "fixed": False, "why": b.why,
            "line": describe_rule(b.key, rule), "rule": rule, "here": here,
            "decider": decider_view(rule["decider"], seats),
            "backup": rule.get("backup"),
            "scope": {"per_project": b.per_project, "projects": _per_project(rules, b.key)},
            "count_24h": tally[b.key]["n"], "bars": tally[b.key]["bars"],
            "agents_may": b.agents_may, "step_up": rule["step_up"],
            "delegate": {"allowed": b.delegate, "mine": delegations},
            "full_auto": "never",
            "trail": trail(connection, b.key),
        })
    roster = people(connection, org_id)
    names = {p["id"]: p["name"] for p in roster}
    for card in cards:
        for item in card.get("trail") or []:
            who = str(item.get("who") or "")
            item["who_name"] = names.get(who) or _actor_words(who)
    return {"cards": cards, "people": roster, "backstop": {"spend_usd": BACKSTOP_SPEND_USD}}


def _viewer_delegations(connection: Any, viewer: Optional[str]) -> int:
    from sqlalchemy import text

    if not viewer:
        return 0
    try:
        with connection.begin_nested():
            return int(connection.execute(text(
                "SELECT count(*) FROM identity_approval_delegations WHERE person_principal_id = :p "
                "AND status = 'active' AND expires_at > now()"), {"p": viewer}).scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


# ── the one line, and what each agent did (Agents at work) ─────────────────────────────────

def activity(connection: Any, *, project_id: Optional[str], hours: int = 24) -> dict[str, Any]:
    """{summary: install-wide counts for the last `hours` — handled by agents, asked a person,
    waiting on you (with the oldest open decision) — agents: per area agent of `project_id`,
    what it did alone vs. what waited for a person, and lanes: those, hour by hour}."""
    from sqlalchemy import text

    from .ambient import task_classes as tc

    h = {"h": int(hours)}
    alone_acts = int(connection.execute(text(
        "SELECT count(*) FROM agent_act_decisions WHERE created_at > now() - make_interval(hours => :h) "
        "AND decided_by NOT LIKE 'person:%'"), h).scalar() or 0)
    auto = int(connection.execute(text(
        "SELECT count(*) FROM decisions WHERE status = 'approved' AND approved_at > now() - "
        "make_interval(hours => :h) AND (approved_by LIKE 'autonomy:%' OR approved_by = "
        "'auto_approval')"), h).scalar() or 0)
    asked = int(connection.execute(text(
        "SELECT count(*) FROM decisions WHERE created_at > now() - make_interval(hours => :h) "
        "AND NOT (status = 'approved' AND (approved_by LIKE 'autonomy:%' OR approved_by = "
        "'auto_approval'))"), h).scalar() or 0)
    open_rows = connection.execute(text(
        "SELECT id, project_id, question, created_at FROM decisions WHERE status IN "
        "('pending', 'delegated') ORDER BY created_at ASC LIMIT 1")).mappings().first()
    waiting = int(connection.execute(text(
        "SELECT count(*) FROM decisions WHERE status IN ('pending', 'delegated')")).scalar() or 0)
    summary = {"hours": hours, "handled_by_agents": alone_acts + auto, "asked_a_person": asked,
               "waiting": waiting,
               "oldest": ({"id": open_rows["id"], "project_id": open_rows["project_id"],
                           "question": str(open_rows["question"] or "")[:120],
                           "at": _iso(open_rows["created_at"])} if open_rows else None)}
    agents: dict[str, dict[str, Any]] = {}
    if project_id:
        owner_of = {k: c.owner_role for k, c in tc.TASK_CLASSES.items()}
        rows = connection.execute(text(
            "SELECT task_class, decided_by, created_at FROM agent_act_decisions WHERE project_id = :p "
            "AND created_at > now() - make_interval(hours => :h)"),
            {"p": project_id, "h": int(hours)}).mappings().all()
        now = datetime.now(timezone.utc)
        for r in rows:
            role = owner_of.get(str(r["task_class"] or "")) or _role_of(str(r["decided_by"]))
            a = agents.setdefault(role, {"role": role, "alone": 0, "waited": 0,
                                         "lane": [[0, 0] for _ in range(24)]})
            person = str(r["decided_by"]).startswith("person:")
            a["waited" if person else "alone"] += 1
            created = r["created_at"]
            if isinstance(created, datetime):
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                slot = 23 - min(23, int((now - created).total_seconds() // 3600 * 24 // hours))
                a["lane"][slot][1 if person else 0] += 1
        for role in sorted({c.owner_role for c in tc.TASK_CLASSES.values()}):
            agents.setdefault(role, {"role": role, "alone": 0, "waited": 0,
                                     "lane": [[0, 0] for _ in range(24)]})
    ordered = sorted(agents.values(), key=lambda a: (-(a["alone"] + a["waited"]), a["role"]))
    return {"summary": summary, "agents": ordered}


def _role_of(decided_by: str) -> str:
    kind, _, who = decided_by.partition(":")
    return who or kind or "agent"


# ── the 30-day replay ────────────────────────────────────────────────────────────────────────

def replay(connection: Any, edits: Sequence[Mapping[str, Any]], *, days: int = 30
           ) -> list[dict[str, Any]]:
    """For each edit, what it would have done to the last `days` of real decisions."""
    rows = _decisions(connection, hours=days * 24, limit=5000)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        k = classify(row)
        if k:
            by_kind.setdefault(k, []).append(row)
    out = []
    for e in edits:
        kind = str(e.get("kind") or "")
        fld = str(e.get("field") or "")
        value = e.get("value")
        mine = by_kind.get(kind, [])
        n, words = 0, ""
        title = BY_KEY[kind].title if kind in BY_KEY else kind
        noun = {"prod_deploy": "deploy", "merge": "merge", "spend": "spend decision",
                "secrets": "secret", "outside": "outside message", "delete": "deletion",
                "access": "access change"}.get(kind, "decision")
        if fld == "quiet" and isinstance(value, Mapping):
            try:
                w = _window(value)
                for row in mine:
                    created = row["created_at"]
                    if isinstance(created, datetime):
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        if in_window(w, created) is not None:
                            n += 1
                words = f"{n} {noun}{'s' if n != 1 else ''} would have waited until {w['to_day'].title()} {w['to_time']}"
            except RuleLooser as refused:
                words = str(refused)
        elif fld in ("decider", "backup") and isinstance(value, Mapping):
            n = len(mine)
            who = _who(value)
            words = (f"{n} {noun}{'s' if n != 1 else ''} would have gone to {who}" if fld == "decider"
                     else f"{who} would have been the backup on {n} {noun}{'s' if n != 1 else ''}")
        elif fld == "cap_usd":
            try:
                cap = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cap = BACKSTOP_SPEND_USD
            for row in rows:
                summary = row.get("cost_summary") or {}
                if isinstance(summary, str):
                    try:
                        summary = json.loads(summary)
                    except ValueError:
                        summary = {}
                cost = None
                for key in ("usd", "amount_usd", "total_usd", "cost_usd"):
                    if isinstance(summary, Mapping) and key in summary:
                        try:
                            cost = float(summary[key])
                        except (TypeError, ValueError):
                            cost = None
                        break
                if cost is not None and cost > cap:
                    n += 1
            words = f"{n} decision{'s' if n != 1 else ''} over ${cap:,.0f} would have asked a person"
        else:
            n = len(mine)
            words = f"{n} {title.lower()} decision{'s' if n != 1 else ''} in {days} days"
        out.append({"kind": kind, "field": fld, "n": n, "words": words})
    return out


# ── "Change a rule in plain words": Helm's structured turn ──────────────────────────────────

RULE_CHANGE_SCHEMA: dict[str, Any] = {
    "properties": {
        "edits": {"type": "array", "description": (
            "each change to a card on the list: {\"kind\": one of list[].key, \"field\": "
            "\"quiet\"|\"decider\"|\"backup\"|\"cap_usd\"|\"approvers\", \"value\": …, \"scope\": "
            "\"install\" or a projects[].id, \"says\": <the change in plain words, one sentence>}. "
            "quiet value: {from_day, from_time, to_day, to_time, tz} (days mon..sun, HH:MM, the "
            "person's tz unless they named one). decider/backup value: {\"person\": people[].id, "
            "\"name\": people[].name} or {\"seat\": seats[]}. cap_usd: dollars (at most the "
            "backstop). approvers: 1-3.")},
        "already_allowed": {"type": "array", "description": (
            "what the person asked for that agents may ALREADY do because it is not on the "
            "list: [{\"says\": <one sentence: why nothing changes>, \"kind\": the nearest "
            "list[].key or \"\"}]")},
        "cannot": {"type": "array", "description": (
            "what would make the list LOOSER than the backstop (take an entry off, let Full auto "
            "approve it, raise the cap above the backstop, drop a passkey): [{\"says\": …}]. "
            "Never put these in edits.")},
        "reply": {"type": "string", "description": "one or two plain sentences to the person"},
    },
    "required": ["edits", "reply"],
}

RULE_CHANGE_RULES = """You are {helm}, BOSS's captain. A person is changing who decides what on \
the compliance list ("Where people decide"). Your one action is `brief`: turn their words into \
structured edits as its arguments.

- The list is `list` in the context: each entry's key, title and what is on it. Anything NOT on \
the list, agents already do on their own; asking to allow it changes nothing: say so in \
`already_allowed` (for example a merge into a branch that is not the default branch).
- STRICTER ONLY. A card may add a quiet window, name who decides or a backup, lower the spend \
cap, add an approver. Anything that would take an entry off the list, let Full auto approve \
it, raise the cap above the backstop or drop a passkey goes in `cannot`, never in `edits`.
- PEOPLE: name a decider or backup only from `people` (their exact id). If the person named \
someone who is not there, put it in `cannot` with why.
- TIME: quiet windows use the person's time zone `tz` unless they name another. "after 3pm on \
Fridays" until "Monday" is from_day fri 15:00 to mon 08:00 unless they say the end.
- SCOPE: "install" unless they name one of `projects`.
- Plain words in every `says`. No markdown."""


def understand_rule_change(connection: Any, settings: Any, project_id: str, *, actor: str,
                           words: str, tz: str = "UTC", org_id: Optional[str] = None,
                           adapter: Any = None, decider: Any = None) -> dict[str, Any]:
    """Helm's reading of a rule in plain words: {status, edits[], already_allowed[], cannot[],
    reply, replay[]}. Every edit is checked by `validate` (stricter only) before it is shown,
    and replayed against the last 30 days of real decisions. Never raises; no model is
    `no_model`."""
    from .project_experience import understanding

    words = (words or "").strip()[:4000]
    if not words:
        return {"status": "empty", "edits": [], "already_allowed": [], "cannot": [],
                "reply": "", "replay": []}
    try:
        tz = _tz(tz)
    except RuleLooser:
        tz = "UTC"
    rules = stored(connection)
    seats = seats_view(connection, org_id)
    roster = people(connection, org_id)
    projects = _projects(connection)
    context = {
        "list": [{"key": b.key, "title": b.title, "on_it": b.rule, "why": b.why,
                  "now": describe_rule(b.key, install_rule(connection, b.key, rules=rules)),
                  "per_project": b.per_project, "fixed": b.fixed} for b in LIST],
        "people": [{"id": p["id"], "name": p["name"], "email": p["email"]} for p in roster],
        "seats": sorted(set(seats) | {b.seat for b in LIST_KINDS}),
        "projects": projects, "tz": tz, "backstop_spend_usd": BACKSTOP_SPEND_USD,
        "their_words": words,
    }
    check_people = {p["id"] for p in roster}

    def check(answer: Mapping[str, Any], _corpus: str) -> str:
        args = answer.get("arguments") or {}
        if not isinstance(args.get("edits"), list):
            return "edits is a list (empty when nothing changes)"
        for e in args.get("edits") or []:
            if not isinstance(e, Mapping) or str(e.get("kind")) not in KINDS:
                return f"each edit's kind is one of {', '.join(KINDS)}"
            v = e.get("value")
            if str(e.get("field")) in ("decider", "backup") and isinstance(v, Mapping) \
                    and v.get("person") and check_people and v["person"] not in check_people:
                return f"{v.get('person')!r} is not in people: name only people[].id"
            try:
                _edit_rule(rules, e)
            except RuleLooser as refused:
                return f"edit {e.get('says') or e.get('kind')!r} is refused: {refused}. Put it in cannot."
        return ""

    helm = "Helm"
    try:
        got = understanding.turn(
            connection, settings, project_id, subject="rule_change", actor=actor, agent=helm,
            message=words, situation=("A person typed a rule change for the list of decisions "
                                      "that wait for a person."),
            external_ref="", schema=RULE_CHANGE_SCHEMA, context=context,
            rules=RULE_CHANGE_RULES.format(helm=helm), check=check, adapter=adapter,
            decider=decider)
    except Exception as exc:  # noqa: BLE001 — Helm's reading is a help, never a failed screen
        return {"status": "no_model", "edits": [], "already_allowed": [], "cannot": [],
                "reply": f"Helm could not read it now ({type(exc).__name__}). Your words are kept.",
                "replay": []}
    if understanding.timed_out(got) or not getattr(got, "decided", False) \
            or getattr(got, "action", None) != "brief":
        return {"status": "no_model", "edits": [], "already_allowed": [], "cannot": [],
                "reply": "Helm could not read it just now. Your words are kept; try again.",
                "replay": []}
    args = dict(got.arguments or {})
    edits = []
    for e in args.get("edits") or []:
        if not isinstance(e, Mapping):
            continue
        scope = str(e.get("scope") or INSTALL)
        if scope != INSTALL and scope not in {p["id"] for p in projects}:
            scope = INSTALL
        edits.append({"kind": str(e["kind"]), "field": str(e.get("field") or ""),
                      "value": e.get("value"), "scope": scope,
                      "title": BY_KEY[str(e["kind"])].title,
                      "says": " ".join(str(e.get("says") or "").split())[:300]})
    said = lambda xs: [{"says": " ".join(str((x or {}).get("says") or "").split())[:300],  # noqa: E731
                        "kind": str((x or {}).get("kind") or "")}
                       for x in xs or [] if isinstance(x, Mapping) and (x or {}).get("says")]
    return {"status": "understood", "edits": edits,
            "already_allowed": said(args.get("already_allowed"))[:6],
            "cannot": said(args.get("cannot"))[:6],
            "reply": " ".join(str(args.get("reply") or "").split())[:600],
            "replay": replay(connection, edits),
            "by": dict(getattr(got, "by", None) or {})}


def _projects(connection: Any) -> list[dict[str, str]]:
    from sqlalchemy import text

    try:
        with connection.begin_nested():
            rows = connection.execute(text(
                "SELECT id, name FROM projects ORDER BY name LIMIT 100")).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    return [{"id": r["id"], "name": r["name"] or r["id"]} for r in rows]


def _edit_rule(rules: Mapping[str, Any], edit: Mapping[str, Any]) -> tuple[str, str, dict]:
    """(kind, scope, the whole new rule) for one structured edit, validated stricter-only."""
    kind = str(edit.get("kind") or "")
    if kind not in KINDS:
        raise RuleLooser(f"{kind!r} is not a card that can change")
    scope = str(edit.get("scope") or INSTALL)
    b = BY_KEY[kind]
    if scope != INSTALL and not b.per_project:
        scope = INSTALL
    base_install = install_rule(None, kind, rules=rules)
    current = dict((rules.get(scope) or {}).get(kind) or (base_install if scope == INSTALL
                                                          else {"decider": base_install["decider"]}))
    fld = str(edit.get("field") or "")
    value = edit.get("value")
    if fld == "quiet":
        current["quiet"] = list(current.get("quiet") or []) + [value]
    elif fld in ("decider", "backup"):
        current[fld] = value
    elif fld == "cap_usd":
        current["cap_usd"] = value
    elif fld == "approvers":
        current["approvers"] = value
    else:
        raise RuleLooser(f"{fld!r} is not something a card sets")
    clean = validate(kind, current, ceiling=base_install if scope != INSTALL else None)
    return kind, scope, clean


def apply_edits(connection: Any, edits: Sequence[Mapping[str, Any]], *, actor: str,
                words: str = "", project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Apply a Rule change card's edits, all or nothing (every edit is validated before any is
    written). Raises RuleLooser naming the edit."""
    if not edits:
        raise RuleLooser("nothing to apply")
    rules = {s: dict(k) for s, k in stored(connection).items()}
    planned: list[tuple[str, str, dict]] = []
    for e in edits:
        kind, scope, clean = _edit_rule(rules, e)
        rules.setdefault(scope, {})[kind] = clean   # the next edit to the same card builds on it
        planned.append((kind, scope, clean))
    last: dict[tuple[str, str], dict] = {}
    for kind, scope, clean in planned:
        last[(kind, scope)] = clean
    return [save(connection, kind, clean, actor=actor, scope=scope, via="words", words=words,
                 project_id=project_id) for (kind, scope), clean in last.items()]


__all__ = ["AMBIENT_ACTIONS", "BACKSTOP_SPEND_USD", "BY_KEY", "IRREVERSIBLE", "KINDS", "LIST",
           "LIST_KINDS", "RuleLooser", "activity", "apply_edits", "board", "classify",
           "default_rule", "effective", "in_window", "on_the_list", "refuse_approval", "replay",
           "save", "spend_cap", "understand_rule_change", "validate"]
