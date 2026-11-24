"""A P0/P1 test may mock the model, never the step that fails for users (BOSSAPP-1433 R5).

THE RULE. Three gaps an enterprise host met passed their gate tests because the test replaced
the very step that failed: the catalogue's end-to-end test inserted a "running" preview row and
started nothing (RCA row 3), the engine-sandbox proof ran Codex directly instead of through the
build's launcher (row 12), and the Setup proof never wrote (B3). Where a full real step is
impossible in a tier, a test uses the real code path with a LOCAL fake service — a local HTTP
server, a local git remote, a temp dir, a fake external binary on PATH — not a monkeypatch of
BOSS's own function.

This reads every P0 and P1 engine/API test file listed in tests/tiers.yaml with `ast` and fails
on any `monkeypatch.setattr`, `mock.patch` or `patch.object` aimed at one of the LAST-MILE
functions below. The list is small and explicit on purpose; each entry says why it is one. A line
that truly must replace one carries `# last-mile-waiver: <reason>` (none does today).
"""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WAIVER = "last-mile-waiver:"

#: (module, attribute) -> why replacing it in a gate test hides the failure users meet.
LAST_MILE: dict[tuple[str, str], str] = {
    ("boss_engine.services.preview_service", "start_preview"):
        "the preview start itself: a faked one passed while License Shelf's never started (RCA 3)",
    ("boss_engine.services.preview_service", "launch_app"):
        "the one launcher a preview and the build's serve proof share",
    ("boss_engine.services.preview_service", "_discover_working_port"):
        "the wait for the app to answer HTTP: replacing it claims an app serves that never bound",
    ("boss_engine.services.preview_service", "prove_app_serves"):
        "the build's start-the-app proof: the step between 'tests pass' and 'it runs'",
    ("boss_engine.services.owned_process", "workload_spawn"):
        "the build launcher's wrap (boss-sandbox-exec run <uid> --): what Codex really starts under",
    ("boss_engine.services.sandbox_identity", "SandboxIdentity.wrap"):
        "the boss-sandbox-exec argv every sandboxed spawn gets (RCA 12)",
    ("boss_engine.services.engine_inner_sandbox", "probe"):
        "the engine inner-sandbox probe: must run through the build's launcher (RCA 12)",
    ("boss_engine.services.engine_inner_sandbox", "as_a_build"):
        "where the probe runs: under a build's sandbox identity, as builds do (RCA 12)",
    ("boss_engine.services.setup_checks", "probe_repository"):
        "Setup's repository proof: the write a build needs (RCA B3)",
    ("boss_engine.services.engine_policy", "writer_check"):
        "Setup's coding-engine proof: one session that must write a file and run a command (B3)",
}


def gate_test_files() -> list[Path]:
    tiers = yaml.safe_load((ROOT / "tests" / "tiers.yaml").read_text())
    out: list[Path] = []
    for tier in ("P0", "P1"):
        for suite in ("engine", "api"):
            for rel in (tiers.get(tier) or {}).get(suite) or []:
                path = ROOT / str(rel).split("::", 1)[0]
                if path.suffix == ".py" and path.is_file() and path not in out:
                    out.append(path)
    return out


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted module or object it stands for, from the file's imports."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                names[a.asname or a.name] = f"{node.module}.{a.name}"
    return names


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _resolve(expr: str, aliases: dict[str, str]) -> str:
    head, _, rest = expr.partition(".")
    base = aliases.get(head, head)
    return f"{base}.{rest}" if rest else base


def _is_last_mile(target: str) -> tuple[str, str] | None:
    for module, attr in LAST_MILE:
        if target == f"{module}.{attr}":
            return module, attr
        # `from boss_engine.services import preview_service` then services.preview_service...
        short = module.rsplit(".", 1)[-1]
        if target.endswith(f".{short}.{attr}") or target == f"{short}.{attr}":
            return module, attr
    return None


def _targets(call: ast.Call, aliases: dict[str, str]) -> list[str]:
    """The dotted names a setattr/patch call replaces."""
    fn = _dotted(call.func)
    last = fn.rsplit(".", 1)[-1]
    args = call.args
    out: list[str] = []
    if last == "setattr" and args:
        if isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
            out.append(args[0].value)                            # setattr("pkg.mod.name", ...)
        elif len(args) >= 2 and isinstance(args[1], ast.Constant) \
                and isinstance(args[1].value, str):
            obj = _dotted(args[0])
            if obj:
                out.append(f"{_resolve(obj, aliases)}.{args[1].value}")
    elif last == "object" and fn.endswith("patch.object") and len(args) >= 2 \
            and isinstance(args[1], ast.Constant):
        obj = _dotted(args[0])
        if obj:
            out.append(f"{_resolve(obj, aliases)}.{args[1].value}")
    elif last == "patch" and args and isinstance(args[0], ast.Constant) \
            and isinstance(args[0].value, str):
        out.append(args[0].value)
    return out


def offences(path: Path) -> list[str]:
    source = path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    aliases = _aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for target in _targets(node, aliases):
            hit = _is_last_mile(target)
            if hit is None:
                continue
            if WAIVER in lines[node.lineno - 1]:
                continue
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} replaces {hit[0]}.{hit[1]} "
                         f"({LAST_MILE[hit]})")
    return found


def test_the_gate_lists_real_files():
    files = gate_test_files()
    assert len(files) > 50, files


def test_no_p0_or_p1_test_replaces_a_last_mile_step():
    found = [o for path in gate_test_files() for o in offences(path)]
    assert not found, (
        "A P0/P1 test may mock the model, never the step that fails for users. Use the real "
        "code path with a local fake service (a local HTTP server, a local git remote, a temp "
        "dir, a fake binary on PATH):\n  " + "\n  ".join(found))


def test_the_guard_sees_every_form_of_replacement(tmp_path):
    """The forms a test uses to replace a function, each caught (and a waiver honoured)."""
    sample = tmp_path / "t.py"
    sample.write_text(
        "from unittest import mock\n"
        "from unittest.mock import patch\n"
        "from boss_engine.services import preview_service as ps, engine_inner_sandbox\n"
        "from boss_engine.services.sandbox_identity import SandboxIdentity\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(ps, 'start_preview', None)\n"
        "    monkeypatch.setattr('boss_engine.services.setup_checks.probe_repository', None)\n"
        "    monkeypatch.setattr(SandboxIdentity, 'wrap', None)\n"
        "    mock.patch.object(engine_inner_sandbox, 'probe')\n"
        "    patch('boss_engine.services.engine_policy.writer_check')\n"
        "    monkeypatch.setattr(ps, 'launch_app', None)  # last-mile-waiver: example\n"
        "    monkeypatch.setattr(ps, 'preview_port', None)\n")
    global ROOT
    saved, ROOT = ROOT, tmp_path
    try:
        found = offences(sample)
    finally:
        ROOT = saved
    assert [f.split(" replaces ")[1].split(" (")[0] for f in found] == [
        "boss_engine.services.preview_service.start_preview",
        "boss_engine.services.setup_checks.probe_repository",
        "boss_engine.services.sandbox_identity.SandboxIdentity.wrap",
        "boss_engine.services.engine_inner_sandbox.probe",
        "boss_engine.services.engine_policy.writer_check",
    ]
