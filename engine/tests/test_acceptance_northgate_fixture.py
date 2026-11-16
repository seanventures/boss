"""THE NORTHGATE V1 REPLAY FIXTURE (BOSSAPP-1217, epic BOSSAPP-1213 AA-C).

The Acceptance Agent is proved on the case that made it necessary: Northgate v1 at 92f7785, a
Node prototype of a Go gateway that shipped with "Automated checks: passed" while a person's
acceptance check found twelve things to change. These tests prove the FIXTURE is right — that it
loads, that its tree is that commit's, that every gap it lists is a real gap on that tree, that
the contract it implies is today's 23 items (48 with its ticket, BOSSAPP-1233), and that the scoring rule the replay will be held to
(>= 11 of 12 caught, the five required catches among them, no false MET) means what it says.
The replay itself is AA-G's gate. A HelpDesk known-good app is the positive control.
"""
from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess

import pytest

from boss_engine.services import acceptance_gate as ag
from boss_engine.services.acceptance import contract as ac

from ._acceptance_fixtures import NORTHGATE, fact_holds, load_helpdesk_control, load_northgate_v1

#: The customer's names, kept out of this file too (rot13), so a grep for them finds nothing.
_NAMES = [codecs.decode(n, "rot13") for n in ("nzrk", "nzrevpna rkcerff")]
BRANDING = re.compile(r"(?i)\b(" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in _NAMES)
                      + r")\b")


@pytest.fixture(scope="module")
def ng():
    return load_northgate_v1()


@pytest.fixture(scope="module")
def tree(ng, tmp_path_factory):
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    return ng.checkout(tmp_path_factory.mktemp("northgate") / "tree")


def test_the_fixture_loads_and_lists_its_expected_catches(ng):
    assert [p.id for p in ng.points] == [f"RC-{n}" for n in range(1, 13)]
    assert ng.catch_threshold == 11 and ng.expected["verdict"] == "not_met"
    # The five the replay must catch, whatever else it catches.
    assert ng.required_catches == ["language", "rulebook_20", "hot_reload_lint", "gwlint_fix",
                                   "make_run_staging"]
    required = {p.key: p for p in ng.points if p.required}
    assert set(required) == set(ng.required_catches)
    assert "Go" in required["language"].human_text
    assert ">=20 rules" in required["rulebook_20"].human_text
    assert "removing auth is rejected" in required["hot_reload_lint"].human_text
    assert "--fix" in required["gwlint_fix"].human_text
    assert "make run-staging" in required["make_run_staging"].human_text
    # Eleven gaps and one "keep" list: 11 of 12 is every gap.
    assert len(ng.gaps) == 11 and [p.id for p in ng.points if p.kind == "keep"] == ["RC-12"]
    # RC-9 (the shipped tenant configs fail gwlint) is in no contract item: a catch beyond the
    # contract, made by a FINDING line. Every other gap names the contract items it concerns.
    beyond = [p for p in ng.points if p.kind == "finding"]
    assert [p.id for p in beyond] == ["RC-9"] and beyond[0].contract_items == ()
    assert all(p.contract_items for p in ng.points if p.kind == "gap")


def test_no_customer_branding_anywhere_in_the_fixture(ng, tree):
    for path in NORTHGATE.iterdir():
        if path.suffix in (".md", ".yaml"):
            assert not BRANDING.search(path.read_text(encoding="utf-8")), path.name
    hits = subprocess.run(["git", "-C", str(tree), "grep", "-I", "-i", "-E", "|".join(_NAMES)],
                          capture_output=True, text=True)
    assert hits.stdout == "", "the delivered tree carries customer branding"
    log = subprocess.run(["git", "-C", str(tree), "log", "--format=%an %ae %s"],
                         capture_output=True, text=True, check=True).stdout
    assert not BRANDING.search(log)


def test_the_bundle_holds_exactly_the_delivered_tree(ng, tree):
    got = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD^{tree}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert got == ng.expected["tree"]["source_tree"]
    assert (tree / "package.json").is_file() and (tree / "Makefile").is_file()


def test_every_gap_is_a_real_gap_on_the_tree(ng, tree):
    """Each point's facts hold at 92f7785: no go.mod, the rulebook is JavaScript, reload never
    consults the rules, --fix re-dumps the YAML, the README has no quick start..."""
    for p in ng.points:
        for fact in p.facts:
            holds, what = fact_holds(tree, fact)
            assert holds, f"{p.id} ({p.key}): expected {what}"
    stated = {p.key for p in ng.points if p.facts}
    assert set(ng.required_catches) <= stated, "every required catch has evidence on the tree"


def test_the_contract_is_todays_23_items_and_every_point_names_real_ones(ng):
    spec = ng.spec()
    contract_items = ac.build_items(ac.gather_sources(spec))
    exp = ng.expected["contract"]
    assert len(contract_items) == exp["items"] == 23
    assert [i["source"] for i in contract_items].count("must_have") == exp["from_brief"]
    assert [i["source"] for i in contract_items].count("prd_gating") == exp["from_prd"]
    # The in-build gate reads the same list (its own per-item cap aside).
    assert [i["text"] for i in ag.acceptance_items(spec)] == [i["text"] for i in contract_items]
    ids = {i["id"] for i in contract_items}
    for p in ng.points:
        assert set(p.contract_items) <= ids, p.id
    texts = {i["id"]: i["text"] for i in contract_items}
    assert "(Go)" in texts["AC-1"] and "at least 20" in texts["AC-3"]
    assert texts["AC-7"].startswith("`make run-staging`")


