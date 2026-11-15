"""boss-runtime: the in-VM agent runtime (P8a, boss-agent-hosts-lld.md §3.13).

It runs the P1 kernel against the run plane — never a database — and takes its runs through the
pull loop (`loop.serve`: POST /v1/run-plane/next, long-poll). The TLS listener for external
callers (Agent Protocol / A2A on the VIP) lands in P8b. On the `process` backend (laptops, CI)
it runs as an ordinary subprocess: `python -m boss_engine.runtime --help`.
"""
