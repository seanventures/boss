"""`python -m boss_engine.runtime`: boss-runtime on the process backend (no TLS listener yet).

    python -m boss_engine.runtime --run-plane http://127.0.0.1:8000 --replica rep_... \
        --host-cert host.pem --host-key host.key [--slots 4] [--once]

In a microVM, boss-init execs it with --run-plane http://127.0.0.1:7104 (boss-vsockd's loopback
port; boss-hostd attaches the host credential) and no host key: the guest holds none.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="boss-runtime")
    parser.add_argument("--run-plane", required=True)
    parser.add_argument("--replica", required=True)
    parser.add_argument("--host-cert")
    parser.add_argument("--host-key")
    parser.add_argument("--slots", type=int, default=4)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--ttl", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    import httpx

    from .client import HostSigner, HttpTransport, RunPlaneClient
    from .loop import serve

    signer = None
    if args.host_key and args.host_cert:
        from cryptography.hazmat.primitives import serialization

        with open(args.host_key, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        with open(args.host_cert, encoding="ascii") as f:
            signer = HostSigner(key, f.read())
    drain = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: drain.set())
    with httpx.Client(base_url=args.run_plane, timeout=60.0) as http:
        client = RunPlaneClient(HttpTransport(http, signer=signer), args.replica)
        serve(client, slots=args.slots, poll_seconds=args.poll, drain=drain, once=args.once,
              ttl=args.ttl)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
