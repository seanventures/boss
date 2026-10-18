"""LABELS AND TRAILERS (BOSSAPP-571 R10, spec §7.5, F11, F12).

  * The LOCAL PR carries every label, `signature:<class>` included; it gains `verified:local`
    when the parked step proves the fix.
  * UPSTREAM a fork cannot label the repository and a label per signature would make thousands:
    the PR body's machine block carries the facts, the labeler workflow turns them into
    `source:boss-agent`, `boss:self-fix`, `area:*`, `engine:*`, `verified:local` — never a
    `signature:` label — and every PR without the block is `source:user`.
  * Trailers name the agent, never the approving person or their email.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from boss_engine.services import improve_allowlist as allow
from boss_engine.services import self_fix as sf
from boss_engine.services import self_fix_upstream as up

from ._self_fix_upstream_support import insert_verified
from ._self_fix_support import clean_project
from .test_workflow_capability_runner import eng, project  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
SFX = {"id": "sfx_0123456789abcdef0123", "class_signature": "deploy:port_held",
       "location": "engine/boss_engine/services/stack_deploy.py::plan_stack_deploy",
       "area": "deploy", "status": "verified", "engine": "claude_code", "tier": 3,
       "gates": {"p0_tier": {"ok": True}}, "red_green": {"tests": ["engine/tests/test_x.py"]}}


def test_upstream_labels_never_carry_the_signature():
    labels = up.upstream_labels(SFX)
    assert labels == ["source:boss-agent", "boss:self-fix", "area:deploy", "verified:local",
                      "engine:claude"]
    assert not any(label.startswith("signature:") for label in labels)
    assert "verified:local" not in up.upstream_labels({**SFX, "status": "installed"})


def test_trailers_name_the_agent_never_the_person():
    trailers = up.upstream_trailers(SFX, "3fa9c0e1b2d4")
    assert trailers["BOSS-Approved"] == "yes" and trailers["BOSS-Self-Fix-Ref"] == "3fa9c0e1b2d4"
    assert trailers["BOSS-Signature"] == "deploy:port_held@stack_deploy.plan_stack_deploy"
    assert "BOSS-Run" not in trailers and "BOSS-Self-Fix" not in trailers, "no install ids"
    # (the signature's `cause@module.function` is not an address; every other value has no @)
    assert not any("@" in v for k, v in trailers.items() if k != "BOSS-Signature")
    assert set(trailers) == {"BOSS-Self-Fix-Ref", "BOSS-Signature", "BOSS-Agent", "BOSS-Engine",
                             "BOSS-Approved"}


def test_the_machine_block_round_trips_and_holds_only_allowed_fields():
    block = up.machine_block(SFX, "3fa9c0e1b2d4")
    parsed = up.parse_machine_block("text before\n" + block + "\nafter")
    assert set(parsed) == {"class_signature", "location", "area", "boss_version", "engine",
                           "engine_family", "verified_local", "red_green", "gates", "tier",
                           "upstream_ref"}
    assert parsed["engine_family"] == "claude" and parsed["verified_local"] is True


def test_the_labeler_workflow_reads_only_the_body_and_knows_the_same_words():
    flow = yaml.safe_load((ROOT / ".github/workflows/boss-agent-labels.yml").read_text())
    trigger = flow.get("on") or flow.get(True)
    assert "pull_request_target" in trigger
    steps = flow["jobs"]["label"]["steps"]
    assert not any("checkout" in str(s.get("uses", "")) for s in steps), \
        "never checks out the PR's code"
    script = steps[0]["with"]["script"]
    assert "boss-self-fix v1" in script and "source:user" in script
    assert "signature:" not in script
    areas = json.loads(re.search(r"const AREAS = (\[.*?\]);", script).group(1))
    engines = json.loads(re.search(r"const ENGINES = (\[.*?\]);", script).group(1))
    assert set(areas) == set(allow.SELF_FIX_AREAS) and set(engines) == set(
        allow.SELF_FIX_ENGINES)
    template = (ROOT / ".github/PULL_REQUEST_TEMPLATE/boss-self-fix.md").read_text()
    assert "<!-- boss-self-fix v1 {" in template and "## Evidence" in template


def test_verified_adds_the_local_label(eng, project):  # noqa: F811
    sfx_id = insert_verified(eng, project, status="verified")
    try:
        with eng.begin() as c:
            sf.add_label(c, sfx_id, sf.VERIFIED_LABEL)
            sf.add_label(c, sfx_id, sf.VERIFIED_LABEL)
            labels = sf.get(c, sfx_id)["pr"]["labels"]
        assert labels.count("verified:local") == 1 and "signature:deploy:port_held" in labels
    finally:
        clean_project(eng, project)
