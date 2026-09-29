"""Community agent organization is counted from source, not from a release note.

The public claim is 34 bundled definitions, 12 workflow templates, and five
opt-in examples. These tests read the files and the installer that actually
ships them. A stale sentence in a doc cannot keep the count green.
"""
from __future__ import annotations

import ast
import inspect
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
    assert len(catalogue_keys) == 12
    assert len(set(catalogue_keys)) == 12
    assert example_dirs == sorted(examples)
    assert len(examples) == 5

    notes = (ROOT / "docs/operations/COMMUNITY_GA_RELEASE_NOTES_DRAFT.md").read_text(encoding="utf-8")
    readme = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    features = (ROOT / "docs/community/FEATURES.md").read_text(encoding="utf-8")
    community = (ROOT / "README.community.md").read_text(encoding="utf-8")
    assert "Thirty-four bundled agent definitions, twelve workflow templates and five" in notes
    assert "The 34 YAML files in `packages/specpack/agents/`" in readme
    assert "First-run seed installs 34 agents" in features
    assert "five example workflows" in community
    assert "None is installed or runs on a" in readme


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
