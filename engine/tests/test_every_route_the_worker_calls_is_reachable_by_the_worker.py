"""Every API route the worker calls must be reachable BY the worker.

The API holds a write whose path ends in a high-risk action (`execute`, `terminate`, ...) for
human confirmation, and the worker is not a human — so a new worker route ending in such a word
is answered 409 unless it is registered as a worker protocol in boss_engine/mcp/api_surface.py.
That happened to POST /v1/discoveries/{run_id}/execute (2026-10-01): every Community discovery
sat queued while its job deadlettered, and the route's own test passed because it called the
route as a human.

This reads the worker's client (apps/worker/boss_worker/control_plane.py), resolves each call to
its mounted route in the Community MCP matrix, and asks the same policy the API middleware asks.
"""
import json
import re
from pathlib import Path

from boss_engine.mcp.api_surface import exclusion_reason, needs_operator_confirmation

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "apps" / "worker" / "boss_worker" / "control_plane.py"
MATRIX = ROOT / "docs" / "community" / "mcp-api-capability-matrix.json"


def _worker_calls() -> set[tuple[str, str]]:
    source = CLIENT.read_text()
    return {(m.group(1).upper(), m.group(2)) for m in re.finditer(
        r'_client\.(post|put|patch|delete)\(\s*f?"(/v1[^"]*)"', source)}


def _template(method: str, path: str, mounted: set[tuple[str, str]]) -> str | None:
    pattern = "^" + re.escape(re.sub(r"\{[^}]*\}", "{x}", path)).replace(
        r"\{x\}", r"\{[^/]+\}") + "$"
    hits = sorted(p for m, p in mounted if m == method and re.match(pattern, p))
    return hits[0] if hits else None


def test_the_worker_client_was_read():
    assert len(_worker_calls()) > 20, "the call pattern no longer matches the worker client"


def test_no_worker_route_is_held_for_human_confirmation():
    mounted = {(row["method"], row["path"])
               for row in json.loads(MATRIX.read_text())["operations"]}
    held, unknown = [], []
    for method, path in sorted(_worker_calls()):
        template = _template(method, path, mounted)
        if template is None:
            unknown.append(f"{method} {path}")
        elif (needs_operator_confirmation(method, template)
              and not exclusion_reason(method, template)):
            held.append(f"{method} {template}")
    assert not unknown, (
        f"worker calls routes missing from the MCP matrix: {unknown}. Regenerate it "
        "(scripts/audit_mcp_api_parity.py) after mounting the route.")
    assert not held, (
        f"the API would answer the worker 409 on {held}: register each as a worker lease "
        "protocol in boss_engine/mcp/api_surface.py (_EXCLUDED_EXACT), as "
        "/v1/discoveries/{run_id}/execute is.")
