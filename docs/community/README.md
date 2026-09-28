# BOSS Community

BOSS Community runs on your own VM. It keeps commands, tasks, agent runs,
approvals, budgets, and receipts in one local control plane. You connect the
model and infrastructure accounts you want it to use. Cloud connection is
optional.

## Start

The root `README.md` has the one-line installer, SSH access, upgrade, and
uninstall commands. `scripts/install/README.md` covers credentials and
recovery. See [Features](FEATURES.md) for the current Community scope.

In the console, **Agent work** shows recent activity by role. **Team
structure** holds a planned role layout and separately shows actual handoffs.
**Agent Studio** creates and tests agent definitions. Moving a role on the
chart does not change task routing, and adding a template does not start work.

## Extend and contribute

Use the [terminal and API guide](TERMINAL_API.md) to automate the same governed
operations available in the console. The [feedback process](FEEDBACK_LOOP.md)
explains how issues become reviewed changes. See root `CONTRIBUTING.md` before
sending code or documentation, and root `SECURITY.md` for private vulnerability
reporting.

## License

Apache License 2.0 applies to the published Community source; see root
`LICENSE`. Third-party components retain their own terms and notices.
