"""id-sec-final2 residual: nothing called sweep_control_plane_tokens, so an unscoped GitHub App
token minted by the control plane lived until the next mint (or GitHub's hour). The identity
maintenance tick sweeps its process's due tokens — BEFORE the cross-process lock, because the list
is in-process — and the worker's loop sweeps its own."""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

from boss_engine.identity import github_app, github_setup, maintenance

ROOT = Path(__file__).resolve().parents[2]


class _LockHeldElsewhere:
    """An engine whose maintenance lock another process holds."""

    @contextlib.contextmanager
    def begin(self):
        class _Db:
            def execute(self, *_a, **_k):
                class _R:
                    def scalar(self):
                        return False
                return _R()
        yield _Db()


def test_the_maintenance_tick_revokes_due_unscoped_tokens_in_every_process(monkeypatch):
    revoked: list[str] = []
    monkeypatch.setattr(github_app, "revoke_installation_token", lambda t: revoked.append(t))
    monkeypatch.setattr(github_setup, "_UNSCOPED", [
        (time.time() - github_setup.CONTROL_PLANE_TTL_SECONDS - 5, "ghs_due"),
        (time.time(), "ghs_fresh"),
    ])
    out = maintenance.tick(_LockHeldElsewhere())
    assert "skipped" in out                       # another process ran the shared steps…
    assert out["control_plane_tokens"] == 1       # …and this one still swept its own
    assert revoked == ["ghs_due"]
    assert [t for _at, t in github_setup._UNSCOPED] == ["ghs_fresh"]


def test_the_worker_loop_sweeps_its_own_tokens():
    src = (ROOT / "apps" / "worker" / "boss_worker" / "main.py").read_text()
    assert "sweep_control_plane_tokens()" in src
    assert "_tick_control_plane_tokens()" in src.split("while not _STOPPING:")[-1]
