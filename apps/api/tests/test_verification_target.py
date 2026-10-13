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
    # NO SHELL OPERATORS. These are a LIST now, because the sandbox refuses `&&` outright —
    # passing the old one-line chain to it failed the whole verification step. The
    # short-circuit the `&&` used to provide is a property of the RUNNER, asserted below.
    assert "&&" not in joined
    assert cmds[0].startswith("npm install"), "install has to come first"


def test_verification_stops_at_the_first_failing_command():
    """Splitting the `&&` chain into a list silently dropped short-circuiting: every call site
    ran the whole list regardless, so a web build whose compile failed still paid for a
    typecheck, a lint and a full Playwright suite — and buried the real error underneath."""
    class _Result:
        def __init__(self, ok): self.ok = ok

    class _Sandbox:
        def __init__(self, fail_at): self.fail_at, self.ran = fail_at, []
        def run(self, cmd):
            self.ran.append(cmd)
            return _Result(len(self.ran) - 1 != self.fail_at)

    cmds = vt.default_test_commands({"project_kind": "web"})
    box = _Sandbox(fail_at=1)                       # the build fails
    results = vt.run_until_failure(box, cmds)
    assert len(box.ran) == 2, box.ran               # install, build, then stop
    assert "npm run test:e2e" not in box.ran
    assert not results[-1].ok

    everything = _Sandbox(fail_at=-1)               # nothing fails
    assert len(vt.run_until_failure(everything, cmds)) == len(cmds)


def test_python_stays_the_default():
    assert vt.default_test_commands({"instructions": "add a function"}) == [vt.PYTHON_TEST_CMD]
    assert "unittest" in vt.PYTHON_TEST_CMD


def test_web_target_matches_scaffold_scripts():
    # APP-3's web steps must be the scripts APP-1's scaffold actually defines
    from apps.api.app.services import scaffold_service as sc
    import json
    scripts = json.loads(sc.scaffold_project("x")["files"]["package.json"])["scripts"]
    for step in ("build", "typecheck", "lint", "test:e2e"):
        # WEB_TEST_CMD is the display string; WEB_TEST_COMMANDS is what actually runs.
        assert step in scripts and f"npm run {step}" in vt.WEB_TEST_CMD
        assert f"npm run {step}" in vt.WEB_TEST_COMMANDS


def test_verification_profile_shape():
    web = vt.verification_profile({"project_kind": "web"})
    assert web["kind"] == "web" and len(web["steps"]) == 5  # build, typecheck, lint, test, e2e
    py = vt.verification_profile({"instructions": "backend task"})
    assert py["kind"] == "python" and py["command"] == vt.PYTHON_TEST_CMD


def test_pipeline_resolves_web_target_for_web_spec():
    # the governed entry uses resolve_authoritative_test_commands — the whole
    # list, not [0]. [0] is npm install after the && split.
    from apps.api.app.services import governed_code_task_service as g
    assert hasattr(g, "verification_target")
    spec = {"project_kind": "web", "test_commands": ["custom cmd"]}
    resolved = vt.resolve_authoritative_test_commands(spec)
    assert resolved == ["custom cmd"]
    resolved_web = vt.resolve_authoritative_test_commands({"project_kind": "web"})
    assert "npm run build" in resolved_web
    assert resolved_web[0].startswith("npm install")
    assert len(resolved_web) > 1
