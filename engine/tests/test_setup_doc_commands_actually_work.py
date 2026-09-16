"""Every command in the customer-facing setup doc, checked against the shipping code.

WHY. `MODEL_AND_SUBSCRIPTION_SETUP.md` is the page a customer follows to connect a model, so a
command that has rotted there is not a documentation defect — it is the product failing on
first contact, at the one moment the customer has no context to debug it.

The first run of this doc proved that. It said:

    -d '{"key":"CODEX_ACCESS_TOKEN","value":"<token>"}'

`<token>` inside double quotes is not expanded by any shell, so it was stored verbatim; the
secret store answered `{"stored": true}` because it cannot tell a credential from a
placeholder; and the deployment was left holding a seven-character string that would have
surfaced hours later as an authentication failure in a build. Every individual part behaved
correctly. The instruction was still wrong, and nothing in the repo could have said so.

These tests are the thing that can say so:

  * a shell block parses as shell;
  * a placeholder is written so the shell REFUSES rather than substituting silently;
  * every tool, route, env var and provider named in the doc exists in this build.

The last one is what keeps the doc honest as the code moves — rename a tool and this fails,
rather than a customer finding out.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "operations" / "MODEL_AND_SUBSCRIPTION_SETUP.md"

#: Docs a customer follows with their hands on a keyboard. The shell-level checks run over all
#: of them; the checks about BOSS's own vocabulary run over the setup doc, which is the one
#: that names tools, routes and settings. Add a page here when it starts carrying commands.
CUSTOMER_FACING = (
    DOC,
    REPO / "docs" / "operations" / "DESIGN_PARTNER_ONBOARDING.md",
    REPO / "README.md",
    REPO / "engine" / "README.md",
)


@pytest.fixture(scope="module")
def doc() -> str:
    if not DOC.exists():  # pragma: no cover - engine installed standalone
        pytest.skip(f"{DOC} not present in this checkout")
    return DOC.read_text()


def _present() -> list[Path]:
    found = [p for p in CUSTOMER_FACING if p.exists()]
    if not found:  # pragma: no cover - engine installed standalone
        pytest.skip("no customer-facing docs in this checkout")
    return found


def shell_blocks(text: str) -> list[tuple[int, str]]:
    """Every ```bash block, with the line it starts on so a failure names a location."""
    out = []
    for match in re.finditer(r"```bash\n(.*?)```", text, re.S):
        out.append((text[: match.start()].count("\n") + 1, match.group(1)))
    return out


def test_the_doc_has_commands_to_check(doc: str) -> None:
    """A parser that silently matches nothing would make every test below vacuous."""
    assert len(shell_blocks(doc)) >= 8


def test_every_shell_block_is_valid_shell() -> None:
    broken = []
    for path in _present():
        for line, body in shell_blocks(path.read_text()):
            result = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True)
            if result.returncode != 0:
                broken.append(f"{path.name}:{line}: {result.stderr.strip()}")
    assert not broken, "shell blocks that do not parse:\n" + "\n".join(broken)


def test_no_command_carries_a_placeholder_that_substitutes_silently() -> None:
    """THE regression. `<token>` is not a variable in any shell — it is a literal.

    A customer who pastes a command containing one gets a success message and a wrong value.
    The fix is `${VAR:?message}`, which makes the shell refuse and print the message, so the
    failure happens at the keyboard instead of inside a build.
    """
    offenders = []
    for path in _present():
        for line, body in shell_blocks(path.read_text()):
            for placeholder in re.findall(r"<[a-z][a-z0-9_ -]*>", body, re.I):
                offenders.append(f"{path.name}:{line}: {placeholder}")
    assert not offenders, (
        "angle-bracket placeholders are literals to the shell and get stored verbatim; "
        "use ${VAR:?what to do} so the command refuses to run instead:\n"
        + "\n".join(offenders))


def test_every_engine_tool_the_doc_invokes_exists(doc: str) -> None:
    from boss_engine.tools.builtin import build_default_registry

    known = set(build_default_registry().names())

    named = set(re.findall(r"boss-engine call ([a-z_]+)", doc))
    assert named, "no boss-engine invocations found — has the doc or this parser drifted?"
    assert named <= known, f"the doc calls tools that do not exist: {sorted(named - known)}"


def test_every_tool_argument_payload_is_valid_json(doc: str) -> None:
    bad = []
    for payload in re.findall(r"boss-engine call [a-z_]+ --args '([^']*)'", doc):
        try:
            json.loads(payload)
        except json.JSONDecodeError as exc:
            bad.append(f"{payload!r}: {exc}")
    assert not bad, "--args payloads that are not JSON:\n" + "\n".join(bad)


def test_every_api_path_the_doc_curls_is_a_real_route(doc: str) -> None:
    routes_file = REPO / "apps" / "api" / "app" / "main.py"
    if not routes_file.exists():  # pragma: no cover - engine-only checkout
        pytest.skip("apps/api not present in this checkout")
    source = routes_file.read_text()
    declared = set(re.findall(r'@app\.(?:get|post|put|patch|delete)\(\s*"([^"]+)"', source))

    paths = set(re.findall(r'/v1/[a-z0-9\-/]+', doc))
    unknown = {p for p in paths if p.rstrip("/") not in {d.rstrip("/") for d in declared}}
    # Paths carrying a path parameter are written with a placeholder in the doc.
    unknown = {p for p in unknown if "{" not in p}
    assert not unknown, f"the doc curls routes the API does not serve: {sorted(unknown)}"


def test_every_environment_variable_the_doc_names_is_read_somewhere(doc: str) -> None:
    """A doc that names a setting the code never reads sends the operator to a dead end."""
    # Engine source AND the shipped compose files: a variable can legitimately be read by
    # docker compose rather than by BOSS — ports, the bundled store's encryption key — and
    # those are still real settings a reader can act on. What must not survive is a name
    # nothing anywhere consumes.
    engine = REPO / "engine" / "boss_engine"
    sources = [p.read_text() for p in engine.rglob("*.py")]
    sources += [p.read_text() for p in (REPO / "scripts" / "install").glob("*.yml")
                if p.is_file()]
    haystack = "\n".join(sources)

    # Vars the customer sets in their OWN shell for the examples, not BOSS settings.
    ours = {"OPS", "OPS_BASE_URL", "BOSS_API"}
    named = {v for v in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", doc)} - ours
    missing = sorted(v for v in named if v not in haystack)
    assert not missing, f"the doc names settings nothing reads: {missing}"


def test_every_provider_lock_value_the_doc_lists_is_accepted(doc: str) -> None:
    from boss_engine.config import Settings

    # Pydantic wraps class-level dicts as private attributes; .default is the real mapping.
    known = set(Settings._LOCK_CLI.default) | set(Settings._LOCK_OPENAI_COMPATIBLE.default)
    block = re.search(r"BOSS_PROVIDER_LOCK=(\S+)\s*#\s*or ([^\n]+)", doc)
    assert block, "the provider-lock example is gone or reshaped; re-point this test"

    listed = {block.group(1)} | {v.strip() for v in block.group(2).split(",")}
    listed = {v for v in listed if re.fullmatch(r"[a-z_]+", v)}
    assert listed <= known, f"the doc offers locks that do nothing: {sorted(listed - known)}"


def test_every_internal_link_lands_on_a_real_heading() -> None:
    """The doc routes readers by link; a dead one drops them back into hunting.

    This page is organised as "find your situation, follow that one section", which only works
    if the chooser's links resolve. A heading reworded without its links updated is silently
    worse than no links at all.
    """
    broken = []
    for path in _present():
        text = path.read_text()
        slugs = {
            re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
            for h in re.findall(r"^#{1,6}\s+(.*)$", text, re.M)
        }
        for target in re.findall(r"\]\(#([a-z0-9_-]+)\)", text):
            if target not in slugs:
                broken.append(f"{path.name}: #{target}")
    assert not broken, f"links pointing at headings that do not exist: {broken}"


def test_the_doc_points_at_a_helper_script_that_exists(doc: str) -> None:
    for script in re.findall(r"(scripts/[a-z_]+\.py)", doc):
        assert (REPO / script).is_file(), f"the doc tells the customer to run a missing {script}"


def test_every_file_a_command_runs_exists_in_this_repo() -> None:
    """A customer doc may only invoke files a customer actually has.

    `python3 ops.py render …` shipped in Path C. `ops.py` is an operator's local deploy helper
    that lives outside this repository — it exists on one laptop and on no customer's machine.
    The reader followed the page, could not find the file, and was right to stop.

    The earlier check only looked at paths spelled `scripts/*.py`, so a bare filename walked
    straight past it. This one takes the argument of any python invocation, whatever it looks
    like, and insists the repository contains it.
    """
    offenders = []
    for path in _present():
        text = path.read_text()
        for line, body in shell_blocks(text):
            for raw in body.splitlines():
                command = raw.strip()
                if not command.startswith(("python3 ", "python ")):
                    continue
                argument = command.split()[1]
                if argument.startswith("-"):      # python3 -c "…" runs no file
                    continue
                if not (REPO / argument).is_file():
                    offenders.append(f"{path.name}:{line}: {argument}")
    assert not offenders, (
        "commands that run a file this repository does not contain:\n" + "\n".join(offenders))


def test_no_command_points_at_a_path_the_doc_never_locates() -> None:
    """`/path/to/your/.env` is the same defect as `<token>`, wearing different syntax.

    Both look like something to substitute; only one leaves the reader able to. The doc shipped
    `--write-env /path/to/your/.env` without ever saying which file that is, and the reader's
    first question was "where is the .env file?" — which the page could not answer.

    A path in a command must either exist, or the surrounding prose must say how to find it. A
    stand-in spelled /path/to/ is proof it does neither.
    """
    offenders = []
    for path in _present():
        for line, body in shell_blocks(path.read_text()):
            for marker in re.findall(r"/path/to/\S*", body):
                offenders.append(f"{path.name}:{line}: {marker}")
    assert not offenders, (
        "paths the reader cannot resolve; name the real location instead:\n"
        + "\n".join(offenders))
