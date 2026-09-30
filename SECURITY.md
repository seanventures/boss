# Security policy

BOSS governs agents that can spend money, change code, and act on infrastructure, so we take security reports seriously and appreciate responsible disclosure.

## Reporting a vulnerability

Email **[boss@sean.net](mailto:boss@sean.net)**. Please do not open a public GitHub issue, discussion, or pull request for a suspected vulnerability.

A useful report includes:

- the affected version or commit
- the impact, and who could exploit it
- reproduction steps that do not touch anyone else's systems
- any mitigation you have already tried
- whether it involves tenant boundaries, approvals, receipts, budgets, secrets, or provider actions

Leave credentials, customer data, and production access details out of the report.

## What happens next

Maintainers acknowledge the report, assess severity, and work with you on a fix and, where appropriate, a coordinated advisory. We do not publish a guaranteed response time, but reports that affect spend controls, approvals, or secrets are prioritized.

## Supported versions

Security fixes land on `main` and in the latest release.

## Not a vulnerability?

For ordinary bugs, use the issue forms described in the [feedback process](docs/community/FEEDBACK_LOOP.md).
