"""The change record a workflow leaves behind, written for a person.

WHAT WAS WRONG WITH THE FIRST ONE
---------------------------------
It concatenated whatever each step had produced. For a Research-Led Discovery run about an
interview scorecard app, that came out as:

  * the directive, then the whole build brief including the eight-line shared quality bar —
    which is a specification for BOSS, not information for a reader;
  * the directive AGAIN, because the research dossier's own summary starts by restating it;
  * `{"questions": [...], "hypothesis": "Build an interview...", "sources_consulted": [...]}` —
    a JSON object, printed;
  * twenty-three findings, each one a JSON dict on a single line, wrapping mid-key.

The operator's verdict — "I dont think anyone will be able to read that format" — is right, and
it is the smaller half of the problem. The larger half is what the dump BURIED. That research
had reached a conclusion:

    verdict: promising, confidence 0.45
    biggest risk: "The pitch's central differentiator — 'no one else surfaces panel
                   disagreement as a first-class signal' — is factually wrong."
    wedge:  not novelty; a standalone four-screen tool for teams that do not want a whole ATS.

None of that appeared. A reader would have had to parse twenty-three inline JSON dicts to find
out that the research had contradicted the premise of the thing being built. A document that
hides its own conclusion is worse than no document, because it looks like diligence.

SO: A SUMMARY FIRST
-------------------
The record opens with what a person needs in order to decide anything — what was asked, what the
research concluded, what would make it wrong, and what happens next — and the detail follows for
whoever wants it. The dossier is read from the discovery run's own structured state rather than
from `_dossier_brief`, which flattens lists to JSON on purpose because it is written to be fed
to a language model, not read.

Nothing here is generated. Every sentence is a field that some step actually recorded; where a
field is missing the section is absent, and what is missing is listed at the end.
"""

from __future__ import annotations

import re
from typing import Any, Optional

#: The market analysis, in reading order, with the heading each field gets. The dossier stores
#: these as loose keys; a reader wants them in the order an argument is made.
_MARKET_FIELDS: tuple[tuple[str, str], ...] = (
    ("problem", "The problem"),
    ("target_segment", "Who has it"),
    ("demand_evidence", "Evidence of demand"),
    ("competition", "Who else does this"),
    ("market_size", "How big"),
    ("timing", "Why now"),
    ("risks", "Risks"),
)

#: What a research angle is called out loud. `signal_findings` tags each finding with one.
_ANGLE_NAMES = {
    "competitor": "Competitors",
    "market": "Market",
    "customer": "Customers",
    "pricing": "Pricing",
    "technology": "Technology",
    "regulatory": "Regulatory",
    "timing": "Timing",
}


