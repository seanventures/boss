"""HELM'S UNDERSTANDING: the brief card ("Task" in New work), one pattern for every box a person
types work into (New work on a project; Add a project reuses the same turn with its own fields).

The person's words stay as they wrote them. Helm — the project's captain — rewrites them into a
short brief, checked against the project's code graph:

  title          a name for the work (4-8 words)
  what_changes   one or two sentences, in the person's terms
  area           the files it touches, ONLY from the code graph (a new file or a migration is
                 marked `new`), each with a few words on why
  done_when      testable criteria: the build's acceptance criteria and the PR's checklist
  risk           low | medium | high, and why
  added          the phrases Helm added that the person did not say (the card marks them)
  open_questions what is still unclear (never blocks Start)

TALK IT THROUGH. Every exchange is one turn through THE ONE AGENT PIPELINE
(AgentService.decide, BOSSAPP-759): a structured decision whose single choice is "brief", with
the brief as its validated arguments, checked here against what Helm was shown (a file the code
graph does not hold is refused and asked once more). The turn returns the brief UPDATED from the
whole conversation, so the card is always current and Start works at any time.

HELM ASKS LITTLE (internal policy, never shown to the person): at most one question per turn,
only when the answer would change the work, and at most MAX_OWN_QUESTIONS on its own initiative
per draft. Past that, anything unclear goes on the card as an open question instead. The person
may keep talking as long as they like; a long discussion can move to Ask Helm with the draft.

NO MODEL, OR NO TIME: status `no_model` and no brief — the console says so and Start uses the
person's words as written. Nothing here writes anything but the turn's own transcript.
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .common import agents, face, repos

#: Questions Helm asks on its own initiative per draft, at most (a policy, never displayed).
MAX_OWN_QUESTIONS = 3
#: Quick replies offered with a question, at most.
MAX_REPLIES = 4
#: Seconds a turn may take before the console falls back to the person's words.
TURN_SECONDS = 45.0
#: Files from the code graph Helm is shown, at most.
GRAPH_FILES = 30
PRIORITIES = ("low", "normal", "high", "urgent")
RISKS = ("low", "medium", "high")
#: The longest words a person may send (the box's own limit, with a margin for a pasted issue).
MAX_WORDS = 8000

_URL = re.compile(r"https?://\S+")
_GITHUB_ISSUE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/(issues|pull)/(\d+)", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
#: Words that never pick a file out of the graph (they match everything).
_COMMON = frozenset("""
the and for with that this from when what which into onto then than they them their there
have has had should would could will can able make made want need lets let just also only
each every some more most less any all not but our your you its it's new add adds added
change changes changed allow allows show shows like page pages app apps file files code
""".split())

BRIEF_SCHEMA: dict[str, Any] = {
    "properties": {
        "title": {"type": "string", "description": "a name for the work, 4 to 8 words"},
        "what_changes": {"type": "string",
                         "description": "one or two sentences: what changes, in the person's terms"},
        "area": {"type": "array", "description": (
            "the files it touches: [{\"path\": <a path from code_graph.files, exactly>, "
            "\"why\": <3-6 words>, \"new\": false}]; a file or migration that does not exist "
            "yet is {\"path\": <proposed>, \"why\": …, \"new\": true}. Empty when the graph "
            "holds nothing relevant.")},
        "done_when": {"type": "array", "description": "2 to 6 testable criteria, short"},
        "risk": {"type": "object", "description": (
            "{\"level\": \"low\"|\"medium\"|\"high\", \"why\": <one short clause>}")},
        "added": {"type": "array", "description": (
            "the exact phrases (copied from what_changes, done_when or risk.why) that YOU "
            "added and the person did not say or clearly imply")},
        "open_questions": {"type": "array",
                           "description": "what is still unclear, as short questions"},
        "ask": {"type": "object", "description": (
            "ONE question for the person, only when its answer changes the work: "
            "{\"text\": <the question>, \"replies\": [<2-4 short quick replies>]}; "
            "{} when there is nothing worth asking")},
        "reply": {"type": "string", "description": (
            "what you say to the person in the chat: one or two plain sentences on what you "
            "found or changed. Never repeat ask.text here: the question is shown on its own "
            "right after this. No headings, no lists.")},
        "applied": {"type": "array", "description": (
            "what this turn changed in the brief or settings, as 2-4 word notes "
            "(\"Done when +1\", \"priority high\"); [] on the first read")},
        "settings": {"type": "object", "description": (
            "what the person asked for in the conversation, else {}: \"priority\" (low|normal|"
            "high|urgent), \"notify\" (who to tell: an email or a #channel), \"workflow_key\" "
            "(one of workflows[].key, only when the work clearly fits another workflow better)")},
    },
    "required": ["title", "what_changes", "done_when", "risk", "reply"],
}

BRIEF_RULES = """You are {helm}, the captain of the project {project}. A person is describing \
{kind_words} for this project in the New work box. Your one action is `brief`: write (or \
update) the brief for this work as its arguments.

