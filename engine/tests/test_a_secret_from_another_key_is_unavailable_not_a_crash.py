"""A secret stored under another master key is UNAVAILABLE, not a crash.

An install restarted with a different (or ephemeral) KMS master key cannot decrypt what the old
key stored. Every reader already says "this key cannot be read — generate a new one" for
SecretUnavailable; a raw cryptography InvalidTag escaped instead and Infra's Roll back answered
500 (found in the Infra + Observability browser walk, 2026-10-05).
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

from ._obs_seed import pg_engine
from boss_engine.services.secret_backends import DatabaseBackend, SecretUnavailable


def test_a_value_from_another_key_raises_secret_unavailable():
    eng = pg_engine()
    key = f"K_test_{uuid4().hex[:8]}"
    DatabaseBackend(eng, master_key=os.urandom(32)).put(key, "s3cret")
    reader = DatabaseBackend(eng, master_key=os.urandom(32))
    with pytest.raises(SecretUnavailable) as caught:
        reader.get(key)
    assert "cannot be decrypted" in str(caught.value)
    assert "s3cret" not in str(caught.value)
