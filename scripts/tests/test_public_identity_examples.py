"""Community-shipped API examples must not name an operator GitHub account.

This reads only the API env example and the five fixture modules that ship with
it. It does not scan deploy/, mkdocs.yml, or the rest of the repository.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR_LOGIN = "seanheiney"
CHECKED_PATHS = (
    "apps/api/.env.example",
    "apps/api/tests/test_self_hosted_repo_credentials.py",
    "apps/api/tests/test_branch_protection_gate_unit.py",
    "apps/api/tests/test_orchestrator_next_move.py",
    "apps/api/tests/test_api_e2e_smoke.py",
    "apps/api/tests/test_concurrency_governance_api.py",
)


def test_public_identity_examples_omit_operator_login():
    offenders = [
        relative
        for relative in CHECKED_PATHS
        if OPERATOR_LOGIN in (ROOT / relative).read_text(encoding="utf-8")
    ]
    assert offenders == []
