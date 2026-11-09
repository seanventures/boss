"""A setting the docs tell an operator to put in `.env` must reach the container that reads it.

The community compose passes a NAMED LIST of variables into api and worker (no env_file, and
.dockerignore keeps .env out of the image). The rc10.11 notes said "set BOSS_AMBIENT_COS=propose
in .env and restart" — and nothing happened, because compose never forwarded it: the always-on
agents could not be switched on at all (2026-11-06). The same was true of the SMTP settings
SIGN_IN_SETUP.md lists and of BOSS_MEETING_PRD_AUTO_APPROVE.

The guard is a class, not a list:

1. Every BOSS_* name the engine reads (a Settings alias, or an os.environ read in engine/api)
   that a doc tells an operator to set in `.env` is forwarded to api AND worker.
2. Every always-on / autonomy setting the engine reads (the ambient family) is forwarded,
   documented or not, with its default OFF.
3. A forwarded Settings knob, left unset in .env, behaves exactly like a stock install: the
   compose default parses and equals config.py's default (`${VAR:-}` SETS "", it does not unset).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "scripts" / "install" / "community-compose.yml"
SERVICES = ("api", "worker")
ENV_READ = re.compile(r"""(?:environ(?:\.get)?\(\s*|environ\[\s*|getenv\(\s*)["'](BOSS_[A-Z0-9_]+)["']""")
#: Constants that name an env var (ENV_DISABLED = "BOSS_AMBIENT_DISABLED", ENV_FLAG = ...).
ENV_CONST = re.compile(r"""^\s*_?ENV_[A-Z_]*\s*=\s*["'](BOSS_[A-Z0-9_]+)["']""", re.M)
#: Named in .env, but read on the HOST (make mcp, the host CLI), never inside a container; or a
#: service that deliberately authenticates with its own key. Each with the reason.
NOT_A_CONTAINER_SETTING: dict[tuple[str, str], str] = {
    ("*", "BOSS_MCP_API_KEY"): "the host MCP client's key (make mcp), not a server setting",
    ("*", "BOSS_PROJECT_ID"): "make mcp-setup's argument on the host",
    ("worker", "BOSS_API_KEY"): "the worker authenticates with BOSS_WORKER_API_KEY",
}
AMBIENT_FAMILY = re.compile(r"^BOSS_(AMBIENT_|ROADMAP_|DEMO_|DOSSIER_)")


def _environment(service: str) -> dict:
    if not COMPOSE.exists():  # pragma: no cover - engine installed standalone
        pytest.skip("the community compose file is not in this checkout")
    return yaml.safe_load(COMPOSE.read_text())["services"][service]["environment"]


def _settings_aliases() -> dict[str, str]:
    """alias → field name, for alias= and every BOSS_* in a validation AliasChoices."""
    from boss_engine.config import Settings

    out: dict[str, str] = {}
    for name, field in Settings.model_fields.items():
        if field.alias:
            out[field.alias] = name
        choices = getattr(field.validation_alias, "choices", None) or []
        for choice in choices:
            if isinstance(choice, str):
                out[choice] = name
    return out


def _env_reads() -> set[str]:
    found: set[str] = set()
    for root in (REPO / "engine" / "boss_engine", REPO / "apps" / "api" / "app"):
        for path in root.rglob("*.py"):
            text = path.read_text(errors="ignore")
            found.update(ENV_READ.findall(text))
            found.update(ENV_CONST.findall(text))
    return found


def _engine_reads() -> set[str]:
    return {k for k in _settings_aliases() if k.startswith("BOSS_")} | _env_reads()


def _told_to_set_in_env() -> dict[str, str]:
    """name → 'doc:line' for every BOSS_* a doc tells an operator to set in .env: on a line that
    says `.env`, or inside a code block introduced by a sentence that does."""
    out: dict[str, str] = {}
    docs = sorted((REPO / "docs").rglob("*.md")) + [REPO / "README.md"]
    for doc in docs:
        if not doc.exists():
            continue
        lines = doc.read_text(errors="ignore").splitlines()
        in_block = block_is_env = False
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                if not in_block:
                    lead = " ".join(lines[max(0, i - 3):i])
                    block_is_env = ".env" in lead or "env file" in lead
                in_block = not in_block
                continue
            if ".env" in line or (in_block and block_is_env):
                for name in re.findall(r"\b(BOSS_[A-Z0-9_]+)\b", line):
                    out.setdefault(name, f"{doc.relative_to(REPO)}:{i + 1}")
    return out


def test_every_setting_the_docs_tell_you_to_set_reaches_the_container():
    reads = _engine_reads()
    told = {k: where for k, where in _told_to_set_in_env().items() if k in reads}
    assert "BOSS_AMBIENT_COS" in told, "the scan no longer sees the rc10.11 instruction"
    missing = [f"{service} drops {name} (told at {where})"
               for service in SERVICES for name, where in sorted(told.items())
               if name not in _environment(service)
               and ("*", name) not in NOT_A_CONTAINER_SETTING
               and (service, name) not in NOT_A_CONTAINER_SETTING]
    assert not missing, ("the docs tell operators to set these in .env, but compose never "
                         "forwards them, so setting them does nothing:\n" + "\n".join(missing))


def test_every_always_on_setting_the_engine_reads_is_forwarded():
    family = sorted(k for k in _engine_reads() if AMBIENT_FAMILY.match(k))
    assert {"BOSS_AMBIENT_COS", "BOSS_AMBIENT_DISABLED", "BOSS_AMBIENT_SQUADS",
            "BOSS_ROADMAP_DISABLED"} <= set(family), family
    missing = [f"{service} drops {name}" for service in SERVICES for name in family
               if name not in _environment(service)]
    assert not missing, "\n".join(missing)


def _compose_default(raw: object) -> str | None:
    match = re.fullmatch(r"\$\{[A-Z0-9_]+:-(.*)\}", str(raw).strip())
    return match.group(1) if match else None


@pytest.mark.parametrize("service", SERVICES)
def test_an_unset_forwarded_knob_behaves_like_a_stock_install(service, monkeypatch):
    """For every forwarded always-on knob and every knob a doc tells you to set: the value
    compose writes when .env is silent must parse and equal config.py's default."""
    from boss_engine.config import Settings

    aliases = _settings_aliases()
    told = set(_told_to_set_in_env())
    env = _environment(service)
    checked, wrong = 0, []
    for name, raw in sorted(env.items()):
        if name not in aliases or not (AMBIENT_FAMILY.match(name) or name in told):
            continue
        default = _compose_default(raw)
        if default is None or "${" in default:   # a nested fallback is another knob's job
            continue
        for other in list(aliases):
            monkeypatch.delenv(other, raising=False)
        monkeypatch.setenv(name, default)
        field = aliases[name]
        try:
            got = getattr(Settings(_env_file=None), field)
        except Exception as exc:  # noqa: BLE001 — a parse error is the finding
            wrong.append(f"{name}={default!r} does not even parse: {type(exc).__name__}")
            continue
        monkeypatch.delenv(name, raising=False)
        stock = getattr(Settings(_env_file=None), field)
        checked += 1
        if got != stock and not (got in ("", None) and stock in ("", None)):
            # "" and None are both "not set" for an optional text knob; anything else differs.
            wrong.append(f"{name}: compose gives {got!r}, a stock install {stock!r}")
    assert checked >= 10
    assert not wrong, "\n".join(wrong)


def test_always_on_ships_off_through_compose(monkeypatch):
    """Nothing set in .env → the forwarded values leave the agents off."""
    from boss_engine.config import Settings
    from boss_engine.services.ambient import policy

    for name, raw in _environment("api").items():
        if AMBIENT_FAMILY.match(name) and _compose_default(raw) is not None:
            monkeypatch.setenv(name, _compose_default(raw))
    s = Settings(_env_file=None)
    assert policy.cos_mode(s) == "off" and not policy.env_pins_mode(s)
    assert not policy.signals_on(s) and not policy.learning_on(s)
