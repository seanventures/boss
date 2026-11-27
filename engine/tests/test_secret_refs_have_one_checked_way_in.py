"""Every secret_refs row is written by ONE of a short, named list of functions (BOSSAPP-1286):

* SecretRefService.create_secret_ref — THE checked path for a reference to material that already
  exists (env and host-file references need org authority; reserved metadata is refused);
* BOSS's own WRITE-THROUGHS, which store the material themselves in the same function and record
  the reference to what they just wrote: secrets_service.put_secret, the database backend's own
  slot (DatabaseBackend.store), the channel OAuth stores (save_oauth_client, complete) and a
  GitHub connection's own slot (GitHubConnectionService._write_connection_slot).

A static scan fails on an INSERT INTO secret_refs anywhere else, so a new code path cannot mint a
reference without the checks (resource_plane._ensure_secret_ref and connection_spend used to).
Adding a write-through to the list is a reviewed change to this file.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_INSERT = re.compile(r"INSERT\s+INTO\s+secret_refs\b", re.I)
CHECKED_PATH = "engine/boss_engine/services/secret_refs.py:create_secret_ref"
WRITE_THROUGHS = {
    "engine/boss_engine/services/secrets_service.py:put_secret",
    "engine/boss_engine/services/secret_backends.py:store",
    "engine/boss_engine/services/channel_oauth.py:save_oauth_client",
    "engine/boss_engine/services/channel_oauth.py:complete",
    "engine/boss_engine/services/github_connections.py:_write_connection_slot",
}


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
                        out.append(f"{rel}:{fn.name}")
    return out


def test_no_code_writes_a_secret_ref_except_the_checked_path_or_a_named_write_through():
    bad = [name for name in _writers() if name != CHECKED_PATH and name not in WRITE_THROUGHS]
    assert not bad, ("create references through SecretRefService.create_secret_ref:\n"
                     + "\n".join(bad))


def test_the_checked_path_is_still_the_one_that_inserts():
    assert CHECKED_PATH in set(_writers())


def test_a_person_cannot_set_the_connection_binding():
    import pytest

    from boss_engine.services.secret_refs import SecretRefService

    with pytest.raises(ValueError):
        SecretRefService._validate_metadata({"connection_slot": {"connection_id": "c"}})  # noqa: SLF001
