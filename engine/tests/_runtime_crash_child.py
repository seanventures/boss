"""The boss-runtime process the P8a crash test kills with SIGKILL (test_p8a_run_plane.py, (a)).

Usage: P8A_WORK='{"run_id",…,"run_token"}' python _runtime_crash_child.py <run plane url>
           <replica id> <host key pem> <host cert pem> <ledger path>

A replica's runtime on the process backend: the parent (the host) claimed the run with its one
hosts/next and hands it over (P8A_WORK, as the work inbox would, plus the run token the host's
gateway would attach); the child drives the three-tool batch on the kernel with a
RemoteCheckpointStore, signing as its host, and hangs inside charge_card so the parent kills it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

from _durable_support import policies, scripted_batch, toolset  # noqa: E402
from boss_engine import kernel  # noqa: E402
from boss_engine.kernel.scripted import ScriptedEngine  # noqa: E402
from boss_engine.runtime import runner  # noqa: E402
from boss_engine.runtime.client import HostSigner, HttpTransport, RunPlaneClient  # noqa: E402
from boss_engine.runtime.remote import RemoteSink  # noqa: E402


def main() -> int:
    url, replica, key_path, cert_path, ledger = sys.argv[1:6]

    def build(package, handle):
        return kernel.RunSpec(engine=ScriptedEngine(scripted_batch()),
                              tools=toolset(Path(ledger), hang=True), policies=policies(),
                              project_id="", system="You are a billing agent.",
                              messages=[m for m in package["run"]["messages"]
                                        if m.get("role") != "system"],
                              sink=RemoteSink(handle), store=None)

    runner.RUNNERS["crash_p8"] = build
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    with httpx.Client(base_url=url, timeout=30.0) as http:
        client = RunPlaneClient(HttpTransport(http, signer=HostSigner(
            key, Path(cert_path).read_text())), replica)
        handle = client.assigned(json.loads(os.environ["P8A_WORK"]))
        runner.drive(handle, handle.package())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
