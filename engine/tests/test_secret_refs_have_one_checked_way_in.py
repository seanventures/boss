"""Every secret_refs row is written by ONE of two kinds of code (BOSSAPP-1286, seventh review):

* SecretRefService.create_secret_ref — the checked path for a reference to material that already
  exists (env and host-file references need org authority; a global-key store reference may name
  only material recorded as its own scope's);
* a WRITE-THROUGH that stores the material itself and records its owner first
  (secrets_service.record_material_owner) — put_secret, the channel OAuth stores, the database
  backend's own slot.

A static scan fails on any other INSERT INTO secret_refs, so a new code path cannot mint a
reference without the checks (resource_plane._ensure_secret_ref and connection_spend used to).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_INSERT = re.compile(r"INSERT\s+INTO\s+secret_refs\b", re.I)
CHECKED_PATH = "engine/boss_engine/services/secret_refs.py:create_secret_ref"


def _writers() -> list[str]:
    out = []
    for top in ("engine/boss_engine", "apps/api/app", "apps/worker", "apps/egress"):
        for path in sorted((ROOT / top).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            if not _INSERT.search(src):
                continue
            for fn in ast.walk(ast.parse(src)):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seg = ast.get_source_segment(src, fn) or ""
                    inner = [n for n in ast.walk(fn) if n is not fn and isinstance(
                        n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _INSERT.search(
                        ast.get_source_segment(src, n) or "")]
                    if _INSERT.search(seg) and not inner:
                        out.append((f"{rel}:{fn.name}", seg))
    return out


def test_no_code_writes_a_secret_ref_except_the_checked_path_or_a_recorded_write_through():
    bad = [name for name, seg in _writers()
           if name != CHECKED_PATH and "record_material_owner(" not in seg]
    assert not bad, ("write secret_refs through SecretRefService.create_secret_ref, or record the "
                     "material's owner (secrets_service.record_material_owner) in the same "
                     "write-through:\n" + "\n".join(bad))


def test_the_checked_path_is_still_the_one_that_inserts():
    assert CHECKED_PATH in {name for name, _seg in _writers()}


def test_the_reserved_secret_scopes_are_the_ones_the_install_uses():
    from boss_engine.services import cli_harness, llm_gateway
    from boss_engine.services.secrets_service import SYSTEM_SECRET_SCOPES

    assert {cli_harness.SUBSCRIPTION_SECRET_SCOPE, llm_gateway.SECRET_SCOPE} <= SYSTEM_SECRET_SCOPES
