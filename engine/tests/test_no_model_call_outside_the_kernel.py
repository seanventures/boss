"""K14 LINT: NO MODEL CALL OUTSIDE THE KERNEL OR THE GOVERNED ADAPTER (AP-P1b acceptance (e)).

Six loops in BOSS each called a model their own way (HARNESS_KERNEL_DESIGN.md §1.1). The kernel
is the one loop with mandatory governance around every call; this test keeps it that way. It
reads every module of the engine, the API and the worker, finds every MODEL CALL
(`create_text_response`, `create_chat_completion`, `stream_text_response`, `create_plain_answer`,
an SDK's `chat.completions.create` / `messages.create` / `responses.create`) and every CODING
ENGINE INVOCATION (`run_role`, `run_research`, `run_verdict`, `run_plain`), and fails when one is
outside `engine/boss_engine/kernel/` and not in model_call_allowlist.yaml — the explicit list of
Delegate harness entry points, the adapters themselves, the old loops kept behind their kernel
flags, and the workers still to move, each with its reason. The list only shrinks: an entry
that no longer makes its call fails too.
"""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("engine/boss_engine", "apps/api/app", "apps/worker/boss_worker")
KERNEL = "engine/boss_engine/kernel/"
ALLOWLIST = Path(__file__).with_name("model_call_allowlist.yaml")

MODEL_CALLS = frozenset({"create_text_response", "create_chat_completion",
                         "stream_text_response", "create_plain_answer"})
ENGINE_CALLS = frozenset({"run_role", "run_research", "run_verdict", "run_plain"})
SDK_CHAINS = (("chat", "completions", "create"), ("messages", "create"),
              ("responses", "create"), ("messages", "stream"))
SECTIONS = ("delegate", "transport", "legacy", "migrate")


def _chain(node: ast.AST) -> tuple:
    parts: list = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return tuple(reversed(parts))


def calls_in(path: Path) -> set:
    found: set = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        chain = _chain(node.func)
        if chain[-1] in MODEL_CALLS or chain[-1] in ENGINE_CALLS:
            found.add(chain[-1])
        for sdk in SDK_CHAINS:
            if chain[-len(sdk):] == sdk:
                found.add(".".join(sdk))
    return found


def call_sites() -> dict:
    out: dict = {}
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            found = calls_in(path)
            if found:
                out[str(path.relative_to(ROOT))] = found
    return out


def allowlist() -> dict:
    data = yaml.safe_load(ALLOWLIST.read_text()) or {}
    assert set(data) <= set(SECTIONS), f"unknown sections {set(data) - set(SECTIONS)}"
    entries: dict = {}
    for section in SECTIONS:
        for path, entry in (data.get(section) or {}).items():
            assert path not in entries, f"{path} is listed twice"
            assert str((entry or {}).get("why") or "").strip(), f"{path} gives no reason"
            entries[path] = {**entry, "section": section}
    return entries


def test_no_model_call_outside_the_kernel_or_the_allowlist():
    sites, allowed = call_sites(), allowlist()
    unlisted = {path: sorted(found) for path, found in sites.items()
                if not path.startswith(KERNEL) and path not in allowed}
    assert not unlisted, (
        "These modules call a model (or a coding engine) outside the kernel. Run the call "
        "through kernel.run with a preset — governance is mandatory there — or, for a "
        "Delegate harness entry point, list it in model_call_allowlist.yaml with its reason: "
        f"{unlisted}")


def test_a_listed_module_makes_only_the_calls_it_is_listed_for():
    sites, allowed = call_sites(), allowlist()
    widened = {path: sorted(sites[path] - set(entry["calls"]))
               for path, entry in allowed.items() if path in sites
               and sites[path] - set(entry["calls"])}
    assert not widened, f"listed modules started new model calls: {widened}"


def test_the_list_only_shrinks():
    sites, allowed = call_sites(), allowlist()
    stale = {path: sorted(set(entry["calls"]) - sites.get(path, set()))
             for path, entry in allowed.items()
             if set(entry["calls"]) - sites.get(path, set())}
    assert not stale, f"these entries no longer need their reason; remove them: {stale}"


def test_delegate_entries_are_harness_entry_points():
    for path, entry in allowlist().items():
        if entry["section"] == "delegate":
            assert set(entry["calls"]) & ENGINE_CALLS, (
                f"{path}: a delegate entry must invoke a coding engine (run_role/run_research)")


def test_every_legacy_loop_is_behind_a_kernel_flag_that_is_on_by_default():
    from boss_engine.kernel.flags import DEFAULTS

    legacy = {path: entry for path, entry in allowlist().items()
              if entry["section"] == "legacy"}
    assert legacy
    for path, entry in legacy.items():
        assert DEFAULTS.get(entry.get("flag")) is True, (path, entry.get("flag"))


def test_the_kernel_is_where_the_engines_call_models():
    sites = call_sites()
    kernel_sites = {path for path in sites if path.startswith(KERNEL)}
    assert kernel_sites == {"engine/boss_engine/kernel/engine.py",
                            "engine/boss_engine/kernel/anthropic_engine.py"}, kernel_sites
