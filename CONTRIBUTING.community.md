# Contributing to BOSS Community

Thanks for helping improve BOSS. Start with the [feedback process](docs/community/FEEDBACK_LOOP.md) for bugs, installation problems, and feature ideas. Search existing issues before opening a new one. Include the BOSS version or commit, your OS and architecture, steps to reproduce, expected behavior, and what happened. Remove secrets and customer data from logs and screenshots.

## Make a change

1. Open or reference an issue so the intended behavior and owner are clear.
2. Keep a pull request focused. Explain the user-facing change, tests run, and any migration or compatibility impact.
3. Add tests for state transitions and for budget, receipt, lease, and tenant boundaries when the change touches them. Run the relevant tests and lint checks before requesting review.
4. Update the user guide or API contract when behavior changes. A new UI action should have a matching API permission check and audit event.

BOSS treats provider calls, spending, and external writes as governed effects. Route them through an adapter, reserve budget before spend, and record a receipt with a dedupe key. Do not commit credentials or use a public issue to report a vulnerability; email [boss@sean.net](mailto:boss@sean.net) and see [Security](SECURITY.md).

Maintainers review scope, safety, tests, and documentation before merging. A bot acknowledgement or draft plan does not authorize work. Community behavior and limits are described in the [README](README.md); the [Code of Conduct](CODE_OF_CONDUCT.md) applies to all project spaces.
