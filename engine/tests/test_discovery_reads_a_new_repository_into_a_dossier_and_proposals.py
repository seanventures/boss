"""Discovery reads a repository with no instructions into a dossier and proposals (BOSSAPP-909).

Given a checkout (the one the code-graph index keeps), BOSS writes the project's dossier — stack,
structure, test and build commands, owners, docs and runbooks, TODO clusters, churn × size
hotspots, the locked dependencies and their advisories, what BOSS has open and what the graph
knows — with no model, and OSV only when it answers (offline is "skipped", never an error). What
is worth doing becomes signals the Chief of Staff triages, so a new project opens on ranked
proposals: "no CI" and "a hot file with no tests" are Needs-you cards, a TODO cluster is digest
only. Rebuilding at the same commit raises nothing new.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from boss_engine.services.ambient import board, chief_of_staff as cos, dossier, signals

from ._ambient_support import eng, project  # noqa: F401

REPO = "acme/shop"


def _git(root: Path, *args: str, who: str = "Ada Lovelace") -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": "ada@example.com",
           "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": "ada@example.com"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "src/orders").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "package.json").write_text(json.dumps(
        {"name": "shop", "scripts": {"test": "jest", "build": "tsc", "lint": "eslint ."}}))
    (root / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "shop"}, "node_modules/lodash": {"version": "4.17.20"},
        "node_modules/express": {"version": "4.19.2"}}}))
    (root / "README.md").write_text("# Shop\n")
    (root / "docs/runbook-oncall.md").write_text("# When orders fail\n")
    (root / "CODEOWNERS").write_text("# owners\n/src/orders/ @acme/payments\n* @acme/core\n")
    (root / "src/util.js").write_text("export const add = (a, b) => a + b;\n")
    (root / "tests/util.test.js").write_text("test('add', () => {});\n")
    _git(root, "init", "-q", "-b", "main")
    service = root / "src/orders/service.js"
    body = []
    for n in range(6):   # a file that keeps changing: the hotspot
        body += [f"// TODO: handle refund case {n}", f"export function step{n}() {{ return {n}; }}"]
        body += [f"const filler{n}_{i} = {i};" for i in range(40)]
        service.write_text("\n".join(body) + "\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"orders change {n}",
             who="Ada Lovelace" if n % 2 else "Grace Hopper")
    return root


def _advisories(packages):
    hit = [p for p in packages if p["name"] == "lodash"]
    return {"status": "ok", "advisories": [{**p, "ids": ["GHSA-35jh-r3h4-6jhm"]} for p in hit]}


def test_the_dossier_says_what_the_repository_is_how_to_run_it_and_where_the_risk_is(checkout):
    facts = dossier.scan_tree(checkout)
    assert facts["stack"]["language"] == "JavaScript" and facts["stack"]["build"] == "npm"
    assert facts["commands"] == {"install": ["npm ci"], "test": ["npm test"],
                                 "lint": ["npm run lint"], "build": ["npm run build"], "run": []}
    assert facts["ci"] == [] and facts["tests"]["files"] == 1
    assert facts["owners"]["codeowners"] == "CODEOWNERS"
    assert {"pattern": "/src/orders/", "owners": ["@acme/payments"]} in facts["owners"]["rules"]
    assert {p["name"] for p in facts["owners"]["top_committers"]} == {"Ada Lovelace",
                                                                       "Grace Hopper"}
    assert "@" not in json.dumps(facts["owners"]["top_committers"]), "names, never emails"
    assert facts["docs"]["readme"] == "README.md"
    assert facts["docs"]["runbooks"] == ["docs/runbook-oncall.md"]
    assert facts["todos"]["clusters"][0] == {"dir": "src/orders", "count": 6,
                                             "files": ["src/orders/service.js"]}
    top = facts["hotspots"][0]
    assert top["file"] == "src/orders/service.js" and top["has_tests"] is False
    assert facts["dependencies"]["count"] == 2
    assert facts["advisories"]["status"] == "skipped", "no OSV unless asked"


def test_building_stores_one_dossier_and_seeds_the_work_worth_proposing(eng, project, checkout):
    with eng.begin() as c:
        built = dossier.build(c, project_id=project, repo=REPO, root=checkout, osv=_advisories)
        assert built["read_from"] == "checkout" and built["version"] == 1 and built["head_sha"]
        assert built["highlights"][:3] == ["JavaScript", "No CI", "1 test files"]
        assert "1 vulnerable packages" in built["highlights"]
        kinds = sorted(s["kind"] for s in built["seeded_signals"])
        assert kinds == ["osv.vulnerable_dependency", "scan.no_ci", "scan.todo_cluster",
                         "scan.untested_hotspot"]
        assert built["facts"]["boss"]["live_items"] == 0
        sig = [s for s in signals.list_signals(c, project_id=project)
               if s["kind"] == "scan.untested_hotspot"][0]
        assert {"kind": "file", "ref": "src/orders/service.js"} == {
            k: v for k, v in sig["evidence"][1].items() if k in ("kind", "ref")}
        # Again at the same commit: the same dossier, a new version, no new signal.
        again = dossier.build(c, project_id=project, repo=REPO, root=checkout, osv=_advisories)
        assert again["id"] == built["id"] and again["version"] == 2
        assert {s["outcome"] for s in again["seeded_signals"]} == {"duplicate"}
        assert len(dossier.get(c, project)) == 1


def test_the_chief_of_staff_turns_the_dossier_into_ranked_proposals(eng, project, checkout):
    with eng.begin() as c:
        dossier.build(c, project_id=project, repo=REPO, root=checkout, osv=_advisories)
    with eng.begin() as c:
        report = cos.triage_project(c, None, project, mode="propose", max_model_calls=0)
    assert report["counts"] == {"proposed": 3, "observed": 1}
    with eng.begin() as c:
        by_class = {i["task_class"]: i for i in board.list_items(c, project)}
        assert set(by_class) == {"ci_setup", "missing_tests", "todo_triage", "security_patch"}
        assert by_class["todo_triage"]["lifecycle"] == "proposed", "A0: digest, no card"
        for klass in ("ci_setup", "missing_tests", "security_patch"):
            assert by_class[klass]["lifecycle"] == "needs_you"
        # The advisory first: the highest priority is worked first.
        assert report["items"][0]["task_class"] == "security_patch"


def test_with_no_checkout_the_graph_file_list_still_gives_a_dossier(eng, project):
    with eng.begin() as c:
        built = dossier.build(c, project_id=project, repo=REPO, paths=[
            "pom.xml", "src/main/java/App.java", "src/main/resources/application.yml"],
            seed_signals=False)
        assert built["read_from"] == "graph"
        assert built["facts"]["stack"]["framework"] == "Spring"
        assert built["facts"]["commands"]["test"] == ["mvn test"]
        assert built["facts"]["advisories"]["reason"] == "no checkout"


def test_osv_offline_is_skipped_not_an_error(monkeypatch):
    monkeypatch.setattr(dossier, "OSV_URL", "http://127.0.0.1:9/v1/querybatch")
    got = dossier.osv_lookup([{"ecosystem": "npm", "name": "lodash", "version": "4.17.20"}],
                             timeout=1)
    assert got["status"] == "skipped" and got["advisories"] == []


def test_a_failed_dossier_never_fails_the_index(eng, project, checkout, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(dossier, "build", broken)
    settings = type("S", (), {"ambient_signals": 1, "ambient_dossier_osv": 0})()
    assert dossier.after_index(eng, settings, project_id=project, repo=REPO, root=checkout,
                               head="abc") is None


def test_after_an_index_the_dossier_is_built_from_the_same_checkout(eng, project, checkout):
    settings = type("S", (), {"ambient_signals": 1, "ambient_dossier_osv": 0})()
    built = dossier.after_index(eng, settings, project_id=project, repo=REPO, root=checkout,
                                head="")
    assert built["repo"] == REPO and built["facts"]["files"] >= 8


def test_the_readme_getting_started_fills_what_the_manifests_leave_unsaid(tmp_path):
    """A README's getting-started block (fenced shell lines under its heading) gives the install
    and run commands a repo's manifests do not; prose is never read as a command."""
    (tmp_path / "README.md").write_text(
        "# App\n\nRun `rm -rf /` never.\n\n## Getting started\n\n```bash\n$ pip install -r "
        "requirements.txt\npython -m app\n# a comment\n```\n\n## License\n\n```\nmake run\n```\n")
    (tmp_path / "requirements.txt").write_text("flask\n")
    got = dossier.commands(tmp_path, ["README.md", "requirements.txt"])
    assert got["install"] == ["pip install -r requirements.txt"]
    assert got["run"] == ["python -m app"]
    assert all("rm -rf" not in c for cs in got.values() for c in cs)
