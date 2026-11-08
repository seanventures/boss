"""The task classes BOSS may find and run on its own, and the actions an ambient agent may take.

THE CATALOGUE IS CODE, NOT CONFIGURATION (design §5, §12.5 "the action catalogue is code")
--------------------------------------------------------------------------------------------
Autonomy is decided per (project, task class), never per project or per role alone (design §3.3
gap 3: "dependency patch: may open PR; schema migration: propose only" was not expressible). A
class names the kind of problem; its DEFAULT level is where every project starts, and its
CEILING is the most autonomy BOSS will ever let it earn in any project. The operator may lower a
ceiling per project (ambient_autonomy.ceiling); nothing raises it above the catalogue's.

The defaults are deliberately conservative (design §8 Phase 0): everything proposes (A1) except
the two classes with objective detectors, small blast radius and outputs that are easy to verify
— fixing CI on BOSS's own PR and a CVE patch to a draft PR — and the SRE staging mitigation the
operator asked for (a reversible staging rollback/restart), which act up to A2. TODO triage
starts at A0: it goes in the digest, it does not file cards (noise is the main metric, §10).

ACTIONS
-------
An ambient agent never does "whatever it decides". It asks `policy.gate(...)` whether a named
ACTION is allowed for its item's class right now. Every action declares the lowest level that
may take it, its risk (which picks the consensus mode, design §11.4) and its reversibility. Four
actions are never decided by a level at all — merge, production deploy, an external send and a
payment always need a person (autonomy.IRREVERSIBLE; the rc10.8 release rule). An action that is
not in this catalogue is refused: a new capability is added here, reviewed, with its risk.

Adding a class or an action is the extension point for the parallel streams (CoS 904, SRE 910,
learning 911): add the entry, give it a level and a risk, and the gate, the matrix endpoint and
the scorecard pick it up with no other change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .. import decide_list as _decide_list

_on_the_list = _decide_list.on_the_list

LEVELS = ("A0", "A1", "A2", "A3")
#: THE ONE NAMING of the autonomy levels (BOSSAPP-913 13-20): Settings, Govern, every tooltip and
#: badge read these words (the wireframe's A0 Ask · A1 Propose · A2 Act · A3 Ship); what a level
#: lets an agent do is LEVEL_MEANINGS, said beside the name, never a second name.
LEVEL_NAMES = {"A0": "Ask", "A1": "Propose", "A2": "Act", "A3": "Ship"}
LEVEL_MEANINGS = {"A0": "Watches and asks; changes nothing",
                  "A1": "Proposes the change; a person decides",
                  "A2": "Acts up to a draft (a branch or draft PR); a person approves",
                  "A3": "Acts and ships on its own, then reports"}
RISKS = ("low", "medium", "high")
REVERSIBILITY = ("reversible", "compensable", "irreversible")


def level_rank(level: Optional[str]) -> int:
    """'A2' -> 2. An unknown or missing level is A0: the least autonomy, never the most."""
    try:
        return LEVELS.index(str(level or "A0"))
    except ValueError:
        return 0


def clamp_level(level: str, ceiling: str) -> str:
    return LEVELS[min(level_rank(level), level_rank(ceiling))]


@dataclass(frozen=True)
class TaskClass:
    key: str
    title: str          # at most ~4 words: it is a badge on the diagram (design §11.0)
    default_level: str
    ceiling: str
    risk: str
    #: The role that owns this class by default (the CoS routes here first; 904 may override).
    #: Every key is a canonical role (agent_world_types.CANONICAL_ROLE_KEYS) except
    #: 'chief_of_staff', which the Chief-of-Staff stream (BOSSAPP-904) ships as a role pack.
    owner_role: str
    #: Which sources' signals usually fall in this class (a hint for triage, never a rule).
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    key: str
    title: str
    min_level: str
    risk: str
    reversibility: str
    #: Never decided by a level: only a person (or a person's standing policy, recorded as a
    #: person's decision) may take it.
    always_a_person: bool = False


TASK_CLASSES: dict[str, TaskClass] = {c.key: c for c in (
    # design §5, rows 1-18, plus the two classes BOSS's own failure stores feed today.
    TaskClass("ci_fix_boss_pr", "Fix CI on BOSS PR", "A2", "A3", "low", "software_engineer",
              ("ci", "github")),
    TaskClass("red_main", "Red main", "A1", "A2", "medium", "sre", ("ci", "github")),
    TaskClass("security_patch", "Security patch", "A2", "A3", "medium", "security_reviewer",
              ("osv", "scan")),
    TaskClass("dependency_upgrade", "Dependency upgrade", "A1", "A3", "low", "software_engineer",
              ("scan", "schedule")),
    TaskClass("flaky_test", "Flaky test", "A1", "A3", "low", "qa", ("ci",)),
    TaskClass("missing_tests", "Missing tests", "A1", "A2", "low", "qa", ("ci", "scan")),
    TaskClass("doc_drift", "Doc drift", "A1", "A2", "low", "docs_writer", ("scan", "kg")),
    TaskClass("todo_triage", "TODO triage", "A0", "A1", "low", "chief_of_staff", ("scan",)),
    TaskClass("dead_code", "Dead code", "A1", "A2", "medium", "software_engineer", ("scan", "kg")),
    TaskClass("issue_triage", "Issue triage", "A1", "A2", "low", "software_engineer", ("github",)),
    TaskClass("incident_response", "Incident", "A1", "A3", "high", "sre", ("alert",)),
    TaskClass("incident_staging_mitigation", "Staging mitigation", "A2", "A3", "medium", "sre",
              ("alert",)),
    TaskClass("perf_regression", "Performance regression", "A1", "A2", "medium", "sre",
              ("alert", "ci")),
    TaskClass("stale_handoff", "Stalled handoff", "A1", "A2", "low", "chief_of_staff",
              ("pr", "shepherd")),
    TaskClass("cost_anomaly", "Cost anomaly", "A1", "A3", "low", "finops_sentinel", ("cost",)),
    TaskClass("platform_self_fix", "BOSS self-fix", "A1", "A2", "medium", "software_engineer",
              ("workflow", "build")),
    TaskClass("release_notes", "Release notes", "A1", "A2", "low", "docs_writer", ("github",)),
    TaskClass("knowledge_refresh", "Knowledge refresh", "A1", "A3", "low", "archivist", ("kg",)),
    TaskClass("iac_drift", "Infra drift", "A1", "A1", "high", "infra_steward", ("scan",)),
    TaskClass("workflow_failure", "Failed step", "A1", "A2", "medium", "software_engineer",
              ("workflow", "build")),
    TaskClass("system_health", "System health", "A1", "A2", "medium", "infra_steward",
              ("doctor",)),
    # BOSSAPP-909: a repository Discovery finds with no CI at all. Setting it up is a draft PR a
    # person reviews; no class gains more than A2 without CI to check its own work.
    TaskClass("ci_setup", "Set up CI", "A1", "A2", "low", "operations_engineer", ("scan",)),
    # BOSSAPP-911: a Recommended-roadmap item. BOSS only ever PROPOSES here (ceiling A1): a
    # person accepts it into a ticket, an epic or a PRD panel, or rejects it with a reason.
    TaskClass("roadmap_item", "Roadmap item", "A1", "A1", "low", "pm",
              ("kg", "cost", "ci", "build", "person")),
)}

ACTIONS: dict[str, Action] = {a.key: a for a in (
    # A0: watch and remember.
    Action("observe", "Record and digest", "A0", "low", "reversible"),
    Action("record_finding", "Post a finding", "A0", "low", "reversible"),
    # A1: tell a person and ask (LangChain's notify / question / review, design §2.2).
    Action("propose", "Propose", "A1", "low", "reversible"),
    Action("ask_question", "Ask a question", "A1", "low", "reversible"),
    Action("comment", "Comment", "A1", "low", "compensable"),
    Action("convene_meeting", "Convene a panel", "A1", "low", "reversible"),
    # Handing an item to the agent that owns its class changes nothing outside BOSS: the owner
    # still acts only within the class's level (its own gate). A routine hand-off is what an
    # always-on agent does by itself from A1; it shows on the board and in the digest.
    Action("route", "Hand to its owner", "A1", "low", "reversible"),
    # A nudge is the Chief of Staff asking the agent that holds stalled work (the Shepherd, for
    # a build) to look at it again: a note inside BOSS, nothing outside it changes, and it is
    # not on the compliance boundary (production deploy, merge, spend, secrets, external
    # communication, deletion, identity). It is an agent's to do from A1, like a hand-off;
    # whatever the woken agent then does passes its own gate (2026-11-07, helpdesk's "2 nudges"
    # card asked a person for two nudges the Chief of Staff could have sent itself).
    Action("nudge", "Nudge", "A1", "low", "reversible"),
    # A2: do the work up to a reviewable artifact or a reversible staging change.
    Action("label", "Label", "A2", "low", "reversible"),
    Action("start_workflow", "Start a run", "A2", "medium", "reversible"),
    Action("open_draft_pr", "Open draft PR", "A2", "medium", "reversible"),
    Action("staging_rollback", "Roll back staging", "A2", "medium", "reversible"),
    Action("staging_restart", "Restart staging", "A2", "medium", "reversible"),
    Action("silence_rule", "Silence alert ≤1h", "A2", "medium", "reversible"),
    # A3: act and report. Production mitigation is HIGH risk: a squad vote AND a person
    # (consensus mode by risk, collab.consensus_mode), whatever the level.
    Action("prod_rollback", "Roll back prod", "A3", "high", "reversible"),
    Action("prod_restart", "Restart prod", "A3", "high", "reversible"),
    Action("quarantine_test", "Quarantine test", "A3", "low", "reversible"),
    # Never a level's call: these are entries on the ONE compliance list (decide_list.py,
    # Govern › Where people decide), which says so, not this catalogue.
    Action("merge", "Merge", "A3", "high", "irreversible", always_a_person=_on_the_list("merge")),
    Action("deploy_prod", "Deploy to prod", "A3", "high", "irreversible",
           always_a_person=_on_the_list("deploy_prod")),
    Action("send_external", "Send externally", "A3", "high", "irreversible",
           always_a_person=_on_the_list("send_external")),
    Action("pay", "Pay", "A3", "high", "irreversible", always_a_person=_on_the_list("pay")),
)}

#: The actions no autonomy level can take: the list's own (decide_list.AMBIENT_ACTIONS).
ALWAYS_A_PERSON = frozenset(k for k, a in ACTIONS.items() if a.always_a_person)
assert ALWAYS_A_PERSON == _decide_list.AMBIENT_ACTIONS, (
    "every ambient action on the compliance list is in this catalogue, and only those")


def task_class(key: str) -> TaskClass:
    found = TASK_CLASSES.get(str(key or ""))
    if found is None:
        raise ValueError(f"unknown task class {key!r}; add it to ambient/task_classes.py")
    return found


def action(key: str) -> Action:
    found = ACTIONS.get(str(key or ""))
    if found is None:
        raise ValueError(f"unknown ambient action {key!r}: the action catalogue is code "
                         "(ambient/task_classes.py)")
    return found
