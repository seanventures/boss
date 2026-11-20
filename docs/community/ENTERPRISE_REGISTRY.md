# Enterprise npm registry

Set `BOSS_NPM_REGISTRY` to your administrator-approved HTTPS npm registry:

```bash
export BOSS_NPM_REGISTRY=https://packages.example.com/api/npm/npm-virtual/
./install.sh
```

The installer saves the value in the private installation `.env` and preserves it
on upgrades. For an existing installation, set the same variable in `.env`.
Rebuild images when changing the registry used for image builds. Recreate API and
worker when changing the runtime setting, after active work has finished.

The setting applies to community image npm downloads and runtime code-agent and
verification subprocesses on the local substrate. The console agent shell also
receives it. Remote runners need their own equivalent configuration. An unset
value preserves existing npm behavior, normally the public npm registry.

Runtime configuration requires HTTPS without embedded credentials, query strings,
or fragments, and keeps npm certificate verification enabled. Supply credentials
through your approved secret-management mechanism, not this URL. This option does
not implement authenticated registry credential provisioning. An internal registry
with a private CA still needs a trusted CA configuration in the runtime.

Package metadata, tarballs, scoped registries, existing lockfile URLs, install
scripts, and browser downloads can use different endpoints. Validate a complete
dependency installation before assuming all traffic uses the configured registry.
This setting is not an egress policy or an automatic fallback to the public registry.

For fenced local builds and previews, the registry hostname must also be permitted
by the egress service's `BOSS_EGRESS_CONNECT_ALLOW`. That variable replaces the
default host list: retain required existing hosts and add only approved registry
and tarball hosts, then recreate egress. Do not disable the sandbox network fence
or use a wildcard. The CONNECT proxy still rejects non-public addresses.
Validate from a sandboxed build or preview, not only from the API container:
an unsandboxed npm request can succeed while the proxy rejects the same hostname
with HTTP 403. Check the proxy's allowlist before treating that as a bad token.

Keep enterprise URLs, certificates, credentials, and installation `.env` files out
of public patches. To revert, remove the variable from `.env` and the host shell,
then recreate the affected services. Removing it does not repair public-registry
connectivity or rewrite project lockfiles.
