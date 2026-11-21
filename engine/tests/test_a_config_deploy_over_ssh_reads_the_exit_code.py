"""A CONFIG DEPLOY OVER SSH READS THE HOST'S EXIT CODE (laptop 2026-11-21).

The Northgate API onboarding's first config deploy to a Fleet host (203.0.113.10) failed at its
first command: "'CommandResult' object has no attribute 'code'". SshShell read `.code` from
stack_deploy.HostAccess.run, whose CommandResult carries `returncode`; only LocalShell had ever
been exercised. This runs SshShell through the REAL HostAccess with a fake SSH adapter.
"""
from __future__ import annotations

from types import SimpleNamespace

from boss_engine.services import config_deploy
from boss_engine.services.stack_deploy import HostAccess


class _Adapter:
    def __init__(self, returncode):
        self.returncode, self.calls = returncode, []

    def stack_exec(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(returncode=self.returncode, stdout="BOSS_DIR_OK\n", stderr="")


def _shell(returncode):
    adapter = _Adapter(returncode)
    access = HostAccess(host="192.0.2.10", user="root", port=22, key_material="k",
                        adapter=adapter)
    return config_deploy.SshShell(access, "chg_1"), adapter


def test_the_host_s_exit_code_and_output_come_back():
    sh, _ = _shell(0)
    got = sh.run(config_deploy.preflight_script("/srv/northgate/tenants"))
    assert got.ok and got.code == 0 and "BOSS_DIR_OK" in got.stdout
    failing, _ = _shell(4)
    assert failing.run("true").code == 4 and not failing.run("true").ok


def test_a_write_carries_its_changeset_and_stdin():
    sh, adapter = _shell(0)
    sh.run("cat > f", stdin="tenant: x\n", write=True)
    call = adapter.calls[-1]
    assert call["write"] is True and call["changeset_id"] == "chg_1"
    assert call["stdin_text"] == "tenant: x\n"
