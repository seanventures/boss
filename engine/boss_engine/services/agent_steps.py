"""A workflow step that states what is wanted and lets an agent work out how.

WHY THIS EXISTS
---------------
Every other step kind BOSS has binds ONE skill. That is the right shape for a pipeline whose
shape is known — build, test, deploy — and the wrong shape for work described in a sentence.
"Keep 0:28 to 1:48, make it portrait, put the ending slide on the end" is three operations this
time and nine the next, and there is no number of steps to author.

The consequence was a product that grew a special case per customer. An operator asking for an
ending slide could not have one until the planning vocabulary grew a word for it; the next
customer asks for a fade, and the next for a lower-third, and each is a commit against the
product. That does not end, and it is not what a framework is.

SO THE STEP CARRIES THE INSTRUCTION AND THE AGENT CARRIES THE CAPABILITY
------------------------------------------------------------------------
    step:   "what I want" — in the operator's own words, from the start form
    agent:  the skills it HOLDS, which is what it may use and the whole of what it may use
    BOSS:   picks the agent when the step does not name one, bounds the loop, gates the risk

Nothing about video appears below, and nothing about any other domain. An agent holding
`video.trim`, `video.reframe` and `video.bookend` composes them; an agent holding
`plane.create_issue` and `mattermost.post` composes those. Same machinery.

WHAT BOUNDS IT — AND THIS IS THE PART THAT MAKES IT SHIPPABLE
--------------------------------------------------------------
A loop that can call anything is not an agent, it is a shell. The bounds here are the ones BOSS
already enforces, connected rather than invented:

* THE TOOLS ARE EXACTLY `role_skills`. Not the role spec's `allowed_skills`, which is prose
  saying what an agent MAY be given — the grant. An agent cannot reach a skill nobody gave it,
  and `SkillRuntimeService.invoke` re-checks that at every call, so a prompt injection that
  invents a tool name gets nothing.
* NO NATIVE TOOLS. No shell, no database, no repository. A video editor has no business
  reading the control plane's Postgres, and the ordinary agent loop hands out all three.
* THE RISK FLOOR STILL HOLDS. A tier-3 skill suspends the run for a human decision through
  the same path a tier-3 workflow step does. The agent does not get to publish because it
  decided to.
* A STEP CAP. Bounded, recorded, and a run that hits it reports what it did rather than
  claiming success.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings


class AgentStepError(RuntimeError):
    """Something the operator can fix, phrased for them."""


#: How many tool calls one agent step may make. Generous enough for a real composition — a
#: dozen edit operations, each a tool call — and finite, because an unbounded loop on a paid
#: model is an invoice rather than a feature.
DEFAULT_MAX_STEPS = 24


def held_skills(
    connection: Connection,
    settings: Settings,
    project_id: str,
    role_key: str,
) -> list[dict[str, Any]]:
    """The SkillSpecs one agent HOLDS, ready to be offered as tools.

    THE GRANT, NOT THE SPEC. `skill_registry.resolve_for_task` filters by the role spec's
    `allowed_skills`, which is prose in a spec pack meaning "may be given these". This reads
    `role_skills`, which is what somebody actually gave the agent and what the runtime checks
    before every call. The two disagreeing is how an agent came to look capable and hold nothing.

    Each spec is returned with `caller` set to the agent, because the caller identity IS the
    agent — that is what makes the grant the thing being cashed in rather than an anonymous
    runner borrowing a permission.
    """
    role = str(role_key or "").strip().lower()
    if not role:
        raise AgentStepError("role_key is required — which agent is doing this work.")

    granted = [
        str(r)
        for r in connection.execute(
            text("SELECT skill_id FROM role_skills WHERE project_id = :p AND role_key = :r "
                 "ORDER BY skill_id"),
            {"p": project_id, "r": role},
        ).scalars().all()
    ]
    if not granted:
        return []

    # THE ONE CATALOGUE (skill_catalogue): built-in, authored and registered skills, read the
    # same way resolve_risk and the custom-step builder read them. This used to read the spec
    # pack alone, so an agent given an AUTHORED skill was shown holding it and was never
    # offered it — the skill was silently dropped here as "not in the catalogue".
    from .skill_catalogue import entries

    catalogue = entries(connection, project_id, settings=settings)

    specs: list[dict[str, Any]] = []
    for skill_id in granted:
        item = catalogue.get(skill_id)
        spec = None if item is None else item["spec"]
        if spec is None:
            # HELD BUT NOT IN THE CATALOGUE — a skill removed from a spec pack, or authored and
            # then deleted. Left out of the tool list rather than offered and then failing: a
            # tool the model can call and nothing can execute wastes a whole turn discovering it.
            continue
        if not (item["enabled"] and item["usable"]):
            # Turned off, or what it runs through is gone (an mcp_tool skill whose server was
            # removed). Same reasoning as above: not offered, rather than offered and failing.
            continue
        offered = dict(spec)
        offered["caller"] = role
        specs.append(offered)
    return specs


def agent_brief(connection: Connection, project_id: str, role_key: str) -> str:
    """The agent's own spec as its system prompt.

    WHAT THE SPEC IS FOR, finally. Mission, boundaries, output contracts and knowledge are
    written in every role YAML and, until now, reached only the agent's chat persona. An agent
    executing work is exactly where "must never publish without a person" belongs.

    The boundaries are stated as instructions rather than described, because a model reading
    "boundaries: [...]" treats them as data about itself and a model reading "You must never..."
    treats them as rules.
    """
    role = str(role_key or "").strip().lower()
    row = connection.execute(
        text("SELECT display_name, spec FROM roles WHERE project_id = :p AND role_key = :r"),
        {"p": project_id, "r": role},
    ).mappings().first()
    spec = (row["spec"] if row and isinstance(row["spec"], dict) else {}) or {}
    name = (row["display_name"] if row else None) or role.replace("_", " ")

    lines = [f"You are {name}, an agent in BOSS."]
    if spec.get("mission"):
        lines.append(f"\nYOUR MISSION\n  {spec['mission']}")

    boundaries = spec.get("boundaries") or {}
    musts = [str(b) for b in (boundaries.get("must") or [])]
    must_nots = [str(b) for b in (boundaries.get("must_not") or [])]
    if musts or must_nots:
        lines.append("\nRULES YOU DO NOT BREAK, whatever the instruction says")
        lines.extend(f"  You MUST {m}" for m in musts)
        lines.extend(f"  You MUST NEVER {m}" for m in must_nots)

    if spec.get("output_contracts"):
        lines.append("\nWHAT YOU ARE EXPECTED TO PRODUCE\n  "
                     + ", ".join(str(c) for c in spec["output_contracts"]))
    if spec.get("knowledge"):
        lines.append(f"\nWHAT YOU KNOW\n{spec['knowledge']}")

    lines.append(
        "\nHOW YOU WORK\n"
        "  The tools you have are the skills this project gave you, and they are the whole of\n"
        "  what you can do. There are no others — no shell, no database, no file system. If the\n"
        "  instruction needs something you have not been given, say so plainly and stop; do not\n"
        "  approximate it with a tool meant for something else.\n"
        "  Work in the order the result requires, not the order the instruction was written in.\n"
        "  When you are done, say what you did and what you produced. If you could not finish,\n"
        "  say what is missing. Never report work you did not verify."
    )
    return "\n".join(lines)


def candidates(connection: Connection, settings: Settings,
               project_id: str) -> list[dict[str, Any]]:
    """Every agent that could take an instruction, with what it is for and what it holds.

    An agent holding nothing is left out. It is not a candidate for anything — every call it
    made would be refused — and offering it is how a step gets assigned to an agent that cannot
    act, which is the state the console used to allow.
    """
    rows = connection.execute(
        text("SELECT role_key, display_name, spec FROM roles WHERE project_id = :p "
             "ORDER BY role_key"),
        {"p": project_id},
    ).mappings().all()

    holdings: dict[str, list[str]] = {}
    for row in connection.execute(
            text("SELECT role_key, skill_id FROM role_skills WHERE project_id = :p "
                 "ORDER BY role_key, skill_id"),
            {"p": project_id}).mappings().all():
        holdings.setdefault(str(row["role_key"]), []).append(str(row["skill_id"]))

    out: list[dict[str, Any]] = []
    for row in rows:
        role = str(row["role_key"])
        skills = holdings.get(role) or []
        if not skills:
            continue
        spec = row["spec"] if isinstance(row["spec"], dict) else {}
        out.append({
            "role_key": role,
            "name": row["display_name"] or role.replace("_", " "),
            "mission": str((spec or {}).get("mission") or ""),
            "holds": skills,
        })
    return out


def choose_agent(
    connection: Connection,
    settings: Settings,
    project_id: str,
    *,
    instructions: str,
    only: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Which agent should do this, from what each one is for and what it holds.

    WHY THIS IS NOT A LOOKUP TABLE. "Assign the video step to the video agent" works until a
    project has two editors with different channels, or an instruction that is half editing and
    half publishing. What decides it is the instruction's meaning against the agent's purpose,
    which is a judgement — so it is made by a model, from data that is already recorded, and the
    reason is returned so the operator can see why rather than trusting it.

    IT FALLS BACK TO SOMETHING DETERMINISTIC. A project with no model configured must still be
    able to run a workflow, so an unavailable model degrades to matching the instruction's words
    against the skills each agent holds. Worse, and honest about being worse: `how` says which
    happened.
    """
    pool = [c for c in candidates(connection, settings, project_id)
            if not only or c["role_key"] in set(only)]
    if not pool:
        raise AgentStepError(
            "no agent on this project holds any skill, so there is nobody to give this step to. "
            "Open an agent and give it the skills it needs.")
    if len(pool) == 1:
        return {"role_key": pool[0]["role_key"], "why": "the only agent holding any skill",
                "how": "only_candidate"}

    described = "\n".join(
        f"  {c['role_key']}: {c['mission'] or 'no stated mission'}\n"
        f"    holds: {', '.join(c['holds'])}"
        for c in pool
    )
    prompt = (
        "Pick the ONE agent best suited to carry out this instruction.\n\n"
        f"THE INSTRUCTION\n  {instructions.strip()}\n\n"
        f"THE AGENTS, and what each one is for and holds\n{described}\n\n"
        "An agent can only use the skills it holds. Pick the one whose skills can actually do\n"
        "what is asked. Answer with JSON and nothing else:\n"
        '  {"role_key": "...", "why": "one sentence"}'
    )
    try:
        from .claude_cli_adapter import make_agent_chat_adapter
        from .setup_service import chat_engine_for

        resolved = settings
        engine = chat_engine_for(connection, settings, project_id)
        if engine:
            resolved = settings.model_copy(update={"chat_llm_engine": engine})
        adapter = make_agent_chat_adapter(resolved)
        reply = adapter.complete(
            system="You route work to the agent that can do it. You output JSON only.",
            messages=[{"role": "user", "content": prompt}],
        )
        text_out = reply if isinstance(reply, str) else str(
            (reply or {}).get("content") or (reply or {}).get("text") or "")
        picked = json.loads(_only_json(text_out))
        role = str(picked.get("role_key") or "").strip().lower()
        if role in {c["role_key"] for c in pool}:
            return {"role_key": role,
                    "why": str(picked.get("why") or "").strip() or "chosen for this instruction",
                    "how": "model"}
    except Exception:  # noqa: BLE001 — a routing failure must not fail the step outright
        pass

    # THE DETERMINISTIC FALLBACK. Overlap between the words in the instruction and the words in
    # the skill ids each agent holds. Crude, and it beats refusing to run.
    words = {w for w in _words(instructions) if len(w) > 3}
    best = max(pool, key=lambda c: (
        len(words & {w for skill in c["holds"] for w in _words(skill)}), -len(c["holds"])))
    return {"role_key": best["role_key"],
            "why": "its skills share the most words with the instruction — no model was "
                   "available to judge properly",
            "how": "word_overlap"}