- KEEP THEIR WORDS. Rewrite only to make the work clear and testable; never change what they \
asked for. Anything you add that they did not say goes, verbatim, in `added`.
- AREA comes ONLY from code_graph.files (exact paths). A file or migration that must be \
created is marked new. If the graph is empty or holds nothing relevant, area is [].
- DONE WHEN: 2 to 6 short, testable criteria. They become the build's acceptance criteria and \
the pull request's checklist.
- THE CONVERSATION: apply every answer the person gave to the brief and list what changed in \
`applied`. If they asked you something, answer it in `reply`.
- ASK: at most one question, only when its answer changes the work and neither their words, \
the issue nor the code graph answers it, and never one already asked in the conversation. \
{ask_rule}
- SETTINGS: only what the person asked for (\"high priority\", \"tell #support-eng\").
- Plain words. No markdown in any field."""

_ASK_OPEN = "Give 2-4 quick replies with it."
_ASK_SPENT = ("You have asked enough on your own: do NOT ask; put anything still unclear in "
              "open_questions instead.")


# ── what Helm reads ─────────────────────────────────────────────────────────────────────────

def _stems(words: str) -> list[str]:
    """The words a file might be named after: 'tickets' → 'ticket'. Candidates only — which of
    the files they find matter is the model's call."""
    out: list[str] = []
    for w in _WORD.findall(_URL.sub(" ", words or "")):
        low = w.lower()
        if low in _COMMON:
            continue
        for suffix in ("ies", "es", "s"):
            if len(low) > 4 and low.endswith(suffix):
                low = low[: -len(suffix)] + ("y" if suffix == "ies" else "")
                break
        if len(low) >= 3 and low not in out:
            out.append(low)
    return out[:16]


def code_area(connection: Connection, project_id: str, words: str,
              limit: int = GRAPH_FILES) -> dict[str, Any]:
    """{files: [{path, names[], kinds[]}], tables: [..], indexed: bool}: the code graph's files
    whose path or symbols carry the person's words, most matches first."""
    stems = _stems(words)
    out: dict[str, Any] = {"files": [], "tables": [], "indexed": False}
    try:
        with connection.begin_nested():
            out["indexed"] = bool(connection.execute(text(
                "SELECT 1 FROM kg_nodes WHERE project_id = :p AND kind = 'file' "
                "AND valid_to IS NULL LIMIT 1"), {"p": project_id}).first())
            if not out["indexed"] or not stems:
                return out
            likes = [f"%{s}%" for s in stems]
            rows = connection.execute(text(
                "SELECT kind, name, path FROM kg_nodes WHERE project_id = :p "
                "AND valid_to IS NULL AND (lower(COALESCE(path, '')) LIKE ANY(:l) "
                "OR lower(name) LIKE ANY(:l)) LIMIT 600"),
                {"p": project_id, "l": likes}).mappings().all()
    except Exception:  # noqa: BLE001 — no graph is an answer: Helm says the area is unknown
        return out
    files: dict[str, dict[str, Any]] = {}
    for r in rows:
        kind, name, path = str(r["kind"]), str(r["name"] or ""), str(r["path"] or "")
        if kind == "table":
            if name and name not in out["tables"]:
                out["tables"].append(name)
            continue
        if not path:
            continue
        f = files.setdefault(path, {"path": path, "names": [], "kinds": [], "hits": set()})
        hay = f"{path} {name}".lower()
        f["hits"].update(s for s in stems if s in hay)
        if kind != "file" and name and len(f["names"]) < 6 and name not in f["names"]:
            f["names"].append(name)
        if kind not in f["kinds"]:
            f["kinds"].append(kind)
    ranked = sorted(files.values(), key=lambda f: (-len(f["hits"]), len(f["path"])))
    out["files"] = [{"path": f["path"], "names": f["names"], "kinds": f["kinds"]}
                    for f in ranked[:limit]]
    out["tables"] = out["tables"][:12]
    return out


def issue_link(words: str) -> Optional[str]:
    """The first issue or pull request link in what the person typed or pasted, on any tab."""
    from .new_work import ticket_link

    return ticket_link(words or "")


def read_issue(connection: Connection, settings: Any, project_id: str,
               url: Optional[str]) -> Optional[dict[str, Any]]:
    """{url, label, title, body, labels[], state, read} for a GitHub issue link (read with the
    project's own GitHub connection), or the link alone when it cannot be read."""
    from .new_work import issue_of

    if not url:
        return None
    found = issue_of(source_url=url) or {}
    out = {"url": url, "label": found.get("label") or "Issue", "provider": found.get("provider"),
           "title": "", "body": "", "labels": [], "state": "", "read": False}
    m = _GITHUB_ISSUE.search(url)
    if not m:
        return out
    owner, repo, _what, number = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        import httpx

        from ..github_connections import github_token_for

        token = github_token_for(connection, settings, project_id, owner, repo)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        got = httpx.get(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
                        headers=headers, timeout=6.0)
        if got.status_code != 200:
            return out
        data = got.json()
    except Exception:  # noqa: BLE001 — the link alone still travels with the work
        return out
    out.update({"title": str(data.get("title") or "")[:300],
                "body": str(data.get("body") or "")[:3000],
                "labels": [str((lb or {}).get("name") or "") for lb in data.get("labels") or []
                           if isinstance(lb, Mapping)][:12],
                "state": str(data.get("state") or ""), "read": True})
    return out


def is_bug(issue: Optional[Mapping[str, Any]]) -> bool:
    """An issue labelled as a bug starts on the project's bug workflow (the Ticket default)."""
    return any("bug" in str(lb).lower() for lb in (issue or {}).get("labels") or [])


# ── the turn ─────────────────────────────────────────────────────────────────────────────────

def own_questions(chat: Sequence[Mapping[str, Any]]) -> int:
    """How many questions Helm has asked on its own in this conversation."""
    return sum(1 for m in chat or [] if str(m.get("from")) == "helm" and m.get("ask"))


def _strings(value: Any, limit: int, size: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = " ".join(str(v or "").split())[:size]
        if s and s not in out:
            out.append(s)
    return out[:limit]


def _check(files: set[str], workflows: set[str]):
    """The caller's check of Helm's answer (one re-ask on refusal): the brief is whole, and
    every file it names that already exists is one the code graph holds."""
    def check(answer: Mapping[str, Any], _corpus: str) -> str:
        args = answer.get("arguments") or {}
        if not str(args.get("title") or "").strip():
            return "the brief needs a title"
        done = args.get("done_when")
        if not isinstance(done, list) or not [d for d in done if str(d or "").strip()]:
            return "done_when needs at least one testable criterion"
        risk = args.get("risk") if isinstance(args.get("risk"), Mapping) else {}
        if str(risk.get("level") or "").lower() not in RISKS:
            return "risk.level is low, medium or high"
        for a in args.get("area") or []:
            path = str((a or {}).get("path") if isinstance(a, Mapping) else a or "").strip()
            if path and not (isinstance(a, Mapping) and a.get("new")) and path not in files:
                return (f"area names {path!r}, which is not in code_graph.files: cite only "
                        "those paths exactly, or mark a file that does not exist yet as new")
        settings = args.get("settings") if isinstance(args.get("settings"), Mapping) else {}
        key = str(settings.get("workflow_key") or "")
        if key and key not in workflows:
            return f"settings.workflow_key {key!r} is not one of workflows[].key"
        prio = str(settings.get("priority") or "")
        if prio and prio.lower() not in PRIORITIES:
            return "settings.priority is low, normal, high or urgent"
        return ""
    return check


def _brief_of(args: Mapping[str, Any], files: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    area = []
    for a in args.get("area") or []:
        if isinstance(a, Mapping):
            path = " ".join(str(a.get("path") or "").split())[:300]
            if path:
                area.append({"path": path, "why": " ".join(str(a.get("why") or "").split())[:80],
                             "new": bool(a.get("new")) or path not in files})
        elif isinstance(a, str) and a.strip():
            area.append({"path": a.strip()[:300], "why": "", "new": a.strip() not in files})
    risk = args.get("risk") if isinstance(args.get("risk"), Mapping) else {}
    return {"title": " ".join(str(args.get("title") or "").split())[:120],
            "what_changes": " ".join(str(args.get("what_changes") or "").split())[:1200],
            "area": area[:10],
            "done_when": _strings(args.get("done_when"), 8),
            "risk": {"level": str(risk.get("level") or "medium").lower(),
                     "why": " ".join(str(risk.get("why") or "").split())[:300]},
            "added": _strings(args.get("added"), 12, 200),
            "open_questions": _strings(args.get("open_questions"), 6)}


#: What the brief is about. One turn shape for each (THE ONE UNDERSTANDING SERVICE): new work on
#: a project (`understand`, this module), a new app on Add a project (`understand_new_app`), and
#: a rule change on Govern › Where people decide (decide_list.understand_rule_change).
SUBJECTS = ("work", "new_app", "rule_change")


def turn(connection: Optional[Connection], settings: Any, project_id: str, *, subject: str,
         actor: str, agent: str, message: str, situation: str, external_ref: str,
         schema: Mapping[str, Any], context: Mapping[str, Any], rules: str, check: Any = None,
         adapter: Any = None, decider: Any = None, seconds: float = TURN_SECONDS) -> Any:
    """ONE TURN OF HELM'S UNDERSTANDING, whatever the brief is about (`subject`): a structured
    decision through the one agent pipeline (AgentService.decide, BOSSAPP-759) whose single
    choice is "brief", with the brief as its validated arguments and the caller's check, inside
    a turn deadline (call_deadline: no model call outlives it). Returns the AgentDecision; a
    missing or slow model is its `no_model` status, never a raise."""
    from ..agent_service import AgentService, Choice
    from .. import call_deadline

    if subject not in SUBJECTS:
        raise ValueError(f"unknown subject {subject!r}")
    with call_deadline.until(time.monotonic() + max(5.0, float(seconds))):
        return AgentService(settings).decide(
            connection, project_id=project_id, actor=actor, role="engineering_captain",
            agent=agent, message=message, situation=situation, external_ref=external_ref,
            ref=external_ref or f"understanding:{subject}:{project_id}",
            choices=[Choice("brief", "write or update the brief, as arguments", acts=False,
                            label="Write the brief", arguments=dict(schema))],
            context=dict(context), brief=rules, check=check, adapter=adapter, decider=decider,
            min_confidence=0.0, context_limit=14000)


def timed_out(decision: Any) -> bool:
    why = str(getattr(decision, "refused", "") or getattr(decision, "error", "") or "")
    return "turn has" in why or "deadline" in why.lower()


def _kind_words(kind: str) -> str:
    return {"bug": "a bug to fix", "ticket": "work from an issue or ticket"}.get(kind,
                                                                                 "a feature")


def understand(connection: Connection, settings: Any, project_id: str, *, actor: str,
               kind: str, words: str, chat: Sequence[Mapping[str, Any]] = (),
               brief: Optional[Mapping[str, Any]] = None, hint: str = "", draft: str = "",
               workflows: Sequence[Mapping[str, Any]] = (), adapter: Any = None,
               seconds: float = TURN_SECONDS) -> dict[str, Any]:
    """One turn of Helm's understanding: the brief (updated from the whole conversation), what
    Helm says, the one question it may ask, and what the person asked to set.

    `chat` is the conversation so far, oldest first: [{from: "you"|"helm", text, ask?}]; its
    last "you" entry, if newer than Helm's last word, is what this turn answers. `brief` is the
    card as the person sees it (their own edits included). `hint` is a Rewrite's direction."""
    from ..agent_service import NO_MODEL
    from .common import project_row

    words = (words or "").strip()[:MAX_WORDS]
    kind = kind if kind in ("feature", "bug", "ticket") else "feature"
    roster = agents(connection, project_id)
    helm = face(roster, "engineering_captain") or {"name": "Helm", "role_key":
                                                   "engineering_captain"}
    link = issue_link(words)
    issue = read_issue(connection, settings, project_id, link) if link else None
    if not words and not (issue and issue.get("read")):
        return {"status": "empty", "brief": None, "reply": "", "ask": None, "applied": [],
                "settings": {}, "issue": issue, "graph": {"indexed": False, "files": 0},
                "agent": helm, "asked": own_questions(chat)}
    graph = code_area(connection, project_id,
                      " ".join([words, (issue or {}).get("title") or "",
                                *[str(m.get("text") or "") for m in chat or []
                                  if str(m.get("from")) == "you"]]))
    files = {f["path"]: f for f in graph["files"]}
    project = project_row(connection, project_id) or {"name": project_id}
    repo = next(iter(repos(connection, project_id)), None)
    asked = own_questions(chat)
    can_ask = asked < MAX_OWN_QUESTIONS
    said_last = [m for m in chat or [] if str(m.get("text") or "").strip()]
    newest = said_last[-1] if said_last else None
    message = (str(newest.get("text") or "") if newest and str(newest.get("from")) == "you"
               else words or (issue or {}).get("title") or "")
    if hint.strip():
        message = f"Rewrite the brief: {hint.strip()[:500]}"
    context = {
        "project": {"name": project.get("name"), "repository": (repo or {}).get("full_name")},
        "kind": kind,
        "their_words": words,
        "issue": ({k: issue[k] for k in ("url", "label", "title", "body", "labels", "state")}
                  if issue and issue.get("read") else ({"url": link} if link else None)),
        "code_graph": {"indexed": graph["indexed"], "files": graph["files"],
                       "tables": graph["tables"]},
        "workflows": [{"key": w.get("key"), "name": w.get("name"), "fits": w.get("fits")}
                      for w in workflows][:12],
        "brief_now": dict(brief) if brief else None,
        "conversation": [{"from": str(m.get("from")), "text": str(m.get("text") or "")[:1200]}
                         for m in (chat or [])][-16:],
        "questions_asked_so_far": asked,
    }
    rules = BRIEF_RULES.format(helm=helm["name"], project=project.get("name") or project_id,
                               kind_words=_kind_words(kind),
                               ask_rule=_ASK_OPEN if can_ask else _ASK_SPENT)
    started = time.monotonic()
    decision = turn(connection, settings, project_id, subject="work", actor=actor,
                    agent=f"{helm['name']}, the project's captain", message=message,
                    situation=("A person is describing new work in the New work box; you write "
                               "the brief Start will run."),
                    external_ref=f"new_work:{draft}" if draft else "",
                    schema=BRIEF_SCHEMA, context=context, rules=rules,
                    check=_check(set(files), {str(w.get("key")) for w in workflows
                                              if w.get("key")}),
                    adapter=adapter, seconds=seconds)
    base = {"issue": issue, "graph": {"indexed": graph["indexed"], "files": len(graph["files"])},
            "agent": helm, "asked": asked, "by": dict(decision.by or {}),
            "seconds": round(time.monotonic() - started, 1)}
    if decision.status == NO_MODEL or decision.action != "brief":
        return {**base, "status": "no_model", "brief": None, "ask": None, "applied": [],
                "settings": {}, "why": ("Helm took too long" if timed_out(decision) else
                                        "no model answered" if not decision.refused else
                                        "Helm's answer was refused"),
                "reply": ("I couldn't read it just now. Start uses your words as written; "
                          "you can try again.")}
    args = decision.arguments or {}
    out_brief = _brief_of(args, files)
    ask = args.get("ask") if isinstance(args.get("ask"), Mapping) else {}
    question = " ".join(str(ask.get("text") or "").split())[:400]
    replies = _strings(ask.get("replies"), MAX_REPLIES, 60)
    asked_before = {" ".join(str(m.get("text") or "").lower().split()) for m in chat or []
                     if str(m.get("from")) == "helm" and m.get("ask")}
    if question and " ".join(question.lower().split()) in asked_before:
        # Asked already and not answered: it stays on the card, never asked twice in the chat.
        if question not in out_brief["open_questions"]:
            out_brief["open_questions"].append(question)
        question, replies = "", []
    if question and not can_ask:
        # Past the policy: the question goes on the card instead of into the chat.
        if question not in out_brief["open_questions"]:
            out_brief["open_questions"].append(question)
        question, replies = "", []
    raw = args.get("settings") if isinstance(args.get("settings"), Mapping) else {}
    chosen: dict[str, Any] = {}
    if str(raw.get("priority") or "").lower() in PRIORITIES:
        chosen["priority"] = str(raw["priority"]).lower()
    if str(raw.get("notify") or "").strip():
        chosen["notify"] = " ".join(str(raw["notify"]).split())[:200]
    if str(raw.get("workflow_key") or "").strip():
        chosen["workflow_key"] = str(raw["workflow_key"]).strip()
    return {**base, "status": "understood", "brief": out_brief,
            "reply": " ".join(str(args.get("reply") or "").split())[:1200],
            "ask": {"text": question, "replies": replies} if question else None,
            "applied": _strings(args.get("applied"), 6, 60), "settings": chosen,
            "asked": asked + (1 if question else 0)}


# ── what Start sends ─────────────────────────────────────────────────────────────────────────

def directive(words: str, brief: Optional[Mapping[str, Any]]) -> str:
    """The run's directive: the brief as the work's spec, with the person's own words kept."""
    words = (words or "").strip()
    if not brief or not str(brief.get("title") or "").strip():
        return words
    lines = [str(brief.get("title")).strip(), ""]
    if brief.get("what_changes"):
        lines += [str(brief["what_changes"]).strip(), ""]
    area = [a for a in brief.get("area") or [] if isinstance(a, Mapping) and a.get("path")]
    if area:
        lines.append("Area (from the code graph):")
        lines += [f"- {a['path']}" + (" (new)" if a.get("new") else "")
                  + (f" — {a['why']}" if a.get("why") else "") for a in area]
        lines.append("")
    done = [str(d).strip() for d in brief.get("done_when") or [] if str(d or "").strip()]
    if done:
        lines.append("Done when:")
        lines += [f"- {d}" for d in done]
        lines.append("")
    risk = brief.get("risk") if isinstance(brief.get("risk"), Mapping) else {}
    if risk.get("level"):
        lines += [f"Risk: {risk['level']}" + (f" — {risk['why']}" if risk.get("why") else ""),
                  ""]
    open_q = [str(q).strip() for q in brief.get("open_questions") or [] if str(q or "").strip()]
    if open_q:
        lines.append("Open questions:")
        lines += [f"- {q}" for q in open_q]
        lines.append("")
    if words:
        lines += ["In the person's words:", words]
    return "\n".join(lines).strip()


__all__ = ["BRIEF_SCHEMA", "MAX_OWN_QUESTIONS", "PRIORITIES", "code_area", "directive",
           "is_bug", "issue_link", "own_questions", "read_issue", "understand"]
