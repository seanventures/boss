"""APP-3 (#93) — web-appropriate verification target: project-kind detection and the command set
it resolves (web build/typecheck/lint/e2e vs the Python unittest default). Additive."""
from apps.api.app.services import verification_target as vt


def test_detect_kind_by_project_kind():
    assert vt.detect_kind({"project_kind": "web"}) == "web"
    assert vt.detect_kind({"project_kind": "spa"}) == "web"
    assert vt.detect_kind({"project_kind": "nextjs"}) == "web"
    assert vt.detect_kind({"project_kind": "python"}) == "python"
    assert vt.detect_kind({"project_kind": "backend"}) == "python"


def test_detect_kind_infers_web_from_ui_task():
    assert vt.detect_kind({"instructions": "Build a responsive landing page in React"}, "Site") == "web"
    assert vt.detect_kind({"instructions": "Add a token-bucket rate limiter"}, "Limiter") == "python"


def test_web_commands_cover_build_typecheck_lint_e2e():
    cmds = vt.default_test_commands({"project_kind": "web"})
    joined = " ".join(cmds)
    for step in ("npm run build", "npm run typecheck", "npm run lint", "npm run test:e2e"):
        assert step in joined
    # && so it short-circuits on the earliest failure (compile before e2e)
    assert "&&" in joined


def test_python_stays_the_default():
    assert vt.default_test_commands({"instructions": "add a function"}) == [vt.PYTHON_TEST_CMD]
    assert "unittest" in vt.PYTHON_TEST_CMD


def test_web_target_matches_scaffold_scripts():
    # APP-3's web steps must be the scripts APP-1's scaffold actually defines
    from apps.api.app.services import scaffold_service as sc
    import json
    scripts = json.loads(sc.scaffold_project("x")["files"]["package.json"])["scripts"]
    for step in ("build", "typecheck", "lint", "test:e2e"):
        assert step in scripts and f"npm run {step}" in vt.WEB_TEST_CMD


def test_verification_profile_shape():
    web = vt.verification_profile({"project_kind": "web"})
    assert web["kind"] == "web" and len(web["steps"]) == 4
    py = vt.verification_profile({"instructions": "backend task"})
    assert py["kind"] == "python" and py["command"] == vt.PYTHON_TEST_CMD


def test_pipeline_resolves_web_target_for_web_spec():
    # the governed entry uses default_test_commands as the fallback (line ~470)
    from apps.api.app.services import governed_code_task_service as g
    assert hasattr(g, "verification_target")
    # explicit test_commands still win over the resolver
    spec = {"project_kind": "web", "test_commands": ["custom cmd"]}
    resolved = (spec.get("test_commands") or vt.default_test_commands(spec))[0]
    assert resolved == "custom cmd"
    resolved_web = ([] or vt.default_test_commands({"project_kind": "web"}))[0]
    assert "npm run build" in resolved_web
