# Storage cleanup on a selected Docker runtime

The API can detect pressure without the optional host helper. That does not mean
host cleanup is running: the helper must be installed, authenticated and able to
reach the intended Docker daemon. An exhausted plan does not mean disk recovery.

For a non-default runtime, persist the selection in the installation `.env`:

```dotenv
BOSS_DOCKER_CONTEXT=colima-example
```

The default is unchanged: without this setting, the helper uses the normal Docker
CLI environment and context. The explicit setting also works under launchd and
systemd, which do not read an interactive shell profile.

Use `boss identity host-key` if a host key has not been provisioned, then
`boss steward run --sample-only` to verify telemetry before `boss steward install`.
Never publish `.env`, host keys, or helper state with a patch.

Docker build cache is daemon-wide. Automatic pruning requires either a
BOSS-owned `BOSS_BUILDX_BUILDER` or `BOSS_DEDICATED_HOST=1` for a Docker daemon
dedicated to this BOSS installation. Do not set the latter for a shared daemon.
Image and volume label checks, signed jobs, dry-run hashes and approval gates for
irreversible cleanup remain in place. Failed steps are not retried indefinitely.

Fresh helper disk readings take precedence over the API container's fallback
filesystem reading. Forecasts and hysteresis use a single reading source, so a
VM filesystem and its physical host are not treated as alternating measurements
of the same disk. If the helper is stale, API fallback telemetry remains available.

Removing unused Docker cache frees space inside the VM; sparse disk reclamation
on the laptop is runtime-dependent. Verify both filesystems before claiming that
the physical host has recovered. Never delete active workspaces, database volumes,
personal files, or backups simply to dismiss the alert.
