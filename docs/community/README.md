# BOSS Community

BOSS Community runs on your own VM. It keeps commands, tasks, agent runs,
approvals, budgets, and receipts in one local control plane. You connect the
model and infrastructure accounts you want it to use. Cloud connection is
optional.

## Start

The root `README.md` has the one-line installer, SSH access, upgrade, and
uninstall commands. `scripts/install/README.md` covers credentials and
recovery. See [model connections](MODEL_CONNECTIONS.md) for supported provider
paths and [Features](FEATURES.md) for the current Community scope.

In the console, **Agent work** shows recent activity by role. **Team
structure** holds a planned role layout and separately shows actual handoffs.
**Agent Studio** creates and tests agent definitions. Moving a role on the
chart does not change task routing, and adding a template does not start work.
On a new install, the planned layout and your workflow list start empty even
though Agent Studio has starter definitions. Place roles deliberately and add
the optional workflows from `examples/` when you are ready. The header's
Software projects count tracks app builds; the seeded BOSS control project is
selected separately.

## Extend and contribute

Use the [API, terminal, and MCP guide](CONTROL_SURFACES.md) to find and invoke
governed operations. The [feedback process](FEEDBACK_LOOP.md)
explains how issues become reviewed changes. See root `CONTRIBUTING.md` before
sending code or documentation, and root `SECURITY.md` for private vulnerability
reporting.

## License

Apache License 2.0 applies to the published Community source; see root
`LICENSE`. Third-party components retain their own terms and notices.
