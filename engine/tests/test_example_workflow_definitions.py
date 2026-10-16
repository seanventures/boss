"""Every example under examples/ is a workflow somebody can install, and the ones in the
use-case library publish a definition BOSS accepts — checked from source, without a BOSS.

THREE THINGS THAT WENT WRONG BEFORE THIS EXISTED, each now a test:

* an example's definition was built inside main() after four API calls, so the only way to
  find a spec BOSS would refuse was to install it on a live BOSS. The library examples expose
  `build_spec(keys, args)`, and this validates every one of them with the engine's own
  validator — the same code Save runs.
* an example granted an agent a skill at risk tier 2 and left the step's approval on auto; the
  step was refused at publish on a live install. Every custom step bound to a tier-2 skill is
  `manual` here, read from the skill specs BOSS ships.
* "nothing is sent until you approve" was a sentence in a README and nothing else. Every leaf
  of every library workflow is a manual stop, and every README says so in the same words.

The dry-run test drives `setup_all.py --dry-run` against a stub of the four reads a dry run
makes. A dry run that POSTs anything fails the stub, and so the test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from boss_engine.services.workflows import (
    CONTROL_KINDS,
    WorkflowValidationError,
    validate_workflow_spec,
)
from boss_engine.services.workflow_custom_steps import MANUAL_APPROVAL_RISK_FLOOR

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"

#: The use-case library: every example that exposes `build_spec`. The five original examples
#: build their spec inline and are covered by the structural and dry-run tests only.
LIBRARY = [
    "release-notes",
    "dependency-upgrade-pr",
    "flaky-test-triage",
    "incident-postmortem",
    "security-finding-triage",
    "competitive-teardown",
    "prd-from-interviews",
    "support-ticket-triage",
    "pipeline-digest",
    "oncall-handoff",
]


def _example_dirs() -> list[str]:
    return sorted(p.name for p in EXAMPLES.iterdir()
                  if p.is_dir() and (p / "setup.py").is_file())


def _listed_in_setup_all() -> list[str]:
    import ast

    tree = ast.parse((EXAMPLES / "setup_all.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXAMPLES" for t in node.targets):
            return [el.elts[0].value for el in node.value.elts]
    raise AssertionError("examples/setup_all.py no longer lists EXAMPLES")


def _load(name: str):
    """Import examples/<name>/setup.py as a module, with examples/ on the path for _common."""
    if str(EXAMPLES) not in sys.path:
        sys.path.insert(0, str(EXAMPLES))
    path = EXAMPLES / name / "setup.py"
    spec = importlib.util.spec_from_file_location(f"example_{name.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _skill_tiers() -> dict[str, int]:
    tiers: dict[str, int] = {}
    for path in sorted((ROOT / "packages" / "specpack" / "skills").glob("*.yaml")):
        for skill in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("skills") or []:
            tiers[str(skill["id"])] = int(skill.get("risk_tier", 0))
    return tiers


def _specs(name: str):
    """(module, keys, spec) for both staffings: agents named, and --no-agents."""
    module = _load(name)
    keys = {s["label"]: f"cs_{i}" for i, s in enumerate(module.STEPS)}
    for no_agents in (False, True):
        args = SimpleNamespace(no_agents=no_agents, dry_run=False)
        yield module, keys, module.build_spec(keys, args), no_agents


# --- structure ------------------------------------------------------------------------------ #

def test_every_example_directory_is_listed_once_and_has_a_readme_and_a_name():
    dirs = _example_dirs()
    listed = _listed_in_setup_all()
    assert sorted(listed) == dirs, "examples/ and setup_all.py disagree about what exists"
    assert len(listed) == len(set(listed))
    names = {}
    for name in dirs:
        assert (EXAMPLES / name / "README.md").is_file(), f"{name} has no README.md"
        module = _load(name)
        assert isinstance(getattr(module, "NAME", None), str) and module.NAME.strip(), (
            f"{name}/setup.py has no NAME; the harness and the Library key come from it")
        slug = module.NAME.lower().replace(" ", "_")
        assert slug not in names, f"{name} and {names[slug]} would publish the same workflow"
        names[slug] = name
        assert callable(getattr(module, "main", None))


def test_the_library_examples_are_the_ones_with_a_build_spec():
    with_spec = sorted(n for n in _example_dirs() if hasattr(_load(n), "build_spec"))
    assert with_spec == sorted(LIBRARY)


# --- the definitions ------------------------------------------------------------------------ #

@pytest.mark.parametrize("name", LIBRARY)
def test_the_definition_passes_the_validator_save_runs(name):
    for _module, _keys, spec, no_agents in _specs(name):
        try:
            normalized = validate_workflow_spec(spec)
        except WorkflowValidationError as error:
            pytest.fail(f"{name} (no_agents={no_agents}): {error}\n"
                        + "\n".join(json.dumps(p) for p in error.problems))
        assert normalized["entry_step"] in {s["id"] for s in normalized["steps"]}
        assert normalized["input_schema"].get("required"), f"{name} asks for no input"


@pytest.mark.parametrize("name", LIBRARY)
def test_every_custom_step_binds_a_shipped_skill_and_declares_every_wired_input(name):
    tiers = _skill_tiers()
    module = _load(name)
    declared = {s["label"]: {i["name"] for i in s["inputs"]} for s in module.STEPS}
    for s in module.STEPS:
        assert s["skill_id"] in tiers, f"{name}: {s['skill_id']} is not a skill BOSS ships"
    for _module, keys, spec, _ in _specs(name):
        by_key = {v: k for k, v in keys.items()}
        for step in spec["steps"]:
            custom = (step.get("binding") or {}).get("custom_step")
            if not custom:
                continue
            wired = set((step["binding"].get("inputs") or {}).keys())
            missing = wired - declared[by_key[custom]]
            assert not missing, (
                f"{name}/{step['id']} wires {sorted(missing)} which '{by_key[custom]}' does "
                f"not declare; the runtime would treat them as required and unknown")


@pytest.mark.parametrize("name", LIBRARY)
def test_every_step_at_or_above_the_risk_floor_is_a_manual_stop(name):
    tiers = _skill_tiers()
    module = _load(name)
    skill_of = {s["label"]: s["skill_id"] for s in module.STEPS}
    for _module, keys, spec, _ in _specs(name):
        by_key = {v: k for k, v in keys.items()}
        for step in spec["steps"]:
            custom = (step.get("binding") or {}).get("custom_step")
            if not custom:
                continue
            if tiers[skill_of[by_key[custom]]] >= MANUAL_APPROVAL_RISK_FLOOR:
                assert (step.get("approval") or {}).get("mode") == "manual", (
                    f"{name}/{step['id']} runs {skill_of[by_key[custom]]} at tier "
                    f"{tiers[skill_of[by_key[custom]]]} and is not manual")


@pytest.mark.parametrize("name", LIBRARY)
def test_every_leaf_is_a_person_so_nothing_leaves_boss_unapproved(name):
    """Every path through every library workflow ends at a manual stop: the last thing a run
    does is wait for a person, never send, file or deploy on its own."""
    for _module, _keys, spec, _ in _specs(name):
        steps = {s["id"]: s for s in spec["steps"]}
        bodies = {s["body"] for s in spec["steps"] if s["kind"] in ("for_each", "bounded_loop")}
        for step in spec["steps"]:
            if step["kind"] in CONTROL_KINDS or step["id"] in bodies:
                continue
            if step.get("next"):
                continue
            assert (step.get("approval") or {}).get("mode") == "manual", (
                f"{name}/{step['id']} is a leaf and does not stop for a person")
        for step in spec["steps"]:
            if step["kind"] == "condition":
                for branch in ("on_true", "on_false"):
                    assert step[branch] in steps


@pytest.mark.parametrize("name", LIBRARY)
def test_the_readme_says_nothing_leaves_until_a_person_approves(name):
    readme = (EXAMPLES / name / "README.md").read_text(encoding="utf-8")
    # Prose wraps; the sentence does not.
    assert "until a person approves" in " ".join(readme.split()), (
        f"{name}/README.md must say so, in those words")
    assert "Build it by hand" in readme
    assert "## What you need" in readme


def test_no_example_carries_a_credential():
    # Private infrastructure names are covered for the whole tree by
    # test_the_public_tree_names_no_private_infrastructure (hash-only denylist).
    for name in _example_dirs():
        for file in ("setup.py", "README.md"):
            text = (EXAMPLES / name / file).read_text(encoding="utf-8")
            assert "sk-" not in text and "whsec_" not in text.replace("whsec_…", ""), name


# --- the dry run ---------------------------------------------------------------------------- #

class _Stub(BaseHTTPRequestHandler):
    """The four reads a --dry-run makes. Anything else is a failure of the dry run."""

    posted: list[str] = []

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if self.headers.get("Authorization") != "Bearer stub-key":
            return self._send(401, {"detail": "unauthorised"})
        if self.path.startswith("/v1/settings"):
            return self._send(200, {"project_id": "boss-dogfood"})
        if "/custom-steps" in self.path:
            return self._send(200, {"steps": []})
        if "/schedules" in self.path:
            return self._send(200, {"schedules": []})
        if self.path.startswith("/v1/roles/"):
            return self._send(200, {"role_key": "present"})
        return self._send(404, {"detail": self.path})

    def do_POST(self) -> None:  # noqa: N802
        _Stub.posted.append(self.path)
        return self._send(500, {"detail": f"a dry run must not write: {self.path}"})

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def stub_api():
    _Stub.posted = []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def test_setup_all_dry_run_checks_every_example_and_writes_nothing(stub_api):
    env = {**os.environ, "BOSS_API_KEY": "stub-key"}
    done = subprocess.run(
        [sys.executable, str(EXAMPLES / "setup_all.py"), "--dry-run", "--api", stub_api],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    expected = len(_listed_in_setup_all())
    assert f"{expected} checked" in done.stdout, done.stdout
    assert "SKIPPED" not in done.stdout, done.stdout
    for name in _listed_in_setup_all():
        assert f"── {name} —" in done.stdout, f"{name} was not checked"
    assert _Stub.posted == [], f"a dry run wrote: {_Stub.posted}"
    # Every library example says what it would create, by its NAME.
    for name in LIBRARY:
        assert f"would create {_load(name).NAME!r}" in done.stdout, name
