"""boss-runner: the sidecar that OWNS coding-agent processes so they survive an API restart (BOSSAPP-828).

Before this, every harness CLI was a child of the API process (services/owned_process.py). An
upgrade SIGTERMed the API, the drain ended every tree, and the next API re-ran the stage from the
last git checkpoint: the in-flight turn was paid for twice. With BOSS_RUNNER_URL set, the API
hands the spawn to this daemon instead (services/runner_dispatch.py), and a restarted API
RE-ATTACHES to the still-running turn.

Pieces:

* `store`      — sqlite in the runner's volume: sessions, the sequenced OUTBOX of turn events
                 (cumulative ACKs from the API trim it) and the COMMAND JOURNAL (start / steer /
                 cancel / close, each applied exactly once by command_id).
* `supervisor` — spawns and ends process groups, pumps their output into the outbox.
* `auth`       — the existing DPoP run-key pattern (identity/dpop.py): the runner's Ed25519 key
                 lives only in memory; the API mints a run token bound to it (cnf.jkt).
* `uplink`     — pushes the outbox to the API (`POST /v1/runner/sessions/{id}/events`), signed
                 with that token and a fresh DPoP proof, and renews the token at /v1/identity/token.
* `server`     — the runner's own HTTP surface, called by the API.
* `daemon`     — wires them together; `python -m apps.runner.boss_runner` runs it.
"""
