"""The Acceptance Agent's battle-test fixtures (epic BOSSAPP-1213, AA-C BOSSAPP-1217).

NORTHGATE V1 (engine/tests/fixtures/northgate_v1/): the build BOSS delivered on 2026-11-15 — a
Node prototype of a Go API gateway — and what a person's acceptance check found on it.

  tree.bundle      one commit holding exactly the tree of 92f7785 (`expected.yaml` names its git
                   tree id, and `checkout` refuses a bundle whose tree differs)
  ticket.md        the source ticket the build started from
  instructions.md  the build spec's instructions as the run held them (brief + PRD)
  expected.yaml    the person's twelve request-changes points, word for word, each with the
                   contract items it concerns, whether the replay MUST catch it, and the facts on
                   the tree that make it a real gap; the catch threshold (11 of 12) and the HelpDesk
                   positive control

`score` is the rule the replay (AA-G) is held to: a point is CAUGHT when any contract item it
concerns is not met, a FALSE MET when every one of them reads met. No customer branding is in any
of these files; test_acceptance_northgate_fixture.py checks it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NORTHGATE = FIXTURES / "northgate_v1"


@dataclass(frozen=True)
class Point:
    id: str
    key: str
    kind: str                      # gap | keep
    title: str
    required: bool
    contract_items: tuple[str, ...]
    human_text: str
    facts: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


@dataclass
class NorthgateV1:
    root: Path
    expected: dict[str, Any]
    instructions: str
    ticket_text: str

    @property
    def points(self) -> list[Point]:
        return [Point(id=p["id"], key=p["key"], kind=p.get("kind", "gap"), title=p["title"],
                      required=bool(p.get("required")),
                      contract_items=tuple(p.get("contract_items") or ()),
                      human_text=p["human_text"], facts=tuple(p.get("facts") or ()))
                for p in self.expected["points"]]

    @property
    def gaps(self) -> list[Point]:
        return [p for p in self.points if p.kind == "gap"]

    @property
    def required_catches(self) -> list[str]:
        return list(self.expected["required_catches"])

    @property
    def catch_threshold(self) -> int:
        return int(self.expected["catch_threshold"])

    def spec(self) -> dict[str, Any]:
        """The build spec as today's pipeline makes it: the instructions, and the PRD's
        tested-and-gating list as the acceptance criteria (prd_acceptance_inputs)."""
        from boss_engine.services.workflow_capability_runner import prd_acceptance_inputs

        return {"instructions": self.instructions,
                **prd_acceptance_inputs({}, self.instructions)}

    def ticket(self) -> dict[str, Any]:
        """The ticket as a run holds it (ticket_sync.snapshot's shape)."""
        t = self.expected["ticket"]
        return {"provider": t["provider"], "key": t["key"], "id": t["key"],
                "url": f"https://tickets.example.test/browse/{t['key']}", "title": t["title"],
                "body": self.ticket_text, "acceptance_criteria": [], "updated_at": "2026-11-14"}

    def checkout(self, dest: Path) -> Path:
        """The delivered tree, cloned from the bundle into `dest` (a fresh directory). Refuses a
        bundle whose tree is not 92f7785's."""
        if not shutil.which("git"):
            raise RuntimeError("git is required to check the fixture out")
        tree = self.expected["tree"]
        subprocess.run(["git", "clone", "-q", "--branch", tree["branch"],
                        str(self.root / tree["bundle"]), str(dest)], check=True)
        got = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD^{tree}"], check=True,
                             capture_output=True, text=True).stdout.strip()
        if got != tree["source_tree"]:
            raise RuntimeError(f"the bundle's tree {got} is not {tree['source_commit']}'s")
        return dest

    def score(self, statuses: Mapping[str, str]) -> dict[str, Any]:
        """How a verdict ({contract item id: met|partial|missing|human|unjudged}) fares against
        the person's check: which gaps it caught, which required catches it missed, and which
        gaps it called met (false MET — never allowed)."""
        caught, false_met = [], []
        for p in self.gaps:
            seen = [str(statuses.get(i) or "unjudged") for i in p.contract_items]
            if seen and all(s == "met" for s in seen):
                false_met.append(p.id)
            elif any(s != "met" for s in seen):
                caught.append(p.id)
        keys = {p.id: p.key for p in self.points}
        missed = [k for k in self.required_catches
                  if not any(keys[c] == k for c in caught)]
        return {"caught": caught, "false_met": false_met, "missed_required": missed,
                "passes": (len(caught) >= self.catch_threshold and not missed
                           and not false_met)}


def load_northgate_v1() -> NorthgateV1:
    expected = yaml.safe_load((NORTHGATE / "expected.yaml").read_text(encoding="utf-8"))
    return NorthgateV1(root=NORTHGATE, expected=expected,
                       instructions=(NORTHGATE / expected["spec"]["instructions"]).read_text(
                           encoding="utf-8"),
                       ticket_text=(NORTHGATE / expected["ticket"]["file"]).read_text(
                           encoding="utf-8"))


def fact_holds(tree: Path, fact: Mapping[str, Any]) -> tuple[bool, str]:
    """One fact about the delivered tree (`expected.yaml` points[].facts): absent/present paths,
    and a file that lacks/has a pattern. (holds, what was checked)."""
    if "absent" in fact:
        return not (tree / fact["absent"]).exists(), f"{fact['absent']} is absent"
    if "present" in fact:
        return (tree / fact["present"]).is_file(), f"{fact['present']} is present"
    for key, want in (("file_lacks", False), ("file_has", True)):
        if key in fact:
            path, pattern = fact[key]["path"], fact[key]["pattern"]
            body = (tree / path).read_text(encoding="utf-8")
            found = re.search(pattern, body) is not None
            return found is want, f"{path} {'has' if want else 'lacks'} /{pattern}/"
    raise ValueError(f"unknown fact {dict(fact)}")


# ---------------------------------------------------------------------------------------------
# The positive control: HelpDesk stats, known good (the harness benchmark's own fixture)
# ---------------------------------------------------------------------------------------------

@dataclass
class HelpDeskControl:
    task: dict[str, Any]
    fixture: Path
    overlay: Path
    acceptance: Path
    include: list[Path]
    verdict: str
    max_human_items: int

    def spec(self) -> dict[str, Any]:
        return {"instructions": self.task["instructions"],
                "acceptance_criteria": list(self.task["acceptance_criteria"])}

    def build(self, dest: Path) -> Path:
        """The known-good tree: the fixture with the task's solution laid over it, and the
        hidden acceptance tests where the benchmark puts them (bench_acceptance/)."""
        shutil.copytree(self.fixture, dest)
        shutil.copytree(self.overlay, dest, dirs_exist_ok=True)
        hidden = dest / "bench_acceptance"
        shutil.copytree(self.acceptance, hidden)
        for extra in self.include:
            shutil.copy2(extra, hidden / extra.name)
        return dest


def load_helpdesk_control(expected: Optional[Mapping[str, Any]] = None) -> HelpDeskControl:
    pc = dict((expected or load_northgate_v1().expected)["positive_control"])
    suite = yaml.safe_load((REPO / pc["suite"]).read_text(encoding="utf-8"))
    task = next(t for t in suite["tasks"] if t["id"] == pc["task"])
    bench = (REPO / pc["suite"]).parent
    return HelpDeskControl(task=task, fixture=REPO / pc["fixture"], overlay=REPO / pc["overlay"],
                           acceptance=bench / task["acceptance"]["hidden"],
                           include=[bench / p for p in task["acceptance"].get("include") or []],
                           verdict=pc["verdict"], max_human_items=int(pc["max_human_items"]))