def _text(value: Any) -> str:
    """A field as prose. Dicts and lists are rendered, never dumped."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(f"- {_text(v)}" for v in value if _text(v))
    if isinstance(value, dict):
        # `problem` and `competition` are dicts of labelled prose. Printing the JSON was the
        # single worst thing in the old record; a labelled line per key is the same data.
        return "\n".join(
            f"- **{_label(str(k))}:** {_text(v)}"
            for k, v in value.items() if _text(v)
        )
    return str(value)


def _label(key: str) -> str:
    """A dossier key as a heading. `severity_0_5` read as "severity 0 5" in the first PDF."""
    key = re.sub(r"_(\d+)_(\d+)$", r" (\1-\2)", key)
    return key.replace("_", " ").strip()


def _pct(value: Any) -> str:
    """A 0–1 confidence as a percentage. 0.45 means nothing to most readers; 45% does."""
    try:
        return f"{round(float(value) * 100)}%"
    except (TypeError, ValueError):
        return ""


def _findings_by_angle(findings: Any) -> list[tuple[str, list[dict]]]:
    """Findings grouped by what they are about, in a stable order."""
    if not isinstance(findings, list):
        return []
    grouped: dict[str, list[dict]] = {}
    for item in findings:
        if not isinstance(item, dict):
            continue
        angle = str(item.get("angle") or "other").strip().lower()
        grouped.setdefault(angle, []).append(item)
    return [(a, grouped[a]) for a in sorted(grouped, key=lambda a: (a == "other", a))]


def research_summary(state: dict) -> dict[str, Any]:
    """The three sentences a reader needs from a research run, and nothing else.

    The verdict, what would make it wrong, and what to actually build — which are exactly the
    fields the old record left at the bottom of a JSON dump.
    """
    gate = state.get("gate") if isinstance(state.get("gate"), dict) else {}
    critique = state.get("critique") if isinstance(state.get("critique"), dict) else {}
    outcomes = state.get("candidate_outcomes")
    best = outcomes[0] if isinstance(outcomes, list) and outcomes else {}
    best = best if isinstance(best, dict) else {}
    return {
        "verdict": str(gate.get("verdict") or "").strip(),
        "confidence": gate.get("confidence"),
        "sources": gate.get("cited_sources"),
        "biggest_risk": str(critique.get("biggest_risk") or "").strip(),
        "outcome": str(best.get("outcome") or "").strip(),
        "wedge": str(best.get("wedge") or "").strip(),
        "first_slice": str(best.get("first_prototype_slice") or "").strip(),
    }


def _summary_section(*, directive: str, research: Optional[dict], decision: Optional[dict],
                     built: dict, missing: list[str]) -> list[str]:
    """What a person needs before deciding anything. Everything below this is detail."""
    lines = ["## Summary", ""]
    if directive:
        first = directive.strip().split("\n")[0].strip()
        lines += [f"**Asked for:** {first}", ""]
    if research:
        verdict = research.get("verdict")
        conf = _pct(research.get("confidence"))
        if verdict:
            lines += [
                f"**Research verdict:** {verdict}"
                + (f" (confidence {conf}" if conf else "")
                + (f", {research['sources']} cited sources)" if research.get("sources") and conf
                   else ")" if conf else ""),
                "",
            ]
        # THE PART THAT WAS BURIED. In the run that prompted this, the critique said the
        # product's central differentiator was factually wrong — under twenty-three lines of
        # JSON. If a reader takes one thing from this document it should be this.
        if research.get("biggest_risk"):
            lines += [f"**Biggest risk:** {research['biggest_risk']}", ""]
        if research.get("outcome"):
            lines += [f"**What to build instead, if anything:** {research['outcome']}", ""]
    if decision:
        who = decision.get("approved_by") or "nobody yet"
        lines += [f"**Decision:** {decision.get('question')} — {decision.get('status')} "
                  f"({who}).", ""]
    if built.get("pull_request_url"):
        lines += [f"**Built:** {built['pull_request_url']}", ""]
    if missing:
        lines += [f"**Not part of this run:** {', '.join(missing)}.", ""]
    return lines


def _research_detail(state: dict, summary: dict) -> list[str]:
    """The research, rendered. Same facts as the JSON dump, in reading order."""
    lines: list[str] = ["## What the research found", ""]

    if summary.get("verdict"):
        conf = _pct(summary.get("confidence"))
        lines += [f"Verdict: **{summary['verdict']}**"
                  + (f", confidence {conf}" if conf else "")
                  + (f", from {summary['sources']} cited sources"
                     if summary.get("sources") else "") + ".", ""]

    brief = state.get("research_brief") if isinstance(state.get("research_brief"), dict) else {}
    questions = brief.get("questions")
    if isinstance(questions, list) and questions:
        lines += ["### Questions it set out to answer", ""]
        lines += [f"{i}. {_text(q)}" for i, q in enumerate(questions, 1)]
        lines += [""]

    market = state.get("market_analysis") if isinstance(state.get("market_analysis"), dict) else {}
    for key, heading in _MARKET_FIELDS:
        body = _text(market.get(key))
        if body:
            lines += [f"### {heading}", "", body, ""]

    for angle, items in _findings_by_angle(state.get("signal_findings")):
        lines += [f"### {_ANGLE_NAMES.get(angle, angle.title())}", ""]
        for item in items:
            finding = _text(item.get("finding"))
            if not finding:
                continue
            lines += [f"- {finding}"]
            evidence = _text(item.get("evidence"))
            if evidence:
                lines += [f"  - *Evidence:* {evidence}"]
            url = _text(item.get("citation_url"))
            if url:
                lines += [f"  - Source: {url}"]
        lines += [""]

    critique = state.get("critique") if isinstance(state.get("critique"), dict) else {}
    disconfirming = critique.get("disconfirming_evidence")
    if critique.get("biggest_risk") or disconfirming:
        # NAMED FOR WHAT IT IS. A section called "critique" gets skimmed; a section called
        # "what would make this wrong" gets read, and it is the same content.
        lines += ["### What would make this wrong", ""]
        if critique.get("biggest_risk"):
            lines += [_text(critique["biggest_risk"]), ""]
        if isinstance(disconfirming, list) and disconfirming:
            lines += [_text(disconfirming), ""]

    outcomes = state.get("candidate_outcomes")
    if isinstance(outcomes, list) and outcomes:
        lines += ["### What it recommends", ""]
        for outcome in outcomes[:3]:
            if not isinstance(outcome, dict):
                continue
            conf = _pct(outcome.get("confidence_0_1"))
            head = _text(outcome.get("outcome"))
            if head:
                lines += [f"**{head}**" + (f" — confidence {conf}" if conf else ""), ""]
            for field, label in (("wedge", "The wedge"),
                                 ("rationale", "Why"),
                                 ("first_prototype_slice", "First slice to build"),
                                 ("validation_plan", "How to test it"),
                                 ("target_pilot", "Who to test it with")):
                body = _text(outcome.get(field))
                if body:
                    lines += [f"- *{label}:* {body}"]
            lines += [""]

    sources = brief.get("sources_consulted")
    if isinstance(sources, list) and sources:
        # De-duplicated: the raw list repeats the same Greenhouse URL five times, which in the
        # old record read as five sources.
        seen, unique = set(), []
        for s in sources:
            url = _text(s)
            if url and url not in seen:
                seen.add(url)
                unique.append(url)
        lines += ["### Sources", ""]
        lines += [f"{i}. {u}" for i, u in enumerate(unique, 1)]
        lines += [""]
    return lines


def compose(*, title: str, directive: str, dossier_state: Optional[dict] = None,
            dossier_text: str = "", discovery_id: str = "", prd: str = "",
            meeting_run_id: str = "", decision: Optional[dict] = None, decision_id: str = "",
            pull_request_url: str = "", code_task_run_id: str = "",
            workflow_name: str = "", objective: str = "") -> tuple[str, list[str]]:
    """The record, and the list of sections it actually contains.

    `dossier_state` is the discovery run's own structured state — the good source. `dossier_text`
    is the flattened brief the panel was handed, used only when the structured state cannot be
    read, because half a document beats none.
    """
    state = dossier_state if isinstance(dossier_state, dict) else {}
    summary = research_summary(state) if state else {}
    built = {"pull_request_url": pull_request_url, "code_task_run_id": code_task_run_id}

    present = {
        "research": bool(state or dossier_text or discovery_id),
        "spec": bool(prd),
        "decision": bool(decision or decision_id),
        "build": bool(pull_request_url or code_task_run_id),
    }
    missing = [name for name, ok in (
        ("research", present["research"]), ("a panel spec", present["spec"]),
        ("a decision", present["decision"]), ("a build", present["build"]),
    ) if not ok]

    lines = [f"# {title}", ""]
    if workflow_name:
        lines += [f"*{workflow_name}*", ""]
    lines += _summary_section(directive=directive, research=summary or None,
                              decision=decision, built=built, missing=missing)

    if directive:
        lines += ["---", "", "## What was asked for", "", directive.strip(), ""]

    if state:
        lines += _research_detail(state, summary)
    elif dossier_text:
        # The panel's flattened brief. Marked as second-best rather than passed off as the
        # research itself — it is a summary written for a model, and it shows.
        lines += ["## What the research found", "",
                  "*Summarised from the panel's brief; the full dossier could not be read.*",
                  "", dossier_text.strip(), ""]
    if discovery_id:
        lines += [f"Discovery run: `{discovery_id}`", ""]

    if prd:
        lines += ["---", "", "## The spec the panel produced", "", prd.strip(), ""]
        if meeting_run_id:
            lines += [f"Panel: `{meeting_run_id}`", ""]

    if decision or decision_id:
        lines += ["---", "", "## The decision", ""]
        if decision:
            who = decision.get("approved_by") or "nobody yet"
            lines += [f"**{decision.get('question')}** — {decision.get('status')}, by {who}."]
            if decision.get("resolution_note"):
                lines += ["", _text(decision["resolution_note"])]
        else:
            lines += [f"Recorded as `{decision_id}`."]
        lines += [""]

    if present["build"]:
        lines += ["---", "", "## What was built", ""]
        if pull_request_url:
            lines += [f"Pull request: {pull_request_url}"]
        if code_task_run_id:
            lines += [f"Code task run: `{code_task_run_id}`"]
        lines += [""]

    if objective:
        # LAST, not first. The workflow's objective explains why the run has the shape it has,
        # which is context for a reader who asks — not the thing they opened the document for.
        lines += ["---", "", "## What this workflow was for", "", objective.strip(), ""]

    if missing:
        lines += ["---", "", "## Not part of this run", "",
                  "This workflow did not include " + ", ".join(missing) + ".", ""]

    sections = [name for name, ok in present.items() if ok]
    if directive:
        sections.insert(0, "directive")
    return "\n".join(lines).rstrip() + "\n", sections




# ── Is the document any good? ────────────────────────────────────────────────────────────────
#
# "Have another agent to validate the Document outcome before accepting the doc ... but I also
#  see a risk of going in endless cycle"
#
# The risk is real, and it is not solvable by a better prompt: two agents arguing about whether
# prose is good have no terminating condition, because "good" is a judgement and there is always
# another objection available. What DOES terminate is a checklist — a fixed set of properties
# that are either true or false about the text in front of you.
#
# So the defects this looks for are the ones that actually occurred, stated objectively:
#
#   * raw JSON on the page, which is what made the first record unreadable;
#   * no summary, which is what made it unusable even where it was readable;
#   * the directive printed more than once, because the dossier restates it;
#   * a claim with no source under a heading that promises evidence;
#   * so long nobody will read it.
#
# None of those needs a model, none of them can be argued with, and the check costs nothing —
# so it runs on every document rather than on the ones somebody remembers to review.

#: Above this, a reader skims. The records that prompted this were four to six pages of dump.
_TOO_LONG_CHARS = 45_000

#: A run of characters that only appears when a data structure has been printed instead of
#: rendered. Deliberately narrow: prose contains braces, but not `{"key":`.
_JSON_ON_THE_PAGE = ('{"', '": [', '": {', "', '")


def review(markdown: str, sections: list[str]) -> list[dict[str, str]]:
    """Defects a checklist can see. Empty means nothing OBJECTIVE is wrong with it.

    Deliberately not a verdict on the writing. It is the half of "is this document acceptable"
    that converges, and it runs before any judgement call is worth spending money on.
    """
    text = markdown or ""
    found: list[dict[str, str]] = []

    def flag(kind: str, detail: str) -> None:
        found.append({"kind": kind, "detail": detail})

    if "## Summary" not in text:
        flag("no_summary",
             "the document opens without a summary, so a reader has to assemble the "
             "conclusion themselves")

    leaked = [marker for marker in _JSON_ON_THE_PAGE if marker in text]
    if leaked:
        flag("raw_data",
             f"a data structure was printed rather than rendered (found {leaked[0]!r})")

    if len(text) > _TOO_LONG_CHARS:
        flag("too_long",
             f"{len(text):,} characters — long enough that the summary is the only part "
             "that will be read")

    # The directive appears once under "What was asked for" and once, quoted, in the summary.
    # More than that means a section is restating it — which is exactly what the dossier does.
    if "## What was asked for" in text:
        opening = text.split("## What was asked for", 1)[1].strip().split("\n")[0].strip()
        if opening and len(opening) > 25 and text.count(opening) > 2:
            flag("repeated",
                 "the directive is printed more than twice; a section is restating it")

    if "research" in sections and "### Sources" not in text and "Source:" not in text:
        flag("uncited",
             "research is summarised with no sources listed, so no claim in it can be checked")

    if not sections:
        flag("empty", "the run produced nothing to record")

    return found




def body_for_pdf(markdown: str) -> tuple[str, str, str]:
    """The document split into (title, subtitle, body) for a renderer that draws its own head.

    The first PDF printed the title twice — once in reportlab's title block and again from the
    markdown's own `# Heading` two lines below it. The markdown has to keep its H1, because the
    same text is shown in the console and committed to GitHub as a .md where nothing else
    supplies one; so the split happens here, at the point where a renderer is about to add its
    own.
    """
    lines = (markdown or "").split("\n")
    title = subtitle = ""
    index = 0
    if index < len(lines) and lines[index].startswith("# "):
        title = lines[index][2:].strip()
        index += 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and lines[index].startswith("*") and lines[index].endswith("*"):
        subtitle = lines[index].strip("* ").strip()
        index += 1
    return title, subtitle, "\n".join(lines[index:]).lstrip("\n")


__all__ = ["compose", "research_summary", "review", "body_for_pdf"]
