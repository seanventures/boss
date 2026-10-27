"""Docs, help and agent copy describe ONE subscription route: sign in inside BOSS (BOSSAPP-769).

The audit found every user-facing page and help text promising an import BOSS had refused for
months: `--connect-codex` "imports" the Codex auth file, `claude setup-token`,
`store_subscription_token.py`, "drop auth.json", and `--accept-database-fallback` for a
subscription. A person following them got `None` and a loop. These checks fail when such
advice comes back, in the docs a customer reads, in every script's help, and in what BOSS's
own agents tell people.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Pages a customer reads, plus every Community guide.
DOCS = sorted({*ROOT.glob("docs/community/*.md"), ROOT / "README.md",
               ROOT / "scripts/install/README.md"})

#: `claude setup-token` may be named only as what BOSS ITSELF runs on its server (the console's
#: Sign in, 2026-10-27) — never as a command a person runs to paste its token somewhere.
SETUP_TOKEN = re.compile(r"setup-token")
RUN_BY_BOSS = re.compile(r"(?i)BOSS runs `claude setup-token` inside BOSS")
#: Phrases that only ever described the retired import.
BANNED = (
    (SETUP_TOKEN, "a Claude setup token is never pasted"),
    (re.compile(r"store_subscription_token"), "the import script is retired"),
    (re.compile(r"accept-database-fallback"), "a subscription stores no credential"),
    (re.compile(r"coding-agents/credential"), "the import route is gone"),
    (re.compile(r"(?i)--connect-codex\b.*\b(import|auth[ .]?json|copy|copies)"),
     "--connect-codex signs in inside BOSS; it imports nothing"),
    (re.compile(r"(?i)\b(import|imports|importing)\b.*\b(codex|claude|grok)\b.*\b(login|subscription|token)"),
     "nothing imports a subscription"),
)
#: auth.json may only be named to say BOSS never touches it.
AUTH_FILE = re.compile(r"auth\.json")
AUTH_FILE_VERB = re.compile(r"(?i)\b(copy|copies|copying|drop|import|paste|upload|send)\b|<\s*~/")
NEGATION = re.compile(r"(?i)\b(not|never|no)\b|n't")


def _problems(name: str, text: str) -> list[str]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern, why in BANNED:
            if pattern.search(line) and not (pattern is SETUP_TOKEN and RUN_BY_BOSS.search(line)):
                found.append(f"{name}:{number}: {why}: {line.strip()[:160]}")
        if AUTH_FILE.search(line) and AUTH_FILE_VERB.search(line) and not NEGATION.search(line):
            found.append(f"{name}:{number}: auth.json is never moved: {line.strip()[:160]}")
    return found


def _heredoc_after(path: Path, marker: str) -> str:
    """The first heredoc body after `marker` (a script's usage text)."""
    text = path.read_text()
    start = text.index(marker)
    head = re.compile(r"<<-?'?(\w+)'?\n").search(text, start)
    assert head, f"{path.name}: no usage heredoc after {marker}"
    end = text.index(f"\n{head.group(1)}\n", head.end())
    return text[head.end():end]


def _help_texts() -> dict[str, str]:
    install = ROOT / "scripts" / "install"
    texts = {
        "boss.sh usage": _heredoc_after(install / "boss.sh", "usage() {"),
        "install.sh usage": _heredoc_after(ROOT / "install.sh", "usage() {"),
        "remote.sh usage": _heredoc_after(install / "remote.sh", "usage() {"),
    }
    helper = subprocess.run([sys.executable, str(install / "coding_subscriptions.py"), "--help"],
                            capture_output=True, text=True, timeout=30)
    texts["coding_subscriptions.py --help"] = helper.stdout
    return texts


@pytest.mark.parametrize("page", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_user_docs_describe_only_the_sign_in(page: Path) -> None:
    assert not _problems(str(page.relative_to(ROOT)), page.read_text())


def test_help_texts_describe_only_the_sign_in() -> None:
    texts = _help_texts()
    assert all(texts.values()), [name for name, text in texts.items() if not text]
    problems = [p for name, text in texts.items() for p in _problems(name, text)]
    assert not problems, "\n".join(problems)
    assert "codex   sign Codex in inside the BOSS container" in texts["boss.sh usage"]
    assert "--sign-in" in texts["coding_subscriptions.py --help"]


def test_agents_and_hints_name_the_sign_in() -> None:
    """What BOSS's own agents and status rows say (MCP tool copy, each CLI's next step)."""
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES
    from boss_engine.tools import build_default_registry

    registry = build_default_registry()
    copy = {name: registry.get(name).description
            for name in ("connect_coding_agent", "detect_coding_agents")}
    for name in ("codex", "claude_code", "grok"):
        spec = BUILTIN_CLI_HARNESSES[name]
        copy[f"{name}.token_hint"] = spec.token_hint
        copy[f"{name}.signin_hint"] = spec.signin_hint
    problems = [p for name, text in copy.items() for p in _problems(name, text)]
    assert not problems, "\n".join(problems)
    for tool in ("connect_coding_agent", "detect_coding_agents"):
        for provider in ("codex", "claude_code", "grok"):
            assert provider in copy[tool], (tool, provider)
        assert "Import" not in copy[tool]
    assert "paused" not in copy["connect_coding_agent"]
    assert "boss connect codex" in copy["codex.token_hint"]


def _source_files():
    skip = {"node_modules", ".next", ".git", "tests", "__pycache__", "dist", "build", ".venv"}
    for top in ("apps", "engine", "scripts", "packages", "docs", "api"):
        for path in (ROOT / top).rglob("*"):
            if path.suffix in {".py", ".sh", ".ts", ".tsx", ".md", ".yaml", ".yml", ".json"} \
                    and not skip.intersection(path.relative_to(ROOT).parts) \
                    and path.is_file():
                yield path
    yield from (p for p in (ROOT / "install.sh", ROOT / "README.md") if p.is_file())


def test_nothing_outside_tests_calls_the_import_route() -> None:
    """P0-1 (a): the import route is gone, and no script, doc or client still names it."""
    outside = [str(path.relative_to(ROOT)) for path in _source_files()
               if "coding-agents/credential" in path.read_text(errors="ignore")]
    assert outside == []
