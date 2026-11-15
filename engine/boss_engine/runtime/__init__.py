"""boss-runtime: the in-VM agent runtime (P8a, boss-agent-hosts-lld.md §3.13).

It runs the P1 kernel against the run plane — never a database. It has NO pull loop: boss-hostd
keeps ONE work request per host (`POST /v1/run-plane/hosts/next`, §3.13.0) and hands each run to
this VM's work inbox (`inbox.py`: vsock 7105, a Unix socket on the process backend). The TLS
listener for external callers (Agent Protocol / A2A on the VIP) lands in P8b. On the `process`
backend (laptops, CI) it runs as an ordinary subprocess: `python -m boss_engine.runtime --help`.
"""
