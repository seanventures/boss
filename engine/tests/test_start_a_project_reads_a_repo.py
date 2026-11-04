"""Start a project (BOSSAPP-810): what BOSS can tell about a repository from its paths alone, how
far its code is mapped, and the sample's fixed choices.

Import › "BOSS reads it" shows Language, Build & test and Runs as before anything is built. Each
comes from file paths (the code graph's, or GitHub's tree) and is None when the paths do not say:
a fact the console prints must never be a guess.
"""
from __future__ import annotations

from boss_engine.services import start_project as sp
from boss_engine.templates import service


SPRING_GRADLE = [
    "build.gradle", "settings.gradle", "gradlew",
    "src/main/java/org/acme/orders/OrdersApplication.java",
    "src/main/resources/application.yml",
    "src/test/java/org/acme/orders/OrdersControllerTest.java",
]


def test_a_spring_gradle_repo_reads_as_a_java_web_api_with_tests():
    facts = sp.detect(SPRING_GRADLE, language="Java")
    assert facts == {"language": "Java", "framework": "Spring", "build": "Gradle", "tests": True,
                     "runs_as": "Web API"}


def test_a_node_app_and_its_lockfile_say_which_tool_builds_it():
    facts = sp.detect(["package.json", "pnpm-lock.yaml", "vite.config.ts", "src/main.tsx",
                       "src/App.test.tsx"])
    assert facts["language"] == "TypeScript"          # counted from the files, no registry answer
    assert (facts["build"], facts["framework"], facts["runs_as"], facts["tests"]) == \
        ("pnpm", "Vite", "Web app", True)


def test_paths_that_say_nothing_give_nothing_never_a_guess():
    facts = sp.detect(["README.md", "docs/intro.md"])
    assert facts == {"language": None, "framework": None, "build": None, "tests": False,
                     "runs_as": None}
    assert sp.detect([]) ["tests"] is None


def test_a_build_file_deep_in_the_tree_is_not_the_repos_build():
    # A vendored example's pom.xml three folders down does not make the repo a Maven build.
    facts = sp.detect(["examples/java/demo/pom.xml", "app.py", "requirements.txt"])
    assert facts["build"] == "pip" and facts["language"] == "Python"


def test_mapping_progress_from_the_repository_index():
    assert sp.mapping(None) == {"state": "not_started", "percent": 0, "done": False}
    assert sp.mapping({"status": "pending"})["state"] == "queued"
    running = sp.mapping({"status": "running", "files": 200, "parsed": 128})
    assert running == {"state": "running", "percent": 64, "done": False}
    assert sp.mapping({"status": "indexed", "graph": {"nodes": 900}}) == \
        {"state": "done", "percent": 100, "done": True}
    failed = sp.mapping({"status": "failed", "error": "clone refused"})
    assert failed["state"] == "failed" and failed["error"] == "clone refused"


def test_a_project_name_from_a_repository():
    assert sp.suggested_name("acme/order-service") == "Order service"
    assert sp.suggested_name("acme/fineract") == "Fineract"
    assert sp.suggested_name("") == "My project"


def test_the_sample_is_the_hello_world_built_in_on_the_smallest_planned_workflow():
    built = {report.manifest.id: report for _, report, _ in service.builtins()}
    hello = built[sp.SAMPLE_TEMPLATE]
    assert hello.ok, hello.reasons
    assert hello.manifest.name == sp.SAMPLE_NAME == "Hello World"
    assert sp.SAMPLE_STACK in hello.manifest.stacks
    assert hello.manifest.tests[sp.SAMPLE_STACK].min_tests == 1
    assert hello.manifest.item_kind == "idea"
    # Planned, built, previewed: Minor Feature has a panel (the plan), a build and a preview.
    from boss_engine.services.workflow_catalogue import CATALOGUE

    steps = [s["id"] for s in next(e for e in CATALOGUE if e["key"] == sp.SAMPLE_WORKFLOW)["steps"]]
    assert {"meeting", "build", "preview"} <= set(steps)
    assert sp.FEATURE_WORKFLOW == "minor_feature" and sp.FEATURE_CLASS == "feature.minor"


def test_the_project_page_names_five_tiles_and_four_examples():
    assert [s["label"] for s in sp.NEXT_STEPS] == ["Plan", "Build", "Review & test", "Pull request",
                                                   "Preview"]
    assert [s["who"] for s in sp.NEXT_STEPS if s["you"]] == ["You approve", "You merge"]
    assert sp.EXAMPLES == ("Add an API endpoint", "Fix a bug", "Add tests", "Upgrade a library")
