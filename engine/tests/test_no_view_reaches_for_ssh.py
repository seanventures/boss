"""No Infra view reaches for SSH (spec §6.8): a poll never opens a session to a host.

`infra_view` must not import the SSH adapter, nor anything that imports it at module load
(resource_plane does). Host checks run only on an explicit Re-verify, which is a write route.
The observability half (obs_view, the cached stack status) is stream S2's.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import boss_engine.services.infra_view as iv

FORBIDDEN = ("ssh_host_adapter", "resource_plane", "deploy_service", "deployment_connectors")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_infra_view_imports_no_ssh_anywhere_in_its_source():
    names = _imports(Path(iv.__file__))
    for bad in FORBIDDEN:
        assert not any(bad in n for n in names), f"infra_view imports {bad}"


def test_loading_every_module_a_view_uses_loads_no_ssh_adapter():
    """infra_view and every module its builders import lazily, loaded in a fresh interpreter:
    the SSH adapter and resource_plane must not come along."""
    code = (
        "import sys\n"
        "import boss_engine.services.infra_view\n"
        "import boss_engine.services.deployment_fleet\n"
        "import boss_engine.services.deployment_health\n"
        "import boss_engine.services.stack_deploy\n"
        "import boss_engine.services.decision_actions\n"
        "import boss_engine.services.agents_live_board\n"
        "bad = [m for m in sys.modules if m.endswith(('ssh_host_adapter', 'resource_plane'))]\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(iv.__file__).resolve().parents[3]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"a view module loads {out.stdout.strip()}"


def test_the_monitoring_resident_is_read_from_events_not_probed():
    src = Path(iv.__file__).read_text()
    body = src[src.index("def monitoring_stored"):src.index("# ── Fleet ──")]
    assert "stack_status" not in body and "_ssh" not in body