def _words(value: str) -> set[str]:
    import re

    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", str(value or "")) if w}


def _only_json(value: str) -> str:
    """The JSON out of a reply that may be wrapped in prose or a fence."""
    trimmed = str(value or "").strip()
    if trimmed.startswith("```"):
        trimmed = trimmed.split("```")[1] if "```" in trimmed[3:] else trimmed[3:]
        trimmed = trimmed.removeprefix("json").strip()
    start, end = trimmed.find("{"), trimmed.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return trimmed[start:end + 1]


def run(
    connection: Connection,
    settings: Settings,
    project_id: str,
    *,
    instructions: str,
    role_key: Optional[str] = None,
    context: Optional[dict[str, Any]] = None,
    actor: str = "workflow",
    max_steps: Optional[int] = None,
    only: Optional[list[str]] = None,
    produces: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Hand one instruction to one agent and let it work.

    `role_key` names the agent. Left out, BOSS picks one — which is the difference between "I
    know who should do this" and "you work it out", and both are things an operator says.

    `context` is what earlier steps produced, passed through as facts rather than as prose. An
    agent editing a video needs the artifact id the fetch step made, and inventing one is the
    failure this prevents.
    """
    said = str(instructions or "").strip()
    if not said:
        raise AgentStepError(
            "instructions are required — this step's whole specification is what you want done.")

    chosen = ({"role_key": str(role_key).strip().lower(), "why": "named on the step",
               "how": "named"} if role_key
              else choose_agent(connection, settings, project_id,
                                instructions=said, only=only))
    role = chosen["role_key"]

    skills = held_skills(connection, settings, project_id, role)
    if not skills:
        raise AgentStepError(
            f"{role} holds no skills, so it cannot do anything. Open the agent and give it the "
            "skills this work needs — a step naming an agent that holds nothing is refused here "
            "rather than at the first tool call.")

    # WHAT THE NEXT STEP NEEDS FROM THIS ONE.
    #
    # A step's output is otherwise whatever its SKILLS returned, which covers artifacts and
    # links and nothing an agent decides for itself. A publish step needs a title, and a title
    # is not the output of any skill — it is a judgement the editing agent makes after reading
    # the brief. Asked for by name, it becomes a field the next step can wire to.
    wanted = [str(name).strip() for name in (produces or []) if str(name).strip()]
    asked_for = ""
    if wanted:
        asked_for = (
            "\n\nWHEN YOU HAVE FINISHED, end your reply with a JSON object on its own line "
            "containing exactly these keys:\n"
            + "\n".join(f"  {name}" for name in wanted)
            + "\nIf the instruction points at a document for any of them, read it and take the "
              "value from there rather than inventing one. Where a value genuinely is not "
              "available, use an empty string — do not guess."
            + "\n\nIF A VALUE IS COPIED OUT OF A DOCUMENT, DO NOT RETYPE IT. Point at it:\n"
              '  \"name\": {\"copied\": true, \"lines\": \"12-18\"}\n'
              "using the numbers from `numbered_text` in the result of the read, and the exact "
              "original is taken from the document. One line is \"12\"; a value that runs over "
              "several is \"12-18\", and everything between comes through as written."
              "\n\nTHE RANGE HAS TO COVER THE VALUE AND NOTHING ELSE. Leave out the heading "
              "that labels it — a line like \"**Title:**\" or \"Description:\" is not part of "
              "the value and would be published as though it were. And take ALL of it: if the "
              "value continues over blank lines and further paragraphs, the range runs to the "
              "last line of it, stopping before whatever comes next. Read the numbered copy and "
              "check both ends of your range against it before you answer. If you have "
              "no numbered copy, quote instead — "
              '\"value\": \"the first words\", \"to\": \"the last words\" — '
              "but the numbers are better, because a QUOTE IS RETYPED TOO and arrives damaged."
              "\n\nWHY THIS MATTERS. Text copied through a reply comes out changed: Tamil, "
              "Hindi, Arabic and every other non-Latin script come out as different words, and "
              "long text comes out shortened. Write a value out in full only when it is YOURS — "
              "something you wrote rather than found."
        )

    facts = ""
    if context:
        # RECORDED, NOT NARRATED. `media_artifact_id: art_9f3c` is a fact the agent must use
        # verbatim; a sentence about it is something a model will paraphrase.
        facts = ("\n\nWHAT EARLIER STEPS PRODUCED — use these values exactly as written:\n"
                 + "\n".join(f"  {k}: {json.dumps(v) if not isinstance(v, str) else v}"
                             for k, v in context.items() if v not in (None, "", {}, [])))

    from .agent_loop import load_agent_run, run_agent_loop

    result = run_agent_loop(
        connection,
        settings,
        project_id,
        agent_brief(connection, project_id, role),
        f"WHAT I WANT DONE\n{said}{facts}{asked_for}",
        max_steps=int(max_steps or DEFAULT_MAX_STEPS),
        # THE WHOLE BOUND, IN ONE PLACE. `agent_loop` reads this and offers these skills as the
        # only tools — no natives, no MCP servers the agent was not given.
        finalize_ctx={
            "agent_step": {
                "role_key": role,
                "skill_ids": [str(s["id"]) for s in skills],
                "actor": actor,
            },
        },
    )
    run_of_the_agent = str(result.get("run_id") or "")
    produced = _what_the_skills_returned(connection, run_of_the_agent)
    # DECLARED VALUES WIN OVER SKILL OUTPUT FOR THEIR OWN KEYS. A skill returning a field called
    # `title` means the name of a Drive file; a declared `title` is the answer to the question
    # the step was asked. Conflating them would publish a video called "short.mp4".
    declared, copied_keys = _declared(str(result.get("output_text") or ""), wanted)
    if copied_keys:
        # THE ORIGINAL BYTES, NOT THE AGENT'S COPY OF THEM. See `_copied_exactly`.
        documents = _documents_the_agent_read(connection, run_of_the_agent)
        for key, (line_range, ends_with) in copied_keys.items():
            original = _lines_of(documents, line_range)
            if original is None:
                original = _copied_exactly(str(declared.get(key) or ""), documents,
                                           ends_with=ends_with)
            if original:
                declared[key] = _without_its_label(original, key)
            elif not str(declared.get(key) or "").strip():
                # It pointed at nothing findable and quoted nothing. An empty value must not
                # travel on as an answer.
                declared.pop(key, None)
    produced.update(declared)
    return {
        # EVERY FIELD THE SKILLS RETURNED, FLATTENED IN, so a later step can wire from this one
        # with {from_step: ..., field: media_artifact_id} exactly as it does from a fixed step.
        # Without this an agent step is a dead end: it does the work and the next step cannot
        # read the result, which is the one thing that would make the whole idea unusable.
        **produced,
        "agent": role,
        "chosen_because": chosen["why"],
        "chosen_how": chosen["how"],
        "skills_offered": [str(s["id"]) for s in skills],
        "skills_used": produced.pop("_used", []) if "_used" in produced else _used_skills(
            connection, str(result.get("run_id") or "")),
        "status": result.get("status"),
        "summary": result.get("output_text") or "",
        "tool_calls": result.get("tool_calls") or len(result.get("transcript") or []),
        "agent_run_id": result.get("run_id"),
        # Set when the agent reached a skill that needs a person. The step waits on this, the
        # same way a tier-3 capability step does.
        "decision_id": result.get("decision_id"),
        "transcript": result.get("transcript") or [],
    }


def _tool_messages(connection: Connection, run_id: str) -> list[dict[str, Any]]:
    """The results the agent's tool calls actually returned.

    READ BACK OFF THE RUN, because the loop executes the tools itself and the transcript keeps
    only a 240-character preview. The run's `messages` hold the real payloads.
    """
    if not run_id:
        return []
    from .agent_loop import load_agent_run

    run = load_agent_run(connection, run_id) or {}
    out: list[dict[str, Any]] = []
    for message in run.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content") or ""))
        except ValueError:
            # A payload long enough to be truncated at 16k does not parse. Skipped rather than
            # guessed at — half a JSON object is not a result.
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def _what_the_skills_returned(connection: Connection, run_id: str) -> dict[str, Any]:
    """The union of every successful skill result, later calls winning.

    LAST WINS, AND THAT IS THE POINT. An agent that fetches, then renders, then verifies has
    produced three `media_artifact_id`s, and the one a publish step wants is the last. Ordering
    by the conversation is what makes "the result of the work" mean the right thing.

    Only dict-valued, JSON-safe fields, and never the bookkeeping the runtime adds around a
    result — an `invocation_id` leaking into a step's output would be wired by somebody.
    """
    skip = {"invocation_id", "status", "mode", "attempts", "caller", "skill_id",
            "permission_decision", "reason", "cost_receipt_id", "reservation_id",
            "estimate_micros", "actual_cost_micros"}
    merged: dict[str, Any] = {}
    for payload in _tool_messages(connection, run_id):
        if str(payload.get("status") or "").lower() in {"denied", "failed", "error",
                                                        "deferred_for_approval"}:
            continue
        # A runtime invocation wraps the executor's own return in `result`; a native-shaped
        # reply is flat. Both happen, so both are read.
        for source in (payload.get("result"), payload):
            if not isinstance(source, dict):
                continue
            for key, value in source.items():
                if key in skip or key.startswith("_"):
                    continue
                if isinstance(value, (str, int, float, bool)) or value is None \
                        or isinstance(value, (list, dict)):
                    merged[key] = value
    return merged


def _used_skills(connection: Connection, run_id: str) -> list[str]:
    """Which skills the agent actually chose. The answer to "what did it do"."""
    used: list[str] = []
    for payload in _tool_messages(connection, run_id):
        skill_id = str(payload.get("skill_id") or "").strip()
        if skill_id and skill_id not in used:
            used.append(skill_id)
    return used


def _documents_the_agent_read(connection: Connection, run_id: str) -> list[str]:
    """Every document the agent read during this step, as text, in the order it read them."""
    documents: list[str] = []
    for payload in _tool_messages(connection, run_id):
        if str(payload.get("status") or "").lower() in {"denied", "failed", "error",
                                                        "deferred_for_approval"}:
            continue
        for source in (payload.get("result"), payload):
            if not isinstance(source, dict):
                continue
            body = source.get("text")
            if isinstance(body, str) and body.strip() and body not in documents:
                documents.append(body)
    return documents


#: How alike a declared value and a span of the document have to be before the span is taken as
#: the thing the agent meant. The agent has already SAID it was copying, so this only has to
#: rule out a match that is nothing to do with it. The corrupt Tamil title that prompted all of
#: this scored 0.78 against its own line and 0.40 against the next best.
COPY_FLOOR = 0.35

#: The longest run of lines treated as one value. A title is one line; a description is a few
#: paragraphs. Beyond this it is the document, not a value out of it.
MAX_COPIED_LINES = 20


#: A heading is short. Anything longer is a sentence that happens to end in a colon.
LONGEST_LABEL = 40


def _without_its_label(value: str, key: str) -> str:
    """A copied value with the heading that labelled it removed — but only on evidence.

    A brief writes its title under a heading, and an agent pointing at a range keeps including
    it, twice over after being told not to:

        title        "**Title:**\nமதுராந்தகத்தில் இடைத்தேர்தலே தேவையில்லை! …"
        description  "**Description:**\n\nமதுராந்தகத்தில் இடைத்தேர்தல் …"

    which is "**Title:**" published as part of the title. Telling the agent again is not a fix —
    it has been told — and trimming any first line that looks like a heading would eventually eat
    a real one.

    So it acts only when the line SAYS what it labels: a short line ending in a colon, naming the
    field it sits above. "**Title:**" over a title, "Description:" over a description. Anything
    else is left exactly as the document has it, because a value with a stray heading is a
    nuisance and a value with its first line silently removed is a lie.
    """
    lines = value.split("\n")
    if len(lines) < 2 or not key.strip():
        return value
    label = lines[0].strip().strip("*_#").strip()
    if len(label) > LONGEST_LABEL or not label.endswith(":"):
        return value
    if key.strip().lower() not in label.lower():
        return value
    return "\n".join(lines[1:]).strip()


def _lines_of(documents: list[str], line_range: str) -> Optional[str]:
    """The lines an agent pointed at, exactly as the document has them.

    A LINE NUMBER IS THE ONE POINTER THAT SURVIVES BEING WRITTEN OUT. An agent asked to quote
    the start of a Tamil title pointed at "மதுராந்தகத்தில்இட்டற்றலெ...?" — its own damaged copy of
    the words — and the closest match to that was the wrong paragraph. Digits cannot be damaged,
    so `numbered_text` goes out with every document and the agent quotes "12-18".

    Resolved against the LAST document read first: an agent that read a brief and then something
    else is pointing at the one it just looked at. Returns None when the range is missing or fits
    no document, and the quoted-words route is tried instead.
    """
    text_value = line_range.strip().replace("–", "-").replace("—", "-")
    if not text_value or not documents:
        return None
    parts = [piece.strip() for piece in text_value.split("-") if piece.strip()]
    try:
        first = int(parts[0])
        last = int(parts[-1]) if len(parts) > 1 else first
    except (ValueError, IndexError):
        return None
    if first < 1 or last < first:
        return None

    for document in reversed(documents):
        lines = document.split("\n")
        if last > len(lines):
            continue
        span = "\n".join(lines[first - 1:last]).strip()
        if span:
            return span
    return None


def _copied_exactly(claimed: str, documents: list[str],
                    ends_with: str = "") -> Optional[str]:
    """The span of the document the agent was pointing at, in the document's own bytes.

    WHY POINTING EXISTS AT ALL. An agent read a brief correctly — the Tamil arrived intact, every
    character of it — and then wrote the title into its reply, and what came out was

        மதுராந்தகத்தில் இடைத்தேர்தலே தேவையில்லை!      what the brief says
        மதுராந்தகத்தில்இட்டைத்தெற்றலே தவெய்ல்ளை!      what the agent typed back

    a different sentence, published as if it were the operator's own words. The description came
    back shortened, with "(full description as extracted from the document)" where the rest
    should have been. Neither is a bug in the reading; both are what happens when text goes
    through a model's reply. No prompt fixes it, because the model is not making a mistake it can
    be told about — it is re-generating the characters.

    So a value that was COPIED is transferred by reference. The agent says which part it means,
    roughly, and the original is lifted out of the document BOSS already holds. A value the agent
    WROTE still comes through as written — that text has no original to be faithful to.

    Returns None when nothing in the documents resembles the claim, and the agent's own words
    stand: better a value that is theirs than a confident substitution of the wrong paragraph.
    """
    wanted = claimed.strip()
    if not wanted or not documents:
        return None

    # (text, which document, which line it starts on) — the position is needed because a value
    # that says where it ENDS is resolved by walking forward from where it begins.
    candidates: list[tuple[str, int, int]] = []
    for which, document in enumerate(documents):
        lines = document.split("\n")
        for start in range(len(lines)):
            if not lines[start].strip():
                continue
            for length in range(1, MAX_COPIED_LINES + 1):
                if start + length > len(lines):
                    break
                span = "\n".join(lines[start:start + length]).strip()
                if span:
                    candidates.append((span, which, start))
    if not candidates:
        return None

    matcher = difflib.SequenceMatcher(autojunk=False)
    best = _closest(matcher, wanted, candidates)
    if best is None:
        return None
    span, which, start = best

    if not ends_with.strip():
        return span

    # THE AGENT SAID WHERE IT ENDS, so the span runs from the line it begins on to the line that
    # closes it. Without this a description of four paragraphs resolves to its first one — which
    # is exactly what the agent pointed at, and a quarter of what it meant.
    lines = documents[which].split("\n")
    tail = [(line, which, index) for index, line in enumerate(lines)
            if index >= start and line.strip()]
    closing = _closest(matcher, ends_with.strip(), tail)
    if closing is None:
        return span
    return "\n".join(lines[start:closing[2] + 1]).strip()


def _closest(matcher: "difflib.SequenceMatcher", wanted: str,
             candidates: list[tuple[str, int, int]]) -> Optional[tuple[str, int, int]]:
    """The candidate most like `wanted`, or None if none of them is like it at all.

    `quick_ratio` is a cheap upper bound, so it narrows the field and only the plausible few are
    measured properly. On a long document that is the difference between milliseconds and
    seconds, on a path every agent step with a copied value walks.
    """
    matcher.set_seq2(wanted)
    rough: list[tuple[float, tuple[str, int, int]]] = []
    for candidate in candidates:
        matcher.set_seq1(candidate[0])
        rough.append((matcher.quick_ratio(), candidate))
    rough.sort(key=lambda pair: pair[0], reverse=True)

    best_score = 0.0
    best: Optional[tuple[str, int, int]] = None
    for _, candidate in rough[:40]:
        matcher.set_seq1(candidate[0])
        score = matcher.ratio()
        if score > best_score:
            best_score, best = score, candidate
    return best if best_score >= COPY_FLOOR else None


def _declared(final_text: str,
              wanted: list[str]) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """The declared values, and which of them the agent said it had COPIED rather than written.

    A copied value is written as {"copied": true, "value": "..."} — see `_copied_exactly` for
    why that exists. A plain string still works and is taken at face value, because an agent
    that wrote the value itself has given us the only version there is.
    """
    raw = _declared_values(final_text, wanted, unwrap=False)
    values: dict[str, Any] = {}
    #: key -> (the line range it pointed at, its rough quote of the last line). Either may be
    #: empty; the range is preferred because digits survive being written out and words do not.
    copied: dict[str, tuple[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            inner = value.get("value")
            if inner in (None, "", [], {}) or not str(inner).strip():
                # A POINTER NEEDS NO QUOTE. `{"copied": true, "lines": "12-18"}` says everything
                # — insisting on a sample of the text alongside it would be asking for the one
                # thing that cannot be copied safely.
                if not str(value.get("lines") or "").strip():
                    continue
                inner = ""
            values[key] = inner
            if bool(value.get("copied")):
                copied[key] = (str(value.get("lines") or ""), str(value.get("to") or ""))
        else:
            values[key] = value
    return values, copied


def _declared_values(final_text: str, wanted: list[str],
                     unwrap: bool = True) -> dict[str, Any]:
    """The values an agent was asked to state, out of the last thing it said.

    `unwrap` flattens a {"copied": true, "value": ...} entry to its value. `_declared` needs the
    wrapper intact to know which values must be lifted out of the document rather than trusted.

    LENIENT ON PURPOSE. A model asked for JSON produces JSON, and also sometimes produces JSON
    inside a fenced block, or after a sentence of preamble. Refusing those would fail a step
    over punctuation, so the LAST balanced object in the reply is taken and read for the keys
    that were asked for. Anything else in it is ignored — this is not a channel for the agent to
    set fields nobody asked about.
    """
    if not wanted or not final_text.strip():
        return {}
    end = final_text.rfind("}")
    while end != -1:
        depth = 0
        for start in range(end, -1, -1):
            if final_text[start] == "}":
                depth += 1
            elif final_text[start] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(final_text[start:end + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        # An EMPTY value is the agent saying "I could not find one" — it is
                        # told to use exactly that — so it must not travel on as an answer. An
                        # empty title reaching YouTube is a video called nothing.
                        found: dict[str, Any] = {}
                        for k in wanted:
                            if k not in parsed:
                                continue
                            value = parsed[k]
                            if isinstance(value, dict) and "value" in value:
                                if unwrap:
                                    value = value.get("value")
                                elif str(value.get("value") or "").strip():
                                    found[k] = value
                                    continue
                            # An EMPTY value is the agent saying "I could not find one" — it is
                            # told to use exactly that — so it must not travel on as an answer.
                            # An empty title reaching YouTube is a video called nothing.
                            if value in (None, [], {}, "") or not str(value).strip():
                                continue
                            found[k] = value
                        if found:
                            return found
                    break
        end = final_text.rfind("}", 0, end)
    return {}
