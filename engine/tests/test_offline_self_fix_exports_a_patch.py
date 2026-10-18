"""AN OFFLINE INSTALL EXPORTS THE FIX (BOSSAPP-571 R18, spec §6.2): a .tar.gz the person keeps —
the fix as `git am` reads it, the redacted evidence, and a README that says how to send it and
what the privacy check of the diff found. It never leaves by itself."""
from __future__ import annotations

import io
import subprocess
import tarfile

from boss_engine.services import self_fix_upstream as up

from ._self_fix_upstream_support import insert_verified
from ._self_fix_upstream_support import settings as _settings
from ._self_fix_support import _patch, clean_project, git, mini_boss
from .test_workflow_capability_runner import eng, project  # noqa: F401


def test_the_export_holds_the_patch_the_evidence_and_the_check(eng, project, monkeypatch,  # noqa: F811
                                                               tmp_path):
    monkeypatch.setattr(up, "_names", lambda c, p: [("NAME", "acme-payroll")])
    repo, _base = mini_boss(tmp_path)
    diff = _patch(repo, {"engine/boss_engine/services/widget.py":
                         '"""A widget BOSS renders."""\n\n\ndef double(x):\n    """Twice x."""\n'
                         "    return x * 2\n\n\ndef label(x):\n"
                         "    return f\"widget {double(x)}\"\n"})
    sfx_id = insert_verified(eng, project, diff=diff, symptom="acme-payroll showed 5 for 3")
    try:
        with eng.begin() as c:
            body, name = up.export(c, _settings(), sfx_id, actor="usr_test")
        members = {m.name: m for m in tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")}
        assert set(members) == {"fix.patch", "evidence.md", "README.txt"}
        read = lambda n: tarfile.open(fileobj=io.BytesIO(body), mode="r:gz").extractfile(  # noqa: E731
            n).read().decode()
        patch, evidence, readme = read("fix.patch"), read("evidence.md"), read("README.txt")
        assert patch.startswith("From ") and "Subject: [PATCH] fix(deploy)" in patch
        assert "BOSS-Approved: yes" in patch and "BOSS-Signature: deploy:port_held@" in patch
        assert "acme-payroll" not in evidence
        assert "nothing private found" in readme and "git am" in readme
        # `git am` takes it on the release it was made on.
        (tmp_path / "fix.patch").write_text(patch)
        git(repo, "am", str(tmp_path / "fix.patch"))
        assert "x * 2" in (repo / "engine/boss_engine/services/widget.py").read_text()
        assert "BOSS-Signature" in subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo,
                                                  capture_output=True, text=True).stdout
    finally:
        clean_project(eng, project)
