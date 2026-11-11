"""The builder writes the Dockerfile, and no repository can teach it not to prune optionals.

The 2026-11-13 chain proof: the generated helpdesk app's Dockerfile ran `npm install
--omit=optional` on node:22-alpine, which strips @rollup/rollup-linux-x64-musl — the one native
binary vite needs on musl — and `vite build` died deterministically (npm/cli#4828).
`--omit=optional` appears nowhere in the BOSS repo; the builder model wrote it, and nothing in
any repository carries the convention that forbids it. That makes it a PLATFORM standard
(platform_standards), injected into every stage of an app build like the observability contract —
and when the class still ships, the deploy's install-failure summary names the cause so the fix
round repairs the Dockerfile instead of guessing.
"""
from __future__ import annotations

from boss_engine.services.governed_code_task_service import _impl_prompt
from boss_engine.services.platform_standards import (
    DEPENDENCY_INTEGRITY_STANDARD,
    STANDARDS,
    platform_standards,
)
from boss_engine.services.stack_deploy import CAUSE_APP, explain_failure


class TestTheStandardExists:
    def test_it_is_a_seeded_platform_standard(self):
        assert STANDARDS["dependency_integrity"] is DEPENDENCY_INTEGRITY_STANDARD

    def test_it_forbids_every_optional_prune_by_name(self):
        for flag in ("--omit=optional", "--no-optional", "--production"):
            assert flag in DEPENDENCY_INTEGRITY_STANDARD, flag

    def test_it_demands_the_lockfile_whole_and_says_why(self):
        """An agent that understands the constraint applies it to pnpm and bun too."""
        assert "exactly as the lockfile" in DEPENDENCY_INTEGRITY_STANDARD
        assert "@rollup/rollup-linux-x64-musl" in DEPENDENCY_INTEGRITY_STANDARD
        assert "npm/cli#4828" in DEPENDENCY_INTEGRITY_STANDARD

    def test_it_pins_the_base_image_family_to_the_deploys(self):
        assert "same base image family" in DEPENDENCY_INTEGRITY_STANDARD
        assert "musl" in DEPENDENCY_INTEGRITY_STANDARD


class TestItReachesTheBuild:
    def test_a_service_build_carries_it(self):
        assert "INSTALL EXACTLY WHAT THE LOCKFILE RESOLVES" in platform_standards("an http api")

    def test_the_build_stage_prompt_for_an_app_task_carries_it(self):
        """The implement stage is where the Dockerfile gets written, so the words must be in
        ITS prompt, not only in a constant somewhere."""
        prompt = _impl_prompt("Build HelpDesk AI", "a web app with sign-in and a dashboard",
                              ["tests/app.test.js"], "npm test", ui=True)
        assert "INSTALL EXACTLY WHAT THE LOCKFILE RESOLVES" in prompt
        assert "--omit=optional" in prompt
        assert "BUILD ON THE BASE IMAGE FAMILY THE DEPLOY RUNS" in prompt

    def test_a_library_is_not_lectured_about_images(self):
        assert "LOCKFILE" not in platform_standards("tidy a markdown parsing function")


ROLLUP_DEATH = """\
#12 [web 5/6] RUN npm run build
#12 1.902 > vite build
#12 2.481 Error: Cannot find module @rollup/rollup-linux-x64-musl. npm has a bug related to
optional dependencies (https://github.com/npm/cli/issues/4828). Please try `npm i` again after
removing both package-lock.json and node_modules directory.
#12 ERROR: process "/bin/sh -c npm run build" did not complete successfully: exit code: 1
"""

ESBUILD_DEATH = """\
> vite build
Error: The package "@esbuild/linux-x64" could not be found, and is needed by esbuild.
"""


class TestTheFailureSummaryNamesTheCause:
    def test_a_pruned_rollup_binary_is_named_with_the_fix(self):
        said = explain_failure("install: docker compose build", 1, ROLLUP_DEATH)
        assert "@rollup/rollup-linux-x64-musl" in said["headline"]
        assert "optional" in said["headline"]
        assert "npm ci" in said["action"] and "--omit=optional" in said["action"]
        assert "base image family" in said["action"]
        assert said["cause"] == CAUSE_APP

    def test_an_esbuild_binary_is_the_same_class(self):
        said = explain_failure("install: docker compose build", 1, ESBUILD_DEATH)
        assert "@esbuild/linux-x64" in said["headline"]
        assert "lockfile" in said["action"]

    def test_it_is_a_hint_never_an_edit(self):
        """The summary tells the fix round the cause; it changes nothing itself."""
        said = explain_failure("install: docker compose build", 1, ROLLUP_DEATH)
        assert "deploy again" in said["action"].lower()

    def test_an_ordinary_install_failure_keeps_its_own_words(self):
        said = explain_failure("install: docker compose build", 1,
                               "npm ERR! code ERESOLVE\nnpm ERR! Could not resolve dependency")
        assert "optional" not in said["headline"]
