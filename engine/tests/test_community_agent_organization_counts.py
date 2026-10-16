"""Community agent organization is counted from source, not from a release note.

The public claim is 34 bundled definitions, 14 workflow templates, and fifteen
opt-in examples. These tests read the files and the installer that actually
ships them. A stale sentence in a doc cannot keep the count green.

The catalogue grew from 12: Security Review (`security_review`) and Incremental
Build (`incremental_build`, ded31293) are deliberate entries. The examples grew
from five to fifteen with the use-case library (release notes, dependency
upgrade PR, flaky-test triage, incident postmortem, security-finding triage,
competitive teardown, PRD from interviews, support ticket triage, pipeline
digest, on-call handoff). The release-notes draft and README.community.md this
test once read do not exist in this tree; the fifteen-examples claim lives in
README.md, and no shipped doc may state a workflow-template count that differs
from the catalogue.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from boss_engine.services.agent_studio import AgentStudioService
from boss_engine.services.workflow_catalogue import CATALOGUE, seed as seed_catalogue


ROOT = Path(__file__).resolve().parents[2]


class _Specpack:
    def __init__(self, root: Path) -> None:
        self.specpack_root = root


def _definition_paths() -> list[Path]:
    return sorted((ROOT / "packages" / "specpack" / "agents").glob("*.yaml"))


def _example_names() -> list[str]:
    tree = ast.parse((ROOT / "examples" / "setup_all.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "EXAMPLES" for target in node.targets):
            continue
        return [elt.elts[0].value for elt in node.value.elts]
    raise AssertionError("examples/setup_all.py no longer lists EXAMPLES")


def test_bundled_definitions_templates_and_examples_match_source():
    definitions = _definition_paths()
    templates = AgentStudioService(_Specpack(ROOT / "packages" / "specpack")).list_templates()
    catalogue_keys = [entry["key"] for entry in CATALOGUE]
    examples = _example_names()
    example_dirs = sorted(
        path.name for path in (ROOT / "examples").iterdir()
        if path.is_dir() and (path / "setup.py").is_file()
    )

    assert len(definitions) == 34
    assert [path.stem for path in definitions] == sorted(item["id"] for item in templates)
    assert len(templates) == 34
    assert len(catalogue_keys) == 14
    assert len(set(catalogue_keys)) == 14
    assert example_dirs == sorted(examples)
    assert len(examples) == 15

    readme = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    features = (ROOT / "docs/community/FEATURES.md").read_text(encoding="utf-8")
    community = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "The 34 YAML files in `packages/specpack/agents/`" in readme
    assert "First-run seed installs 34 agents" in features
    assert "fifteen example workflows" in community
    assert "None is installed or runs on a" in readme

    # No shipped doc states a workflow-template count the catalogue does not have.
    words = {"twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16}
    claim = re.compile(r"\b(\d+|" + "|".join(words) + r")\s+(?:built-in\s+|catalogue\s+|bundled\s+)?"
                       r"workflow\s+templates\b", re.IGNORECASE)
    stale = []
    for doc in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")), ROOT / "examples" / "README.md"]:
        for m in claim.finditer(doc.read_text(encoding="utf-8")):
            n = m.group(1).lower()
            if (int(n) if n.isdigit() else words[n]) != len(catalogue_keys):
                stale.append(f"{doc.relative_to(ROOT)}: {m.group(0)}")
    assert not stale, stale


def test_examples_are_opt_in_and_installing_a_definition_does_not_start_work():
    bootstrap = inspect.getsource(
        __import__("boss_engine.bootstrap", fromlist=["bootstrap"])
    )
    assert "examples" not in bootstrap
    assert "setup_all" not in bootstrap

    install_sources = "\n".join(
        inspect.getsource(fn)
        for fn in (
            AgentStudioService.list_templates,
            AgentStudioService.seed_template,
            AgentStudioService.seed_catalog,
            AgentStudioService._seed_agent,
            AgentStudioService.create_agent,
            AgentStudioService.create_agent_version,
            AgentStudioService.publish_agent_version,
            seed_catalogue,
        )
    )
    assert "start_agent_run" not in install_sources
    assert "enqueue_job" not in install_sources
    assert "start_run" not in install_sources

    router = (ROOT / "apps/api/app/routers/agent_studio_seed.py").read_text(encoding="utf-8")
    install = router.split("def install_agent_template", 1)[1].split("def _project_scope", 1)[0]
    assert "start_agent_run" not in install
    assert "launched_live_agent" in install
    assert '"jobs_enqueued": 0' in install
    assert '"model_calls": 0' in install
