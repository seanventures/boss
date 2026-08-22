"""`npm start` runs an app; it does not compile one.

Shorty — a complete, correct, 6,988-line TypeScript URL shortener that had just passed its own
test suite and opened PR #106 — could not be previewed. Its start script is `node dist/server.js`
and nothing ever ran `tsc`, so the process died instantly on MODULE_NOT_FOUND while the operator
watched "Starting preview…" until it timed out.

The requirement was already understood: the two Vite branches build first, and so does the
generated Dockerfile. One branch out of four went straight from install to start. That is the
shape — the same rule expressed in four places and satisfied in three — so this test asserts it
of EVERY command the detector can return, not of the one that was wrong.
"""

import inspect
import re

from boss_engine.services import preview_service

#: Whatever eventually runs the app must have been preceded by a build, unless it is a dev
#: server (which compiles on the fly).
STARTS_THE_APP = re.compile(r"npm (?:start|run start)\b")
#: `{build}` is an f-string hole holding a build command; test_the_build_variable_only_ever_
#: holds_a_build below is what makes trusting it safe.
BUILDS_FIRST = re.compile(r"npm run build --if-present|vite build|npm run build\b|\{build\}")


#: Scanned WHOLE-MODULE on purpose. The rule was already satisfied in three places and missed
#: in a fourth, and pinning the check to one function is how the fifth gets missed too. This
#: reads the file, so a branch added to any function — or to a function that does not exist
#: yet — is covered.
MODULE_SOURCE = inspect.getsource(preview_service)


def _return_blocks(src: str) -> list[str]:
    """Every `return [...]` command literal in the module."""
    return [b.split("]")[0] for b in src.split("return [")[1:]]


def test_there_are_command_blocks_to_check():
    """If this fails the extraction broke, not the behaviour — fix the test, do not delete it."""
    blocks = [b for b in _return_blocks(MODULE_SOURCE) if "npm" in b]
    assert blocks, "no npm commands found anywhere; the guards below would pass vacuously"


def test_no_branch_anywhere_starts_an_app_without_building_it():
    offenders = []
    for body in _return_blocks(MODULE_SOURCE):
        if STARTS_THE_APP.search(body) and not BUILDS_FIRST.search(body):
            offenders.append(" ".join(body.split())[:110])
    assert not offenders, (
        "these run the app without compiling it, so a TypeScript service dies on "
        "MODULE_NOT_FOUND:\n  " + "\n  ".join(offenders))


def test_a_dev_server_is_not_forced_to_build():
    """A dev server compiles on the fly; building first only makes the preview slower."""
    src = MODULE_SOURCE
    assert 'build = "" if script == "dev"' in src


def test_the_build_is_optional_so_apps_without_one_are_unaffected():
    src = MODULE_SOURCE
    assert "--if-present" in src, (
        "an app with no build script must not fail because we asked for one")


def test_install_still_happens_before_the_build():
    """Building before dependencies exist fails just as hard, in a more confusing way."""
    for body in _return_blocks(MODULE_SOURCE):
        if "npm run build" in body and "npm install" in body:
            assert body.index("npm install") < body.index("npm run build"), body[:120]


def test_the_dockerfile_path_agrees_with_the_host_path():
    """Both paths must make the SAME choice; disagreeing is how the wrong app got previewed."""
    src = inspect.getsource(preview_service._generate_dockerfile)
    assert "npm run build --if-present" in src


def test_the_build_variable_only_ever_holds_a_build_command():
    """The guard above accepts `{build}` as satisfying "builds first". That is only sound if
    every assignment to it is genuinely a build — otherwise the hole becomes the loophole."""
    # Only STRING assignments matter: those are the ones that can reach an f-string hole.
    # `build = subprocess.run(["docker", "build", ...])` is a docker image build in another
    # scope entirely and is never interpolated into a shell command.
    assignments = [v.strip() for v in re.findall(r"^\s*build = (.+)$", MODULE_SOURCE, re.M)
                   if v.strip().startswith(('"', "'", 'f"', "f'"))]
    assert assignments, "the {build} hole is trusted; some string must assign it"
    for value in assignments:
        assert BUILDS_FIRST.search(value) or value.startswith('"" if'), (
            f"`build` is interpolated into a run command but assigned: {value[:90]}")
