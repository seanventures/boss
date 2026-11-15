"""`python -m boss_engine.runtime`: boss-runtime in a microVM, or on the process backend.

    python -m boss_engine.runtime --run-plane http://127.0.0.1:7104 --replica rep_… \\
        (--vsock | --inbox /run/…/w.sock) [--slots 4]

It serves the WORK INBOX (runtime/inbox.py) and drives each run the host hands it; it never
polls BOSS (§3.13.0: one long-poll per host, kept by boss-hostd). Its run-plane calls go to the
guest gateway's loopback port, which attaches the host's credential and the run's token.
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
    parser.add_argument("--inbox", help="the work inbox's Unix socket (process backend)")
    parser.add_argument("--vsock", action="store_true", help="serve the inbox on vsock 7105")
    parser.add_argument("--slots", type=int, default=4)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    import httpx

    from .client import HttpTransport, RunPlaneClient
    from .inbox import Inbox

    drain, stop = threading.Event(), threading.Event()

    def terminate(*_: object) -> None:
        drain.set()

    signal.signal(signal.SIGTERM, terminate)
    with httpx.Client(base_url=args.run_plane, timeout=60.0) as http:
        client = RunPlaneClient(HttpTransport(http), args.replica)
        inbox = Inbox(client.assigned, total_slots=args.slots, drain=drain)
        if args.vsock:
            inbox.serve_vsock(stop)
        elif args.inbox:
            inbox.serve_unix(args.inbox, stop)
        else:
            parser.error("say where the inbox is: --inbox PATH or --vsock")
        drain.wait()
        inbox.join(timeout=60)
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
