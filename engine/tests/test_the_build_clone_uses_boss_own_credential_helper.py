"""R2 (the real-stack build walk): BOSS's own clone of a build's repository, through egress, asked
the run's SHIM SOCKET for the credential — the socket belongs to the run's sandbox uid, so the clone
died "Permission denied" / "could not read Username". BOSS's own git asks its own environment
(runs.OwnGit); the shim helper is for the agent's git only. The last credential.helper the clone
sees is BOSS's own."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from boss_engine.identity.runs import CloneAccess, OwnGit
from boss_engine.services import owned_process
from boss_engine.services.sandbox import EphemeralSandbox

OWN = "!own-helper"
SHIM = "!shim-helper"


def _helpers(argv: list[str]) -> list[str]:
    return [argv[i + 1].split("=", 1)[1] for i, a in enumerate(argv[:-1])
            if a == "-c" and argv[i + 1].startswith("credential.helper=")]


def test_boss_own_clone_never_asks_the_shim(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def run(argv, **kw):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="0" * 40 if "rev-parse" in argv else "", stderr="")

    monkeypatch.setattr(owned_process, "run", run)
    egress = CloneAccess(
        run_id="run_ct:x", clone_url="http://egress:8090/github/o/r.git",
        git_config=(("url.http://egress:8090/github/.insteadOf", "https://github.com/"),
                    ("credential.helper", ""), ("credential.helper", SHIM),
                    ("credential.useHttpPath", "true")),
        own_git=OwnGit(config=(("credential.helper", ""), ("credential.helper", OWN)),
                       env={"BOSS_EGR_CREDENTIAL": "boss_egr_test"}))
    settings = SimpleNamespace(sandbox_clone_depth=1, sandbox_validation_timeout_seconds=60,
                               sandbox_integration_branch="main", sandbox_root=str(tmp_path))
    box = EphemeralSandbox(settings, task_id="t1")
    monkeypatch.setattr(EphemeralSandbox, "_root", lambda self: Path(tmp_path))
    monkeypatch.setattr(EphemeralSandbox, "_write_local_excludes", lambda self: None)
    try:
        box.provision("o", "r", base_branch="main", work_branch="boss/x", egress=egress)
    except Exception:  # noqa: BLE001 — only the clone's argv matters here
        pass
    clone = next(argv for argv in seen if "clone" in argv)
    assert _helpers(clone)[-1] == OWN, clone
    assert SHIM not in _helpers(clone)
