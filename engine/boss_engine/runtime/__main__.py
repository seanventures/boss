"""`python -m boss_engine.runtime`: boss-runtime in a microVM, or on the process backend.

    python -m boss_engine.runtime [--run-plane http://127.0.0.1:7104 | --run-plane-uds D/g_7104.sock]
        --replica rep_… (--vsock | --inbox D/w_7105.sock) [--slots 4] [--runner-module M]
        [--listen VIP:443 [--max-connections 256]]

It serves the WORK INBOX (runtime/inbox.py) and drives each run the host hands it, and on a
replica the TLS LISTENER on the VIP (runtime/listener.py: Agent Protocol + A2A, P8b-9); it never
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
    parser.add_argument("--run-plane", default="http://127.0.0.1:7104",
                        help="the guest gateway's run-plane port (loopback in a VM)")
    parser.add_argument("--run-plane-uds",
                        help="process backend: the guest gateway's run-plane Unix socket "
                             "(<vm dir>/g_7104.sock)")
    parser.add_argument("--replica", required=True)
    parser.add_argument("--inbox", help="the work inbox's Unix socket (process backend)")
    parser.add_argument("--vsock", action="store_true", help="serve the inbox on vsock 7105")
    parser.add_argument("--slots", type=int, default=4)
    parser.add_argument("--listen", help="replicas: serve Agent Protocol + A2A over TLS on "
                                         "HOST:PORT (the VIP, e.g. 203.0.113.17:443; P8b-9)")
    parser.add_argument("--max-connections", type=int, default=256)
    parser.add_argument("--runner-module", action="append", default=[],
                        help="import a module that registers runtime runners (tests, demos)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    import httpx

    import importlib

    from .client import HttpTransport, RunPlaneClient
    from .inbox import Inbox

    for module in args.runner_module:
        importlib.import_module(module)

    drain, stop = threading.Event(), threading.Event()

    def terminate(*_: object) -> None:
        drain.set()

    signal.signal(signal.SIGTERM, terminate)
    transport = httpx.HTTPTransport(uds=args.run_plane_uds) if args.run_plane_uds else None
    with httpx.Client(base_url="http://guest" if args.run_plane_uds else args.run_plane,
                      transport=transport, timeout=60.0) as http:
        client = RunPlaneClient(HttpTransport(http), args.replica)
        inbox = Inbox(client.assigned, total_slots=args.slots, drain=drain)
        if args.vsock:
            inbox.serve_vsock(stop)
        elif args.inbox:
            inbox.serve_unix(args.inbox, stop)
        else:
            parser.error("say where the inbox is: --inbox PATH or --vsock")
        listener = None
        if args.listen:
            from .listener import serve

            host, _, port = args.listen.rpartition(":")
            listener = serve(client, host=host or "0.0.0.0", port=int(port or 443),
                             max_connections=args.max_connections, inbox=inbox, stop=stop)
        drain.wait()
        if listener is not None:
            listener.shutdown()
        inbox.join(timeout=60)
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
