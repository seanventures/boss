"""The change record, written for a person.

"Is this the output that Document prepares ... I dont think anyone will be able to read that
 format currently displayed in the Workflow"

The first record concatenated each step's output. For a Research-Led Discovery run about an
interview scorecard app that produced: the build brief including its eight-line shared quality
bar, the directive twice (the dossier's own summary restates it), a printed
`{"questions": [...], "hypothesis": ...}` object, and twenty-three research findings as inline
JSON dicts.

The formatting is the smaller half. The larger half is what the dump BURIED — that research had
reached a verdict and a critique saying the product's central differentiator was factually
wrong. A document that hides its own conclusion is worse than no document, because it looks
like diligence.

The fixture is the real dossier from that run, trimmed but structurally identical, so these
tests fail against the shape BOSS actually produces rather than one invented to pass.

Run directly (pytest collection wipes the local BOSS database):
    PYTHONPATH=engine .venv/bin/python tests/test_change_record_is_readable.py
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, "engine")

from boss_engine.services import change_record as cr  # noqa: E402

DOSSIER = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "discovery_dossier.json").read_text())

DIRECTIVE = ("Build an interview scorecard app.\n"
             "Who it is for: a hiring manager tired of vague feedback.")

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
    if not ok:
        _failures.append(name)


def main() -> int:
    md, sections = cr.compose(
        title="Interview Scorecard App", directive=DIRECTIVE,
        dossier_state=DOSSIER, discovery_id="drun_1",
        workflow_name="Research-Led Discovery",
        objective="Decide whether this is worth building at all.",
    )
    summary = md.split("## What was asked for")[0]

    print("== the conclusion is at the top, not at the bottom of a dump ==")
    check("there is a summary", "## Summary" in md)
    check("it states the verdict", "promising" in summary, summary[:200])
    # 0.45 means nothing to most readers; 45% does.
    check("confidence as a percentage", "45%" in summary and "0.45" not in summary)
    # THE BURIED FINDING. The research contradicted the premise of the product; a reader had to
    # parse twenty-three inline JSON dicts to discover that.
    check("the biggest risk is in the summary", "factually wrong" in summary, summary[:400])
    check("and what to build instead", "What to build instead" in summary)
    check("and what the run did not include", "Not part of this run" in summary)

    print("== nothing is dumped ==")
    for marker in ('{"', '": [', '": {'):
        check(f"no {marker!r} anywhere in the document", marker not in md)
    check("findings are grouped and named", "### Competitors" in md, md[:0])
    check("sources are listed", "### Sources" in md)
    # The raw list repeats the same Greenhouse URL; five copies read as five sources.
    sources = md.split("### Sources")[1]
    greenhouse = sources.count("how-scorecard-peeking-can-create-bias-in-hiring")
    check("and de-duplicated", greenhouse == 1, f"appears {greenhouse} times")

    print("== the fields are labelled in words ==")
    check("severity_0_5 does not read as 'severity 0 5'", "severity 0 5" not in md)
    check("it reads as a range", "severity (0-5)" in md, md[md.find("severity") - 40:][:80])

    print("== the checklist agrees ==")
    check("a well-formed record has no defects", cr.review(md, sections) == [],
          str(cr.review(md, sections)))

    print("== and catches the failures that actually happened ==")
    # Each of these is a defect the FIRST record shipped with.
    dumped, _ = cr.compose(title="X", directive="Do a thing.",
                           dossier_text='{"questions": ["a"], "hypothesis": "b"}',
                           discovery_id="drun_2")
    kinds = {d["kind"] for d in cr.review(dumped, ["directive", "research"])}
    check("a printed data structure is flagged", "raw_data" in kinds, str(kinds))
    check("research with no sources is flagged", "uncited" in kinds, str(kinds))
    no_summary = cr.review("# Title\n\nsome prose", ["directive"])
    check("a document with no summary is flagged",
          any(d["kind"] == "no_summary" for d in no_summary))
    check("an empty run is flagged", any(d["kind"] == "empty" for d in cr.review("# T", [])))
    check("a document nobody will read is flagged",
          any(d["kind"] == "too_long" for d in cr.review("## Summary\n" + "x" * 50_000, ["a"])))

    print("== the checklist terminates, which is the point of it ==")
    # A reviewer that can always find another objection never converges. Every check here is a
    # property of the text, so running it twice on the same text gives the same answer, and a
    # fixed document has strictly fewer defects rather than different ones.
    check("it is deterministic", cr.review(md, sections) == cr.review(md, sections))
    fixed = cr.review(md, sections)
    check("a clean document stays clean on a second pass", fixed == [])

    print("== the PDF gets the title, and the body does not repeat it ==")
    title, subtitle, body = cr.body_for_pdf(md)
    check("title extracted", title == "Interview Scorecard App", title)
    check("subtitle extracted", subtitle == "Research-Led Discovery", subtitle)
    # The H1 specifically — `## Summary` is a heading and belongs in the body.
    check("and the H1 is not repeated in the body",
          not body.startswith("# ") and "\n# " not in body, body[:60])
    # The markdown itself keeps its H1: the same text is shown in the console and committed to
    # GitHub as a .md, where nothing else supplies a heading.
    check("but the markdown keeps it", md.startswith("# Interview Scorecard App"))

    print("== a run with no research still produces something honest ==")
    bare, bare_sections = cr.compose(title="Y", directive="Do a thing.")
    check("it has a summary", "## Summary" in bare)
    check("it does not claim research", "What the research found" not in bare)
    check("and says what is missing", "research" in bare.split("Not part of this run")[1],
          bare[-200:])
    check("sections name only what is there", bare_sections == ["directive"], str(bare_sections))

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("the record reads like a document")
    return no_json_anywhere()




def no_json_anywhere() -> int:
    """No structure printed instead of rendered — in the record OR in the panel's brief.

    Reported twice. The first fix rendered the change record from the discovery run's own
    state, which is right, but left two paths still dumping:

      * `_dossier_brief`, which `json.dumps`ed every dict — and which the record FALLS BACK to
        when the structured dossier cannot be read, so the JSON reached the PDF anyway;
      * the same brief is what the agent panel argues from, so the panel had been reading
        `{"questions": [...], "hypothesis": "Build an interview scorecard app.\\nWho it is
        for..."}` truncated at 1200 characters, mid-URL.

    One renderer now serves both, so a fix to either cannot leave the other behind.
    """
    from boss_engine.services import workflow_capability_runner as runner

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'' if ok else f'  <- {detail}'}")
        if not ok:
            failures.append(name)

    print("== the panel's brief renders too ==")
    brief = runner._dossier_brief("Interview Scorecard App", DOSSIER)
    for marker in ('{"', '": [', '": {'):
        check(f"no {marker!r} in the panel brief", marker not in brief)
    check("a nested list is indented, not run on to its label",
          "- **questions:**\n  - " in brief, brief[:200])
    # The panel already HAS the directive; a copy of it truncated mid-word is only noise.
    check("the truncated hypothesis is gone", "hypothesis" not in brief, brief[:400])

    print("== and the record's fallback path is clean ==")
    # When the structured dossier cannot be read, the record uses this text. That path is what
    # put JSON in the PDF, so it is worth asserting end to end rather than by inspection.
    md, sections = cr.compose(title="X", directive="Do a thing.",
                              dossier_text=brief, discovery_id="drun_3")
    kinds = {d["kind"] for d in cr.review(md, sections)}
    check("the fallback record trips no raw-data check", "raw_data" not in kinds, str(kinds))

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        return 1
    print("nothing is dumped, on either path")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