def test_with_its_ticket_the_contract_leads_with_the_tickets_acceptance_list(ng):
    sources = ac.gather_sources(ng.spec(), ticket=ng.ticket())
    assert [s["kind"] for s in sources] == ["ticket", "brief", "prd", "criteria"]
    items = ac.build_items(sources)
    ticket = [i for i in items if i["source"] == "ticket"]
    exp = ng.expected["with_ticket"]
    # BOSSAPP-1233: every requirement the ticket states, its "Acceptance" list first.
    assert len(ticket) == exp["ticket_items"] == 26 and ticket[0]["id"] == "AC-1"
    assert ticket[0]["text"].startswith("`make run-staging` starts the gateway")
    # The ticket's "gwlint validate examples/lumen-pay.good.yaml exits 0" is the PRD's word for
    # word: held once, under the ticket (the first source that said it).
    assert len(items) == 23 + 26 - 1 == exp["items"]
    assert sum(1 for i in items if "lumen-pay.good.yaml` exits 0" in i["text"]) == 1
    # The three things the 2026-11-15 run never checked are items, word for word.
    for words in exp["word_for_word"]:
        assert any(words in i["text"] and i["source"] == "ticket" for i in items), words
    # All judged: the gate's passes of MAX_ITEMS cover every item.
    assert -(-len(items) // ag.MAX_ITEMS) == 2


def test_a_rules_file_nothing_loads_is_not_the_rulebook_delivered(ng, tree, tmp_path):
    """BOSSAPP-1233: the reviewer's MET on the rulebook item stands only when code in the tree
    loads the file; a rules YAML beside hard-coded checks reads PARTIAL."""
    case = ng.expected["unwired_evidence"]
    item = next(i for i in ac.ticket_items(ng.ticket()) if case["item_contains"] in i)
    items = [{"n": 1, "text": item, "source": "ticket"}]
    work = tmp_path / "tree"
    shutil.copytree(tree, work, ignore=shutil.ignore_patterns(".git"))
    (work / case["plant"]).parent.mkdir(parents=True, exist_ok=True)
    (work / case["plant"]).write_text("rules:\n  - id: SEC-001\n", encoding="utf-8")
    reply = f"ITEM 1: {case['reply']}\nACCEPTANCE: MET\n"
    got = ag.parse_acceptance(reply, items, root=work)[0]
    assert got["status"] == case["verdict"] == "partial"
    assert "loadRulebook is not in the code of gwlint/src/rules.js" in got["evidence"]
    # Wired: code that loads the file makes the same answer stand.
    with open(work / case["wired"]["path"], "a", encoding="utf-8") as fh:
        fh.write(case["wired"]["append"])
    assert ag.parse_acceptance(reply, items, root=work)[0]["status"] == case["wired_verdict"]
    # The reviewer is told to show the wiring.
    prompt = ag.acceptance_prompt(items, title="t", stack={}, tree="")
    assert "WIRED" in prompt and "rules YAML the linter never reads" in prompt
    assert "LOADED BY <file>:<function>" in prompt


def test_the_scoring_rule_means_what_it_says(ng):
    every_ac = {f"AC-{n}" for n in range(1, 24)}
    # A verdict that finds every gap passes; one that calls a gap met never does.
    all_missing = {i: "missing" for i in every_ac}
    score = ng.score(all_missing, findings={"shipped_configs_pass"})
    assert score["passes"] and len(score["caught"]) == 11 and not score["false_met"]
    # Without the finding beyond the contract it is 10 of 12: still short of the threshold of 11.
    score = ng.score(all_missing)
    assert len(score["caught"]) == 10 and "RC-9" not in score["caught"] and not score["passes"]
    all_met = {i: "met" for i in every_ac}
    score = ng.score(all_met)
    assert not score["passes"] and len(score["false_met"]) == 10       # RC-9 is not an item
    assert set(score["missed_required"]) == set(ng.required_catches)
    # Missing one REQUIRED catch fails even with 10 others caught... and calling it met is a
    # false MET on top.
    lang = next(p for p in ng.points if p.key == "language")
    almost = {**all_missing, **{i: "met" for i in lang.contract_items}}
    score = ng.score(almost, findings={"shipped_configs_pass"})
    assert "language" in score["missed_required"] and lang.id in score["false_met"]
    assert not score["passes"]
    # Unjudged is never met: a check that stopped at its budget still "catches" nothing falsely.
    assert ng.score({})["false_met"] == []


def test_the_helpdesk_positive_control_is_known_good(tmp_path):
    control = load_helpdesk_control()
    assert control.verdict == "met" and control.max_human_items <= 1
    root = control.build(tmp_path / "helpdesk")
    spec = control.spec()
    items = ac.build_items(ac.gather_sources(spec))
    assert [i["text"] for i in items] == list(control.task["acceptance_criteria"])
    assert (root / "src" / "app.js").is_file() and (root / "bench_acceptance").is_dir()
    if not shutil.which("node"):
        pytest.skip("node is not installed: the known-good tree is built, not run")
    env = {**os.environ, "NODE_NO_WARNINGS": "1"}
    for argv in (["node", "--test"],
                 ["node", "--test", "bench_acceptance/stats.accept.mjs"]):
        ran = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120, env=env)
        assert ran.returncode == 0, f"{' '.join(argv)}: {ran.stdout[-800:]}{ran.stderr[-800:]}"
