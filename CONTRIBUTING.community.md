# Contributing to BOSS

Thanks for helping build BOSS. Bug reports, documentation fixes, and code changes are all welcome, and every one of them lands the same way: through a GitHub issue and a pull request.

## Start with an issue

Search existing issues first. If nothing matches, open one with the bug, install, or feature form, and include what you were doing, what you expected, how you installed, and which edition you run. The [feedback process](docs/community/FEEDBACK_LOOP.md) describes how a report moves from triage to a merged change.

Strip secrets, tokens, and customer data from any logs or screenshots before you post them.

> **Found a vulnerability?** Do not open a public issue. Email [boss@sean.net](mailto:boss@sean.net) and see [SECURITY.md](SECURITY.md).

## Set up a development environment

You need Node 20 with pnpm 9, Python 3.11 or newer, and Docker with Compose. The fastest way to a running stack is the installer from your clone:

```sh
git clone https://github.com/<you>/boss.git && cd boss
./install.sh --dir "$PWD"
```

`make help` lists the day-to-day targets (`make dev`, `make status`, `make check`, `make stop`). To run the test and lint suites from a local Python virtual environment:

```sh
python3 -m venv .venv && ./.venv/bin/pip install -r apps/api/requirements.txt -r apps/worker/requirements.txt -r engine/requirements.txt
pnpm install
pnpm test
pnpm lint
```

## Make the change

1. **Fork and branch.** Use a focused branch per change.
2. **Keep it small.** A reviewable pull request does one thing. Say what a user can now do differently, which tests you ran, and any migration or compatibility impact.
3. **Test state changes.** Add a test for every state machine transition you touch.
4. **Update the docs.** If behavior changes, update the user guide under `docs/community/` or the API contract in the same pull request.

## Ground rules for the control plane

BOSS exists to make agent work safe and auditable, so a few rules are non-negotiable:

- **Spend needs a reservation.** Anything that can cost money reserves budget before it runs.
- **Effects need a receipt.** Anything that writes outside BOSS goes through the existing adapter boundary and records a receipt with a unique dedupe key.
- **Work needs a lease.** Worker jobs hold a lease with a TTL and heartbeat.
- **Failures stay visible.** Retry, dead-letter, or escalate; never swallow an error.
- **The console follows the API.** A new console action needs a matching API permission check and an audit event.
- **Scope stays enforced.** Changes that read or write project data respect the tenant boundary.

If your change can spend money or write outside BOSS, include tests that cover the reservation, the receipt, the lease, and the tenant boundary. Never commit credentials.

Installing an agent definition or example workflow starts no work, and docs and code should describe it that way.

## Review

Maintainers review each pull request for scope, safety, tests, and documentation before merging. An acknowledged issue or a draft plan is not approval to start the work, so check in on the issue before a large change.

Everyone in project spaces follows the [Code of Conduct](CODE_OF_CONDUCT.md). For what BOSS does from a user's point of view, see the [README](README.md) and the [documentation home](docs/community/README.md).

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
